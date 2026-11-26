from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))

from linklab.model import (  # noqa: E402
    DEFAULT_CONFIG,
    ClassCfg,
    LabConfig,
    invariant_snapshot,
    violations,
)
from linklab.planner import PlanError, build_plan  # noqa: E402


def cfg(T, ar, ac, vr, vc):
    return LabConfig(T, ClassCfg(ar, ac), ClassCfg(vr, vc))


class ModelValidationTests(unittest.TestCase):
    def test_default_is_valid(self):
        self.assertEqual(violations(DEFAULT_CONFIG), [])
        snap = invariant_snapshot(DEFAULT_CONFIG)
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["guarantee_sum_kbps"], 8000)

    def test_sum_of_guarantees_must_fit(self):
        bad = cfg(10_000, 6000, 10_000, 5000, 10_000)
        errs = violations(bad)
        self.assertTrue(any("保底之和" in e for e in errs))

    def test_ceil_must_fit_total(self):
        bad = cfg(10_000, 2000, 12_000, 6000, 9000)
        errs = violations(bad)
        self.assertTrue(any("告警速率上限" in e for e in errs))

    def test_rate_must_not_exceed_ceil(self):
        bad = cfg(10_000, 4000, 3000, 4000, 10_000)
        errs = violations(bad)
        self.assertTrue(any("rate <= ceil" in e for e in errs))


class PlannerOrderingTests(unittest.TestCase):
    def test_identical_config_has_no_steps(self):
        plan = build_plan(DEFAULT_CONFIG, DEFAULT_CONFIG)
        self.assertEqual(plan.steps, [])

    def test_total_increase_happens_first(self):
        new = cfg(20_000, 4000, 20_000, 12_000, 20_000)
        plan = build_plan(DEFAULT_CONFIG, new)
        self.assertGreater(len(plan.steps), 0)
        self.assertEqual(plan.steps[0].operation.target, "root")
        self.assertEqual(plan.steps[0].effective_total_kbps, 20_000)
        for s in plan.steps:
            self.assertTrue(s.invariant_ok, s.violations)

    def test_total_decrease_happens_last(self):
        new = cfg(8000, 1600, 8000, 4800, 8000)
        plan = build_plan(DEFAULT_CONFIG, new)
        self.assertGreater(len(plan.steps), 1)
        self.assertEqual(plan.steps[-1].operation.target, "root")
        self.assertEqual(plan.steps[-1].effective_total_kbps, 8000)
        for s in plan.steps:
            self.assertTrue(s.invariant_ok, s.violations)
            self.assertLessEqual(
                s.after.alarm.rate_kbps + s.after.video.rate_kbps,
                s.effective_total_kbps,
            )
            self.assertLessEqual(s.after.alarm.ceil_kbps, s.effective_total_kbps)
            self.assertLessEqual(s.after.video.ceil_kbps, s.effective_total_kbps)

    def test_rebalance_swap_never_overcommits(self):
        old = cfg(10_000, 6000, 10_000, 2000, 10_000)
        new = cfg(10_000, 2000, 10_000, 6000, 10_000)
        plan = build_plan(old, new)
        for s in plan.steps:
            self.assertTrue(s.invariant_ok, s.violations)
            self.assertLessEqual(
                s.after.alarm.rate_kbps + s.after.video.rate_kbps, 10_000
            )
        self.assertEqual(plan.steps[-1].after, new)

    def test_invalid_target_rejected(self):
        bad = cfg(10_000, 8000, 10_000, 8000, 10_000)
        with self.assertRaises(PlanError):
            build_plan(DEFAULT_CONFIG, bad)

    def test_rollback_plan_is_also_clean(self):
        new = cfg(6000, 1000, 6000, 4000, 6000)
        fwd = build_plan(DEFAULT_CONFIG, new)
        # Simulate failure after the second forward step: current kernel
        # state equals fwd.steps[1].after.
        current = fwd.steps[1].after
        rb = build_plan(current, DEFAULT_CONFIG)
        for s in rb.steps:
            self.assertTrue(s.invariant_ok, s.violations)
        self.assertEqual(rb.steps[-1].after, DEFAULT_CONFIG)


class PlannerFuzzTests(unittest.TestCase):
    def test_every_valid_pair_produces_clean_path(self):
        import random

        rng = random.Random(20261125)
        checked = 0
        for _ in range(4000):
            T = rng.choice([1000, 2000, 5000, 8000, 10_000, 20_000, 100_000])
            # guarantees that fit together
            ar = rng.randint(64, max(65, T - 64))
            vr = rng.randint(64, max(65, T - ar))
            ac = rng.randint(ar, T)
            vc = rng.randint(vr, T)
            a = cfg(T, ar, ac, vr, vc)

            T2 = rng.choice([1000, 2000, 5000, 8000, 10_000, 20_000, 100_000])
            ar2 = rng.randint(64, max(65, T2 - 64))
            vr2 = rng.randint(64, max(65, T2 - ar2))
            ac2 = rng.randint(ar2, T2)
            vc2 = rng.randint(vr2, T2)
            b = cfg(T2, ar2, ac2, vr2, vc2)

            plan = build_plan(a, b)
            cur = a
            for s in plan.steps:
                self.assertTrue(s.invariant_ok, s.violations)
                # The advertised effective total is the post-step root rate.
                self.assertEqual(s.effective_total_kbps, s.after.total_kbps)
                # Re-validate the advertised post-state from scratch.
                self.assertEqual(violations(s.after), [])
                cur = s.after
            self.assertEqual(cur, b)

            # And the reverse path (rollback direction) too.
            back = build_plan(b, a)
            for s in back.steps:
                self.assertTrue(s.invariant_ok, s.violations)
                self.assertEqual(violations(s.after), [])
            checked += 1
        self.assertGreater(checked, 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
