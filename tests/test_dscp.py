"""Tests for the pure DSCP/ECN logic and the bounded MarkVerifier."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from linklab.dscp import (  # noqa: E402
    ECN_ECT0,
    MAX_DSCP,
    MarkVerifier,
    dscp_of,
    ecn_name,
    ecn_of,
    tos_byte,
    validate_new_dscp,
)


class TosByteTests(unittest.TestCase):
    def test_start_marker_is_dscp0_ect0(self):
        self.assertEqual(tos_byte(0, ECN_ECT0), 0x01)
        self.assertEqual(dscp_of(0x01), 0)
        self.assertEqual(ecn_of(0x01), ECN_ECT0)

    def test_dscp_shift_and_ecn_preserved(self):
        # DSCP 20 / ECT(0) -> 0x51; DSCP 63 / ECT(0) -> 0xFD.
        self.assertEqual(tos_byte(20, ECN_ECT0), 0x51)
        self.assertEqual(dscp_of(0x51), 20)
        self.assertEqual(ecn_of(0x51), ECN_ECT0)
        self.assertEqual(tos_byte(63, ECN_ECT0), 0xFD)
        for dscp in range(0, 64):
            for ecn in range(4):
                b = tos_byte(dscp, ecn)
                self.assertEqual(dscp_of(b), dscp)
                self.assertEqual(ecn_of(b), ecn)

    def test_ecn_name_known(self):
        self.assertEqual(ecn_name(ECN_ECT0), "ECT(0)")

    def test_validate_range_and_types(self):
        self.assertEqual(validate_new_dscp(1), 1)
        self.assertEqual(validate_new_dscp("46"), 46)
        self.assertEqual(validate_new_dscp(MAX_DSCP), 63)
        for bad in (0, 64, -1, "abc", "3.5", 1.5, True, None, ""):
            with self.assertRaises(ValueError):
                validate_new_dscp(bad)


class MarkVerifierTests(unittest.TestCase):
    def test_single_old_segment_before_plan(self):
        v = MarkVerifier(flow="alarm")
        for seq in range(10):
            v.record(tos_byte(0, ECN_ECT0), seq)
        snap = v.snapshot()
        self.assertIsNone(snap["boundary_seq"])
        self.assertFalse(snap["plan_installed"])
        self.assertEqual(snap["segments"]["old"]["packets"], 10)
        self.assertEqual(snap["segments"]["old"]["mismatch"], 0)
        self.assertTrue(snap["segments"]["old"]["confirmed"])
        self.assertIsNone(snap["segments"]["new"])
        self.assertFalse(snap["verified"])  # no new segment yet

    def test_split_at_boundary_and_verification(self):
        v = MarkVerifier(flow="alarm")
        # Old segment: seq 0..49 at DSCP 0 / ECT(0).
        for seq in range(50):
            v.record(tos_byte(0, ECN_ECT0), seq)
        v.install_plan(boundary_seq=50, new_dscp=20)
        # New segment pending before first new packet.
        snap = v.snapshot()
        self.assertTrue(snap["plan_installed"])
        self.assertFalse(snap["segments"]["new"]["confirmed"])
        self.assertEqual(snap["segments"]["new"]["status"], "pending")
        for seq in range(50, 100):
            v.record(tos_byte(20, ECN_ECT0), seq)
        snap = v.snapshot()
        self.assertEqual(snap["boundary_seq"], 50)
        self.assertEqual(snap["segments"]["old"]["packets"], 50)
        self.assertEqual(snap["segments"]["new"]["packets"], 50)
        self.assertEqual(snap["segments"]["new"]["expected_dscp"], 20)
        self.assertTrue(snap["segments"]["new"]["confirmed"])
        self.assertEqual(snap["total_mismatch"], 0)
        self.assertTrue(snap["verified"])

    def test_late_old_packet_stays_in_old_segment(self):
        v = MarkVerifier(flow="alarm")
        for seq in range(30):
            v.record(tos_byte(0, ECN_ECT0), seq)
        v.install_plan(boundary_seq=30, new_dscp=20)
        # New packets first...
        for seq in range(30, 35):
            v.record(tos_byte(20, ECN_ECT0), seq)
        # ...then a late old-segment packet (seq 29 was lost earlier and
        # now arrives) carrying the old marker DSCP 0.
        v.record(tos_byte(0, ECN_ECT0), 29)
        snap = v.snapshot()
        self.assertEqual(snap["segments"]["old"]["packets"], 31)
        self.assertEqual(snap["segments"]["new"]["packets"], 5)
        # It matched the old expectation, so no mismatch anywhere.
        self.assertEqual(snap["total_mismatch"], 0)
        self.assertTrue(snap["verified"])

    def test_mismatched_dscp_and_ecn_are_counted(self):
        v = MarkVerifier(flow="alarm")
        v.record(tos_byte(0, ECN_ECT0), 0)
        v.install_plan(boundary_seq=1, new_dscp=20)
        # Wrong DSCP but ECN preserved -> DSCP mismatch only.
        v.record(tos_byte(10, ECN_ECT0), 1)
        # Correct DSCP but ECN changed -> ECN mismatch only.
        v.record(tos_byte(20, 3), 2)
        snap = v.snapshot()
        new = snap["segments"]["new"]
        self.assertEqual(new["packets"], 2)
        self.assertEqual(new["dscp_mismatch"], 1)
        self.assertEqual(new["ecn_mismatch"], 1)
        self.assertEqual(new["mismatch"], 2)
        self.assertFalse(snap["verified"])

    def test_plan_arrives_after_new_packets_is_reconciled(self):
        # The receiver learns the boundary only after new-marker packets
        # have already arrived (plan-file poll race). The bounded ring keeps
        # them so the plan can re-segment by sequence number.
        v = MarkVerifier(flow="alarm")
        for seq in range(40):
            v.record(tos_byte(0, ECN_ECT0), seq)
        # New marker packets arrive before the plan is installed.
        for seq in range(40, 60):
            v.record(tos_byte(20, ECN_ECT0), seq)
        self.assertIsNone(v.boundary_seq)
        self.assertEqual(v.snapshot()["ring_buffered"], 60)
        # Now the plan arrives with boundary 40.
        v.install_plan(boundary_seq=40, new_dscp=20)
        snap = v.snapshot()
        self.assertEqual(snap["segments"]["old"]["packets"], 40)
        self.assertEqual(snap["segments"]["new"]["packets"], 20)
        self.assertEqual(snap["segments"]["old"]["mismatch"], 0)
        self.assertEqual(snap["segments"]["new"]["mismatch"], 0)
        self.assertTrue(snap["verified"])
        self.assertEqual(snap["ring_buffered"], 0)

    def test_late_old_packet_after_plan_stays_old(self):
        v = MarkVerifier(flow="alarm")
        for seq in range(40):
            v.record(tos_byte(0, ECN_ECT0), seq)
        v.install_plan(boundary_seq=40, new_dscp=20)
        for seq in range(40, 50):
            v.record(tos_byte(20, ECN_ECT0), seq)
        # A delayed old-segment packet (seq 39) arrives last.
        v.record(tos_byte(0, ECN_ECT0), 39)
        snap = v.snapshot()
        self.assertEqual(snap["segments"]["old"]["packets"], 41)
        self.assertEqual(snap["segments"]["new"]["packets"], 10)
        self.assertEqual(snap["total_mismatch"], 0)

    def test_ring_is_bounded_and_evicted_packets_stay_old(self):
        v = MarkVerifier(flow="alarm", ring_size=10)
        for seq in range(25):
            v.record(tos_byte(0, ECN_ECT0), seq)
        snap = v.snapshot()
        self.assertLessEqual(snap["ring_buffered"], 10)
        self.assertEqual(snap["ring_evicted"], 15)
        # Evicted packets remain committed in the old segment (provably old,
        # since the re-mark window is far shorter than the ring).
        self.assertEqual(snap["segments"]["old"]["packets"], 25)

        # A late plan whose boundary lies beyond the evicted range still
        # re-segments the buffered tail correctly.
        v.install_plan(boundary_seq=20, new_dscp=20)
        snap = v.snapshot()
        self.assertEqual(
            snap["segments"]["old"]["packets"] + snap["segments"]["new"]["packets"],
            25,
        )

    def test_unknown_tos_and_headerless_packets(self):
        v = MarkVerifier(flow="alarm")
        v.record(None, 0)
        v.record(tos_byte(0, ECN_ECT0), None)
        snap = v.snapshot()
        self.assertEqual(snap["unknown_tos"], 1)
        self.assertEqual(snap["unparsable_packets"], 1)
        self.assertEqual(snap["segments"]["old"]["packets"], 0)

    def test_memory_is_bounded_under_many_distinct_tos(self):
        v = MarkVerifier(flow="alarm")
        v.install_plan(boundary_seq=1, new_dscp=20)
        # Far more distinct TOS values than the cap; only 256 values exist.
        for seq in range(1, 5000):
            v.record(seq & 0xFF, seq)
        snap = v.snapshot()
        observed = snap["segments"]["new"]["observed"]
        # MAX_OBSERVED distinct TOS keys plus at most one "other" bucket.
        self.assertLessEqual(len(observed), 64 + 1)
        self.assertIn("other", observed)
        # Counters still see every packet.
        self.assertEqual(snap["segments"]["new"]["packets"], 4999)

    def test_plan_file_roundtrip_and_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "plan.json"
            p.write_text(json.dumps({
                "flow": "alarm", "request_id": "r1",
                "new_dscp": 20, "boundary_seq": 42,
            }), encoding="utf-8")
            v = MarkVerifier(flow="alarm")
            self.assertTrue(v.load_plan_file(p))
            self.assertEqual(v.boundary_seq, 42)
            self.assertEqual(v.new_dscp, 20)
            # Re-reading the same request id must not reset counters.
            v.record(tos_byte(20, ECN_ECT0), 42)
            self.assertTrue(v.load_plan_file(p))
            self.assertEqual(v.snapshot()["segments"]["new"]["packets"], 1)
            # Garbage file is tolerated.
            p.write_text("not-json", encoding="utf-8")
            self.assertFalse(v.load_plan_file(p))
            # Missing file is tolerated.
            self.assertFalse(v.load_plan_file(Path(tmp) / "nope.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
