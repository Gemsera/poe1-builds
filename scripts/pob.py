#!/usr/bin/env python3
"""poe1-builds CLI: build, validate and calculate Path of Exile 1 builds with
Path of Building Community running headless.

Run `python3 pob.py --help` for commands. Run scripts/setup.sh once first.
"""
import argparse
import base64
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import zlib
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
POB_HOME = os.environ.get("POB_HOME", os.path.expanduser("~/.cache/poe1-pob"))
POB_SRC = os.path.join(POB_HOME, "pob", "src")
LUAJIT = os.path.join(POB_HOME, "luajit", "bin", "luajit")
LUAMODS = os.path.join(POB_HOME, "luamods")
DRIVER = os.path.join(HERE, "driver.lua")


# ---------------------------------------------------------------------------
# PoB import codes: XML -> zlib -> base64 (URL-safe alphabet)
# ---------------------------------------------------------------------------
def encode_code(xml_text: str) -> str:
    data = zlib.compress(xml_text.encode("utf-8"), 9)
    return base64.b64encode(data).decode("ascii").replace("+", "-").replace("/", "_")


def decode_code(code: str) -> str:
    code = re.sub(r"\s+", "", code.strip())
    code = code.replace("-", "+").replace("_", "/")
    code += "=" * (-len(code) % 4)
    raw = base64.b64decode(code)
    try:
        return zlib.decompress(raw).decode("utf-8")
    except zlib.error:
        return zlib.decompress(raw, -15).decode("utf-8")


def read_build_input(arg: str) -> str:
    """Accept a PoB code, a file containing a code, or a PoB .xml file."""
    if re.match(r"^https?://", arg.strip()):
        sys.exit(
            "Links (pobb.in, pastebin, poe.ninja) can't be fetched from this sandbox.\n"
            "Ask for the PoB code itself: in Path of Building, Import/Export Build > Generate, then copy."
        )
    text = arg
    if len(arg) < 4096 and os.path.exists(arg):
        with open(arg, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    stripped = text.strip()
    if stripped.startswith("<"):
        return stripped
    return decode_code(stripped)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
import fastpath  # noqa: E402  (batching, cache, timing)


def ensure_setup():
    fastpath.ensure_setup()


def run_driver(request: dict, timeout=600) -> dict:
    """One driver request. Identical complete requests are answered from the cache."""
    return fastpath.run_one(request, timeout)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def fmt_num(v):
    if isinstance(v, (int, float)):
        if abs(v) >= 1e6:
            return "%.2fM" % (v / 1e6)
        if abs(v) >= 1e4:
            return "%.1fk" % (v / 1e3)
        if isinstance(v, float) and not v.is_integer():
            return "%.2f" % v
        return str(int(v))
    return str(v)


TREE_COSTLY = 7   # a target that needs this many points is worth a second look


def print_report(resp: dict, code: str = None, out_files=None):
    p = print
    p("=" * 70)
    p("RESULT: %s" % ("VALID" if resp.get("valid") else "INVALID"))
    p("PoB %s | tree %s (latest %s)" % (resp.get("pob_version"), (resp.get("tree") or {}).get("tree_version"), resp.get("latest_tree_version")))
    p("=" * 70)
    for title, key in (("ERRORS (must fix)", "errors"), ("WARNINGS", "warnings"), ("PATH OF BUILDING WARNINGS", "pob_warnings"), ("NOTES", "notes")):
        items = resp.get(key) or []
        if items:
            p("\n%s:" % title)
            for x in items:
                p("  - " + str(x))

    tree = resp.get("tree")
    if tree:
        p("\nCHARACTER: level %s %s / %s%s" % (resp.get("level"), tree.get("class"), tree.get("ascendancy"),
                                             (" / bloodline " + tree["bloodline"]) if tree.get("bloodline") else ""))
        p("Passive points: %s used / %s available at this level (max %s at 100)" % (
            tree.get("points_used"), tree.get("points_available_at_level"), tree.get("points_max_at_100")))
        p("Ascendancy points: %s used (%s expected at this level)" % (
            tree.get("ascendancy_points_used"), tree.get("ascendancy_points_available_at_level")))
        if tree.get("keystones"):
            p("Keystones: " + ", ".join(tree["keystones"]))
        if tree.get("notables"):
            p("Notables (%d): %s" % (len(tree["notables"]), ", ".join(tree["notables"])))
        if tree.get("masteries"):
            p("Masteries: " + " | ".join(tree["masteries"]))

    plan = resp.get("plan")
    if plan:
        costs = [x for x in plan.get("tree", []) if x.get("cost")]
        if costs:
            p("\nTree pathing (target: points spent to reach it, in allocation order):")
            p("  " + ", ".join("%s +%d" % (x["name"], x["cost"]) for x in costs))
            pricey = [x for x in costs if x["cost"] >= TREE_COSTLY]
            if pricey:
                p("  Costliest targets (>= %d points): %s. If one looks weak for the concept, test a nearby alternative "
                  "with compare, or run tree-audit --targets on just these." % (
                      TREE_COSTLY, ", ".join("%s +%d" % (x["name"], x["cost"]) for x in sorted(pricey, key=lambda x: -x["cost"]))))

    pth = (resp.get("plan") or {}).get("pathing")
    if pth:
        saved = (pth.get("nearest_points") or 0) - (pth.get("total_points") or 0)
        if pth.get("kept_pob_route"):
            p("  Pathing: %d points connect the targets; the Steiner search found nothing cheaper (%s), so PoB's own route is kept. "
              "To spend fewer points, change the targets, not the route: the costliest ones above are the lever." % (
                  pth.get("total_points", 0), ", ".join(pth.get("strategies") or [])))
        else:
            p("  Pathing: %d points connect the targets (strategy '%s', %d fewer than nearest-first; tried %s). Per-target costs above are "
              "the strategy's steps before local re-routing." % (pth.get("total_points", 0), pth.get("strategy"), saved, ", ".join(pth.get("strategies") or [])))
        if pth.get("attribute_travel_dropped"):
            p("  The route drops %d attribute-only travel node%s that nearest-first pathing took. If an attribute requirement now fails, "
              "meet it deliberately (ATTRIBUTES section / step 5): gem level, amulet base, a suffix, or a named +30 node." % (
                  pth["attribute_travel_dropped"], "" if pth["attribute_travel_dropped"] == 1 else "s"))
    pr = (resp.get("plan") or {}).get("pruned")
    if pr:
        p("  Pruned after pathing (travel no target needed once the later routes were in): %d node%s: %s" % (
            len(pr), "" if len(pr) == 1 else "s", ", ".join(x.get("name") or "?" for x in pr)))
    cl = (resp.get("plan") or {}).get("cluster")
    if cl:
        p("Cluster jewel nodes allocated after loading the jewels: " + ", ".join("%s +%d" % (x["name"], x["cost"]) for x in cl))
    inf = resp.get("inferred_config") or {}
    if inf.get("set"):
        p("\nConfig set from sources already in the build (gems, items, passives, ascendancy):")
        for e in inf["set"]:
            p("  %s = %s  <- %s" % (e["var"], _cfg_value(e), e.get("source") or "?"))
        p("  Set `\"<option>\": false` in the spec's config to opt out; the audit lists each option's evidence.")
    if inf.get("not_sustaining"):
        p("  Sources found but NOT switched on (not sustaining against a single target): " + "; ".join(
            "%s <- %s" % (e["var"], e.get("source") or "?") for e in inf["not_sustaining"]))
        p("  Set the option yourself if the content keeps it up (mapping), and say so in the write-up.")
    fill = resp.get("fill")
    if fill:
        added = fill.get("added") or []
        if added:
            p("\nTree fill (objective %s): %d points spent on %d nodes, %d PoB recalculations; %d -> %d points used of %d." % (
                fill.get("objective"), fill.get("points_added", 0), len(added), fill.get("recalcs", 0),
                fill.get("points_before", 0), fill.get("points_after", 0), fill.get("points_available", 0)))
            p("  " + ", ".join("%s%s (+%d%s)" % (x["name"], "" if x.get("type") == "Notable" else "", x["cost"],
                                                 (": " + "/".join(x["via"])) if x.get("via") else "") for x in added))
            p("  Each node's measured value when it was added (DPS% / defence%, defence = life+ES%, weakest max hit%, hits avoided% and recovery% combined): " + ", ".join(
                "%s %+.1f/%+.1f" % (x["name"], x.get("dps_pct") or 0, x.get("def_pct") or 0) for x in added))
        for e in fill.get("enabled_config") or []:
            p("  Config set by the fill: %s = %s (sustaining source: %s). The node was measured with it set; the audit lists its evidence." % (
                e["var"], _cfg_value(e), e["source"]))
        if fill.get("pruned_after"):
            rr = fill.get("refill_round") or {}
            p("  Pruned after the fill: %s (%d point%s freed, %d re-spent in a second round)." % (
                ", ".join(x.get("name") or "?" for x in fill["pruned_after"]), rr.get("freed", 0),
                "" if rr.get("freed", 0) == 1 else "s", rr.get("spent", 0)))
        if fill.get("stopped"):
            p("  Fill stopped: %s." % fill["stopped"])

    rep = resp.get("attribute_repair")
    if rep:
        if rep.get("added"):
            p("\nAttribute repair: %d point%s on %s. The tighter pathing no longer passes through the attributes the gems and gear need; "
              "these nodes cover the shortfall. Cheaper fixes may exist (gem level, amulet base, a free suffix): see ATTRIBUTES and step 5." % (
                  rep.get("points", 0), "" if rep.get("points", 0) == 1 else "s",
                  ", ".join("%s (+%d %s, %d pt)" % (x["name"], x["gain"], x["attr"], x["cost"]) for x in rep["added"])))
        if rep.get("unmet"):
            p("  Attribute repair could not cover: %s(no points left or no attribute node in reach)." % rep["unmet"])
    attrs = resp.get("attributes")
    if attrs and attrs.get("attributes"):
        at = attrs["attributes"]
        p("\nATTRIBUTES (have / need, margin; target margin %d):" % attrs.get("margin_target", 10))
        p("  " + " | ".join("%s %d/%d (%+d%s)" % (a, at[a]["have"], at[a]["need"], at[a]["margin"],
                                                 ", set by " + ", ".join(at[a]["set_by"]) if at[a].get("set_by") else "")
                            for a in ("Str", "Dex", "Int") if a in at))
        for a in ("Str", "Dex", "Int"):
            adv = (attrs.get("advice") or {}).get(a)
            if not adv:
                continue
            p("  %s %s by %d. Ways to meet it, cheapest first (each says what it displaces; never fill points):" % (
                a, "SHORT" if adv.get("short") else "margin thin", adv["gap"]))
            for o in adv.get("options") or []:
                p("    %d. %s -- costs: %s" % (o["order"], o["text"], o.get("cost", "?")))
        if (resp.get("fill") or {}).get("attribute_shortfall"):
            p("  Fill: " + resp["fill"]["attribute_shortfall"])

    skills = resp.get("skills") or []
    if skills:
        p("\nSKILLS (main: %s):" % resp.get("main_skill"))
        for g in skills:
            gems = ", ".join(
                "%s %s/%s%s" % (x.get("name"), x.get("level"), x.get("quality"), "*" if x.get("socket_color_match") is False else "")
                for x in g.get("gems", []))
            p("  [%d]%s %s%s: %s" % (g["index"], " MAIN" if g.get("main") else "", g.get("slot") or "(no slot)",
                                     (" %dL" % g["link_size"]) if g.get("link_size") else "", gems))
        msc = resp.get("main_skill_config") or {}
        bits = []
        if msc.get("part"):
            bits.append("part %d/%d '%s'%s" % (msc["part"]["index"], msc["part"]["count"], msc["part"]["name"],
                                             "" if msc["part"].get("explicit") else " (PoB default)"))
        if msc.get("stages"):
            bits.append("%d/%d stages%s" % (msc["stages"]["used"], msc["stages"]["max"],
                                           "" if msc["stages"].get("explicit") else " (not set: PoB uses the maximum)"))
        if bits:
            p("  Main skill calculated with: " + ", ".join(bits))
        if any(g.get("socket_color_mismatches") for g in skills):
            p("  (* = gem colour doesn't match its socket; fine in-game only if the item really has those colours)")

    items = resp.get("items") or []
    if items:
        p("\nITEMS:")
        for it in items:
            extra = ""
            if it.get("prefixes") is not None:
                extra = " [%dP/%dS]" % (it.get("prefixes", 0), it.get("suffixes", 0))
            p("  %-14s %s (%s, %s)%s" % (it.get("slot"), it.get("name"), it.get("base"), (it.get("rarity") or "").lower(), extra))
            for n in it.get("notes") or []:
                p("      note: " + n)

    te = resp.get("trigger_events")
    if te:
        fd = lambda v: fmt_num(v) if v is not None else "?"
        if te.get("events_per_second") is not None:
            p("\nTRIGGER RATE (main skill fires when you %s; PoB's own rate is the cooldown ceiling, %.2f/s):" % (
                "are stunned" if te["kind"] == "stun" else "pass the damage-taken threshold", te["pob_rate"]))
            p("  at %s events/s x %s%% chance = %s triggers/s: hit DPS %s, combined DPS %s%s. Mana/life cost and sustain use this rate." % (
                te["events_per_second"], te["chance"], te["rate"], fd(te.get("hit_dps")), fd(te.get("combined_dps")),
                (", full DPS %s" % fd(te["full_dps"])) if te.get("full_dps") else ""))
            p("  KEY STATS and the sidebar below are PoB's ceiling-rate numbers; quote the line above as the DPS.")
        else:
            p("\nTRIGGER RATE: PoB assumes %.2f triggers/s (cooldown ceiling) for a skill fired when you %s; set trigger_events_per_second." % (
                te["pob_rate"], "are stunned" if te["kind"] == "stun" else "take damage"))
    stats = resp.get("stats")
    if stats:
        p("\nKEY STATS:")
        for section in ("offence", "defence", "attributes"):
            vals = stats.get(section) or {}
            if vals:
                p("  %s: %s" % (section, ", ".join("%s %s" % (k, fmt_num(v)) for k, v in vals.items() if v not in (0, None))))
    sidebar = resp.get("sidebar")
    if sidebar:
        p("\nPATH OF BUILDING SIDEBAR:")
        for line in sidebar:
            p("  " + line)

    if out_files:
        p("\nFiles: " + ", ".join(out_files))
    if code:
        p("\nPOB IMPORT CODE:")
        p(code)


def print_results(resp, formatter):
    for e in resp.get("errors") or []:
        print("ERROR: " + e)
    results = resp.get("results") or []
    for r in results:
        print(formatter(r))
    if resp.get("total") and resp["total"] > len(results):
        print("... %d more (use --limit)" % (resp["total"] - len(results)))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:60] or "build"


