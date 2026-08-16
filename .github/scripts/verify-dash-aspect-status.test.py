#!/usr/bin/env python3
"""Unit tests for the PURE decision logic in verify-dash-aspect-status.py.

Covers the boundary values where this verifier's history had pre-ship bugs
(DASH-VERIFIER-TESTS-1): divergence bands (0.5%/5%), freshness bands (30m/6h),
latency budget (5s/15s), check-run status transitions (missing/in_progress/
completed x success/failure), and the unauth probe ladder (302/200/500).

Run: python3 .github/scripts/verify-dash-aspect-status.test.py
     (also wired into the Gate workflow)
"""
import importlib.util
import pathlib
import unittest

_spec = importlib.util.spec_from_file_location(
    "vdas", pathlib.Path(__file__).parent / "verify-dash-aspect-status.py")
vdas = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vdas)


class ClassifyCheckTest(unittest.TestCase):
    def test_missing_check_is_yellow_transient(self):
        self.assertEqual(vdas.classify_check(None, "verify"),
                         ("YELLOW", "no 'verify' check on main HEAD"))

    def test_in_progress_is_yellow_not_red(self):
        # The fresh-push false-RED class: a not-yet-completed check-run must
        # never page (check-40) — it is a transient, not a failure.
        for status in ("in_progress", "queued", "pending"):
            self.assertEqual(
                vdas.classify_check({"status": status}, "Workers Builds")[0],
                "YELLOW", status)

    def test_completed_success_is_green(self):
        s, msg = vdas.classify_check({"status": "completed", "conclusion": "success"}, "verify")
        self.assertEqual((s, msg), ("GREEN", "verify success on main HEAD"))

    def test_completed_failure_is_red(self):
        for conclusion in ("failure", "cancelled", "timed_out"):
            self.assertEqual(
                vdas.classify_check({"status": "completed", "conclusion": conclusion},
                                    "Workers Builds")[0], "RED", conclusion)


class UnauthProbeTest(unittest.TestCase):
    def test_200_is_exposure_red(self):
        self.assertEqual(vdas.classify_unauth(200), "RED")

    def test_redirect_and_deny_are_not_red(self):
        # The redirect-follow false-RED class: 3xx is TERMINAL denial (Access
        # login redirect) — an opener that followed it would read 200 and page.
        for code in (302, 301, 307, 401, 403, 500):
            self.assertEqual(vdas.classify_unauth(code), "YELLOW", code)


class DivergenceBandTest(unittest.TestCase):
    def test_agree_below_half_percent(self):
        self.assertEqual(vdas.divergence_band(94600, 94647)[0], "agree")

    def test_boundary_half_percent_is_lagging(self):
        # 0.5% exactly is NOT agree (strict <) — band boundaries are load-bearing.
        self.assertEqual(vdas.divergence_band(1005, 1000), ("lagging", 0.5))

    def test_lagging_below_five_percent(self):
        self.assertEqual(vdas.divergence_band(1049, 1000), ("lagging", 4.9))

    def test_boundary_five_percent_is_broken(self):
        self.assertEqual(vdas.divergence_band(1050, 1000), ("broken", 5.0))

    def test_direction_agnostic(self):
        # Served BELOW R2 (stale trailing row) is the same magnitude of lag.
        self.assertEqual(vdas.divergence_band(950, 1000), ("broken", 5.0))

    def test_not_computable(self):
        self.assertEqual(vdas.divergence_band(None, 1000), (None, None))
        self.assertEqual(vdas.divergence_band(1000, None), (None, None))
        self.assertEqual(vdas.divergence_band(1000, 0), (None, None))


class FreshnessBandTest(unittest.TestCase):
    def test_live_under_30m(self):
        self.assertEqual(vdas.freshness_band(0), "GREEN")
        self.assertEqual(vdas.freshness_band(29.9), "GREEN")

    def test_boundary_30m_is_delayed(self):
        self.assertEqual(vdas.freshness_band(30), "YELLOW")
        self.assertEqual(vdas.freshness_band(359.9), "YELLOW")

    def test_boundary_6h_is_down(self):
        self.assertEqual(vdas.freshness_band(360), "RED")
        self.assertEqual(vdas.freshness_band(720), "RED")


class LatencyBandTest(unittest.TestCase):
    def test_under_5s_green(self):
        self.assertEqual(vdas.latency_band(0.1), "GREEN")
        self.assertEqual(vdas.latency_band(4.9), "GREEN")

    def test_5s_to_15s_yellow(self):
        self.assertEqual(vdas.latency_band(5), "YELLOW")
        self.assertEqual(vdas.latency_band(14.9), "YELLOW")

    def test_at_15s_red(self):
        self.assertEqual(vdas.latency_band(15), "RED")
        self.assertEqual(vdas.latency_band(60), "RED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
