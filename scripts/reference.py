"""Reference-build analysis: a normalised, PoB-calculated summary of an imported build, and an
optional side-by-side comparison with a generated build, with a comparability check.

Everything under "facts" comes from Path of Building (driver check/build with audit + reference
measurements). "signals" are simple counts for the model to interpret; they are not PoB numbers
and not prices. Nothing here judges which build is better.
"""
import audit as qa

CONDITIONAL_CATEGORIES = ("charges", "kill condition", "enemy condition", "buff", "player condition", "stacks/uptime", "other")


def _f(v):
    if v is None:
        return "-"
    v = float(v)
    if abs(v) >= 1e6:
        return "%.2fM" % (v / 1e6)
    if abs(v) >= 1e4:
        return "%.1fk" % (v / 1e3)
    return "%.0f" % v


def driver_messages(resp):
    """Every driver warning and note, plus PoB's own non-blocking warnings, deduplicated in order.
    Errors are reported separately as validity problems."""
    out, index = [], {}
    for severity, source, key in (("warning", "driver", "warnings"), ("warning", "PoB", "pob_warnings"), ("note", "driver", "notes")):
        for msg in resp.get(key) or []:
            k = (severity, msg)
            if k in index:
                index[k]["count"] += 1
            else:
                index[k] = {"severity": severity, "source": source, "message": msg, "count": 1}
                out.append(index[k])
    return out


