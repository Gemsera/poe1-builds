# Performance: where the time goes and how to keep it low

Measured on PoB Community 2.67.2 (tree 3_29) in this sandbox, October 2026.

## Cost of one Path of Building call

| Stage | Seconds |
|---|---|
| PoB start-up (loading game data) | 2.7–2.8 (CPU) |
| Turning a spec into a build (tree pathing ~1.4, items, XML) | 1.3–1.7 |
| PoB load + first calculation | 0.5–0.6 |
| Validation reports + export | 0.2–0.3 |
| Tree fill (`tree.fill`, ~150–200 cheap calculations for ~18 points) | 2–3 |
| Quality audit (17–22 recalculations) | 1.3–1.5 |

A lookup is almost all start-up (about 3 s). A build with the audit takes about 8 s.

## What the tools do about it

| Mechanism | Effect |
|---|---|
| `batch` | Lookup commands share one start-up |
| `compare`, `tree-audit` | Candidates run in at most 2 PoB processes (`--workers`), not one each |
| Result cache | An identical request returns the stored answer in under a second |
| Preflight name index | Built once per PoB version (`setup.sh` runs `warm`, ~3 s); checks names in about 0.02 s |
| `--repair` | Fixes mechanical spec mistakes in the same run, without a second build |
| No audit on invalid builds | Saves about 2 s on every failed attempt |
| MAIN-only recalculation | PoB's own frame runs four passes (MAIN, CALCS breakdown, two CALCULATOR baselines); the driver reads only MAIN, so `measure()` runs that pass alone. `POB_FULL_FRAME=1` restores the full frame for comparison |
| Tree fill uses PoB's tree-change calculator | ~20 ms per candidate node instead of ~170 ms for a full recalculation |
| `compare --max-candidates 8` | Default budget. Variants beyond it are listed but not evaluated, so put the most promising first |

**What the cache covers.**
- The key is the whole request, plus the PoB release and git commit, plus the driver source.
- Only lookups without errors and VALID builds are stored. Invalid builds, driver errors and
  incomplete answers never are.
- It lives in `~/.cache/poe1-pob/cache/` and keeps the newest 400 entries.
- `--no-cache` or `POE1_BUILDS_NO_CACHE=1` forces fresh calculations.

**Batching is checked to be exact.** `tests/test_fastpath.py` compares batched results with
separate runs field by field: errors, warnings, stats, audit and XML (in an order-independent
form, because PoB writes some lists in hash-table order). The check covers valid builds, invalid
builds, a fatal error and lookups.

## Measuring

`--timing` (or `POE1_BUILDS_TIMING=1`) prints this to stderr:
- the wall time per stage;
- the CPU time per PoB stage;
- the PoB processes, requests and cache hits;
- candidates evaluated and skipped, and repairs applied.

`POE1_BUILDS_TIMING_LOG=<file>` appends the same data as one JSON line. Only stage names, counts
and seconds are recorded.

`--legacy` (or `POE1_BUILDS_LEGACY=1`) runs one process per request with no cache, which is how
the previous version worked. `tests/bench_workflow.py --old <dir> --new <dir> --data <json>`
compares two versions of the skill on fixed scenarios.

## Benchmark: previous version vs this one

Same specs, same PoB install, cold cache. The new version's one-time name index (about 3.3 s,
built by `setup.sh`) is not included.

| Scenario | Before | After | PoB processes | Build attempts |
|---|---|---|---|---|
| S1: 6 lookups + build | 28.7 s | 13.4 s | 7 → 2 | 1 → 1 |
| S2: attribute + socket error, fix, rebuild | 16.4 s | 14.2 s | 2 → 2 | 2 → 2 |
| S3: gem level/quality/name mistakes | 16.9 s | 7.9 s | 2 → 1 | 2 → 1 |
| S4: 2 compares (10 + 12 variants), tree-audit, final build | 160.7 s | 83.1 s | 48 → 7 | 1 → 1 |
| S5: invalid build | 9.0 s | 5.8 s | 1 → 1 | 1 → 1 |

All five final builds gave identical PoB numbers in both versions (DPS, EHP, life, min max hit,
validity). S4 was run with `--max-candidates 0`, so both versions evaluated the same 47
candidates. With the default budget of 8, each of those compares took about 16 s.

## Not optimised (yet)

- **Tree pathing**, about 1.4 s per build. Greedy nearest-first, one full path calculation per
  target. A different algorithm could change the allocated tree, so it was left as is. Reusing
  the allocation for variants that don't touch the tree (gems, items, config) would save about
  1.4 s per such candidate.
- **Tree fill** scores each node kind once against the tree as it was when first seen (no
  re-measuring after every allocation). Re-measuring would cost ~20 ms × candidates per point.
- **`candidates` tie-breaking** is not deterministic: two identical runs can shortlist a different
  item for a slot when static scores tie (seen with Boots on the RF example). Sort ties by name.
- **The quality audit** recalculates once per option, flask, skill group and unique. It is part
  of the deliverable, so it was kept as is.

## Pathing search

Connecting the named targets runs three strategies plus a local re-route pass on the tree graph in
Lua, without PoB recalculations. It adds roughly 1–4 s to a build with 20–30 targets (the lookahead
strategy is quadratic in the target count per step). `POE1_BUILDS_GREEDY_PATHING=1` skips it.