def load_json_lenient(text: str):
    """JSON that may contain // or /* */ comments and trailing commas."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    cleaned = re.sub(r",(\s*[}\]])", r"\1", "".join(out))
    return json.loads(cleaned)


def load_spec(path):
    with open(path, encoding="utf-8") as f:
        return load_json_lenient(f.read())


def run_preflight(spec, allow_spawn=False):
    """Structure + (when the name index is cached) name checks. Returns the preflight dict."""
    import preflight
    with fastpath.stage("preflight"):
        raw = preflight.load_names(allow_spawn=allow_spawn)
        names = preflight.Names(raw) if raw else None
        res = preflight.check(spec, names)
        res["names_checked"] = names is not None
    return res


def print_preflight(pf, repaired_path=None):
    if pf.get("errors"):
        print("PREFLIGHT ERRORS:")
        for e in pf["errors"]:
            print("  - " + e)
    if pf.get("repairs"):
        if repaired_path:
            print("PREFLIGHT REPAIRS APPLIED (original spec unchanged; repaired copy: %s):" % repaired_path)
        else:
            print("PREFLIGHT: PoB would reject these; rerun with --repair to fix them automatically:")
        for r in pf["repairs"]:
            print("  - " + r[0])
    if pf.get("warnings"):
        print("PREFLIGHT WARNINGS:")
        for w in pf["warnings"]:
            print("  - " + w)


def cmd_build(a):
    import preflight
    spec = load_spec(a.spec)
    name = spec.get("name") if isinstance(spec, dict) and spec.get("name") else os.path.splitext(os.path.basename(a.spec))[0]
    pf = {"errors": [], "warnings": [], "repairs": [], "names_checked": False}
    repaired_path = None
    a.preflight, a.repair = getattr(a, "preflight", True), getattr(a, "repair", False)
    fail_fast = getattr(a, "fail_fast", False)
    if a.preflight:
        pf = run_preflight(spec)
        if pf["repairs"] and a.repair and not pf["errors"]:
            spec = preflight.repaired(spec, pf["repairs"])
            fastpath.count("repairs_applied", len(pf["repairs"]))
            outdir = a.out or "."
            os.makedirs(outdir, exist_ok=True)
            repaired_path = os.path.join(outdir, slug(name) + ".repaired.json")
            with open(repaired_path, "w", encoding="utf-8") as f:
                json.dump(spec, f, indent=1)
        blocking = pf["errors"] + ([] if (a.repair or not pf["repairs"]) else [r[0] for r in pf["repairs"]])
        if blocking and fail_fast:
            fastpath.count("stopped_at_preflight")
            if a.json:
                print(json.dumps({"valid": False, "stage": "preflight", "errors": blocking, "warnings": pf["warnings"],
                                  "repairs_available": [r[0] for r in pf["repairs"]]}, indent=1))
            else:
                print("=" * 70)
                print("RESULT: INVALID (preflight)")
                print("=" * 70)
                print_preflight(pf)
            return 2
    req = {"mode": "build", "spec": spec, "audit": a.audit}
    with fastpath.stage("PoB build + calculation"):
        if a.preflight and not pf["names_checked"] and not fastpath.legacy():
            # warm the name index in the same PoB start-up, so the next preflight checks names too
            names_resp, resp = fastpath.run_many([{"mode": "names"}, req], workers=1)
            preflight.store_names(names_resp)
        else:
            resp = run_driver(req)
    resp["preflight"] = {k: pf[k] for k in ("errors", "warnings", "names_checked")}
    resp["preflight"]["repairs_applied"] = [r[0] for r in pf["repairs"]] if repaired_path else []
    if repaired_path:
        resp["preflight"]["repaired_spec"] = repaired_path
    if not a.json and (pf["errors"] or pf["warnings"] or pf["repairs"]):
        # shown together with PoB's own findings, so one revision fixes everything
        print_preflight({"warnings": pf["warnings"], "repairs": pf["repairs"],
                         "errors": [e + " [preflight]" for e in pf["errors"]]}, repaired_path)
    return finish_build_like(a, resp, name, spec.get("profile"), requirements=spec.get("requirements"),
                             assumptions=spec.get("assumptions"), gear_basis=spec.get("gear_basis"), spec=spec)


def cmd_check(a):
    xml = read_build_input(a.build)
    req = None
    if getattr(a, "requirements", None):
        import preflight
        import requirements as rq
        loaded = load_spec(a.requirements)
        req = loaded.get("requirements", loaded) if isinstance(loaded, dict) else loaded
        raw = preflight.load_names()
        errs = [e for e in rq.check_spec({"requirements": req}, preflight.Names(raw) if raw else None)
                if not e.startswith(rq.PREFIX)]
        if errs:
            print("REQUIREMENTS FILE ERRORS:\n  - " + "\n  - ".join(errs))
            return 2
    resp = run_driver({"mode": "check", "xml": xml, "audit": a.audit})
    return finish_build_like(a, resp, a.name or "checked_build", None, requirements=req)


def enforce_requirements(resp, req, xml=None):
    """Check PoB's result (and the exported XML) against the user's requirements. A violation is an
    error: the build is invalid and never exported as the valid version."""
    import preflight
    import requirements as rq
    if not req:
        return None
    raw = preflight.load_names()
    names = preflight.Names(raw) if raw else None
    violations, notes = rq.check_result(req, resp, names)
    export = rq.check_export(req, xml, names) if xml else []
    for v in violations + [e for e in export if e not in violations]:
        resp.setdefault("errors", []).append(rq.PREFIX + v)
    if violations or export:
        resp["valid"] = False
    resp["requirements"] = {"requested": req, "violations": violations, "export_violations": export, "notes": notes,
                            "export_checked": bool(xml)}
    return resp["requirements"]


def share_code(code, base, args, source):
    """Upload the final code to pobb.in; never raises. Returns the result dict (ok True/False)."""
    import pobb_in
    try:
        res = pobb_in.upload(code, timeout=getattr(args, "timeout", 30), verify=getattr(args, "verify", True))
    except pobb_in.UploadError as e:
        return {"ok": False, "error": str(e), "kind": e.kind, "status": e.status}
    if base:
        res["manifest"] = pobb_in.write_manifest(base, res, source)
    return res


def print_share(res, files):
    if res.get("ok"):
        print("\nPOBB.IN LINK: %s" % res["url"])
        print("  (%s)" % res.get("verification"))
    else:
        print("\nPOBB.IN UPLOAD FAILED: %s" % res.get("error"))
        print("  Nothing was shared. The local build is unchanged: %s. Use the PoB import code instead."
              % (", ".join(files) if files else "no local files"))


def _cfg_value(e):
    v = e.get("value", True)
    return "true" if v is True else str(v)


def pin_fill(spec, resp):
    """The spec with its tree.fill result pinned: the nodes the fill chose are listed explicitly (by id)
    and fill is switched off, so later runs (compare, tree-audit, candidates) work on exactly this
    tree and pay no fill cost. Returns None when the response holds no fill result."""
    fill = resp.get("fill") if isinstance(resp, dict) else None
    if not fill or not fill.get("added"):
        return None
    ids = (resp.get("tree") or {}).get("allocated_ids")
    if not ids:
        return None
    out = json.loads(json.dumps(spec))
    tree = out.setdefault("tree", {})
    # the whole tree, exactly as built (targets, travel and fill); nodes inside cluster jewels (ids >= 65536) exist
    # only after the jewels load and stay in tree.cluster
    tree["allocated"] = [int(i) for i in ids if int(i) < 65536]
    # notables the fill chose become named targets too, so a tree-editing variant (which drops 'allocated'
    # and re-fills) keeps them and the masteries that hang off them; small nodes stay re-fillable
    nodes = list(tree.get("nodes") or [])
    fill_nodes = []
    for x in fill["added"]:
        if x.get("type") == "Notable" and x.get("id") is not None and int(x["id"]) not in nodes:
            nodes.append(int(x["id"]))
            fill_nodes.append(int(x["id"]))
    tree["nodes"] = nodes
    # what the fill contributed, so a tree-editing variant can undo it and let the fill decide again
    tree["_fill_added"] = {"nodes": fill_nodes, "masteries": [m.get("node_id") for m in fill.get("masteries") or []],
                           "config": [e["var"] for e in fill.get("enabled_config") or []]}
    for e in fill.get("enabled_config") or []:  # conditions the fill switched on with their source
        out.setdefault("config", {})[e["var"]] = e.get("value", True)
    # conditions switched on from sources already in the build: pinned too, so compare/candidates/sweep on this
    # file start from the same configuration the fill measured against
    for e in (resp.get("inferred_config") or {}).get("set") or []:
        out.setdefault("config", {}).setdefault(e["var"], e.get("value", True))
    for m in fill.get("masteries") or []:        # mastery effects the fill chose: pinned as ordinary mastery entries
        entry = {"mastery": m.get("mastery"), "effect": m.get("text"), "node": m.get("node_id")}
        if entry not in (tree.get("masteries") or []):
            tree.setdefault("masteries", []).append(entry)
    # fill stays: ignored while 'allocated' is present, active again when a variant edits the tree
    tree["_fill_pinned"] = "%d nodes (%d points) were chosen by tree.fill (%s): %s. 'allocated' reproduces the tree exactly; " \
        "'nodes' and 'masteries' stay for readability and are already inside it" % (
            len(fill["added"]), fill.get("points_added", 0), fill.get("objective", "?"),
            ", ".join("%s (+%d)" % (x["name"], x["cost"]) for x in fill["added"]))
    return out


def finish_build_like(a, resp, name, spec_profile=None, requirements=None, assumptions=None, gear_basis=None, spec=None):
    quality = profile = None
    basis_changes = []
    if gear_basis:
        import candidates as cd
        basis_changes = cd.signature_changes(gear_basis, (resp.get("build_profile") or {}).get("signature"))
        if basis_changes:
            resp.setdefault("notes", []).append(
                "Gear basis changed since the equipment was chosen (%s): re-run `candidates` and re-check items whose "
                "reason depended on what changed; keep the ones whose assumptions still hold" % "; ".join(basis_changes))
    resp["gear_basis_changes"] = basis_changes
    # the user's requirements are checked on PoB's result and on the XML about to be exported
    req_report = enforce_requirements(resp, requirements, resp.get("xml"))
    if resp.get("audit"):
        import audit as qa
        with fastpath.stage("quality audit (thresholds)"):
            profile = qa.resolve_profile(spec_profile, {"investment": a.investment, "content": a.content, "mode": a.game_mode},
                                         resp.get("level"))
            quality = qa.evaluate(resp, profile)
    code, files = None, []
    xml = resp.pop("xml", None)
    resp.pop("cache", None)
    if xml:
        with fastpath.stage("export (files, PoB code)"):
            code = encode_code(xml)
            outdir = a.out or "."
            os.makedirs(outdir, exist_ok=True)
            base = os.path.join(outdir, slug(name))
            # an invalid result never overwrites the last valid export of the same build
            stem = base if resp.get("valid") else base + ".invalid"
            with open(stem + ".xml", "w", encoding="utf-8") as f:
                f.write(xml)
            with open(stem + ".pob.txt", "w", encoding="utf-8") as f:
                f.write(code + "\n")
            files = [stem + ".xml", stem + ".pob.txt"]
            pinned = pin_fill(spec, resp) if (spec and resp.get("valid")) else None
            if pinned:
                with open(base + ".filled.json", "w", encoding="utf-8") as f:
                    json.dump(pinned, f, indent=1, ensure_ascii=False)
                files.append(base + ".filled.json")
                resp.setdefault("notes", []).append(
                    "Tree fill pinned in %s.filled.json (fill's nodes listed by id, fill off): use that file for compare, "
                    "tree-audit and candidates so variants test one idea on this exact tree" % os.path.basename(base))
    share = stale = None
    if code and resp.get("valid"):
        import pobb_in
        stale = pobb_in.stale_link(base, code)
    if getattr(a, "share", False):
        if not code or not resp.get("valid"):
            share = {"ok": False, "kind": "invalid_build",
                     "error": "the build is INVALID, so it was not uploaded; fix the errors first"}
        else:
            share = share_code(code, base, a, files[1])
    if a.json:
        resp["pob_code"] = code
        resp["files"] = files
        if quality is not None:
            resp["profile"] = profile
            resp["quality"] = [{"area": x[0], "status": x[1], "message": x[2], "severity": x[3], "priority": x[4]}
                               for x in quality]
            resp["quality_summary"] = qa.summary(quality, resp.get("errors") or [])
        if stale:
            resp["stale_share_url"] = stale
        if share is not None:
            resp["share"] = share
        print(json.dumps(resp, indent=1))
    else:
        print_report(resp, None, files)
        if req_report is not None:
            import requirements as rq
            bad = req_report["violations"] + req_report["export_violations"]
            print("\nUSER REQUIREMENTS (%s): %s" % (rq.describe(requirements),
                                                  "NOT MET -- see errors" if bad else "all met in PoB's result and in the exported build"))
            for n in req_report["notes"]:
                print("  - " + n)
        if assumptions:
            print("ASSUMPTIONS (inferred, not required by the user; not enforced): " + "; ".join(assumptions))
        if quality is not None:
            qa.print_audit(quality, profile, resp.get("errors") or [], getattr(a, "audit_detail", False))
        elif resp.get("audit_skipped"):
            print("\nQUALITY AUDIT skipped: %s." % resp["audit_skipped"])
        if code and a.show_code:
            print("\nPOB IMPORT CODE:")
            print(code)
        if stale:
            print("\nNOTE: the earlier pobb.in link %s is for a previous version of this build (marked stale in %s.pobbin.json)."
                  % (stale, os.path.basename(base)))
        if share is not None:
            print_share(share, files)
    if not resp.get("valid"):
        return 2
    return 3 if share is not None and not share.get("ok") else 0


def cmd_upload(a):
    import pobb_in
    src, base, text = a.build, None, a.build
    if re.match(r"^https?://", src.strip()):
        res = {"ok": False, "kind": "invalid_input", "error": "that is already a link; give a PoB code, a .pob.txt or an .xml file"}
    else:
        if len(src) < 4096 and os.path.exists(src):
            with open(src, encoding="utf-8", errors="replace") as f:
                text = f.read()
            base = re.sub(r"(\.pob\.txt|\.xml|\.txt)$", "", src)
        text = text.strip()
        try:
            code = encode_code(text) if text.startswith("<") else text
            res = share_code(code, base, a, src if base else "code argument")
        except ValueError as e:
            res = {"ok": False, "kind": "invalid_input", "error": "could not read the build: %s" % e}
    if a.json:
        print(json.dumps(res, indent=1))
    elif res.get("ok"):
        print(res["url"])
        print("verified: %s" % res.get("verification"))
        if res.get("manifest"):
            print("recorded in %s" % res["manifest"])
    else:
        print("UPLOAD FAILED (%s): %s" % (res.get("kind"), res.get("error")), file=sys.stderr)
        if base:
            print("The local build %s is unchanged." % src, file=sys.stderr)
    return 0 if res.get("ok") else 1


# ---------------------------------------------------------------------------
# Variations: apply small edits to a spec and compare PoB results side by side
# ---------------------------------------------------------------------------
def _norm(x):
    return (x["name"] if isinstance(x, dict) else str(x)).strip().lower()


def _find_group(spec, sel):
    groups = spec.get("skills") or []
    if sel in (None, "main"):
        for g in groups:
            if g.get("main"):
                return g
        raise ValueError("no main skill group")
    if isinstance(sel, int):
        return groups[sel - 1]          # 1-based, same numbering as the report
    for g in groups:
        if any(_norm(x) == str(sel).lower() for x in g.get("gems", [])):
            return g
    raise ValueError("skill group %r not found" % sel)


def apply_variant(spec, v):
    """Return a modified deep copy of spec. Keys (all optional):
    set {key: value}            top-level spec keys (level, bandit, pantheon_*, ascendancy...)
    config {var: value|null}    merged into config; null resets to PoB default
    tree_add / tree_remove      node names or ids
    ascendancy_nodes [..]       replace tree.ascendancy
    masteries_add [{mastery, effect}], masteries_remove [effect substring]
    gems [{group: "main"|n|"gem name", replace: {old: new}, add: [..], remove: [..], set: [..]}]
    skills_add [group], skills_remove [group selector]
    items [{slot, ...item entry...} | {slot, remove: true}]
    """
    s = json.loads(json.dumps(spec))
    for k, val in (v.get("set") or {}).items():
        import preflight as _pf
        if "." in k or "[" in k or k not in _pf.KNOWN_KEYS:
            raise ValueError("set: %r is not a top-level spec key (set only changes level, bandit, pantheon_*, ... ; "
                             "use gems/items/tree_add/config for anything inside skills, items, tree or config)" % k)
        s[k] = val
    if "config" in v:
        cfg = s.setdefault("config", {})
        for k, val in v["config"].items():
            if val is None:
                cfg.pop(k, None)
            else:
                cfg[k] = val
    tree = s.setdefault("tree", {})
    nodes = tree.setdefault("nodes", [])
    if "allocated" in tree and any(k in v for k in ("tree_add", "tree_remove", "ascendancy_nodes", "masteries_add",
                                                     "masteries_remove")) or (v.get("set") or {}).get("level"):
        # a pinned tree can't be edited by name: re-derive it from the targets, undoing what the fill added
        # (its notables, masteries and config switches) so tree.fill decides again on the edited tree
        tree.pop("allocated", None)
        tree.pop("_fill_pinned", None)
        fa = tree.pop("_fill_added", None) or {}
        nodes[:] = [n for n in nodes if not (isinstance(n, int) and n in (fa.get("nodes") or []))]
        tree["masteries"] = [m for m in tree.get("masteries") or [] if not (isinstance(m, dict) and m.get("node") in (fa.get("masteries") or []))]
        for var in fa.get("config") or []:
            s.get("config", {}).pop(var, None)
    for n in v.get("tree_remove") or []:
        before = len(nodes)
        nodes[:] = [x for x in nodes if str(x).lower() != str(n).lower()]
        if len(nodes) == before:
            raise ValueError("tree_remove: %r is not in tree.nodes" % n)
    nodes.extend(v.get("tree_add") or [])
    if "ascendancy_nodes" in v:
        tree["ascendancy"] = v["ascendancy_nodes"]
    ms = tree.setdefault("masteries", [])
    for e in v.get("masteries_remove") or []:
        ms[:] = [m for m in ms if e.lower() not in m.get("effect", "").lower()]
    ms.extend(v.get("masteries_add") or [])
    for gv in v.get("gems") or []:
        g = _find_group(s, gv.get("group"))
        gems = g.setdefault("gems", [])
        if "set" in gv:
            gems[:] = gv["set"]
        for old, new in (gv.get("replace") or {}).items():
            idx = [i for i, x in enumerate(gems) if _norm(x) == old.lower()]
            if not idx:
                raise ValueError("gems.replace: %r not in that group" % old)
            gems[idx[0]] = new
        for r in gv.get("remove") or []:
            gems[:] = [x for x in gems if _norm(x) != r.lower()]
        gems.extend(gv.get("add") or [])
    for sel in v.get("skills_remove") or []:
        g = _find_group(s, sel)
        s["skills"].remove(g)
    s.setdefault("skills", []).extend(v.get("skills_add") or [])
    items = s.setdefault("items", [])
    for it in v.get("items") or []:
        slot = it["slot"].lower()
        items[:] = [x for x in items if x.get("slot", "").lower() != slot]
        if not it.get("remove"):
            items.append(it)
    return s


def _summ(resp, req=None):
    sm = resp.get("summary") or {}
    tree = resp.get("tree") or {}
    viol, notes = [], []
    if req:
        import preflight
        import requirements as rq
        raw = preflight.load_names()
        viol, notes = rq.check_result(req, resp, preflight.Names(raw) if raw else None)
    return {"requirement_violations": viol, "requirement_notes": notes,
            "valid": bool(resp.get("valid")), "errors": resp.get("errors") or [], "dps": sm.get("dps"), "ehp": sm.get("ehp"),
            "life": sm.get("life"), "es": sm.get("es"), "min_ele_res": sm.get("min_ele_res"), "chaos_res": sm.get("chaos_res"),
            "min_max_hit": sm.get("min_max_hit"), "points_used": tree.get("points_used"),
            "points_available": tree.get("points_available_at_level"), "main_skill_disabled": sm.get("main_skill_disabled"),
            "fill": bool(resp.get("fill"))}


def _run_specs(specs, audit=False, workers=2, preflight_check=True):
    """Evaluate specs with as few PoB start-ups as possible. Specs that fail the preflight (or
    could not be built from the variant) are not calculated at all."""
    out = [None] * len(specs)
    todo, reqs = [], []
    for i, sp in enumerate(specs):
        if isinstance(sp, Exception):
            out[i] = {"valid": False, "errors": [str(sp)], "skipped": "variant could not be applied"}
            continue
        if preflight_check:
            import requirements as rq
            pf = run_preflight(sp)
            # a candidate that breaks a user requirement is still calculated (so its numbers can be shown)
            # and then rejected; anything else that fails the preflight is not calculated at all
            blocking = [e for e in pf["errors"] if not e.startswith(rq.PREFIX)] + [r[0] for r in pf["repairs"]]
            if blocking:
                out[i] = {"valid": False, "errors": blocking, "skipped": "preflight", "requirement_violations": []}
                fastpath.count("candidates_skipped_preflight")
                continue
        todo.append(i)
        reqs.append({"mode": "build", "spec": sp, "audit": audit})
    fastpath.count("candidates_evaluated", len(reqs))
    with fastpath.stage("PoB candidate evaluations"):
        for i, resp in zip(todo, fastpath.run_many(reqs, workers=workers)):
            out[i] = _summ(resp, specs[i].get("requirements") if isinstance(specs[i], dict) else None)
    return out


def _requirements(spec):
    """Hard requirements a variant must not break, from the spec's profile and the audit thresholds."""
    import audit as qa
    prof = qa.resolve_profile(spec.get("profile") if isinstance(spec, dict) else None, {}, spec.get("level", 90))
    th = qa.load_thresholds()
    ct = qa._content_thresholds(th, prof)
    scale = qa._scale(th, prof)
    hit = min(v for k, v in ct["max_hit"].items() if k != "Chaos") * scale
    return {"min_ele_res": ct["min_ele_res"], "chaos_res": ct["min_chaos_res"], "min_max_hit": hit}, prof


