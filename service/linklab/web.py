"""Flask speed-control web application."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request

from .flows import FlowManager
from .model import LabConfig
from .runner import SubprocessRunner
from .stub import StubTcRunner
from .tc_argv import Topology
from .tc_manager import TcManager


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _writable_run_dir() -> Path:
    preferred = Path(os.environ.get("LAB_RUN_DIR", "/run/lab"))
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return preferred
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "linklab"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def create_app(
    manager: TcManager | None = None,
    flows: FlowManager | None = None,
    start_runtime: bool | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    app.json.ensure_ascii = False  # Flask >= 2.3 provider

    run_dir = _writable_run_dir()
    topo = Topology.from_env()

    if manager is None:
        backend = os.environ.get("LAB_BACKEND", "real").strip().lower()
        state_path = Path(
            os.environ.get("LAB_STATE_PATH", str(run_dir / "lab_config.json"))
        )
        if backend == "stub":
            runner: Any = StubTcRunner(dev=topo.dev)
        else:
            runner = SubprocessRunner()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        manager = TcManager(topo, runner, state_path)

    if start_runtime is None:
        start_runtime = _env_bool("LAB_START_FLOWS", True)

    if flows is None and start_runtime:
        try:
            flows = FlowManager(run_dir=run_dir, topo=topo, start_receiver=True)
        except Exception as e:  # pragma: no cover - defensive
            app.logger.warning("FlowManager unavailable: %s", e)
            try:
                if flows is not None:
                    flows.stop_all()
            except Exception:
                pass
            flows = None

    app.extensions["lab_manager"] = manager
    app.extensions["lab_flows"] = flows

    # Graceful shutdown of child processes.
    if flows is not None:
        import atexit

        atexit.register(flows.stop_all)

    # ------------------------------------------------------------------
    @app.get("/")
    def index() -> str:
        return render_template(
            "index.html",
            initial_config=manager.current().to_dict(),
            backend=os.environ.get("LAB_BACKEND", "real"),
            topo=manager.topo,
        )

    @app.get("/api/state")
    def api_state() -> Any:
        cfg = manager.current()
        return jsonify(
            {
                "config": cfg.to_dict(),
                "backend": os.environ.get("LAB_BACKEND", "real"),
                "topology": {
                    "src_netns": manager.topo.src_netns,
                    "dst_netns": manager.topo.dst_netns,
                    "dev": manager.topo.dev,
                    "dst_ip": manager.topo.dst_ip,
                },
            }
        )

    @app.post("/api/config/validate")
    def api_validate() -> Any:
        body = request.get_json(silent=True) or request.form.to_dict()
        try:
            candidate = LabConfig.from_form(body)
        except (ValueError, KeyError, TypeError) as e:
            return jsonify({"valid": False, "errors": [str(e)], "plan": None}), 400
        return jsonify(manager.preview(candidate))

    @app.post("/api/config/apply")
    def api_apply() -> Any:
        body = request.get_json(silent=True) or {}
        # Also accept classic form posts.
        if not body and request.form:
            body = request.form.to_dict()
        try:
            candidate = LabConfig.from_form(body)
        except (ValueError, KeyError, TypeError) as e:
            return jsonify({"ok": False, "reason": f"输入无效: {e}"}), 400

        fault_step = body.get("fault_step")
        if fault_step in ("", None):
            fault_step = None
        else:
            try:
                fault_step = int(fault_step)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "reason": "fault_step 必须是整数"}), 400
        fault_mode = str(body.get("fault_mode", "stub")).lower()
        if fault_mode not in ("stub", "real"):
            fault_mode = "stub"

        result = manager.apply(
            candidate, fault_step=fault_step, fault_mode=fault_mode
        )
        status = 200 if result.get("ok") else 409
        return jsonify(result), status

    @app.post("/api/demo/failure")
    def api_demo_failure() -> Any:
        result = manager.demo_failure()
        return jsonify(result), 200 if result.get("ok") else 409

    @app.get("/api/kernel")
    def api_kernel() -> Any:
        return jsonify(manager.kernel_report())

    @app.get("/api/history")
    def api_history() -> Any:
        return jsonify({"items": manager.history_list()})

    # ------------------------------------------------------------------
    @app.get("/api/flows")
    def api_flows_status() -> Any:
        if flows is None:
            return jsonify({"flows": {}, "receiver_alive": False, "disabled": True})
        return jsonify(flows.status())

    @app.post("/api/flows/<key>/start")
    def api_flow_start(key: str) -> Any:
        if flows is None:
            return jsonify({"started": False, "reason": "流量管理器未启用"}), 503
        body = request.get_json(silent=True) or {}
        rate = body.get("rate_mbps", request.args.get("rate_mbps", 12))
        try:
            result = flows.start_flow(key, rate=rate)
        except KeyError:
            return jsonify({"started": False, "reason": f"未知流: {key}"}), 404
        except ValueError as e:
            return jsonify({"started": False, "reason": str(e)}), 400
        return jsonify(result), 200 if result.get("started") else 409

    @app.post("/api/flows/<key>/stop")
    def api_flow_stop(key: str) -> Any:
        if flows is None:
            return jsonify({"stopped": True, "disabled": True})
        try:
            return jsonify(flows.stop_flow(key))
        except KeyError:
            return jsonify({"stopped": False, "reason": f"未知流: {key}"}), 404

    @app.post("/api/flows/<key>/remark")
    def api_flow_remark(key: str) -> Any:
        """Switch the DSCP marker of a running flow exactly once per trial."""
        if key not in ("alarm", "video"):
            return jsonify({"ok": False, "reason": f"未知流: {key}"}), 404
        if flows is None:
            return jsonify({"ok": False, "reason": "流量管理器未启用"}), 503
        body = request.get_json(silent=True) or request.form.to_dict()
        result = flows.remark_flow(key, body.get("new_dscp"))
        return jsonify(result), 200 if result.get("ok") else 409

    @app.post("/api/flows/stop-all")
    def api_flows_stop_all() -> Any:
        if flows is not None:
            flows.stop_all()
            # Re-start the receiver so the page keeps measuring zero.
            flows.start_receiver()
        return jsonify({"ok": True})

    return app


# `waitress-serve ... linklab.web:app`
app = create_app()


if __name__ == "__main__":  # pragma: no cover
    app.run(host="0.0.0.0", port=8080, threaded=True)
