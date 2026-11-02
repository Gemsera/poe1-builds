"""Passive-tree point filler (tree.fill): preflight validation, driver behaviour, report.

Claims are checked against PoB itself: the filled build is a VALID build whose point count
is PoB's own, and whose DPS and life are at least those of the unfilled build. No expected
numbers are hard-coded.

Run: python3 tests/test_fill.py      (needs scripts/setup.sh; ~1 minute)
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

EXAMPLE = os.path.join(HERE, "..", "examples", "rf_chieftain.json")
READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)


def base_spec():
    with open(EXAMPLE) as f:
        s = pob.load_json_lenient(f.read())
    s["tree"].pop("fill", None)
    return s


class PreflightFill(unittest.TestCase):
    def test_objective_names(self):
        s = base_spec()
        for ok in ("balanced", "dps", "defence", {"objective": "dps", "reserve": 2, "max_cost": 2}, True):
            s["tree"]["fill"] = ok
            self.assertEqual([e for e in preflight.check(s)["errors"] if "fill" in e], [], msg=repr(ok))
        s["tree"]["fill"] = "damage"
        self.assertTrue(any("fill" in e for e in preflight.check(s)["errors"]))
        s["tree"]["fill"] = {"objective": "balanced", "reserve": "two"}
        self.assertTrue(any("reserve" in e for e in preflight.check(s)["errors"]))

    def test_unknown_fill_key_warns(self):
        s = base_spec()
        s["tree"]["fill"] = {"objective": "balanced", "resrve": 2}
        self.assertTrue(any("resrve" in w for w in preflight.check(s)["warnings"]))


@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class DriverFill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        plain = base_spec()
        filled = copy.deepcopy(plain)
        filled["tree"]["fill"] = "balanced"
        reserved = copy.deepcopy(plain)
        reserved["tree"]["fill"] = {"objective": "dps", "reserve": 3}
        cls.plain, cls.filled, cls.reserved = fastpath.run_many(
            [{"mode": "build", "spec": s} for s in (plain, filled, reserved)], workers=1)

    def test_fills_to_the_level_budget(self):
        r = self.filled
        self.assertTrue(r["valid"], r["errors"])
        tree = r["tree"]
        self.assertEqual(tree["points_used"], tree["points_available_at_level"])
        fill = r["fill"]
        self.assertEqual(fill["points_after"], tree["points_used"])
        self.assertEqual(fill["points_before"] + fill["points_added"], fill["points_after"])
        self.assertEqual(sum(a["cost"] for a in fill["added"]), fill["points_added"])
        self.assertGreater(len(fill["added"]), 0)

    def test_never_worse_than_unfilled(self):
        a, b = self.plain["summary"], self.filled["summary"]
        self.assertGreaterEqual(b["dps"], a["dps"])
        self.assertGreaterEqual(b["life"] + b["es"], a["life"] + a["es"])
        self.assertGreaterEqual(b["min_ele_res"], min(a["min_ele_res"], 75))

    def test_adds_only_normal_notable_and_mastery_nodes(self):
        for a in self.filled["fill"]["added"]:
            self.assertIn(a["type"], ("Normal", "Notable", "Mastery"), a)
        names = {n for n in self.filled["tree"]["keystones"]}
        self.assertEqual(names, set(self.plain["tree"]["keystones"]), "fill must not allocate keystones")

    def test_reserve_is_respected(self):
        r = self.reserved
        self.assertTrue(r["valid"], r["errors"])
        self.assertEqual(r["tree"]["points_available_at_level"] - r["tree"]["points_used"], 3)

    def test_no_fill_key_means_no_fill(self):
        self.assertNotIn("fill", self.plain)
        self.assertLess(self.plain["tree"]["points_used"], self.plain["tree"]["points_available_at_level"])

    def test_report_prints_fill(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            pob.print_report(self.filled)
        self.assertIn("Tree fill (objective balanced)", buf.getvalue())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class PinnedFill(unittest.TestCase):
    """build writes <name>.filled.json: the fill's tree pinned by node id, reproduced exactly and without fill cost."""

    @classmethod
    def setUpClass(cls):
        s = base_spec()
        s["tree"]["fill"] = "balanced"
        cls.filled = fastpath.run_many([{"mode": "build", "spec": s}], workers=1)[0]
        cls.pinned_spec = pob.pin_fill(s, cls.filled)
        item_only = pob.apply_variant(cls.pinned_spec, {"items": [{"slot": "Belt", "remove": True}]})
        tree_edit = pob.apply_variant(cls.pinned_spec, {"tree_remove": ["Profane Chemistry"]})
        cls.pinned, cls.item_only, cls.tree_edit = fastpath.run_many(
            [{"mode": "build", "spec": x} for x in (cls.pinned_spec, item_only, tree_edit)], workers=1)

    def test_pinned_reproduces_the_build_exactly(self):
        self.assertTrue(self.pinned["valid"], self.pinned["errors"])
        self.assertNotIn("fill", self.pinned, "a pinned tree must not be filled again")
        self.assertEqual(self.pinned["tree"]["allocated_ids"], self.filled["tree"]["allocated_ids"])
        self.assertEqual(self.pinned["summary"]["dps"], self.filled["summary"]["dps"])
        self.assertEqual(self.pinned["summary"]["life"], self.filled["summary"]["life"])

    def test_item_variant_keeps_the_pinned_tree(self):
        self.assertEqual(self.item_only["tree"]["allocated_ids"], self.filled["tree"]["allocated_ids"])

    def test_tree_variant_drops_the_pin_and_refills(self):
        self.assertIn("fill", self.tree_edit)
        self.assertNotIn("allocated", pob.apply_variant(self.pinned_spec, {"tree_remove": ["Profane Chemistry"]})["tree"])
        self.assertEqual(self.tree_edit["tree"]["points_used"], self.tree_edit["tree"]["points_available_at_level"])
        # the target is gone from the spec; the fill may still take the notable on merit, which is its job
        v = pob.apply_variant(self.pinned_spec, {"tree_remove": ["Profane Chemistry"]})
        self.assertNotIn("Profane Chemistry", [str(n) for n in v["tree"]["nodes"]])


