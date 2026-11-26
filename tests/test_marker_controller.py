"""Sender-side MarkerController unit tests: success and failure handling."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from linklab import udp_send  # noqa: E402


class FakeSocket:
    def __init__(self, fail_on: int | None = None):
        # fail_on: fail setsockopt once the TOS reaches this value.
        self.fail_on = fail_on
        self.tos_calls: list[int] = []

    def setsockopt(self, level, opt, value):
        if opt == udp_send.socket.IP_TOS:
            if self.fail_on is not None and value == self.fail_on:
                raise OSError("simulated setsockopt failure")
            self.tos_calls.append(value)


class MarkerControllerTests(unittest.TestCase):
    def test_initial_marker_is_dscp0_ect0(self):
        with tempfile.TemporaryDirectory() as d:
            ctl = Path(d) / "ctl.json"
            stats = Path(d) / "stats.json"
            sock = FakeSocket()
            mc = udp_send.MarkerController(sock, stats, ctl)
            mc.apply_initial()
            self.assertEqual(sock.tos_calls, [0x01])
            block = mc.stats_block()
            self.assertEqual(block["dscp"], 0)
            self.assertEqual(block["ecn"], 1)
            self.assertEqual(block["tos_byte"], 0x01)
            self.assertFalse(block["remark_applied"])
            self.assertIsNone(block["boundary_seq"])

    def test_successful_remark_preserves_ecn_and_sets_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            ctl = Path(d) / "ctl.json"
            stats = Path(d) / "stats.json"
            sock = FakeSocket()
            mc = udp_send.MarkerController(sock, stats, ctl)
            mc.apply_initial()
            mc.start_watcher()
            ctl.write_text(json.dumps(
                {"request_id": "r1", "new_dscp": 20}), encoding="utf-8")
            deadline = time.time() + 3
            while time.time() < deadline and not mc.remark_applied:
                time.sleep(0.03)
            self.assertTrue(mc.remark_applied)
            self.assertIn(0x51, sock.tos_calls)  # DSCP20/ECT(0)
            # Boundary is latched at the first send after the switch.
            mc.before_send(123)
            self.assertEqual(mc.boundary_seq, 123)
            mc.before_send(124)
            self.assertEqual(mc.boundary_seq, 123)  # stable
            block = mc.stats_block()
            self.assertEqual(block["dscp"], 20)
            self.assertEqual(block["ecn"], 1)  # ECN preserved
            self.assertEqual(block["remark_request_id"], "r1")

    def test_failed_remark_keeps_old_marker_and_reports_reason(self):
        with tempfile.TemporaryDirectory() as d:
            ctl = Path(d) / "ctl.json"
            stats = Path(d) / "stats.json"
            # Fail when the new TOS (DSCP20/ECT0 = 0x51) is requested.
            sock = FakeSocket(fail_on=0x51)
            mc = udp_send.MarkerController(sock, stats, ctl)
            mc.apply_initial()
            mc.start_watcher()
            ctl.write_text(json.dumps(
                {"request_id": "r-bad", "new_dscp": 20}), encoding="utf-8")
            deadline = time.time() + 3
            while time.time() < deadline and mc.remark_error is None:
                time.sleep(0.03)
            self.assertIsNotNone(mc.remark_error)
            self.assertIn("IP_TOS", mc.remark_error)
            # Old marker retained; no boundary, not applied.
            self.assertFalse(mc.remark_applied)
            self.assertEqual(mc.dscp, 0)
            self.assertEqual(mc.tos, 0x01)
            self.assertIsNone(mc.boundary_seq)
            self.assertEqual(mc.pending_request_id, "r-bad")
            block = mc.stats_block()
            self.assertEqual(block["dscp"], 0)
            self.assertIsNotNone(block["remark_error"])

    def test_invalid_dscp_in_control_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            ctl = Path(d) / "ctl.json"
            stats = Path(d) / "stats.json"
            sock = FakeSocket()
            mc = udp_send.MarkerController(sock, stats, ctl)
            mc.start_watcher()
            ctl.write_text(json.dumps(
                {"request_id": "r-x", "new_dscp": 99}), encoding="utf-8")
            deadline = time.time() + 3
            while time.time() < deadline and mc.remark_error is None:
                time.sleep(0.03)
            self.assertFalse(mc.remark_applied)
            self.assertEqual(mc.dscp, 0)
            self.assertIn("1~63", mc.remark_error)

    def test_only_one_remark_applied_per_controller(self):
        with tempfile.TemporaryDirectory() as d:
            ctl = Path(d) / "ctl.json"
            stats = Path(d) / "stats.json"
            sock = FakeSocket()
            mc = udp_send.MarkerController(sock, stats, ctl)
            mc.apply_initial()
            mc.start_watcher()
            ctl.write_text(json.dumps(
                {"request_id": "r1", "new_dscp": 20}), encoding="utf-8")
            deadline = time.time() + 3
            while time.time() < deadline and not mc.remark_applied:
                time.sleep(0.03)
            self.assertTrue(mc.remark_applied)
            # A second request is ignored by the sender: only one switch per
            # sending round (the FlowManager also guards this server-side).
            ctl.write_text(json.dumps(
                {"request_id": "r2", "new_dscp": 30}), encoding="utf-8")
            time.sleep(0.4)
            self.assertEqual(mc.applied_request_id, "r1")
            self.assertEqual(mc.dscp, 20)
            self.assertNotIn(0x79, sock.tos_calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)
