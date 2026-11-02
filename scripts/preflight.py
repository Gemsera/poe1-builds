"""Deterministic checks on a build spec before Path of Building is started.

Two layers:
1. Structure (no game data): types, required fields, slot names, unknown keys (typos), several
   "main" groups, duplicate slots.
2. Names (cached name index from the driver's "names" mode, one per PoB version): class and
   ascendancy, gems (a port of PoB's own SkillsTab:FindSkillGem matcher, checked against PoB in
   tests), uniques, item bases, passive and ascendancy nodes, config options, gem levels.

A preflight ERROR means PoB would reject the same thing, so the build is not calculated. When the
port can't be sure (unusual characters in a gem name), the name is left for PoB to judge.
Nothing here replaces PoB's validation: every build that passes still goes through PoB.

Repairs (only applied with --repair; the original spec file is never modified):
  gem level above the obtainable maximum      -> the gem's natural maximum
  gem quality above 23                        -> 20
  gem level needs a higher character level    -> the highest level the character can use
  gem name differing only in case/punctuation -> PoB's exact gem name
"""
import copy
import difflib
import re

import fastpath

KNOWN_KEYS = {"name", "class", "ascendancy", "bloodline", "level", "bandit", "pantheon_major", "pantheon_minor", "tree",
              "skills", "items", "config", "profile", "implicit_roll", "tree_version", "ascendancy_nodes", "_meta",
              "_comment", "requirements", "assumptions", "gear_basis", "infer_config", "legacy_gems", "trigger_events_per_second"}
TREE_KEYS = {"nodes", "targets", "passives", "ascendancy", "masteries", "fill", "_fill_pinned", "_fill_added", "allocated", "cluster", "prune", "attribute_repair"}
GROUP_KEYS = {"slot", "main", "gems", "enabled", "full_dps", "label", "main_active_skill"}
GEM_KEYS = {"name", "level", "quality", "enabled", "count", "stages", "skill_part", "minion_skill"}
ITEM_KEYS = {"slot", "unique", "text", "raw", "base", "variant", "exarch", "eater", "implicits", "corrupted_implicits",
             "corrupted", "implicit_roll", "anoint", "title"}
SLOT_ALIASES = {
    "weapon": "Weapon 1", "mainhand": "Weapon 1", "weapon1": "Weapon 1", "offhand": "Weapon 2", "shield": "Weapon 2",
    "quiver": "Weapon 2", "weapon2": "Weapon 2", "helm": "Helmet", "helmet": "Helmet", "body": "Body Armour",
    "bodyarmour": "Body Armour", "chest": "Body Armour", "bodyarmor": "Body Armour", "gloves": "Gloves", "boots": "Boots",
    "amulet": "Amulet", "ring1": "Ring 1", "ring2": "Ring 2", "belt": "Belt", "flask1": "Flask 1", "flask2": "Flask 2",
    "flask3": "Flask 3", "flask4": "Flask 4", "flask5": "Flask 5",
}
SLOTS = set(SLOT_ALIASES.values())


def norm(s):
    return re.sub(r"[^0-9a-z]", "", str(s or "").lower())


def canon_slot(slot):
    if slot is None:
        return None
    n = norm(slot)
    if n in SLOT_ALIASES:
        return SLOT_ALIASES[n]
    m = re.match(r"^[Jj]ewel\s*(\d+)$", str(slot))
    if m:
        return "Jewel " + m.group(1)
    if n == "jewel":
        return "Jewel"
    # an abyss jewel in an item's abyssal socket: "Belt Abyssal Socket 1", "Body Armour Abyssal Socket 2" (PoB's slot names)
    m = re.match(r"^(.*?)\s+Abyssal\s+Socket\s+(\d+)$", str(slot).strip(), re.I)
    if m and canon_slot(m.group(1)) in SLOTS:
        return "%s Abyssal Socket %s" % (canon_slot(m.group(1)), m.group(2))
    return None


def _suggest(name, options, n=3):
    opts = [o for o in options if not str(o).startswith("Awakened ")]   # drop-disabled since 3.29: never suggested
    return difflib.get_close_matches(str(name), opts, n=n, cutoff=0.6)


