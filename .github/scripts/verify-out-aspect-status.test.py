#!/usr/bin/env python3
"""Unit tests for the leg-1 conformance policy in verify-out-aspect-status.py
(OUT-SEMCONF-LEG1-PROPWINDOW-2; gate-wired, DASH/SUP verifier test pattern).

What is pinned here: a one-reading transient never REDs (first sighting publishes count and
ids as "propagating"); a row still wrong at a reading >= persist_min after its first sighting
REDs, including on a frozen artifact and when the row fell out of the re-drawn 400-sample;
parity stays instant RED; the leg-2 ceiling is never masked by a leg-1 sighting; the published
id list caps at ids_cap while the re-test reads the uncapped set; malformed previous payloads
never crash the check. Plus the discord timing-class owner label (evidence-only).

Hermetic by construction: the verifier's R2, Supabase, landing-API and proxy readers and its
clock are replaced in-memory for every reading; no network, no R2, no environment variable is
read for an outcome (the lag-classify URL string is built from env if present, but the lookup
itself is patched), and no file is written.

Run: python3 .github/scripts/verify-out-aspect-status.test.py
     (also wired into the Gate workflow)
"""
import copy
import datetime
import importlib.util
import io
import json
import pathlib
import unittest
import urllib.error

_spec = importlib.util.spec_from_file_location(
    "voas", pathlib.Path(__file__).parent / "verify-out-aspect-status.py")
voas = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(voas)

T0 = datetime.datetime(2026, 11, 24, 12, 2, 28, tzinfo=datetime.timezone.utc)
UUID_RE = voas._UUID_RE


def at(hours=0, minutes=0):
    return T0 + datetime.timedelta(hours=hours, minutes=minutes)


def row(i, visa, h1b=None):
    return {"id": i, "company": "Co " + i, "role": "Role " + i, "visaSignal": visa,
            "h1bSponsor": (visa == "sponsor") if h1b is None else h1b}


def crow(i, signal):
    return {"id": i, "visa_signal": signal}


def consistent(n, prefix="r", signal="none"):
    visa = {"job_sponsor": "sponsor", "company_sponsor": "possible", "none": "none"}[signal]
    rows = [row(f"{prefix}{i:04d}", visa) for i in range(n)]
    return rows, [crow(r["id"], signal) for r in rows]


def drift_blob(canon_rows, ids):
    """Blob rows that classify every mismatched id as DRIFT (Supabase in sync, artifact stale):
    the case the pre-PROPWINDOW-2 code paged on."""
    cmap = {r["id"]: r["visa_signal"] for r in canon_rows}
    return [{"job_id": f"u{i}", "source_id": i, "enriched": {"visaSignal": cmap[i]}}
            for i in ids if i in cmap]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Reading:
    """One offline reading: R2 objects, the Supabase lag-classify lookup, the landing API, the
    previously published payload and the clock are all in-memory."""

    def __init__(self, art, canon, blob=None, prev=None, now=None, blob_error=None, landing=None):
        self.art, self.canon, self.blob, self.prev, self.now = art, canon, blob or [], prev, now
        self.blob_error, self.landing = blob_error, landing or {}

    def _install(self):
        v = voas

        def fake_urlopen(req, timeout=60):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "rest/v1/jobs" in url and "details->enriched" in url:
                if self.blob_error is not None:
                    raise self.blob_error
                return _Resp(json.dumps(self.blob).encode())
            raise RuntimeError("unexpected url " + url)

        def fake_fetch(url, timeout=60):
            co = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["company"][0]
            return json.dumps({"jobs": self.landing.get(co, [])})

        v.urllib.request.urlopen = fake_urlopen
        v._r2_json = lambda key: (json.loads(json.dumps(self.art))
                                  if key == "data/sjd-jobs.json" else None)
        v._r2_json_or_jsonl = lambda key: (json.loads(json.dumps(self.canon))
                                           if key == "data/enriched_jobs.json" else None)
        v.proxy_json = lambda path: (json.loads(json.dumps(self.prev))
                                     if path == "out-aspect-status.json" and self.prev is not None
                                     else None)
        v._fetch = fake_fetch
        v._conf_resolve_ids = lambda ids: {i: i for i in ids
                                           if isinstance(i, str) and not UUID_RE.match(i)}
        return self

    def __enter__(self):
        return self._install()

    def __exit__(self, *a):
        pass

    def run(self):
        self._install()
        voas._SJD_MEMO.clear()
        voas._RIDER_STATE.clear()
        voas.NOW = self.now
        status, ev, src = voas.c_semantic_conformance()
        return status, ev, json.loads(json.dumps(voas._RIDER_STATE.get("semantic_conformance")))


