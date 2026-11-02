"""Crafted rare templates in `candidates` (weapon and amulet): the best legal affixes for this build on the equipped
base, each measured by PoB, assembled with one mod per group, the equipped item's life/resistance/attribute lines
kept, tiers capped for the investment. See references/candidates-rare-templates.md.

Run: python3 tests/test_rare_templates.py      (needs scripts/setup.sh; ~3 minutes)
"""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import candidates as cd  # noqa: E402
import fastpath  # noqa: E402
import pob  # noqa: E402

READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)
FIX = os.path.join(HERE, "fixtures", "ls_berserker.json")

WEAK_MACE = ("Rarity: RARE\nDull Edge\nEternal Sword\nItem Level: 84\n60% increased Physical Damage\n12% increased Attack Speed\n"
             "+25 to Strength\n+20 to Dexterity")


def spec_weak():
    with open(FIX) as f:
        s = pob.load_json_lenient(f.read())
    for it in s["items"]:
        if it["slot"] == "Weapon 1":
            it["text"] = WEAK_MACE
    return s


def run_cand(spec, **opts):
    opts.setdefault("max_evals", 60)
    opts.setdefault("rare_evals", 60)
    opts.setdefault("investment", "medium")
    return fastpath.run_one({"mode": "build", "spec": spec, "candidates": opts}, 900)


def templates(c, slot):
    g = next(g for g in c["groups"] if g["target"] == slot)
    return [x for x in g["candidates"] if x.get("kind") == "rare"]


@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class RareTemplates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = spec_weak()
        cls.r = run_cand(cls.spec)
        cls.c = cls.r["candidates"]

    def test_templates_exist_for_both_slots_and_are_measured(self):
        rt = self.c["rare_templates"]
        self.assertEqual([s["slot"] for s in rt["slots"]], ["Weapon 1", "Amulet"])
        for slot in ("Weapon 1", "Amulet"):
            tpl = templates(self.c, slot)
            self.assertTrue(tpl, slot)
            for t in tpl:
                self.assertEqual(t["status"], "evaluated")
                self.assertTrue(t["spec_item"]["text"].startswith("Rarity: RARE"))

    def test_weak_weapon_is_beaten_and_its_attributes_kept(self):
        tpl = templates(self.c, "Weapon 1")
        best = max(tpl, key=lambda t: t["eval"]["dps_pct"])
        self.assertGreater(best["eval"]["dps_pct"], 10)
        text = best["spec_item"]["text"]
        self.assertIn("+25 to Strength", text)
        self.assertIn("+20 to Dexterity", text)
        kinds = [l["tier"] for l in best["template"]["lines"]]
        self.assertEqual(kinds.count("kept"), 2)
        # legal shape: at most 3 prefixes / 3 suffixes, one mod per group (the driver's own item check is the oracle)
        self.assertNotIn("rejected", cd.classify(best, self.c["baseline"], {"min_ele_res": 75})[0])

    def test_amulet_template_keeps_life_and_is_not_worse(self):
        tpl = templates(self.c, "Amulet")
        best = max(tpl, key=lambda t: t["eval"]["dps_pct"] + t["eval"]["ehp_pct"])
        self.assertIn("maximum Life", best["spec_item"]["text"])
        self.assertGreaterEqual(best["eval"]["ehp_pct"], -1.0)

    def test_tier_caps_follow_the_investment(self):
        hi = run_cand(self.spec, investment="high")["candidates"]
        lo = run_cand(self.spec, investment="league_starter")["candidates"]
        def costly_top(c):
            best = max(templates(c, "Weapon 1"), key=lambda t: t["eval"]["dps_pct"])
            return sum(1 for l in best["template"]["lines"] if l["tier"] == "T1*")
        self.assertLessEqual(costly_top(lo), 1)
        self.assertGreaterEqual(costly_top(hi), costly_top(lo))
        self.assertEqual(lo["rare_templates"]["top_tier_limit"], 1)

    def test_budget_is_separate_and_bounded(self):
        n = self.c["counts"]
        self.assertLessEqual(n["rare_evals"], 60)
        self.assertLessEqual(n["evaluated"], n["eval_limit"])
        z = run_cand(self.spec, rare_slots=[])["candidates"]
        self.assertFalse(z["rare_templates"]["slots"])
        self.assertEqual(z["counts"]["rare_evals"], 0)

    def test_report_and_variants(self):
        targets = {"min_ele_res": 75, "chaos_res": 0}
        ranked = cd.rank(self.c, targets, investment="medium")
        text = cd.format_report(self.c, ranked)
        text = text if isinstance(text, str) else "\n".join(text)
        self.assertIn("Rare templates:", text)
        self.assertIn("crafted rare template", text)
        self.assertIn("cost not verified", text)
        var = cd.variants(ranked, limit=4)
        self.assertTrue(any(v["items"][0].get("text", "").startswith("Rarity: RARE") for v in var))

    def test_accepted_template_passes_the_gear_audit(self):
        import audit as qa
        tpl = templates(self.c, "Weapon 1")
        best = max(tpl, key=lambda t: t["eval"]["dps_pct"])
        s = copy.deepcopy(self.spec)
        for it in s["items"]:
            if it["slot"] == "Weapon 1":
                it["text"] = best["spec_item"]["text"]
        r = fastpath.run_one({"mode": "build", "spec": s, "audit": True})
        self.assertTrue(r["valid"], r["errors"])
        prof = qa.resolve_profile(s.get("profile") or {}, {}, r.get("level") or 90)
        bad = [e for e in qa.evaluate(r, prof) if "Weapon 1" in str(e[2]) and "demanding" in str(e[2])]
        self.assertFalse(bad, bad)


if __name__ == "__main__":
    unittest.main()
