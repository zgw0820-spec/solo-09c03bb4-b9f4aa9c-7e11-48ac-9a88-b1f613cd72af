"""Stateful tc manager: preview, safe apply, failure injection, rollback."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .model import DEFAULT_CONFIG, LabConfig, invariant_snapshot
from .planner import Plan, PlanError, build_plan
from .runner import CmdResult, Runner
from .tc_argv import Topology, argv_for_operation, kernel_report_argv

# Appended to a real tc argv for the "genuine bad command" fault demo.
# iproute2 rejects the unknown token during parsing, before any change.
FAULT_TOKEN = "__lab_inject_failure__"


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config(path: Path) -> LabConfig:
    with path.open("r", encoding="utf-8") as f:
        return LabConfig.from_dict(json.load(f))


class TcManager:
    def __init__(
        self,
        topo: Topology,
        runner: Runner,
        state_path: Path | str,
        initial: LabConfig = DEFAULT_CONFIG,
        history_size: int = 10,
    ):
        self.topo = topo
        self.runner = runner
        self.state_path = Path(state_path)
        self._lock = threading.RLock()
        if self.state_path.exists():
            try:
                self.state = load_config(self.state_path)
            except Exception:
                self.state = initial
        else:
            self.state = initial
            self._persist_locked()
        self.history: deque[dict[str, Any]] = deque(maxlen=history_size)

    # ------------------------------------------------------------------
    def _persist_locked(self) -> None:
        atomic_write_json(self.state_path, self.state.to_dict())

    def current(self) -> LabConfig:
        with self._lock:
            return self.state

    def preview(self, new_cfg: LabConfig) -> dict[str, Any]:
        """Validate + build the plan without running anything."""
        try:
            plan = build_plan(self.state, new_cfg)
        except PlanError as e:
            return {"valid": False, "errors": e.errors, "plan": None}
        return {
            "valid": True,
            "errors": [],
            "plan": self._plan_with_argv(plan),
            "current_invariant": invariant_snapshot(self.state),
            "target_invariant": invariant_snapshot(new_cfg),
        }

    def _plan_with_argv(self, plan: Plan) -> dict[str, Any]:
        d = plan.to_dict()
        for step, pstep in zip(d["steps"], plan.steps):
            step["argv"] = argv_for_operation(pstep.operation, self.topo)
            step["cmd"] = CmdResult(step["argv"], 0).display_argv()
        return d

    # ------------------------------------------------------------------
    def apply(
        self,
        new_cfg: LabConfig,
        fault_step: int | None = None,
        fault_mode: str = "stub",
    ) -> dict[str, Any]:
        """Apply ``new_cfg`` stepwise; on any failure restore ``self.state``.

        ``fault_step`` is 1-based over the forward plan.
        ``fault_mode``:
        * ``"stub"`` -- synthesise a failed command (works without tc);
        * ``"real"`` -- append an invalid token so real tc exits non-zero.
        """
        started = time.time()
        with self._lock:
            before = self.state
            try:
                plan = build_plan(before, new_cfg)
            except PlanError as e:
                return {
                    "ok": False,
                    "rejected": True,
                    "reason": "目标配置未通过安全校验，未执行任何命令: "
                    + "; ".join(e.errors),
                    "errors": e.errors,
                    "steps": [],
                    "rollback": None,
                    "config_before": before.to_dict(),
                    "config_after": before.to_dict(),
                }

            if fault_step is not None:
                if not plan.steps or not isinstance(fault_step, int) or not (
                    1 <= fault_step <= len(plan.steps)
                ):
                    return {
                        "ok": False,
                        "rejected": True,
                        "reason": (
                            f"故障注入步号非法: {fault_step!r}，"
                            + (
                                "目标配置与当前相同，计划为 0 步，无中途步骤可失败"
                                if not plan.steps
                                else f"本次计划共 {len(plan.steps)} 步"
                            )
                        ),
                        "errors": ["fault_step out of range or empty plan"],
                        "steps": [],
                        "rollback": None,
                        "config_before": before.to_dict(),
                        "config_after": before.to_dict(),
                    }

            step_records: list[dict[str, Any]] = []
            failed_record: dict[str, Any] | None = None

            for pstep in plan.steps:
                argv = argv_for_operation(pstep.operation, self.topo)
                inject = fault_step == pstep.seq
                exec_argv = list(argv)
                if inject and fault_mode == "real":
                    exec_argv = exec_argv + [FAULT_TOKEN]

                if inject and fault_mode == "stub":
                    res = CmdResult(
                        argv=exec_argv,
                        rc=1,
                        stdout="",
                        stderr=(
                            f"【故障注入】模拟第 {pstep.seq} 步 tc 变更命令失败，"
                            "内核返回: RTNETLINK answers: Connection reset by peer"
                        ),
                        fault_injected=True,
                    )
                else:
                    res = self.runner.run(exec_argv)
                    if inject:
                        res.fault_injected = True

                record = {
                    "seq": pstep.seq,
                    "note": pstep.operation.note,
                    "phase": pstep.operation.phase,
                    "target": pstep.operation.target,
                    "effective_total_kbps": pstep.effective_total_kbps,
                    "planned_invariant_ok": pstep.invariant_ok,
                    "config_after": pstep.after.to_dict(),
                    **res.to_dict(),
                }
                step_records.append(record)

                if not res.ok:
                    failed_record = record
                    break

                # Command succeeded -> kernel now matches this step.
                self.state = pstep.after

            if failed_record is None:
                # All forward steps succeeded.
                self._persist_locked()
                result = {
                    "ok": True,
                    "reason": None,
                    "steps": step_records,
                    "failure": None,
                    "rollback": None,
                    "fault": (
                        {"step": fault_step, "mode": fault_mode}
                        if fault_step is not None
                        else None
                    ),
                    "config_before": before.to_dict(),
                    "target": new_cfg.to_dict(),
                    "config_after": self.state.to_dict(),
                    "invariant": invariant_snapshot(self.state),
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
                self.history.appendleft(result)
                return result

            # ----------------------------------------------------------
            # Failure -> roll back from last-known-good state to `before`,
            # using the same invariant-safe planner.
            # ----------------------------------------------------------
            reason = self._failure_reason(failed_record)
            rollback = self._rollback_locked(before)

            result = {
                "ok": False,
                "rejected": False,
                "reason": reason,
                "steps": step_records,
                "failure": {
                    "seq": failed_record["seq"],
                    "cmd": failed_record["cmd"],
                    "rc": failed_record["rc"],
                    "stderr": failed_record["stderr"],
                    "fault_injected": failed_record["fault_injected"],
                },
                "rollback": rollback,
                "fault": (
                    {"step": fault_step, "mode": fault_mode}
                    if fault_step is not None
                    else None
                ),
                "config_before": before.to_dict(),
                "target": new_cfg.to_dict(),
                "config_after": self.state.to_dict(),
                "invariant": invariant_snapshot(self.state),
                "elapsed_ms": int((time.time() - started) * 1000),
            }
            self.history.appendleft(result)
            return result

    def _failure_reason(self, record: dict[str, Any]) -> str:
        stderr = (record.get("stderr") or "").strip()
        head = f"第 {record['seq']} 步变更命令失败（rc={record['rc']}）"
        if record.get("fault_injected"):
            head = f"第 {record['seq']} 步被注入故障，命令失败（rc={record['rc']}）"
        if stderr:
            return f"{head}；原因: {stderr}"
        return head

    def _rollback_locked(self, before: LabConfig) -> dict[str, Any]:
        current = self.state
        try:
            rb_plan = build_plan(current, before)
        except PlanError as e:
            return {
                "ok": False,
                "reason": "回退规划失败，当前内核配置可能与保存状态不一致: "
                + "; ".join(e.errors),
                "steps": [],
                "config_after": current.to_dict(),
            }

        records: list[dict[str, Any]] = []
        rb_ok = True
        rb_reason: str | None = None
        for pstep in rb_plan.steps:
            argv = argv_for_operation(pstep.operation, self.topo)
            res = self.runner.run(argv)
            records.append(
                {
                    "seq": pstep.seq,
                    "note": "回退: " + pstep.operation.note,
                    "phase": pstep.operation.phase,
                    "target": pstep.operation.target,
                    "effective_total_kbps": pstep.effective_total_kbps,
                    "planned_invariant_ok": pstep.invariant_ok,
                    "config_after": pstep.after.to_dict(),
                    **res.to_dict(),
                }
            )
            if not res.ok:
                rb_ok = False
                rb_reason = (
                    f"回退第 {pstep.seq} 步失败（rc={res.rc}）: "
                    f"{res.stderr or res.stdout or '未知错误'}"
                )
                break
            self.state = pstep.after

        if rb_ok:
            self.state = before
            self._persist_locked()
            return {
                "ok": True,
                "reason": None,
                "steps": records,
                "config_after": self.state.to_dict(),
                "invariant": invariant_snapshot(self.state),
            }

        # Rollback failed part-way: do not pretend on-disk state is fine.
        return {
            "ok": False,
            "reason": rb_reason,
            "steps": records,
            "config_after": self.state.to_dict(),
            "invariant": invariant_snapshot(self.state),
            "drift": True,
        }

    # ------------------------------------------------------------------
    def kernel_report(self) -> dict[str, Any]:
        sections = []
        for title, argv, wrapped in kernel_report_argv(self.topo):
            real_argv = self.topo.src_wrap(argv) if wrapped else argv
            res = self.runner.run(real_argv)
            sections.append({"title": title, **res.to_dict()})
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "sections": sections,
            "state": self.state.to_dict(),
        }

    def history_list(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.history)

    # ------------------------------------------------------------------
    def demo_failure(self) -> dict[str, Any]:
        """One-click mid-change failure demonstration.

        Picks one of two known-valid presets that differs from the current
        state, so the forward plan always has several steps, then arms a
        failure on its second step.
        """
        preset_a = LabConfig.from_dict(DEFAULT_CONFIG.to_dict())
        preset_b = LabConfig(
            total_kbps=8_000,
            alarm=type(preset_a.alarm)(1_600, 8_000),
            video=type(preset_a.video)(4_800, 8_000),
        )
        with self._lock:
            target = preset_b if self.state != preset_b else preset_a
            plan = build_plan(self.state, target)
            if len(plan.steps) >= 2:
                fault_step = 2
            elif plan.steps:
                fault_step = 1
            else:
                # Already equals the only alternative: force a tiny safe
                # perturbation so there is something to demonstrate.
                target = LabConfig(
                    total_kbps=self.state.total_kbps,
                    alarm=type(self.state.alarm)(
                        max(64, self.state.alarm.rate_kbps - 64),
                        self.state.alarm.ceil_kbps,
                    ),
                    video=self.state.video,
                )
                plan = build_plan(self.state, target)
                fault_step = 1
        return self.apply(target, fault_step=fault_step, fault_mode="stub")
