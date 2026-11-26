"""Bandwidth configuration model and the safety invariant.

The lab carries exactly two shaped UDP classes:

* ``alarm`` -- park alarms (告警), UDP/5001, HTB class 1:10
* ``video`` -- recording / video (录像), UDP/5002, HTB class 1:20

plus the HTB root class 1:1 whose ``rate``/``ceil`` is the effective
uplink total.

Hard safety rule (the core requirement)
--------------------------------------
At *every* reconfiguration step -- including rollback -- against the
total rate that is effective in the kernel at that moment:

    rate(alarm) + rate(video) <= total_effective
    ceil(alarm)               <= total_effective
    ceil(video)               <= total_effective

We also enforce the HTB-intrinsic ``rate <= ceil`` per class and the
global lab bounds.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from .units import MAX_KBPS, MIN_KBPS, kbps_to_mbps, mbps_to_kbps

CLASS_KEYS = ("alarm", "video")

CLASS_LABELS = {
    "alarm": "告警",
    "video": "录像",
}


@dataclass(frozen=True)
class ClassCfg:
    """One HTB class: guaranteed rate (``rate``) and upper limit (``ceil``)."""

    rate_kbps: int
    ceil_kbps: int

    def to_dict(self) -> dict[str, int]:
        return {"rate_kbps": self.rate_kbps, "ceil_kbps": self.ceil_kbps}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ClassCfg":
        return cls(int(d["rate_kbps"]), int(d["ceil_kbps"]))


@dataclass(frozen=True)
class LabConfig:
    total_kbps: int
    alarm: ClassCfg
    video: ClassCfg

    # -- helpers -------------------------------------------------------
    def class_cfg(self, key: str) -> ClassCfg:
        if key == "alarm":
            return self.alarm
        if key == "video":
            return self.video
        raise KeyError(key)

    def with_class(self, key: str, cfg: ClassCfg) -> "LabConfig":
        if key == "alarm":
            return replace(self, alarm=cfg)
        if key == "video":
            return replace(self, video=cfg)
        raise KeyError(key)

    def with_total(self, total_kbps: int) -> "LabConfig":
        return replace(self, total_kbps=total_kbps)

    # -- (de)serialisation --------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "total_kbps": self.total_kbps,
            "total_mbps": kbps_to_mbps(self.total_kbps),
            "alarm": {
                **self.alarm.to_dict(),
                "rate_mbps": kbps_to_mbps(self.alarm.rate_kbps),
                "ceil_mbps": kbps_to_mbps(self.alarm.ceil_kbps),
                "label": CLASS_LABELS["alarm"],
            },
            "video": {
                **self.video.to_dict(),
                "rate_mbps": kbps_to_mbps(self.video.rate_kbps),
                "ceil_mbps": kbps_to_mbps(self.video.ceil_kbps),
                "label": CLASS_LABELS["video"],
            },
            "guarantee_sum_kbps": self.alarm.rate_kbps + self.video.rate_kbps,
            "guarantee_sum_mbps": kbps_to_mbps(
                self.alarm.rate_kbps + self.video.rate_kbps
            ),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LabConfig":
        return cls(
            total_kbps=int(d["total_kbps"]),
            alarm=ClassCfg.from_dict(d["alarm"]),
            video=ClassCfg.from_dict(d["video"]),
        )

    @classmethod
    def from_form(cls, form: dict[str, Any]) -> "LabConfig":
        """Build from web form fields, all rates in Mbit/s.

        Accepted field names:
        total_mbps
        alarm_rate_mbps, alarm_ceil_mbps
        video_rate_mbps, video_ceil_mbps
        """
        return cls(
            total_kbps=mbps_to_kbps(form.get("total_mbps")),
            alarm=ClassCfg(
                mbps_to_kbps(form.get("alarm_rate_mbps")),
                mbps_to_kbps(form.get("alarm_ceil_mbps")),
            ),
            video=ClassCfg(
                mbps_to_kbps(form.get("video_rate_mbps")),
                mbps_to_kbps(form.get("video_ceil_mbps")),
            ),
        )


# Lab default: 10 Mbit/s uplink after the park speed reduction,
# 2 Mbit/s alarm guarantee, 6 Mbit/s recording guarantee,
# both may borrow up to the full 10 Mbit/s when the other is idle.
DEFAULT_CONFIG = LabConfig(
    total_kbps=10_000,
    alarm=ClassCfg(rate_kbps=2_000, ceil_kbps=10_000),
    video=ClassCfg(rate_kbps=6_000, ceil_kbps=10_000),
)


def validate_values(
    total_kbps: Any,
    alarm_rate: Any,
    alarm_ceil: Any,
    video_rate: Any,
    video_ceil: Any,
    effective_total_kbps: int | None = None,
) -> list[str]:
    """Validate raw int values. Returns a list of human-readable errors."""
    errors: list[str] = []
    values = {
        "总速率": total_kbps,
        "告警保底": alarm_rate,
        "告警上限": alarm_ceil,
        "录像保底": video_rate,
        "录像上限": video_ceil,
    }
    ints: dict[str, int] = {}
    for label, v in values.items():
        if not isinstance(v, int) or isinstance(v, bool):
            errors.append(f"{label}必须是整数 kbit/s")
            continue
        if v < MIN_KBPS:
            errors.append(f"{label}不能小于 {MIN_KBPS} kbit/s（当前 {v}）")
        if v > MAX_KBPS:
            errors.append(f"{label}不能大于 {MAX_KBPS} kbit/s（当前 {v}）")
        ints[label] = v

    if errors:
        return errors

    T = effective_total_kbps if effective_total_kbps is not None else total_kbps

    if alarm_rate + video_rate > T:
        errors.append(
            f"两类保底之和 {alarm_rate + video_rate} kbit/s "
            f"超过当前生效总速率 {T} kbit/s"
        )
    if alarm_ceil > T:
        errors.append(f"告警速率上限 {alarm_ceil} kbit/s 超过当前总速率 {T} kbit/s")
    if video_ceil > T:
        errors.append(f"录像速率上限 {video_ceil} kbit/s 超过当前总速率 {T} kbit/s")
    if alarm_rate > T:
        errors.append(f"告警保底 {alarm_rate} kbit/s 超过当前总速率 {T} kbit/s")
    if video_rate > T:
        errors.append(f"录像保底 {video_rate} kbit/s 超过当前总速率 {T} kbit/s")
    if alarm_rate > alarm_ceil:
        errors.append(
            f"告警保底 {alarm_rate} 大于其速率上限 {alarm_ceil}（HTB 要求 rate <= ceil）"
        )
    if video_rate > video_ceil:
        errors.append(
            f"录像保底 {video_rate} 大于其速率上限 {video_ceil}（HTB 要求 rate <= ceil）"
        )
    return errors


def violations(cfg: LabConfig, effective_total_kbps: int | None = None) -> list[str]:
    """Invariant violations of ``cfg`` against an effective total."""
    return validate_values(
        cfg.total_kbps,
        cfg.alarm.rate_kbps,
        cfg.alarm.ceil_kbps,
        cfg.video.rate_kbps,
        cfg.video.ceil_kbps,
        effective_total_kbps=(
            cfg.total_kbps if effective_total_kbps is None else effective_total_kbps
        ),
    )


def invariant_snapshot(cfg: LabConfig) -> dict[str, Any]:
    """Machine/UI readable invariant check for a config state."""
    errs = violations(cfg)
    guarantee_sum = cfg.alarm.rate_kbps + cfg.video.rate_kbps
    return {
        "effective_total_kbps": cfg.total_kbps,
        "guarantee_sum_kbps": guarantee_sum,
        "guarantee_sum_within_total": guarantee_sum <= cfg.total_kbps,
        "alarm_ceil_within_total": cfg.alarm.ceil_kbps <= cfg.total_kbps,
        "video_ceil_within_total": cfg.video.ceil_kbps <= cfg.total_kbps,
        "alarm_rate_within_class": cfg.alarm.rate_kbps <= cfg.alarm.ceil_kbps,
        "video_rate_within_class": cfg.video.rate_kbps <= cfg.video.ceil_kbps,
        "ok": not errs,
        "violations": errs,
    }
