"""Guard skills: trigger restriction, PoB uptime, guard-down max hits, temporary (not persistent)
protection, and the weak-guard advisory. Real PoB calculations; assertions compare PoB numbers
with each other, no expected values are hard-coded.

Run: python3 tests/test_guard.py      (needs scripts/setup.sh; ~2 minutes)
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
FIX = os.path.join(HERE, "fixtures", "ci_es_occultist.json")
READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)


def load(p):
    """An example without tree.fill: these tests vary only the guard setup and expect the tree to stay the same,
    and a fill would re-spend points differently per setup (its defence score sees the guard)."""
    with open(p) as f:
        s = pob.load_json_lenient(f.read())
    s.get("tree", {}).pop("fill", None)
    return s


SETUPS = {
    "token": None,                                                                # CWDT 1 + Steelskin 3 (examples)
    "cwdt20": [{"name": "Cast when Damage Taken", "level": 20}, {"name": "Steelskin", "level": 20}],
    "cwdt1_ss20": [{"name": "Cast when Damage Taken", "level": 1}, {"name": "Steelskin", "level": 20}],
    "selfcast": [{"name": "Steelskin", "level": 20}],
    "automation": ["Automation", {"name": "Steelskin", "level": 20}],
}


@unittest.skipUnless(READY, "PoB not set up")
class GuardOnExamples(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = {}
        reqs, keys = [], []
        for ex in ("burning_arrow_vigour_deadeye", "rf_chieftain"):
            s = load(os.path.join(EX, ex + ".json"))
            for k, gems in SETUPS.items():
                spec = s if gems is None else pob.apply_variant(s, {"gems": [{"group": "Cast when Damage Taken", "set": gems}]})
                keys.append((ex, k))
                reqs.append({"mode": "build", "spec": spec, "audit": True})
        for k, r in zip(keys, fastpath.run_many(reqs, workers=2)):
            cls.r[k] = r

    def ev(self, key):
        r = self.r[key]
        return qa.evaluate(r, qa.resolve_profile(r.get("profile") or {"content": "mapping"}, {}, r["level"]))

    def test_trigger_level_restriction_is_enforced(self):
        for ex in ("burning_arrow_vigour_deadeye", "rf_chieftain"):
            r = self.r[(ex, "cwdt1_ss20")]
            self.assertFalse(r["valid"])
            self.assertTrue(any("won't be triggered" in e for e in r["errors"]))
            self.assertTrue(self.r[(ex, "cwdt20")]["valid"])     # CWDT 20 allows Steelskin 20

    def test_guard_down_max_hit_is_the_same_for_every_valid_setup(self):
        for ex in ("burning_arrow_vigour_deadeye", "rf_chieftain"):
            downs = [self.r[(ex, k)]["audit"]["guards"][0]["without"]["max_hit"] for k in ("token", "cwdt20", "selfcast", "automation")]
            self.assertTrue(all(d == downs[0] for d in downs), downs)
            up_token = self.r[(ex, "token")]["audit"]["defence"]["PhysicalMaximumHitTaken"]
            up_big = self.r[(ex, "cwdt20")]["audit"]["defence"]["PhysicalMaximumHitTaken"]
            self.assertGreater(up_big, up_token)                  # more protection only while the guard is up
            self.assertGreaterEqual(up_token, downs[0]["Physical"])

    def test_uptime_comes_from_pob_and_is_partial(self):
        for key, r in self.r.items():
            if r["valid"]:
                g = r["audit"]["guards"][0]
                self.assertTrue(0 < g["uptime_pct"] < 100, (key, g))
                self.assertTrue(g["duration"] and g["cooldown"])

    def test_guard_is_temporary_not_a_layer(self):
        for ex in ("burning_arrow_vigour_deadeye", "rf_chieftain"):
            lines = {k: next(e[2] for e in self.ev((ex, k)) if e[2].startswith("Defensive layers")) for k in ("token", "cwdt20")}
            n = {k: int(v.split(": ")[1].split(" ")[0]) for k, v in lines.items()}
            self.assertEqual(n["token"], n["cwdt20"])
            self.assertIn("Temporary (not counted as a layer): guard", lines["cwdt20"])
            hit = next(e[2] for e in self.ev((ex, "cwdt20")) if e[2].startswith("Max hit"))
            self.assertTrue(hit.startswith("Max hit with the guard down"), hit)

    def test_weak_guard_advisory_only_for_weak_guards(self):
        for ex in ("burning_arrow_vigour_deadeye", "rf_chieftain"):
            weak = [e for e in self.ev((ex, "token")) if e[2].startswith("Guard ")]
            self.assertEqual(len(weak), 1)
            self.assertEqual(weak[0][3], "advisory")
            self.assertIn("Cast when Damage Taken 1 triggers skills requiring up to level", weak[0][2])
            self.assertFalse([e for e in self.ev((ex, "cwdt20")) if e[2].startswith("Guard ")])
            self.assertTrue(self.r[(ex, "token")]["valid"])        # advisory never invalidates


@unittest.skipUnless(READY, "PoB not set up")
class GuardOnIntBuild(unittest.TestCase):
    """An ES / int build where a high-level Steelskin isn't available (attributes)."""

    @classmethod
    def setUpClass(cls):
        fx = load(FIX)
        add = lambda gems: pob.apply_variant(fx, {"skills_add": [{"slot": "Boots", "gems": gems}]})
        cls.specs = {"token": add([{"name": "Cast when Damage Taken", "level": 1}, {"name": "Steelskin", "level": 3}]),
                     "ss20": add([{"name": "Steelskin", "level": 20}]),
                     "cloak": add([{"name": "Arcane Cloak", "level": 20}])}
        keys = list(cls.specs)
        cls.r = dict(zip(keys, fastpath.run_many([{"mode": "build", "spec": cls.specs[k], "audit": True} for k in keys], workers=2)))

    def test_attribute_limits_are_reported_not_forced(self):
        self.assertFalse(self.r["ss20"]["valid"])
        self.assertTrue(any("Strength requirement of Steelskin" in e for e in self.r["ss20"]["errors"]))
        r = self.r["token"]
        ev = qa.evaluate(r, qa.resolve_profile(self.specs["token"]["profile"], {}, r["level"]))
        adv = [e for e in ev if e[2].startswith("Guard ")]
        self.assertTrue(adv and "Str (have" in adv[0][2], adv)
        self.assertEqual(adv[0][3], "advisory")

    def test_alternative_guard_measured(self):
        r = self.r["cloak"]
        self.assertTrue(r["valid"], r["errors"])
        g = r["audit"]["guards"][0]
        self.assertEqual((g["guard"], g["trigger"]), ("Arcane Cloak", "self-cast"))
        self.assertGreaterEqual(r["audit"]["defence"]["PhysicalMaximumHitTaken"], g["without"]["max_hit"]["Physical"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
