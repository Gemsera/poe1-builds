"""Tests for reference-build analysis (pob.py reference, scripts/reference.py, driver "reference" flag).

The PoB tests compare PoB results only with other PoB results from the same install; no
expected build statistics are hard-coded.

Run: python3 tests/test_reference.py      (PoB part needs scripts/setup.sh; ~1 minute)
"""
import argparse
import contextlib
import copy
import io
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, HERE)
import fastpath  # noqa: E402
import pob  # noqa: E402
import reference as ra  # noqa: E402
from consistency import same_response  # noqa: E402

EX = os.path.join(HERE, "..", "examples")
READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)


def norm_stub(**over):
    facts = {"pob_version": "2.67.2", "tree_version": "3_29", "level": 95, "main_skill": "Burning Arrow of Vigour",
             "enemy_is_boss": "Pinnacle", "conditional_config": [{"var": "useFrenzyCharges"}], "flasks": [{"name": "x"}]}
    n = {"facts": facts, "signals": {"build_enabling_uniques": ["Lioneye's Glare"], "top_tier_affixes_total": 2},
         "profile": {"investment": "high", "content": "bossing", "mode": "softcore", "level": 95}, "profile_assumed": []}
    for k, v in over.items():
        if k in facts:
            facts[k] = v
        elif k in n["profile"]:
            n["profile"][k] = v
        else:
            n[k] = v
    return n


