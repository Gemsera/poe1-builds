"""Stage counts and skill parts: preflight validation, driver application, audit disclosure.

Every "applied" claim is checked against PoB itself: the same build with PoB's own gem
attribute (skillStageCount / skillPart) set directly must give the same DPS. No expected DPS
values are hard-coded.

Run: python3 tests/test_stages.py      (needs scripts/setup.sh; ~1-2 minutes)
"""
import copy
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import audit as qa  # noqa: E402
import fastpath  # noqa: E402
import pob  # noqa: E402
import preflight  # noqa: E402
import reference as ra  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "ci_es_occultist.json")
READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)


def base_spec():
    with open(FIX) as f:
        return pob.load_json_lenient(f.read())


def with_main(gem):
    s = base_spec()
    s["skills"][0]["gems"][0] = gem
    return s


def set_attr(xml, gem, attr, value):
    """Set one PoB gem attribute in a saved build (replace it if PoB already wrote it)."""
    tag = re.search(r'<Gem [^>]*nameSpec="%s"[^>]*/>' % re.escape(gem), xml).group(0)
    new = re.sub(r'%s="\d+"' % attr, '%s="%s"' % (attr, value), tag) if (attr + '="') in tag else \
        tag.replace("<Gem ", '<Gem %s="%s" ' % (attr, value), 1)
    return xml.replace(tag, new)