def _verdict(r, base, req, gain, tol):
    # a user requirement outranks everything, including higher DPS or better defences
    if r.get("requirement_violations"):
        return "rejected: breaks user requirement (%s)" % "; ".join(r["requirement_violations"])
    if r.get("dps") is None:
        return "not evaluated (%s)" % (r.get("skipped") or "error")
    if not r["valid"]:
        return "invalid"
    broken, fixed = [], []
    for k, need in req.items():
        v, b = r.get(k), base.get(k)
        if v is None or b is None:
            continue
        if v < need and (b >= need or v < b):
            broken.append("%s %s < %s" % (k, fmt_num(v), fmt_num(round(need))))
        elif b < need <= v:
            fixed.append(k)
    if broken:
        return "rejected: breaks " + ", ".join(broken)
    if fixed:
        # meeting a requirement the original misses outranks any damage trade
        return "fixes requirement: " + ", ".join(fixed)
    d = [_pct(r[k], base[k]) or 0 for k in ("dps", "ehp", "min_max_hit")]
    if all(abs(x) < gain for x in d):
        return "no meaningful change"
    if any(x >= gain for x in d) and all(x >= -tol for x in d):
        return "better"
    if all(x <= tol for x in d):
        return "worse"
    return "trade-off"


def _pct(a, b):
    return None if not b or a is None else (a / b - 1) * 100