def payload(rs):
    return {"rider_state": {"semantic_conformance": rs}}


def run_chain(steps):
    out, last = {}, None
    for st in steps:
        prev = last if st.get("prev", "chain") == "chain" else st["prev"]
        r = Reading(st["art"], st["canon"], blob=st.get("blob"), prev=prev, now=st["now"],
                    blob_error=st.get("blob_error"), landing=st.get("landing")).run()
        out[st["name"]] = (r, prev)
        last = payload(r[2])
    return out


def warm(art, canon, n=3):
    return [dict(name=f"warm{i}", art=art, canon=canon, now=at(i)) for i in range(n)]


class PureTriggerTest(unittest.TestCase):
    """_conformance_verdict directly: RED-first ordering."""

    LEG2_HOT = {"compared": 80, "mismatches": 20, "companies_attempted": 150,
                "company_fetch_failures": 0, "company_name_misses": 0, "distinct": True}
    PREV = {"baseline_established": True, "zero_streak": 0,
            "leg2": {"compared": 80, "mismatches": 20}}

    def leg1(self, mismatches=0, parity=0):
        return {"compared": 372, "mismatches": mismatches, "drift": mismatches, "parity": parity,
                "lag": 0, "unresolved": 28, "ids": [], "samples": []}

    def test_ceiling_reds_when_leg1_clean(self):
        r = voas._conformance_verdict(self.PREV, {"leg1": self.leg1(), "leg2": self.LEG2_HOT})
        self.assertEqual((r[0], r[1]), ("RED", "T1"))

    def test_first_sighting_never_masks_the_ceiling(self):
        cur = {"leg1": self.leg1(mismatches=1), "leg2": self.LEG2_HOT,
               "leg1_carried": None, "leg1_persisted": [], "leg1_new_ids": ["x"], "leg1_run": 1,
               "artifact_same": None, "prev_age_min": None}
        r = voas._conformance_verdict(self.PREV, cur)
        self.assertEqual((r[0], r[1]), ("RED", "T1"))

    def test_parity_instant_even_with_sighting_and_ceiling(self):
        cur = {"leg1": self.leg1(mismatches=1, parity=1), "leg2": self.LEG2_HOT,
               "leg1_carried": None, "leg1_persisted": [], "leg1_new_ids": ["x"], "leg1_run": 1,
               "artifact_same": None, "prev_age_min": None}
        r = voas._conformance_verdict(self.PREV, cur)
        self.assertEqual((r[0], r[1]), ("RED", "A"))
        self.assertIn("parity", r[5])

    def test_persisted_reds_before_the_ceiling(self):
        cur = {"leg1": self.leg1(mismatches=1), "leg2": self.LEG2_HOT,
               "leg1_carried": {"tested": 1, "still": 1, "healed": 0, "gone": 0,
                                "unresolved": 0, "parity": 0},
               "leg1_persisted": ["x"], "leg1_new_ids": [], "leg1_run": 2,
               "artifact_same": True, "prev_age_min": 60}
        r = voas._conformance_verdict(self.PREV, cur)
        self.assertEqual((r[0], r[1]), ("RED", "A"))
        self.assertIn("unchanged since the prior reading", r[5])


