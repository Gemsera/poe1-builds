"""User requirements are hard constraints across the whole workflow.

The fixture is a generated Elemental Hit of the Spectrum Warden. Ordinary Elemental Hit and
Elemental Hit of the Spectrum share PoB's gameId (and the gemId written into exported builds);
they differ in variantId (ElementalHit / ElementalHitAltX), which is what the checks use.

Covered: wrong gem in either direction, gem and ascendancy kept through compare / tree-audit,
higher-DPS candidates rejected, missing ascendancy, invalid or unavailable requirements, user-
permitted alternatives labelled, no requirements = unchanged behaviour, and the final export
checked against the original requirements.

Run: python3 tests/test_requirements.py      (needs scripts/setup.sh; ~3 minutes)
"""
import argparse
import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import fastpath  # noqa: E402
import pob  # noqa: E402
import preflight  # noqa: E402
import requirements as rq  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "eh_spectrum_warden.json")
EX = os.path.join(HERE, "..", "examples")
READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)
SPECTRUM, ORDINARY = "Elemental Hit of the Spectrum", "Elemental Hit"


def fixture():
    with open(FIX) as f:
        return json.load(f)


def with_req(spec, **req):
    s = copy.deepcopy(spec)
    s["requirements"] = req
    return s


def main_gem(spec, name):
    s = copy.deepcopy(spec)
    g = s["skills"][0]["gems"]
    g[0] = dict(g[0], name=name) if isinstance(g[0], dict) else name
    return s


def names():
    raw = preflight.load_names(allow_spawn=True)
    return preflight.Names(raw)


def run_cli(fn, **kw):
    a = argparse.Namespace(**kw)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(a)
    return rc, buf.getvalue()


def build_cli(spec, outdir, json_out=True):
    path = os.path.join(outdir, "spec_%d.json" % abs(hash(json.dumps(spec, sort_keys=True))))
    with open(path, "w") as f:
        json.dump(spec, f)
    rc, out = run_cli(pob.cmd_build, spec=path, out=outdir, json=json_out, show_code=False, audit=False, audit_detail=False,
                      investment=None, content=None, game_mode=None, share=False, repair=False, preflight=True,
                      fail_fast=False)
    return rc, (json.loads(out) if json_out else out)


def compare_cli(spec, variants, outdir):
    sp, vp = os.path.join(outdir, "cmp_spec.json"), os.path.join(outdir, "cmp_variants.json")
    with open(sp, "w") as f:
        json.dump(spec, f)
    with open(vp, "w") as f:
        json.dump(variants, f)
    rc, out = run_cli(pob.cmd_compare, spec=sp, variants=vp, json=True, workers=2, preflight=True, min_gain=1.0,
                      tolerance=1.0, objective="dps", max_candidates=None)
    return json.loads(out)


@unittest.skipUnless(READY, "PoB not set up")
class GemIdentity(unittest.TestCase):
    def test_pob_identifiers(self):
        """variantId is unique; gameId is shared, so it can't identify the requested gem."""
        n = names()
        o, s = n.gems[ORDINARY], n.gems[SPECTRUM]
        self.assertEqual((o["variant_id"], s["variant_id"]), ("ElementalHit", "ElementalHitAltX"))
        self.assertEqual(o["game_id"], s["game_id"])
        ids = [g["variant_id"] for g in n.raw["gems"] if g.get("variant_id")]
        self.assertEqual(len(ids), len(set(ids)))


@unittest.skipUnless(READY, "PoB not set up")
class WrongGem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        base = fixture()
        cls.rc_a, cls.a = build_cli(with_req(base, main_skill=ORDINARY), cls.tmp)        # ordinary asked, Spectrum supplied
        cls.rc_b, cls.b = build_cli(with_req(main_gem(base, ORDINARY), main_skill=SPECTRUM), cls.tmp)  # the reverse
        cls.rc_ok, cls.ok = build_cli(with_req(base, main_skill=SPECTRUM, ascendancy="Warden", **{"class": "Ranger"}), cls.tmp)
        cls.rc_alt, cls.alt = build_cli(with_req(base, main_skill=ORDINARY, also_accept=[SPECTRUM]), cls.tmp)

    def test_ordinary_requested_spectrum_supplied(self):
        self.assertEqual(self.rc_a, 2)
        self.assertFalse(self.a["valid"])
        errs = [e for e in self.a["errors"] if e.startswith(rq.PREFIX)]
        self.assertTrue(any("[ElementalHitAltX]" in e and "[ElementalHit]" in e for e in errs), errs)
        self.assertTrue(self.a["requirements"]["export_violations"])          # the export was checked too
        self.assertTrue(all(f.endswith((".invalid.xml", ".invalid.pob.txt")) for f in self.a["files"]))

    def test_spectrum_requested_ordinary_supplied(self):
        self.assertEqual(self.rc_b, 2)
        errs = [e for e in self.b["errors"] if e.startswith(rq.PREFIX)]
        self.assertTrue(any("main skill is Elemental Hit [ElementalHit], the user asked for Elemental Hit of the Spectrum"
                            in e for e in errs), errs)

    def test_matching_requirements_pass_and_export_is_checked(self):
        self.assertEqual(self.rc_ok, 0, self.ok["errors"])
        r = self.ok["requirements"]
        self.assertEqual((r["violations"], r["export_violations"], r["export_checked"]), ([], [], True))

    def test_user_permitted_alternative_is_labelled(self):
        self.assertEqual(self.rc_alt, 0, self.alt["errors"])
        self.assertTrue(any("an alternative the user permitted" in n for n in self.alt["requirements"]["notes"]))


