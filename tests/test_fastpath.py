"""Regression tests for batching, caching, preflight, repairs, audit skipping and bounded compare.

Run: python3 tests/test_fastpath.py      (needs scripts/setup.sh to have run; ~3 minutes)
"""
import argparse
import contextlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, HERE)
import fastpath  # noqa: E402
import pob  # noqa: E402
import preflight  # noqa: E402
from consistency import same_response  # noqa: E402

EX = os.path.join(HERE, "..", "examples")
READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)


def load(name):
    with open(os.path.join(EX, name)) as f:
        return pob.load_json_lenient(f.read())


class TempCache:
    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        self.p = mock.patch.object(fastpath, "CACHE_DIR", self.dir)
        self.p.start()
        os.environ.pop("POE1_BUILDS_NO_CACHE", None)
        os.environ.pop("POE1_BUILDS_LEGACY", None)
        return self

    def __exit__(self, *a):
        self.p.stop()
        shutil.rmtree(self.dir)


@unittest.skipUnless(READY, "PoB not set up")
class BatchTests(unittest.TestCase):
    def test_batch_equals_single_runs(self):
        de, rf = load("burning_arrow_vigour_deadeye.json"), load("rf_chieftain.json")
        reqs = [{"mode": "build", "spec": de, "audit": True},
                {"mode": "build", "spec": pob.apply_variant(de, {"tree_remove": ["Hired Killer"], "tree_add": ["Discipline and Training"]})},
                {"mode": "build", "spec": dict(rf, level=20), "audit": True},     # invalid
                {"mode": "build", "spec": dict(rf, **{"class": "Wizard"})},        # fatal error inside the driver
                {"mode": "gems", "query": "mirage"},
                {"mode": "build", "spec": rf}]
        with mock.patch.dict(os.environ, {"POE1_BUILDS_NO_CACHE": "1"}):
            single = [fastpath.run_one(r) for r in reqs]
            batch = fastpath.run_many(reqs, workers=1)
        for i, (a, b) in enumerate(zip(single, batch)):
            self.assertEqual(same_response(a, b), [], "request %d differs" % i)
        self.assertNotIn("audit", single[2])                    # invalid build: audit skipped
        self.assertIn("audit_skipped", single[2])
        self.assertIn("audit", single[0])


@unittest.skipUnless(READY, "PoB not set up")
class CacheTests(unittest.TestCase):
    def test_hit_miss_and_invalidation(self):
        rf = load("rf_chieftain.json")
        req = {"mode": "build", "spec": rf}
        with TempCache():
            fastpath.STATS["cache_hits"] = 0
            a = fastpath.run_one(req)
            b = fastpath.run_one(req)
            self.assertEqual(b.get("cache"), "hit")
            self.assertEqual(same_response(a, b), [])
            changed = fastpath.cache_get({"mode": "build", "spec": dict(rf, level=91)})
            self.assertIsNone(changed)                         # any input change -> miss
            self.assertIsNone(fastpath.cache_get(dict(req, audit=True)))
            with mock.patch.object(fastpath, "_fingerprint", "another-pob-version"):
                self.assertIsNone(fastpath.cache_get(req))      # PoB / driver change -> miss

    def test_invalid_and_crashed_results_not_cached(self):
        with TempCache():
            bad = {"mode": "build", "spec": dict(load("rf_chieftain.json"), level=20)}
            r = fastpath.run_one(bad)
            self.assertFalse(r["valid"])
            self.assertIsNone(fastpath.cache_get(bad))
            self.assertFalse(fastpath.cacheable({"mode": "build"}, {"valid": True, "errors": ["Driver error: boom"]}))
            self.assertFalse(fastpath.cacheable({"mode": "build"}, {"valid": True}))     # incomplete
            self.assertFalse(fastpath.cacheable({"mode": "gems"}, {"errors": ["x"]}))

    def test_no_cache_env(self):
        with TempCache():
            req = {"mode": "classes"}
            fastpath.run_one(req)
            with mock.patch.dict(os.environ, {"POE1_BUILDS_NO_CACHE": "1"}):
                self.assertIsNone(fastpath.cache_get(req))


