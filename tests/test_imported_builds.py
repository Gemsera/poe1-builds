"""Imported-build correctness (reference analysis and plain checks):

  W1  PoB's Auto character level must not change while the audit removes passives
  W2  driver warnings reach the reference report, deduplicated
  W3  crafted rolls stored as ranges are checked as rolled values (no weaker legality check)
  W4  flask counting and the flasks-on adjustment use the active gear set only
  W5  saved export stats are compared with PoB's recalculation, with tested causes
  W6  a plain check never mixes statistics from two character levels

Builds are the skill's own examples, turned into the shapes PoB exports produce (Auto level,
several gear sets, item-crafter rolls). Expected values come from PoB itself (a manual-level copy,
a recalculation), never from hard-coded numbers.

Run: python3 tests/test_imported_builds.py      (needs scripts/setup.sh; ~3 minutes)
"""
import argparse
import contextlib
import io
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import fastpath  # noqa: E402
import pob  # noqa: E402
import reference as ra  # noqa: E402
import audit as qa  # noqa: E402

EX = os.path.join(HERE, "..", "examples")
READY = os.path.exists(fastpath.LUAJIT) and os.path.exists(fastpath.POB_SRC)


def spec(name, fill=False):
    """An example spec. The examples use tree.fill; these tests want the unfilled tree (unspent points
    are what makes PoB's Auto level differ from the saved level), so fill is off unless asked for."""
    with open(os.path.join(EX, name + ".json")) as f:
        s = pob.load_json_lenient(f.read())
    if not fill:
        s.get("tree", {}).pop("fill", None)
    return s


def built_xml(s):
    return fastpath.run_one({"mode": "build", "spec": s})["xml"]


def auto_level(xml):
    out = xml.replace('characterLevelAutoMode="false"', 'characterLevelAutoMode="true"')
    assert out != xml
    return out


def manual_level(xml, level):
    return re.sub(r'(<Build\b[^>]*?)\blevel="\d+"', r'\g<1>level="%d"' % level, xml)


def stats(r):
    out = {}
    for g in (r.get("stats") or {}).values():
        if isinstance(g, dict):
            out.update(g)
    return out


def cli_reference(xml, adjust=None):
    tmp = os.path.join(fastpath.POB_HOME, "cache", "test_imported_%d.xml" % os.getpid())
    with open(tmp, "w") as f:
        f.write(xml)
    a = argparse.Namespace(build=tmp, against=None, investment="high", content="mapping", game_mode="softcore",
                           json=True, adjust=adjust)
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = pob.cmd_reference(a)
    finally:
        os.remove(tmp)
    return rc, json.loads(buf.getvalue())


