"""Helpers to compare two driver responses for the same build, ignoring nondeterministic XML ordering."""
import json
import re
import xml.etree.ElementTree as ET


def _attr(v):
    # PoB writes some id lists (tree nodes, mastery effects) in hash-table order
    if re.fullmatch(r"[\d,]+", v) and "," in v:
        return ",".join(sorted(v.split(",")))
    if re.fullmatch(r"(\{\d+,\d+\},?)+", v):
        return ",".join(sorted(re.findall(r"\{\d+,\d+\}", v)))
    return v


def canon_xml(text):
    def c(e):
        return "<%s %s>%s%s</>" % (e.tag, " ".join("%s=%r" % (k, _attr(v)) for k, v in sorted(e.attrib.items())),
                                   "\n".join(sorted((e.text or "").strip().splitlines())), "".join(sorted(c(x) for x in e)))
    return c(ET.fromstring(text))


IGNORE = {"xml", "timing", "startup_seconds", "recalcs", "cache", "preflight"}


def same_response(a, b):
    """Return a list of differing keys (empty = equivalent)."""
    diffs = []
    for k in sorted(set(a) | set(b)):
        if k in IGNORE:
            continue
        if json.dumps(a.get(k), sort_keys=True) != json.dumps(b.get(k), sort_keys=True):
            diffs.append(k)
    if (a.get("xml") is None) != (b.get("xml") is None):
        diffs.append("xml")
    elif a.get("xml") and canon_xml(a["xml"]) != canon_xml(b["xml"]):
        diffs.append("xml")
    return diffs