class Comparability(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(ra.comparability(norm_stub(), norm_stub()), ("comparable", []))

    def test_hard_differences(self):
        for kw in ({"level": 90}, {"main_skill": "Righteous Fire"}, {"content": "mapping"}, {"investment": "budget"},
                   {"pob_version": "2.60.0"}, {"enemy_is_boss": "None"}):
            verdict, reasons = ra.comparability(norm_stub(), norm_stub(**kw))
            self.assertEqual(verdict, "not directly comparable", kw)
            self.assertTrue(reasons)

    def test_soft_differences(self):
        other = norm_stub(conditional_config=[])
        verdict, reasons = ra.comparability(norm_stub(), other)
        self.assertEqual(verdict, "comparable with caveats")
        self.assertIn("compare floor DPS", reasons[0])
        verdict, _ = ra.comparability(norm_stub(profile_assumed=["investment"]), norm_stub())
        self.assertEqual(verdict, "comparable with caveats")

    def test_small_level_gap_is_allowed(self):
        self.assertEqual(ra.comparability(norm_stub(), norm_stub(level=94))[0], "comparable")


@unittest.skipUnless(READY, "PoB not set up")
class ReferencePoB(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(EX, "burning_arrow_vigour_deadeye.json")) as f:
            cls.spec = pob.load_json_lenient(f.read())
        built = fastpath.run_one({"mode": "build", "spec": cls.spec})
        cls.xml = built["xml"]
        cls.plain, cls.ref = fastpath.run_many([{"mode": "check", "xml": cls.xml, "audit": True},
                                               {"mode": "check", "xml": cls.xml, "audit": True, "reference": True}], workers=2)

    def test_reference_measurements_do_not_change_the_build(self):
        self.assertEqual(self.plain["summary"], self.ref["summary"])
        diff = [k for k in same_response(self.plain, self.ref) if k not in ("audit", "warnings")]
        self.assertEqual(diff, [])
        self.assertFalse(any("did not return to its baseline" in w for w in self.ref["warnings"]))
        for k in ("baseline", "config", "flasks", "uniques", "skill_groups", "defence"):
            self.assertEqual(json.dumps(self.plain["audit"][k], sort_keys=True), json.dumps(self.ref["audit"][k], sort_keys=True), k)

    def test_reference_flag_off_by_default(self):
        self.assertNotIn("main_supports", self.plain["audit"])
        self.assertNotIn("leaf_nodes", self.plain["audit"])

    def test_every_main_support_measured(self):
        main = next(g for g in self.ref["skills"] if g.get("main"))
        supports = [g["name"] for g in main["gems"] if g.get("support")]
        self.assertEqual(sorted(s["gem"] for s in self.ref["audit"]["main_supports"]), sorted(supports))

    def test_leaf_nodes_are_allocated_notables_or_keystones(self):
        tree = self.ref["tree"]
        known = set(tree.get("notables") or []) | set(tree.get("keystones") or [])
        names = {n["name"] for n in self.ref["audit"]["leaf_nodes"]}
        self.assertTrue(names)
        for n in self.ref["audit"]["leaf_nodes"]:
            label = n["name"] + (" [%s]" % n["ascendancy"] if n.get("ascendancy") else "")
            self.assertIn(label, known)

    def test_cli_json_separates_facts_signals_and_self_comparison(self):
        path = os.path.join(EX, "burning_arrow_vigour_deadeye.json")
        tmp = os.path.join(fastpath.POB_HOME, "cache", "test_ref.xml")
        with open(tmp, "w") as f:
            f.write(self.xml)
        a = argparse.Namespace(build=tmp, against=path, investment="high", content="bossing", game_mode="softcore", json=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = pob.cmd_reference(a)
        out = json.loads(buf.getvalue())
        os.remove(tmp)
        self.assertEqual(rc, 0)
        self.assertIn("_note", out["reference"]["signals"])
        self.assertIn("dps", out["reference"]["facts"])
        self.assertEqual(out["comparability"]["verdict"], "comparable")       # same build, same profile
        self.assertEqual(out["reference"]["facts"]["dps"], out["against"]["facts"]["dps"])

    def test_invalid_reference_exit_code(self):
        bad = copy.deepcopy(self.spec)
        bad["level"] = 20
        xml = fastpath.run_one({"mode": "build", "spec": bad})["xml"]
        tmp = os.path.join(fastpath.POB_HOME, "cache", "test_ref_bad.xml")
        with open(tmp, "w") as f:
            f.write(xml)
        a = argparse.Namespace(build=tmp, against=None, investment=None, content=None, game_mode=None, json=True)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = pob.cmd_reference(a)
        os.remove(tmp)
        self.assertEqual(rc, 2)


def _ref_cli(xml, adjust=None):
    tmp = os.path.join(fastpath.POB_HOME, "cache", "test_ref_cli_%d.xml" % os.getpid())
    with open(tmp, "w") as f:
        f.write(xml)
    a = argparse.Namespace(build=tmp, against=None, investment="high", content="mapping", game_mode="softcore",
                           json=True, adjust=adjust)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = pob.cmd_reference(a)
    finally:
        os.remove(tmp)
    return rc, json.loads(buf.getvalue())


@unittest.skipUnless(READY, "PoB not set up")
class InvalidAndPartialReferences(unittest.TestCase):
    """An invalid reference keeps its PoB measurements; nothing is repaired silently."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(EX, "burning_arrow_vigour_deadeye.json")) as f:
            spec = pob.load_json_lenient(f.read())
        cls.valid_xml = fastpath.run_one({"mode": "build", "spec": spec})["xml"]
        # flasks equipped but switched off, as PoB imports them
        # (PoB's attribute order varies between runs, so edit whole Slot tags)
        cls.flasks_off_xml = re.sub(r'<Slot [^>]*name="Flask [1-5]"[^>]*/>',
                                    lambda m: m.group(0).replace(' active="true"', ""), cls.valid_xml)
        assert cls.flasks_off_xml != cls.valid_xml
        under = copy.deepcopy(spec)
        under["level"] = 20                                   # gem/item requirements fail: invalid
        cls.invalid_xml = fastpath.run_one({"mode": "build", "spec": under})["xml"]
        nobow = copy.deepcopy(spec)
        nobow["items"] = [i for i in nobow["items"] if i.get("slot") != "Weapon 1"]   # main skill needs a bow
        cls.nobow_xml = fastpath.run_one({"mode": "build", "spec": nobow})["xml"]
        cls.direct = dict(zip(("valid", "flasks_off", "invalid", "nobow"), fastpath.run_many(
            [{"mode": "check", "xml": x} for x in (cls.valid_xml, cls.flasks_off_xml, cls.invalid_xml, cls.nobow_xml)],
            workers=2)))

    def test_invalid_reference_keeps_measurements_and_lists_problems(self):
        rc, out = _ref_cli(self.invalid_xml)
        f = out["reference"]["facts"]
        self.assertEqual(rc, 2)
        self.assertFalse(f["valid"])
        self.assertEqual(f["validity_problems"], self.direct["invalid"]["errors"])
        self.assertTrue(f["validity_problems"])
        self.assertIn("INVALID", f["analysis_scope"])
        self.assertEqual(f["dps"], self.direct["invalid"]["summary"]["dps"])    # the build as given
        self.assertTrue(f["life"] and f["max_hit"])
        self.assertTrue(f["audit_findings"])                                  # audit ran on the invalid build

    def test_inactive_flasks_are_measured_as_given_and_adjustment_is_separate(self):
        rc, out = _ref_cli(self.flasks_off_xml)
        self.assertNotIn("adjusted", out)
        self.assertIn("--adjust flasks-on", out["note"])
        self.assertEqual(out["reference"]["facts"]["dps"], self.direct["flasks_off"]["summary"]["dps"])
        rc2, out2 = _ref_cli(self.flasks_off_xml, adjust="flasks-on")
        self.assertEqual(out2["reference"]["facts"]["dps"], out["reference"]["facts"]["dps"])   # original unchanged
        adj = out2["adjusted"]
        self.assertTrue(adj["adjustments"] and "flask" in adj["adjustments"][0])
        self.assertEqual(adj["facts"]["dps"], self.direct["valid"]["summary"]["dps"])          # = flasks-on build
        self.assertNotEqual(adj["facts"]["dps"], out["reference"]["facts"]["dps"])

    def test_adjust_is_a_no_op_when_flasks_are_already_on(self):
        rc, out = _ref_cli(self.valid_xml, adjust="flasks-on")
        self.assertEqual(rc, 0)
        self.assertNotIn("adjusted", out)
        self.assertNotIn("note", out)

    def test_partially_analysable_reference(self):
        r = self.direct["nobow"]
        self.assertTrue(r["summary"].get("main_skill_disabled"), r["summary"])
        rc, out = _ref_cli(self.nobow_xml)
        f = out["reference"]["facts"]
        self.assertFalse(f["valid"])
        self.assertTrue(any("disabled" in e for e in f["validity_problems"]))
        self.assertTrue(any(u.startswith("damage") for u in f["unavailable"]), f["unavailable"])
        self.assertTrue(f["life"] and f["max_hit"].get("Physical"))           # defences still reported

    def test_unavailable_is_empty_for_a_clean_reference(self):
        rc, out = _ref_cli(self.valid_xml)
        self.assertEqual(out["reference"]["facts"]["analysis_scope"], "full")
        self.assertEqual(out["reference"]["facts"]["unavailable"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