def normalise(resp, profile):
    """Turn a driver response (audit + reference flags) into facts and signals."""
    a = resp.get("audit") or {}
    stats_def = (resp.get("stats") or {}).get("defence") or {}
    # the audit's defence list when it ran; PoB's standard stats (always calculated) otherwise
    d = a.get("defence") or stats_def
    tree = resp.get("tree") or {}
    base = a.get("baseline") or resp.get("summary") or {}
    floor = a.get("floor_no_flasks_no_conditional_config")
    stats = (resp.get("stats") or {}).get("offence") or {}
    main = next((g for g in resp.get("skills") or [] if g.get("main")), None)
    ev = qa.evaluate(resp, profile) if a else []
    line = lambda area, start: next((e[2] for e in ev if e[0] == area and e[2].startswith(start)), None)
    if a and resp.get("audit_on_invalid_build"):
        scope = "measured on a build PoB marks INVALID (numbers are PoB's for the build as given)"
    elif a:
        scope = "full"
    else:
        scope = "audit unavailable: %s" % (resp.get("audit_skipped") or next(
            (w for w in resp.get("warnings") or [] if w.startswith("Audit failed")), "not run"))
    unavailable = []
    if not a:
        unavailable.append("config, flask, skill-group, support, passive and unique contributions; floor DPS; recovery "
                           "and layer lines (they need the audit: %s)" % scope)
    if a and not a.get("main_supports") and not (resp.get("summary") or {}).get("main_skill_disabled"):
        unavailable.append("support contributions (no enabled supports in the main group)")
    if (resp.get("summary") or {}).get("main_skill_disabled"):
        unavailable.append("damage (PoB disables the main skill: see validity problems)")
    for t in ("Physical", "Fire", "Cold", "Lightning", "Chaos"):
        if d.get(t + "MaximumHitTaken") is None and not (t == "Chaos" and a.get("chaos_immune")):
            unavailable.append("%s max hit (not reported by PoB for this build)" % t)
    facts = {
        "valid": resp.get("valid"), "errors": resp.get("errors") or [],
        "validity_problems": resp.get("errors") or [], "analysis_scope": scope, "unavailable": unavailable,
        "defence_source": "audit" if a.get("defence") else "PoB standard stats",
        "pob_version": resp.get("pob_version"), "tree_version": tree.get("tree_version"),
        "latest_tree_version": resp.get("latest_tree_version"),
        "level": resp.get("level"), "class": tree.get("class"), "ascendancy": tree.get("ascendancy"),
        "bloodline": tree.get("bloodline"), "main_skill": resp.get("main_skill"),
        "main_group": [g.get("name") for g in (main or {}).get("gems", [])], "main_links": (main or {}).get("link_size"),
        "points_used": tree.get("points_used"), "points_available": tree.get("points_available_at_level"),
        "keystones": tree.get("keystones") or [], "notables": len(tree.get("notables") or []),
        "masteries": tree.get("masteries") or [],
        "dps": base.get("dps"), "ehp": base.get("ehp"),
        "floor_dps": (floor or {}).get("dps", base.get("dps")), "floor_ehp": (floor or {}).get("ehp", base.get("ehp")),
        "floor_dps_pct": (floor or {}).get("dps_pct", 0.0), "floor_ehp_pct": (floor or {}).get("ehp_pct", 0.0),
        "damage_split": {k: stats.get(k) for k in ("TotalDPS", "TotalDot", "IgniteDPS", "BleedDPS", "PoisonDPS",
                                                     "CombinedDPS", "FullDPS") if stats.get(k)},
        "enemy_is_boss": a.get("enemy_is_boss"),
        "conditional_config": [{"var": c["var"], "value": c["value"], "category": c["category"],
                                "dps_pct": c["without"].get("dps_pct"), "ehp_pct": c["without"].get("ehp_pct"),
                                "evidence": (c.get("evidence") or {}).get("status"),
                                "sustaining_source": (c.get("evidence") or {}).get("sustaining_source"),
                                "triggers": sorted({e.get("trigger") for e in (c.get("evidence") or {}).get("evidence") or []
                                                    if e.get("trigger")}),
                                "stack": c.get("stack") and {"value": c["stack"]["value"], "at_pob_cap": c["stack"].get("at_pob_cap"),
                                                             "half": c["stack"]["half"],
                                                             "dps_pct_at_half": (c["stack"].get("at_half") or {}).get("dps_pct")}}
                               for c in a.get("config") or [] if c.get("without")],
        "conditional_levels": line("config", "Conditional performance"),
        "random_element": line("config", (resp.get("main_skill_config") or {}).get("name", "?") + " picks its element"),
        "flasks": [{"name": f["name"], "dps_pct": f["without"].get("dps_pct"), "ehp_pct": f["without"].get("ehp_pct")}
                   for f in a.get("flasks") or []],
        "life": d.get("Life"), "es": d.get("EnergyShield"), "mana": d.get("Mana"),
        "res": {t: d.get(t + "Resist") for t in ("Fire", "Cold", "Lightning", "Chaos")},
        "res_over_cap": {t: d.get(t + "ResistOver75") for t in ("Fire", "Cold", "Lightning")},
        "chaos_immune": bool(a.get("chaos_immune")),
        "max_hit": {t: d.get(t + "MaximumHitTaken") for t in ("Physical", "Fire", "Cold", "Lightning", "Chaos")},
        "defence_layers": line("defence", "Defensive layers"),
        "recovery": line("recovery", "Life recovery") or line("recovery", "Energy shield recovery"),
        "sustain": resp.get("sustain") or [],
        "supports": [{"gem": s["gem"], "dps_pct": s["without"].get("dps_pct"), "ehp_pct": s["without"].get("ehp_pct")}
                     for s in a.get("main_supports") or []],
        "leaf_nodes": [{"name": n["name"], "type": n["type"], "ascendancy": n.get("ascendancy"),
                        "dps_pct": n["without"].get("dps_pct"), "ehp_pct": n["without"].get("ehp_pct"),
                        "life": n["without"].get("life")} for n in a.get("leaf_nodes") or []],
        "uniques": [{"slot": u["slot"], "name": u["name"], "dps_pct": u["without"].get("dps_pct"),
                     "ehp_pct": u["without"].get("ehp_pct"), "main_skill_disabled": bool(u["without"].get("main_skill_disabled"))}
                    for u in a.get("uniques") or []],
        "main_skill_config": resp.get("main_skill_config"),
        "level_info": resp.get("level_info"),
        "saved_vs_recalculated": resp.get("saved_vs_recalculated"),
        "messages": driver_messages(resp),
        "skill_config_lines": [e[2] for e in ev if e[0] == "config" and (" stages" in e[2] or "skill part" in e[2])],
        "audit_summary": qa.summary(ev, resp.get("errors") or []) if ev else None,
        "audit_findings": [{"area": e[0], "severity": e[3], "message": e[2]} for e in ev
                           if e[3] in ("critical", "material", "advisory")],
    }
    items = resp.get("items") or []
    rares = [i for i in items if (i.get("rarity") or "").upper() in ("RARE", "MAGIC") and not str(i.get("slot", "")).startswith("Flask")]
    signals = {
        "_note": "counts for the model to interpret; not PoB numbers and not prices",
        "uniques": len(facts["uniques"]),
        "build_enabling_uniques": [u["name"] for u in facts["uniques"] if u["main_skill_disabled"]],
        "rares": len(rares),
        "top_tier_affixes_total": sum(i.get("top_tier_affixes") or 0 for i in rares),
        "rares_over_3_top_tier": [i.get("name") for i in rares if (i.get("top_tier_affixes") or 0) > 3],
        "crafting_signals": sorted({sig for i in items for sig in i.get("crafting_signals") or []}),
        "unspent_points": (facts["points_available"] or 0) - (facts["points_used"] or 0)
        if facts["points_available"] is not None else None,
    }
    return {"facts": facts, "signals": signals, "profile": {k: profile[k] for k in ("investment", "content", "mode", "level")},
            "profile_assumed": profile.get("assumed") or []}