class TransientNeverRedsTest(unittest.TestCase):
    """F1/F6 shape: N rows wrong in one reading -> propagating, never RED; healed -> GREEN."""

    def test_two_row_transient(self):
        art0, canon0 = consistent(300)
        bad = [row("NV", "none"), row("HD", "none")]
        badc = [crow("NV", "company_sponsor"), crow("HD", "company_sponsor")]
        healed = [row("NV", "possible"), row("HD", "possible")]
        blob = drift_blob(canon0 + badc, ["NV", "HD"])
        steps = warm(art0, canon0) + [
            dict(name="R1", art=art0 + bad, canon=canon0 + badc, blob=blob, now=at(3)),
            dict(name="R2", art=art0 + healed, canon=canon0 + badc, blob=blob, now=at(4)),
        ]
        out = run_chain(steps)
        st, ev, rs = out["R1"][0]
        self.assertEqual((st, rs["trigger"], rs["leg1_state"]), ("YELLOW", "A1", "propagating"))
        self.assertEqual(rs["leg1"]["mismatches"], 2)
        self.assertEqual(rs["leg1_ids"], ["HD", "NV"])
        self.assertNotIn("RED", st)
        self.assertIn("first sighting (propagating)", ev)
        self.assertIn("[HD, NV]", ev)
        st, ev, rs = out["R2"][0]
        self.assertEqual((st, rs["trigger"], rs["leg1_state"]), ("GREEN", "none", "clean"))
        self.assertEqual(rs["leg1_carried"], {"tested": 2, "still": 0, "healed": 2, "gone": 0,
                                              "unresolved": 0, "parity": 0})
        self.assertIn("2 tested, 2 healed, 0 still wrong, 0 gone", ev)

    def test_live_incident_shape_is_not_red(self):
        """The 2026-11-24 15:02Z reading (2/376 drift, 0 parity, 0 lag, 24 unresolved) that
        paged as RED trigger A must now be a first sighting."""
        art0, canon0 = consistent(374)
        nocanon = [row(f"nc{i:02d}", "none") for i in range(24)]
        bad = [row("workday-nvidia-nvidiaexternalcareersite-JR2008387", "none"),
               row("workday-homedepot-careerdepot-Req198673", "none")]
        badc = [crow(bad[0]["id"], "company_sponsor"), crow(bad[1]["id"], "company_sponsor")]
        blob = drift_blob(canon0 + badc, [b["id"] for b in bad])
        steps = warm(art0[:300], canon0[:300]) + [
            dict(name="R1", art=art0 + bad + nocanon, canon=canon0 + badc, blob=blob, now=at(3))]
        st, ev, rs = run_chain(steps)["R1"][0]
        self.assertEqual((st, rs["trigger"]), ("YELLOW", "A1"))
        self.assertIn("leg1 2/376 drift, 0 parity (0 lag)", ev)
        self.assertIn("workday-nvidia-nvidiaexternalcareersite-JR2008387", ev)


class PersistedRedTest(unittest.TestCase):
    def test_same_row_still_wrong_reds(self):
        art0, canon0 = consistent(300)
        Y, Yc = row("Y", "none"), crow("Y", "company_sponsor")
        art, canon = art0 + [Y], canon0 + [Yc]
        blob = drift_blob(canon, ["Y"])
        steps = warm(art0, canon0) + [
            dict(name="R1", art=art, canon=canon, blob=blob, now=at(3)),
            dict(name="R2", art=copy.deepcopy(art), canon=canon, blob=blob, now=at(4)),
            dict(name="R3", art=copy.deepcopy(art), canon=canon, blob=blob, now=at(5)),
        ]
        out = run_chain(steps)
        st, ev, rs = out["R1"][0]
        self.assertEqual((st, rs["leg1_state"]), ("YELLOW", "propagating"))
        st, ev, rs = out["R2"][0]
        self.assertEqual((st, rs["trigger"], rs["leg1_state"]), ("RED", "A", "persisted"))
        self.assertIn("artifact content unchanged since the prior reading", ev)
        self.assertIn("[Y]", ev)
        st, ev, rs = out["R3"][0]
        self.assertEqual((st, rs["trigger"]), ("RED", "A"))

    def test_persistence_survives_a_sample_redraw(self):
        """The drifted row falls out of the re-drawn 400-sample at reading 2 and must still RED:
        the re-test reads the previous ids from the full artifact, not from the sample."""
        art0, canon0 = consistent(600, prefix="b")
        X, Xc = row("X-real-drift", "none"), crow("X-real-drift", "job_sponsor")
        blob = drift_blob(canon0 + [Xc], ["X-real-drift"])
        found = None
        for p in range(3, 600, 5):
            art1 = art0[:p] + [X] + art0[p:]
            if "X-real-drift" not in {r["id"] for r in voas._conf_pick(voas._conf_lcg(), art1, 400)}:
                continue
            for r in (0, 1, 5, 37, 101):
                art2 = [x for idx, x in enumerate(art1) if idx != r]
                if "X-real-drift" not in {x["id"] for x in voas._conf_pick(voas._conf_lcg(), art2, 400)}:
                    found = (art1, art2)
                    break
            if found:
                break
        self.assertIsNotNone(found, "fixture construction failed")
        art1, art2 = found
        steps = warm(art0[:300], canon0[:300]) + [
            dict(name="R1", art=art1, canon=canon0 + [Xc], blob=blob, now=at(3)),
            dict(name="R2", art=art2, canon=canon0 + [Xc], blob=blob, now=at(4)),
        ]
        out = run_chain(steps)
        st, ev, rs = out["R2"][0]
        self.assertEqual((st, rs["trigger"], rs["leg1_state"]), ("RED", "A", "persisted"))
        self.assertEqual(rs["leg1"]["mismatches"], 0)  # the sample no longer sees it
        self.assertEqual(rs["leg1_carried"]["still"], 1)

    def test_frozen_artifact_reds_with_its_own_note(self):
        art0, canon0 = consistent(300)
        Y, Yc = row("Y", "none"), crow("Y", "company_sponsor")
        art, canon = art0 + [Y], canon0 + [Yc]
        blob = drift_blob(canon, ["Y"])
        steps = warm(art0, canon0) + [
            dict(name="R1", art=art, canon=canon, blob=blob, now=at(3)),
            dict(name="R2", art=copy.deepcopy(art), canon=canon, blob=blob, now=at(4)),
            dict(name="R3", art=art0[1:] + [row("new", "none"), Y], canon=canon, blob=blob, now=at(5)),
        ]
        out = run_chain(steps)
        self.assertIn("artifact content unchanged since the prior reading", out["R2"][0][1])
        self.assertIn("artifact rebuilt since the prior reading", out["R3"][0][1])