def _unknown_keys(obj, known, where, out):
    for k in obj:
        if k not in known:
            sug = _suggest(k, known, 1)
            out.append("%s: unknown key '%s' is ignored%s" % (where, k, (" (did you mean '%s'?)" % sug[0]) if sug else ""))


# ---------------------------------------------------------------------------
# Port of PoB SkillsTab:FindSkillGem (src/Classes/SkillsTab.lua) for names made of letters
# and spaces. Lua patterns -> Python regex: %a letter, %l lowercase letter, . any char.
# ---------------------------------------------------------------------------
class Uncertain(Exception):
    pass


def _find_skill_gem(name, gem_names):
    if not re.fullmatch(r"[A-Za-z ]+", name):
        raise Uncertain()
    ci = lambda a: "[%s%s]" % (a.upper(), a.lower())
    nospace = name.replace(" ", "")
    pats = [
        "^ " + re.sub(r"[A-Za-z]", lambda m: ci(m.group(0)), name) + "$",
        "^" + re.sub(r"[A-Za-z]", lambda m: " " + m.group(0) + "[a-z]+", name) + "$",
        "^ " + re.sub(r"[a-z]", lambda m: "[a-z]*" + m.group(0), nospace) + "[a-z]+$",
        "^" + re.sub(r"[A-Za-z]", lambda m: ".*" + m.group(0), nospace),
        "^" + re.sub(r"[A-Za-z]", lambda m: ".*" + ci(m.group(0)), nospace),
    ]
    for p in pats:
        rx = re.compile(p, re.S)
        found = [g for g in gem_names if rx.match(" " + g)]
        if len(found) > 1:
            return None, "ambiguous: matches '%s', '%s'" % (found[0], found[1])
        if found:
            return found[0], None
    return None, "unrecognised"


def resolve_gem(name, gem_names):
    """Mirror of the driver's findGem: exact (case-insensitive) name, else the name with/without ' Support'.
    PoB's own abbreviation matching is not used: it turned a renamed gem into a different one
    ("Onslaught" -> "Frenzy of Onslaught"). Returns the gem name or None (the driver rejects it)."""
    want = name.strip().lower()
    alt = re.sub(r"\s*support$", "", want) if want.endswith("support") else want + " support"
    by = {}
    for g in gem_names:
        by.setdefault(g.lower(), g)
    return by.get(want) or by.get(alt)


# ---------------------------------------------------------------------------
# Name index (cached per PoB version by fastpath)
# ---------------------------------------------------------------------------
def load_names(allow_spawn=True):
    raw = fastpath.static_get("names")
    # an index from before gem tags were recorded is refetched once
    if raw is not None and raw.get("gems") and "tags" not in raw["gems"][0]:
        raw = None
    if raw is not None or not allow_spawn:
        return raw
    resp = fastpath.run_one({"mode": "names"})
    if resp.get("errors") or not resp.get("results"):
        return None
    fastpath.static_put("names", resp["results"])
    return resp["results"]


def store_names(resp):
    """Keep a names response that was fetched together with another request."""
    if isinstance(resp, dict) and resp.get("results") and not resp.get("errors"):
        fastpath.static_put("names", resp["results"])


