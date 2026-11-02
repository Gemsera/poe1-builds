# Reference build analysis (improving the skill from a good build)

Use this when the user gives a PoB code or `.xml` and asks to learn from it. Typical wording:
"analyse this build and improve the skill so it can make builds like it". The goal is better
future builds, **not a copy of this character**. Don't rebuild the reference as a spec unless a
test needs it.

## 1. Import and normalise (one command)

```bash
python3 pob.py reference "<code or file>" --content bossing --investment high [--mode softcore]
```

**Profile.** If the user didn't say the reference's content or budget, ask once. Otherwise
assume and label it; the output marks assumed values.

**What the command returns.** One PoB start-up runs validation, the quality audit and the
reference measurements. Everything in the main block is **calculated by PoB**:
- PoB and tree version, level, class and ascendancy;
- the main skill and its link;
- configured DPS and the **floor DPS** (flasks off, conditional config reset), with the damage
  split;
- what each conditional option and each flask is worth;
- life, ES, resistances (including over cap), chaos immunity, max hit for each damage type and
  EHP;
- the defensive-layer line, net recovery and main-skill cost sustain;
- **what each main-skill support is worth**: each removed in turn;
- **leaf passives removed alone**: keystones, ascendancy notables and notables nothing else
  depends on;
- unique dependency, and the audit's critical/material findings.

**Signals.** The `Signals` line is plain counts: uniques, build-enabling uniques, top-tier
affixes, crafting steps, unspent points. They are for **your interpretation** and are not PoB
numbers or prices.

**Invalid or partly analysable references.** The analysis always describes the build **as
given**. If PoB marks it invalid, the output lists the problems under `VALIDITY`. The audit
still runs, and `Analysis scope` says the numbers come from the invalid build. Anything PoB
can't calculate (for example damage when the main skill is disabled, or a max hit PoB doesn't
report) is listed under `Unavailable` with its cause. Don't fill these in with estimates.
PoB imports flasks switched off. When equipped flasks are off, a `NOTE` says so and the
numbers stay as given. `--adjust flasks-on` adds a second block, headed `ADJUSTED COPY, NOT
THE BUILD AS GIVEN`, for a copy with only the equipped flasks switched on. Report the two
blocks separately, and never present the adjusted numbers as the reference's own. No other
repair is applied automatically. If the user wants a fix tested, follow §4 and label it as a
change.

**How PoB exports differ from generated builds.** Real exports have features the skill's own
builds don't. The driver handles each one:
- **Auto level.**
  - With PoB's Auto level on, PoB derives the level from the passive points spent. The driver
    recalculates until that level is stable, so every number comes from one level.
  - The level line says "(PoB Auto level; file says N)" when the result differs from the saved
    level.
  - Passive checks hold the level fixed: removing a passive doesn't also de-level the character.
- **Several gear sets.** Only the active set is used. That covers the flask count, the flask
  hint and `--adjust flasks-on`; other sets are left exactly as they are.
- **Rolls from PoB's item crafter.** These keep a roll as a range plus a stored value
  ("(31-36)% … {range:0.5}"). They're resolved the way PoB resolves them, then checked against
  the tier ranges like any other value.
- **Saved stats.** The export's saved stats are compared with PoB's recalculation (over 1%
  counts as a difference). Identifiable causes are tested one at a time:
  - the character level, when Auto level changed it;
  - the boss setting, when the build doesn't set it (this PoB defaults to Pinnacle).

  The output says which stats each tested change reproduces. Anything left over is reported as
  not fully explained, with the possible causes that can't be tested here (a different PoB
  version or game data).
  - Neither side is assumed correct.
  - Don't state a cause the output didn't reproduce.
- **Messages.** Every driver warning and note, and PoB's own non-blocking warnings, are listed
  under `Driver and PoB messages`, deduplicated with a count. Read them: they include item
  legality problems and calculations that didn't return to their starting point.

**Keep these apart in the report.** PoB-calculated values are facts. Anything you conclude from
them (why something works, what the budget probably is) is interpretation, and must be labelled.

**Don't compare unlike builds.** Use the reference as a benchmark only once you know its level,
budget, content and configuration. Its headline DPS doesn't prove it is better: look at the floor
DPS, the weakest max hit and recovery too.

## 2. Find the design decisions that matter

Work from the biggest numbers down and stop after the few that explain most of the result.