def comparability(ref, other):
    """Reasons the two builds can't be compared like for like. Returns (verdict, reasons)."""
    rf, of = ref["facts"], other["facts"]
    hard, soft = [], []
    if rf["pob_version"] != of["pob_version"] or rf["tree_version"] != of["tree_version"]:
        hard.append("PoB %s/tree %s vs PoB %s/tree %s" % (rf["pob_version"], rf["tree_version"], of["pob_version"],
                                                         of["tree_version"]))
    if rf["level"] and of["level"] and abs(rf["level"] - of["level"]) >= 3:
        hard.append("level %s vs %s" % (rf["level"], of["level"]))
    if (rf["main_skill"] or "").lower() != (of["main_skill"] or "").lower():
        hard.append("different main skill (%s vs %s): compare design choices, not numbers" % (rf["main_skill"], of["main_skill"]))
    for k in ("content", "investment", "mode"):
        if ref["profile"][k] != other["profile"][k]:
            hard.append("%s %s vs %s" % (k, ref["profile"][k], other["profile"][k]))
    if rf["enemy_is_boss"] != of["enemy_is_boss"]:
        hard.append("enemyIsBoss %s vs %s" % (rf["enemy_is_boss"], of["enemy_is_boss"]))
    rc = {c["var"] for c in rf["conditional_config"]}
    oc = {c["var"] for c in of["conditional_config"]}
    if rc != oc:
        soft.append("conditional config differs (only reference: %s; only other: %s): compare floor DPS, not configured DPS"
                    % (", ".join(sorted(rc - oc)) or "-", ", ".join(sorted(oc - rc)) or "-"))
    if bool(rf["flasks"]) != bool(of["flasks"]):
        soft.append("only one build has active flasks: compare floor numbers")
    for name in ("profile_assumed",):
        if ref[name]:
            soft.append("reference %s not stated, assumed %s" % ("/".join(ref[name]), ", ".join(
                "%s=%s" % (k, ref["profile"][k]) for k in ref[name])))
    rs, os_ = ref["signals"], other["signals"]
    if len(rs["build_enabling_uniques"]) != len(os_["build_enabling_uniques"]) or \
            abs(rs["top_tier_affixes_total"] - os_["top_tier_affixes_total"]) >= 6:
        soft.append("gear signals differ (build-enabling uniques %d vs %d, top-tier affixes %d vs %d): budgets may differ"
                    % (len(rs["build_enabling_uniques"]), len(os_["build_enabling_uniques"]),
                       rs["top_tier_affixes_total"], os_["top_tier_affixes_total"]))
    verdict = "not directly comparable" if hard else ("comparable with caveats" if soft else "comparable")
    return verdict, hard + soft


