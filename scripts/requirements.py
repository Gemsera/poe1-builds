"""User requirements: the build-defining choices the user explicitly named.

They are hard constraints at every step: the first build, every error-fixing rerun, `compare`
and `tree-audit` candidates, and the final export. A build or candidate that breaks one is
invalid / rejected, whatever its DPS or defences.

Only what the user actually said belongs here. Things inferred from the request (the weapon a
skill usually uses, a playstyle, gear) go in the spec's "assumptions" list instead: shown in the
report, never enforced. Mechanical compatibility (a melee skill needs a melee weapon) is
enforced by Path of Building itself.

  "requirements": {
    "main_skill": "Elemental Hit",          the exact gem; a transfigured, Vaal or Awakened gem is a different gem
    "also_accept": ["..."],                 other gems the user said are acceptable as the main skill (optional)
    "class": "Ranger",
    "ascendancy": "Warden",
    "gems": ["Trinity"],                    other gems the user named: present and enabled
    "uniques": ["Mortem Morsu"],            equipped
    "passives": ["Elemental Equilibrium"],  keystones / notables / ascendancy notables allocated
    "weapon_type": "Claw"                   only when the user named it (PoB base type, e.g. Claw, Bow, Wand)
  }

Gem identity is PoB's variantId, which is unique per gem. PoB's gameId (and the gemId written into
exported builds) is shared by a gem and its transfigured versions -- Elemental Hit and Elemental
Hit of the Spectrum both have Metadata/Items/Gems/SkillGemElementalHit -- so it is never used to
decide that a gem matches. There is no generic "allow variants" switch: an alternative is
accepted only when it is listed by name in also_accept, and it is reported as an alternative.
"""
import html
import re

KEYS = ("main_skill", "also_accept", "class", "ascendancy", "gems", "uniques", "passives", "weapon_type")
LIST_KEYS = ("also_accept", "gems", "uniques", "passives")
PREFIX = "Requirement not met: "


def _lc(s):
    # exported XML escapes apostrophes (Oro&apos;s Sacrifice); compare the unescaped text
    return html.unescape(str(s)).strip().lower()


def get(spec):
    req = spec.get("requirements") if isinstance(spec, dict) else None
    return req if isinstance(req, dict) and req else None