class Names:
    def __init__(self, raw):
        self.raw = raw
        self.classes = {norm(c): (c, a) for c, a in raw["classes"].items()}
        self.gem_names = [g["name"] for g in raw["gems"]]
        self.gems = {}
        for g in raw["gems"]:
            self.gems.setdefault(g["name"], g)
        self.gem_norm = {}
        for n in self.gem_names:
            self.gem_norm.setdefault(norm(n), set()).add(n)
        self.uniques_lc = {u.lower(): u for u in raw["uniques"]}
        self.uniques_norm = {norm(u): u for u in raw["uniques"]}
        self.bases = set(raw["bases"])
        self.node_ids = set(raw["nodes"].keys())
        self.nodes_norm = {norm(v) for v in raw["nodes"].values()}
        self.asc_nodes = {a: {norm(n) for n in ns} for a, ns in raw["ascendancy_nodes"].items()}
        self.config = raw["config"]
        self.bloodlines = {norm(b) for b in raw.get("bloodlines") or []}


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
def check(spec, names=None):
    """Returns dict(errors, warnings, repairs) where repairs is a list of (description, path, new_value)."""
    errors, warnings, repairs = [], [], []
    if not isinstance(spec, dict):
        return {"errors": ["the spec must be a JSON object"], "warnings": [], "repairs": []}
    _unknown_keys(spec, KNOWN_KEYS, "spec", warnings)
    cls = spec.get("class")
    if not isinstance(cls, str) or not cls.strip():
        errors.append("'class' is required (Marauder, Ranger, Witch, Duelist, Templar, Shadow, Scion)")
    level = spec.get("level", 90)
    if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 100:
        errors.append("'level' must be a whole number from 1 to 100 (got %r)" % (level,))
        level = 90
    tree = spec.get("tree", {})
    if not isinstance(tree, dict):
        errors.append("'tree' must be an object with nodes / ascendancy / masteries")
        tree = {}
    _unknown_keys(tree, TREE_KEYS, "tree", warnings)
    for key in ("nodes", "ascendancy", "masteries", "allocated", "cluster"):
        if key in tree and not isinstance(tree[key], list):
            errors.append("tree.%s must be a list" % key)
    if any(not isinstance(i, int) for i in tree.get("allocated") or []):
        errors.append("tree.allocated must be a list of node ids (integers)")
    nodes = [n for n in tree.get("nodes") or [] if isinstance(n, (str, int))]
    dup = sorted({str(n) for n in nodes if [str(x).lower() for x in nodes].count(str(n).lower()) > 1})
    if dup:
        warnings.append("tree.nodes lists %s more than once" % ", ".join(dup))
    fill = tree.get("fill")
    if fill is not None and fill is not False:
        fo = fill if isinstance(fill, dict) else {"objective": fill}
        if not isinstance(fo, dict):
            errors.append("tree.fill must be an objective name or an object")
        else:
            _unknown_keys(fo, {"objective", "reserve", "max_cost", "max_recalcs"}, "tree.fill", warnings)
            obj = fo.get("objective", "balanced")
            if obj is True:
                obj = "balanced"
            if str(obj).lower() not in ("balanced", "dps", "defence", "life"):
                errors.append("tree.fill.objective must be balanced, dps or defence (got %r)" % (obj,))
            for k in ("reserve", "max_cost", "max_recalcs"):
                if k in fo and not isinstance(fo[k], int):
                    errors.append("tree.fill.%s must be an integer" % k)
    for m in tree.get("masteries") or []:
        if isinstance(m, dict):
            _unknown_keys(m, {"mastery", "effect", "node"}, "tree.masteries[]", warnings)
        if not isinstance(m, dict) or not m.get("effect"):
            errors.append("each tree.masteries entry needs an 'effect' (got %r)" % (m,))

    skills = spec.get("skills", [])
    if not isinstance(skills, list):
        errors.append("'skills' must be a list of socket groups")
        skills = []
    mains = [i for i, g in enumerate(skills, 1) if isinstance(g, dict) and g.get("main")]
    if len(mains) > 1:
        warnings.append("skill groups %s are all marked main; PoB uses only the first (%d)" % (mains, mains[0]))
    for gi, g in enumerate(skills, 1):
        if not isinstance(g, dict):
            errors.append("skills[%d] must be an object with 'slot' and 'gems'" % gi)
            continue
        _unknown_keys(g, GROUP_KEYS, "skills[%d]" % gi, warnings)
        if g.get("slot") is not None and canon_slot(g["slot"]) is None:
            warnings.append("skills[%d]: slot '%s' is not an item slot, so these gems aren't socketed anywhere" % (gi, g["slot"]))
        gems = g.get("gems")
        if not isinstance(gems, list) or not gems:
            errors.append("skills[%d] has no gems" % gi)
            continue
        for k, gem in enumerate(gems):
            if isinstance(gem, dict):
                _unknown_keys(gem, GEM_KEYS, "skills[%d].gems[%d]" % (gi, k), warnings)
                if not isinstance(gem.get("name"), str):
                    errors.append("skills[%d].gems[%d] needs a 'name'" % (gi, k))
                for f in ("level", "quality"):
                    if f in gem and (not isinstance(gem[f], int) or isinstance(gem[f], bool) or gem[f] < 0):
                        errors.append("skills[%d] %s: %s must be a whole number" % (gi, gem.get("name"), f))
                if "stages" in gem and (not isinstance(gem["stages"], int) or isinstance(gem["stages"], bool) or gem["stages"] < 1):
                    errors.append("skills[%d] %s: stages must be a whole number >= 1 (got %r)" % (gi, gem.get("name"), gem["stages"]))
                if "skill_part" in gem and not (isinstance(gem["skill_part"], str) or
                                                (isinstance(gem["skill_part"], int) and not isinstance(gem["skill_part"], bool))):
                    errors.append("skills[%d] %s: skill_part must be a part number (1, 2...) or a part name" % (gi, gem.get("name")))
            elif not isinstance(gem, str):
                errors.append("skills[%d].gems[%d] must be a gem name or an object" % (gi, k))

    items = spec.get("items", [])
    if not isinstance(items, list):
        errors.append("'items' must be a list")
        items = []
    seen = {}
    for ii, it in enumerate(items, 1):
        if not isinstance(it, dict):
            errors.append("items[%d] must be an object" % ii)
            continue
        _unknown_keys(it, ITEM_KEYS, "items[%d]" % ii, warnings)
        if not it.get("slot"):
            errors.append("items[%d] needs a 'slot'" % ii)
        elif canon_slot(it["slot"]) is None:
            warnings.append("items[%d]: slot '%s' isn't an equipment slot; the item won't be equipped" % (ii, it["slot"]))
        else:
            cs = canon_slot(it["slot"])
            if cs != "Jewel" and cs in seen:
                warnings.append("items[%d] and items[%d] both use slot %s; only one can be equipped" % (seen[cs], ii, cs))
            seen.setdefault(cs, ii)
        if not any(it.get(k) for k in ("unique", "text", "raw", "base")):
            errors.append("items[%d] (%s) needs one of: unique, text, base" % (ii, it.get("slot")))
    if not isinstance(spec.get("config", {}), dict):
        errors.append("'config' must be an object of PoB config options")

    if names:
        _check_names(spec, names, level, errors, warnings, repairs)
    if "assumptions" in spec and not (isinstance(spec["assumptions"], list) and all(isinstance(x, str) for x in spec["assumptions"])):
        errors.append("'assumptions' must be a list of plain-text notes (they are shown, not enforced)")
    # what the user explicitly asked for: invalid or unavailable requirements, and a spec that breaks one
    import requirements as rq
    errors.extend(rq.check_spec(spec, names))
    return {"errors": errors, "warnings": warnings, "repairs": repairs}


