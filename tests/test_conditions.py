"""Conditional performance, multi-element skills and stacks (advisory reporting only):

  G1  each conditional option gets evidence (PoB chances, PoB option gates/flags, source lines in the
      build); two scenarios between configured and floor are PoB recalculations
  G2  a random-element skill is disclosed as one part of several; no blended figure is invented
  G3  count options report PoB's cap (detected by recalculation) and the effect at half the value

Expected numbers come from independent PoB calculations of the same build with the assumption
removed, never from hard-coded values. Nothing here may raise a material or critical finding.

Run: python3 tests/test_conditions.py      (needs scripts/setup.sh; ~2 minutes)
"""
import copy
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
UNSOURCED = {"usePowerCharges": True, "buffOnslaught": True, "conditionEnemyBlinded": True}


def spec(name):
    with open(os.path.join(EX, name + ".json")) as f:
        return pob.load_json_lenient(f.read())


def with_config(s, **extra):
    s = copy.deepcopy(s)
    s["config"] = dict(s.get("config") or {}, **extra)
    return s


def with_main_gems(s, gems):
    s = copy.deepcopy(s)
    s["skills"][0]["gems"] = gems
    return s


def evaluate(r, s):
    return qa.evaluate(r, qa.resolve_profile(s.get("profile") or {"content": "mapping"}, {}, r["level"]))


def cfg(r, var):
    return next(c for c in r["audit"]["config"] if c["var"] == var)


@unittest.skipUnless(READY, "PoB not set up")
class ConditionEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        de = spec("burning_arrow_vigour_deadeye")
        trinity = with_main_gems(de, de["skills"][0]["gems"][:5] + ["Trinity"])
        # the example's own ring (it supplies Intelligence the build needs) plus the line under test
        ring = next(i["text"] for i in de["items"] if i["slot"] == "Ring 1") + "\n{}"
        cls.specs = {
            "base": de,
            "extra": with_config(de, **UNSOURCED),
            # the same build with the unsourced options simply not set: the independent check
            "extra_removed": de,
            "trinity50": with_config(trinity, configResonanceCount=50),
            "trinity80": with_config(trinity, configResonanceCount=80),
            "trinity25": with_config(trinity, configResonanceCount=25),
            "trinity20": with_config(trinity, configResonanceCount=20),
            "blind_consumer": with_config(pob.apply_variant(de, {"items": [{"slot": "Ring 1", "text": ring.format(
                "25% increased Critical Strike Chance against Blinded Enemies")}]}), conditionEnemyBlinded=True),
            "blind_source": with_config(pob.apply_variant(de, {"items": [{"slot": "Ring 1", "text": ring.format(
                "10% chance to Blind Enemies on Hit with Attacks")}]}), conditionEnemyBlinded=True),
        }
        keys = list(cls.specs)
        xmls = fastpath.run_many([{"mode": "build", "spec": cls.specs[k]} for k in keys], workers=2)
        # check mode: imported-build rules (an unusual ring line is a warning, not an error)
        res = fastpath.run_many([{"mode": "check", "xml": x["xml"], "audit": True} for x in xmls], workers=2)
        cls.r = dict(zip(keys, res))

    def test_unsourced_conditions_found_and_scenario_is_a_real_recalculation(self):
        r = self.r["extra"]
        for var in UNSOURCED:
            self.assertEqual(cfg(r, var)["evidence"]["status"], "no source found", var)
        sc = r["audit"]["evidence_supported"]
        self.assertEqual(sorted(sc["turned_off"]), sorted(UNSOURCED))
        self.assertAlmostEqual(sc["without"]["dps"], self.r["extra_removed"]["summary"]["dps"], places=4)
        self.assertEqual(sc["without"]["ehp"], round(self.r["extra_removed"]["audit"]["baseline"]["ehp"]))

    def test_sourced_condition_has_sustaining_evidence(self):
        ev = cfg(self.r["base"], "useFrenzyCharges")["evidence"]
        self.assertEqual(ev["status"], "source found")
        self.assertTrue(ev["sustaining_source"])
        self.assertTrue(any(e["kind"] == "source line" and e["sustaining"] for e in ev["evidence"]))
        self.assertIsNotNone(ev["pob_values"]["max"])
        self.assertEqual(self.r["base"]["audit"]["evidence_supported"]["turned_off"], [])

    def test_consumer_lines_are_not_sources(self):
        self.assertEqual(cfg(self.r["blind_consumer"], "conditionEnemyBlinded")["evidence"]["status"], "no source found")
        ev = cfg(self.r["blind_source"], "conditionEnemyBlinded")["evidence"]
        self.assertEqual(ev["status"], "source found")
        self.assertTrue(any("chance to Blind" in e["text"] and e["trigger"] == "on hit" for e in ev["evidence"]))

    def test_pob_cap_detected_by_recalculation(self):
        """G3: Trinity's resonance option is clamped by PoB; the audit finds that by recalculating."""
        for k, capped in (("trinity50", True), ("trinity80", True), ("trinity20", False)):
            st = cfg(self.r[k], "configResonanceCount")["stack"]
            self.assertEqual(st["at_pob_cap"], capped, k)
        self.assertAlmostEqual(self.r["trinity80"]["summary"]["dps"], self.r["trinity50"]["summary"]["dps"], places=4)
        st = cfg(self.r["trinity50"], "configResonanceCount")["stack"]
        self.assertEqual(st["half"], 25)
        self.assertAlmostEqual(st["at_half"]["dps"], self.r["trinity25"]["summary"]["dps"], places=4)
        ev = cfg(self.r["trinity50"], "configResonanceCount")["evidence"]
        self.assertTrue(any(e["kind"] == "PoB option" and "Trinity" in e["text"] for e in ev["evidence"]))

    def test_everything_stays_advisory(self):
        for k, r in self.r.items():
            for e in evaluate(r, self.specs[k]):
                if e[0] == "config":
                    self.assertIn(e[3], ("advisory", "info"), (k, e[2][:120]))

    def test_levels_line(self):
        line = next(e for e in evaluate(self.r["extra"], self.specs["extra"]) if e[2].startswith("Conditional performance"))
        self.assertIn("with conditions that have no source off", line[2])
        self.assertIn("assume full uptime for what they keep", line[2])
        self.assertIn("lower bound, not a prediction", line[2])
        self.assertTrue([e for e in evaluate(self.r["extra"], self.specs["extra"]) if e[2].startswith("Configured: DPS")])
        base = [e for e in evaluate(self.r["base"], self.specs["base"]) if e[2].startswith("Conditional performance")]
        self.assertEqual(base[0][3], "info")          # nothing turned off in either level: not a review item
        self.assertIn("No level estimates uptime", line[2])


