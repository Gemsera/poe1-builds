"""Running the PoB driver efficiently: batching, a result cache, and optional timing.

- Batching: several requests share one PoB start-up (driver mode "batch"); start-up is
  ~2.7 s of every call, so this is where most of the time goes.
- Cache: identical requests return the stored response. The key covers the whole request
  plus the PoB release + git commit and the driver source, so a PoB update or a driver change
  never reuses an old answer. Only complete answers are stored: lookups without errors and
  VALID builds. Invalid builds, crashes and partial results are never cached.
  Disable with POE1_BUILDS_NO_CACHE=1 (or pob.py --no-cache).
- Timing: POE1_BUILDS_TIMING=1 (or pob.py --timing) prints a stage summary to stderr;
  POE1_BUILDS_TIMING_LOG=<file> also appends it as one JSON line. Only stage names, counts and
  seconds are recorded, never build content or personal data.
- POE1_BUILDS_LEGACY=1 turns batching and caching off (one process per request, like the
  previous version) so old and new workflows can be measured on the same PoB install.
"""
import atexit
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
POB_HOME = os.environ.get("POB_HOME", os.path.expanduser("~/.cache/poe1-pob"))
POB_SRC = os.path.join(POB_HOME, "pob", "src")
LUAJIT = os.path.join(POB_HOME, "luajit", "bin", "luajit")
LUAMODS = os.path.join(POB_HOME, "luamods")
DRIVER = os.path.join(HERE, "driver.lua")
CACHE_DIR = os.path.join(POB_HOME, "cache", "v1")
CACHE_MAX_FILES = 400
LOOKUP_MODES = {"nodes", "masteries", "gems", "uniques", "bases", "mods", "classes", "config"}


def _env(name):
    return os.environ.get(name, "").strip() not in ("", "0")


def legacy():
    return _env("POE1_BUILDS_LEGACY")


def cache_enabled():
    return not _env("POE1_BUILDS_NO_CACHE") and not legacy()


def timing_enabled():
    return _env("POE1_BUILDS_TIMING") or bool(os.environ.get("POE1_BUILDS_TIMING_LOG"))


# ---------------------------------------------------------------------------
# Instrumentation
# ---------------------------------------------------------------------------
STATS = {"pob_processes": 0, "pob_requests": 0, "cache_hits": 0, "cache_stores": 0, "pob_wall_s": 0.0,
         "pob_startup_s": 0.0, "stages_s": {}, "py_stages_s": {}, "counters": {}}
_t0 = time.time()


def count(name, n=1):
    STATS["counters"][name] = STATS["counters"].get(name, 0) + n