def print_summary(n, title):
    f, s = n["facts"], n["signals"]
    p = print
    p("=" * 70)
    p("%s (PoB-calculated unless marked)" % title)
    p("=" * 70)
    if not f["valid"]:
        p("VALIDITY: INVALID in PoB as given:")
        for e in f["validity_problems"][:8]:
            p("  - " + e)
    p("Analysis scope: %s" % f["analysis_scope"])
    li = f.get("level_info") or {}
    p("PoB %s, tree %s (latest %s) | level %s%s %s / %s%s | profile %s/%s/%s%s" % (
        f["pob_version"], f["tree_version"], f["latest_tree_version"], f["level"],
        (" (PoB Auto level; file says %s)" % li["saved"]) if li.get("auto") and li.get("saved") not in (None, li.get("used"))
        else (" (PoB Auto level)" if li.get("auto") else ""), f["class"], f["ascendancy"],
        (" / " + f["bloodline"]) if f["bloodline"] else "", n["profile"]["investment"], n["profile"]["content"],
        n["profile"]["mode"], (" (assumed: %s)" % ", ".join(n["profile_assumed"])) if n["profile_assumed"] else ""))
    p("Main skill: %s | %s-link: %s" % (f["main_skill"], f["main_links"] or "?", ", ".join(f["main_group"])))
    for line in f.get("skill_config_lines") or []:
        p("  " + line)
    p("DPS configured %s | floor %s (%+.1f%%, flasks off, conditional config reset) | enemyIsBoss %s" % (
        _f(f["dps"]), _f(f["floor_dps"]), f["floor_dps_pct"] or 0, f["enemy_is_boss"]))
    if f["damage_split"]:
        p("Damage split: " + ", ".join("%s %s" % (k, _f(v)) for k, v in f["damage_split"].items()))
    if f["conditional_config"]:
        def tag(c):
            if c.get("evidence") is None:
                return ""
            t = {"source found": "source: " + ", ".join(c.get("triggers") or []) + ("" if c.get("sustaining_source") else
                                                                                       "; none sustaining"),
                 "no source found": "no source found", "not assessed": "source not assessed"}.get(c["evidence"], c["evidence"])
            st = c.get("stack")
            if st:
                t += "; %s%s, at %s %+.1f%% DPS" % (st["value"], " = PoB cap" if st.get("at_pob_cap") else "", st["half"],
                                                   st.get("dps_pct_at_half") or 0)
            return " [%s]" % t
        p("Conditional config (each removed alone; uptime not estimated): " + "; ".join(
            "%s=%s (%s) %+.1f%% DPS%s%s" % (c["var"], c["value"], c["category"], c["dps_pct"] or 0,
                                           (" %+.1f%% EHP" % c["ehp_pct"]) if c.get("ehp_pct") else "", tag(c))
            for c in f["conditional_config"]))
    if f.get("conditional_levels"):
        p(f["conditional_levels"])
    if f.get("random_element"):
        p(f["random_element"])
    if f["flasks"]:
        shown = [x for x in f["flasks"] if abs(x["dps_pct"] or 0) >= 0.5 or abs(x["ehp_pct"] or 0) >= 0.5]
        rest = len(f["flasks"]) - len(shown)
        p("Flasks (each off): " + ("; ".join("%s %+.1f%% DPS %+.1f%% EHP" % (x["name"], x["dps_pct"] or 0, x["ehp_pct"] or 0)
                                            for x in shown) or "no measurable effect")
          + ("; %d more with no measurable effect" % rest if shown and rest else ""))
    res = f["res"]
    p("Life %s, ES %s | res F/C/L %s/%s/%s (over cap %s/%s/%s), chaos %s | EHP %s" % (
        _f(f["life"]), _f(f["es"]), res["Fire"], res["Cold"], res["Lightning"], f["res_over_cap"]["Fire"],
        f["res_over_cap"]["Cold"], f["res_over_cap"]["Lightning"], "immune" if f["chaos_immune"] else res["Chaos"], _f(f["ehp"])))
    p("Max hit: " + ", ".join("%s %s" % (k, "immune" if (k == "Chaos" and f["chaos_immune"]) else _f(v))
                              for k, v in f["max_hit"].items()))
    if f["defence_layers"]:
        p(f["defence_layers"])
    if f["recovery"]:
        p(f["recovery"])
    for su in f["sustain"]:
        p("Main-skill cost: %s %s/s vs %s/s recovery%s" % (su.get("cost_per_second"), su.get("pool"), su.get("recovery_per_second"),
                                                        (", empties in %ss" % su["seconds_until_empty"]) if su.get("seconds_until_empty") else ""))
    if f["supports"]:
        p("Supports (DPS lost without each): " + ", ".join("%s %+.1f%%" % (x["gem"], x["dps_pct"] or 0)
                                                         for x in sorted(f["supports"], key=lambda x: x["dps_pct"] or 0)))
    if f["leaf_nodes"]:
        top = f["leaf_nodes"][:10]
        p("Leaf passives removed alone (sensitivity, travel kept): " + ", ".join(
            "%s%s %+.1f%% DPS/%+.1f%% EHP" % (x["name"], " [asc]" if x["ascendancy"] else "", x["dps_pct"] or 0, x["ehp_pct"] or 0)
            for x in top))
    if f["keystones"]:
        p("Keystones: " + ", ".join(f["keystones"]))
    if f["uniques"]:
        p("Uniques (slot emptied): " + "; ".join("%s%s %+.0f%% DPS %+.0f%% EHP" % (
            u["name"], " [BUILD-ENABLING]" if u["main_skill_disabled"] else "", u["dps_pct"] or 0, u["ehp_pct"] or 0)
            for u in f["uniques"]))
    if f["audit_summary"]:
        sm = f["audit_summary"]
        p("Audit: critical %d | material %d | advisory %d" % (sm["critical"], sm["material"], sm["advisory"]))
        for x in f["audit_findings"]:
            if x["severity"] != "advisory":
                p("  %s [%s] %s" % (x["severity"].upper(), x["area"], x["message"]))
    if f["unavailable"]:
        p("Unavailable: " + "; ".join(f["unavailable"]))
    sv = f.get("saved_vs_recalculated")
    if sv and sv.get("stats"):
        if sv["differing"]:
            p("Saved export stats vs PoB's recalculation (differences over %s%%):" % sv["tolerance_pct"])
            for k in sv["differing"]:
                e = sv["stats"][k]
                p("  %s: saved %s, recalculated %s (%+.1f%%)" % (k, _f(e["saved"]), _f(e["recalculated"]), e["diff_pct"]))
            for t in sv.get("tested") or []:
                if not t["still_differing"]:
                    p("  tested %s -> matches every saved stat" % t["change"])
                else:
                    p("  tested %s -> matches %s%s" % (t["change"], ", ".join(t.get("resolves") or []) or "none of the differing stats",
                                                    ("; but then differs: " + ", ".join(t["newly_differing"])) if t.get("newly_differing") else ""))
            if sv.get("explained_by"):
                p("  Explained by: " + sv["explained_by"])
            else:
                pe = sv.get("partly_explained_by")
                if pe:
                    p("  Partly explained by: %s (matches %s%s)" % (pe["change"], ", ".join(pe["resolves"]),
                                                                  ("; the other stats then differ: " + ", ".join(pe["newly_differing"]))
                                                                  if pe.get("newly_differing") else ""))
                p("  Not fully explained: " + sv["unexplained"])
        else:
            p("Saved export stats match PoB's recalculation (%d stats, within %s%%)." % (len(sv["stats"]), sv["tolerance_pct"]))
    msgs = [m for m in f.get("messages") or [] if not (sv and m["message"].startswith("Saved export stats differ"))]
    if msgs:
        p("Driver and PoB messages (%d):" % len(msgs))
        for m in msgs:
            p("  %s [%s] %s%s" % (m["severity"].upper(), m["source"], m["message"], (" (x%d)" % m["count"]) if m["count"] > 1 else ""))
    p("Signals (counts for interpretation; not PoB numbers, not prices): uniques %d (build-enabling: %s), rares %d, "
      "top-tier affixes %d%s, crafting: %s, unspent points %s" % (
          s["uniques"], ", ".join(s["build_enabling_uniques"]) or "none", s["rares"], s["top_tier_affixes_total"],
          (" (rares with >3: %s)" % ", ".join(s["rares_over_3_top_tier"])) if s["rares_over_3_top_tier"] else "",
          ", ".join(s["crafting_signals"]) or "none", s["unspent_points"]))


