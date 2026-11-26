"""Command execution abstraction.

The real backend shells out to ``ip``/``tc`` via :class:`SubprocessRunner`.
Tests plug in an in-memory runner so the full apply -> fail -> rollback
flow can be exercised without ``CAP_NET_ADMIN``.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol


@dataclass
class CmdResult:
    argv: list[str]
    rc: int
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    skipped: bool = False
    fault_injected: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def display_argv(self) -> str:
        return " ".join(_quote(a) for a in self.argv)

    def to_dict(self) -> dict:
        return {
            "argv": self.argv,
            "cmd": self.display_argv(),
            "rc": self.rc,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "ok": self.ok,
            "skipped": self.skipped,
            "fault_injected": self.fault_injected,
        }


def _quote(token: str) -> str:
    if token and all(c.isalnum() or c in "/:._-=" for c in token):
        return token
    return "'" + token.replace("'", "'\\''") + "'"


class Runner(Protocol):
    def run(self, argv: list[str]) -> CmdResult: ...


class SubprocessRunner:
    """Runs real commands with a timeout and captured output."""

    def __init__(self, timeout_s: float = 20.0):
        self.timeout_s = timeout_s

    def run(self, argv: list[str]) -> CmdResult:
        start = time.monotonic()
        try:
            p = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
            return CmdResult(
                argv=list(argv),
                rc=p.returncode,
                stdout=p.stdout.strip(),
                stderr=p.stderr.strip(),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except subprocess.TimeoutExpired as e:
            return CmdResult(
                argv=list(argv),
                rc=124,
                stdout=(e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""),
                stderr=f"命令超时（>{self.timeout_s}s）",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except FileNotFoundError as e:
            return CmdResult(
                argv=list(argv),
                rc=127,
                stdout="",
                stderr=f"命令不存在: {e.filename or argv[0]}（请先安装 iproute2）",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except Exception as e:  # pragma: no cover - defensive
            return CmdResult(
                argv=list(argv),
                rc=1,
                stdout="",
                stderr=f"执行异常: {type(e).__name__}: {e}",
                duration_ms=int((time.monotonic() - start) * 1000),
            )


class FakeRunner:
    """Test double.

    ``handler`` may return a :class:`CmdResult`, raise, or return ``None``
    to accept the command (rc=0, empty output).  Commands are recorded.
    """

    def __init__(
        self,
        handler: Callable[[list[str]], CmdResult | None] | None = None,
    ):
        self.handler = handler
        self.calls: list[list[str]] = []

    def run(self, argv: list[str]) -> CmdResult:
        self.calls.append(list(argv))
        if self.handler is not None:
            res = self.handler(list(argv))
            if res is not None:
                if not res.argv:
                    res.argv = list(argv)
                return res
        return CmdResult(argv=list(argv), rc=0)
