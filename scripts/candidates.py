"""Equipment candidates: ranking and reporting of what the driver's candidate pass found (no PoB here).

The driver discovers candidates from PoB's data (parsed modifiers tested against the main skill's weapon
and skill flags and conditions), orders them with a static score and measures a capped number with quick
PoB calculations against one baseline. This module turns those measurements into a short list per slot:

  discovered   compatible with the build on paper (some line applies); never a recommendation
  evaluated    measured by PoB as a drop-in replacement, at the build's current configuration
  shortlisted  evaluated, not rejected, and among the best few for its slot
  accepted     only after `compare` (full build, validation, requirements) and the user's decision

Nothing here prices an item: cost is "not verified" unless the user supplies prices.
"""

SIGNATURE_KEYS = ("main_skill", "weapon_class", "ascendancy", "archetype")


def signature_changes(old, new):
    """What changed between a recorded gear basis and the current build profile signature."""
    out = []
    for k in SIGNATURE_KEYS:
        a, b = (old or {}).get(k), (new or {}).get(k)
        if a is not None and a != b:
            out.append("%s: %s -> %s" % (k.replace("_", " "), a, b))
    return out


def _weakest(baseline, targets):
    """The baseline metric closest to (or furthest below) its target, as a relative margin."""
    rows = []
    for key in ("min_max_hit", "chaos_res", "min_ele_res"):
        b, need = baseline.get(key), targets.get(key)
        if b is None or need is None:
            continue
        if key == "min_ele_res" and b >= need:
            continue                            # capped at the target: not a weakness
        if key == "min_max_hit":
            margin = (b / need - 1) if need else 1
        else:
            margin = (b - need) / 100.0         # resistances: percentage points
        rows.append((margin, key))
    return min(rows)[1] if rows else None


def classify(c, baseline, targets, weakest=None):
    """-> (verdict, value, notes). verdict: rejected | promising | trade-off | no measured gain | worse | not evaluated."""
    e = c.get("eval")
    if not e:
        return "not evaluated", None, ["not measured (outside the evaluation limit)"]
    notes = []
    if e.get("main_skill_no_damage"):
        return "rejected", None, ["the main skill deals no damage with it (weapon or skill restriction)"]
    broken = []
    for key, need in targets.items():
        v, b = e.get(key), baseline.get(key)
        if v is None or b is None or need is None:
            continue
        if v < need and (b >= need or v < b):
            broken.append("%s %s < %s" % (key.replace("_", " "), _num(v), _num(need)))
    if broken:
        return "rejected", None, ["breaks the profile target: " + ", ".join(broken)]
    dps, ehp = e.get("dps_pct") or 0.0, e.get("ehp_pct") or 0.0
    mh = e.get("min_max_hit_pct") or 0.0
    value = dps + ehp
    if weakest == "min_max_hit" and mh < 0:
        value += 2 * mh
        notes.append("lowers the weakest metric (min max hit %+.1f%%)" % mh)
    elif weakest in ("chaos_res", "min_ele_res") and (e.get(weakest + "_change") or 0) < 0:
        value += 2 * e[weakest + "_change"]
        notes.append("lowers the weakest metric (%s %+d)" % (weakest.replace("_", " "), e[weakest + "_change"]))
    if e.get("attr_short"):
        notes.append("the build then lacks " + ", ".join("%s %s" % (v, k) for k, v in sorted(e["attr_short"].items()))
                     + ": move those attributes to another item")
    if e.get("utility_changed"):
        notes.append("also changes: " + ", ".join("%s %s -> %s" % (k, _num(v["from"]), _num(v["to"]))
                                                  for k, v in sorted(e["utility_changed"].items())))
    if abs(dps) < 1 and abs(ehp) < 1:
        verdict = "no measured gain"
    elif (dps >= 1 or ehp >= 1) and dps >= -1 and ehp >= -1 and mh >= -1:
        verdict = "promising"
    elif dps <= 1 and ehp <= 1:
        verdict = "worse"
    else:
        verdict = "trade-off"
    return verdict, round(value, 1), notes


ORDER = {"promising": 0, "trade-off": 1, "no measured gain": 2, "worse": 3, "not evaluated": 4, "rejected": 5}