| Area | Where to look |
|---|---|
| Skill and supports | `Supports` line: which supports carry the damage |
| Damage scaling | Damage split, conditional config, leaf passives with a large DPS share, uniques with a large DPS share |
| Passive tree | Keystones, leaf passives (sensitivity only), unspent points |
| Ascendancy | `[asc]` entries in the leaf-passive line |
| Defensive architecture | Defensive-layers line, max hit per type, resistances over cap, chaos immunity |
| Equipment | Unique dependency (build-enabling, major), rare signals, crafting steps |
| Resource sustain | Main-skill cost line, recovery line |
| Configuration | Conditional config and flasks: what is assumed, and how much depends on it |

For each significant finding, write these six points (briefly):
1. what the reference does;
2. why it may be effective (interpretation; say so);
3. whether the skill already covers it (`SKILL.md`, `quality-review.md`, audit, preflight);
4. which part of generation or validation falls short;
5. the targeted change that would address it;
6. whether it is **general**, **archetype-specific** or **reference-specific**.

## 3. Diagnose before changing anything

Put each finding in one class:

| Class | Typical fix |
|---|---|
| Build-specific advantage | None: note it |
| Missing game-mechanics knowledge | A sentence in `SKILL.md` or `quality-review.md` |
| Poor candidate selection | Guidance on which alternatives to try, in the quality review |
| Inefficient tree or gem optimisation | Guidance, or a deterministic hint in `pob.py` |
| Unrealistic equipment assumptions | Gear-realism guidance or audit thresholds |
| Weak defensive evaluation | `audit.py` or thresholds, when it can be checked reliably |
| Incorrect or misleading configuration | Config-honesty checks |
| Tooling limitation | Script change, or a documented limit |
| Difference from investment, level or content | **No skill change** |

Don't recommend a change when budget, level or content explains the gap. Rank the remaining
deficiencies by how many future builds they affect.

## 4. Test the proposed change (bounded)

1. **Requirements.** Set the reference's level, content, investment and mode, and the same
   `enemyIsBoss` and config policy.
2. **Baseline.** Generate one build with the **current** skill (same main skill or archetype) and
   save its spec. This is the protected baseline.
3. **Change.** Apply **one** targeted change and generate the comparable build. Keep the change
   to the build itself small: a few `compare` variants, no exhaustive search.
4. **Compare.** Run `reference "<ref>" --against new.json`, and `compare baseline.json
   variants.json` for the baseline vs. the change. Check:
   - validity and that the main skill works;
   - floor DPS under equal configuration;
   - life/ES, max hit per type and resistances;
   - recovery and sustain;
   - gear needs and fit for the content.
5. **Regression.** Run the change against both examples (`build examples/*.json`) and
   `tests/test_audit.py`.
6. **If it can't be tested**, label it **unverified**. Never report numbers you didn't run.

Matching the reference's DPS for one archetype is not proof of a general improvement. Only claim
what the comparison shows.

## 5. Change the skill only when the user asked for it

- **If the user only asked for analysis,** report the proposed changes and stop.
- **When they want the skill updated,** change only the files a confirmed deficiency needs:
  - a deterministic check (`audit.py`, `preflight.py`, `pob.py`, `driver.lua`) when the rule can
    be verified reliably from PoB data;
  - instructions (`SKILL.md`, `quality-review.md`) when it needs judgment;
  - thresholds (`audit-thresholds.json`) only for a calibration that holds beyond this one
    build. **Never** change a threshold just to make the reference pass.
- An archetype-specific rule must be conditional ("for DoT builds…", "for minion builds…"), not
  a change to the whole process.
- **Don't duplicate an existing check or contradict the current workflow.** One reference build
  never establishes a universal rule.

## 6. Report

- **Reference summary:**
  - main skill and archetype;
  - PoB numbers: configured and floor DPS, pool, resistances, weakest max hit, recovery;
  - key gear dependencies;
  - configuration assumptions;
  - comparability caveats.
- **Deficiencies found:** each with its evidence (the PoB line it came from), class, likely
  impact and generality label.
- **Changes:** proposed, or made with the exact files and the expected benefit.
- **Validation:** the tests actually run, their PoB results, and what remains unverified.
- **Generality:** which changes should help most builds, which apply to this archetype only, and
  which observations stay reference-specific.

## Limits of the tooling

- **Leaf passives only.** A notable that other allocated nodes depend on isn't measured; use
  `tree-audit` on a spec for that. Removing a node alone keeps its travel points, and nodes
  interact, so treat it as a sensitivity test.
- **Supports are removed one at a time.** Interactions between supports aren't separated.
- **PoB's own limits apply.** Cluster-jewel notables, timeless jewels, uptime, and mechanics PoB
  doesn't model aren't measured. Prices are never available.
