"""Gear-realism tiers: "top tier" is the game's best tier for the base (PoB mod data), not the best
tier the item's own level allows. Flasks are excluded from realism scoring. Real PoB item checks;
the expected tier counts come from PoB's mod data via the driver, not from hard-coded tables.

Run: python3 tests/test_gear_tiers.py      (needs scripts/setup.sh; ~1 minute)
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import audit as qa  # noqa: E402
import fastpath  # noqa: E402
import pob  # noqa: E402

EX = os.path.join(HERE, "..", "examples")
READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)

# best rolls a level-30 ring can have, all far below the game's best tiers
LOW_RING = ("Rarity: RARE\nGlyph Band\nRuby Ring\nItem Level: 30\n+49 to maximum Life\n+23% to Cold Resistance\n"
            "+23% to Lightning Resistance\n+20 to Intelligence")
# three damage prefixes at the game's best tier (item level 76-81 tiers). Resistance and attribute top tiers are
# not counted as demanding any more: essences and the bench put T1 resistances within any budget
DEMANDING_RING = ("Rarity: RARE\nGlyph Band\nRuby Ring\nItem Level: 86\nAdds 20 to 38 Cold Damage to Attacks\n"
                  "Adds 22 to 42 Fire Damage to Attacks\n40% increased Elemental Damage with Attack Skills\n+48% to Cold Resistance\n+20 to Intelligence")


def load(name):
    with open(os.path.join(EX, name + ".json")) as f:
        return pob.load_json_lenient(f.read())


def gear(r, investment):
    p = qa.resolve_profile({"content": "mapping", "investment": investment}, {}, r["level"])
    return [e for e in qa.evaluate(r, p) if e[0] == "gear"]


def item(r, slot):
    return next(i for i in r["items"] if i["slot"] == slot)


@unittest.skipUnless(READY, "PoB not set up")
class GearTiers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rf = load("rf_chieftain")
        specs = {"rf": rf, "deadeye": load("burning_arrow_vigour_deadeye")}
        for k, text in (("low", LOW_RING), ("demanding", DEMANDING_RING)):
            specs[k] = pob.apply_variant(rf, {"items": [{"slot": "Ring 2", "text": text}]})
        keys = list(specs)
        cls.r = dict(zip(keys, fastpath.run_many([{"mode": "build", "spec": specs[k], "audit": True} for k in keys],
                                                workers=2)))

    def test_low_level_rare_is_not_top_tier(self):
        it = item(self.r["low"], "Ring 2")
        self.assertEqual(it["item_level"], 30)
        self.assertGreaterEqual(it["top_tier_at_ilvl"], 2)      # the old, item-relative measure counted these
        self.assertEqual(it["top_tier_affixes"], 0)
        self.assertFalse([e for e in gear(self.r["low"], "league_starter") if "Ring 2" in e[2]])

    def test_ordinary_endgame_rares_are_not_flagged(self):
        r = self.r["rf"]
        for slot in ("Boots", "Gloves", "Ring 1", "Helmet"):
            self.assertLessEqual(item(r, slot)["top_tier_affixes"], 1, slot)
        self.assertFalse([e for e in gear(r, "medium") if e[1] == "WARN"])

    def test_flasks_are_not_scored(self):
        r = self.r["deadeye"]
        flasks = [i for i in r["items"] if i["slot"].startswith("Flask")]
        self.assertTrue(any(i["top_tier_affixes"] >= 2 for i in flasks))   # data still reported
        self.assertFalse([e for e in gear(r, "league_starter") if "Flask" in e[2]])

    def test_demanding_combination_still_warned(self):
        r = self.r["demanding"]
        it = item(r, "Ring 2")
        self.assertEqual(it["top_tier_affixes"], 3, it.get("top_tier_mods"))
        self.assertTrue(all(m["tiers"] > 1 for m in it["top_tier_mods"]))
        warn = [e for e in gear(r, "budget") if "Ring 2" in e[2]]
        self.assertEqual(len(warn), 1)
        self.assertEqual((warn[0][1], warn[0][3]), ("WARN", "material"))
        self.assertIn("item level 86", warn[0][2])
        # three best-tier resistances alone are not "demanding" (cheap top tiers)
        tri = pob.apply_variant(load("rf_chieftain"), {"items": [{"slot": "Ring 2", "text": "Rarity: RARE\nGlyph Band\nRuby Ring\nItem Level: 86\n+79 to maximum Life\n+48% to Cold Resistance\n+48% to Lightning Resistance\n+48% to Fire Resistance"}]})
        rt = fastpath.run_one({"mode": "build", "spec": tri, "audit": True})
        self.assertEqual(item(rt, "Ring 2")["top_tier_affixes"], 0)
        self.assertIn("not a price", warn[0][2])
        self.assertFalse([e for e in gear(r, "high") if "Ring 2" in e[2]])    # investment changes the reading


class CharacterLevelNote(unittest.TestCase):
    def resp(self, level):
        a = {"baseline": {"dps": 1, "ehp": 1, "life": 1}, "defence": {}, "config": [], "flasks": [], "uniques": [],
             "skill_groups": [], "enemy_is_boss": "Pinnacle"}
        it = {"slot": "Ring 1", "name": "x", "rarity": "RARE", "base_type": "Ring", "item_level": 84,
              "top_tier_affixes": 3, "explicit_affixes": 4,
              "top_tier_mods": [{"text": "+48% to Cold Resistance", "item_level": 84, "tiers": 8}] * 3}
        return {"valid": True, "errors": [], "audit": a, "items": [it], "sustain": [], "level": level}

    def test_tiers_above_character_level_are_noted(self):
        for level, noted in ((60, True), (90, False)):
            r = self.resp(level)
            msg = [e for e in qa.evaluate(r, qa.resolve_profile({"investment": "budget"}, {}, level)) if e[0] == "gear"][0][2]
            self.assertEqual("above character level %d" % level in msg, noted, msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