def _check_names(spec, N, level, errors, warnings, repairs):
    cls = N.classes.get(norm(spec.get("class")))
    if spec.get("class") and not cls:
        errors.append("Unknown class '%s' (%s)" % (spec.get("class"), ", ".join(sorted(c for c, _ in N.classes.values()))))
    asc = spec.get("ascendancy")
    asc_name = None
    if cls and asc and norm(asc) != "none":
        match = [a for a in cls[1] if norm(a) == norm(asc)]
        if match:
            asc_name = match[0]
        else:
            owner = [c for c, ascs in N.classes.values() if any(norm(a) == norm(asc) for a in ascs)]
            errors.append(("Ascendancy '%s' belongs to %s, not %s" % (asc, owner[0], cls[0])) if owner
                          else "Unknown ascendancy '%s' (%s: %s)" % (asc, cls[0], ", ".join(cls[1])))
    if spec.get("bloodline") and norm(spec["bloodline"]) not in N.bloodlines:
        errors.append("Unknown bloodline / secondary ascendancy '%s'" % spec["bloodline"])

    tree = spec.get("tree") if isinstance(spec.get("tree"), dict) else {}
    for n in tree.get("nodes") or tree.get("targets") or tree.get("passives") or []:
        if isinstance(n, int) or (isinstance(n, str) and n.isdigit()):
            if str(n) not in N.node_ids:
                errors.append("Passive node id %s doesn't exist on this tree" % n)
        elif isinstance(n, str) and norm(n) not in N.nodes_norm:
            sug = _suggest(n, N.raw["nodes"].values())
            errors.append("Passive '%s' not found%s" % (n, (" (did you mean: %s?)" % ", ".join(sug)) if sug else ""))
    if asc_name:
        for n in tree.get("ascendancy") or spec.get("ascendancy_nodes") or []:
            if isinstance(n, str) and not n.isdigit() and norm(n) not in N.asc_nodes.get(asc_name, set()):
                errors.append("Passive '%s' not found in %s" % (n, asc_name))

    for gi, g in enumerate(spec.get("skills") or [], 1):
        if not isinstance(g, dict) or not isinstance(g.get("gems"), list):
            continue
        for k, gem in enumerate(g["gems"]):
            gname = gem.get("name") if isinstance(gem, dict) else gem
            if not isinstance(gname, str):
                continue
            try:
                found = resolve_gem(gname, N.gem_names)
            except Uncertain:
                found = None
                exact = N.gem_norm.get(norm(gname)) or N.gem_norm.get(norm(gname + " Support"))
                if exact and len(exact) == 1:
                    found = next(iter(exact))
                    if found.lower() not in (gname.lower(), (gname + " support").lower()):
                        # PoB's matcher treats punctuation as pattern syntax; the exact name always resolves
                        repairs.append(("skills[%d] gem '%s' -> '%s' (same name apart from case/punctuation)"
                                        % (gi, gname, found), ("skills", gi - 1, "gems", k, "name"), found))
                else:
                    continue                      # let PoB decide
            path = ("skills", gi - 1, "gems", k)
            if not found:
                close = N.gem_norm.get(norm(gname)) or N.gem_norm.get(norm(gname) + "support")
                if close and len(close) == 1:
                    fixed = next(iter(close))
                    repairs.append(("skills[%d] gem '%s' -> '%s' (same name apart from case/punctuation)" % (gi, gname, fixed),
                                    path + ("name",), fixed))
                    found = fixed
                else:
                    sug = _suggest(gname, N.gem_names)
                    errors.append("Gem '%s' not found%s" % (gname, (" (similar: %s)" % ", ".join(sug)) if sug else ""))
                    continue
            data = N.gems.get(found)
            if not data:
                continue
            # Awakened gems are drop-disabled since 3.29: legacy items that exist only in Standard. The regular gem
            # always exists; some have a Greater version (an exceptional, low-max-level gem) worth a compare.
            if "awakened" in (data.get("tags") or []) and not spec.get("legacy_gems"):
                base = found[len("Awakened "):] if found.startswith("Awakened ") else None
                if base and base in N.gems:
                    greater = ("Greater " + base) if ("Greater " + base) in N.gems else None
                    repairs.append(("skills[%d] %s: Awakened gems are not obtainable in 3.29+ leagues (drop-disabled; legacy in Standard) -> %s%s"
                                    % (gi, found, base, (" (also compare '%s')" % greater) if greater else ""), path + ("name",), base))
                    found = base
                    data = N.gems.get(found) or data
                    # an Awakened level (1-6) means nothing on the regular gem: take it to the regular maximum
                    if isinstance(gem, dict) and isinstance(gem.get("level"), int) and gem["level"] <= 6 and (data.get("max") or 20) > 6:
                        repairs.append(("skills[%d] %s level %d was the Awakened gem's level -> %d" % (gi, base, gem["level"], data.get("max") or 20),
                                        path + ("level",), data.get("max") or 20))
                        gem = dict(gem, level=data.get("max") or 20)
                else:
                    errors.append("skills[%d] %s: Awakened gems are not obtainable in 3.29+ leagues (drop-disabled; legacy in Standard). "
                                  "Set \"legacy_gems\": true for a Standard build." % (gi, found))
            if isinstance(gem, dict):
                _check_stage_part(gi, found, gem, data, errors, warnings)
            mx = data.get("max") or 20
            lvl = gem.get("level") if isinstance(gem, dict) and isinstance(gem.get("level"), int) else mx
            q = gem.get("quality") if isinstance(gem, dict) and isinstance(gem.get("quality"), int) else 20
            if lvl > mx + 1:
                repairs.append(("skills[%d] %s level %d is above the obtainable maximum -> %d" % (gi, found, lvl, mx),
                                path + ("level",), mx))
                lvl = mx
            if q > 23:
                repairs.append(("skills[%d] %s quality %d is impossible -> 20" % (gi, found, q), path + ("quality",), 20))
            req = data.get("req") or []
            if 1 <= lvl <= len(req) and isinstance(req[lvl - 1], int) and req[lvl - 1] > level:
                usable = [l for l in range(1, min(lvl, len(req)) + 1) if isinstance(req[l - 1], int) and req[l - 1] <= level]
                if usable:
                    repairs.append(("skills[%d] %s level %d needs character level %d -> level %d (usable at %d)"
                                    % (gi, found, lvl, req[lvl - 1], usable[-1], level), path + ("level",), usable[-1]))

    for ii, it in enumerate(spec.get("items") or [], 1):
        if not isinstance(it, dict):
            continue
        u = it.get("unique")
        if isinstance(u, str) and u.lower() not in N.uniques_lc and norm(u) not in N.uniques_norm:
            sug = _suggest(u, N.uniques_lc.values())
            errors.append("Unique '%s' not found%s" % (u, (" (similar: %s)" % ", ".join(sug)) if sug else ""))
        b = it.get("base")
        if isinstance(b, str) and not it.get("text") and b not in N.bases:
            ci = [x for x in N.bases if x.lower() == b.lower()]
            if not ci:
                sug = _suggest(b, N.bases)
                errors.append("Unknown base '%s'%s" % (b, (" (similar: %s)" % ", ".join(sug)) if sug else ""))

    cfg = dict(spec.get("config") or {}) if isinstance(spec.get("config"), dict) else {}
    for k, v in cfg.items():
        if k not in N.config:
            sug = _suggest(k, N.config.keys(), 1)
            warnings.append("Unknown config option '%s'%s" % (k, (" (did you mean '%s'?)" % sug[0]) if sug else ""))
        else:
            vals = N.config[k].get("values")
            if vals and isinstance(v, str) and v not in vals:
                warnings.append("Config %s = '%s' is not one of %s" % (k, v, "/".join(vals)))