class CapAndRetestSetTest(unittest.TestCase):
    """Fix A: the published list caps at ids_cap; the re-test reads the uncapped set."""

    def test_200_wrong_rows_persist_at_reading_2(self):
        art0, canon0 = consistent(200)
        bad = [row(f"big{i:03d}", "none") for i in range(200)]
        badc = [crow(f"big{i:03d}", "job_sponsor") for i in range(200)]
        art, canon = art0 + bad, canon0 + badc
        blob = drift_blob(canon, [r["id"] for r in bad])
        steps = warm(art0[:200], canon0[:200]) + [
            dict(name="R1", art=art, canon=canon, blob=blob, now=at(3)),
            dict(name="R2", art=copy.deepcopy(art), canon=canon, blob=blob, now=at(4)),
        ]
        out = run_chain(steps)
        st, ev, rs = out["R1"][0]
        self.assertEqual(st, "YELLOW")
        self.assertEqual(len(rs["leg1_ids"]), voas._CONF_LEG1["ids_cap"])
        self.assertEqual(len(rs["leg1_retest_ids"]), 200)
        st, ev, rs = out["R2"][0]
        self.assertEqual((st, rs["trigger"]), ("RED", "A"))
        self.assertIn("200 row(s) still wrong at a 2nd reading", ev)


class ParityTest(unittest.TestCase):
    def test_parity_reds_instantly_and_says_no_propagating(self):
        art0, canon0 = consistent(300)
        art, canon = art0 + [row("P", "none", h1b=True)], canon0 + [crow("P", "none")]
        steps = warm(art0, canon0) + [
            dict(name="R1", art=art, canon=canon, blob=[], now=at(3))]
        st, ev, rs = run_chain(steps)["R1"][0]
        self.assertEqual((st, rs["trigger"]), ("RED", "A"))
        self.assertIn("parity", ev)
        self.assertNotIn("propagating", ev)  # fix B