@unittest.skipUnless(READY, "PoB not set up")
class RandomElement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rf = spec("rf_chieftain")
        cls.ws_spec = with_main_gems(rf, ["Wild Strike", "Elemental Damage with Attacks", "Added Fire Damage", "Inspiration"])
        cls.ws, cls.rf = fastpath.run_many([{"mode": "build", "spec": cls.ws_spec, "audit": True},
                                            {"mode": "build", "spec": rf, "audit": True}], workers=2)

    def test_disclosed_with_every_part_and_no_blend(self):
        msc = self.ws["main_skill_config"]
        self.assertTrue(msc["random_element"])
        ev = evaluate(self.ws, self.ws_spec)
        line = [e for e in ev if "picks its element per use" in e[2]]
        self.assertEqual(len(line), 1)
        self.assertEqual(line[0][3], "advisory")
        for name in msc["part"]["names"]:
            self.assertIn(name, line[0][2])
        self.assertIn("No blended figure", line[0][2])
        others = {o["name"] for o in self.ws["audit"]["other_parts"]}
        self.assertEqual(others | {msc["part"]["name"]}, set(msc["part"]["names"]))

    def test_not_shown_for_other_skills(self):
        self.assertNotIn("random_element", self.rf["main_skill_config"])
        self.assertFalse([e for e in evaluate(self.rf, spec("rf_chieftain")) if "picks its element" in e[2]])


class EvidenceText(unittest.TestCase):
    """Wording without PoB: statuses, non-sustaining sources and caps are stated, uptime never is."""

    def line(self, evidence, stack=None):
        c = {"var": "x", "value": True, "category": "buff", "evidence": evidence, "stack": stack}
        return qa._evidence_text(c)

    def test_statuses(self):
        self.assertIn("No source found", self.line({"status": "no source found", "evidence": []}))
        self.assertIn("not assessed", self.line({"status": "not assessed", "evidence": []}))
        t = self.line({"status": "source found", "sustaining_source": False,
                       "evidence": [{"kind": "source line", "text": "passive X: 8% chance to gain a Frenzy Charge on Kill",
                                     "trigger": "on kill", "sustaining": False}]})
        self.assertIn("[on kill]", t)
        self.assertIn("can be assumed to keep it up", t)

    def test_stack_and_counter(self):
        t = self.line({"status": "source found", "sustaining_source": True, "counter": ["main skill (as configured): 0% chance to Ignite per hit"],
                       "evidence": [{"kind": "PoB option", "text": "T is in an enabled skill group", "trigger": "build mechanic",
                                     "sustaining": True}]},
                      {"value": 50, "half": 25, "at_pob_cap": True, "at_half": {"dps_pct": -11.5}})
        self.assertIn("PoB's cap", t)
        self.assertIn("at 25: -11.5% DPS", t)
        self.assertIn("0% chance to Ignite", t)
        self.assertNotIn("uptime is", t.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