@unittest.skipUnless(READY, "PoB not set up")
class AutoLevel(unittest.TestCase):
    """W1 and W6. RF's example has unspent points, so PoB's Auto level is far below the saved level 90;
    the Deadeye example's Auto level is one below its saved 95."""

    @classmethod
    def setUpClass(cls):
        cls.xml = {"rf": built_xml(spec("rf_chieftain")), "de": built_xml(spec("burning_arrow_vigour_deadeye"))}
        cls.auto = {k: auto_level(v) for k, v in cls.xml.items()}
        first = fastpath.run_many([{"mode": "check", "xml": cls.auto[k]} for k in ("rf", "de")], workers=2)
        cls.plain = dict(zip(("rf", "de"), first))
        cls.level = {k: r["level"] for k, r in cls.plain.items()}
        reqs = [{"mode": "check", "xml": manual_level(cls.xml[k], cls.level[k])} for k in ("rf", "de")]
        reqs += [{"mode": "check", "xml": cls.auto[k], "audit": True, "reference": True} for k in ("rf", "de")]
        reqs += [{"mode": "check", "xml": manual_level(cls.xml["de"], cls.level["de"]), "audit": True, "reference": True}]
        res = fastpath.run_many(reqs, workers=2)
        cls.manual = dict(zip(("rf", "de"), res[:2]))
        cls.ref = dict(zip(("rf", "de"), res[2:4]))
        cls.ref_manual_de = res[4]

    def test_auto_level_differs_from_saved_level(self):
        self.assertLess(self.level["rf"], 90)
        self.assertEqual(self.plain["rf"]["level_info"], {"auto": True, "saved": 90, "used": self.level["rf"]})
        self.assertTrue(any("Auto level sets the character to level %d" % self.level["rf"] in w for w in self.plain["rf"]["warnings"]))

    def test_plain_check_uses_one_level(self):
        """W6: every statistic equals a manual-level build at the Auto level."""
        for k in ("rf", "de"):
            p, m = stats(self.plain[k]), stats(self.manual[k])
            for key in ("Life", "Mana", "TotalEHP", "PhysicalMaximumHitTaken", "FireMaximumHitTaken"):
                self.assertEqual(p.get(key), m.get(key), (k, key))
            self.assertEqual(self.plain[k]["summary"]["dps"], self.manual[k]["summary"]["dps"], k)
            self.assertEqual(self.plain[k]["summary"]["life"], p["Life"], k)

    def test_reference_audit_keeps_the_level(self):
        """W1: the audit's defence values and passive checks match the manual-level build."""
        for k in ("rf", "de"):
            d, m = self.ref[k]["audit"]["defence"], stats(self.manual[k])
            for key in ("Life", "TotalEHP", "PhysicalMaximumHitTaken", "ChaosMaximumHitTaken"):
                self.assertEqual(d.get(key), m.get(key), (k, key))
            self.assertFalse([w for w in self.ref[k]["warnings"] if "did not return" in w], k)
            self.assertEqual(self.ref[k]["level"], self.level[k])
        auto_nodes = {n["name"]: n["without"] for n in self.ref["de"]["audit"]["leaf_nodes"]}
        manual_nodes = {n["name"]: n["without"] for n in self.ref_manual_de["audit"]["leaf_nodes"]}
        self.assertEqual(json.dumps(auto_nodes, sort_keys=True), json.dumps(manual_nodes, sort_keys=True))

    def test_saved_stats_difference_traced_to_the_level(self):
        """W5: the export's stats were saved at level 90; recalculating at the saved level reproduces them."""
        sv = self.plain["rf"]["saved_vs_recalculated"]
        self.assertTrue(sv["differing"])
        self.assertIn("character level", sv.get("explained_by") or "")
        self.assertTrue(any("level 90" in t["change"] and not t["still_differing"] for t in sv["tested"]))
        self.assertEqual(self.plain["rf"]["summary"]["dps"], self.manual["rf"]["summary"]["dps"])   # state restored