@unittest.skipUnless(READY, "PoB not set up")
class Optimisation(unittest.TestCase):
    """compare and tree-audit keep the gem and the ascendancy; higher numbers never win over a requirement."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        base = fixture()
        base["skills"][0]["gems"][5] = "Inspiration"            # Hypothermia measured higher DPS for this build
        cls.base = with_req(base, main_skill=SPECTRUM, ascendancy="Warden", gems=["Inspiration"],
                            passives=["Forces of Nature"], **{"class": "Ranger"})
        cls.variants = [
            {"name": "support swap (allowed)", "gems": [{"group": "main", "replace": {"barrage support": "Mirage Archer"}}]},
            {"name": "required gem swapped for more DPS", "gems": [{"group": "main", "replace": {"inspiration": "Hypothermia"}}]},
            {"name": "main gem swapped", "gems": [{"group": "main", "replace": {SPECTRUM.lower(): ORDINARY}}]},
            {"name": "ascendancy changed", "set": {"ascendancy": "Deadeye"},
             "ascendancy_nodes": ["Gathering Winds", "Far Shot", "Endless Munitions"]},
            {"name": "requirements loosened", "set": {"requirements": {"main_skill": SPECTRUM}},
             "gems": [{"group": "main", "replace": {"inspiration": "Hypothermia"}}]},
            {"name": "config assumption", "config": {"Unbound": True}},
        ]
        cls.cmp = compare_cli(cls.base, cls.variants, cls.tmp)
        cls.by = {r["name"]: r for r in cls.cmp["results"]}

    def test_higher_dps_variant_rejected(self):
        r, b = self.by["required gem swapped for more DPS"], self.by["original"]
        self.assertGreater(r["dps"], b["dps"])
        self.assertTrue(r["verdict"].startswith("rejected: breaks user requirement"), r["verdict"])

    def test_gem_and_ascendancy_changes_rejected(self):
        for n in ("main gem swapped", "ascendancy changed", "requirements loosened"):
            self.assertTrue(self.by[n]["verdict"].startswith("rejected: breaks user requirement"), (n, self.by[n]["verdict"]))

    def test_allowed_change_is_judged_normally_and_nothing_illegal_is_recommended(self):
        self.assertFalse(self.by["support swap (allowed)"]["requirement_violations"])
        self.assertFalse(self.by["support swap (allowed)"]["verdict"].startswith("rejected"))
        self.assertTrue(self.by["config assumption"]["verdict"].startswith("assumption test"))
        self.assertIn(self.cmp["recommended"], ("original", "support swap (allowed)"))

    def test_tree_audit_keeps_required_passive(self):
        sp = os.path.join(self.tmp, "ta.json")
        with open(sp, "w") as f:
            json.dump(self.base, f)
        rc, out = run_cli(pob.cmd_tree_audit, spec=sp, targets="Forces of Nature,Master Fletcher", json=True, workers=2)
        rows = {r["node"]: r for r in json.loads(out)}
        self.assertEqual(rows["Forces of Nature"]["note"], "required by the user: not audited")
        self.assertIsNotNone(rows["Master Fletcher"]["points_freed"])


@unittest.skipUnless(READY, "PoB not set up")
class AscendancyAndAvailability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.n = names()
        s = fixture()
        s.pop("ascendancy")
        s["tree"].pop("ascendancy")
        cls.no_asc = with_req(s, ascendancy="Warden")
        cls.rc, cls.out = build_cli(cls.no_asc, cls.tmp)

    def test_missing_required_ascendancy(self):
        self.assertEqual(self.rc, 2)
        errs = self.out["errors"]
        self.assertTrue(any("no ascendancy is selected, the user asked for Warden" in e for e in errs), errs)
        self.assertTrue(any("ascendancy is not set" in e for e in self.out["preflight"]["errors"]))

    def test_invalid_or_unavailable_requirements(self):
        base = fixture()
        cases = {
            "unknown key": ({"main_skil": SPECTRUM}, "unknown key 'main_skil'"),
            "wrong type": ({"gems": "Trinity"}, "'gems' must be a list"),
            "gem missing": ({"main_skill": "Elemental Hit of Nonsense"}, "does not exist in this PoB version"),
            "case differs": ({"main_skill": "elemental hit"}, "exact name: Elemental Hit"),
            "support as main": ({"main_skill": "Trinity"}, "is a support gem"),
            "wrong class": ({"class": "Witch", "ascendancy": "Warden"}, "belongs to Ranger, not Witch"),
            "unknown ascendancy": ({"ascendancy": "Wardenn"}, "unknown ascendancy"),
            "unknown unique": ({"uniques": ["Bow of Nothing"]}, "unique 'Bow of Nothing' does not exist"),
            "unknown passive": ({"passives": ["Totally Real Keystone"]}, "passive 'Totally Real Keystone' does not exist"),
            "orphan alternative": ({"also_accept": [ORDINARY]}, "'main_skill', which is not set"),
        }
        for label, (req, text) in cases.items():
            s = copy.deepcopy(base)
            s["requirements"] = req
            errs = preflight.check(s, self.n)["errors"]
            self.assertTrue(any(text in e for e in errs), (label, errs))
        self.assertEqual(rq.check_spec({"requirements": "x"}), ["Requirements: 'requirements' must be an object "
                                                               "(see references/spec-format.md)"])


@unittest.skipUnless(READY, "PoB not set up")
class NoRequirements(unittest.TestCase):
    """Without requirements nothing changes: same validity, no requirement output, normal verdicts."""

    def test_examples_unchanged(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(EX, "rf_chieftain.json")) as f:
            spec = json.load(f)
        rc, out = build_cli(spec, tmp)
        self.assertEqual(rc, 0)
        self.assertNotIn("requirements", out)
        self.assertFalse([e for e in out["errors"] if e.startswith(rq.PREFIX)])
        cmp = compare_cli(spec, [{"name": "swap", "gems": [{"group": "main", "replace": {"swift affliction": "Controlled Destruction"}}]}],
                          tmp)
        self.assertIsNone(cmp["user_requirements"])
        self.assertFalse(cmp["results"][1]["verdict"].startswith(("rejected: breaks user", "assumption test", "user-permitted")))
        self.assertEqual(cmp["results"][1]["requirement_violations"], [])

    def test_variants_still_free_to_compare_gems(self):
        """No main_skill requirement: a gem swap is just another candidate."""
        tmp = tempfile.mkdtemp()
        cmp = compare_cli(fixture(), [{"name": "ordinary EH", "gems": [{"group": "main", "replace": {SPECTRUM.lower(): ORDINARY}}]}],
                          tmp)
        self.assertFalse(cmp["results"][1]["verdict"].startswith("rejected: breaks user"))


@unittest.skipUnless(READY, "PoB not set up")
class FinalExport(unittest.TestCase):
    """The exported file itself is checked against the original requirements, by variantId (not gemId)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.req = {"main_skill": SPECTRUM, "ascendancy": "Warden", "class": "Ranger", "passives": ["Forces of Nature"]}
        rc, out = build_cli(with_req(fixture(), **cls.req), cls.tmp)
        assert rc == 0, out["errors"]
        cls.xml = open(next(f for f in out["files"] if f.endswith(".xml"))).read()
        cls.n = names()

    def test_valid_export_passes(self):
        self.assertEqual(rq.check_export(self.req, self.xml, self.n), [])

    def test_tampered_export_detected(self):
        tag = re.search(r'<Gem\b[^>]*nameSpec="Elemental Hit of the Spectrum"[^>]*/>', self.xml).group(0)
        ordinary = (tag.replace('variantId="ElementalHitAltX"', 'variantId="ElementalHit"')
                       .replace('skillId="ElementalHitAltX"', 'skillId="ElementalHit"')
                       .replace('nameSpec="Elemental Hit of the Spectrum"', 'nameSpec="Elemental Hit"'))
        self.assertIn('gemId="Metadata/Items/Gems/SkillGemElementalHit"', tag)     # identical in both: not usable
        bad = self.xml.replace(tag, ordinary)
        v = rq.check_export(self.req, bad, self.n)
        self.assertTrue(any("exported main skill is Elemental Hit [ElementalHit]" in x for x in v), v)
        bad_asc = re.sub(r'ascendClassName="Warden"', 'ascendClassName="Deadeye"', self.xml)
        self.assertTrue(any("exported ascendancy is Deadeye" in x for x in rq.check_export(self.req, bad_asc, self.n)))
        # the CLI check of an exported build against the original requirements
        xp, rp = os.path.join(self.tmp, "tampered.xml"), os.path.join(self.tmp, "req.json")
        with open(xp, "w") as f:
            f.write(bad)
        with open(rp, "w") as f:
            json.dump({"requirements": self.req}, f)
        rc, out = run_cli(pob.cmd_check, build=xp, out=self.tmp, name="tampered", json=True, show_code=False, audit=False,
                          audit_detail=False, investment=None, content=None, game_mode=None, share=False, requirements=rp)
        self.assertEqual(rc, 2)
        self.assertTrue(json.loads(out)["requirements"]["violations"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
