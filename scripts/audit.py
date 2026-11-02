"""Quality audit for pob.py build/check: applies the thresholds in
references/audit-thresholds.json to the numbers the driver measured with PoB.

Nothing here calculates game mechanics; every number comes from Path of Building
(driver.lua runAudits). This module only compares those numbers with targets and
prints PASS / WARN / REVIEW lines. A WARN never makes a build invalid.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
THRESHOLDS = os.path.join(HERE, "..", "references", "audit-thresholds.json")

INVESTMENTS = ("league_starter", "budget", "medium", "high")
CONTENTS = ("campaign", "mapping", "bossing", "uber")
MODES = ("softcore", "hardcore")


def load_thresholds():
    with open(THRESHOLDS, encoding="utf-8") as f:
        return json.load(f)


def resolve_profile(spec_profile, overrides, level):
    """Merge spec.profile with CLI overrides; record which values were assumed."""
    p = dict(spec_profile or {})
    for k, v in (overrides or {}).items():
        if v is not None:
            p[k] = v
    assumed = []
    defaults = {"investment": "medium", "content": "mapping" if (level or 90) >= 70 else "campaign", "mode": "softcore"}
    allowed = {"investment": INVESTMENTS, "content": CONTENTS, "mode": MODES}
    problems = []
    for k, d in defaults.items():
        if not p.get(k):
            p[k] = d
            assumed.append(k)
        elif p[k] not in allowed[k]:
            problems.append("profile.%s '%s' is not one of %s; using %s" % (k, p[k], "/".join(allowed[k]), d))
            p[k] = d
            assumed.append(k)
    p["assumed"] = assumed
    p["problems"] = problems
    p["level"] = level or 90
    return p


def _scale(th, profile):
    factor = 1.0
    for row in th["level_scaling"]:
        if profile["level"] <= row["max_level"]:
            factor = row["factor"]
            break
    return factor * th["mode_multiplier"].get(profile["mode"], 1.0)


def _content_thresholds(th, profile):
    c = json.loads(json.dumps(th["content"][profile["content"]]))
    for k, v in (profile.get("thresholds") or {}).items():
        if isinstance(v, dict) and isinstance(c.get(k), dict):
            c[k].update(v)
        else:
            c[k] = v
    return c


# Priorities (lower = handle first) and what each severity asks of the model
PRIORITY = {"validity": 1, "main skill": 2, "sustain": 3, "defence": 4, "recovery": 4, "ailments": 4, "config": 5,
            "gear": 6, "slots": 6, "attributes": 6, "skills": 7, "profile": 8}
SEVERITY_TEXT = {
    "critical": "must fix before delivering",
    "material": "fix when a reasonable alternative exists; otherwise explain the trade-off",
    "advisory": "explain or accept; never required to clear",
}


def _fmt(v):
    v = float(v or 0)
    if abs(v) >= 1e6:
        return "%.2fM" % (v / 1e6)
    if abs(v) >= 1e4:
        return "%.1fk" % (v / 1e3)
    return "%.0f" % v


ELEMENTS = ("Fire", "Cold", "Lightning", "Chaos", "Physical")


def _short(text, n=110):
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _evidence_text(c):
    """Evidence lines for one conditional option, as recorded by the driver (no uptime is estimated)."""
    ev = c.get("evidence") or {}
    out = []
    items = ev.get("evidence") or []
    if ev.get("status") == "no source found":
        out.append("No source found in passives, equipped gear or enabled skills.")
    elif ev.get("status") == "not assessed":
        out.append("Source not assessed for this option.")
    elif items:
        shown = sorted(items, key=lambda e: not e.get("sustaining"))[:3]
        out.append("Evidence: " + "; ".join("%s [%s]" % (_short(e["text"]), e.get("trigger") or e["kind"]) for e in shown)
                   + (" (+%d more)" % (len(items) - len(shown)) if len(items) > len(shown) else "") + ".")
        if not ev.get("sustaining_source"):
            out.append("None of these sources can be assumed to keep it up (on kill, while moving, minimum count, or other).")
    for x in ev.get("counter") or []:
        out.append("PoB: %s." % x)
    v = ev.get("pob_values") or {}
    if v.get("charges") is not None:
        out.append("PoB uses %s of max %s charges (minimum %s, duration %ss); charge generation isn't modelled."
                   % (_fmt(v["charges"]), _fmt(v.get("max")), _fmt(v.get("minimum")), _fmt(v.get("duration"))))
    elif v.get("max") is not None:
        out.append("PoB maximum: %s." % _fmt(v["max"]))
    st = c.get("stack")
    if st:
        h = st.get("at_half") or {}
        out.append("Value %s%s; at %s: %+.1f%% DPS%s." % (
            st["value"], " is PoB's cap (doubling it changes nothing)" if st.get("at_pob_cap") else "", st["half"],
            h.get("dps_pct") or 0, (", %+.1f%% EHP" % h["ehp_pct"]) if h.get("ehp_pct") else ""))
    return (" " + " ".join(out)) if out else ""


def _performance_levels(base, audit):
    """The two evidence levels between configured and floor, each a PoB recalculation."""
    if base.get("dps") is None or (audit.get("evidence_supported") is None and audit.get("sustained_sources_only") is None):
        return None
    rows = []
    for key, name in (("evidence_supported", "with conditions that have no source off"),
                      ("sustained_sources_only", "with only conditions that have a sustaining source")):
        sc = audit.get(key)
        if sc is None:
            continue
        w = sc.get("without")
        if w:
            rows.append(("%s (%s)" % (name, ", ".join(sc["turned_off"])), w.get("dps_pct"), w.get("ehp_pct"), w.get("dps"), w.get("ehp")))
        else:
            rows.append(("%s (none turned off)" % name, 0.0, 0.0, base.get("dps"), base.get("ehp")))
    return rows


def _random_element_text(msc, audit, base):
    part = msc["part"]
    dps = {part["name"]: base.get("dps")}
    for o in audit.get("other_parts") or []:
        dps[o["name"]] = (o.get("without") or {}).get("dps")
    area = part.get("area") or []
    is_area = {n: bool(i < len(area) and area[i]) for i, n in enumerate(part["names"])}
    groups = []
    fmt = lambda names: ", ".join("%s %s%s" % (n, _fmt(dps.get(n)), " [area]" if is_area[n] else "") for n in names)
    for el in ELEMENTS:
        names = [n for n in part["names"] if el in n]
        if names:
            groups.append("%s: %s" % (el, fmt(names)))
    rest = [n for n in part["names"] if not any(el in n for el in ELEMENTS)]
    if rest:
        groups.append("other (element not in the part name): " + fmt(rest))
    vals = [dps[n] for n in part["names"] if not is_area[n] and dps.get(n) is not None]
    el_cfg = [c["var"] for c in audit.get("config") or [] if c.get("without") and any(e in c["var"] for e in ELEMENTS)]
    txt = ("%s picks its element per use (PoB skill type RandomElement); the headline DPS is part %d '%s' only. "
           "PoB parts by element: %s" % (msc["name"], part["index"], part["name"], "; ".join(groups) or ", ".join(part["names"])))
    if vals:
        txt += " (parts PoB doesn't flag as area: %s to %s)" % (_fmt(min(vals)), _fmt(max(vals)))
    txt += (". No blended figure is given: how often each element occurs depends on the skill's selection rule, and all "
            "parts are calculated with the same configuration")
    if el_cfg:
        txt += ", including element-dependent settings (%s) that can't be true in the same way for every element" % ", ".join(el_cfg)
    return txt + ". Report the range, or derive a blend from the mechanics and say how."


def evaluate(resp, profile):
    """Return a list of (area, status, message, severity, priority).

    status: PASS | WARN | REVIEW | INFO (as before). severity: critical | material | advisory | pass | info.
    Nothing here changes validity: only PoB errors make a build invalid.
    """
    th = load_thresholds()
    audit = resp.get("audit") or {}
    out = []

    def add(area, status, msg, severity=None):
        if severity is None:
            severity = {"PASS": "pass", "INFO": "info", "REVIEW": "advisory", "WARN": "material"}[status]
        out.append((area, status, msg, severity, PRIORITY.get(area, 9)))

    if not audit:
        add("profile", "INFO", "No audit data (%s)." % (resp.get("audit_skipped") or "run without --no-audit"))
        return out
    for pr in profile.get("problems") or []:
        add("profile", "WARN", pr, "advisory")
    if profile.get("assumed"):
        add("profile", "WARN", "No %s given in spec.profile; assumed %s. State it so thresholds and gear checks fit the build."
            % ("/".join(profile["assumed"]), ", ".join("%s=%s" % (k, profile[k]) for k in profile["assumed"])), "advisory")
    # attribute margins: a thin margin breaks on the next gem level or support swap
    at = resp.get("attributes") or {}
    for a, m in sorted((at.get("thin_margin") or {}).items()):
        info = (at.get("attributes") or {}).get(a) or {}
        add("attributes", "REVIEW", "%s margin is %d (have %d, need %d%s; target %d). Meet it in this order: gem level, amulet base, "
            "a free suffix, a nearby +30 node; never fill points. The ATTRIBUTES section lists the options for this build."
            % (a, m, info.get("have", 0), info.get("need", 0), ", set by " + ", ".join(info.get("set_by") or []) if info.get("set_by") else "",
               at.get("margin_target", 10)), "advisory")
    ct = _content_thresholds(th, profile)
    scale = _scale(th, profile)
    d = audit.get("defence") or {}
    base = audit.get("baseline") or {}
    life, es = d.get("Life") or 0, d.get("EnergyShield") or 0
    es_based = es > life * 2
    pool_name, pool = ("energy shield", es) if es_based else ("life", life)

    # ---- main skill sustain (priority 3) --------------------------------------------
    for sus in resp.get("sustain") or []:
        empty = sus.get("seconds_until_empty")
        add("sustain", "WARN" if empty else "PASS", "Main skill costs %s %s/s vs %s/s recovery%s" % (
            sus.get("cost_per_second"), sus.get("pool"), sus.get("recovery_per_second"),
            (": pool empties in %ss of continuous use" % empty) if empty else ""))

    # ---- configuration honesty (priority 5) ------------------------------------------
    flag = th.get("config_flag_pct", 5)
    boss = audit.get("enemy_is_boss")
    if profile["content"] in ("bossing", "uber") and boss not in ("Pinnacle", "Uber"):
        add("config", "WARN", "Target content is %s but enemyIsBoss = %s; boss DPS is overstated." % (profile["content"], boss))
    if profile["content"] == "uber" and boss != "Uber":
        add("config", "WARN", "Uber content: set enemyIsBoss = Uber (higher boss resistances / less curse effect).")
    for c in audit.get("config") or []:
        w = c.get("without")
        if not w:
            continue
        loss, eloss = -(w.get("dps_pct") or 0), -(w.get("ehp_pct") or 0)
        if loss >= flag or eloss >= flag:
            add("config", "REVIEW", "%s = %s (%s) supplies %.1f%% of DPS%s.%s Confirm the uptime is realistic, or turn it "
                "off." % (c["var"], c["value"], c["category"], loss, (" / %.1f%% of EHP" % eloss) if eloss >= flag else "",
                          _evidence_text(c)))
    floor = audit.get("floor_no_flasks_no_conditional_config")
    levels = _performance_levels(base, audit)
    if levels:
        big = any(abs(x[1] or 0) >= flag or abs(x[2] or 0) >= flag for x in levels)
        add("config", "REVIEW" if big else "INFO", "Conditional performance between configured and the floor (each a PoB "
            "recalculation): " + "; ".join("%s DPS %s (%+.1f%%), EHP %s (%+.1f%%)" % (n, _fmt(d), dp or 0, _fmt(e), ep or 0)
                                            for n, dp, ep, d, e in levels)
            + ". Both still assume full uptime for what they keep; only the floor removes every conditional effect, and it is "
              "a lower bound, not a prediction of normal combat. No level estimates uptime.")
    if base.get("dps") is not None:
        if floor and floor.get("dps") is not None:
            add("config", "INFO", "Configured: DPS %s, EHP %s. Sustained floor (flasks off, conditional config reset): "
                "DPS %s (%+.1f%%), EHP %s (%+.1f%%)." % (_fmt(base["dps"]), _fmt(base.get("ehp")), _fmt(floor["dps"]),
                                                       floor.get("dps_pct") or 0, _fmt(floor.get("ehp")), floor.get("ehp_pct") or 0))
        elif not floor:
            add("config", "INFO", "Configured: DPS %s, EHP %s. No flasks or conditional config, so the sustained floor is the "
                "same." % (_fmt(base["dps"]), _fmt(base.get("ehp"))))
    # skill stages / parts (PoB uses the maximum stage count when none is set)
    msc = audit.get("main_skill_config") or {}
    st = msc.get("stages")
    ms = audit.get("min_stage")
    if st:
        drop = -((ms or {}).get("without", {}).get("dps_pct") or 0)
        low = (ms or {}).get("without", {}).get("dps")
        txt = "%s is calculated at %d of %d stages%s" % (msc["name"], st["used"], st["max"],
                                                       " (set in the build)" if st.get("explicit") else
                                                       " (no stage count set, so PoB assumes the maximum)")
        if low is not None:
            txt += "; at %d stage%s DPS is %s (%+.1f%%)" % (ms["stages"], "" if ms["stages"] == 1 else "s", _fmt(low), -drop)
        if not st.get("explicit") and st["used"] >= st["max"] and drop >= th.get("stage_flag_pct", 10):
            add("config", "WARN", txt + ". State the stage count the build sustains (gem key \"stages\") and why, or "
                "report the lower number.")
        else:
            add("config", "INFO", txt + ".")
    part = msc.get("part")
    if part and part.get("count", 0) > 1 and msc.get("random_element"):
        add("config", "REVIEW", _random_element_text(msc, audit, base))
    if part and part.get("count", 0) > 1:
        others = ["%d '%s' %s" % (o["index"], o["name"], _fmt(o["without"].get("dps"))) for o in audit.get("other_parts") or []]
        add("config", "INFO", "%s skill part %d '%s'%s; other parts: %s." % (
            msc["name"], part["index"], part["name"], " (set in the build)" if part.get("explicit") else " (PoB default)",
            "; ".join(others) or "-"))
    big = ["%s %.1f%% DPS / %.1f%% EHP" % (fl["name"], -(fl["without"].get("dps_pct") or 0), -(fl["without"].get("ehp_pct") or 0))
           for fl in audit.get("flasks") or []
           if -(fl["without"].get("dps_pct") or 0) >= flag or -(fl["without"].get("ehp_pct") or 0) >= flag]
    if big:
        add("config", "REVIEW", "PoB counts active flasks as always on. Worth it: %s. Check charge sustain (flask "
            "mods, charge-gain nodes) or present these numbers as flask-up burst." % "; ".join(big))

    # ---- essential defences (priority 4) ------------------------------------------------
    lr = th["layer_rules"]
    guard_abs = audit.get("guard_absorb") or d.get("sharedGuardAbsorb") or 0
    guards = audit.get("guards") or []
    guard_real = bool(pool and guard_abs and guard_abs / pool * 100 >= lr["guard_pool_pct"])
    # a guard is temporary protection: judge max hit with it down, show it up as well
    down = {}
    if guard_real:
        for g in guards:
            for k, v in ((g.get("without") or {}).get("max_hit") or {}).items():
                down[k] = min(down.get(k, v), v)
    crit_frac = th.get("critical_max_hit_fraction", 0.5)
    hits = []
    for dtype, target in ct["max_hit"].items():
        v = down.get(dtype, d.get(dtype + "MaximumHitTaken"))
        if v is not None:
            hits.append((v / (target * scale) if target else 9e9, dtype, v, target * scale))
    if hits:
        hits.sort()
        ratio, dtype, v, t = hits[0]
        listing = ", ".join("%s %s" % (h[1][:4], _fmt(h[2])) for h in sorted(hits, key=lambda h: h[1]))
        if down:
            up = ", ".join("%s %s" % (k[:4], _fmt(d.get(k + "MaximumHitTaken"))) for k in sorted(down))
            msg = ("Max hit with the guard down (PoB): %s (guard up: %s). Weakest relative to target: %s %s vs %s for "
                   "%s %s lvl %d" % (listing, up, dtype, _fmt(v), _fmt(t), profile["mode"], profile["content"], profile["level"]))
        else:
            msg = ("Max hit (PoB): %s. Weakest relative to target: %s %s vs %s for %s %s lvl %d"
                   % (listing, dtype, _fmt(v), _fmt(t), profile["mode"], profile["content"], profile["level"]))
        below = [h for h in hits if h[0] < 1]
        if ratio < crit_frac:
            add("defence", "WARN", msg + " (under %d%% of target: one-shot risk)" % (crit_frac * 100), "critical")
        elif below:
            add("defence", "WARN", msg + "; below target: %s" % ", ".join(h[1] for h in below))
        else:
            add("defence", "PASS", msg)
    ele = [d.get(k) for k in ("FireResist", "ColdResist", "LightningResist") if d.get(k) is not None]
    if ele:
        m = min(ele)
        endgame = profile["content"] != "campaign"
        add("defence", "PASS" if m >= ct["min_ele_res"] else "WARN", "Lowest elemental resistance %.0f%% (target %d%%)"
            % (m, ct["min_ele_res"]), None if m >= ct["min_ele_res"] else ("critical" if endgame else "material"))
    if audit.get("chaos_immune"):
        add("defence", "PASS", "Immune to chaos damage (PoB), so chaos resistance and chaos max hit don't apply")
    elif d.get("ChaosResist") is not None:
        add("defence", "PASS" if d["ChaosResist"] >= ct["min_chaos_res"] else "WARN",
            "Chaos resistance %.0f%% (target %d%%)" % (d["ChaosResist"], ct["min_chaos_res"]))

    # defensive mechanisms by category; each mechanism counts once, resistances and recovery are not layers
    mitigation, avoidance, buffers, notes = [], [], [], []
    if (d.get("PhysicalDamageReduction") or 0) >= lr["armour_phys_reduction_pct"]:
        mitigation.append("phys damage reduction %.0f%%" % d["PhysicalDamageReduction"])
    over = max(d.get("%sResistOver75" % t) or 0 for t in ("Fire", "Cold", "Lightning"))
    if over >= 1:
        notes.append("max res +%.0f (already inside the resistance and elemental max-hit checks)" % over)
    ev = max(d.get("MeleeEvadeChance") or 0, d.get("ProjectileEvadeChance") or 0)
    if ev >= lr["evade_pct"]:
        avoidance.append("evade %.0f%% (attacks)" % ev)
    ab, sb = d.get("EffectiveBlockChance") or 0, d.get("EffectiveSpellBlockChance") or 0
    if ab >= lr["attack_block_pct"] or sb >= lr["spell_block_pct"]:
        avoidance.append("block %.0f%% attacks / %.0f%% spells" % (ab, sb))
    if (d.get("EffectiveSpellSuppressionChance") or 0) >= lr["spell_suppression_pct"]:
        avoidance.append("spell suppression %.0f%%" % d["EffectiveSpellSuppressionChance"])
    avoid = max(d.get("Avoid%sDamageChance" % t) or 0 for t in ("Physical", "Fire", "Cold", "Lightning", "Chaos"))
    if avoid >= lr["avoid_damage_pct"]:
        avoidance.append("damage avoidance up to %.0f%%" % avoid)
    if (d.get("sharedMindOverMatter") or 0) >= lr["mind_over_matter"]:
        buffers.append("mind over matter %.0f%%" % d["sharedMindOverMatter"])
    temporary = []
    if guard_abs and pool:
        up = [g.get("uptime_pct") for g in guards if g.get("uptime_pct")]
        label = "guard %s (%.0f%% of %s%s)" % (_fmt(guard_abs), guard_abs / pool * 100, pool_name,
                                              (", up at most %.0f%% of the time per PoB" % max(up)) if up else "")
        if guard_real:
            temporary.append(label)
        else:
            notes.append("guard %s absorb is %.1f%% of %s: too small to count" % (_fmt(guard_abs), guard_abs / pool * 100, pool_name))
    aegis = (d.get("sharedAegis") or 0) + (d.get("sharedElementalAegis") or 0)
    if aegis and pool and aegis / pool * 100 >= lr["aegis_pool_pct"]:
        buffers.append("aegis %s" % _fmt(aegis))
    n = len(mitigation) + len(avoidance) + len(buffers)
    msg = "Defensive layers beyond life/res: %d (target %d). Mitigation: %s. Avoidance: %s. Hit buffers: %s." % (
        n, ct["min_layers"], ", ".join(mitigation) or "none", ", ".join(avoidance) or "none", ", ".join(buffers) or "none")
    if temporary:
        msg += " Temporary (not counted as a layer): " + ", ".join(temporary) + "."
    if notes:
        msg += " Not counted: " + "; ".join(notes) + "."
    # the breakdown describes the defensive architecture, so it stays visible even when it passes
    add("defence", "PASS" if n >= ct["min_layers"] else "WARN", msg, None if n < ct["min_layers"] else "info")

    for g in guards:
        if guard_real or not pool:
            break
        setup = "%s %d%s" % (g["guard"], g["guard_level"], (" via %s %d" % (g["trigger"], g["trigger_level"]))
                             if g.get("trigger_level") else " (%s)" % g["trigger"])
        facts = []
        if g.get("trigger_requirement_limit"):
            facts.append("%s %d triggers skills requiring up to level %d (%s up to level %s)" % (
                g["trigger"], g["trigger_level"], g["trigger_requirement_limit"], g["guard"], g.get("best_guard_level_for_trigger")))
        top = g.get("best_guard_level_at_character_level")
        if top:
            need = {k: v for k, v in (g.get("attributes_for_best_level") or {}).items() if v}
            have = g.get("attributes") or {}
            short = ["%d %s (have %d)" % (v, k, have.get(k, 0)) for k, v in need.items() if v > have.get(k, 0)]
            facts.append("%s level %d is allowed at character level %d%s" % (
                g["guard"], top, profile["level"], ("; it needs " + ", ".join(short)) if short else ""))
            ok = g.get("best_guard_level_with_attributes")
            if short:
                facts.append("with current attributes up to %s level %s" % (g["guard"], ok or "none"))
        if g.get("automation_requirement"):
            facts.append("Automation needs level %d%s" % (g["automation_requirement"],
                                                        "" if g.get("automation_attributes_ok") else " and more attributes than the build has"))
        add("defence", "REVIEW", "Guard %s absorbs %s (%.1f%% of %s): little protection. If the sockets are worth it, compare a "
            "higher-level setup (trigger, self-cast or Automation) against the current one, or use the sockets elsewhere. Facts: %s. "
            "A guard is temporary (up at most %.0f%% of the time per PoB), so judge it by max hit with the guard down too."
            % (setup, _fmt(guard_abs), guard_abs / pool * 100, pool_name, "; ".join(facts) or "-", g.get("uptime_pct") or 0))
        break

    # recovery: net of degens, and how much of it needs flasks (from the floor measurement)
    rth = ct["recovery"]
    recoup = max([d.get("LifeRecoup") or 0] + [d.get(t + "LifeRecoup") or 0 for t in ("Physical", "Fire", "Cold", "Lightning", "Chaos")])
    if pool:
        if not es_based:
            gross = d.get("LifeRegenRecovery") or 0
            net = d.get("NetLifeRegen", gross)
            leech = d.get("LifeLeechGainRate") or 0
            f_leech = floor.get("life_leech_rate") if floor else None
            f_net = floor.get("net_life_regen") if floor else None
        else:
            gross = d.get("EnergyShieldRegenRecovery") or 0
            net = d.get("NetEnergyShieldRegen", gross) if "NetEnergyShieldRegen" in d else gross
            leech = d.get("EnergyShieldLeechGainRate") or 0
            recoup = d.get("EnergyShieldRecoup") or 0
            f_leech, f_net = None, (floor.get("net_es_regen") if floor else None)
        net_pct, leech_pct = net / pool * 100, leech / pool * 100
        recharge = d.get("EnergyShieldRecharge") or 0
        recharge_pct = recharge / pool * 100 if es_based else 0
        parts = ["net regen %.1f%%/s%s" % (net_pct, (" (gross %.1f%%, degen %s/s)" % (gross / pool * 100, _fmt(gross - net)))
                                         if gross - net >= 1 else ""),
                 "leech %.1f%%/s while hitting" % leech_pct, "recoup %.0f%%" % recoup]
        ok = [net_pct >= rth["regen_pct"], leech_pct >= rth["leech_pct"], recoup >= rth["recoup_pct"]]
        if es_based and recharge:
            counts = rth.get("es_recharge_counts", False)
            parts.append("recharge %.0f%%/s after %.1fs without damage%s" % (
                recharge_pct, d.get("EnergyShieldRechargeDelay") or 0, "" if counts else " (not counted: hits interrupt it here)"))
            ok.append(bool(counts) and recharge_pct >= rth.get("es_recharge_pct", 20))
        flask_only = []
        if ok[1] and f_leech is not None and f_leech / pool * 100 < rth["leech_pct"]:
            flask_only.append("leech")
        if ok[0] and f_net is not None and f_net / pool * 100 < rth["regen_pct"]:
            flask_only.append("regen")
        msg = "%s recovery: %s (%s content needs one of: regen %.1f%%/s, leech %.1f%%/s, recoup %d%%%s)" % (
            pool_name.capitalize(), ", ".join(parts), profile["content"], rth["regen_pct"], rth["leech_pct"], rth["recoup_pct"],
            (", recharge %d%%/s" % rth.get("es_recharge_pct", 20)) if es_based and rth.get("es_recharge_counts") else "")
        if any(ok) and flask_only and sum(ok) == len(flask_only):
            add("recovery", "WARN", msg + "; only met while a flask is up (%s)" % ", ".join(flask_only))
        else:
            add("recovery", "PASS" if any(ok) else "WARN", msg)

    fz = d.get("FreezeAvoidChance")
    if fz is not None:
        need = ct.get("require_freeze_immunity")
        f_fz = floor.get("freeze_avoid") if floor else None
        txt = "Freeze avoid %.0f%%%s" % (fz, (", %.0f%% without flasks" % f_fz) if f_fz is not None and f_fz < fz else "")
        if need and fz >= 100 and f_fz is not None and f_fz < 100:
            add("ailments", "REVIEW", txt + ": freeze immunity relies on flask uptime; make sure that flask stays up in "
                "boss fights or say so")
        else:
            add("ailments", ("PASS" if fz >= 100 else "WARN") if need else "INFO",
                txt + (" (immunity expected for this content)" if need else ""))
    ail = ["%s %.0f%%" % (k.replace("AvoidChance", ""), d[k]) for k in
           ("ChillAvoidChance", "ShockAvoidChance", "IgniteAvoidChance", "BleedAvoidChance", "PoisonAvoidChance", "StunAvoidChance")
           if d.get(k) is not None]
    if ail:
        add("ailments", "INFO", "Other ailment avoidance: " + ", ".join(ail))

    # breakpoints: a layer that is close to its cap is worth finishing (the cap changes the mechanic)
    supp = d.get("EffectiveSpellSuppressionChance")
    if supp is not None and 0 < supp < 100:
        add("defence", "REVIEW", "Spell suppression %d%% (cap 100%%; at the cap suppressed spell damage is halved, below it the "
            "spell max hit is a roll). %d%% short: a suppression mastery, a notable such as Instinct/Entrench/Quickstep, or a "
            "higher body/boots suffix. The tree fill scores max hit, so it will close a small gap on its own when points allow."
            % (supp, 100 - supp), "advisory" if (supp >= 80 or supp < 20) else "material")   # <20%: incidental, not a layer being built
    for key, cap, name in (("EffectiveBlockChance", 75, "attack block"), ("EffectiveSpellBlockChance", 75, "spell block")):
        v = d.get(key)
        if v is not None and 50 <= v < cap:
            add("defence", "REVIEW", "%s %d%% (cap %d%%): %d%% short of the cap." % (name.capitalize(), v, cap, cap - v), "advisory")

    # cheap, usually-filled slots that are empty: an anoint, eldritch implicits, flask affixes
    cheap = []
    items = resp.get("items") or []
    by_slot = {i.get("slot"): i for i in items}
    am = by_slot.get("Amulet")
    if am and not resp.get("anoint"):
        cheap.append("amulet has no anoint (any notable, allocated for free: `\"anoint\": \"<Notable>\"` on the amulet)")
    for slot in ("Helmet", "Body Armour", "Gloves", "Boots"):
        it = by_slot.get(slot)
        if it and it.get("rarity") in ("RARE", "MAGIC", "NORMAL") and "eldritch implicits" not in (it.get("crafting_signals") or []) \
                and "corrupted" not in (it.get("crafting_signals") or []):
            cheap.append("%s has its base implicit only (Exarch/Eater implicits are standard: `mods \"%s\" --eldritch`)" % (slot, it.get("base") or slot))
    plain = [i.get("slot") for i in items if str(i.get("slot", "")).startswith("Flask") and i.get("rarity") == "NORMAL"]
    if plain:
        cheap.append("plain flask bases in %s: no charge/duration prefix and no immunity suffix (bleed, curse, freeze, shock); "
                     "write them as magic flasks" % ", ".join(sorted(plain)))
    if cheap:
        add("gear", "REVIEW", "Cheap slots left unused: " + "; ".join(cheap) + ".", "advisory")
    if (ct.get("require_stun_avoid_pct") or 0) > (d.get("StunAvoidChance") or 0):
        add("ailments", "WARN", "Stun avoid %.0f%% below target %d%%" % (d.get("StunAvoidChance") or 0, ct["require_stun_avoid_pct"]))

    # ---- gear realism (priority 6) -------------------------------------------------------
    lim = th["investment"][profile["investment"]]["top_tier_affixes_per_rare"]
    budget = profile["investment"] in ("league_starter", "budget")
    char_level = resp.get("level")
    for it in resp.get("items") or []:
        tt = it.get("top_tier_affixes")
        rarity = (it.get("rarity") or "").upper()
        # flasks have their own legality checks; their 2 affixes are not a gear-realism signal
        if tt is None or rarity not in ("RARE", "MAGIC") or it.get("base_type") == "Flask" \
                or str(it.get("slot", "")).startswith("Flask"):
            continue
        sig = it.get("crafting_signals") or []
        if tt > lim:
            lvl_note = ""
            need = max([m.get("item_level") or 0 for m in it.get("top_tier_mods") or []] or [0])
            if char_level and need > char_level:
                lvl_note = "; those tiers need item level up to %d, above character level %d" % (need, char_level)
            add("gear", "WARN", "%s %s '%s': %d of %d affixes are the best tier the game has for this base (item level %s%s%s; "
                "heuristic limit %d for %s). Legal, but a demanding combination of rolls (roll rarity, not a price: no "
                "market data); check whether lower tiers would do%s."
                % (it.get("slot"), rarity.lower(), it.get("name"), tt, it.get("explicit_affixes") or tt,
                   it.get("item_level", "?"), " assumed" if it.get("item_level_assumed") else "", lvl_note,
                   lim, profile["investment"], ("; crafting steps: " + ", ".join(sig)) if sig else ""))
        elif budget and sig:
            add("gear", "REVIEW", "%s %s '%s' needs: %s. Fine if obtainable at this budget; cost not verified."
                % (it.get("slot"), rarity.lower(), it.get("name"), ", ".join(sig)))
    major = th["dependency"]["major_pct"]
    deps = []
    for u in audit.get("uniques") or []:
        w = u["without"]
        dl, el = -(w.get("dps_pct") or 0), -(w.get("ehp_pct") or 0)
        if w.get("main_skill_disabled"):
            kind = "BUILD-ENABLING"
        elif dl >= major or el >= major:
            kind = "major"
        elif dl < 1 and el < 1 and abs(w.get("life") or 0) < 50:
            kind = "no measurable effect"
        else:
            kind = "replaceable"
        deps.append("%s %s (-%.0f%% DPS, -%.0f%% EHP)" % (u["name"], kind, dl, el))
        if kind in ("BUILD-ENABLING", "major") and budget:
            add("gear", "REVIEW", "%s is %s for a %s build: confirm it's obtainable and name a fallback; cost not verified."
                % (u["name"], kind, profile["investment"]))
        if kind == "no measurable effect":
            add("gear", "REVIEW", "%s shows no measurable PoB effect: keep it only for a reason PoB can't show." % u["name"])
    if deps:
        add("gear", "INFO", "Unique dependency (slot emptied, no replacement): " + "; ".join(deps) + ". Cost not verified.")

    # ---- slots with no measured benefit (advisory; never a verdict on the item) --------------
    reported = {u["name"] for u in audit.get("uniques") or [] if ("%s no measurable effect" % u["name"]) in " ".join(deps)}
    for s in audit.get("slots") or []:
        if not s.get("no_measured_benefit") or s.get("empty") or s.get("name") in reported:
            continue
        w = s.get("without") or {}
        other = []
        for k, ch in sorted((w.get("utility_changed") or {}).items()):
            f = (lambda v: ("%.2f" % v).rstrip("0").rstrip(".") if isinstance(v, (int, float)) and abs(v) < 100 else _fmt(v))
            other.append("%s %s -> %s" % (k, f(ch.get("from")), f(ch.get("to"))))
        if w.get("attr_short"):
            other.append("without it the build lacks " + ", ".join("%s %s" % (v, k) for k, v in sorted(w["attr_short"].items())))
        if w.get("min_ele_res_change"):
            other.append("lowest elemental resistance %+d" % w["min_ele_res_change"])
        if w.get("chaos_res_change"):
            other.append("chaos resistance %+d" % w["chaos_res_change"])
        add("slots", "REVIEW", "%s %s: removing it changes neither DPS nor EHP as PoB calculates them (%+.1f%% DPS, %+.1f%% EHP)%s. "
            "That doesn't make it useless: movement speed, ailment protection, recovery, attributes, sockets and other effects "
            "these two numbers don't capture may matter. If none applies, `candidates` lists replacements to test."
            % (s["slot"], s.get("name") or "item", w.get("dps_pct") or 0, w.get("ehp_pct") or 0,
               ("; PoB does show: " + "; ".join(other)) if other else ""))

    # ---- skill groups (priority 7) -------------------------------------------------------
    plain = []
    guard_groups = {g.get("group") for g in audit.get("guards") or []}
    for g in audit.get("skill_groups") or []:
        if g["index"] in guard_groups:
            continue                      # reported by the guard finding above
        w = g["without"]
        dl, el = -(w.get("dps_pct") or 0), -(w.get("ehp_pct") or 0)
        txt = "[%d] %s: %.1f%% DPS, %.1f%% EHP" % (g["index"], " + ".join(g["gems"]), dl, el)
        if dl <= -0.5 and el < 0.5:
            add("skills", "WARN", txt + ": PoB shows MORE damage without this group. Check for a conflicting effect (two "
                "marks/curses, a 'less damage' support) before keeping it.")
        elif abs(dl) < 0.5 and abs(el) < 0.5:
            add("skills", "REVIEW", txt + ": no measurable PoB effect; fine for utility PoB can't show (movement, curse on "
                "hit, a small triggered guard), otherwise swap it.")
        else:
            plain.append(txt)
    if plain:
        add("skills", "INFO", "Utility groups: " + "; ".join(plain))
    return out


def summary(evaluation, errors=()):
    """Counts per severity plus the stop guidance. Errors (PoB) are always critical."""
    crit = [e for e in evaluation if e[3] == "critical"]
    mat = [e for e in evaluation if e[3] == "material"]
    adv = [e for e in evaluation if e[3] == "advisory"]
    n_crit = len(crit) + len(errors)
    if n_crit:
        status = "NOT READY: fix the critical items first"
    elif mat:
        status = "OK TO DELIVER once each material weakness is fixed or its trade-off is explained"
    else:
        status = "READY: no critical or material items; further optimisation is optional"
    return {"critical": n_crit, "material": len(mat), "advisory": len(adv), "status": status}


def print_audit(evaluation, profile, errors=(), detail=False):
    """Concise by default: problems first by severity and priority, passed checks on one line."""
    print("\nQUALITY AUDIT (profile: %s investment, %s, %s, level %d; thresholds are advisory)" % (
        profile["investment"], profile["content"], profile["mode"], profile["level"]))
    order = sorted(evaluation, key=lambda e: e[4])
    for sev in ("critical", "material", "advisory"):
        items = [e for e in order if e[3] == sev]
        if items:
            print("  %s (%s):" % (sev.upper(), SEVERITY_TEXT[sev]))
            for a, _, msg, _, _ in items:
                print("    [%s] %s" % (a, msg))
    infos = [e for e in order if e[3] == "info"]
    if infos:
        print("  INFO:")
        for a, _, msg, _, _ in infos:
            print("    [%s] %s" % (a, msg))
    passed = [e for e in order if e[3] == "pass"]
    if passed:
        if detail:
            print("  PASSED:")
            for a, _, msg, _, _ in passed:
                print("    [%s] %s" % (a, msg))
        else:
            print("  PASSED: " + "; ".join("max hit (all types at target)" if msg.startswith("Max hit") else msg.split(". ")[0]
                                           for a, _, msg, _, _ in passed))
    s = summary(evaluation, errors)
    print("  STATUS: critical %d | material %d | advisory %d -> %s" % (s["critical"], s["material"], s["advisory"], s["status"]))