def rank(cand, targets, investment="medium", per_slot=3):
    """Shortlist per slot group. cand = the driver's `candidates` object; targets = profile minimums."""
    baseline = cand.get("baseline") or {}
    weakest = _weakest(baseline, targets)
    groups = []
    for g in cand.get("groups") or []:
        rows = []
        for c in g.get("candidates") or []:
            verdict, value, notes = classify(c, baseline, targets, weakest)
            cost = ("crafted rare template: tiers capped for the budget, but a tier is not a price; cost not verified" if c.get("kind") == "rare"
                    else "craftable base: crafting cost not verified" if c.get("kind") in ("base", "magic")
                    else "unique: price and availability not verified")
            rows.append(dict(c, verdict=verdict, value=value, notes=notes, cost=cost))

        rows.sort(key=lambda r: (ORDER[r["verdict"]], -(r["value"] if r["value"] is not None else -1e9), r.get("name") or ""))
        # no prices: on a near tie (within 2 points) a low/medium budget puts a craftable item first (its cost is
        # still not verified)
        if investment in ("league_start", "low", "medium"):
            for i in range(len(rows) - 1):
                a, b = rows[i], rows[i + 1]
                if (a["verdict"] == b["verdict"] and a["value"] is not None and b["value"] is not None
                        and abs(a["value"] - b["value"]) <= 2 and a.get("kind") == "unique" and b.get("kind") in ("base", "magic", "rare")):
                    rows[i], rows[i + 1] = b, a
        keep = [r for r in rows if r["verdict"] in ("promising", "trade-off")][:per_slot]
        groups.append(dict(g, shortlist=keep, rejected=[r for r in rows if r["verdict"] == "rejected"],
                           others=[r for r in rows if r not in keep and r["verdict"] != "rejected"]))
    groups.sort(key=lambda g: (min([ORDER[r["verdict"]] for r in g["shortlist"]] or [9]),
                               -max([r["value"] or 0 for r in g["shortlist"]] or [0])))
    return {"weakest_metric": weakest, "groups": groups}


def variants(ranked, limit=4):
    """compare variants for the best shortlisted candidate of the best groups (at most `limit`)."""
    out = []
    for g in ranked["groups"]:
        if len(out) >= limit:
            break
        if not g["shortlist"]:
            continue
        c = g["shortlist"][0]
        cond_gain = ((c.get("conditional") or {}).get("item_effect") or {}).get("dps_pct") or 0
        if c["verdict"] != "promising" and (c["value"] or 0) <= 0 and cond_gain < 5:
            continue          # a trade-off that loses overall is not worth a full comparison
        out.append({"name": "%s: %s" % (g["target"], c["name"]), "items": [c["spec_item"]]})
    return out


def _num(v):
    if isinstance(v, (int, float)):
        if abs(v) >= 1e4:
            return "%.1fk" % (v / 1e3)
        return ("%.2f" % v).rstrip("0").rstrip(".") if isinstance(v, float) else str(v)
    return str(v)


def _pct(v):
    return "n/a" if v is None else "%+.1f%%" % v


