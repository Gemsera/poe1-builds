"""Regression tests for the quality audit: sustained floor, layer accounting, recovery,
severity / stop rules, and the effect on validity.

Unit tests feed hand-written defence values to audit.evaluate (they test the accounting rules,
not PoB). Integration tests run real builds through PoB and only compare PoB numbers with other
PoB numbers from the same install; no expected PoB values are hard-coded.

Run: python3 tests/test_audit.py      (integration part needs scripts/setup.sh; ~1-2 minutes)
"""
import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import audit  # noqa: E402
import fastpath  # noqa: E402
import pob  # noqa: E402

EX = os.path.join(HERE, "..", "examples")
FIX = os.path.join(HERE, "fixtures")
READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)


def load(path):
    with open(path) as f:
        return pob.load_json_lenient(f.read())


def profile(content="mapping", investment="medium", level=90, mode="softcore"):
    return audit.resolve_profile({"content": content, "investment": investment, "mode": mode}, {}, level)


def resp_with(defence, floor=None, baseline=None, **audit_extra):
    base = baseline or {"dps": 1000.0, "ehp": 50000.0, "life": 5000}
    a = {"baseline": base, "defence": defence, "config": [], "flasks": [], "uniques": [], "skill_groups": [],
         "enemy_is_boss": "Pinnacle"}
    if floor is not None:
        a["floor_no_flasks_no_conditional_config"] = floor
    a.update(audit_extra)
    return {"valid": True, "errors": [], "audit": a, "items": [], "sustain": []}


LIFE = {"Life": 5000, "EnergyShield": 0, "FireResist": 75, "ColdResist": 75, "LightningResist": 75, "ChaosResist": 20,
        "PhysicalMaximumHitTaken": 9000, "FireMaximumHitTaken": 20000, "ColdMaximumHitTaken": 20000,
        "LightningMaximumHitTaken": 20000, "ChaosMaximumHitTaken": 8000, "LifeRegenRecovery": 150, "NetLifeRegen": 150}


def find(ev, area, start):
    return [e for e in ev if e[0] == area and e[2].startswith(start)]


class FloorReporting(unittest.TestCase):
    def test_absolute_and_percent(self):
        r = resp_with(dict(LIFE), floor={"dps": 700.0, "dps_pct": -30.0, "ehp": 40000, "ehp_pct": -20.0})
        line = find(audit.evaluate(r, profile()), "config", "Configured")[0][2]
        self.assertIn("Configured: DPS 1000, EHP 50.0k", line)
        self.assertIn("DPS 700 (-30.0%)", line)
        self.assertIn("EHP 40.0k (-20.0%)", line)

    def test_no_floor_means_equal(self):
        line = find(audit.evaluate(resp_with(dict(LIFE)), profile()), "config", "Configured")[0][2]
        self.assertIn("sustained floor is the same", line)


class LayerAccounting(unittest.TestCase):
    def layers(self, extra, content="mapping"):
        ev = audit.evaluate(resp_with(dict(LIFE, **extra)), profile(content))
        line = find(ev, "defence", "Defensive layers")[0][2]
        return int(line.split(": ")[1].split(" ")[0]), line

    def test_small_guard_not_counted(self):
        n, line = self.layers({"sharedGuardAbsorb": 86})        # 1.7% of 5000 life
        self.assertEqual(n, 0)
        self.assertIn("too small to count", line)

    def test_large_guard_is_temporary_not_a_layer(self):
        # a guard is temporary protection: listed once, never counted as a persistent layer
        n, line = self.layers({"sharedGuardAbsorb": 1500})
        self.assertEqual(n, 0)
        self.assertEqual(line.count("guard"), 1)
        self.assertIn("Temporary (not counted as a layer): guard", line)

    def test_block_counts_once(self):
        n, _ = self.layers({"EffectiveBlockChance": 50, "EffectiveSpellBlockChance": 40})
        self.assertEqual(n, 1)

    def test_resists_and_recoup_are_not_layers(self):
        n, line = self.layers({"FireResistOver75": 5, "LifeRecoup": 30})
        self.assertEqual(n, 0)
        self.assertIn("max res +5", line)
        rec = find(audit.evaluate(resp_with(dict(LIFE, LifeRecoup=30)), profile()), "recovery", "Life recovery")[0]
        self.assertEqual(rec[1], "PASS")                       # recoup still counts as recovery

    def test_distinct_mechanisms(self):
        n, line = self.layers({"PhysicalDamageReduction": 50, "MeleeEvadeChance": 60, "ProjectileEvadeChance": 70,
                               "EffectiveSpellSuppressionChance": 100, "sharedMindOverMatter": 40})
        self.assertEqual(n, 4)                                  # armour, evade (once), suppression, MoM
        self.assertIn("evade 70%", line)


