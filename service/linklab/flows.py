"""Process manager for the two UDP test flows and the receiver."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .tc_argv import FLOW_PORT, Topology
from .units import parse_offered_rate

FLOW_META = {
    "alarm": {"label": "告警流", "port": FLOW_PORT["alarm"], "classid": "1:10"},
    "video": {"label": "录像流", "port": FLOW_PORT["video"], "classid": "1:20"},
}

_SCRIPT_DIR = Path(__file__).resolve().parent


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
        self.receiver: subprocess.Popen | None = None
        self.recv_stats = self.run_dir / "recv_stats.json"
        if start_receiver:
            self.start_receiver()

    # ------------------------------------------------------------------
    def _prefix(self, netns: str | None) -> list[str]:
        return ["ip", "netns", "exec", netns] if netns else []

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
                ]
                + flow_args
            )
            log = open(self.run_dir / "receiver.log", "ab", buffering=0)
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
            log = open(log_path, "ab", buffering=0)
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
                return {
                    "started": False,
                    "reason": f"发送进程启动后立即退出 rc={proc.returncode}",
                    "log_tail": tail,
                    "argv": argv,
                }
            return {"started": True, "pid": proc.pid, "argv": argv}

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
                }
            return out