class BaselineAndShapeTests(unittest.TestCase):
    def test_first_ever_reading_is_baseline_and_records_ids(self):
        art0, canon0 = consistent(298)
        art, canon = art0 + [row("A", "none"), row("B", "none")], \
            canon0 + [crow("A", "company_sponsor"), crow("B", "company_sponsor")]
        blob = drift_blob(canon, ["A", "B"])
        out = run_chain([
            dict(name="R1", art=art, canon=canon, blob=blob, now=at(0)),
            dict(name="R2", art=copy.deepcopy(art), canon=canon, blob=blob, now=at(1)),
        ])
        st, ev, rs = out["R1"][0]
        self.assertEqual((st, rs["trigger"]), ("YELLOW", "baseline"))
        self.assertFalse(rs["baseline_established"])
        self.assertEqual(rs["leg1_ids"], ["A", "B"])
        st, ev, rs = out["R2"][0]
        self.assertEqual((st, rs["trigger"], rs["leg1_state"]), ("RED", "A", "persisted"))

    def test_rotating_ids_never_red_and_count_the_run(self):
        art0, canon0 = consistent(300)
        wrong = {i: row(i, "none") for i in "ABC"}
        healed = {i: row(i, "sponsor") for i in "ABC"}
        cw = [crow(i, "job_sponsor") for i in "ABC"]
        canon = canon0 + cw
        steps = warm(art0, canon0) + [
            dict(name="R1", art=art0 + [wrong["A"]], canon=canon, blob=drift_blob(canon, ["A"]), now=at(3)),
            dict(name="R2", art=art0 + [healed["A"], wrong["B"]], canon=canon, blob=drift_blob(canon, ["B"]), now=at(4)),
            dict(name="R3", art=art0 + [healed["A"], healed["B"], wrong["C"]], canon=canon, blob=drift_blob(canon, ["C"]), now=at(5)),
            dict(name="R4", art=art0 + [healed[i] for i in "ABC"], canon=canon, blob=[], now=at(6)),
        ]
        out = run_chain(steps)
        for name, run_n in (("R1", 1), ("R2", 2), ("R3", 3)):
            st, ev, rs = out[name][0]
            self.assertEqual((st, rs["trigger"], rs["leg1_state"]), ("YELLOW", "A1", "propagating"))
            self.assertEqual(rs["leg1_run"], run_n)
            if run_n > 1:
                self.assertIn(f"{run_n} consecutive hourly reading", ev)
            else:
                self.assertNotIn("consecutive hourly reading", ev)
        st, ev, rs = out["R4"][0]
        self.assertEqual((st, rs["leg1_state"]), ("GREEN", "clean"))
        self.assertEqual(rs["leg1_run"], 0)

    def test_unclassified_and_lag_mismatches_are_judged_too(self):
        """Old code: GREEN in both cases (no blob row = counted nowhere; lag = exempt)."""
        art0, canon0 = consistent(300)
        U, Uc = row("U", "none"), crow("U", "company_sponsor")
        art, canon = art0 + [U], canon0 + [Uc]
        steps = warm(art0, canon0) + [
            dict(name="R1", art=art, canon=canon, blob=[], now=at(3)),
            dict(name="R2", art=copy.deepcopy(art), canon=canon, blob=[], now=at(4)),
        ]
        out = run_chain(steps)
        self.assertEqual(out["R1"][0][2]["leg1"]["drift"], 0)  # unclassified: in neither count
        self.assertEqual((out["R1"][0][0], out["R1"][0][2]["leg1_state"]), ("YELLOW", "propagating"))
        self.assertEqual(out["R2"][0][0], "RED")

    def test_unmeasured_reading_between_keeps_fail_closed(self):
        art0, canon0 = consistent(300)
        A, Ac = row("A", "none"), crow("A", "job_sponsor")
        art, canon = art0 + [A], canon0 + [Ac]
        blob = drift_blob(canon, ["A"])
        err = urllib.error.HTTPError("https://x/rest/v1/jobs", 504, "Gateway Time-out", None, None)
        out = run_chain([
            dict(name="R1", art=art, canon=canon, blob=blob, now=at(3)),
            dict(name="R2", art=copy.deepcopy(art), canon=canon, blob=blob, blob_error=err, now=at(4)),
            dict(name="R3", art=copy.deepcopy(art), canon=canon, blob=blob, now=at(5)),
            dict(name="R4", art=copy.deepcopy(art), canon=canon, blob=blob, now=at(6)),
        ])
        st, ev, rs = out["R2"][0]
        self.assertEqual((st, rs["trigger"]), ("RED", "U"))
        st, ev, rs = out["R3"][0]
        self.assertEqual((st, rs["trigger"]), ("YELLOW", "baseline"))
        st, ev, rs = out["R4"][0]
        self.assertEqual((st, rs["trigger"], rs["leg1_state"]), ("RED", "A", "persisted"))


