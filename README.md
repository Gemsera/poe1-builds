# poe1-builds

A Claude skill that designs, validates and calculates **Path of Exile 1** character builds by running
[Path of Building Community](https://github.com/PathOfBuildingCommunity/PathOfBuilding) headless, and
hands back a ready-to-import PoB code.

Claude decides the build: class, ascendancy, main skill, notables, gem links and gear. The bundled
tools do the parts that have to be exact. They path the passive tree, check item mods and gem links
for legality, run PoB's own calculations, audit the result, and search PoB's item data for upgrades.
Every number in a report comes from Path of Building, not from the model.

> Path of Exile 1 only. Path of Exile 2 has a different tree and skill system and is not supported.

## What it can do

- **Generate builds** from a short JSON spec. Name the notables and keystones and the driver connects
  them with the fewest points it can find. A greedy "fill" spends the remaining points on whatever PoB
  measures as best.
- **Check pasted builds**: decode a PoB code or `.xml`, recalculate it, and list errors (attribute
  shortfalls, impossible links, illegal mods, mana that can't be sustained) in plain language.
- **Audit quality** against content and budget profiles (`league_starter`, `budget`, `medium`, `high`;
  mapping or bossing). It checks max hits, resistances, defensive layers, recovery, ailment avoidance,
  how much DPS each config assumption provides, flask dependence and unique dependence.
- **Find equipment** (`candidates`). It searches every unique and craftable flask base in PoB's data,
  measures the shortlist as drop-in replacements, and builds a crafted rare "template" for the weapon
  and amulet slots with tiers capped for the budget.
- **Sweep standard mechanics** (`sweep`): rage, frenzy, power and endurance charges, onslaught,
  intimidate, exposure, eldritch implicits and culling, each switched on with its source and measured.
- **Compare variants** (`compare`) and **value tree targets** (`tree-audit`) on the exact same tree.
- **Analyse reference builds** (`reference`): normalised PoB facts, what each support gem and leaf
  passive is worth, and conditional config, used to improve how the skill builds.
- **Share** a finished build on pobb.in (needs internet).

## Requirements

- Linux or macOS with `bash`, `git` and **Python 3** (standard library only; tested with 3.13).
- About 400 MB of disk for Path of Building's Lua sources and data.
- On x86-64 Linux, a bundled LuaJIT and `lua-utf8` are used. Elsewhere `setup.sh` builds them from
  source, which needs `make` and a C compiler.

## Installation

### As a Claude skill

- **Claude.ai / Claude desktop:** zip the `poe1-builds` folder (or use a released `.skill` file) and
  upload it under _Settings → Capabilities → Skills_.
- **Claude Code:** clone or copy the folder into `~/.claude/skills/poe1-builds` (personal) or
  `.claude/skills/poe1-builds` (project).

The skill runs `scripts/setup.sh` itself the first time it's needed.

### Standalone (command line)

```bash
git clone <this repo> poe1-builds
cd poe1-builds
bash scripts/setup.sh            # LuaJIT, lua-utf8, Path of Building (latest release), name index
bash scripts/setup.sh --update   # later: pull the newest Path of Building release
```

`setup.sh` is idempotent. It installs into `$POB_HOME` (default `~/.cache/poe1-pob`). Set
`POB_REF=dev` to track Path of Building's development branch, for example right after a league
launches.

## Quick start

```bash
cd scripts

# build a spec, print the report and the PoB import code
python3 pob.py build ../examples/rf_chieftain.json

# check a build someone pasted (a code, a file containing the code, or a PoB .xml)
python3 pob.py check "eNrtXW..." -o out/

# the build step writes <name>.filled.json with the tree pinned by node id; use it for analysis
python3 pob.py candidates   RF_Chieftain.filled.json     # equipment that may suit the build
python3 pob.py sweep        RF_Chieftain.filled.json     # standard mechanics, measured
python3 pob.py tree-audit   RF_Chieftain.filled.json     # value per point of each tree target
python3 pob.py compare      RF_Chieftain.filled.json variants.json

# lookups (several in one PoB start-up)
python3 pob.py batch - <<'END'
gems "ice nova"
nodes "Chaos Inoculation" --class Witch
uniques "Kaom's Heart"
mods "Vaal Regalia" --ilvl 86 --eldritch
END
```

Run `python3 pob.py --help` or `python3 pob.py <command> --help` for every command and option.

## A minimal spec

```json
{
  "name": "Ice Nova Occultist",
  "class": "Witch",
  "ascendancy": "Occultist",
  "level": 90,
  "profile": { "investment": "league_starter", "content": "mapping" },
  "tree": {
    "nodes": ["Frost Walker", "Arcanist's Dominion", "Heart and Soul"],
    "ascendancy": [
      "Frigid Wake",
      "Void Beacon",
      "Profane Bloom",
      "Forbidden Power"
    ],
    "fill": "balanced"
  },
  "skills": [
    {
      "slot": "Body Armour",
      "main": true,
      "gems": [
        "Ice Nova",
        "Inspiration",
        "Hypothermia",
        "Cold Penetration",
        "Controlled Destruction",
        "Elemental Focus"
      ]
    },
    { "slot": "Gloves", "gems": ["Herald of Ice", "Clarity"] }
  ],
  "items": [
    {
      "slot": "Weapon 1",
      "text": "Rarity: RARE\nGrim Song\nImbued Wand\nItem Level: 82\n+1 to Level of all Cold Spell Skill Gems\n68% increased Spell Damage"
    },
    {
      "slot": "Helmet",
      "text": "Rarity: RARE\n...",
      "eater": "8% of Physical Damage from Hits taken as Cold Damage"
    },
    { "slot": "Belt", "unique": "Darkness Enthroned" }
  ],
  "config": { "enemyIsBoss": "Pinnacle" }
}
```

The full format covers requirements, masteries, cluster jewels, eldritch and corrupted implicits,
anoints, skill parts and stages, minion skills, and event-triggered skills (`trigger_events_per_second`
for Cast when Stunned and Cast when Damage Taken). See
[`references/spec-format.md`](references/spec-format.md).

## Repository layout

```
SKILL.md                  instructions Claude follows (workflow, rules, what to report)
scripts/
  setup.sh                installs LuaJIT, lua-utf8 and Path of Building
  pob.py                  command-line entry point (build, check, compare, candidates, sweep, ...)
  driver.lua              runs inside headless PoB: builds, paths the tree, calculates, audits
  preflight.py            spec validation and name checks before PoB starts
  audit.py                quality audit against the profile thresholds
  candidates.py           ranking and reporting for equipment candidates
  reference.py            reference-build analysis
  requirements.py         user-requirement checks on the exported build
  fastpath.py             batching, result cache, timing
  pobb_in.py              pobb.in upload
references/               formats, thresholds, mechanics catalogue, review guides
examples/                 complete example specs
tests/                    unittest suites (need setup.sh first) and fixtures
bin/                      bundled LuaJIT and lua-utf8 for x86-64 Linux, with their licenses
```

## Tests

```bash
python3 tests/test_candidates.py      # one suite
for t in tests/test_*.py; do python3 "$t" || break; done
```

The suites start Path of Building, so run them after `setup.sh`. Run them one at a time: several in
parallel can run out of memory. Set `POE1_BUILDS_NO_CACHE=1` to bypass the result cache.

## Environment variables

| Variable                                               | Effect                                                                          |
| ------------------------------------------------------ | ------------------------------------------------------------------------------- |
| `POB_HOME`                                             | Where Path of Building, LuaJIT and the cache live (default `~/.cache/poe1-pob`) |
| `POB_REF`                                              | Path of Building branch or tag for `setup.sh` (default `master`)                |
| `POE1_BUILDS_NO_CACHE=1`                               | Don't read or write the result cache (same as `pob.py --no-cache`)              |
| `POE1_BUILDS_GREEDY_PATHING=1`                         | Use PoB's nearest-first pathing only, without the shorter-route search          |
| `POE1_BUILDS_TIMING=1`                                 | Print timing per stage                                                          |
| `POE1_BUILDS_PATH_TRACE`, `_FILL_TRACE`, `_ITEM_TRACE` | Debug traces for pathing, the tree fill and item checks                         |

## Limits

- PoB's numbers are only as good as the config. Every conditional assumption the build relies on
  (charges, enemy states, flask uptime) is listed with its evidence and its share of the DPS.
- Prices are never checked. Reports say "cost not verified".
- Legacy items and mods that are no longer obtainable are flagged, not silently accepted.
- Accuracy follows the installed Path of Building version; run `setup.sh --update` after a patch.

## Credits and licences

- [Path of Building Community](https://github.com/PathOfBuildingCommunity/PathOfBuilding) does all
  the calculations. It is downloaded by `setup.sh` and not redistributed here.
- [LuaJIT](https://luajit.org/) and [luautf8](https://github.com/starwing/luautf8) binaries are
  bundled in `bin/`, under their own licences (`bin/LICENSE-*.txt`).
- This project is not affiliated with or endorsed by Grinding Gear Games. Path of Exile is a
  trademark of Grinding Gear Games.

<!-- Add a LICENSE file and state the project's licence here before publishing. -->
