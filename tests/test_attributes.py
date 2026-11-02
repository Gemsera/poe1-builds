"""Attribute report and advisor: have/need/margin, what sets the need, and the ordered options on a
shortfall or thin margin. Numbers come from PoB; the tests check structure, order and consistency.

Run: python3 tests/test_attributes.py      (needs scripts/setup.sh; ~30 s)
"""
import copy
import io
import contextlib
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import audit as qa  # noqa: E402
import fastpath  # noqa: E402
import pob  # noqa: E402

EXAMPLE = os.path.join(HERE, "..", "examples", "rf_chieftain.json")
READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)


def base_spec():
    with open(EXAMPLE) as f:
        return pob.load_json_lenient(f.read())


def set_gem_level(spec, name, level):
    for g in spec["skills"]:
        for i, gm in enumerate(g["gems"]):
            if (gm if isinstance(gm, str) else gm.get("name")) == name:
                g["gems"][i] = {"name": name, "level": level}


@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class Attributes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ok = base_spec()
        short = copy.deepcopy(ok)
        short["tree"]["nodes"] = [n for n in short["tree"]["nodes"] if n != "Heart of Oak"]   # the Dex on that path
        # the pathing's attribute repair would spend the points it saved on Dex nodes and hide the shortfall these
        # tests are about: switch it off here (the strict policy), see test_pathing.py for the repair itself
        short["tree"]["attribute_repair"] = False
        cls.ok, cls.short = fastpath.run_many([{"mode": "build", "spec": s, "audit": True} for s in (ok, short)], workers=1)
        # a thin (but met) margin: the Dex the route supplies depends on the pathing, so find the gem level that
        # lands within the margin target instead of assuming one
        have = cls.short["attributes"]["attributes"]["Dex"]["have"]
        cls.thin = None
        for lvl in range(20, 3, -1):
            thin = copy.deepcopy(short)
            set_gem_level(thin, "Swift Affliction", lvl)
            r = fastpath.run_one({"mode": "build", "spec": thin, "audit": True})
            if r["valid"] and "Dex" in (r["attributes"].get("thin_margin") or {}):
                cls.thin = r
                break
        assert cls.thin is not None, "no Swift Affliction level gives a thin Dex margin (have %s)" % have

    def test_every_build_reports_have_need_margin(self):
        for r in (self.ok, self.short, self.thin):
            at = r["attributes"]["attributes"]
            for a in ("Str", "Dex", "Int"):
                self.assertEqual(at[a]["margin"], at[a]["have"] - at[a]["need"])
                self.assertEqual(at[a]["have"], int(r["stats"]["attributes"][a]))
                self.assertEqual(at[a]["need"], int(r["stats"]["attributes"]["Req" + a]))
                if at[a]["need"] > 0:
                    self.assertTrue(at[a]["set_by"], a)

    def test_ok_build_has_no_advice(self):
        self.assertEqual(self.ok["attributes"]["advice"], {} if isinstance(self.ok["attributes"]["advice"], dict) else [])
        self.assertEqual(self.ok["attributes"]["shortfall"], {} if isinstance(self.ok["attributes"]["shortfall"], dict) else [])

    def test_shortfall_is_an_error_with_ordered_options(self):
        r = self.short
        self.assertFalse(r["valid"])
        self.assertTrue(any("Dexterity requirement" in e for e in r["errors"]))
        at = r["attributes"]
        self.assertEqual(at["shortfall"]["Dex"], at["attributes"]["Dex"]["need"] - at["attributes"]["Dex"]["have"])
        adv = at["advice"]["Dex"]
        self.assertTrue(adv["short"])
        orders = [o["order"] for o in adv["options"]]
        self.assertEqual(orders, sorted(orders), "options are listed cheapest first")
        kinds = {o["kind"] for o in adv["options"]}
        self.assertIn("gem_level", kinds)
        self.assertIn("amulet_base", kinds)
        self.assertIn("node", kinds)
        gem = next(o for o in adv["options"] if o["kind"] == "gem_level")
        self.assertEqual(gem["gem"], "Swift Affliction")
        self.assertLessEqual(gem["need_at_fit"] + at["margin_target"], at["attributes"]["Dex"]["have"])
        for o in adv["options"]:
            if o["kind"] == "node":
                self.assertGreaterEqual(o["gain"], 10)
                self.assertLessEqual(o["points"], 2)
                self.assertIn("not fill points", o["cost"])
        # this example's rares (Titan Greaves, a sceptre, ...) can't roll Dex on a free suffix: the advisor must say so
        self.assertIn("suffix_none", kinds)

    def test_thin_margin_is_advisory_not_error(self):
        r = self.thin
        self.assertTrue(r["valid"], r["errors"])
        at = r["attributes"]
        self.assertIn("Dex", at["thin_margin"])
        self.assertLess(at["thin_margin"]["Dex"], at["margin_target"])
        findings = qa.evaluate(r, qa.resolve_profile(base_spec().get("profile"), {}, r.get("level")))
        att = [f for f in findings if f[0] == "attributes"]
        self.assertEqual(len(att), 1)
        self.assertEqual(att[0][3], "advisory")
        gem = next(o for o in at["advice"]["Dex"]["options"] if o["kind"] == "gem_level")
        self.assertLessEqual(gem["need_at_fit"] + at["margin_target"], at["attributes"]["Dex"]["have"])

    def test_fill_never_fixes_attributes_and_says_so(self):
        s = base_spec()
        s["tree"]["nodes"] = [n for n in s["tree"]["nodes"] if n != "Heart of Oak"]
        s["tree"]["attribute_repair"] = False   # the shortfall must reach the fill for this test
        r = fastpath.run_many([{"mode": "build", "spec": s}], workers=1)[0]
        self.assertIn("fill", r)
        self.assertIn("Dex", r["fill"].get("attribute_shortfall", ""))
        for a in r["fill"]["added"]:
            self.assertFalse(a["name"] in ("Dexterity", "Strength", "Intelligence") and a["dps_pct"] == 0 and a["def_pct"] == 0,
                             "fill took a pure attribute node: %r" % a)

    def test_report_prints_the_section(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pob.print_report(self.short)
        text = buf.getvalue()
        self.assertIn("ATTRIBUTES (have / need", text)
        self.assertIn("Dex SHORT by", text)
        self.assertIn("never fill points", text)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class AnointAndCheapSlots(unittest.TestCase):
    """An anoint on a rare amulet survives the implicit rewrite and is checked; the audit names cheap unused slots
    and the suppression breakpoint."""

    @classmethod
    def setUpClass(cls):
        s = base_spec()
        for it in s["items"]:
            if it["slot"] == "Amulet":
                it["anoint"] = "Heart of Oak"
        bad = copy.deepcopy(s)
        for it in bad["items"]:
            if it["slot"] == "Amulet":
                it["anoint"] = "Not A Notable"
        cls.ok, cls.bad = fastpath.run_many([{"mode": "build", "spec": s, "audit": True}, {"mode": "build", "spec": bad}], workers=1)

    def test_anoint_is_applied_and_reported(self):
        self.assertTrue(self.ok["valid"], self.ok["errors"])
        self.assertEqual(self.ok.get("anoint"), "Heart of Oak")
        self.assertIn("Allocates Heart of Oak", self.ok["xml"])

    def test_bad_anoint_is_an_error(self):
        self.assertFalse(self.bad["valid"])
        self.assertTrue(any("noint" in e for e in self.bad["errors"]))

    def test_cheap_slot_line_and_suppression_breakpoint(self):
        findings = qa.evaluate(self.ok, qa.resolve_profile(base_spec().get("profile"), {}, self.ok.get("level")))
        cheap = [f for f in findings if f[2].startswith("Cheap slots left unused")]
        self.assertEqual(len(cheap), 1)
        self.assertIn("plain flask bases", cheap[0][2])
        self.assertNotIn("amulet has no anoint", cheap[0][2])
        # synthetic: 88% suppression is reported with the gap and the breakpoint
        r = copy.deepcopy(self.ok)
        r["audit"]["defence"]["EffectiveSpellSuppressionChance"] = 88
        f2 = [f for f in qa.evaluate(r, qa.resolve_profile(base_spec().get("profile"), {}, r.get("level"))) if "Spell suppression 88%" in f[2]]
        self.assertEqual(len(f2), 1)
        self.assertEqual(f2[0][3], "advisory")
        self.assertIn("12% short", f2[0][2])