@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class FillMasteriesAndDefence(unittest.TestCase):
    """The fill measures mastery effects on reachable mastery nodes, scores defence by pool and weakest max
    hit, and re-verifies a cached winner against the current tree before committing to it."""

    @classmethod
    def setUpClass(cls):
        s = base_spec()
        s["tree"]["fill"] = "balanced"
        cls.r = fastpath.run_many([{"mode": "build", "spec": s}], workers=1)[0]
        cls.pinned = pob.pin_fill(s, cls.r)
        cls.pinned_r = fastpath.run_many([{"mode": "build", "spec": cls.pinned}], workers=1)[0]

    def test_scores_carry_pool_and_hit_components(self):
        for a in self.r["fill"]["added"]:
            self.assertIn("dps_pct", a)
            self.assertIn("def_pct", a)

    def test_masteries_are_candidates_and_get_pinned(self):
        fill = self.r["fill"]
        self.assertIn("masteries", fill)
        for m in fill["masteries"]:
            self.assertTrue(m.get("mastery") and m.get("text"))
            self.assertIn({"mastery": m["mastery"], "effect": m["text"], "node": m["node_id"]}, self.pinned["tree"]["masteries"])
        chosen = [a for a in fill["added"] if a.get("mastery_effect")]
        self.assertEqual(len(chosen), len(fill["masteries"]))
        # a chosen mastery is one point and is reported with its effect text
        for a in chosen:
            self.assertEqual(a["cost"], 1)
            self.assertIn(":", a["name"])
        self.assertTrue(self.pinned_r["valid"], self.pinned_r["errors"])
        self.assertEqual(self.pinned_r["summary"]["dps"], self.r["summary"]["dps"])

    def test_reverification_never_commits_a_stale_zero(self):
        # every committed node was worth something on the tree it was added to
        for a in self.r["fill"]["added"]:
            self.assertGreater(abs(a["dps_pct"]) + abs(a["def_pct"]), 0.0, a)


ASSASSIN = os.path.join(HERE, "fixtures", "eh_melee_assassin.json")


@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class ConditionAwareFill(unittest.TestCase):
    """A node that is a sustaining source for a config condition that is off is measured with the condition on; when
    it wins, the fill switches the condition on, names the source, and the pinned spec carries the flag."""

    @classmethod
    def setUpClass(cls):
        with open(ASSASSIN) as f:
            cls.spec = pob.load_json_lenient(f.read())
        cls.spec.get("config", {}).pop("useFrenzyCharges", None)
        cls.r = fastpath.run_many([{"mode": "build", "spec": cls.spec, "audit": True}], workers=1)[0]

    def test_frenzy_source_switches_the_flag_on(self):
        self.assertTrue(self.r["valid"], self.r["errors"])
        en = self.r["fill"].get("enabled_config") or []
        self.assertTrue(any(e["var"] == "useFrenzyCharges" and "Frenzy Charge" in e["source"] for e in en), en)
        self.assertTrue(any(a.get("enables") == ["useFrenzyCharges"] for a in self.r["fill"]["added"]))
        self.assertTrue(any(c["var"] == "useFrenzyCharges" and c.get("value") for c in self.r["audit"]["config"]))

    def test_pinned_spec_carries_the_flag_and_reproduces(self):
        pinned = pob.pin_fill(self.spec, self.r)
        self.assertTrue(pinned["config"].get("useFrenzyCharges"))
        pr = fastpath.run_many([{"mode": "build", "spec": pinned}], workers=1)[0]
        self.assertTrue(pr["valid"], pr["errors"])
        self.assertEqual(pr["summary"]["dps"], self.r["summary"]["dps"])

    def test_flag_already_set_means_source_scores_nothing_extra(self):
        s = copy.deepcopy(self.spec)
        s["config"]["useFrenzyCharges"] = True
        r = fastpath.run_many([{"mode": "build", "spec": s}], workers=1)[0]
        self.assertEqual(r["fill"].get("enabled_config") or [], [])


SLAYER = os.path.join(HERE, "fixtures", "ms_zenith_slayer.json")


