#!/usr/bin/env python3
"""Unit tests for the PURE decision logic in verify-sup-aspect-status.py
(gate-wired, DASH-verifier test pattern). Covers SUP-WINDOWDECAY-1: the
latest-run verdict plus the 48h decay of healed ci-gate failures.
"""
import datetime
import importlib.util
import pathlib
import unittest

_spec = importlib.util.spec_from_file_location(
    "vsas", pathlib.Path(__file__).parent / "verify-sup-aspect-status.py")
vsas = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vsas)

NOW = datetime.datetime(2026, 11, 9, 12, 0, tzinfo=datetime.timezone.utc)


def run(conclusion, hours_ago):
    return {"conclusion": conclusion,
            "createdAt": (NOW - datetime.timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z"),
            "status": "completed"}


class VerificationDecisionTest(unittest.TestCase):
    def test_frozen_window_heals_green(self):
        # 2026-11-09 incident shape: first-red healed 5 min later, repo quiet since.
        runs = [run("success", 62), run("failure", 62.1), run("success", 84)]
        status, evidence = vsas.verification_decision(runs, NOW)
        self.assertEqual(status, "GREEN")
        self.assertIn("all decayed", evidence)

    def test_fresh_first_red_stays_red(self):
        runs = [run("success", 2), run("failure", 2.1), run("success", 26)]
        status, evidence = vsas.verification_decision(runs, NOW)
        self.assertEqual(status, "RED")
        self.assertIn("within 48h", evidence)

    def test_latest_failure_is_red(self):
        runs = [run("failure", 1), run("success", 2), run("failure", 3)]
        self.assertEqual(vsas.verification_decision(runs, NOW)[0], "RED")

    def test_fail_success_fail_within_hours_stays_red(self):
        runs = [run("failure", 1), run("success", 2), run("failure", 4)]
        self.assertEqual(vsas.verification_decision(runs, NOW)[0], "RED")

    def test_no_runs_is_red(self):
        self.assertEqual(vsas.verification_decision([], NOW),
                         ("RED", "no completed ci-gate runs found"))

    def test_clean_window_is_green(self):
        runs = [run("success", 1), run("success", 2), run("success", 3)]
        status, evidence = vsas.verification_decision(runs, NOW)
        self.assertEqual(status, "GREEN")
        self.assertIn("0 fail", evidence)

    def test_unparseable_fail_age_never_decays(self):
        runs = [run("success", 100), {"conclusion": "failure", "createdAt": "garbage",
                                      "status": "completed"}]
        self.assertEqual(vsas.verification_decision(runs, NOW)[0], "RED")

    def test_decay_boundary(self):
        # Exactly 48h is still fresh; the window decays only after it elapses.
        self.assertEqual(vsas.verification_decision(
            [run("success", 1), run("failure", 48)], NOW)[0], "RED")
        self.assertEqual(vsas.verification_decision(
            [run("success", 1), run("failure", 48.01)], NOW)[0], "GREEN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
