"""DSCP / ECN marking primitives and the bounded re-marking verifier.

TOS byte layout (IPv4, RFC 2474 / 3168)::

    bit 7 6 5 4 3 2 | 1 0
        DSCP (6b)    | ECN (2b)

Every alarm trial starts at DSCP 0 with ECN = ECT(0) (TOS byte ``0x01``).
A re-mark changes the DSCP bits only; the two ECN bits are always carried
over unchanged.

The verifier in this module keeps *bounded* state per flow: two fixed
segment counters, a capped table of observed TOS values, and a small ring
buffer of the most recent (seq, TOS) packets.  The ring size is fixed, so
memory use is bounded regardless of how many packets are received.  When
the sender-reported boundary arrives, the ring is replayed against it, so
packets that reached the receiver just before the plan was known -- and any
late old-segment packet -- are still placed in the correct segment by their
sequence number.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ECN codepoints (low two bits of the TOS byte).
ECN_NON_ECT = 0
ECN_ECT0 = 1
ECN_ECT1 = 2
ECN_CE = 3

ECN_NAMES = {
    ECN_NON_ECT: "Non-ECT(00)",
    ECN_ECT0: "ECT(0)",
    ECN_ECT1: "ECT(1)",
    ECN_CE: "CE(11)",
}

# Every alarm trial begins unmarked: DSCP 0, ECT(0).
START_DSCP = 0
START_ECN = ECN_ECT0

MIN_DSCP = 1
MAX_DSCP = 63

# Cap on the distinct observed-TOS table so even adversarial traffic cannot
# make the verifier grow without bound.
MAX_OBSERVED = 64


def tos_byte(dscp: int, ecn: int = START_ECN) -> int:
    """Compose an IPv4 TOS byte from a DSCP value and an ECN codepoint."""
    return ((int(dscp) & 0x3F) << 2) | (int(ecn) & 0x03)


def dscp_of(tos: int) -> int:
    return (int(tos) & 0xFC) >> 2


def ecn_of(tos: int) -> int:
    return int(tos) & 0x03


def ecn_name(ecn: int) -> str:
    return ECN_NAMES.get(int(ecn), f"未知({ecn})")


def validate_new_dscp(value: Any) -> int:
    """Validate the web/API input: an integer DSCP in 1..63."""
    if isinstance(value, bool):
        raise ValueError("DSCP 必须是 1~63 的整数")
    try:
        dscp = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"DSCP 必须是 1~63 的整数，收到: {value!r}") from None
    # Reject strings like "3.5" / "12x" that int() would not parse anyway,
    # and floats by comparing the round-trip.
    if isinstance(value, float) and float(value) != dscp:
        raise ValueError(f"DSCP 必须是整数，收到: {value!r}")
    if not (MIN_DSCP <= dscp <= MAX_DSCP):
        raise ValueError(f"新 DSCP 必须在 {MIN_DSCP}~{MAX_DSCP} 之间，收到: {dscp}")
    return dscp


def default_segment(expected_dscp: int, expected_ecn: int, label: str) -> dict[str, Any]:
    return {
        "label": label,
        "expected_dscp": int(expected_dscp),
        "expected_ecn": int(expected_ecn),
        "expected_ecn_name": ecn_name(expected_ecn),
        "expected_tos": tos_byte(expected_dscp, expected_ecn),
        "packets": 0,
        "dscp_mismatch": 0,
        "ecn_mismatch": 0,
        "mismatch": 0,
        "confirmed": False,
        # Bounded summary of what was actually seen in this segment.
        "observed": {},
    }


def _account(seg: dict[str, Any], tos: int) -> None:
    """Add one packet with a known TOS byte to a segment's fixed counters."""
    got_dscp = dscp_of(tos)
    got_ecn = ecn_of(tos)
    seg["packets"] += 1
    seg["confirmed"] = True
    dscp_bad = got_dscp != seg["expected_dscp"]
    ecn_bad = got_ecn != seg["expected_ecn"]
    if dscp_bad:
        seg["dscp_mismatch"] += 1
    if ecn_bad:
        seg["ecn_mismatch"] += 1
    if dscp_bad or ecn_bad:
        seg["mismatch"] += 1
    observed = seg["observed"]
    key = str(tos)
    if key not in observed and len(observed) >= MAX_OBSERVED:
        # Bound the table; further distinct values fold into "other".
        key = "other"
    observed[key] = observed.get(key, 0) + 1