def cmd_candidates(a):
    """Equipment candidates for a spec: discovered from PoB's data, measured by PoB (capped), ranked here."""
    import candidates as cd
    import requirements as rq
    spec = load_spec(a.spec)
    if a.preflight:
        pf = run_preflight(spec)
        if pf["errors"]:
            print_preflight(pf)
            return 2
    user_req = rq.get(spec) or {}
    _, prof0 = _requirements(spec)
    rare_slots = [x.strip() for x in (a.rare_slots or "Weapon 1,Amulet").split(",") if x.strip()] if a.rare_slots != "none" else []
    opts = {"max_evals": a.max_evals, "item_level": a.item_level, "investment": prof0["investment"], "rare_slots": rare_slots, "rare_evals": a.rare_evals,
            "requirements": {"uniques": user_req.get("uniques") or [], "weapon_type": user_req.get("weapon_type")}}
    if a.trace:
        opts["trace"] = a.trace
    resp = run_driver({"mode": "build", "spec": spec, "candidates": opts})
    if not resp.get("valid") or not resp.get("candidates"):
        print("Candidates need a valid build first:")
        for e in (resp.get("errors") or []) + ([resp["candidates_skipped"]] if resp.get("candidates_skipped") else []):
            print("  - " + e)
        for w in resp.get("warnings") or []:
            if "andidate" in w:
                print("  - " + w)
        return 2
    cand = resp["candidates"]
    targets, prof = _requirements(spec)
    ranked = cd.rank(cand, targets, investment=prof["investment"], per_slot=a.per_slot)
    var = cd.variants(ranked, limit=a.variants)
    out = a.variants_out or (os.path.splitext(os.path.basename(a.spec))[0] + ".candidate_variants.json")
    if var:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(var, f, indent=1)
    sig = (cand.get("profile") or {}).get("signature")
    if a.json:
        print(json.dumps({"candidates": cand, "ranked": ranked, "variants": var, "variants_file": out if var else None,
                          "gear_basis": sig, "targets": targets,
                          "profile": {k: prof[k] for k in ("investment", "content", "mode", "level")}}, indent=1, default=str))
        return 0
    print(cd.format_report(cand, ranked))
    if var:
        print("\nVariants for `compare` (best shortlisted item of the top %d slots): %s" % (len(var), out))
    print("GEAR BASIS (record it in the spec as \"gear_basis\"; a later change of any of these is flagged for re-deriving): "
          + json.dumps(sig))
    return 0


