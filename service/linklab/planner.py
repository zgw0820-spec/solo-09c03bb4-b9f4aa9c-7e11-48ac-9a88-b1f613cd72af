"""Safe online transition planner for live ``tc class change`` operations.

Why a planner?
--------------
A new web form can change the total rate *and* the two guarantees/ceilings
at once.  Naively replaying the fields in form order can briefly put the
kernel into a state where guarantees sum beyond the effective root rate,
or a ceiling exceeds the root rate.  The task explicitly forbids that at
*every* step of both change and rollback.

The planner emits an ordered list of in-place class changes (never
qdisc/class deletion, so the live HTB queue and its counters are kept)
such that the bandwidth invariant holds after every single operation.

Ordering proof (fixed cap C for the child transition)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
When the total grows, the root rate is raised first so C = new total;
when the total shrinks, children are moved first while C = old total
(which is looser), then the root is lowered.

Child attributes are moved in four phases, both classes processed in a
fixed order (alarm then video):

1. decrease ``rate``  -- only frees guarantee budget; rate <= ceil stays;
2. decrease ``ceil``  -- current rate is provably <= target ceil;
3. increase ``ceil``  -- only after this may rate grow;
4. increase ``rate``  -- every intermediate rate vector is bounded
   component-wise by the target rate vector, whose sum is valid.

Hence guarantee sum <= C and every ceil <= C after every step.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .model import CLASS_KEYS, LabConfig, violations


class PlanError(ValueError):
    """Raised when a requested target configuration is not applicable."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


Target = Literal["root", "alarm", "video"]


@dataclass(frozen=True)
class Operation:
    """One logical class change; mapped to an argv by ``tc_argv``."""

    target: Target
    rate_kbps: int
    ceil_kbps: int
    phase: str
    note: str

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "rate_kbps": self.rate_kbps,
            "ceil_kbps": self.ceil_kbps,
            "phase": self.phase,
            "note": self.note,
        }


@dataclass(frozen=True)
class PlanStep:
    seq: int
    operation: Operation
    effective_total_kbps: int
    after: LabConfig
    invariant_ok: bool
    violations: list[str]

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "operation": self.operation.to_dict(),
            "effective_total_kbps": self.effective_total_kbps,
            "config_after": self.after.to_dict(),
            "invariant_ok": self.invariant_ok,
            "violations": self.violations,
        }


@dataclass(frozen=True)
class Plan:
    old: LabConfig
    new: LabConfig
    steps: list[PlanStep]

    def __len__(self) -> int:
        return len(self.steps)

    def to_dict(self) -> dict:
        return {
            "old": self.old.to_dict(),
            "target": self.new.to_dict(),
            "step_count": len(self.steps),
            "steps": [s.to_dict() for s in self.steps],
        }


class _Builder:
    def __init__(self, current: LabConfig):
        self.current = current
        self.steps: list[PlanStep] = []

    def _append(self, op: Operation) -> None:
        errs = violations(self.current)
        self.steps.append(
            PlanStep(
                seq=len(self.steps) + 1,
                operation=op,
                effective_total_kbps=self.current.total_kbps,
                after=self.current,
                invariant_ok=not errs,
                violations=errs,
            )
        )

    def set_root(self, total_kbps: int, note: str) -> None:
        if self.current.total_kbps == total_kbps:
            return
        self.current = self.current.with_total(total_kbps)
        self._append(
            Operation(
                target="root",
                rate_kbps=total_kbps,
                ceil_kbps=total_kbps,
                phase="total",
                note=note,
            )
        )

    def set_class(
        self,
        key: str,
        rate_kbps: int | None = None,
        ceil_kbps: int | None = None,
        phase: str = "",
        note: str = "",
    ) -> None:
        cur = self.current.class_cfg(key)
        new_rate = cur.rate_kbps if rate_kbps is None else rate_kbps
        new_ceil = cur.ceil_kbps if ceil_kbps is None else ceil_kbps
        if new_rate == cur.rate_kbps and new_ceil == cur.ceil_kbps:
            return
        self.current = self.current.with_class(
            key, type(cur)(rate_kbps=new_rate, ceil_kbps=new_ceil)
        )
        self._append(
            Operation(
                target=key,
                rate_kbps=new_rate,
                ceil_kbps=new_ceil,
                phase=phase,
                note=note,
            )
        )


def _move_children(b: _Builder, target: LabConfig) -> None:
    # Phase 1: decrease guarantees first (frees the shared budget).
    for key in CLASS_KEYS:
        old_r = b.current.class_cfg(key).rate_kbps
        new_r = target.class_cfg(key).rate_kbps
        if new_r < old_r:
            b.set_class(
                key,
                rate_kbps=new_r,
                phase="rate-down",
                note=f"先下调{key}保底速率（释放保底预算，速率仍不超过原上限）",
            )

    # Phase 2: decrease ceilings.
    for key in CLASS_KEYS:
        old_c = b.current.class_cfg(key).ceil_kbps
        new_c = target.class_cfg(key).ceil_kbps
        if new_c < old_c:
            b.set_class(
                key,
                ceil_kbps=new_c,
                phase="ceil-down",
                note=f"下调{key}速率上限（当前保底已不高于新上限）",
            )

    # Phase 3: increase ceilings before increasing rates.
    for key in CLASS_KEYS:
        old_c = b.current.class_cfg(key).ceil_kbps
        new_c = target.class_cfg(key).ceil_kbps
        if new_c > old_c:
            b.set_class(
                key,
                ceil_kbps=new_c,
                phase="ceil-up",
                note=f"上调{key}速率上限，为提高保底做准备",
            )

    # Phase 4: increase guarantees last.
    for key in CLASS_KEYS:
        old_r = b.current.class_cfg(key).rate_kbps
        new_r = target.class_cfg(key).rate_kbps
        if new_r > old_r:
            b.set_class(
                key,
                rate_kbps=new_r,
                phase="rate-up",
                note=f"最后上调{key}保底速率（每步保底之和仍不超过总速率）",
            )


def build_plan(old: LabConfig, new: LabConfig) -> Plan:
    """Return the safe in-place transition plan ``old -> new``.

    Raises :class:`PlanError` if either endpoint violates the invariant.
    Every produced :class:`PlanStep` is independently invariant-clean.
    """
    old_errors = violations(old)
    if old_errors:
        raise PlanError([f"改前配置本身已越界: {e}" for e in old_errors])
    new_errors = violations(new)
    if new_errors:
        raise PlanError(new_errors)

    b = _Builder(old)

    if new.total_kbps >= old.total_kbps:
        # Growing (or equal) total: raise the effective root first.
        if new.total_kbps > old.total_kbps:
            b.set_root(new.total_kbps, "扩容：先提高根类总速率，再调整两类")
        _move_children(b, new)
    else:
        # Shrinking total: shrink the classes into the tighter target
        # envelope first, while the old, larger total is still effective.
        _move_children(b, new)
        b.set_root(
            new.total_kbps,
            "降速：两类保底/上限均已落入新总速率，最后降低根类总速率",
        )

    # Defensive: planner must land exactly on target.
    if b.current != new:
        raise PlanError(
            [
                "内部规划错误：计划终态与目标不一致 "
                f"({b.current.to_dict()} != {new.to_dict()})"
            ]
        )
    for s in b.steps:
        if not s.invariant_ok:
            raise PlanError(
                [f"内部规划错误：第 {s.seq} 步后不变量被破坏: {s.violations}"]
            )

    return Plan(old=old, new=new, steps=b.steps)
