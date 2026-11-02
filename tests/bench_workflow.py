"""Measure the build workflow of two versions of this skill on the same PoB install.

    python3 tests/bench_workflow.py --old <dir with old scripts/> --new <dir with new scripts/> [--out results.json]

Each scenario is a fixed sequence of CLI commands, like the ones an agent runs for that kind of
request. A wrapper around LuaJIT counts every PoB process. Each version gets its own empty
POB_HOME cache (the real PoB files are symlinked), except that the new version's preflight name
index is built once beforehand, as setup.sh does; that one-time cost is reported separately.
Nothing here estimates anything: times are wall-clock measurements of the actual commands.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

REAL_HOME = os.environ.get("POB_HOME", os.path.expanduser("~/.cache/poe1-pob"))
HERE = os.path.dirname(os.path.abspath(__file__))


def make_home(root):
    """A POB_HOME that shares the real PoB files but counts LuaJIT launches and has its own cache."""
    home = os.path.join(root, "pobhome")
    os.makedirs(os.path.join(home, "luajit", "bin"))
    for name in ("pob", "luamods"):
        os.symlink(os.path.join(REAL_HOME, name), os.path.join(home, name))
    real = os.path.join(REAL_HOME, "luajit", "bin", "luajit")
    wrapper = os.path.join(home, "luajit", "bin", "luajit")
    with open(wrapper, "w") as f:
        f.write('#!/bin/sh\necho x >> "%s"\nexec "%s" "$@"\n' % (os.path.join(root, "launches.txt"), real))
    os.chmod(wrapper, 0o755)
    return home


def launches(root):
    p = os.path.join(root, "launches.txt")
    return sum(1 for _ in open(p)) if os.path.exists(p) else 0


def run(version_dir, home, root, argv, stdin=None, timing_log=None):
    env = dict(os.environ, POB_HOME=home)
    env.pop("POE1_BUILDS_TIMING", None)
    if timing_log:
        env["POE1_BUILDS_TIMING_LOG"] = timing_log
    t = time.time()
    p = subprocess.run([sys.executable, os.path.join(version_dir, "scripts", "pob.py")] + argv, env=env, cwd=root,
                       input=stdin, capture_output=True, text=True)
    return time.time() - t, p


def summary_of(out):
    try:
        d = json.loads(out)
    except ValueError:
        return None
    s = d.get("summary") or {}
    return {"valid": d.get("valid"), "dps": s.get("dps"), "ehp": s.get("ehp"), "life": s.get("life"),
            "min_max_hit": s.get("min_max_hit"), "errors": len(d.get("errors") or [])}


def scenarios(data):
    """Each scenario: list of steps (label, argv, stdin, kind). kind 'build' = a build attempt."""
    ex = data["examples"]
    lookups = ['gems "burning arrow"', 'gems mirage --support', 'uniques "Kaom\'s Heart"',
               'nodes "Hired Killer" --class Ranger', 'mods "Slink Gloves" --eldritch', 'config frenzy']
    return {
        "S1 straightforward (6 lookups + build)": {
            "old": [("lookup " + l, l, None, "lookup") for l in lookups] + [("build", "build %s -o out --no-code" % ex, None, "build")],
            "new": [("batch lookups", "batch -", "\n".join(lookups), "lookup"), ("build", "build %s -o out --no-code" % ex, None, "build")],
            "final": "build %s -o out --no-code --json --no-audit" % ex,
        },
        "S2 attribute + socket problem (fail, fix, rebuild)": {
            "old": [("build (broken)", "build %s -o out --no-code" % data["s2_broken"], None, "build"),
                    ("build (fixed)", "build %s -o out --no-code" % ex, None, "build")],
            "new": [("build (broken)", "build %s -o out --no-code" % data["s2_broken"], None, "build"),
                    ("build (fixed)", "build %s -o out --no-code" % ex, None, "build")],
            "final": "build %s -o out --no-code --json --no-audit" % ex,
        },
        "S3 targeted repair (gem level/quality/name)": {
            "old": [("build (needs repair)", "build %s -o out --no-code" % data["s3_repair"], None, "build"),
                    ("build (hand-fixed)", "build %s -o out --no-code" % data["s3_fixed"], None, "build")],
            "new": [("build --repair", "build %s -o out --no-code --repair" % data["s3_repair"], None, "build")],
            "final": "build %s -o out --no-code --json --no-audit" % data["s3_fixed"],
        },
        "S4 optimisation (2 compares, tree-audit, final build)": {
            "old": [("compare supports", "compare %s %s" % (ex, data["var_sup"]), None, "compare"),
                    ("compare tree", "compare %s %s" % (ex, data["var_tree"]), None, "compare"),
                    ("tree-audit", "tree-audit %s" % ex, None, "compare"),
                    ("final build", "build %s -o out --no-code" % ex, None, "build")],
            "new": [("compare supports", "compare %s %s --max-candidates 0" % (ex, data["var_sup"]), None, "compare"),
                    ("compare tree", "compare %s %s --max-candidates 0" % (ex, data["var_tree"]), None, "compare"),
                    ("tree-audit", "tree-audit %s" % ex, None, "compare"),
                    ("final build", "build %s -o out --no-code" % ex, None, "build")],
            "final": "build %s -o out --no-code --json --no-audit" % ex,
        },
        "S5 fails validation": {
            "old": [("build (invalid)", "build %s -o out --no-code" % data["s5_invalid"], None, "build")],
            "new": [("build (invalid)", "build %s -o out --no-code" % data["s5_invalid"], None, "build")],
            "final": "build %s -o out --no-code --json --no-audit" % data["s5_invalid"],
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--data", required=True, help="JSON with spec/variant paths for the scenarios")
    ap.add_argument("--out")
    ap.add_argument("--only", help="run only scenarios whose name starts with this")
    a = ap.parse_args()
    with open(a.data) as f:
        data = json.load(f)
    results = {}
    for name, sc in scenarios(data).items():
        if a.only and not name.startswith(a.only):
            continue
        results[name] = {}
        for ver in ("old", "new"):
            root = tempfile.mkdtemp(prefix="bench_%s_" % ver)
            home = make_home(root)
            vdir = getattr(a, ver)
            setup_s = None
            if ver == "new":
                setup_s, _ = run(vdir, home, root, ["warm"])
                os.remove(os.path.join(root, "launches.txt"))
            log = os.path.join(root, "timing.jsonl")
            steps, builds = [], 0
            total = 0.0
            for label, cmd, stdin, kind in sc[ver]:
                dt, p = run(vdir, home, root, shlex.split(cmd), stdin=stdin, timing_log=log if ver == "new" else None)
                total += dt
                builds += kind == "build"
                steps.append({"step": label, "seconds": round(dt, 2), "exit": p.returncode})
            n_launch = launches(root)
            # the final build's numbers, for the consistency check (not counted in the timings)
            _, fp = run(vdir, home, root, (["--no-cache"] if ver == "new" else []) + shlex.split(sc["final"]))
            counters = {}
            if ver == "new" and os.path.exists(log):
                for line in open(log):
                    d = json.loads(line)
                    for k, v in d.get("counters", {}).items():
                        counters[k] = counters.get(k, 0) + v
                    counters["cache_hits"] = counters.get("cache_hits", 0) + d.get("cache_hits", 0)
                    counters["pob_requests"] = counters.get("pob_requests", 0) + d.get("pob_requests", 0)
            results[name][ver] = {"total_seconds": round(total, 2), "pob_processes": n_launch, "build_attempts": builds,
                                  "retries": builds - 1, "steps": steps, "final": summary_of(fp.stdout),
                                  "counters": counters, "one_time_setup_seconds": round(setup_s, 2) if setup_s else None}
            shutil.rmtree(root, ignore_errors=True)
            print("%-52s %-3s %7.1fs  PoB processes %3d  build attempts %d  final %s" % (
                name, ver, total, n_launch, builds, results[name][ver]["final"]), flush=True)
    if a.out:
        with open(a.out, "w") as f:
            json.dump(results, f, indent=1)


if __name__ == "__main__":
    main()