class RecoveryAndDefence(unittest.TestCase):
    def test_net_regen_used(self):
        d = dict(LIFE, LifeRegenRecovery=300, NetLifeRegen=50)  # 6% gross, 1% net
        rec = find(audit.evaluate(resp_with(d), profile()), "recovery", "Life recovery")[0]
        self.assertEqual(rec[1], "WARN")
        self.assertIn("net regen 1.0%/s (gross 6.0%", rec[2])

    def test_flask_only_recovery_flagged(self):
        d = dict(LIFE, LifeLeechGainRate=600, NetLifeRegen=50)  # leech 12%/s with the flask, regen only 1%
        r = resp_with(d, floor={"dps": 900.0, "dps_pct": -10, "ehp": 45000, "ehp_pct": -10, "life_leech_rate": 0,
                                "net_life_regen": 150})
        rec = find(audit.evaluate(r, profile()), "recovery", "Life recovery")[0]
        self.assertEqual(rec[3], "material")
        self.assertIn("only met while a flask is up", rec[2])

    def test_weakest_max_hit_and_critical(self):
        ev = audit.evaluate(resp_with(dict(LIFE, PhysicalMaximumHitTaken=2500)), profile("mapping"))
        hit = find(ev, "defence", "Max hit")[0]
        self.assertIn("Weakest relative to target: Physical", hit[2])
        self.assertEqual(hit[3], "critical")                   # 2.5k < 50% of 6k
        ev = audit.evaluate(resp_with(dict(LIFE, PhysicalMaximumHitTaken=5000)), profile("mapping"))
        self.assertEqual(find(ev, "defence", "Max hit")[0][3], "material")

    def test_uncapped_res_is_critical_for_endgame(self):
        ev = audit.evaluate(resp_with(dict(LIFE, ColdResist=60)), profile("mapping"))
        self.assertEqual(find(ev, "defence", "Lowest elemental")[0][3], "critical")
        ev = audit.evaluate(resp_with(dict(LIFE, ColdResist=60)), profile("campaign", level=40))
        self.assertEqual(find(ev, "defence", "Lowest elemental")[0][3], "material")

    def test_chaos_immunity(self):
        ev = audit.evaluate(resp_with(dict(LIFE, ChaosResist=-60), chaos_immune=True), profile())
        self.assertTrue(find(ev, "defence", "Immune to chaos"))
        self.assertFalse(find(ev, "defence", "Chaos resistance"))


class StopRules(unittest.TestCase):
    def test_advisory_only_is_ready(self):
        r = resp_with(dict(LIFE, PhysicalDamageReduction=40))  # one layer, so nothing material is left
        r["audit"]["config"] = [{"var": "useFrenzyCharges", "value": True, "category": "charges",
                                 "without": {"dps_pct": -20, "ehp_pct": 0}}]
        ev = audit.evaluate(r, profile())
        self.assertTrue(any(e[3] == "advisory" for e in ev))
        self.assertTrue(audit.summary(ev)["status"].startswith("READY"))

    def test_critical_blocks_stopping(self):
        ev = audit.evaluate(resp_with(dict(LIFE, FireResist=40)), profile())
        self.assertTrue(audit.summary(ev)["status"].startswith("NOT READY"))

    def test_pob_errors_count_as_critical(self):
        s = audit.summary(audit.evaluate(resp_with(dict(LIFE)), profile()), errors=["x"])
        self.assertEqual(s["critical"], 1)
        self.assertTrue(s["status"].startswith("NOT READY"))

    def test_compare_never_takes_dps_over_a_broken_requirement(self):
        base = {"valid": True, "dps": 100.0, "ehp": 100.0, "min_max_hit": 9000, "min_ele_res": 75, "chaos_res": 10}
        req = {"min_ele_res": 75, "chaos_res": 0, "min_max_hit": 8000}
        self.assertTrue(pob._verdict(dict(base, dps=300, min_ele_res=70), base, req, 1, 1).startswith("rejected"))
        broken = dict(base, min_ele_res=60)                    # a fix for a broken original is still accepted
        v = pob._verdict(dict(broken, min_ele_res=75, dps=90), broken, req, 1, 1)
        self.assertTrue(v.startswith("fixes requirement"), v)