# ---------------------------------------------------------------------------
# Spec check (structure, availability, consistency with the spec) -- used by the preflight
# ---------------------------------------------------------------------------
def check_spec(spec, names=None):
    """Errors for an unusable requirements block, or a spec that already contradicts it."""
    if "requirements" not in spec:
        return []
    req = spec["requirements"]
    if not isinstance(req, dict):
        return ["Requirements: 'requirements' must be an object (see references/spec-format.md)"]
    errors = []
    for k in req:
        if k not in KEYS:
            errors.append("Requirements: unknown key '%s' (known: %s)" % (k, ", ".join(KEYS)))
    for k in ("main_skill", "class", "ascendancy", "weapon_type"):
        if k in req and (not isinstance(req[k], str) or not req[k].strip()):
            errors.append("Requirements: '%s' must be a non-empty string" % k)
    for k in LIST_KEYS:
        if k in req and (not isinstance(req[k], list) or not all(isinstance(x, str) and x.strip() for x in req[k])):
            errors.append("Requirements: '%s' must be a list of names" % k)
    if "also_accept" in req and "main_skill" not in req:
        errors.append("Requirements: 'also_accept' lists alternatives to 'main_skill', which is not set")
    if errors:
        return errors

    # availability in this PoB version
    if names is not None:
        for g in ([req["main_skill"]] if "main_skill" in req else []) + req.get("also_accept", []) + req.get("gems", []):
            if g not in names.gems:
                close = [n for n in names.gem_names if _lc(n) == _lc(g)]
                errors.append("Requirements: gem '%s' does not exist in this PoB version%s" % (
                    g, (" (exact name: %s)" % close[0]) if close else ""))
            elif "main_skill" in req and g in [req["main_skill"]] + req.get("also_accept", []) and names.gems[g].get("support"):
                errors.append("Requirements: '%s' is a support gem; the main skill must be an active skill" % g)
        if "class" in req and _lc(req["class"]) not in {_lc(c) for c in names.raw["classes"]}:
            errors.append("Requirements: unknown class '%s'" % req["class"])
        if "ascendancy" in req:
            owners = [c for c, ascs in names.raw["classes"].items() if _lc(req["ascendancy"]) in {_lc(x) for x in ascs}]
            if not owners:
                errors.append("Requirements: unknown ascendancy '%s'" % req["ascendancy"])
            elif "class" in req and _lc(req["class"]) not in {_lc(c) for c in owners}:
                errors.append("Requirements: ascendancy '%s' belongs to %s, not %s" % (req["ascendancy"], owners[0], req["class"]))
        for u in req.get("uniques", []):
            if _lc(u) not in names.uniques_lc:
                errors.append("Requirements: unique '%s' does not exist in this PoB version" % u)
        all_nodes = {_lc(n) for n in names.raw["nodes"].values()}
        for ns in names.raw["ascendancy_nodes"].values():
            all_nodes |= {_lc(n) for n in ns}
        for p in req.get("passives", []):
            if _lc(p) not in all_nodes:
                errors.append("Requirements: passive '%s' does not exist in this tree" % p)
    if errors:
        return errors

    # the spec itself must honour the requirements (the build is checked again after PoB runs)
    out = []
    if "class" in req and _lc(spec.get("class", "")) != _lc(req["class"]):
        out.append("class is %s, the user asked for %s" % (spec.get("class") or "not set", req["class"]))
    if "ascendancy" in req and _lc(spec.get("ascendancy") or "") != _lc(req["ascendancy"]):
        out.append("ascendancy is %s, the user asked for %s" % (spec.get("ascendancy") or "not set", req["ascendancy"]))
    spec_gems = []
    for g in spec.get("skills") or []:
        if isinstance(g, dict):
            for gem in g.get("gems") or []:
                n = gem.get("name") if isinstance(gem, dict) else gem
                if isinstance(n, str):
                    spec_gems.append((n, bool(g.get("main")), not (isinstance(gem, dict) and gem.get("enabled") is False)))
    if "main_skill" in req:
        accepted = [req["main_skill"]] + req.get("also_accept", [])
        main_active = [n for n, main, _ in spec_gems if main and not (names and names.gems.get(n, {}).get("support"))]
        if not any(_lc(n) in {_lc(a) for a in accepted} for n in main_active):
            out.append("the main skill group has %s, the user asked for %s" % (
                ", ".join(main_active) or "no active gem", req["main_skill"]))
    for g in req.get("gems", []):
        if not any(_lc(n) == _lc(g) and en for n, _, en in spec_gems):
            out.append("gem %s is not in the spec (or is disabled)" % g)
    spec_uniques = {_lc(it.get("unique")) for it in spec.get("items") or [] if isinstance(it, dict) and it.get("unique")}
    for it in spec.get("items") or []:
        if isinstance(it, dict) and isinstance(it.get("text"), str) and re.match(r"\s*Rarity:\s*UNIQUE", it["text"], re.I):
            lines = [l.strip() for l in it["text"].splitlines() if l.strip()]
            if len(lines) > 1:
                spec_uniques.add(_lc(lines[1]))
    for u in req.get("uniques", []):
        if _lc(u) not in spec_uniques:
            out.append("unique %s is not in the spec's items" % u)
    return [PREFIX + x for x in out]


# ---------------------------------------------------------------------------
# Result check (what Path of Building actually calculated)
# ---------------------------------------------------------------------------
def _gem_id(name, names):
    g = names.gems.get(name) if names is not None else None
    return g.get("variant_id") if g else None


def _gem_matches(entry_name, entry_id, wanted, names):
    """Same gem: PoB variantId when both are known, otherwise the exact gem name (also unique in PoB)."""
    wid = _gem_id(wanted, names)
    if wid and entry_id:
        return entry_id == wid
    return entry_name is not None and _lc(entry_name) == _lc(wanted)


