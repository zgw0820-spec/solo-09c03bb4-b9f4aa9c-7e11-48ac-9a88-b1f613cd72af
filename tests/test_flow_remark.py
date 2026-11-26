"""FlowManager integration: start a real sender and drive one re-mark."""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from linklab.flows import FlowManager  # noqa: E402
from linklab.tc_argv import Topology  # noqa: E402


def _wait(path: Path, check, timeout=8.0):
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
    raise AssertionError(f"timeout waiting on {path}; last={last}")


class FlowManagerRemarkTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        # No netns in the sandbox: sender/receiver both use loopback.
        self.topo = Topology(
            src_netns=None, dst_netns=None, dst_ip="127.0.0.1",
        )
        self.fm = FlowManager(
            run_dir=self.run_dir, topo=self.topo, start_receiver=True
        )

    def tearDown(self):
        self.fm.stop_all()
        self._tmp.cleanup()

    def test_full_remark_lifecycle_and_one_shot_guard(self):
        start = self.fm.start_flow("alarm", rate=4)
        self.assertTrue(start["started"], start)

        recv_stats = self.run_dir / "recv_stats.json"
        _wait(recv_stats, lambda d: d["flows"]["alarm"]["packets_total"] >= 20)

        res = self.fm.remark_flow("alarm", 20)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["new_dscp"], 20)
        self.assertEqual(res["old_dscp"], 0)
        boundary = res["boundary_seq"]
        self.assertGreaterEqual(boundary, 0)
        # Plan file published for the receiver.
        plan = json.loads((self.run_dir / "mark_plan_alarm.json").read_text())
        self.assertEqual(plan["boundary_seq"], boundary)
        self.assertEqual(plan["new_dscp"], 20)

        # Receiver verifies both segments.
        final = _wait(
            recv_stats,
            lambda d: (d["flows"]["alarm"].get("marking") or {})
            .get("verified") is True,
            timeout=10.0,
        )
        marking = final["flows"]["alarm"]["marking"]
        self.assertEqual(marking["total_mismatch"], 0)
        self.assertEqual(marking["segments"]["new"]["confirmed"], True)

        # One switch per round only.
        again = self.fm.remark_flow("alarm", 30)
        self.assertFalse(again["ok"])
        self.assertIn("一次", again["reason"])

        # Input validation.
        bad = self.fm.remark_flow("alarm", 0)
        self.assertFalse(bad["ok"])
        self.assertIn("1~63", bad["reason"])

    def test_remark_requires_running_sender_and_restart_resets(self):
        # Not running -> rejected with guidance, old marker retained.
        res = self.fm.remark_flow("alarm", 20)
        self.assertFalse(res["ok"])
        self.assertIn("未运行", res["reason"])

        start = self.fm.start_flow("alarm", rate=4)
        self.assertTrue(start["started"], start)
        recv_stats = self.run_dir / "recv_stats.json"
        _wait(recv_stats, lambda d: d["flows"]["alarm"]["packets_total"] >= 10)
        ok = self.fm.remark_flow("alarm", 20)
        self.assertTrue(ok["ok"], ok)

        # Stop + start resets the round: a fresh re-mark is allowed and the
        # stale control/plan files were cleaned.
        self.fm.stop_flow("alarm")
        time.sleep(0.3)
        self.assertFalse((self.run_dir / "mark_plan_alarm.json").exists())
        start2 = self.fm.start_flow("alarm", rate=4)
        self.assertTrue(start2["started"], start2)
        _wait(recv_stats, lambda d: d["flows"]["alarm"]["packets_total"] >= 5)
        ok2 = self.fm.remark_flow("alarm", 30)
        self.assertTrue(ok2["ok"], ok2)
        self.assertEqual(ok2["new_dscp"], 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