def cmd_compare(a):
    spec = load_spec(a.spec)
    variants = load_spec(a.variants)
    if isinstance(variants, dict):
        variants = variants.get("variants") or [variants]
    dropped = variants[a.max_candidates:] if a.max_candidates and len(variants) > a.max_candidates else []
    variants = variants[:a.max_candidates] if a.max_candidates else variants
    import requirements as rq
    user_req = rq.get(spec)
    specs, names, tampered = [spec], ["original"], set()
    for i, v in enumerate(variants, 1):
        names.append(v.get("name") or "variant %d" % i)
        try:
            sp = apply_variant(spec, v)
            if json.dumps(sp.get("requirements"), sort_keys=True) != json.dumps(spec.get("requirements"), sort_keys=True):
                tampered.add(i)                    # a variant may not loosen what the user asked for
            if "requirements" in spec:
                sp["requirements"] = spec["requirements"]
            specs.append(sp)
        except (ValueError, KeyError, IndexError) as e:
            specs.append(ValueError("could not apply variant: %s" % e))
    res = _run_specs(specs, workers=a.workers, preflight_check=a.preflight)
    for i in tampered:
        res[i].setdefault("requirement_violations", []).insert(0, "the variant changes the user's requirements")
    base = res[0]
    req, prof = _requirements(spec)
    verdicts = ["baseline"] + [_verdict(r, base, req, a.min_gain, a.tolerance) if base.get("dps") else "no baseline"
                               for r in res[1:]]
    if base.get("requirement_violations"):
        verdicts[0] = "baseline (BREAKS USER REQUIREMENT: %s)" % "; ".join(base["requirement_violations"])
    # labels, never recommendations: config-only variants test an assumption, and a user-permitted
    # alternative main skill is shown separately for the user to choose
    for i, v in enumerate(variants, 1):
        if verdicts[i].startswith("rejected"):
            continue
        if set(v) <= {"name", "config"}:
            verdicts[i] = "assumption test (config only, not a build change): " + verdicts[i]
        elif res[i].get("requirement_notes"):
            verdicts[i] = "user-permitted alternative (%s): %s" % ("; ".join(res[i]["requirement_notes"]), verdicts[i])
    fixes = [(i, r) for i, (r, v) in enumerate(zip(res, verdicts)) if v.startswith("fixes requirement")]
    better = fixes or [(i, r) for i, (r, v) in enumerate(zip(res, verdicts)) if v == "better"]
    pick = max(better, key=lambda x: (verdicts[x[0]].count(",") if fixes else 0,
                                      _pct(x[1][a.objective], base[a.objective]) or 0))[0] if better else None
    if a.json:
        print(json.dumps({"requirements": req, "user_requirements": user_req,
                          "profile": {k: prof[k] for k in ("investment", "content", "mode", "level")},
                          "results": [dict(r, name=n, verdict=v) for n, r, v in zip(names, res, verdicts)],
                          "recommended": names[pick] if pick is not None else "original",
                          "not_evaluated_over_budget": [v.get("name") for v in dropped]}, indent=1))
        return 0
    print("%-34s %-7s %9s %7s %8s %7s %8s %6s %6s %6s  %s" % ("variant", "valid", "DPS", "dDPS%", "EHP", "dEHP%", "life", "minHit",
                                                               "eleRes", "pts", "verdict"))
    for n, r, v in zip(names, res, verdicts):
        if r.get("dps") is None:
            print("%-34s %-7s %s" % (n[:34], "SKIPPED" if r.get("skipped") else "ERROR", "; ".join(r["errors"])[:200]))
            continue
        dd, de = _pct(r["dps"], base.get("dps")), _pct(r["ehp"], base.get("ehp"))
        print("%-34s %-7s %9s %7s %8s %7s %8s %6s %6s %6s  %s" % (
            n[:34], "yes" if r["valid"] else "NO", fmt_num(r["dps"]), "" if dd is None or n == "original" else "%+.1f" % dd,
            fmt_num(r["ehp"]), "" if de is None or n == "original" else "%+.1f" % de, fmt_num(r["life"]),
            fmt_num(r["min_max_hit"]), fmt_num(r["min_ele_res"]), "%s/%s" % (r["points_used"], r["points_available"]), v))
        if not r["valid"]:
            for e in r["errors"][:4]:
                print("      error: " + e)
    print("\nRequirements kept (profile %s/%s/%s, level %d): lowest ele res >= %s, chaos res >= %s, min max hit >= %s."
          % (prof["investment"], prof["content"], prof["mode"], prof["level"], req["min_ele_res"], req["chaos_res"],
             fmt_num(round(req["min_max_hit"]))))
    if user_req:
        print("User requirements (a candidate that breaks one is rejected whatever its numbers): %s." % rq.describe(user_req))
    print("'better' = gains >= %.1f%% on DPS, EHP or min max hit while losing no more than %.1f%% on any of them."
          % (a.min_gain, a.tolerance))
    print("RECOMMENDED: %s" % (("%s (%s)" % (names[pick], "meets a requirement the original misses" if fixes else
                                             "highest %s among the 'better' variants" % a.objective)) if pick is not None
                               else "keep the original (no variant is better without a trade-off)"))
    if dropped:
        print("Not evaluated (over --max-candidates %d): %s. Raise the budget only if the evaluated ones point that way."
              % (a.max_candidates, ", ".join(v.get("name") or "?" for v in dropped)))
    return 0