def check_result(req, resp, names=None):
    """(violations, notes) for a PoB response. Notes say when a user-permitted alternative is used."""
    if not req:
        return [], []
    out, notes = [], []
    tree = resp.get("tree") or {}
    if "class" in req and _lc(tree.get("class") or "") != _lc(req["class"]):
        out.append("class is %s, the user asked for %s" % (tree.get("class") or "unknown", req["class"]))
    if "ascendancy" in req:
        asc = tree.get("ascendancy")
        if not asc or _lc(asc) in ("none", ""):
            out.append("no ascendancy is selected, the user asked for %s" % req["ascendancy"])
        elif _lc(asc) != _lc(req["ascendancy"]):
            out.append("ascendancy is %s, the user asked for %s" % (asc, req["ascendancy"]))
    if "main_skill" in req:
        mg = resp.get("main_skill_gem") or {}
        name, vid = mg.get("name") or resp.get("main_skill"), mg.get("variant_id")
        if _gem_matches(name, vid, req["main_skill"], names):
            pass
        else:
            alt = next((a for a in req.get("also_accept", []) if _gem_matches(name, vid, a, names)), None)
            if alt:
                notes.append("main skill is %s, an alternative the user permitted (requested: %s)" % (alt, req["main_skill"]))
            else:
                wid = _gem_id(req["main_skill"], names)
                out.append("main skill is %s%s, the user asked for %s%s" % (
                    name or "none", " [%s]" % vid if vid else "", req["main_skill"], " [%s]" % wid if wid else ""))
    groups = resp.get("skills") or []
    for g in req.get("gems", []):
        ok = any(grp.get("enabled", True) and e.get("enabled", True) and _gem_matches(e.get("gem_name") or e.get("name"),
                                                                                       e.get("variant_id"), g, names)
                 for grp in groups for e in grp.get("gems") or [])
        if not ok:
            out.append("gem %s is not used (missing or disabled)" % g)
    equipped = {_lc(i.get("name")) for i in resp.get("items") or [] if (i.get("rarity") or "").upper() in ("UNIQUE", "RELIC")}
    for u in req.get("uniques", []):
        if _lc(u) not in equipped:
            out.append("unique %s is not equipped" % u)
    alloc = {_lc(re.sub(r"\s*\[[^\]]*\]$", "", n)) for n in (tree.get("keystones") or []) + (tree.get("notables") or [])}
    for p in req.get("passives", []):
        if _lc(p) not in alloc:
            out.append("passive %s is not allocated" % p)
    if "weapon_type" in req:
        w = next((i for i in resp.get("items") or [] if i.get("slot") == "Weapon 1"), None)
        wt = w and w.get("base_type")
        if _lc(wt or "") != _lc(req["weapon_type"]):
            out.append("weapon is %s, the user asked for %s" % (wt or "none", req["weapon_type"]))
    return out, notes


# ---------------------------------------------------------------------------
# Export check: the saved build itself, read independently of the PoB response
# ---------------------------------------------------------------------------
def _attrs(tag):
    return dict(re.findall(r'(\w+)="([^"]*)"', tag))


def _active_block(xml, outer, inner, active_attr):
    m = re.search(r"<%s\b([^>]*)>(.*?)</%s>" % (outer, outer), xml, re.S)
    if not m:
        return ""
    active = _attrs("<x " + m.group(1)).get(active_attr)
    blocks = list(re.finditer(r"<%s\b([^>]*)>(.*?)</%s>" % (inner, inner), m.group(2), re.S))
    for b in blocks:
        if active and _attrs("<x " + b.group(1)).get("id") == active:
            return b.group(2)
    return blocks[0].group(2) if blocks else m.group(2)


