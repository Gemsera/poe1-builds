"""Mechanic sweep: catalogue entries become measured variants; attribute-blocked gems are retried at lower levels and
never bought with off-tree attributes.

Run: python3 tests/test_sweep.py      (needs scripts/setup.sh; ~2 minutes)
"""
import io
import contextlib
import argparse
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import fastpath  # noqa: E402
import pob  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "eh_melee_assassin.json")
READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)


class Catalogue(unittest.TestCase):
    def test_catalogue_is_well_formed(self):
        with open(os.path.join(HERE, "..", "references", "mechanics.json")) as f:
            cat = json.load(f)["mechanics"]
        ids = [m["id"] for m in cat]
        self.assertEqual(len(ids), len(set(ids)))
        for m in cat:
            self.assertTrue(m.get("name") and m.get("variant") and "applies" in m, m)
        self.assertIn("taken_as_body", ids)
        self.assertIn("taken_as_helmet", ids)


@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class SweepRun(unittest.TestCase):
    def test_sweep_measures_retries_and_reports(self):
        with open(FIX) as f:
            spec = pob.load_json_lenient(f.read())
        # the fixture already has Rage: take it out so the sweep has something to find, and pin the tree
        g = spec["skills"][0]["gems"]
        g[[i for i, x in enumerate(g) if (x if isinstance(x, str) else x.get("name")) == "Rage"][0]] = "Increased Critical Damage"
        spec["config"].pop("multiplierRage", None)
        r = fastpath.run_many([{"mode": "build", "spec": spec}], workers=1)[0]
        pinned = pob.pin_fill(spec, r) or spec
        with tempfile.TemporaryDirectory() as td:
            sp = os.path.join(td, "spec.json")
            with open(sp, "w") as f:
                json.dump(pinned, f)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                pob.cmd_sweep(argparse.Namespace(spec=sp, workers=1, variants_out=os.path.join(td, "v.json")))
            out = buf.getvalue()
        self.assertIn("MECHANIC SWEEP", out)
        self.assertIn("Rage", out)
        # Rage Support at 20 needs Strength the build lacks: retried at lower levels, never bought
        self.assertIn("ATTRIBUTE-BLOCKED", out)
        self.assertIn("Rage @ level 16", out)
        self.assertIn("NOT PURSUED", out)
        self.assertIn("skipped:", out)
        # defensive Eater implicits are swept too (body and helmet), with the element taken from spare resistance
        self.assertIn("Phys taken as element (Eater body", out)
        self.assertIn("Phys taken as element (Eater helme", out)


if __name__ == "__main__":
    unittest.main()