def _check_stage_part(gi, found, gem, data, errors, warnings):
    """stages / skill_part only where PoB's data supports them (the driver confirms after calculating)."""
    where = "skills[%d] %s" % (gi, found)
    if "stages" in gem:
        if data.get("support"):
            errors.append("%s: stages can't be set on a support gem" % where)
        elif not data.get("stages"):
            errors.append("%s: PoB has no stages for this skill, so 'stages' would do nothing" % where)
        elif isinstance(gem["stages"], int) and data.get("stages_base_max") and gem["stages"] > data["stages_base_max"]:
            warnings.append("%s: %d stages is above the base maximum of %d; fine only if gear or passives raise it "
                            "(the build report confirms what PoB used)" % (where, gem["stages"], data["stages_base_max"]))
    if "skill_part" in gem:
        parts = data.get("parts") or []
        sp = gem["skill_part"]
        if not parts:
            errors.append("%s: this skill has no selectable parts in PoB" % where)
        elif isinstance(sp, int) and not isinstance(sp, bool) and not 1 <= sp <= len(parts):
            errors.append("%s: skill_part %d is out of range; parts are %s" % (
                where, sp, ", ".join("%d '%s'" % (i, p) for i, p in enumerate(parts, 1))))
        elif isinstance(sp, str) and norm(sp) not in {norm(p) for p in parts}:
            errors.append("%s: skill_part '%s' isn't a part of this skill; parts are %s" % (
                where, sp, ", ".join("%d '%s'" % (i, p) for i, p in enumerate(parts, 1))))


def normalise_repairs(spec, repairs):
    """Turn string gems that need a level/quality repair into objects so the path resolves."""
    s = copy.deepcopy(spec)
    for _, path, _ in repairs:
        if len(path) == 5 and path[0] == "skills" and path[2] == "gems":
            gems = s["skills"][path[1]]["gems"]
            if isinstance(gems[path[3]], str):
                gems[path[3]] = {"name": gems[path[3]]}
    return s


def repaired(spec, repairs):
    base = normalise_repairs(spec, repairs)
    out = copy.deepcopy(base)
    for _, path, value in repairs:
        obj = out
        for p in path[:-1]:
            obj = obj[p]
        obj[path[-1]] = value
    return out