@unittest.skipUnless(READY, "PoB not set up")
class PreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.names = preflight.Names(preflight.load_names())

    def test_gem_matcher_port_agrees_with_pob(self):
        random.seed(7)
        sample = random.sample(self.names.gem_names, 60)
        tests = []
        for n in sample:
            tests += [n, n.lower(), n.replace(" Support", ""), "".join(w[0] for w in n.split()), n[:-1]]
        tests += ["Barrage", "Mirage Archer", "Increased Duration", "fire pen"]
        resolved = fastpath.run_one({"mode": "names", "resolve_gems": tests})["results"]["resolved"]
        checked = 0
        for t, want in zip(tests, resolved):
            try:
                got = preflight.resolve_gem(t, self.names.gem_names)
            except preflight.Uncertain:
                continue
            checked += 1
            self.assertEqual(got, want, t)
        self.assertGreater(checked, 250)

    def test_examples_pass(self):
        for f in ("burning_arrow_vigour_deadeye.json", "rf_chieftain.json"):
            r = preflight.check(load(f), self.names)
            self.assertEqual((r["errors"], r["warnings"], r["repairs"]), ([], [], []), f)

    def test_detects_typos(self):
        s = load("rf_chieftain.json")
        s["skills"][0]["gems"][1] = "Fire Penatration"
        s["items"][0]["unique"] = "Kaoms Hart"
        s["pantheon"] = "Lunaris"
        s["config"]["useFrenzyCharge"] = True
        s["tree"]["nodes"].append("Heart of Oke")
        r = preflight.check(s, self.names)
        self.assertTrue(any("Fire Penatration" in e for e in r["errors"]))
        self.assertTrue(any("Kaoms Hart" in e for e in r["errors"]))
        self.assertTrue(any("Heart of Oke" in e for e in r["errors"]))
        self.assertTrue(any("pantheon" in w for w in r["warnings"]))
        self.assertTrue(any("useFrenzyCharge" in w for w in r["warnings"]))

    def test_structure_without_names(self):
        r = preflight.check({"level": 0, "skills": [{"gems": []}], "items": [{"slot": "Ring 1"}]}, None)
        self.assertEqual(len(r["errors"]), 4)                    # class, level, empty group, item source

    def test_repairs_are_exact_and_leave_original(self):
        s = load("burning_arrow_vigour_deadeye.json")
        s["level"] = 40
        s["skills"][0]["gems"][1] = {"name": "Mirage Archer", "level": 23, "quality": 25}
        s["skills"][0]["gems"][5] = "Elemental-Focus"
        before = json.dumps(s, sort_keys=True)
        r = preflight.check(s, self.names)
        self.assertEqual(r["errors"], [])
        fixed = preflight.repaired(s, r["repairs"])
        self.assertEqual(json.dumps(s, sort_keys=True), before)  # input untouched
        g = fixed["skills"][0]["gems"]
        self.assertEqual(g[1]["quality"], 20)
        self.assertEqual(g[5]["name"], "Elemental Focus")
        req = self.names.gems["Mirage Archer"]["req"]
        self.assertLessEqual(req[g[1]["level"] - 1], 40)       # usable at the character's level
        self.assertEqual(preflight.check(fixed, self.names)["repairs"], [])


class VerdictTests(unittest.TestCase):
    base = {"valid": True, "dps": 100.0, "ehp": 100.0, "min_max_hit": 10000, "min_ele_res": 75, "chaos_res": 10}
    req = {"min_ele_res": 75, "chaos_res": 0, "min_max_hit": 8000}

    def v(self, **kw):
        return pob._verdict(dict(self.base, **kw), self.base, self.req, 1.0, 1.0)

    def test_classes(self):
        self.assertEqual(self.v(dps=105), "better")
        self.assertEqual(self.v(dps=105, ehp=90), "trade-off")
        self.assertEqual(self.v(dps=95), "worse")
        self.assertEqual(self.v(dps=100.5), "no meaningful change")
        self.assertEqual(self.v(valid=False), "invalid")

    def test_dps_never_beats_a_broken_requirement(self):
        self.assertTrue(self.v(dps=200, min_ele_res=70).startswith("rejected"))
        self.assertTrue(self.v(dps=200, min_max_hit=7000).startswith("rejected"))
        self.assertTrue(self.v(dps=200, chaos_res=-5).startswith("rejected"))


@unittest.skipUnless(READY, "PoB not set up")
class CliTests(unittest.TestCase):
    def test_invalid_rebuild_keeps_last_valid_export(self):
        td = tempfile.mkdtemp()
        try:
            rf = load("rf_chieftain.json")
            rf["name"] = "keep"
            ok_path, bad_path = os.path.join(td, "ok.json"), os.path.join(td, "bad.json")
            for path, spec in ((ok_path, rf), (bad_path, dict(rf, level=20))):
                with open(path, "w") as f:
                    json.dump(spec, f)
            ns = lambda p: argparse.Namespace(spec=p, out=td, json=True, show_code=False, audit=False, investment=None,
                                              content=None, game_mode=None, share=False, timeout=5, verify=True,
                                              preflight=True, repair=False, fail_fast=False)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pob.cmd_build(ns(ok_path)), 0)
                good = _read(os.path.join(td, "keep.pob.txt"))
                self.assertEqual(pob.cmd_build(ns(bad_path)), 2)
            self.assertEqual(_read(os.path.join(td, "keep.pob.txt")), good)
            self.assertTrue(os.path.exists(os.path.join(td, "keep.invalid.pob.txt")))
        finally:
            shutil.rmtree(td)

    def test_batch_lookups_match_single_commands(self):
        lines = ['gems "mirage" --support', 'uniques "Kaom\'s Heart"', 'classes']
        pob.PARSER = None
        out_single = []
        for l in lines:
            p = io.StringIO()
            with contextlib.redirect_stdout(p):
                ap_args = _parser().parse_args(__import__("shlex").split(l))
                ap_args.fn(ap_args)
            out_single.append(p.getvalue())
        buf = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO("\n".join(lines))), contextlib.redirect_stdout(buf):
            pob.PARSER = _parser()
            pob.cmd_batch(argparse.Namespace(file="-"))
        text = buf.getvalue()
        for l, single in zip(lines, out_single):
            self.assertIn("### " + l + "\n" + single, text)


def _read(path):
    with open(path) as f:
        return f.read()


def _parser():
    return pob.build_parser()


if __name__ == "__main__":
    unittest.main(verbosity=2)