@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class NumericConditionFill(unittest.TestCase):
    """A node granting Rage on (melee) hit in a melee build is measured with multiplierRage at PoB's maximum; when it
    wins, the fill sets the number (not a boolean), names the source, and the pinned spec carries the value."""

    @classmethod
    def setUpClass(cls):
        with open(SLAYER) as f:
            cls.spec = pob.load_json_lenient(f.read())
        cls.spec.get("config", {}).pop("multiplierRage", None)
        cls.r = fastpath.run_many([{"mode": "build", "spec": cls.spec, "audit": True}], workers=1)[0]

    def test_rage_source_sets_the_multiplier(self):
        self.assertTrue(self.r["valid"], self.r["errors"])
        en = self.r["fill"].get("enabled_config") or []
        rage = [e for e in en if e["var"] == "multiplierRage"]
        self.assertEqual(len(rage), 1, en)
        self.assertGreaterEqual(rage[0]["value"], 30)
        self.assertIn("Rage on Hit", rage[0]["source"])
        self.assertTrue(any(a.get("enables") == ["multiplierRage"] for a in self.r["fill"]["added"]))

    def test_pinned_spec_carries_the_number_and_reproduces(self):
        pinned = pob.pin_fill(self.spec, self.r)
        self.assertGreaterEqual(pinned["config"].get("multiplierRage", 0), 30)
        pr = fastpath.run_many([{"mode": "build", "spec": pinned}], workers=1)[0]
        self.assertTrue(pr["valid"], pr["errors"])
        self.assertEqual(pr["summary"]["dps"], self.r["summary"]["dps"])

    def test_switching_a_charge_config_on_remeasures_max_charge_notables(self):
        # "+1 to Maximum Endurance Charges" scored nothing while useEnduranceCharges was off; once the fill switched it on
        # (Inexorable) the stale score is dropped and the notable is measured again, so it can be taken
        fill = self.r["fill"]
        self.assertTrue(any(e["var"] == "useEnduranceCharges" for e in fill.get("enabled_config") or []))
        self.assertGreater(fill.get("invalidated", 0), 0)
        names = [a["name"] for a in fill["added"]]
        self.assertTrue(any(n in ("Stamina", "Vigour", "Endurance") for n in names), names)

    def test_value_already_set_means_source_scores_nothing_extra(self):
        s = copy.deepcopy(self.spec)
        s["config"]["multiplierRage"] = 30
        r = fastpath.run_many([{"mode": "build", "spec": s}], workers=1)[0]
        self.assertEqual([e for e in (r["fill"].get("enabled_config") or []) if e["var"] == "multiplierRage"], [])


class VariantSetKeys(unittest.TestCase):
    def test_set_rejects_paths_inside_the_spec(self):
        s = base_spec()
        with self.assertRaises(ValueError):
            pob.apply_variant(s, {"set": {"skills[0].gems[1]": "Multistrike"}})
        with self.assertRaises(ValueError):
            pob.apply_variant(s, {"set": {"tree.allocated": [1, 2]}})
        self.assertEqual(pob.apply_variant(s, {"set": {"level": 95}})["level"], 95)


BERSERKER = os.path.join(HERE, "fixtures", "ls_berserker.json")


@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class AilmentConditionNeedsTheElement(unittest.TestCase):
    """A node with "chance to Freeze, Shock and Ignite" is a shock source for a lightning build, and no ignite source:
    the fill switches conditionEnemyShocked on, never conditionEnemyIgnited (the build deals no fire damage)."""

    @classmethod
    def setUpClass(cls):
        with open(BERSERKER) as f:
            cls.spec = pob.load_json_lenient(f.read())
        for k in ("conditionEnemyShocked", "conditionEnemyIgnited", "multiplierRage"):
            cls.spec.get("config", {}).pop(k, None)
        cls.r = fastpath.run_many([{"mode": "build", "spec": cls.spec}], workers=1)[0]

    def test_only_the_elements_the_main_skill_deals(self):
        self.assertTrue(self.r["valid"], self.r["errors"])
        on = {e["var"] for e in self.r["fill"].get("enabled_config") or []}
        self.assertNotIn("conditionEnemyIgnited", on)
        self.assertNotIn("conditionEnemyChilled", on)
        names = [a["name"] for a in self.r["fill"]["added"]]
        if "Ash, Frost and Storm" in names:
            self.assertIn("conditionEnemyShocked", on)


class AbyssalSocketSlots(unittest.TestCase):
    def test_abyss_jewel_slot_names_are_equipment_slots(self):
        self.assertEqual(preflight.canon_slot("Belt Abyssal Socket 1"), "Belt Abyssal Socket 1")
        self.assertEqual(preflight.canon_slot("body Abyssal Socket 2"), "Body Armour Abyssal Socket 2")
        self.assertIsNone(preflight.canon_slot("Nowhere Abyssal Socket 1"))
        s = base_spec()
        s["items"].append({"slot": "Belt Abyssal Socket 1", "text": "Rarity: RARE\nHorror Gem\nMurderous Eye Jewel\nItem Level: 84\n+38 to maximum Life"})
        self.assertFalse(any("Abyssal" in w for w in preflight.check(s)["warnings"]))
