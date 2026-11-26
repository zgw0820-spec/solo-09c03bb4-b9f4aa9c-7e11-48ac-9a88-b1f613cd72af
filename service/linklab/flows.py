"""Process manager for the two UDP test flows and the receiver."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .dscp import validate_new_dscp
from .tc_argv import FLOW_PORT, Topology
from .units import parse_offered_rate

FLOW_META = {
    "alarm": {"label": "告警流", "port": FLOW_PORT["alarm"], "classid": "1:10"},
    "video": {"label": "录像流", "port": FLOW_PORT["video"], "classid": "1:20"},
}

_SCRIPT_DIR = Path(__file__).resolve().parent

# The sender publishes its boundary within roughly this many seconds;
# polling is cheap (one small JSON read) and bounded by this deadline.
REMARK_WAIT_TIMEOUT_S = 3.0
REMARK_POLL_INTERVAL_S = 0.05


def _atomic_write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class FlowManager:
    def __init__(
        self,
        run_dir: Path | str,
        topo: Topology,
        python_exe: str | None = None,
        start_receiver: bool = True,
    ):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.topo = topo
        self.python_exe = python_exe or sys.executable
        self._lock = threading.RLock()
        self._senders: dict[str, subprocess.Popen] = {}
        self._logs: dict[str, Path] = {}
        self._log_files: dict[str, Any] = {}
        self.receiver: subprocess.Popen | None = None
        self._receiver_log: Any = None
        self.recv_stats = self.run_dir / "recv_stats.json"
        # Per-flow re-mark bookkeeping (in-memory; one trial per start).
        self._remark_state: dict[str, dict[str, Any]] = {}
        if start_receiver:
            self.start_receiver()

    # ------------------------------------------------------------------
    def _prefix(self, netns: str | None) -> list[str]:
        return ["ip", "netns", "exec", netns] if netns else []

    def _ctl_file(self, key: str) -> Path:
        return self.run_dir / f"mark_ctl_{key}.json"

    def _plan_file(self, key: str) -> Path:
        return self.run_dir / f"mark_plan_{key}.json"

    def _reset_flow_marking_files(self, key: str) -> None:
        """Remove stale marking files so a new trial starts clean."""
        for p in (self._ctl_file(key), self._plan_file(key)):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        self._remark_state[key] = {
            "request_id": None,
            "new_dscp": None,
            "applied": False,
            "boundary_seq": None,
            "error": None,
            "updated_at": None,
        }

    def start_receiver(self) -> dict[str, Any]:
        with self._lock:
            if self.receiver is not None and self.receiver.poll() is None:
                return {"started": False, "reason": "receiver already running"}
            flow_args: list[str] = []
            for key in FLOW_META:
                flow_args += ["--flow", f"{key}:{FLOW_META[key]['port']}"]
            argv = (
                self._prefix(self.topo.dst_netns)
                + [
                    self.python_exe,
                    str(_SCRIPT_DIR / "udp_recv.py"),
                    "--stats-file",
                    str(self.recv_stats),
                    "--plan-dir",
                    str(self.run_dir),
                ]
                + flow_args
            )
            log = open(self.run_dir / "receiver.log", "ab", buffering=0)
            self._receiver_log = log
            self.receiver = subprocess.Popen(
                argv,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            # Give the sockets a moment to bind.
            time.sleep(0.4)
            return {"started": True, "pid": self.receiver.pid, "argv": argv}

    # ------------------------------------------------------------------
    def start_flow(self, key: str, rate: str | float | int = 12) -> dict[str, Any]:
        if key not in FLOW_META:
            raise KeyError(f"unknown flow {key!r}")
        with self._lock:
            p = self._senders.get(key)
            if p is not None and p.poll() is None:
                return {"started": False, "reason": f"{key} 已在发送中", "pid": p.pid}

            # Validate rate early.
            parse_offered_rate(rate)
            rate_text = rate if isinstance(rate, str) else f"{rate}mbit"
            stats_file = self.run_dir / f"sender_{key}.json"
            log_path = self.run_dir / f"sender_{key}.log"
            # A fresh round: every trial starts again at DSCP 0 / ECT(0).
            self._reset_flow_marking_files(key)
            log = open(log_path, "ab", buffering=0)
            self._log_files[key] = log
            argv = (
                self._prefix(self.topo.src_netns)
                + [
                    self.python_exe,
                    str(_SCRIPT_DIR / "udp_send.py"),
                    "--dst-ip",
                    self.topo.dst_ip,
                    "--port",
                    str(FLOW_META[key]["port"]),
                    "--flow",
                    key,
                    "--flow-id",
                    "1" if key == "alarm" else "2",
                    "--rate",
                    rate_text,
                    "--stats-file",
                    str(stats_file),
                    "--ctl-file",
                    str(self._ctl_file(key)),
                ]
            )
            proc = subprocess.Popen(
                argv,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._senders[key] = proc
            self._logs[key] = log_path
            time.sleep(0.25)
            if proc.poll() is not None:
                tail = ""
                try:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
                except OSError:
                    pass
                log.close()
                self._log_files.pop(key, None)
                return {
                    "started": False,
                    "reason": f"发送进程启动后立即退出 rc={proc.returncode}",
                    "log_tail": tail,
                    "argv": argv,
                }
            return {"started": True, "pid": proc.pid, "argv": argv}

    # ------------------------------------------------------------------
    def remark_flow(self, key: str, new_dscp: Any) -> dict[str, Any]:
        """Ask a running sender to switch DSCP once, mid-flight.

        On success the boundary sequence is published both to the caller
        and to the receiver's plan file.  On failure the sender keeps the
        old marker (DSCP 0 / ECT(0)), the reason is returned and no plan
        file is written, so the receiver keeps verifying the single old
        segment while traffic continues.
        """
        if key not in FLOW_META:
            raise KeyError(f"unknown flow {key!r}")
        try:
            dscp = validate_new_dscp(new_dscp)
        except ValueError as e:
            return {"ok": False, "reason": str(e)}

        with self._lock:
            proc = self._senders.get(key)
            alive = bool(proc and proc.poll() is None)
            if not alive:
                return {
                    "ok": False,
                    "reason": f"{key} 发送进程未运行，请先开始试发（每轮从 DSCP 0 / ECT(0) 开始）",
                }
            state = self._remark_state.setdefault(
                key,
                {
                    "request_id": None,
                    "new_dscp": None,
                    "applied": False,
                    "boundary_seq": None,
                    "error": None,
                    "updated_at": None,
                },
            )
            if state["applied"]:
                return {
                    "ok": False,
                    "reason": (
                        "本轮试发已经换过一次标（每轮仅允许途中切换一次）；"
                        "请停止后重新开始，序号将从 0 重新计"
                    ),
                    "boundary_seq": state["boundary_seq"],
                }
            if state["request_id"] is not None and state["error"] is None:
                return {"ok": False, "reason": "上一条换标请求仍在处理中"}

            request_id = uuid.uuid4().hex
            state.update(
                {
                    "request_id": request_id,
                    "new_dscp": dscp,
                    "applied": False,
                    "boundary_seq": None,
                    "error": None,
                    "updated_at": time.time(),
                }
            )
            _atomic_write_json(
                self._ctl_file(key),
                {"request_id": request_id, "new_dscp": dscp},
            )

            stats_file = self.run_dir / f"sender_{key}.json"
            deadline = time.time() + REMARK_WAIT_TIMEOUT_S
            while time.time() < deadline:
                doc = self._read_json(stats_file)
                marking = (doc or {}).get("marking") or {}
                if marking.get("remark_request_id") == request_id:
                    boundary = marking.get("boundary_seq")
                    if boundary is not None:
                        # Success: publish the split point for the receiver.
                        state.update(
                            applied=True,
                            boundary_seq=int(boundary),
                            error=None,
                            updated_at=time.time(),
                        )
                        _atomic_write_json(
                            self._plan_file(key),
                            {
                                "flow": key,
                                "request_id": request_id,
                                "old_dscp": 0,
                                "new_dscp": dscp,
                                "ecn": marking.get("ecn"),
                                "boundary_seq": int(boundary),
                                "published_at": time.time(),
                            },
                        )
                        return {
                            "ok": True,
                            "flow": key,
                            "request_id": request_id,
                            "old_dscp": 0,
                            "new_dscp": dscp,
                            "ecn": marking.get("ecn"),
                            "boundary_seq": int(boundary),
                        }
                pending_err = (
                    marking.get("remark_error")
                    if marking.get("pending_request_id") == request_id
                    else None
                )
                if pending_err:
                    state.update(error=pending_err, updated_at=time.time())
                    return {
                        "ok": False,
                        "reason": f"换标失败，继续使用旧标记 DSCP 0 / ECT(0): {pending_err}",
                        "request_id": request_id,
                    }
                time.sleep(REMARK_POLL_INTERVAL_S)

            reason = "等待发送端确认换标超时，发送端可能未读取控制文件，继续使用旧标记"
            state.update(error=reason, updated_at=time.time())
            return {"ok": False, "reason": reason, "request_id": request_id}

    def stop_flow(self, key: str) -> dict[str, Any]:
        if key not in FLOW_META:
            raise KeyError(f"unknown flow {key!r}")
        with self._lock:
            proc = self._senders.get(key)
            if proc is None or proc.poll() is not None:
                self._senders.pop(key, None)
                return {"stopped": True, "was_running": False}
            self._terminate(proc)
            self._senders.pop(key, None)
            log_f = self._log_files.pop(key, None)
            if log_f is not None:
                try:
                    log_f.close()
                except OSError:
                    pass
            # Remove the per-round marking files so a later start begins a
            # clean trial (receiver drops the plan when the file is gone).
            self._reset_flow_marking_files(key)
            return {"stopped": True, "was_running": True}

    def stop_all(self) -> None:
        with self._lock:
            for key in list(self._senders):
                try:
                    self.stop_flow(key)
                except Exception:
                    pass
            if self.receiver is not None and self.receiver.poll() is None:
                self._terminate(self.receiver)
            self.receiver = None
            if self._receiver_log is not None:
                try:
                    self._receiver_log.close()
                except OSError:
                    pass
                self._receiver_log = None

    @staticmethod
    def _terminate(proc: subprocess.Popen, timeout: float = 2.0) -> None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass

    # ------------------------------------------------------------------
    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            recv = self._read_json(self.recv_stats) or {"flows": {}}
            recv_flows = recv.get("flows", {})
            out: dict[str, Any] = {
                "receiver_alive": (
                    self.receiver is not None and self.receiver.poll() is None
                ),
                "receiver_pid": self.receiver.pid if self.receiver else None,
                "recv_updated_at": recv.get("updated_at"),
                "flows": {},
            }
            for key, meta in FLOW_META.items():
                proc = self._senders.get(key)
                alive = bool(proc and proc.poll() is None)
                if proc is not None and not alive:
                    self._senders.pop(key, None)
                sender_stats = self._read_json(
                    self.run_dir / f"sender_{key}.json"
                )
                rx = recv_flows.get(key) or recv_flows.get(str(meta["port"])) or {}
                out["flows"][key] = {
                    **meta,
                    "running": alive,
                    "pid": proc.pid if alive else None,
                    "sender": sender_stats,
                    "rx": rx,
                    "marking_ctl": dict(self._remark_state.get(key) or {}),
                }
            return out