def print_comparison(ref, other, other_name):
    verdict, reasons = comparability(ref, other)
    print("\n" + "=" * 70)
    print("COMPARISON: reference vs %s -> %s" % (other_name, verdict.upper()))
    print("=" * 70)
    for r in reasons:
        print("  - " + r)
    rf, of = ref["facts"], other["facts"]
    rows = [("valid", rf["valid"], of["valid"]), ("level", rf["level"], of["level"]),
            ("main skill", rf["main_skill"], of["main_skill"]),
            ("DPS configured", _f(rf["dps"]), _f(of["dps"])), ("DPS floor", _f(rf["floor_dps"]), _f(of["floor_dps"])),
            ("life", _f(rf["life"]), _f(of["life"])), ("ES", _f(rf["es"]), _f(of["es"])), ("EHP", _f(rf["ehp"]), _f(of["ehp"])),
            ("lowest ele res", min(v for k, v in rf["res"].items() if k != "Chaos" and v is not None),
             min(v for k, v in of["res"].items() if k != "Chaos" and v is not None)),
            ("chaos res", "immune" if rf["chaos_immune"] else rf["res"]["Chaos"], "immune" if of["chaos_immune"] else of["res"]["Chaos"])]
    for t in ("Physical", "Fire", "Cold", "Lightning", "Chaos"):
        rows.append(("max hit " + t, _f(rf["max_hit"][t]), _f(of["max_hit"][t])))
    rows += [("points used/avail", "%s/%s" % (rf["points_used"], rf["points_available"]),
              "%s/%s" % (of["points_used"], of["points_available"])),
             ("audit crit/mat/adv", "%d/%d/%d" % tuple((rf["audit_summary"] or {}).get(k, 0) for k in ("critical", "material", "advisory")),
              "%d/%d/%d" % tuple((of["audit_summary"] or {}).get(k, 0) for k in ("critical", "material", "advisory")))]
    print("  %-20s %-22s %-22s" % ("", "reference", other_name[:22]))
    for name, x, y in rows:
        print("  %-20s %-22s %-22s" % (name, str(x)[:22], str(y)[:22]))
    print("Numbers are PoB's. A higher number on one row does not make a build better overall; "
          "interpret design differences per references/reference-analysis.md.")