class GapGuardTest(unittest.TestCase):
    """Runs minutes apart are not "the next hourly reading" (push/dispatch/cron-jitter)."""

    def test_repeat_under_persist_min_stays_yellow_with_its_own_wording(self):
        art0, canon0 = consistent(300)
        A, Ac = row("A", "none"), crow("A", "job_sponsor")
        art, canon = art0 + [A], canon0 + [Ac]
        blob = drift_blob(canon, ["A"])
        same = lambda: copy.deepcopy(art)
        out = run_chain([
            dict(name="R1", art=art, canon=canon, blob=blob, now=at(3)),
            dict(name="R1b", art=same(), canon=canon, blob=blob, now=at(3, 3)),
            dict(name="R1c", art=same(), canon=canon, blob=blob, now=at(3, 29)),
            dict(name="R1d", art=same(), canon=canon, blob=blob, now=at(3, 59)),
        ])
        st, ev, rs = out["R1b"][0]
        self.assertEqual(st, "YELLOW")
        self.assertIn("still wrong (3 min since the prior reading; persistence not judged yet)", ev)
        self.assertNotIn("first sighting", ev)
        st, ev, rs = out["R1c"][0]
        self.assertIn("still wrong (26 min since the prior reading; persistence not judged yet)", ev)
        st, ev, rs = out["R1d"][0]
        self.assertEqual((st, rs["trigger"], rs["leg1_state"]), ("RED", "A", "persisted"))


class RobustnessTest(unittest.TestCase):
    def test_malformed_prev_payloads_never_crash(self):
        art0, canon0 = consistent(300)
        A, Ac = row("A", "none"), crow("A", "job_sponsor")
        art, canon = art0 + [A], canon0 + [Ac]
        blob = drift_blob(canon, ["A"])
        base = {"generated_at": at(3).isoformat(), "baseline_established": True, "zero_streak": 0,
                "leg2": {"compared": 0, "mismatches": 0}, "history": [], "artifact_fp": "nope"}
        cases = [
            dict(base, leg1_retest_ids="garbage"),
            dict(base, leg1_retest_ids=[1, None, {"a": 1}, "A"]),
            dict(base, leg1_retest_ids=["A"], history={"x": 1}),
            dict(base, leg1_retest_ids=["A"], history=[1, "x", {"ts": "bad"}]),
            dict(base, baseline_established=None),
        ]
        for i, rs_prev in enumerate(cases):
            r = Reading(copy.deepcopy(art), canon, blob=blob,
                        prev={"rider_state": {"semantic_conformance": rs_prev}}, now=at(4)).run()
            self.assertIn(r[0], ("YELLOW", "RED"), f"case {i}")

    def test_old_shape_prev_payload_is_safe(self):
        """A payload published by the pre-PROPWINDOW-2 code carries none of the new keys."""
        art0, canon0 = consistent(300)
        old = {"generated_at": at(3).isoformat(), "baseline_established": True, "zero_streak": 0,
               "leg1_strike": 0, "leg1": {"compared": 300, "drift": 0, "parity": 0, "lag": 0,
                                          "unresolved": 0},
               "leg2": {"compared": 0, "mismatches": 0, "companies_attempted": 0,
                        "company_fetch_failures": 0, "company_name_misses": 0, "distinct": True},
               "verdict": "GREEN", "trigger": "none", "like_for_like": False, "samples": []}
        r = Reading(art0, canon0, prev=payload(old), now=at(4)).run()
        self.assertEqual(r[0], "GREEN")
        self.assertEqual(len(r[2]["history"]), 1)
        self.assertTrue(r[2]["history_baseline_reset"])
        self.assertIsNone(r[2]["leg1_carried"])


class PriorIdGoneTest(unittest.TestCase):
    def test_gone_id_closes_without_red(self):
        art0, canon0 = consistent(300)
        G, Gc = row("G", "none"), crow("G", "company_sponsor")
        blob = drift_blob(canon0 + [Gc], ["G"])
        out = run_chain([
            dict(name="R1", art=art0 + [G], canon=canon0 + [Gc], blob=blob, now=at(3)),
            dict(name="R2", art=art0, canon=canon0 + [Gc], blob=blob, now=at(4)),
        ])
        st, ev, rs = out["R2"][0]
        self.assertEqual(st, "GREEN")
        self.assertIn("0 tested, 0 healed, 0 still wrong, 1 gone", ev)