@unittest.skipUnless(READY, "PoB not set up")
class RealBuilds(unittest.TestCase):
    """The two examples (life: armour/evasion, life: armour/regen) and an ES / CI fixture."""

    SPECS = [os.path.join(EX, "burning_arrow_vigour_deadeye.json"), os.path.join(EX, "rf_chieftain.json"),
             os.path.join(FIX, "ci_es_occultist.json")]

    @classmethod
    def setUpClass(cls):
        cls.specs = [load(p) for p in cls.SPECS]
        cls.resps = fastpath.run_many([{"mode": "build", "spec": s, "audit": True} for s in cls.specs], workers=2)

    def test_valid_main_skill_and_audit(self):
        for p, r in zip(self.SPECS, self.resps):
            self.assertTrue(r["valid"], (p, r.get("errors")))
            self.assertTrue(r.get("main_skill"))
            self.assertFalse(r["summary"].get("main_skill_disabled"))
            self.assertIn("audit", r)

    def test_floor_matches_an_independent_calculation(self):
        """Floor = flasks off + conditional config reset. Rebuild that state by removing the flask items and
        resetting the same config options, and compare PoB's numbers."""
        variants, idx = [], []
        for i, (s, r) in enumerate(zip(self.specs, self.resps)):
            floor = r["audit"].get("floor_no_flasks_no_conditional_config")
            if not floor:
                continue
            cond = {c["var"]: None for c in r["audit"]["config"] if c.get("without")}
            # the examples use tree.fill: pin the filled tree so the rebuilt state has the same passives
            pinned = pob.pin_fill(s, r) or s
            v = pob.apply_variant(pinned, {"items": [{"slot": "Flask %d" % k, "remove": True} for k in range(1, 6)],
                                           "config": cond})
            v["infer_config"] = False   # the floor is "conditions off": don't let the build re-derive them from their sources
            variants.append(v)
            idx.append(i)
        self.assertTrue(idx)
        for i, v in zip(idx, fastpath.run_many([{"mode": "build", "spec": v} for v in variants], workers=2)):
            floor = self.resps[i]["audit"]["floor_no_flasks_no_conditional_config"]
            self.assertAlmostEqual(floor["dps"], v["summary"]["dps"], delta=max(1.0, 1e-6 * v["summary"]["dps"]))
            self.assertAlmostEqual(floor["ehp"], v["summary"]["ehp"], delta=1.0)
            prof = audit.resolve_profile(self.specs[i].get("profile"), {}, self.resps[i].get("level"))
            line = find(audit.evaluate(self.resps[i], prof), "config", "Configured")[0][2]
            self.assertIn(audit._fmt(floor["dps"]), line)

    def test_layer_lines_are_consistent_with_pob_values(self):
        for s, r in zip(self.specs, self.resps):
            prof = audit.resolve_profile(s.get("profile"), {}, r.get("level"))
            d = r["audit"]["defence"]
            line = find(audit.evaluate(r, prof), "defence", "Defensive layers")[0][2]
            pool = d["EnergyShield"] if d.get("EnergyShield", 0) > d.get("Life", 0) * 2 else d.get("Life", 0)
            g = d.get("sharedGuardAbsorb") or 0
            if g and g / pool < 0.05:
                self.assertIn("too small to count", line)
                self.assertNotIn("Hit buffers: guard", line)
            self.assertNotIn("Mitigation: max res", line)
            self.assertNotIn("recoup", line)
            listed = sum(len([x for x in part.split(", ") if x and x != "none"])
                         for part in [line.split("Mitigation: ")[1].split(".")[0], line.split("Avoidance: ")[1].split(".")[0],
                                      line.split("Hit buffers: ")[1].split(".")[0]])
            self.assertEqual(int(line.split(": ")[1].split(" ")[0]), listed)

    def test_sustain_warning_preserved(self):
        es = self.resps[2]
        self.assertTrue(es.get("sustain"))
        prof = audit.resolve_profile(self.specs[2].get("profile"), {}, es.get("level"))
        items = [e for e in audit.evaluate(es, prof) if e[0] == "sustain"]
        if es["sustain"][0].get("seconds_until_empty"):
            self.assertEqual(items[0][3], "material")
            self.assertIn("Main skill costs", " ".join(es["warnings"]))  # the driver warning is still there too

    def test_unsustainable_skill_still_invalidates(self):
        s = json.loads(json.dumps(self.specs[2]))
        s["skills"][1]["gems"] = [g for g in s["skills"][1]["gems"] if g != "Clarity"]
        s["skills"][0]["gems"] += ["Arcane Surge", "Swift Affliction"]
        r = fastpath.run_one({"mode": "build", "spec": s, "audit": True})
        msgs = " ".join(r["errors"] + r["warnings"])
        self.assertIn("Main skill costs", msgs)
        if any("unsustainable" in e for e in r["errors"]):
            self.assertFalse(r["valid"])
            self.assertIn("audit_skipped", r)

    def test_material_items_do_not_change_exit_code(self):
        td = tempfile.mkdtemp()
        try:
            a = argparse.Namespace(spec=self.SPECS[2], out=td, json=True, show_code=False, audit=True, investment=None,
                                   content=None, game_mode=None, share=False, timeout=5, verify=True, preflight=True,
                                   repair=False, fail_fast=False, audit_detail=False)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = pob.cmd_build(a)
            out = json.loads(buf.getvalue())
            self.assertGreater(out["quality_summary"]["material"], 0)
            self.assertEqual(rc, 0)
            self.assertTrue(out["valid"])
        finally:
            shutil.rmtree(td)


if __name__ == "__main__":
    unittest.main(verbosity=2)