@dataclass
class MarkVerifier:
    """Per-flow bounded verifier for one DSCP re-marking trial.

    Segmentation
    ~~~~~~~~~~~~
    Before a re-mark plan is installed every packet belongs to the single
    "old" segment (DSCP 0 / ECT(0)).  After :meth:`install_plan` the flow is
    split at ``boundary_seq``: ``seq < boundary`` is old, ``seq >= boundary``
    is new.  The new segment stays *unconfirmed* until its first packet is
    accounted into it.

    Plan-arrival race (bounded ring buffer)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
    The receiver learns the boundary asynchronously while packets keep
    arriving.  Every packet with a parseable seq/TOS is therefore pushed into
    a fixed-capacity ring (``ring_size`` entries) and, while no plan exists,
    also counted provisionally in the old segment.  When the plan arrives the
    ring is replayed: the provisional old-segment counters are rolled back
    and every buffered packet is re-assigned by seq against the boundary.
    Packets older than the ring were committed before the re-mark window and
    are provably old-segment, so they are never touched.  Once the plan is
    known later packets are assigned directly, and late old-segment packets
    still land in the old segment.  Memory stays bounded by the ring size.
    """

    flow: str
    old_dscp: int = START_DSCP
    old_ecn: int = START_ECN
    boundary_seq: int | None = None
    new_dscp: int | None = None
    new_ecn: int = START_ECN
    plan_request_id: str | None = None
    ring_size: int = 2048
    unknown_tos: int = 0
    unparsable_packets: int = 0
    ring_evicted: int = 0
    total_packets: int = 0
    _old: dict[str, Any] = field(default_factory=dict)
    _new: dict[str, Any] | None = None
    # Bounded ring of the most recent (seq, tos) packets.
    _ring: deque = field(default_factory=deque)
    # Packets provisionally counted into the old segment that are still
    # replayable in the ring (tos -> count), so install can roll them back.
    _ring_tos: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self._old:
            self._old = default_segment(self.old_dscp, self.old_ecn, "旧段")

    # ------------------------------------------------------------------
    def install_plan(
        self, boundary_seq: int, new_dscp: int, request_id: str | None = None
    ) -> None:
        """Install the sender-reported split point and new marker.

        Idempotent for the same ``request_id`` (the plan file is re-read
        periodically); a different request id replaces a pending plan and
        re-segments the buffered packets.
        """
        boundary_seq = int(boundary_seq)
        new_dscp = int(new_dscp)
        if (
            self.boundary_seq is not None
            and request_id is not None
            and request_id == self.plan_request_id
        ):
            return
        self.boundary_seq = boundary_seq
        self.new_dscp = int(new_dscp)
        # ECN is preserved across re-marking.
        self.new_ecn = self.old_ecn
        self.plan_request_id = request_id
        self._new = default_segment(new_dscp, self.new_ecn, "新段")

        # 1) Roll back the provisional old-segment accounting for every
        #    packet still held in the ring.
        for tos_str, count in self._ring_tos.items():
            self._rollback(self._old, int(tos_str), count)
        self._ring_tos = {}

        # 2) Re-assign each buffered packet by seq against the boundary.
        for seq, tos in self._ring:
            self._assign(seq, tos)
        self._ring.clear()

    @staticmethod
    def _rollback(seg: dict[str, Any], tos: int, count: int) -> None:
        """Undo ``count`` provisional ``_account`` calls for one TOS value."""
        got_dscp = dscp_of(tos)
        got_ecn = ecn_of(tos)
        seg["packets"] -= count
        dscp_bad = got_dscp != seg["expected_dscp"]
        ecn_bad = got_ecn != seg["expected_ecn"]
        if dscp_bad:
            seg["dscp_mismatch"] -= count
        if ecn_bad:
            seg["ecn_mismatch"] -= count
        if dscp_bad or ecn_bad:
            seg["mismatch"] -= count
        observed = seg["observed"]
        key = str(tos)
        if key in observed:
            observed[key] -= count
            if observed[key] <= 0:
                del observed[key]
        elif "other" in observed:
            observed["other"] -= count
            if observed["other"] <= 0:
                del observed["other"]
        if seg["packets"] <= 0:
            seg["confirmed"] = False

    def load_plan_file(self, path: Path | str) -> bool:
        """Load a plan written by the FlowManager. Returns True on success."""
        try:
            doc = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        try:
            self.install_plan(
                boundary_seq=doc["boundary_seq"],
                new_dscp=doc["new_dscp"],
                request_id=doc.get("request_id"),
            )
        except (KeyError, TypeError, ValueError):
            return False
        return True

    # ------------------------------------------------------------------
    def _assign(self, seq: int, tos: int) -> None:
        """Place a known packet into old/new by seq against the boundary."""
        if self.boundary_seq is not None and seq >= self.boundary_seq:
            assert self._new is not None
            _account(self._new, tos)
        else:
            _account(self._old, tos)

    def record(self, tos: int | None, seq: int | None) -> None:
        """Account for one received datagram. Bounded memory only."""
        self.total_packets += 1
        if tos is None:
            self.unknown_tos += 1
            return
        tos = int(tos) & 0xFF
        if seq is None:
            # Headerless packet: cannot place it by sequence; count it once
            # and never grow per-seq state.
            self.unparsable_packets += 1
            return

        if self.boundary_seq is not None:
            # Plan known: direct assignment; late old packets stay old.
            self._assign(seq, tos)
            return

        # Plan not known yet: account provisionally into the old segment and
        # keep the packet in the bounded ring for a possible re-segmentation.
        _account(self._old, tos)
        self._ring.append((seq, tos))
        key = str(tos)
        self._ring_tos[key] = self._ring_tos.get(key, 0) + 1
        if len(self._ring) > self.ring_size:
            ev_seq, ev_tos = self._ring.popleft()
            self.ring_evicted += 1
            ev_key = str(ev_tos)
            self._ring_tos[ev_key] -= 1
            if self._ring_tos[ev_key] <= 0:
                del self._ring_tos[ev_key]
            # The evicted packet stays in the old segment (the re-mark window
            # is far shorter than the ring, so an evicted packet is old).

    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        new_segment = None
        if self._new is not None:
            new_segment = dict(self._new)
            new_segment["observed"] = dict(self._new["observed"])
            new_segment["status"] = (
                "confirmed" if self._new["confirmed"] else "pending"
            )
        old_segment = dict(self._old)
        old_segment["observed"] = dict(self._old["observed"])
        old_segment["status"] = "confirmed" if self._old["confirmed"] else "pending"
        total_mismatch = old_segment["mismatch"] + (
            self._new["mismatch"] if self._new is not None else 0
        )
        return {
            "flow": self.flow,
            "start_dscp": self.old_dscp,
            "start_ecn": self.old_ecn,
            "start_ecn_name": ecn_name(self.old_ecn),
            "boundary_seq": self.boundary_seq,
            "new_dscp": self.new_dscp,
            "new_ecn": self.new_ecn if self._new is not None else None,
            "new_ecn_name": ecn_name(self.new_ecn) if self._new is not None else None,
            "plan_installed": self._new is not None,
            "segments": {
                "old": old_segment,
                "new": new_segment,
            },
            "total_packets": self.total_packets,
            "unknown_tos": self.unknown_tos,
            "unparsable_packets": self.unparsable_packets,
            "ring_buffered": len(self._ring),
            "ring_evicted": self.ring_evicted,
            "total_mismatch": total_mismatch,
            "verified": (
                self._new is not None
                and self._new["confirmed"]
                and total_mismatch == 0
                and self.unknown_tos == 0
            ),
        }