@unittest.skipUnless(READY, "PoB not set up")
class Stages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.names = preflight.Names(preflight.load_names())
        specs = {
            "default": with_main("Incinerate"),
            "three": with_main({"name": "Incinerate", "stages": 3}),
            "release": with_main({"name": "Incinerate", "skill_part": "Release"}),
            "vortex": base_spec(),
            "vortex_stages": with_main({"name": "Vortex", "stages": 3}),
            "too_many": with_main({"name": "Incinerate", "stages": 99}),
            "bad_part": with_main({"name": "Incinerate", "skill_part": 9}),
        }
        cls.specs = specs
        keys = list(specs)
        res = fastpath.run_many([{"mode": "build", "spec": specs[k], "audit": True} for k in keys], workers=2)
        cls.r = dict(zip(keys, res))
        cls.max = cls.r["default"]["main_skill_config"]["stages"]["max"]
        extra = {"explicit_max": with_main({"name": "Incinerate", "stages": cls.max})}
        cls.r.update(dict(zip(extra, fastpath.run_many([{"mode": "build", "spec": s, "audit": True}
                                                         for s in extra.values()], workers=1))))

    def evaluate(self, key):
        r = self.r[key]
        return qa.evaluate(r, qa.resolve_profile({"content": "mapping", "investment": "medium"}, {}, r["level"]))

    # --- a skill with stages ------------------------------------------------------------
    def test_stage_count_is_applied_by_pob(self):
        cfg = self.r["three"]["main_skill_config"]["stages"]
        self.assertEqual((cfg["used"], cfg["explicit"], cfg["requested"]), (3, True, 3))
        direct = fastpath.run_one({"mode": "check", "xml": set_attr(self.r["default"]["xml"], "Incinerate", "skillStageCount", 3)})
        self.assertAlmostEqual(direct["summary"]["dps"], self.r["three"]["summary"]["dps"], places=4)
        self.assertNotAlmostEqual(self.r["three"]["summary"]["dps"], self.r["default"]["summary"]["dps"], places=0)

    def test_unset_stages_use_maximum_and_are_flagged(self):
        cfg = self.r["default"]["main_skill_config"]["stages"]
        self.assertEqual(cfg["used"], cfg["max"])
        self.assertFalse(cfg["explicit"])
        flag = [e for e in self.evaluate("default") if "no stage count set" in e[2]]
        self.assertTrue(flag)
        low = self.r["default"]["audit"]["min_stage"]["without"]["dps"]
        if low < self.r["default"]["summary"]["dps"] * 0.9:
            self.assertEqual(flag[0][3], "material")
        self.assertTrue(self.r["default"]["valid"])            # disclosure, never invalidity

    def test_explicit_stage_count_is_not_penalised(self):
        for key in ("three", "explicit_max"):
            ev = [e for e in self.evaluate(key) if " stages" in e[2]]
            self.assertTrue(ev, key)
            self.assertEqual(ev[0][3], "info", key)
            self.assertIn("(set in the build)", ev[0][2])

    def test_min_stage_measurement_restores_the_build(self):
        r = self.r["default"]
        self.assertFalse(any("did not return" in w for w in r["warnings"]))
        self.assertEqual(r["audit"]["baseline"]["dps"], r["summary"]["dps"])

    # --- a skill with selectable parts -----------------------------------------------------
    def test_skill_part_is_applied_by_pob(self):
        part = self.r["release"]["main_skill_config"]["part"]
        self.assertEqual((part["index"], part["name"], part["explicit"]), (2, "Release", True))
        direct = fastpath.run_one({"mode": "check", "xml": set_attr(self.r["default"]["xml"], "Incinerate", "skillPart", 2)})
        self.assertAlmostEqual(direct["summary"]["dps"], self.r["release"]["summary"]["dps"], places=4)
        others = {p["name"]: p["without"]["dps"] for p in self.r["default"]["audit"]["other_parts"]}
        self.assertAlmostEqual(others["Release"], self.r["release"]["summary"]["dps"], delta=1e-6 * self.r["release"]["summary"]["dps"] + 1)

    # --- a skill without stages --------------------------------------------------------------
    def test_skill_without_stages(self):
        r = self.r["vortex"]
        self.assertTrue(r["valid"])
        self.assertNotIn("stages", r["main_skill_config"])
        self.assertNotIn("min_stage", r["audit"])
        self.assertFalse([e for e in self.evaluate("vortex") if " stages" in e[2]])

    # --- unsupported / invalid settings ------------------------------------------------------
    def test_preflight_rejects_unsupported_settings(self):
        bad = {"vortex_stages": "no stages", "bad_part": "out of range"}
        for key, text in bad.items():
            errs = preflight.check(self.specs[key], self.names)["errors"]
            self.assertTrue(any(text in e for e in errs), (key, errs))
        s = base_spec()
        s["skills"][0]["gems"][1] = {"name": "Controlled Destruction", "stages": 2}
        self.assertTrue(any("support gem" in e for e in preflight.check(s, self.names)["errors"]))
        s = with_main({"name": "Incinerate", "stages": 0})
        self.assertTrue(any(">= 1" in e for e in preflight.check(s, self.names)["errors"]))

    def test_driver_rejects_what_pob_would_clamp(self):
        self.assertFalse(self.r["vortex_stages"]["valid"])
        self.assertTrue(any("no stages" in e for e in self.r["vortex_stages"]["errors"]))
        self.assertFalse(self.r["too_many"]["valid"])
        self.assertTrue(any("maximum is %d" % self.max in e for e in self.r["too_many"]["errors"]))
        self.assertFalse(self.r["bad_part"]["valid"])
        self.assertTrue(any("isn't one of its PoB parts" in e for e in self.r["bad_part"]["errors"]))

    # --- a reference whose headline relies on maximum stages ----------------------------------
    def test_reference_discloses_maximum_stage_assumption(self):
        ref = fastpath.run_one({"mode": "check", "xml": self.r["default"]["xml"], "audit": True, "reference": True})
        n = ra.normalise(ref, qa.resolve_profile({"content": "mapping", "investment": "medium"}, {}, ref["level"]))
        self.assertEqual(n["facts"]["main_skill_config"]["stages"]["used"], n["facts"]["main_skill_config"]["stages"]["max"])
        self.assertTrue(any("no stage count set" in l for l in n["facts"]["skill_config_lines"]))
        self.assertTrue(any(f["severity"] == "material" and "stage" in f["message"] for f in n["facts"]["audit_findings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
