from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from linklab.model import DEFAULT_CONFIG, ClassCfg, LabConfig  # noqa: E402
from linklab.runner import CmdResult, FakeRunner  # noqa: E402
from linklab.tc_argv import Topology  # noqa: E402
from linklab.tc_manager import FAULT_TOKEN, TcManager  # noqa: E402


def make_manager(handler=None):
    tmp = tempfile.TemporaryDirectory()
    topo = Topology(src_netns=None, dst_netns=None)  # plain argv for assertions
    runner = FakeRunner(handler)
    mgr = TcManager(topo, runner, Path(tmp.name) / "state.json")
    return mgr, runner, tmp


class ApplySuccessTests(unittest.TestCase):
    def test_successful_apply_uses_only_class_change(self):
        mgr, runner, tmp = make_manager()
        try:
            new = LabConfig(8000, ClassCfg(1600, 8000), ClassCfg(4800, 8000))
            res = mgr.apply(new)
            self.assertTrue(res["ok"], res.get("reason"))
            self.assertEqual(mgr.current(), new)
            self.assertTrue(len(runner.calls) >= 3)
            for c in runner.calls:
                self.assertIn("class", c)
                self.assertIn("change", c)
                # Never tear down the queue during an online update.
                self.assertNotIn("del", c)
            # state persisted
            self.assertTrue((Path(tmp.name) / "state.json").exists())
        finally:
            tmp.cleanup()

    def test_invalid_target_runs_nothing(self):
        mgr, runner, tmp = make_manager()
        try:
            bad = LabConfig(10_000, ClassCfg(8000, 10_000), ClassCfg(8000, 10_000))
            res = mgr.apply(bad)
            self.assertFalse(res["ok"])
            self.assertTrue(res.get("rejected"))
            self.assertEqual(runner.calls, [])
            self.assertEqual(mgr.current(), DEFAULT_CONFIG)
        finally:
            tmp.cleanup()


class FailureRollbackTests(unittest.TestCase):
    def test_stub_fault_midway_restores_before(self):
        mgr, runner, tmp = make_manager()
        try:
            before = mgr.current()
            new = LabConfig(8000, ClassCfg(1600, 8000), ClassCfg(4800, 8000))
            res = mgr.apply(new, fault_step=2, fault_mode="stub")

            self.assertFalse(res["ok"])
            self.assertIsNotNone(res["failure"])
            self.assertEqual(res["failure"]["seq"], 2)
            self.assertTrue(res["rollback"]["ok"], res["rollback"].get("reason"))
            self.assertEqual(mgr.current(), before)
            self.assertEqual(res["config_after"], before.to_dict())

            # Forward: exactly steps 1 attempted ok, step 2 failed, no step 3+.
            seqs = [s["seq"] for s in res["steps"]]
            self.assertEqual(seqs, [1, 2])
            self.assertEqual(res["steps"][0]["rc"], 0)
            self.assertNotEqual(res["steps"][1]["rc"], 0)

            # Rollback steps all invariant-clean.
            for s in res["rollback"]["steps"]:
                self.assertTrue(s["planned_invariant_ok"])
                self.assertEqual(s["rc"], 0)

            # The reason is surfaced with the injected stderr.
            self.assertIn("故障注入", res["reason"])
        finally:
            tmp.cleanup()

    def test_real_fault_token_makes_runner_fail_and_rollback(self):
        def handler(argv):
            if FAULT_TOKEN in argv:
                return CmdResult(
                    argv=argv, rc=1,
                    stderr=f'tc: error: unknown option "{FAULT_TOKEN}"',
                )
            return None

        mgr, runner, tmp = make_manager(handler)
        try:
            new = LabConfig(12_000, ClassCfg(3000, 12_000), ClassCfg(7000, 12_000))
            res = mgr.apply(new, fault_step=1, fault_mode="real")
            self.assertFalse(res["ok"])
            self.assertIn(FAULT_TOKEN, res["failure"]["cmd"])
            self.assertIn("unknown option", res["reason"])
            self.assertTrue(res["rollback"]["ok"])
            self.assertEqual(mgr.current(), DEFAULT_CONFIG)
            # The bad argv really reached the runner.
            self.assertTrue(any(FAULT_TOKEN in c for c in runner.calls))
        finally:
            tmp.cleanup()

    def test_rollback_command_failure_is_reported_as_drift(self):
        # Fail the 2nd mutating change and every change call after it,
        # so both the forward apply and the rollback hit errors.
        counts = {"n": 0}

        def counting_handler(argv):
            if "change" in argv:
                counts["n"] += 1
                # forward call #2 fails, and all calls after that fail too
                if counts["n"] >= 2:
                    return CmdResult(argv=argv, rc=1, stderr="boom")
            return None

        mgr, runner, tmp = make_manager(counting_handler)
        try:
            new = LabConfig(8000, ClassCfg(1600, 8000), ClassCfg(4800, 8000))
            res = mgr.apply(new, fault_step=None)
            # Forward failed at its 2nd change; rollback commands also fail
            # because the handler keeps failing call counts >= 2.
            self.assertFalse(res["ok"])
            self.assertFalse(res["rollback"]["ok"])
            self.assertTrue(res["rollback"].get("drift"))
            self.assertIn("boom", res["rollback"]["reason"])
        finally:
            tmp.cleanup()

    def test_demo_failure_end_to_end_logic(self):
        mgr, runner, tmp = make_manager()
        try:
            res = mgr.demo_failure()
            self.assertFalse(res["ok"])
            self.assertTrue(res["rollback"]["ok"])
            self.assertEqual(mgr.current(), DEFAULT_CONFIG)
            self.assertIsNotNone(res["fault"])
        finally:
            tmp.cleanup()

    def test_fault_on_unchanged_config_is_rejected(self):
        mgr, runner, tmp = make_manager()
        try:
            res = mgr.apply(DEFAULT_CONFIG, fault_step=1, fault_mode="stub")
            self.assertFalse(res["ok"])
            self.assertTrue(res.get("rejected"))
            self.assertEqual(runner.calls, [])
            self.assertEqual(mgr.current(), DEFAULT_CONFIG)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
