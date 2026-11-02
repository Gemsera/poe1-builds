"""Awakened gems are drop-disabled since 3.29 (legacy items, Standard only): a build spec that names one is
invalid unless it says legacy_gems; --repair swaps in the regular gem at its own max level and names the
Greater gem where one exists; an imported build only gets a warning; suggestions never offer Awakened gems.

Run: python3 tests/test_awakened.py      (needs scripts/setup.sh; ~1 minute)
"""
import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import fastpath  # noqa: E402
import pob  # noqa: E402
import preflight  # noqa: E402

READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)


def spec_with_awakened():
    with open(os.path.join(HERE, "fixtures", "ls_berserker.json")) as f:
        s = pob.load_json_lenient(f.read())
    for g in s["skills"]:
        if g.get("main"):
            g["gems"][1] = {"name": "Awakened Multistrike", "level": 5}
    return s


def spec_with_main(gems):
    with open(os.path.join(HERE, "fixtures", "ls_berserker.json")) as f:
        s = pob.load_json_lenient(f.read())
    for g in s["skills"]:
        if g.get("main"):
            g["gems"] = gems
    return s


@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class Awakened(unittest.TestCase):
    def test_preflight_repairs_to_regular_gem_and_level(self):
        pf = preflight.check(spec_with_awakened(), preflight.Names(preflight.load_names()))
        texts = [r[0] for r in pf["repairs"]]
        self.assertTrue(any("Awakened Multistrike" in t and "-> Multistrike" in t and "Greater Multistrike" in t for t in texts), texts)
        self.assertTrue(any("level 5 was the Awakened gem's level -> 20" in t for t in texts), texts)
        fixed = preflight.repaired(spec_with_awakened(), pf["repairs"])
        gem = [g for g in fixed["skills"] if g.get("main")][0]["gems"][1]
        self.assertEqual((gem["name"], gem["level"]), ("Multistrike", 20))

    def test_build_rejects_without_repair_and_accepts_legacy(self):
        r = fastpath.run_one({"mode": "build", "spec": spec_with_awakened()})
        self.assertFalse(r["valid"])
        self.assertTrue(any("Awakened gems are not obtainable" in e for e in r["errors"]), r["errors"])
        s = spec_with_awakened()
        s["legacy_gems"] = True
        r2 = fastpath.run_one({"mode": "build", "spec": s})
        self.assertTrue(r2["valid"], r2["errors"])
        self.assertFalse(any("Awakened" in e for e in r2["errors"]))

    def test_check_mode_only_warns(self):
        s = spec_with_awakened()
        s["legacy_gems"] = True
        r = fastpath.run_one({"mode": "build", "spec": s})
        c = fastpath.run_one({"mode": "check", "xml": r["xml"]})
        self.assertTrue(c["valid"], c["errors"])
        self.assertTrue(any("Awakened gems are not obtainable" in w for w in c["warnings"]), c["warnings"])

    def test_suggestions_never_offer_awakened(self):
        names = preflight.Names(preflight.load_names())
        self.assertFalse(any(n.startswith("Awakened") for n in preflight._suggest("Awakened Multistrik", names.gem_names, 5)))
        r = fastpath.run_one({"mode": "build", "spec": dict(spec_with_awakened(), legacy_gems=True)})
        s = spec_with_awakened()
        [g for g in s["skills"] if g.get("main")][0]["gems"][1] = "Awakend Multistrike"
        r = fastpath.run_one({"mode": "build", "spec": s})
        for e in r["errors"]:
            if "not found" in e:
                self.assertNotIn("Awakened", e)




@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class ExactGemNames(unittest.TestCase):
    """Gem names resolve exactly: PoB's own abbreviation matching turned 'Onslaught' (a support that no longer
    exists) into 'Frenzy of Onslaught', an active gem, and silently doubled a build's DPS."""

    def test_abbreviation_is_not_a_gem(self):
        s = spec_with_main(["Flicker Strike", "Onslaught"])
        r = fastpath.run_one({"mode": "build", "spec": s}, 900)
        self.assertFalse(r["valid"])
        self.assertTrue(any("Onslaught" in e and "not found" in e for e in r["errors"]), r["errors"])
        names = [g["name"] for grp in r.get("skills") or [] for g in grp.get("gems") or []]
        self.assertNotIn("Frenzy of Onslaught", names)

    def test_support_suffix_prefers_the_support(self):
        s = spec_with_main(["Flicker Strike", "Barrage Support", "Faster Attacks"])
        r = fastpath.run_one({"mode": "build", "spec": s}, 900)
        main = next(g for g in r["skills"] if g.get("main"))
        names = [g["name"] for g in main["gems"]]
        self.assertIn("Barrage Support", names)
        self.assertEqual(names.count("Barrage"), 0)


if __name__ == "__main__":
    unittest.main()