@unittest.skipUnless(READY, "PoB not set up")
class SavedStats(unittest.TestCase):
    """W5 with the boss setting, an unexplained difference and an exact match."""

    @classmethod
    def setUpClass(cls):
        s = spec("burning_arrow_vigour_deadeye")
        xml = built_xml(s)
        boss_input = r'\s*<Input\b[^>]*\bname="enemyIsBoss"[^>]*/>'      # PoB's attribute order varies
        s["config"] = dict(s.get("config") or {}, enemyIsBoss="Boss")
        saved_at_boss = built_xml(s)                                       # stats saved with "Boss"
        assert re.search(r'<Input\b(?=[^>]*\bname="enemyIsBoss")(?=[^>]*\bstring="Boss")[^>]*/>', saved_at_boss)
        cls.unset = re.sub(boss_input, "", saved_at_boss)                  # the setting dropped, stats kept
        assert not re.search(boss_input, cls.unset)

        def triple(m):
            tag = m.group(0)
            return re.sub(r'\bvalue="([\d.]+)"', lambda v: 'value="%s"' % (float(v.group(1)) * 1.5), tag)
        cls.tampered = re.sub(r'<PlayerStat\b[^>]*\bstat="FullDPS"[^>]*/>', triple, xml)
        assert cls.tampered != xml
        cls.r = dict(zip(("same", "unset", "tampered"), fastpath.run_many(
            [{"mode": "check", "xml": x} for x in (xml, cls.unset, cls.tampered)], workers=2)))

    def test_matching_export(self):
        sv = self.r["same"]["saved_vs_recalculated"]
        self.assertEqual(sv["differing"], [])
        self.assertGreater(len(sv["stats"]), 10)

    def test_unset_boss_setting_found(self):
        sv = self.r["unset"]["saved_vs_recalculated"]
        self.assertIn("FullDPS", sv["differing"])
        self.assertIn("enemyIsBoss = Boss", sv.get("explained_by") or "")

    def test_unexplained_difference_is_not_given_a_cause(self):
        sv = self.r["tampered"]["saved_vs_recalculated"]
        self.assertEqual(sv["differing"], ["FullDPS"])
        self.assertIsNone(sv.get("explained_by"))
        self.assertIsNone(sv.get("partly_explained_by"))
        self.assertIn("not testable here", sv["unexplained"])
        self.assertTrue(sv["tested"])                    # boss settings were tried, none matched


@unittest.skipUnless(READY, "PoB not set up")
class ActiveGearSet(unittest.TestCase):
    """W4: a second gear set with a different number of flasks; only the active set is counted or changed."""

    @classmethod
    def setUpClass(cls):
        xml = built_xml(spec("burning_arrow_vigour_deadeye"))
        m = re.search(r'<ItemSet\b[^>]*id="1"[^>]*>(.*?)</ItemSet>', xml, re.S)
        off = re.sub(r'<Slot [^>]*name="Flask [1-5]"[^>]*/>', lambda t: t.group(0).replace(' active="true"', ""), m.group(1))
        # set 2: same gear, flasks 4 and 5 removed, all off
        two = re.sub(r'<Slot [^>]*name="Flask [45]"[^>]*/>', lambda t: re.sub(r'itemId="\d+"', 'itemId="0"', t.group(0)), off)
        sets = ('<ItemSet id="1" useSecondWeaponSet="false">%s</ItemSet>' % off
                + '<ItemSet id="2" title="Second" useSecondWeaponSet="false">%s</ItemSet>' % two)
        base = xml[:m.start()] + sets + xml[m.end():]
        cls.active1 = base
        cls.active2 = re.sub(r'(<Items\b[^>]*\bactiveItemSet=")1"', r'\g<1>2"', base)
        cls.sets = staticmethod(lambda x: re.findall(r'<ItemSet\b[^>]*>.*?</ItemSet>', x, re.S))

    def test_counts_follow_the_active_set(self):
        self.assertEqual(len(pob.inactive_equipped_flasks(self.active1)), 5)
        self.assertEqual(len(pob.inactive_equipped_flasks(self.active2)), 3)

    def test_adjustment_changes_only_the_active_set(self):
        for xml, active in ((self.active1, 0), (self.active2, 1)):
            before, after = self.sets(xml), self.sets(pob.flasks_on(xml))
            self.assertEqual(after[1 - active], before[1 - active])           # inactive set byte-identical
            self.assertNotEqual(after[active], before[active])
            self.assertEqual(len(pob.inactive_equipped_flasks(pob.flasks_on(xml))), 0)

    def test_reference_reports_active_set_flasks(self):
        rc, out = cli_reference(self.active2, adjust="flasks-on")
        self.assertIn("3 equipped flask(s) of the active gear set", out["adjusted"]["adjustments"][0])
        self.assertNotEqual(out["adjusted"]["facts"]["dps"], out["reference"]["facts"]["dps"])