def cmd_sweep(a):
    """Mechanic sweep: the standard mechanics this archetype could add (references/mechanics.json), each as one
    measured variant on the build as it stands, with sources the build's attributes can't meet reported as
    skipped rather than pursued."""
    import requirements as rq
    spec = load_spec(a.spec)
    with open(os.path.join(HERE, "..", "references", "mechanics.json"), encoding="utf-8") as f:
        catalogue = json.load(f)["mechanics"]
    base_resp = run_driver({"mode": "build", "spec": spec, "audit": True, "reference": True})   # reference: per-support values
    if not base_resp.get("valid"):
        print("The spec is not valid; fix it first:\n  " + "\n  ".join(base_resp.get("errors") or []))
        return 2
    prof = base_resp.get("build_profile") or {}
    flags = set()
    for k in ("skill_flags", "flags", "types"):
        for x in prof.get(k) or []:
            flags.add(str(x).lower())
    arche = (prof.get("signature") or {}).get("archetype") or ""
    for w in arche.replace(",", " ").split():
        flags.add(w.lower())
    ms = base_resp.get("main_skill_config") or {}
    if ms.get("melee") or "melee" in (prof.get("skill_tags") or []):
        flags.add("melee")
    # main skill gem tags are the most reliable source of flags
    for g in base_resp.get("skills") or []:
        if g.get("main"):
            for x in g.get("gems") or []:
                for t in x.get("tags") or []:
                    flags.add(str(t).lower())
    main_skill_tags = set()
    cfg = spec.get("config") or {}
    items = {i.get("slot"): i for i in spec.get("items") or []}
    bases_equipped = {(i.get("base") or "").lower() for i in base_resp.get("items") or []}
    stats = (base_resp.get("stats") or {}).get("offence") or {}
    audit = base_resp.get("audit") or {}
    supports = sorted(audit.get("main_supports") or [], key=lambda x: abs((x.get("without") or {}).get("dps_pct") or 0))
    weakest = supports[0]["gem"] if supports else None
    weakest2 = supports[1]["gem"] if len(supports) > 1 else None
    # the flask slot the audit found least useful: no measured benefit and no other effect it records
    weakest_flask = None
    for sl in audit.get("slots") or []:
        if str(sl.get("slot", "")).startswith("Flask") and sl.get("no_measured_benefit") and not sl.get("other_effects"):
            weakest_flask = sl["slot"]
            break
    # a slot with a free socket for a utility gem (armour pieces hold 4, the body 6; the main link is never used)
    free_slot = None
    used = {}
    for g in spec.get("skills") or []:
        used[g.get("slot")] = used.get(g.get("slot"), 0) + len(g.get("gems") or [])
    for sl, cap in (("Boots", 4), ("Gloves", 4), ("Helmet", 4), ("Body Armour", 6)):
        if any(g.get("main") and g.get("slot") == sl for g in spec.get("skills") or []):
            continue
        if used.get(sl, 0) < cap:
            free_slot = sl
            break
    guard = None
    for gd in (audit.get("guards") or []):
        guard = gd.get("gem") or gd.get("name")
    gem_groups = {}
    for gi, g in enumerate(spec.get("skills") or [], 1):
        for x in g.get("gems") or []:
            gem_groups[(x if isinstance(x, str) else x.get("name"))] = (gi, x)
    main_elem = None
    dmg = prof.get("damage_types") or {}
    if isinstance(dmg, dict) and dmg:
        main_elem = max(dmg, key=lambda k: dmg[k])
    elif isinstance(dmg, list) and dmg:
        main_elem = str(dmg[0]).capitalize()
    # crit flag from the calculation, melee from the skill types
    if (stats.get("CritChance") or 0) >= 30:
        flags.add("crit")
    for t in prof.get("skill_types") or []:
        flags.add(str(t).lower())
    for t in prof.get("skill_flags") or []:
        flags.add(str(t).lower())
    flags.add(str(prof.get("delivery") or "").lower())
    flags.add(str(prof.get("hit_or_dot") or "").lower())
    # keystones as flags (chaosinoculation, ...), and the delivery supports already in the main link
    for k in (base_resp.get("tree") or {}).get("keystones") or []:
        flags.add("".join(ch for ch in k.lower() if ch.isalpha()))
    for g in base_resp.get("skills") or []:
        if g.get("main"):
            for x in g.get("gems") or []:
                n = (x.get("name") or "").lower()
                if "mine" in n: flags.add("mine")
                if "trap" in n: flags.add("trap")
                if "totem" in n: flags.add("totem")
    flags.discard("")
    variants, skipped = [], []
    for m in catalogue:
        need = set(x.lower() for x in m.get("applies") or [])
        if need and not need <= flags:
            skipped.append((m["name"], "does not apply (build is %s; needs %s)" % (", ".join(sorted(flags)) or "?", ", ".join(sorted(need)))))
            continue
        if m.get("unless_flag") and any(f in flags for f in m["unless_flag"]):
            skipped.append((m["name"], "already a %s build" % "/".join(f for f in m["unless_flag"] if f in flags)))
            continue
        if m.get("requires_unreserved_mana_below") is not None:
            dstats = (base_resp.get("stats") or {}).get("defence") or {}
            mana, unres = dstats.get("Mana") or 0, dstats.get("ManaUnreserved")
            pct = (100.0 * unres / mana) if (mana and isinstance(unres, (int, float))) else None
            if pct is None or pct >= m["requires_unreserved_mana_below"]:
                skipped.append((m["name"], "unreserved mana %s%% is not below %d%%" % ("%.0f" % pct if pct is not None else "?", m["requires_unreserved_mana_below"])))
                continue
        if m.get("unless_config") and cfg.get(m["unless_config"]):
            skipped.append((m["name"], "already in the build (config %s set)" % m["unless_config"]))
            continue
        if m.get("requires_config") and not cfg.get(m["requires_config"]):
            skipped.append((m["name"], "needs %s first" % m["requires_config"]))
            continue
        if m.get("requires_item_base") and m["requires_item_base"].lower() not in bases_equipped:
            skipped.append((m["name"], "no %s equipped" % m["requires_item_base"]))
            continue
        if m.get("unless_item_base") and m["unless_item_base"].lower() in bases_equipped:
            skipped.append((m["name"], "%s already equipped (its own entry applies)" % m["unless_item_base"]))
            continue
        if m.get("unless_stat") and stats.get(m["unless_stat"]):
            skipped.append((m["name"], "already present (%s)" % m["unless_stat"]))
            continue
        if m.get("requires_gem_below_max"):
            gg = gem_groups.get(m["requires_gem_below_max"])
            lvl = gg[1].get("level") if gg and isinstance(gg[1], dict) else (20 if gg else None)
            if not gg or (lvl or 20) >= 20:
                skipped.append((m["name"], "%s not present below level 20" % m["requires_gem_below_max"]))
                continue
        v = json.loads(json.dumps(m["variant"]))
        text = json.dumps(v)
        if "$weakest_support2" in text:
            if not weakest2:
                skipped.append((m["name"], "fewer than two measured supports to replace"))
                continue
            text = text.replace("$weakest_support2", weakest2)
        if "$free_slot" in text:
            if not free_slot:
                skipped.append((m["name"], "no free socket in a utility slot"))
                continue
            text = text.replace("$free_slot", free_slot)
        if "$weakest_flask" in text:
            if not weakest_flask:
                skipped.append((m["name"], "every flask has a measured benefit or another effect"))
                continue
            text = text.replace("$weakest_flask", weakest_flask)
        if "$weakest_support" in text:
            if not weakest:
                skipped.append((m["name"], "no measured support to replace"))
                continue
            text = text.replace("$weakest_support", weakest)
        if "$guard" in text:
            if not guard:
                skipped.append((m["name"], "no guard skill to replace"))
                continue
            gg = gem_groups.get(guard)
            text = text.replace("$guard_group", str(gg[0]) if gg else "1").replace("$guard", guard)
            v = json.loads(text)
            for g in v.get("gems") or []:
                if isinstance(g.get("group"), str) and g["group"].isdigit():
                    g["group"] = int(g["group"])
            text = json.dumps(v)
        if "$main_element" in text:
            if not main_elem or main_elem.lower() not in ("fire", "cold", "lightning"):
                skipped.append((m["name"], "main damage type is not elemental"))
                continue
            text = text.replace("$main_element", main_elem)
        if "$capped_element" in text:
            dstats = (base_resp.get("stats") or {}).get("defence") or {}
            over = {e: dstats.get("%sResistOverCap" % e) for e in ("Fire", "Cold", "Lightning")}
            over = {e: v for e, v in over.items() if isinstance(v, (int, float))}
            if not over:
                skipped.append((m["name"], "no resistance figures in the calculation"))
                continue
            # a chaos-immune character (CI, or 100% chaos res) takes "phys as chaos" for free: strictly the best element
            if (dstats.get("ChaosResist") or 0) >= 100 or "chaosinoculation" in flags:
                text = text.replace("$capped_element", "Chaos")
            else:
                text = text.replace("$capped_element", max(over, key=lambda e: over[e]))
        if "$gem_group:" in text:
            import re as _re
            def _grp(mm):
                gg = gem_groups.get(mm.group(1))
                return str(gg[0]) if gg else "1"
            text = _re.sub(r'"\$gem_group:([^"]+)"', lambda mm: str(gem_groups.get(mm.group(1), (1,))[0]), text)
        v = json.loads(text)
        if m.get("id") == "mine_delivery":
            # supports that cannot apply to a mine go too, for the mine-friendly ones in order
            cast_only = ["spell echo", "greater spell echo", "awakened spell echo", "unleash", "greater unleash", "awakened unleash",
                         "cast while channelling", "awakened cast while channelling", "spell totem", "arcanist brand", "spell cascade", "greater spell cascade"]
            mine_friendly = ["Charged Mines", "Trap and Mine Damage", "Swift Assembly", "Minefield", "High-Impact Mine"]
            main = next((g for g in spec.get("skills") or [] if g.get("main")), None)
            used = set()
            for g2 in v.get("gems") or []:
                for _, new in (g2.get("replace") or {}).items():
                    used.add(new if isinstance(new, str) else new.get("name"))
            for x in (main or {}).get("gems") or []:
                n = x if isinstance(x, str) else x.get("name")
                if n and n.lower() in cast_only:
                    pick = next((f for f in mine_friendly if f not in used), None)
                    if pick:
                        used.add(pick)
                        v.setdefault("gems", [{"group": "main", "replace": {}}])
                        v["gems"][0].setdefault("replace", {})[n] = pick
        for it in v.get("items") or []:
            key = "$set_eater" if "$set_eater" in it else ("$set_exarch" if "$set_exarch" in it else None)
            if key:
                cur = items.get(it["slot"])
                if not cur or cur.get("unique") or "Rarity: UNIQUE" in (cur.get("text") or ""):
                    skipped.append((m["name"], "no rare %s equipped (eldritch implicits need a rare)" % it["slot"]))
                    v = None
                    break
                entry = json.loads(json.dumps(cur))
                kind = "Eater" if key == "$set_eater" else "Exarch"
                # raw item text may already carry that eldritch implicit as a line (a game import): take it out
                rep = next((r for r in base_resp.get("items") or [] if r.get("slot") == it["slot"]), None) or {}
                old_line = (rep.get("eldritch_lines") or {}).get(kind)
                if old_line and entry.get("text"):
                    lines = entry["text"].split("\n")
                    idx = next((i for i, l in enumerate(lines) if l.strip().lstrip("{eldritch}").strip() == old_line), None)
                    if idx is not None:
                        del lines[idx]
                        for i, l in enumerate(lines):
                            if l.startswith("Implicits: "):
                                try:
                                    lines[i] = "Implicits: %d" % max(0, int(l.split(":")[1]) - 1)
                                except ValueError:
                                    pass
                        entry["text"] = "\n".join(lines)
                entry["eater" if kind == "Eater" else "exarch"] = it.pop(key)
                it.clear()
                it.update(entry)
        if v is None:
            continue
        v["name"] = m["name"]
        v["_mechanic"] = m["id"]
        v["_note"] = m.get("note")
        variants.append(v)
    out = {"variants": variants, "skipped": skipped}
    path = a.variants_out or (os.path.splitext(os.path.basename(a.spec))[0] + ".sweep_variants.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([{k: val for k, val in v.items() if not k.startswith("_")} for v in variants], f, indent=1)
    print("MECHANIC SWEEP for %s (archetype flags: %s)" % (os.path.basename(a.spec), ", ".join(sorted(flags)) or "?"))
    for name, why in skipped:
        print("  skipped: %-70s %s" % (name[:70], why))
    if not variants:
        print("Nothing to measure.")
        return 0
    print("  measuring %d mechanics with compare (variants written to %s):" % (len(variants), path))
    ns = argparse.Namespace(spec=a.spec, variants=path, workers=a.workers, max_candidates=0, objective="dps", min_gain=1.0,
                            tolerance=1.0, preflight=True, json=False)
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_compare(ns)
    text = buf.getvalue()
    print(text)
    # sources the build's attributes can't meet: reported, never pursued
    blocked = []
    for line in text.splitlines():
        if "error: You do not meet the" in line and "requirement of" in line:
            blocked.append(line.strip().replace("error: ", ""))
    if blocked:
        # advisor rule 1: a lower gem level first. Retry each blocked gem at 16 / 12 / 8 / 4 in one compare and
        # report the highest level that fits; what still fails is not pursued (no off-tree attributes).
        import re as _re
        retry = []
        for v in variants:
            vt = json.dumps(v)
            for b in blocked:
                gem = _re.search(r"requirement of (.+)$", b)
                gem = gem.group(1).strip() if gem else None
                if gem and ('"%s"' % gem) in vt:
                    for lvl in (16, 12, 8, 4):
                        c = json.loads(vt.replace('"%s"' % gem, json.dumps({"name": gem, "level": lvl})))
                        c["name"] = "%s @ level %d" % (gem, lvl)
                        c = {k: val for k, val in c.items() if not k.startswith("_")}
                        retry.append(c)
        if retry:
            rpath = path.replace(".json", ".retry.json")
            with open(rpath, "w", encoding="utf-8") as f:
                json.dump(retry, f, indent=1)
            print("ATTRIBUTE-BLOCKED SOURCES retried at lower gem levels (advisor rule 1; no off-tree attributes are bought for them):")
            ns2 = argparse.Namespace(spec=a.spec, variants=rpath, workers=a.workers, max_candidates=0, objective="dps", min_gain=1.0,
                                     tolerance=1.0, preflight=True, json=False)
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                cmd_compare(ns2)
            for line in buf2.getvalue().splitlines():
                if line.startswith(("variant", "original")) or "@ level" in line:
                    print(line)
        print("NOT PURSUED at full level (attribute the build doesn't have): " + "; ".join(blocked))
    print("\nSweep rules: a source the build's attributes can't meet is reported, not pursued (meet attributes in the advisor's "
          "order only when the gain justifies it); a config switched on by a variant is listed with its source, and the audit's "
          "evidence line decides whether that uptime is honest. Notes:")
    for v in variants:
        if v.get("_note"):
            print("  - %s: %s" % (v["name"], v["_note"]))
    return rc


def cmd_tree_audit(a):
    import requirements as rq
    spec = load_spec(a.spec)
    targets = list((spec.get("tree") or {}).get("nodes") or [])
    if a.targets:
        wanted = {t.strip().lower() for t in a.targets.split(",") if t.strip()}
        targets = [t for t in targets if str(t).lower() in wanted]
    required = {str(p).lower() for p in ((rq.get(spec) or {}).get("passives") or [])}
    kept = [t for t in targets if str(t).lower() in required]
    targets = [t for t in targets if str(t).lower() not in required]
    specs = [spec] + [apply_variant(spec, {"tree_remove": [t]}) for t in targets]
    res = _run_specs(specs, workers=a.workers, preflight_check=False)
    base = res[0]
    if base.get("requirement_violations"):
        print("The original spec breaks a user requirement; fix it first:\n  " + "\n  ".join(base["requirement_violations"]))
        return 2
    if base.get("dps") is None or not base["valid"]:
        print("The original spec is not valid; fix it first:\n  " + "\n  ".join(base.get("errors") or []))
        return 2
    rows = []
    for t, r in zip(targets, res[1:]):
        if r.get("dps") is None or r.get("points_used") is None:
            rows.append((t, None, None, None, None, "; ".join(r.get("errors") or [])[:150]))
            continue
        freed = base["points_used"] - r["points_used"]
        dd, de = _pct(r["dps"], base["dps"]), _pct(r["ehp"], base["ehp"])
        note = "" if r["valid"] else "invalid without it: " + "; ".join(r["errors"])[:120]
        if r.get("requirement_violations"):
            note = "breaks user requirement without it: " + "; ".join(r["requirement_violations"])[:120]
        rows.append((t, freed, dd, de, (r["life"] or 0) - (base["life"] or 0), note))
    def value(row):
        if row[1] is None or not row[1]:
            return 1e9
        return (-(row[2] or 0) - (row[3] or 0)) / row[1]
    rows.sort(key=value)
    if a.json:
        print(json.dumps([{"node": r[0], "points_freed": r[1], "dps_pct": r[2], "ehp_pct": r[3], "life": r[4], "note": r[5]}
                          for r in rows] + [{"node": k, "points_freed": None, "dps_pct": None, "ehp_pct": None, "life": None,
                                             "note": "required by the user: not audited"} for k in kept], indent=1))
        return 0
    if kept:
        print("Not audited (required by the user): %s" % ", ".join(str(k) for k in kept))
    print("Removing each tree target (and the travel nodes only it needed), weakest value per point first.")
    print("Base: DPS %s, EHP %s, life %s, %s/%s points\n" % (fmt_num(base["dps"]), fmt_num(base["ehp"]), fmt_num(base["life"]),
                                                          base["points_used"], base["points_available"]))
    print("%-32s %6s %8s %8s %7s %9s  %s" % ("target", "freed", "DPS%", "EHP%", "life", "%/point", "note"))
    for t, freed, dd, de, life, note in rows:
        if freed is None:
            print("%-32s %s" % (str(t)[:32], "ERROR " + note))
            continue
        vp = value((t, freed, dd, de))
        print("%-32s %6d %8s %8s %7s %9s  %s" % (str(t)[:32], freed, "%+.1f" % dd, "%+.1f" % de, "%+d" % life,
                                                 "-" if vp >= 1e9 else "%.2f" % vp, note))
    if base.get("fill"):
        print("\nNOTE: this spec has tree.fill, so each removal's points were re-spent by the fill: 'freed' is the net "
              "change and DPS%/EHP% compare the target against the fill's best alternative use of its points. "
              "Run tree-audit on the plain spec (no fill, no 'allocated') to see raw point costs instead.")
    print("\n%/point = (DPS% + EHP% lost) / points freed. Low values are candidates to drop or reroute; check a")
    print("replacement with `compare` before changing the build. Attribute/requirement and mastery effects show up as 'invalid'.")
    return 0


def cmd_nodes(a):
    resp = run_driver({"mode": "nodes", "query": a.query, "class": a.cls, "ascendancy": a.ascendancy, "type": a.type,
                       "limit": a.limit, "include_ascendancies": a.all_ascendancies})
    if a.json:
        print(json.dumps(resp, indent=1))
        return 0
    print_results(resp, lambda r: "%6s  %-9s %-32s %s%s" % (
        r["id"], r["type"], r["name"] + (" [%s]" % r["ascendancy"] if r.get("ascendancy") else ""),
        ("dist %d  " % r["distance"]) if r.get("distance") is not None else "", " | ".join(r.get("stats") or [])))
    return 0


def cmd_masteries(a):
    resp = run_driver({"mode": "masteries", "query": a.query or "", "class": a.cls})
    if a.json:
        print(json.dumps(resp, indent=1))
        return 0

    def f(r):
        s = "%s:\n    - %s" % (r["mastery"], "\n    - ".join(r["effects"]))
        if a.cls and r.get("clusters"):
            s += "\n    clusters (allocate one of these notables to unlock that mastery node):"
            for c in r["clusters"][:a.limit]:
                s += "\n      dist %-4s %s" % (c.get("distance"), " / ".join(c["notables"]))
        return s
    print_results(resp, f)
    return 0


def cmd_gems(a):
    support = True if a.support else (False if a.active else None)
    resp = run_driver({"mode": "gems", "query": a.query, "support": support, "search_tags": a.tags, "limit": a.limit})
    if a.json:
        print(json.dumps(resp, indent=1))
        return 0
    print_results(resp, lambda r: "%-45s %s %s  [%s]%s%s" % (
        r["name"], r["color"], "support" if r["support"] else "active ", ",".join(r.get("tags") or []),
        "  stages" if r.get("stages") else "", ("  parts: " + " | ".join("%d %s" % (i, n) for i, n in enumerate(r["parts"], 1)))
        if r.get("parts") else ""))
    return 0


def cmd_uniques(a):
    resp = run_driver({"mode": "uniques", "query": a.query, "full": a.full, "exact": True, "limit": a.limit})
    if a.json:
        print(json.dumps(resp, indent=1))
        return 0

    def f(r):
        s = "%-36s %-28s %-12s req %s" % (r["name"], r.get("base"), r.get("type"), r.get("req_level"))
        if r.get("text"):
            s += "\n" + "\n".join("    " + l for l in r["text"].splitlines()) + "\n"
        return s
    print_results(resp, f)
    return 0


def cmd_bases(a):
    resp = run_driver({"mode": "bases", "query": a.query, "limit": a.limit})
    if a.json:
        print(json.dumps(resp, indent=1))
        return 0
    print_results(resp, lambda r: "%-30s %-12s %-10s lvl %-3s sockets %s  %s" % (
        r["name"], r.get("type"), r.get("subType") or "", (r.get("req") or {}).get("level", 0), r.get("socket_limit"),
        (r.get("implicit") or "").replace("\n", " / ")))
    return 0


def cmd_mods(a):
    resp = run_driver({"mode": "mods", "base": a.base, "ilvl": a.ilvl, "query": a.query or "", "influences": a.influence or [],
                       "eldritch": a.eldritch, "corrupted": a.corrupted, "crafted": not (a.eldritch or a.corrupted)})
    if a.json:
        print(json.dumps(resp, indent=1))
        return 0
    for e in resp.get("errors") or []:
        print("ERROR: " + e)
    b = resp.get("base")
    if b:
        print("%s (%s), item level %s; implicit: %s" % (b["name"], b["type"], b["item_level"], (b.get("implicit") or "none").replace("\n", " / ")))
    title = ("Searing Exarch / Eater of Worlds implicits (highest tier per group; not allowed on uniques or corrupted items)" if a.eldritch
             else "Corruption (Vaal) implicits for this item class (replace the item's implicits; also for corrupted uniques)" if a.corrupted
             else "Best available tier per affix group")
    print("\n%s:" % title)
    for r in resp.get("results") or []:
        print("  %-6s %-40s (tier lvl %s)" % (r["type"], r["best_tier"], r["best_tier_level"]))
    if resp.get("crafted"):
        print("\nBench crafts:")
        for r in resp["crafted"]:
            print("  %-6s %s" % (r["type"], r["text"]))
    return 0


def cmd_classes(a):
    resp = run_driver({"mode": "classes"})
    if a.json:
        print(json.dumps(resp, indent=1))
        return 0
    print("Tree version %s" % resp.get("tree_version"))
    for r in resp.get("results") or []:
        print("%-9s %s" % (r["class"], ", ".join(r["ascendancies"])))
    if resp.get("bloodlines"):
        print("Bloodlines / secondary ascendancies: " + ", ".join(resp["bloodlines"]))
    return 0


def cmd_config(a):
    resp = run_driver({"mode": "config", "query": a.query or ""})
    if a.json:
        print(json.dumps(resp, indent=1))
        return 0
    print_results(resp, lambda r: "%-38s %-8s %s%s" % (r["var"], r.get("type"), r.get("label") or "",
                                                      (" values=" + "|".join(r["values"])) if r.get("values") else ""))
    return 0


def cmd_decode(a):
    print(read_build_input(a.code))
    return 0


def cmd_encode(a):
    with open(a.xml, encoding="utf-8") as f:
        print(encode_code(f.read()))
    return 0


def add_share_args(s):
    s.add_argument("--share", action="store_true",
                   help="after a VALID build, upload the final PoB code to pobb.in and print the link (needs internet)")
    s.add_argument("--timeout", type=float, default=30, help="pobb.in request timeout in seconds")
    s.add_argument("--no-verify", dest="verify", action="store_false",
                   help="don't download the paste back to check it (the id is still checked against the content hash)")


def add_audit_args(s):
    s.add_argument("--no-audit", dest="audit", action="store_false",
                   help="skip the quality audit (config/flask/unique contributions, defence thresholds; ~5s more)")
    s.add_argument("--investment", choices=("league_starter", "budget", "medium", "high"), help="overrides spec.profile.investment")
    s.add_argument("--content", choices=("campaign", "mapping", "bossing", "uber"), help="overrides spec.profile.content")
    s.add_argument("--mode", dest="game_mode", choices=("softcore", "hardcore"), help="overrides spec.profile.mode")
    s.add_argument("--audit-detail", action="store_true", help="list every passed audit check, not just a one-line summary")


def _reference_request(arg, audit=True):
    """A driver request for a build given as a spec (.json) or as a PoB code / code file / .xml."""
    if arg.lower().endswith(".json") and os.path.exists(arg):
        spec = load_spec(arg)
        return {"mode": "build", "spec": spec, "audit": audit, "reference": True}, spec.get("profile")
    return {"mode": "check", "xml": read_build_input(arg), "audit": audit, "reference": True}, None


def _active_item_set_span(xml):
    """(start, end) of the active <ItemSet>'s contents, or None for a build without item sets.

    Flask counts and adjustments only look at the gear set PoB calculates with; other sets in the
    export are left exactly as they are."""
    sets = list(re.finditer(r'<ItemSet\b([^>]*)>(.*?)</ItemSet>', xml, re.S))
    if not sets:
        return None
    m = re.search(r'<Items\b[^>]*\bactiveItemSet="(\d+)"', xml)
    for st in sets:
        if m and re.search(r'\bid="%s"' % m.group(1), st.group(1)):
            return st.span(2)
    return sets[0].span(2)


def inactive_equipped_flasks(xml):
    """Flask slots of the active gear set that hold an item but are switched off (PoB imports flasks off)."""
    span = _active_item_set_span(xml)
    body = xml[span[0]:span[1]] if span else xml
    return [m.group(0) for m in re.finditer(r'<Slot [^>]*name="Flask [1-5]"[^>]*/>', body)
            if not re.search(r'itemId="0"', m.group(0)) and 'active="true"' not in m.group(0)]


def flasks_on(xml):
    """A copy with the active gear set's equipped flasks switched on. The original text and the other
    gear sets are not modified."""
    span = _active_item_set_span(xml)
    a, b = span if span else (0, len(xml))
    body = xml[a:b]
    for tag in inactive_equipped_flasks(xml):
        body = body.replace(tag, re.sub(r'\sactive="[^"]*"', "", tag).replace("/>", ' active="true"/>'))
    return xml[:a] + body + xml[b:]


def cmd_reference(a):
    """Normalised PoB summary of a reference build, optionally side by side with another build."""
    import audit as qa
    import reference as ra
    reqs, profiles, names = [], [], ["reference"]
    req, prof = _reference_request(a.build)
    reqs.append(req)
    profiles.append(prof)
    adjusted = None
    inactive = inactive_equipped_flasks(req["xml"]) if req["mode"] == "check" else []
    if getattr(a, "adjust", None) == "flasks-on" and req["mode"] == "check":
        if inactive:
            adjusted = {"mode": "check", "xml": flasks_on(req["xml"]), "audit": True, "reference": True}
            reqs.append(adjusted)
            profiles.append(None)
    if a.against:
        req2, prof2 = _reference_request(a.against)
        reqs.append(req2)
        profiles.append(prof2)
        names.append(os.path.basename(a.against))
    with fastpath.stage("PoB reference analysis"):
        resps = fastpath.run_many(reqs, workers=2)
    over = {"investment": a.investment, "content": a.content, "mode": a.game_mode}
    norms = []
    adj_norm = None
    for i, (resp, prof) in enumerate(zip(resps, profiles)):
        # the CLI profile describes the reference (and its adjusted copy); a generated spec keeps its own
        is_adjusted = adjusted is not None and reqs[i] is adjusted
        p = qa.resolve_profile(prof, over if (i == 0 or is_adjusted) else {}, resp.get("level"))
        n = ra.normalise(resp, p)
        if is_adjusted:
            n["adjustments"] = ["%d equipped flask(s) of the active gear set switched on (they were off in the build as given)"
                                % len(inactive)]
            adj_norm = n
        else:
            norms.append(n)
    hint = None
    if inactive and adj_norm is None:
        hint = ("%d equipped flask(s) of the active gear set are switched off (PoB imports them off). The numbers above are for that "
                "state. Add --adjust flasks-on to also measure them switched on; that is reported separately." % len(inactive))
    if a.json:
        out = {"reference": norms[0]}
        if adj_norm:
            out["adjusted"] = adj_norm
        if hint:
            out["note"] = hint
        if len(norms) > 1:
            verdict, reasons = ra.comparability(norms[0], norms[1])
            out.update({"against": norms[1], "comparability": {"verdict": verdict, "reasons": reasons}})
        print(json.dumps(out, indent=1))
        return 0 if norms[0]["facts"]["valid"] else 2
    ra.print_summary(norms[0], "REFERENCE BUILD (as given)")
    if hint:
        print("NOTE: " + hint)
    if adj_norm:
        print()
        ra.print_summary(adj_norm, "ADJUSTED COPY, NOT THE BUILD AS GIVEN: " + "; ".join(adj_norm["adjustments"]))
    if len(norms) > 1:
        print()
        ra.print_summary(norms[1], "COMPARED BUILD: " + names[1])
        ra.print_comparison(norms[0], norms[1], names[1])
    return 0 if norms[0]["facts"]["valid"] else 2


LOOKUP_COMMANDS = {"nodes", "masteries", "gems", "uniques", "bases", "mods", "classes", "config"}
PARSER = None


def cmd_batch(a):
    """Run lookup commands with a shared PoB start-up. Each command's request is recorded in a
    first pass, all requests run together, then each command prints its own output."""
    import io
    import contextlib
    import shlex
    text = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    parsed = []
    for l in lines:
        argv = shlex.split(l)
        if argv and argv[0] == "pob.py":
            argv = argv[1:]
        if not argv or argv[0] not in LOOKUP_COMMANDS:
            print("### %s\nERROR: batch runs lookup commands only (%s)" % (l, ", ".join(sorted(LOOKUP_COMMANDS))))
            parsed.append(None)
            continue
        try:
            parsed.append(PARSER.parse_args(argv))
        except SystemExit:
            print("### %s\nERROR: could not parse this command" % l)
            parsed.append(None)
    global run_driver
    real = run_driver
    recorded = []
    run_driver = lambda req, timeout=600: recorded.append(req) or {}
    try:
        for p in parsed:
            if p is not None:
                with contextlib.redirect_stdout(io.StringIO()):
                    try:
                        p.fn(p)
                    except Exception:
                        pass
        with fastpath.stage("PoB lookups (batched)"):
            results = iter(fastpath.run_many(recorded, workers=1))
        run_driver = lambda req, timeout=600: next(results)
        for l, p in zip(lines, parsed):
            if p is not None:
                print("### " + l)
                p.fn(p)
                print()
    finally:
        run_driver = real
    return 0


def cmd_warm(a):
    import preflight
    raw = preflight.load_names(allow_spawn=True)
    if not raw:
        print("Could not build the name index")
        return 1
    print("Name index ready: %d gems, %d uniques, %d passives, %d bases (PoB %s)" % (
        len(raw["gems"]), len(raw["uniques"]), len(raw["nodes"]), len(raw["bases"]), raw.get("tree_version")))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timing", action="store_true", help="print stage timings and PoB call counts to stderr")
    ap.add_argument("--no-cache", action="store_true", help="don't reuse cached PoB results")
    ap.add_argument("--legacy", action="store_true", help="one PoB process per request, no cache (for comparisons)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("build", help="Build from a JSON spec, validate, calculate, emit PoB code")
    s.add_argument("spec")
    s.add_argument("-o", "--out", help="output directory (default: current)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-code", dest="show_code", action="store_false", help="don't print the PoB code")
    add_audit_args(s)
    add_share_args(s)
    s.add_argument("--repair", action="store_true",
                   help="apply the unambiguous preflight repairs (gem levels/quality, gem name spelling) to a copy of the spec")
    s.add_argument("--no-preflight", dest="preflight", action="store_false",
                   help="skip the checks that run before Path of Building starts")
    s.add_argument("--fail-fast", action="store_true",
                   help="stop without running PoB when the preflight finds errors (default: report them with PoB's results)")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("check", help="Validate/calculate an existing build (PoB code, code file or .xml)")
    s.add_argument("build")
    s.add_argument("-o", "--out")
    s.add_argument("--name")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-code", dest="show_code", action="store_false")
    s.add_argument("--requirements", help="JSON file with the user's requirements (or a spec containing them); "
                                          "the build is invalid when one is not met")
    add_audit_args(s)
    add_share_args(s)
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("upload", help="Upload a PoB code, .pob.txt or .xml to pobb.in and print the link (needs internet)")
    s.add_argument("build", help="PoB export code, a .pob.txt file containing one, or a PoB .xml file")
    s.add_argument("--timeout", type=float, default=30, help="request timeout in seconds (default 30)")
    s.add_argument("--no-verify", dest="verify", action="store_false",
                   help="don't download the paste back to check it (the id must then equal the content hash)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_upload)

    s = sub.add_parser("sweep", help="Mechanic sweep: measure the standard mechanics this archetype could add (references/mechanics.json)")
    s.add_argument("spec")
    s.add_argument("--workers", type=int, default=2)
    s.add_argument("--variants-out", help="where to write the generated variants (default <spec>.sweep_variants.json)")
    s.set_defaults(fn=cmd_sweep)
    s = sub.add_parser("compare", help="Compare the spec with variations of it (PoB numbers side by side)")
    s.add_argument("spec")
    s.add_argument("variants", help="JSON list of variants (see apply_variant / references/quality-review.md)")
    s.add_argument("--workers", type=int, default=2, help="parallel PoB processes (each runs a batch)")
    s.add_argument("--max-candidates", type=int, default=8,
                   help="evaluate at most this many variants (default 8; 0 = no limit)")
    s.add_argument("--objective", choices=("dps", "ehp", "min_max_hit"), default="dps",
                   help="which gain picks the recommendation among 'better' variants (default dps)")
    s.add_argument("--min-gain", type=float, default=1.0, help="%% gain that counts as an improvement (default 1)")
    s.add_argument("--tolerance", type=float, default=1.0,
                   help="%% loss on DPS / EHP / min max hit still accepted in a 'better' variant (default 1)")
    s.add_argument("--no-preflight", dest="preflight", action="store_false")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_compare)

    s = sub.add_parser("candidates", help="Equipment candidates found from PoB's data and measured as drop-ins (capped)")
    s.add_argument("spec")
    s.add_argument("--max-evals", type=int, default=60, help="PoB evaluations at most (default 60)")
    s.add_argument("--per-slot", type=int, default=3, help="shortlist size per slot (default 3)")
    s.add_argument("--variants", type=int, default=4, help="compare variants to write (default 4)")
    s.add_argument("--variants-out", help="where to write them (default <spec>.candidate_variants.json)")
    s.add_argument("--item-level", type=int, default=84, help="item level for crafted affixes (default 84)")
    s.add_argument("--rare-evals", type=int, default=60, help="PoB evaluations for the rare templates' affix measurements (default 60, separate from --max-evals)")
    s.add_argument("--rare-slots", default="Weapon 1,Amulet",
                   help="slots to build a crafted rare template for, comma-separated (default 'Weapon 1,Amulet'; 'none' to skip)")
    s.add_argument("--trace", action="append", help="report where this item landed in the search (checking only)")
    s.add_argument("--no-preflight", dest="preflight", action="store_false")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_candidates)

    s = sub.add_parser("tree-audit", help="Value per point of each passive tree target (removes each and recalculates)")
    s.add_argument("spec")
    s.add_argument("--workers", type=int, default=2)
    s.add_argument("--targets", help="comma-separated subset of tree.nodes to test (default: all)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_tree_audit)

    s = sub.add_parser("reference", help="Analyse a reference build (PoB code / .xml): normalised PoB facts, supports, "
                                         "leaf passives, conditions; optionally side by side with --against")
    s.add_argument("build", help="PoB code, code file or .xml of the reference build")
    s.add_argument("--against", help="another build to compare: a spec .json (built) or a PoB code / .xml (checked)")
    s.add_argument("--investment", choices=("league_starter", "budget", "medium", "high"), help="the reference's budget, if known")
    s.add_argument("--content", choices=("campaign", "mapping", "bossing", "uber"), help="the reference's target content")
    s.add_argument("--mode", dest="game_mode", choices=("softcore", "hardcore"))
    s.add_argument("--adjust", choices=("flasks-on",),
                   help="also analyse a copy with the equipped flasks switched on; reported separately from the build as given")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_reference)

    s = sub.add_parser("batch", help="Run several lookup commands with one PoB start-up (one command per line)")
    s.add_argument("file", help="file with one lookup command per line (e.g. gems \"mirage\"), or - for stdin")
    s.set_defaults(fn=cmd_batch)

    s = sub.add_parser("warm", help="Build the cached name index used by the preflight (setup.sh runs this)")
    s.set_defaults(fn=cmd_warm)

    s = sub.add_parser("nodes", help="Search passive tree nodes by name or stat text")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--class", dest="cls", help="show distance from this class's start")
    s.add_argument("--ascendancy", help="include this ascendancy's nodes")
    s.add_argument("--all-ascendancies", action="store_true")
    s.add_argument("--type", help="Normal | Notable | Keystone | Mastery | Socket")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_nodes)

    s = sub.add_parser("masteries", help="List mastery effects (optionally filtered); with --class also which notables unlock each mastery node")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--class", dest="cls", help="show the clusters (notables) of each mastery type, nearest to this class first")
    s.add_argument("--limit", type=int, default=12, help="clusters to show per mastery type")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_masteries)

    s = sub.add_parser("gems", help="Search skill/support gems")
    s.add_argument("query", nargs="?", default="")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--support", action="store_true")
    g.add_argument("--active", action="store_true")
    s.add_argument("--tags", action="store_true", help="also match gem tags (e.g. 'cold', 'minion')")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_gems)

    s = sub.add_parser("uniques", help="Search unique items (exact name match prints full text)")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--full", action="store_true", help="also search mod text and print full item text")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_uniques)

    s = sub.add_parser("bases", help="Search item bases")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_bases)

    s = sub.add_parser("mods", help="List affixes that can roll on a base")
    s.add_argument("base")
    s.add_argument("--ilvl", type=int, default=86)
    s.add_argument("--query", help="filter text, e.g. 'life' or 'resistance'")
    s.add_argument("--influence", action="append", help="Shaper, Elder, Crusader, Redeemer, Hunter, Warlord (repeatable)")
    s.add_argument("--eldritch", action="store_true", help="list Searing Exarch / Eater of Worlds implicits instead (body, helmet, gloves, boots)")
    s.add_argument("--corrupted", action="store_true", help="list corruption (Vaal) implicits for the item class instead")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_mods)

    s = sub.add_parser("classes", help="List classes, ascendancies and bloodlines on the current tree")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_classes)

    s = sub.add_parser("config", help="List PoB configuration options (for the spec's config block)")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_config)

    s = sub.add_parser("decode", help="PoB code -> XML")
    s.add_argument("code")
    s.set_defaults(fn=cmd_decode)

    s = sub.add_parser("encode", help="XML file -> PoB code")
    s.add_argument("xml")
    s.set_defaults(fn=cmd_encode)

    return ap


def main():
    if hasattr(signal, "SIGPIPE"):  # quiet exit when output is piped into `head`
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    global PARSER
    PARSER = ap = build_parser()
    a = ap.parse_args()
    if a.timing:
        os.environ["POE1_BUILDS_TIMING"] = "1"
    if a.no_cache:
        os.environ["POE1_BUILDS_NO_CACHE"] = "1"
    if a.legacy:
        os.environ["POE1_BUILDS_LEGACY"] = "1"
    sys.exit(a.fn(a))


if __name__ == "__main__":
    main()