def format_report(cand, ranked, limit_line=True):
    p = []
    cnt = cand.get("counts") or {}
    prof = cand.get("profile") or {}
    sig = prof.get("signature") or {}
    p.append("CANDIDATES (equipment that may suit this build; measured by PoB, not yet accepted)")
    rt = cand.get("rare_templates") or {}
    if rt.get("slots"):
        p.append("  Rare templates: " + "; ".join("%s: %s" % (sl["slot"], sl.get("skipped") or ", ".join(
            "%s%s" % (b["base"], " (equipped base)" if b.get("equipped_base") else "") for b in sl.get("bases") or [])) for sl in rt["slots"])
                 + " (each affix measured by PoB on the equipped base: %s evaluations; tiers capped for %s; results below)" % (
                     (cand.get("counts") or {}).get("rare_evals", "?"), rt.get("investment")))
    p.append("  Searched %s items (every unique and the craftable flask-slot bases); %s compatible on paper; %s shortlisted for "
             "measurement; %s PoB evaluations (limit %s: %s drop-in, %s crafted, %s condition scenario) in %ss."
             % (cnt.get("pool"), cnt.get("discovered"), cnt.get("static_kept"), cnt.get("evaluated"), cnt.get("eval_limit"),
                cnt.get("drop_in_evals"), cnt.get("crafted_evals"), cnt.get("conditional_evals"), cnt.get("seconds")))
    dt = ", ".join("%s" % k for k in sorted(prof.get("damage_types") or {}))
    ail = ", ".join("%s %s%%" % (k, v) for k, v in sorted((prof.get("ailment_chance") or {}).items()))
    p.append("  Build profile: %s (%s); weapon %s; %s; damage types %s%s; ailment chance per hit (highest part): %s"
             % (sig.get("main_skill"), sig.get("archetype"), sig.get("weapon_class"), sig.get("ascendancy") or "no ascendancy",
                dt or "-", (" (parts read: %s)" % ", ".join(prof["parts_read"])) if prof.get("parts_read") else "", ail or "none"))
    b = cand.get("baseline") or {}
    p.append("  Baseline (one PoB calculation, current config; every change below is against it): DPS %s, EHP %s, life %s, "
             "lowest ele res %s, chaos res %s, min max hit %s. Weakest metric vs target: %s."
             % (_num(b.get("dps")), _num(b.get("ehp")), _num(b.get("life")), b.get("min_ele_res"), b.get("chaos_res"),
                _num(b.get("min_max_hit")), (ranked.get("weakest_metric") or "-").replace("_", " ")))
    if cand.get("locked"):
        p.append("  Not searched (user requirement): " + ", ".join("%s (%s)" % kv for kv in sorted(cand["locked"].items())))
    p.append("")
    for g in ranked["groups"]:
        tgt = "%s replacing %s" % (g["target"], g.get("target_item") or "nothing (empty)")
        flag = ""
        if g.get("target_empty"):
            flag = " [empty slot]"
        elif g.get("target_no_measured_benefit"):
            flag = " [no measured DPS/EHP benefit" + ("; it does change: " + ", ".join(sorted(g["target_utility"])) if g.get("target_utility") else "") + "]"
        p.append("%s%s: %d compatible, %d shortlisted" % (tgt, flag, g.get("discovered") or 0, len(g["shortlist"])))
        if not g["shortlist"]:
            best = (g["others"] or [None])[0]
            p.append("    nothing measured better%s" % ((" (best: %s, %s, DPS %s, EHP %s)" % (
                best["name"], best["verdict"], _pct((best.get("eval") or {}).get("dps_pct")),
                _pct((best.get("eval") or {}).get("ehp_pct")))) if best and best.get("eval") else ""))
        for i, c in enumerate(g["shortlist"], 1):
            e = c.get("eval") or {}
            tpl = c.get("template") or {}
            kind = {"unique": "unique", "base": "base item", "magic": "crafted magic (best measured affixes)",
                    "rare": "crafted rare template%s" % ((", tiers capped for %s" % (cand.get("rare_templates") or {}).get("investment")) if cand.get("rare_templates") else ""),
                    "supplied": "supplied item"}.get(c.get("kind"), c.get("kind"))
            p.append("    %d. %s (%s) - %s: DPS %s, EHP %s, life %+d, min max hit %s"
                     % (i, c["name"], kind, c["verdict"], _pct(e.get("dps_pct")), _pct(e.get("ehp_pct")), e.get("life") or 0,
                        _pct(e.get("min_max_hit_pct"))))
            useful = [l for l in c.get("lines") or [] if l.get("status") not in ("no effect on this skill",)]
            if c.get("kind") == "rare":
                p.append("       lines: " + " | ".join("%s (%s)" % (l["text"], l["tier"]) for l in tpl.get("lines") or []))
                p.append("       T* = a costly group at its best tier (%d allowed at this budget%s); 'kept' = copied from the equipped item; "
                         "cost not verified - a 5-6 affix rare at these tiers is a trade or a multi-step craft"
                         % ((cand.get("rare_templates") or {}).get("top_tier_limit") or 0,
                            (", %d stepped down a tier" % tpl["demoted"]) if tpl.get("demoted") else ""))
            elif c.get("kind") == "magic":
                p.append("       lines: " + " | ".join(l["text"] for l in c.get("lines") or []))
            elif useful:
                p.append("       why: " + " | ".join("%s [%s]" % (l["text"][:70], l["status"]) for l in useful[:4]))
            for n in c.get("notes") or []:
                p.append("       note: " + n)
            conds = c.get("conditions") or []
            if conds:
                p.append("       conditions: " + "; ".join(
                    "%s%s %s (%s%s)" % ("not " if d.get("negated") else "", d["actor"], d["condition"],
                                        d.get("evidence") or "?", ", config " + d["config"] if d.get("config") else "")
                    for d in conds[:6]))
            cd = c.get("conditional")
            if cd:
                p.append("       if %s were set: the config change alone %s DPS / %s EHP; this item on top of it %s DPS / %s EHP "
                         "(an assumption test: PoB doesn't model how often the condition holds)"
                         % (", ".join(cd["config"]), _pct(cd["config_effect"].get("dps_pct")), _pct(cd["config_effect"].get("ehp_pct")),
                            _pct(cd["item_effect"].get("dps_pct")), _pct(cd["item_effect"].get("ehp_pct"))))
            for l in c.get("limitations") or []:
                p.append("       limit: " + l)
            if c.get("changes_weapon_class"):
                p.append("       note: changes the %s to %s (a material change: re-derive the rest of the gear)"
                         % ("off-hand" if g["target"] == "Weapon 2" else "weapon class", c["changes_weapon_class"]))
            p.append("       cost: " + c["cost"] + ((" (source: %s)" % c["source"]) if c.get("source") else ""))
        if g["rejected"]:
            p.append("    rejected: " + "; ".join("%s (%s)" % (r["name"], r["notes"][0]) for r in g["rejected"][:3]))
    dc = cand.get("different_configuration") or []
    if dc:
        p.append("")
        p.append("Different build configuration (not drop-ins; each would change the build):")
        for d in dc[:6]:
            p.append("  - %s (%s, %s): %s" % (d["name"], d.get("type"), d.get("slot"), d["reason"]))
    en = cand.get("enablers") or []
    if en:
        ap = cand.get("ascendancy_points") or {}
        p.append("")
        p.append("Passives that could enable an interaction (points from the current tree; ascendancy %s/%s used):"
                 % (ap.get("used"), ap.get("available")))
        for n in en[:8]:
            p.append("  - %s%s, %s point(s): %s. %s" % (n["name"], " [%s]" % n["ascendancy"] if n.get("ascendancy") else "",
                                                       n.get("points") or "?", n["reason"], " / ".join(n.get("text") or [])[:120]))
    p.append("")
    p.append("Configured DPS is not guaranteed practical DPS. Test the best of these with `compare` (full build, validity, profile "
             "targets and your requirements); only that, and the user's choice, puts one in the build.")
    return "\n".join(p)