@unittest.skipUnless(READY, "PoB not set up")
class CraftedRanges(unittest.TestCase):
    """W3: rolls the item crafter stores as ranges are resolved like PoB does, then checked as before."""

    @classmethod
    def setUpClass(cls):
        rf = spec("rf_chieftain")

        def item(base, lines):
            return "Rarity: RARE\nTest Piece\n%s\nCrafted: true\nItem Level: 84\n%s" % (base, "\n".join(lines))
        cls.cases = {
            "legal_craft": ("Amulet", item("Onyx Amulet", ["+70 to maximum Life", "+40% to Cold Resistance",
                                                            "{crafted}{range:0.5}(31-36)% increased Damage while Leeching"])),
            "legal_hybrid_craft": ("Belt", item("Heavy Belt", ["+70 to maximum Life", "+40% to Cold Resistance",
                                                               "{crafted}{range:0.5}+(13-16)% to Fire and Cold Resistances"])),
            "craft_on_wrong_base": ("Ring 1", item("Ruby Ring", ["+70 to maximum Life", "+40% to Cold Resistance",
                                                                 "{crafted}{range:0.5}(31-36)% increased Damage while Leeching"])),
            "legal_ranged_affix": ("Ring 1", item("Ruby Ring", ["{range:0.5}+(70-79) to maximum Life",
                                                                "{range:1}+(42-45)% to Cold Resistance"])),
            "ranged_affix_out_of_range": ("Ring 1", item("Ruby Ring", ["{range:0.5}+(150-160) to maximum Life",
                                                                       "+40% to Cold Resistance"])),
        }
        reqs = []
        for slot, text in cls.cases.values():
            xml = built_xml(pob.apply_variant(rf, {"items": [{"slot": slot, "text": text}]}))
            reqs.append({"mode": "check", "xml": xml})
        cls.r = {k: next(i for i in r["items"] if i["slot"] == cls.cases[k][0])
                 for k, r in zip(cls.cases, fastpath.run_many(reqs, workers=2))}

    def test_legal_crafts_are_recognised(self):
        for k in ("legal_craft", "legal_hybrid_craft"):
            self.assertEqual(self.r[k]["problems"], [], k)
            self.assertIn("1 bench craft", self.r[k]["crafting_signals"], k)
        self.assertEqual(self.r["legal_ranged_affix"]["problems"], [])

    def test_illegal_lines_still_rejected(self):
        self.assertTrue(any("not a bench craft" in p for p in self.r["craft_on_wrong_base"]["problems"]))
        self.assertTrue(any("outside the rollable range" in p for p in self.r["ranged_affix_out_of_range"]["problems"]))


@unittest.skipUnless(READY, "PoB not set up")
class Messages(unittest.TestCase):
    """W2: driver warnings appear once each in the reference facts and in the printed report."""

    def test_warnings_reach_the_report(self):
        xml = auto_level(built_xml(spec("rf_chieftain")))
        xml = re.sub(r'(<Items\b[^>]*>)', r'\1<Item id="900">Rarity: RARE\nSpare\nRuby Ring\n+10 to maximum Life</Item>'
                     r'<Item id="901">Rarity: RARE\nSpare\nRuby Ring\n+10 to maximum Life</Item>', xml, count=1)
        resp = fastpath.run_one({"mode": "check", "xml": xml, "audit": True, "reference": True})
        n = ra.normalise(resp, qa.resolve_profile({"content": "mapping"}, {}, resp["level"]))
        msgs = n["facts"]["messages"]
        texts = [m["message"] for m in msgs]
        self.assertEqual(len(texts), len(set(texts)))                          # deduplicated
        for w in resp["warnings"]:
            self.assertIn(w, texts)                                             # nothing dropped
        spare = [m for m in msgs if m["message"] == "Item 'Spare' is not equipped in any slot"]
        self.assertEqual((spare[0]["count"], spare[0]["severity"]), (2, "warning"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ra.print_summary(n, "T")
        self.assertIn("Auto level sets the character to level", buf.getvalue())
        self.assertIn("(x2)", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