class stage:
    """with stage("preflight"): ...  -> adds wall seconds to STATS when timing is on."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.t = time.time()
        return self

    def __exit__(self, *a):
        d = STATS["py_stages_s"]
        d[self.name] = round(d.get(self.name, 0) + time.time() - self.t, 3)
        return False


def _record_driver_timing(resp):
    for name, secs in resp.get("timing") or []:
        d = STATS["stages_s"]
        d[name] = round(d.get(name, 0) + secs, 3)
    if resp.get("recalcs"):
        count("pob_recalculations", resp["recalcs"])


def _report():
    if not timing_enabled():
        return
    STATS["total_wall_s"] = round(time.time() - _t0, 3)
    STATS["pob_wall_s"] = round(STATS["pob_wall_s"], 3)
    STATS["pob_startup_s"] = round(STATS["pob_startup_s"], 3)
    STATS["mode"] = "legacy" if legacy() else ("no-cache" if not cache_enabled() else "default")
    STATS["command"] = os.path.basename(sys.argv[0]) + " " + (sys.argv[1] if len(sys.argv) > 1 else "")
    log = os.environ.get("POE1_BUILDS_TIMING_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(STATS) + "\n")
    if _env("POE1_BUILDS_TIMING"):
        s = STATS
        sys.stderr.write("\n[timing] total %.2fs | PoB processes %d (start-up %.2fs), requests %d, cache hits %d, PoB wall %.2fs\n"
                         % (s["total_wall_s"], s["pob_processes"], s["pob_startup_s"], s["pob_requests"], s["cache_hits"],
                            s["pob_wall_s"]))
        for k, v in s["py_stages_s"].items():
            sys.stderr.write("[timing]   %-34s %7.2fs\n" % (k, v))
        for k, v in s["stages_s"].items():
            sys.stderr.write("[timing]   PoB: %-29s %7.2fs (CPU)\n" % (k, v))
        for k, v in s["counters"].items():
            sys.stderr.write("[timing]   %-34s %7d\n" % (k, v))


atexit.register(_report)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
_fingerprint = None


def fingerprint():
    """PoB release + git commit + driver source: anything that changes calculations."""
    global _fingerprint
    if _fingerprint is None:
        h = hashlib.sha256()
        for p in (DRIVER, os.path.join(POB_HOME, "pob", "manifest.xml")):
            try:
                with open(p, "rb") as f:
                    h.update(f.read())
            except OSError:
                h.update(b"missing:" + p.encode())
        try:
            head = subprocess.run(["git", "-C", os.path.join(POB_HOME, "pob"), "rev-parse", "HEAD"], capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            head = ""
        h.update(head.encode() or b"no-git")
        _fingerprint = h.hexdigest()
    return _fingerprint


def _key(request):
    req = {k: v for k, v in request.items() if k != "timing"}
    blob = json.dumps(req, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((fingerprint() + "\n" + blob).encode("utf-8")).hexdigest()


def _cache_path(key):
    return os.path.join(CACHE_DIR, key[:2], key + ".json")


def cacheable(request, resp):
    if not isinstance(resp, dict) or any(str(e).startswith(("Driver error", "Internal:")) for e in resp.get("errors") or []):
        return False
    mode = request.get("mode")
    if mode in LOOKUP_MODES:
        return not resp.get("errors")
    if mode in ("build", "check"):
        return resp.get("valid") is True and "xml" in resp and "summary" in resp
    return False


def cache_get(request):
    if not cache_enabled():
        return None
    p = _cache_path(_key(request))
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("fingerprint") != fingerprint():
        return None
    os.utime(p, None)
    STATS["cache_hits"] += 1
    resp = data["response"]
    resp["cache"] = "hit"
    return resp


def cache_put(request, resp):
    if not cache_enabled() or not cacheable(request, resp):
        return
    key = _key(request)
    p = _cache_path(key)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    stored = {k: v for k, v in resp.items() if k not in ("timing", "cache")}
    tmp = p + ".%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"fingerprint": fingerprint(), "response": stored}, f)
    os.replace(tmp, p)
    STATS["cache_stores"] += 1
    _prune()


def _prune():
    files = []
    for root, _, names in os.walk(CACHE_DIR):
        files.extend(os.path.join(root, n) for n in names if n.endswith(".json"))
    if len(files) <= CACHE_MAX_FILES:
        return
    files.sort(key=lambda p: os.path.getmtime(p))
    for p in files[:len(files) - CACHE_MAX_FILES]:
        try:
            os.remove(p)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Static game data (the preflight name index): one file per PoB version + driver; not
# affected by --no-cache because it holds no calculation results.
# ---------------------------------------------------------------------------
def static_path(name):
    return os.path.join(POB_HOME, "cache", "static", "%s-%s.json" % (name, fingerprint()[:16]))


def static_get(name):
    try:
        with open(static_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def static_put(name, data):
    p = static_path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# Running the driver
# ---------------------------------------------------------------------------
def ensure_setup():
    missing = [p for p in (POB_SRC, LUAJIT, os.path.join(LUAMODS, "lua-utf8.so")) if not os.path.exists(p)]
    if missing:
        sys.exit("Path of Building isn't set up yet (missing: %s).\nRun: bash %s"
                 % (", ".join(missing), os.path.join(HERE, "setup.sh")))


def _spawn(request, timeout):
    ensure_setup()
    with tempfile.TemporaryDirectory() as td:
        req_path = os.path.join(td, "req.json")
        resp_path = os.path.join(td, "resp.json")
        with open(req_path, "w", encoding="utf-8") as f:
            json.dump(request, f)
        env = dict(os.environ)
        env["LUA_PATH"] = "../runtime/lua/?.lua;../runtime/lua/?/init.lua;./?.lua;;"
        env["LUA_CPATH"] = os.path.join(LUAMODS, "?.so") + ";;"
        t = time.time()
        proc = subprocess.run([LUAJIT, DRIVER, req_path, resp_path], cwd=POB_SRC, env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)
        STATS["pob_processes"] += 1
        STATS["pob_wall_s"] += time.time() - t
        if not os.path.exists(resp_path):
            sys.stderr.write(proc.stdout[-3000:] + "\n" + proc.stderr[-3000:] + "\n")
            sys.exit("Path of Building driver crashed (exit %d)" % proc.returncode)
        with open(resp_path, encoding="utf-8") as f:
            resp = json.load(f)
    if resp.get("startup_seconds"):
        STATS["pob_startup_s"] += resp["startup_seconds"]
    return resp


def _with_timing(request):
    if timing_enabled():
        request = dict(request, timing=True)
    return request


def run_one(request, timeout=600):
    """One request (cached when possible)."""
    hit = cache_get(request)
    if hit is not None:
        return hit
    STATS["pob_requests"] += 1
    resp = _spawn(_with_timing(request), timeout)
    _record_driver_timing(resp)
    cache_put(request, resp)
    return resp


def run_many(requests, workers=2, timeout=1800):
    """Many requests: cached ones are returned directly; the rest share PoB start-ups
    (at most `workers` processes, each running a batch). Results keep the input order."""
    results = [None] * len(requests)
    todo = []
    for i, r in enumerate(requests):
        hit = cache_get(r)
        if hit is not None:
            results[i] = hit
        else:
            todo.append(i)
    if not todo:
        return results
    if legacy():
        def one(i):
            STATS["pob_requests"] += 1
            resp = _spawn(_with_timing(requests[i]), timeout)
            _record_driver_timing(resp)
            return resp
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for i, resp in zip(todo, ex.map(one, todo)):
                results[i] = resp
        return results
    n = max(1, min(workers, len(todo)))
    chunks = [todo[k::n] for k in range(n)]

    def run_chunk(idx):
        STATS["pob_requests"] += len(idx)
        if len(idx) == 1:
            return [_spawn(_with_timing(requests[idx[0]]), timeout)]
        out = _spawn({"mode": "batch", "requests": [_with_timing(requests[i]) for i in idx]}, timeout)
        return out.get("results") or []

    with ThreadPoolExecutor(max_workers=n) as ex:
        for idx, resps in zip(chunks, ex.map(run_chunk, chunks)):
            for i, resp in zip(idx, resps):
                _record_driver_timing(resp)
                cache_put(requests[i], resp)
                results[i] = resp
    return results
