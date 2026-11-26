"""Real-process tests: DSCP re-marking over loopback, incl. late old packets.

These launch the actual standalone ``udp_send.py`` / ``udp_recv.py``
scripts (no mocks).  The sandbox loopback preserves the IP TOS byte, so the
whole re-mark -> boundary -> receiver verification path is exercised for
real here; on the Docker target the same code path runs over the veth link.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "service" / "linklab"


def free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_json(path: Path, check, timeout=8.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = json.loads(path.read_text())
            if check(last):
                return last
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"condition not met for {path}; last={last}")


class RemarkEndToEndTests(unittest.TestCase):
    def _start_pair(self, tmp: Path, port: int, reorder_every: int = 0):
        recv_stats = tmp / "recv.json"
        send_stats = tmp / "send.json"
        ctl = tmp / "ctl.json"
        plan_dir = tmp
        recv_argv = [
            sys.executable, str(SCRIPT_DIR / "udp_recv.py"),
            "--flow", f"alarm:{port}",
            "--stats-file", str(recv_stats),
            "--plan-dir", str(plan_dir),
        ]
        if reorder_every:
            recv_argv += ["--reorder-every", str(reorder_every)]
        recv = subprocess.Popen(recv_argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        time.sleep(0.8)
        send = subprocess.Popen(
            [
                sys.executable, str(SCRIPT_DIR / "udp_send.py"),
                "--dst-ip", "127.0.0.1", "--port", str(port),
                "--flow", "alarm", "--rate", "4mbit",
                "--stats-file", str(send_stats),
                "--ctl-file", str(ctl),
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        return recv, send, recv_stats, send_stats, ctl

    def test_remark_boundary_and_receiver_verification(self):
        port = free_udp_port()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            recv, send, recv_stats, send_stats, ctl = self._start_pair(tmp, port)
            try:
                # First confirm the old segment runs at DSCP 0 / ECT(0).
                _wait_json(recv_stats, lambda doc:
                           doc["flows"]["alarm"]["packets_total"] >= 20)
                ctl.write_text(json.dumps({
                    "request_id": "req-1", "new_dscp": 20,
                }), encoding="utf-8")

                # Sender applies and reports a boundary.
                send_doc = _wait_json(send_stats, lambda doc:
                                      (doc.get("marking") or {})
                                      .get("remark_request_id") == "req-1"
                                      and doc["marking"].get("boundary_seq") is not None)
                boundary = send_doc["marking"]["boundary_seq"]
                self.assertEqual(send_doc["marking"]["dscp"], 20)
                self.assertEqual(send_doc["marking"]["ecn"], 1)  # ECT(0) kept
                self.assertGreater(boundary, 0)

                # Publish the boundary plan exactly like FlowManager does.
                (tmp / "mark_plan_alarm.json").write_text(json.dumps({
                    "flow": "alarm", "request_id": "req-1",
                    "old_dscp": 0, "new_dscp": 20, "ecn": 1,
                    "boundary_seq": boundary,
                }), encoding="utf-8")

                # The receiver verifies both segments with zero mismatches.
                final = _wait_json(recv_stats, lambda doc:
                                   (doc["flows"]["alarm"].get("marking") or {})
                                   .get("verified") is True)
                marking = final["flows"]["alarm"]["marking"]
                self.assertEqual(marking["boundary_seq"], boundary)
                self.assertEqual(marking["segments"]["old"]["expected_dscp"], 0)
                self.assertEqual(marking["segments"]["new"]["expected_dscp"], 20)
                self.assertEqual(marking["segments"]["new"]["expected_ecn"], 1)
                self.assertGreater(marking["segments"]["old"]["packets"], 0)
                self.assertGreater(marking["segments"]["new"]["packets"], 0)
                self.assertEqual(marking["total_mismatch"], 0)
                self.assertEqual(marking["unknown_tos"], 0)

                # Sequence numbers kept advancing across the switch.
                self.assertEqual(send_doc["seq"] > boundary, True)
            finally:
                send.terminate()
                recv.terminate()
                for p in (send, recv):
                    try:
                        p.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        p.kill()

    def test_late_old_packet_is_classified_in_old_segment(self):
        # Hold one packet back every 8 arrivals to force reordering.
        port = free_udp_port()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            recv, send, recv_stats, send_stats, ctl = self._start_pair(
                tmp, port, reorder_every=8)
            try:
                _wait_json(recv_stats, lambda doc:
                           doc["flows"]["alarm"]["packets_total"] >= 16)
                ctl.write_text(json.dumps({
                    "request_id": "req-late", "new_dscp": 46,
                }), encoding="utf-8")
                send_doc = _wait_json(send_stats, lambda doc:
                                      (doc.get("marking") or {})
                                      .get("remark_request_id") == "req-late"
                                      and doc["marking"].get("boundary_seq") is not None)
                boundary = send_doc["marking"]["boundary_seq"]

                (tmp / "mark_plan_alarm.json").write_text(json.dumps({
                    "flow": "alarm", "request_id": "req-late",
                    "old_dscp": 0, "new_dscp": 46, "ecn": 1,
                    "boundary_seq": boundary,
                }), encoding="utf-8")

                final = _wait_json(recv_stats, lambda doc:
                                   (doc["flows"]["alarm"].get("marking") or {})
                                   .get("verified") is True, timeout=12.0)
                marking = final["flows"]["alarm"]["marking"]
                self.assertEqual(marking["boundary_seq"], boundary)
                self.assertEqual(marking["total_mismatch"], 0)
                # Both segments received packets despite the reordering.
                self.assertGreater(marking["segments"]["old"]["packets"], 0)
                self.assertGreater(marking["segments"]["new"]["packets"], 0)
                # The throughput counters kept working under reordering.
                self.assertGreater(
                    final["flows"]["alarm"]["packets_total"], 30)
            finally:
                send.terminate()
                recv.terminate()
                for p in (send, recv):
                    try:
                        p.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        p.kill()

    def test_invalid_dscp_request_keeps_old_marker(self):
        port = free_udp_port()
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            recv, send, recv_stats, send_stats, ctl = self._start_pair(tmp, port)
            try:
                _wait_json(recv_stats, lambda doc:
                           doc["flows"]["alarm"]["packets_total"] >= 10)
                # Out-of-range DSCP: the sender must reject and keep DSCP 0.
                ctl.write_text(json.dumps({
                    "request_id": "req-bad", "new_dscp": 99,
                }), encoding="utf-8")
                doc = _wait_json(send_stats, lambda d_:
                                 (d_.get("marking") or {})
                                 .get("pending_request_id") == "req-bad")
                marking = doc["marking"]
                self.assertFalse(marking["remark_applied"])
                self.assertEqual(marking["dscp"], 0)
                self.assertIsNotNone(marking["remark_error"])
                self.assertIsNone(marking["boundary_seq"])

                # Traffic continues and the receiver still verifies DSCP 0.
                later = _wait_json(recv_stats, lambda x:
                                   x["flows"]["alarm"]["packets_total"] >= 40)
                m = later["flows"]["alarm"]["marking"]
                self.assertIsNone(m["boundary_seq"])
                self.assertEqual(m["segments"]["old"]["expected_dscp"], 0)
                self.assertEqual(m["segments"]["old"]["mismatch"], 0)
            finally:
                send.terminate()
                recv.terminate()
                for p in (send, recv):
                    try:
                        p.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        p.kill()


if __name__ == "__main__":
    unittest.main(verbosity=2)
