"""Equipment candidates: discovery from PoB's data, capped measurement, ranking, slot audit, gear basis.

Three stages are kept apart throughout:
  discovered   a line of the item applies to the build (static, PoB's parsed modifiers)
  evaluated    PoB measured it as a drop-in against one baseline (status "evaluated", counted)
  accepted     only `compare` and the user put an item in the build; `candidates` never changes a spec

Reference builds: the ordinary Elemental Hit Warden (dual claws, a flask that measures nothing), the
Elemental Hit of the Spectrum Warden (bow) and the RF Chieftain example (damage over time, a negative
control). Named items appear only as expectations about those builds; the code has no item, skill or
ascendancy rules. Expected numbers come from independent full PoB builds, never from constants.

Run: python3 tests/test_candidates.py      (needs scripts/setup.sh; ~3 minutes)
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)
import audit as qa  # noqa: E402
import candidates as cd  # noqa: E402
import fastpath  # noqa: E402
import pob  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
EX = os.path.join(HERE, "..", "examples")
READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)
TRACE = ["The Taming", "Prismatic Tincture", "Sap of the Seasons"]


def load(path):
    with open(path) as f:
        return pob.load_json_lenient(f.read())


def melee():
    return load(os.path.join(FIX, "eh_melee_warden.json"))


def bow():
    return load(os.path.join(FIX, "eh_spectrum_warden.json"))


def run_cand(spec, **opts):
    opts.setdefault("max_evals", 60)
    return fastpath.run_one({"mode": "build", "spec": spec, "candidates": opts}, 900)


def cli(*args):
    p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "pob.py")] + list(args), capture_output=True, text=True,
                       timeout=900)
    return p.returncode, p.stdout, p.stderr


def all_cands(c):
    for g in c["groups"]:
        for x in g["candidates"]:
            yield g, x


def full_dps(spec):
    r = fastpath.run_one({"mode": "build", "spec": spec}, 900)
    return r["summary"]["dps"], r["summary"]["ehp"]


# ---------------------------------------------------------------------------------------------
# Ranking without PoB: general rules, no item names
# ---------------------------------------------------------------------------------------------
def cand(name, dps=None, ehp=0.0, kind="unique", **ev):
    c = {"name": name, "kind": kind, "lines": [], "conditions": [], "limitations": [], "spec_item": {"slot": "X", "unique": name}}
    if dps is not None:
        c["eval"] = dict({"dps_pct": dps, "ehp_pct": ehp, "min_max_hit_pct": 0.0, "min_ele_res": 75, "chaos_res": 0,
                          "min_max_hit": 9000, "life": 0}, **ev)
        c["status"] = "evaluated"
    else:
        c["status"] = "discovered"
    return c


BASE = {"dps": 1e5, "ehp": 3e4, "min_ele_res": 75, "chaos_res": 0, "min_max_hit": 9000}
TARGETS = {"min_ele_res": 75, "chaos_res": -20, "min_max_hit": 6000}


class Ranking(unittest.TestCase):
    def ranked(self, cands, investment="medium"):
        return cd.rank({"baseline": BASE, "groups": [{"kind": "S", "target": "S", "candidates": cands}]}, TARGETS,
                       investment=investment)["groups"][0]

    def test_breaking_a_profile_target_rejects_whatever_the_dps(self):
        g = self.ranked([cand("A", 80.0, min_ele_res=60), cand("B", 3.0)])
        self.assertEqual([r["name"] for r in g["shortlist"]], ["B"])
        self.assertEqual(g["rejected"][0]["name"], "A")
        self.assertIn("breaks the profile target", g["rejected"][0]["notes"][0])

    def test_main_skill_losing_its_damage_is_rejected(self):
        g = self.ranked([cand("A", -100.0, main_skill_no_damage=True)])
        self.assertEqual(g["rejected"][0]["verdict"], "rejected")
        self.assertEqual(g["shortlist"], [])

    def test_verdicts_and_order(self):
        g = self.ranked([cand("small", 2.0), cand("big", 30.0), cand("trade", 20.0, -10.0), cand("flat", 0.2, 0.1),
                         cand("bad", -5.0, -2.0)])
        self.assertEqual([r["name"] for r in g["shortlist"]], ["big", "small", "trade"])
        verdict = {r["name"]: r["verdict"] for r in g["shortlist"] + g["others"]}
        self.assertEqual(verdict["trade"], "trade-off")
        self.assertEqual(verdict["flat"], "no measured gain")
        self.assertEqual(verdict["bad"], "worse")

    def test_unmeasured_candidates_are_never_shortlisted(self):
        g = self.ranked([cand("seen only"), cand("measured", 1.5)])
        self.assertEqual([r["name"] for r in g["shortlist"]], ["measured"])
        self.assertEqual(g["others"][0]["verdict"], "not evaluated")

    def test_loss_on_the_weakest_metric_counts_double(self):
        base = dict(BASE, min_max_hit=6100)                     # max hit is the thinnest margin
        r = cd.rank({"baseline": base, "groups": [{"kind": "S", "target": "S", "candidates": [
            cand("A", 10.0, min_max_hit_pct=-4.0, min_max_hit=6000), cand("B", 8.0)]}]}, TARGETS)
        self.assertEqual(r["weakest_metric"], "min_max_hit")
        g = r["groups"][0]
        self.assertEqual(g["shortlist"][0]["name"], "B")
        self.assertTrue(any("weakest metric" in n for n in g["shortlist"][1]["notes"]))

    def test_near_tie_prefers_craftable_under_a_moderate_budget_only(self):
        pair = [cand("U", 10.0), cand("C", 9.0, kind="magic")]
        self.assertEqual(self.ranked(copy.deepcopy(pair), "medium")["shortlist"][0]["name"], "C")
        self.assertEqual(self.ranked(copy.deepcopy(pair), "high")["shortlist"][0]["name"], "U")
        for r in self.ranked(copy.deepcopy(pair))["shortlist"]:
            self.assertIn("not verified", r["cost"])

    def test_variants_only_for_worthwhile_candidates(self):
        groups = [{"kind": k, "target": k, "candidates": c} for k, c in
                  (("A", [cand("good", 12.0)]), ("B", [cand("loses", 5.0, -9.0)]),
                   ("C", [dict(cand("conditional", 2.0, -4.0), conditional={"item_effect": {"dps_pct": 15.0}})]))]
        v = cd.variants(cd.rank({"baseline": BASE, "groups": groups}, TARGETS))
        self.assertEqual(sorted(x["name"] for x in v), ["A: good", "C: conditional"])

    def test_signature_changes(self):
        old = {"main_skill": "S", "weapon_class": "Claw (dual wield)", "ascendancy": "A", "archetype": "attack, hit, elemental"}
        self.assertEqual(cd.signature_changes(old, dict(old)), [])
        ch = cd.signature_changes(old, dict(old, weapon_class="Bow (quiver)"))
        self.assertEqual(len(ch), 1)
        self.assertIn("weapon class", ch[0])


# ---------------------------------------------------------------------------------------------
# PoB: discovery, measurement, conditions, restrictions
# ---------------------------------------------------------------------------------------------
PROBES = {
    "Probe Claw Band": "40% increased Elemental Damage with Claws",
    "Probe Bow Band": "40% increased Elemental Damage with Bows",
    "Probe Spell Band": "40% increased Spell Damage",
    "Probe Shock Band": "60% increased Damage with Hits against Shocked Enemies",
    "Probe Blind Band": "60% increased Damage with Hits against Blinded Enemies",
}


def probe_items():
    return ["Rarity: RARE\n%s\nIron Ring\nItem Level: 84\n%s" % (n, line) for n, line in PROBES.items()]


@unittest.skipUnless(READY, "PoB not set up")
class MeleeWarden(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = melee()
        cls.r = run_cand(cls.spec, trace=TRACE)
        cls.c = cls.r["candidates"]
        cls.trace = {t["name"]: t for t in cls.c.get("trace") or []}
        # supplied probe items in a separate search, so they don't change the reference search
        cls.p = run_cand(cls.spec, trace=list(PROBES), extra_items=probe_items())["candidates"]
        cls.ptrace = {t["name"]: t for t in cls.p.get("trace") or []}

    def group(self, kind):
        return next(g for g in self.c["groups"] if g["kind"] == kind)

    def test_profile(self):
        p = self.c["profile"]
        self.assertEqual(p["main_skill"], "Elemental Hit")
        self.assertEqual(p["weapon_class"], "Claw")
        self.assertEqual(sorted(p["damage_types"]), ["Cold", "Fire", "Lightning"])   # every part counted
        self.assertEqual(p["signature"]["archetype"], "attack, hit, elemental")

    def test_target_is_the_slot_that_measures_nothing(self):
        g = self.group("Flask")
        slot = next(s for s in self.c["slots"] if s["slot"] == g["target"])
        self.assertTrue(slot["no_measured_benefit"])
        self.assertFalse(slot.get("other_effects"))
        # flasks whose removal changes recovery or movement are not picked as the slot to replace
        for s in self.c["slots"]:
            if s["slot"].startswith("Flask") and s.get("other_effects"):
                self.assertNotEqual(s["slot"], g["target"])

    def test_tincture_discovered_and_measured_like_a_full_build(self):
        t = self.trace["Prismatic Tincture"]
        self.assertTrue(t["discovered"])
        g = self.group("Flask")
        x = next(x for x in g["candidates"] if x["kind"] == "base" and x["name"] == "Prismatic Tincture")
        self.assertEqual(x["status"], "evaluated")
        self.assertTrue(any("always on" in l for l in x["limitations"]))
        base = full_dps(self.spec)
        alt = full_dps(pob.apply_variant(self.spec, {"items": [x["spec_item"]]}))
        self.assertAlmostEqual(x["eval"]["dps_pct"], (alt[0] / base[0] - 1) * 100, delta=0.3)
        self.assertGreater(x["eval"]["dps_pct"], 0)

    def test_the_taming_value_depends_on_enemy_ailments(self):
        x = next(x for g, x in all_cands(self.c) if x["name"] == "The Taming")
        self.assertEqual(x["status"], "evaluated")
        conds = {d["condition"]: d for d in x["conditions"]}
        self.assertEqual(conds["Shocked"]["evidence"], "source found")
        cdn = x["conditional"]
        self.assertTrue(cdn and "conditionEnemyShocked" in cdn["config"])
        # the config change and the item are separate, and the item is worth more with the ailments set
        self.assertGreater(cdn["config_effect"]["dps_pct"], 0)
        self.assertGreater(cdn["item_effect"]["dps_pct"], x["eval"]["dps_pct"])
        # independent check: two full builds under the scenario's configuration
        scen = copy.deepcopy(self.spec)
        scen["config"].update({v: True for v in cdn["config"]})
        b, _ = full_dps(scen)
        a, _ = full_dps(pob.apply_variant(scen, {"items": [x["spec_item"]]}))
        self.assertAlmostEqual(cdn["item_effect"]["dps_pct"], (a / b - 1) * 100, delta=0.5)

    def test_modifier_compatibility(self):
        t = self.ptrace
        self.assertTrue(t["Probe Claw Band"]["discovered"])
        self.assertFalse(t["Probe Bow Band"]["discovered"])
        self.assertIn("needs a bow", t["Probe Bow Band"]["lines"][0]["status"])
        self.assertFalse(t["Probe Spell Band"]["discovered"])
        self.assertTrue(t["Probe Shock Band"]["discovered"])              # a condition the build has a source for
        self.assertIn("the build has a source", t["Probe Shock Band"]["lines"][0]["status"])
        self.assertFalse(t["Probe Blind Band"]["discovered"])             # no source: weighted below discovery
        self.assertIn("no source", t["Probe Blind Band"]["lines"][0]["status"])
        ring = next(g for g in self.p["groups"] if g["kind"] == "Ring")
        claw = next(x for x in ring["candidates"] if x["name"] == "Probe Claw Band")
        self.assertEqual(claw["status"], "evaluated")                   # supplied items are measured first

    def test_enabling_passives_from_the_tree_data(self):
        types = {e.get("item_type") for e in self.c["enablers"]}
        self.assertIn("Tincture", types)
        asc = [e for e in self.c["enablers"] if e.get("ascendancy")]
        self.assertTrue(asc)
        for e in asc:
            self.assertEqual(e["ascendancy"], "Warden")

    def test_bounded(self):
        n = self.c["counts"]
        self.assertLessEqual(n["evaluated"], n["eval_limit"])
        self.assertEqual(n["evaluated"], n["drop_in_evals"] + n["crafted_evals"] + n["conditional_evals"])
        self.assertLess(n["seconds"], 30)
        self.assertLess(n["static_kept"], n["discovered"])
        evaluated = sum(1 for _, x in all_cands(self.c) if x["status"] == "evaluated")
        self.assertLessEqual(evaluated, n["drop_in_evals"] + n["crafted_evals"])

    def test_smaller_limit_and_zero(self):
        r = run_cand(self.spec, max_evals=12)["candidates"]
        self.assertLessEqual(r["counts"]["evaluated"], 12)
        z = run_cand(self.spec, max_evals=0)["candidates"]
        self.assertEqual(z["counts"]["evaluated"], 0)
        self.assertTrue(all(x["status"] == "discovered" for _, x in all_cands(z)))


@unittest.skipUnless(READY, "PoB not set up")
class BowWarden(unittest.TestCase):
    """The same ascendancy with a bow: melee-only lines are not drop-ins, they need a different setup."""

    @classmethod
    def setUpClass(cls):
        cls.c = run_cand(bow(), trace=["Prismatic Tincture"])["candidates"]

    def test_melee_lines_are_not_drop_ins(self):
        for g, x in all_cands(self.c):
            if x.get("type") == "Tincture":
                self.assertNotIn(x["status"], ("evaluated",), x["name"])
        t = next(t for t in self.c["trace"] if t["name"] == "Prismatic Tincture")
        self.assertFalse(t["discovered"])
        self.assertIn("melee weapon", t["lines"][0]["status"])

    def test_listed_as_different_configuration_with_enablers(self):
        dc = [d for d in self.c["different_configuration"] if d.get("type") == "Tincture"]
        self.assertTrue(dc)
        self.assertTrue(all("melee weapon" in d["reason"] for d in dc))
        self.assertTrue(any(e.get("item_type") == "Tincture" for e in self.c["enablers"]))


@unittest.skipUnless(READY, "PoB not set up")
class DamageOverTimeControl(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = run_cand(load(os.path.join(EX, "rf_chieftain.json")), trace=TRACE)["candidates"]

    def test_negative_control(self):
        self.assertEqual(self.c["profile"]["hit_or_dot"], "damage over time")
        ranked = cd.rank(self.c, TARGETS)
        short = [x["name"] for g in ranked["groups"] for x in g["shortlist"]]
        self.assertNotIn("The Taming", short)
        self.assertFalse([x for x in short if "Tincture" in x])
        t = {t["name"]: t for t in self.c["trace"]}
        self.assertGreater(t["The Taming"]["rank"], 4)                    # discovered at most, low in its slot


@unittest.skipUnless(READY, "PoB not set up")
class RequirementsAndDefences(unittest.TestCase):
    def test_required_unique_slots_are_not_searched(self):
        spec = load(os.path.join(EX, "burning_arrow_vigour_deadeye.json"))
        uniques = [i["unique"] for i in spec["items"] if i.get("unique") and not i["slot"].startswith("Flask")]
        self.assertTrue(uniques)
        spec["requirements"] = {"uniques": uniques}
        c = run_cand(spec, requirements={"uniques": uniques})["candidates"]
        locked = set(c["locked"])
        self.assertEqual(len(locked), len(uniques))
        for g in c["groups"]:
            self.assertNotIn(g["target"], locked)

    def test_candidates_breaking_defences_are_rejected_and_spec_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            with open(path, "w") as f:
                json.dump(melee(), f)
            with open(path) as f:
                before = f.read()
            code, out, err = cli("candidates", path, "--json", "--variants-out", os.path.join(d, "v.json"))
            self.assertEqual(code, 0, err)
            data = json.loads(out)
            with open(path) as f:
                self.assertEqual(f.read(), before)                        # discovering never accepts
            rejected = [r for g in data["ranked"]["groups"] for r in g["rejected"]]
            self.assertTrue(any("breaks the profile target" in r["notes"][0] for r in rejected))
            for g in data["ranked"]["groups"]:
                for r in g["shortlist"]:
                    e = r["eval"]
                    self.assertGreaterEqual(e["min_ele_res"], data["targets"]["min_ele_res"])
            self.assertLessEqual(len(data["variants"]), 4)
            # accepted only through compare: the best variant is judged there, against the full build
            code, out, err = cli("compare", path, os.path.join(d, "v.json"), "--json")
            self.assertEqual(code, 0, err)
            res = json.loads(out)
            self.assertEqual(len(res["results"]), len(data["variants"]) + 1)
            self.assertTrue(all(r["verdict"] for r in res["results"]))


@unittest.skipUnless(READY, "PoB not set up")
class SlotAuditAndGearBasis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = melee()
        cls.r = fastpath.run_one({"mode": "build", "spec": cls.spec, "audit": True}, 900)

    def test_dead_slot_advisory(self):
        ev = qa.evaluate(self.r, qa.resolve_profile(self.spec.get("profile"), {}, self.r["level"]))
        gear = [e for e in ev if e[0] == "slots" and "changes neither DPS nor EHP" in e[2]]
        dead = [s for s in self.r["audit"]["slots"] if s.get("no_measured_benefit")]
        self.assertTrue(dead)
        for s in dead:
            self.assertTrue(any(e[2].startswith(s["slot"] + " ") for e in gear), s["slot"])
        for e in gear:
            self.assertEqual(e[3], "advisory")
            self.assertIn("doesn't make it useless", e[2])
        live = [s for s in self.r["audit"]["slots"] if s.get("without") and not s.get("no_measured_benefit")]
        for s in live:
            self.assertFalse(any(e[2].startswith(s["slot"] + " ") for e in gear), s["slot"])

    def test_gear_basis_change_is_flagged(self):
        sig = self.r["build_profile"]["signature"]
        same = dict(self.spec, gear_basis=sig)
        other = dict(self.spec, gear_basis=dict(sig, weapon_class="Bow (quiver)"))
        with tempfile.TemporaryDirectory() as d:
            for spec, expect in ((same, False), (other, True)):
                path = os.path.join(d, "g.json")
                with open(path, "w") as f:
                    json.dump(spec, f)
                code, out, err = cli("build", path, "-o", d, "--json", "--no-audit")
                data = json.loads(out)
                flagged = any("Gear basis changed" in n for n in data.get("notes") or [])
                self.assertEqual(flagged, expect, data.get("gear_basis_changes"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
