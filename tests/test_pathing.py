"""Passive-tree pathing: the Steiner-style target connection, pruning and attribute repair.

Claims are checked against PoB itself and against the old nearest-first allocation (kept behind
POE1_BUILDS_GREEDY_PATHING=1): the new route never costs more points for the same targets, every
named target is still allocated, the result is deterministic, and a spec that was valid under the old
routing stays valid (attribute repair) while a genuine attribute shortfall stays an error.

Run: python3 tests/test_pathing.py      (needs scripts/setup.sh; ~3 minutes)
"""
import copy
import glob
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import fastpath  # noqa: E402
import pob  # noqa: E402

READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)
SPECS = sorted(glob.glob(os.path.join(HERE, "fixtures", "*.json"))) + sorted(glob.glob(os.path.join(HERE, "..", "examples", "*.json")))


def load(path):
    with open(path) as f:
        s = pob.load_json_lenient(f.read())
    for k in ("allocated", "_fill_pinned", "_fill_added"):
        s["tree"].pop(k, None)
    return s


def run(spec, greedy=False):
    env = {"POE1_BUILDS_NO_CACHE": "1"}
    if greedy:
        env["POE1_BUILDS_GREEDY_PATHING"] = "1"
    with mock.patch.dict(os.environ, env):
        if not greedy:
            os.environ.pop("POE1_BUILDS_GREEDY_PATHING", None)
        return fastpath.run_one({"mode": "build", "spec": copy.deepcopy(spec)})


def target_points(resp):
    return sum(x.get("cost") or 0 for x in (resp.get("plan") or {}).get("tree") or [])


@unittest.skipUnless(READY, "Path of Building is not set up (run scripts/setup.sh)")
class Pathing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.specs = {p: load(p) for p in SPECS}
        cls.new = {p: run(s) for p, s in cls.specs.items()}
        cls.old = {p: run(s, greedy=True) for p, s in cls.specs.items()}

    def test_every_named_target_is_allocated(self):
        for p, r in self.new.items():
            self.assertTrue(r["valid"], (p, r.get("errors")))
            names = {x["name"] for x in r["plan"]["tree"]}
            for t in self.specs[p]["tree"].get("nodes") or []:
                if isinstance(t, str):
                    self.assertIn(t, names, (p, t))

    def test_never_more_points_than_nearest_first(self):
        for p in self.specs:
            pth = self.new[p]["plan"].get("pathing")
            self.assertIsNotNone(pth, p)
            self.assertLessEqual(pth["total_points"], pth["nearest_points"], p)
            # the old allocator's per-target costs sum to the tree it built; the new total must not exceed it
            self.assertLessEqual(pth["total_points"], target_points(self.old[p]), p)

    def test_saves_points_somewhere(self):
        saved = [self.new[p]["plan"]["pathing"]["nearest_points"] - self.new[p]["plan"]["pathing"]["total_points"] for p in self.specs]
        self.assertTrue(any(s > 0 for s in saved), saved)

    def test_deterministic(self):
        p = SPECS[0]
        a, b = run(self.specs[p]), run(self.specs[p])
        self.assertEqual(a["tree"]["allocated_ids"], b["tree"]["allocated_ids"])

    def test_valid_under_old_routing_stays_valid(self):
        for p in self.specs:
            if self.old[p]["valid"]:
                self.assertTrue(self.new[p]["valid"], (p, self.new[p].get("errors")))

    def test_attribute_repair_is_paid_for_by_the_saving(self):
        """Repair spends at most the points the search saved (plus what the old travel supplied, for the rest),
        so the build is never worse off than nearest-first left it; the fill never touches the repaired nodes."""
        for p, r in self.new.items():
            rep = r.get("attribute_repair")
            if rep and rep.get("added"):
                pth = r["plan"]["pathing"]
                saved = pth["nearest_points"] - pth["total_points"]
                over = rep["points"] - saved
                if over > 0:
                    # beyond the saving only when the old travel supplied the attribute
                    self.assertTrue(any(pth["attribute_delta"][a["attr"]] > 0 for a in rep["added"]), (p, rep))
                for a in rep["added"]:
                    self.assertGreaterEqual(a["gain"], 10)
                fill_ids = {x["id"] for x in (r.get("fill") or {}).get("added") or []}
                self.assertFalse({a["id"] for a in rep["added"]} & fill_ids, p)
                # the repaired nodes survive the post-fill prune
                for a in rep["added"]:
                    self.assertIn(a["id"], r["tree"]["allocated_ids"], (p, a))

    def test_strict_policy_when_repair_is_off(self):
        spec = copy.deepcopy(self.specs[os.path.join(HERE, "..", "examples", "rf_chieftain.json")])
        spec["tree"]["nodes"] = [n for n in spec["tree"]["nodes"] if n != "Heart of Oak"]   # the Dex on that path
        spec["tree"]["attribute_repair"] = False
        r = run(spec)
        self.assertFalse(r["valid"])
        self.assertTrue(any("Dexterity requirement" in e for e in r["errors"]), r["errors"])
        self.assertTrue(any("Attribute shortfall context" in n for n in r.get("notes") or []), r.get("notes"))
        self.assertNotIn("attribute_repair", r)

    def test_prune_false_disables_prune_and_repair(self):
        spec = copy.deepcopy(self.specs[SPECS[0]])
        spec["tree"]["prune"] = False
        r = run(spec)
        self.assertNotIn("attribute_repair", r)
        self.assertNotIn("pruned", r["plan"])
        self.assertNotIn("pruned_after", r.get("fill") or {})

    def test_pinned_tree_is_left_alone(self):
        p = SPECS[0]
        pinned = pob.pin_fill(self.specs[p], self.new[p]) or dict(self.specs[p], tree=dict(self.specs[p]["tree"], allocated=self.new[p]["tree"]["allocated_ids"]))
        r = run(pinned)
        self.assertEqual(sorted(r["tree"]["allocated_ids"]), sorted(self.new[p]["tree"]["allocated_ids"]))

    def test_report_lines(self):
        import contextlib
        import io
        for p, r in self.new.items():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                pob.print_report(r)
            text = buf.getvalue()
            self.assertIn("Pathing:", text, p)
            if (r.get("attribute_repair") or {}).get("added"):
                self.assertIn("Attribute repair:", text, p)


if __name__ == "__main__":
    unittest.main()