def check_export(req, xml, names=None):
    """Violations found in the exported XML (the file the user will import)."""
    if not req or not xml:
        return []
    out = []
    bm = re.search(r"<Build\b[^>]*>", xml)
    b = _attrs(bm.group(0)) if bm else {}
    if "class" in req and _lc(b.get("className", "")) != _lc(req["class"]):
        out.append("exported class is %s, the user asked for %s" % (b.get("className") or "none", req["class"]))
    if "ascendancy" in req and _lc(b.get("ascendClassName", "")) != _lc(req["ascendancy"]):
        out.append("exported ascendancy is %s, the user asked for %s" % (b.get("ascendClassName") or "none", req["ascendancy"]))
    skillset = _active_block(xml, "Skills", "SkillSet", "activeSkillSet")
    groups = [(_attrs("<x " + g.group(1)), [_attrs(t) for t in re.findall(r"<Gem\b[^>]*/>", g.group(2))])
              for g in re.finditer(r"<Skill\b([^>]*)>(.*?)</Skill>", skillset, re.S)]

    def gem_is(gem, wanted):
        gid = gem.get("variantId") or gem.get("skillId")       # never gemId: shared with transfigured versions
        return _gem_matches(gem.get("nameSpec"), gid, wanted, names)

    def is_support(gem):
        if names is None:
            return gem.get("nameSpec", "").lower().endswith(" support")
        vid = gem.get("variantId") or gem.get("skillId")
        for g in names.raw["gems"]:
            if (vid and g.get("variant_id") == vid) or (not vid and g["name"] == gem.get("nameSpec")):
                return bool(g.get("support"))
        return False

    if "main_skill" in req:
        try:
            msg = int(b.get("mainSocketGroup") or 1)
        except ValueError:
            msg = 1
        grp = groups[msg - 1] if 0 < msg <= len(groups) else None
        if not grp:
            out.append("exported build has no main skill group")
        else:
            actives = [g for g in grp[1] if g.get("enabled", "true") != "false" and not is_support(g)]
            try:
                idx = int(grp[0].get("mainActiveSkill") or 1)
            except ValueError:
                idx = 1
            main = actives[idx - 1] if 0 < idx <= len(actives) else (actives[0] if actives else None)
            accepted = [req["main_skill"]] + req.get("also_accept", [])
            if not main or not any(gem_is(main, a) for a in accepted):
                wid = _gem_id(req["main_skill"], names)
                out.append("exported main skill is %s%s, the user asked for %s%s" % (
                    main.get("nameSpec") if main else "none",
                    " [%s]" % (main.get("variantId") or main.get("skillId")) if main else "", req["main_skill"],
                    " [%s]" % wid if wid else ""))
    for want in req.get("gems", []):
        if not any(gattr.get("enabled", "true") != "false" and gem.get("enabled", "true") != "false" and gem_is(gem, want)
                   for gattr, gems in groups for gem in gems):
            out.append("exported build does not use gem %s" % want)
    if req.get("uniques"):
        itemset = _active_block(xml, "Items", "ItemSet", "activeItemSet")
        equipped_ids = {_attrs(t).get("itemId") for t in re.findall(r"<Slot\b[^>]*/>", itemset)} - {"0", None}
        titles = set()
        for m in re.finditer(r'<Item\b([^>]*)>(.*?)</Item>', xml, re.S):
            if _attrs("<x " + m.group(1)).get("id") in equipped_ids:
                lines = [re.sub(r"^\{[^}]*\}", "", l).strip() for l in m.group(2).splitlines()]
                lines = [l for l in lines if l and not l.startswith("<")]
                if lines and re.match(r"Rarity:\s*(UNIQUE|RELIC)", lines[0], re.I) and len(lines) > 1:
                    titles.add(_lc(lines[1]))
        for u in req["uniques"]:
            if _lc(u) not in titles:
                out.append("exported build does not equip %s" % u)
    if req.get("passives") and names is not None:
        spec_m = re.search(r'<Spec\b[^>]*\bnodes="([^"]*)"', xml)
        ids = set((spec_m.group(1) if spec_m else "").split(","))
        id_name = dict(names.raw["nodes"])
        id_name.update(names.raw.get("ascendancy_node_ids") or {})
        alloc = {_lc(id_name[i]) for i in ids if i in id_name}
        for p in req["passives"]:
            if _lc(p) not in alloc:
                out.append("exported tree does not allocate %s" % p)
    return out


def describe(req):
    if not req:
        return ""
    parts = []
    if "main_skill" in req:
        parts.append("main skill %s%s" % (req["main_skill"], (" (also accepted: %s)" % ", ".join(req["also_accept"]))
                                          if req.get("also_accept") else ""))
    for k in ("class", "ascendancy", "weapon_type"):
        if k in req:
            parts.append("%s %s" % (k.replace("_", " "), req[k]))
    for k in ("gems", "uniques", "passives"):
        if req.get(k):
            parts.append("%s: %s" % (k, ", ".join(req[k])))
    return "; ".join(parts)