class DiscordOwnerLabelTest(unittest.TestCase):
    """Evidence-only owner label on the timing classes (lead word 2026-11-25)."""

    def test_timing_classes_name_their_owners(self):
        posts = [
            dict(status="completed", conclusion="success", createdAt="2026-11-25T10:05:00Z",
                 updatedAt="2026-11-25T10:06:00Z", event="schedule"),
            dict(status="completed", conclusion="success", createdAt="2026-11-25T10:10:00Z",
                 updatedAt="2026-11-25T10:11:00Z", event="schedule"),
            dict(status="completed", conclusion="success", createdAt="2026-11-25T11:50:00Z",
                 updatedAt="2026-11-25T11:51:00Z", event="schedule"),
        ]
        fetches = [
            dict(status="completed", conclusion="success", createdAt="2026-11-25T09:55:00Z",
                 updatedAt="2026-11-25T10:12:00Z", event="schedule"),
        ]
        verify_runs = [dict(status="completed", conclusion="success", createdAt="2026-11-25T10:00:00Z",
                            updatedAt="2026-11-25T10:01:00Z", event="push")]
        art = {"timestamp": "2026-11-25T11:55:00Z", "criticalIssuesCount": {},
               "hasCriticalIssues": False}
        voas._runs_full = lambda repo, workflow, limit, fields="status,conclusion,createdAt,updatedAt,event": {
            ("zapplyjobs/jobs-data-2026", "post-to-discord.yml"): posts,
            ("zapplyjobs/jobs-data-2026", "verify-discord.yml"): verify_runs,
            ("zapplyjobs/jobs-aggregator-private", "fetch-jobs.yml"): fetches,
        }.get((repo, workflow), [])
        voas._r2_json = lambda key: art if key == "data/discord-verification.json" else None
        voas._supabase_metric = lambda key: None
        voas.proxy_json = lambda path: None
        voas.NOW = datetime.datetime(2026, 11, 25, 12, 0, tzinfo=datetime.timezone.utc)
        status, ev, src = voas.c_discord_conformance()
        self.assertEqual(status, "RED")
        self.assertIn("owner: AGG fetch wave (fetch-jobs wall: pipeline-alert check-13 aggregator runtime)", ev)
        self.assertIn("cancelled fetch runs: INF (pipeline-alert check-28 aggregator cancel rate)", ev)
        self.assertIn("if the fetch wave is healthy, the OUT poster (post-to-discord)", ev)

    def test_quiet_window_carries_no_owner_label(self):
        posts = [dict(status="completed", conclusion="success",
                      createdAt=f"2026-11-25T10:0{i}:30Z", updatedAt=f"2026-11-25T10:0{i}:40Z",
                      event="schedule") for i in range(4)]
        fetches = [dict(status="completed", conclusion="success", createdAt="2026-11-25T10:00:20Z",
                        updatedAt="2026-11-25T10:00:45Z", event="schedule")]
        art = {"timestamp": "2026-11-25T10:07:00Z", "criticalIssuesCount": {},
               "hasCriticalIssues": False}
        voas._runs_full = lambda repo, workflow, limit, fields="status,conclusion,createdAt,updatedAt,event": {
            ("zapplyjobs/jobs-data-2026", "post-to-discord.yml"): posts,
            ("zapplyjobs/jobs-aggregator-private", "fetch-jobs.yml"): fetches,
        }.get((repo, workflow), [])
        voas._r2_json = lambda key: art if key == "data/discord-verification.json" else None
        voas._supabase_metric = lambda key: None
        voas.proxy_json = lambda path: None
        voas.NOW = datetime.datetime(2026, 11, 25, 12, 0, tzinfo=datetime.timezone.utc)
        status, ev, src = voas.c_discord_conformance()
        self.assertEqual(status, "GREEN")
        self.assertNotIn("owner:", ev)


if __name__ == "__main__":
    unittest.main(verbosity=2)
