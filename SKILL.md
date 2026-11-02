---
name: poe1-builds
description: Generate, validate and calculate Path of Exile 1 (PoE1) character builds with Path of Building Community running headless, producing a ready-to-import PoB code. Use this whenever the user wants a PoE / Path of Exile 1 build made, checked, fixed, compared or theorycrafted - league starters, endgame builds, "make me a Vortex Occultist / RF Chieftain build", pasted PoB codes ("what's wrong with my build", "how do I get more DPS / cap my resists"), passive tree or gem-link questions for a specific build, rare-item mod legality, or analysing a reference build to improve how this skill generates builds. Not for Path of Exile 2 (different game, different tree).
---

# PoE1 builds with headless Path of Building

This skill turns build ideas into **valid, calculated Path of Building builds**. You decide the
build (class, skills, notables, gear); the bundled tools do the parts that must be exact:

- **Passive tree pathing**: you name the notables/keystones; the driver connects them to the class
  start with the fewest points it can find (PoB's nearest-first allocation, then a Steiner-style
  search over three connection orders with pruning and local re-routing; a cheaper tree replaces
  PoB's, a tie keeps it) and reports the point cost of each target plus the total. On the real tree
  the search saves a point or two at most: **the route is rarely the waste; the target list is**
  (see step 6, Tree). `tree.fill` then spends the points the targets leave over on the nodes PoB
  measures as most valuable for this build, and travel the fill made redundant is pruned and
  re-spent.
- **Validation**: points vs. level, ascendancy points, gem names, gem levels/quality, support
  compatibility, link counts vs. sockets, attribute and level requirements, mana/reservation,
  rare-item affixes (rollable on that base, value in range for the item level, max 3 prefixes /
  3 suffixes, one mod per mod group, crafted-mod limits).
- **Real numbers** from PoB's calculation engine: DPS, EHP, max hits, resistances, etc.
- **Output**: a PoB import code the user pastes into Path of Building (Import/Export Build > Import).

Everything runs in this sandbox; nothing touches the user's computer.

## 1. Setup (once per session)

```bash
bash <skill-dir>/scripts/setup.sh
```

Downloads Path of Building's Lua + game data from GitHub (~1 minute the first time; instant
after). If it fails on network access, tell the user the sandbox needs GitHub access (their
code-execution network setting) and stop. `--update` pulls the newest PoB release.
`POB_REF=dev` uses PoB's development branch (useful in the first days of a new league).

All commands below are `python3 <skill-dir>/scripts/pob.py <command>`; `--help` on any command
lists options. Starting Path of Building costs ~3 s per call, so **group work into few calls**:
put several lookups in one `batch` (below), give `compare` all the variations at once, and let
`--repair` fix mechanical spec mistakes in the same run. Repeating an identical build or lookup is
answered from a cache (keyed to the PoB version) in well under a second.

## 2. Lookups: never guess names or numbers

Your memory of PoE is a good starting point but goes stale: gems get renamed (Increased
Duration is now *More Duration*), notables move or disappear, mod tiers change, transfigured
gems and new ascendancies/bloodlines appear. Check against the live data instead of trusting
recall. Run several lookups with **one** PoB start-up:

```bash
python3 pob.py batch - <<'END'
gems "burning arrow"
uniques "Kaom's Heart"
nodes "Hired Killer" --class Ranger
END
```

| Need | Command |
|---|---|
| Classes, ascendancies, bloodlines, tree version | `classes` |
| Passive nodes by name or stat text, with distance from class start | `nodes "cold damage" --class Witch --type Notable` |
| Ascendancy notables | `nodes --class Witch --ascendancy Occultist --type Notable` |
| Mastery effects, and which notables unlock each mastery node | `masteries "life" --class Ranger` |
| Gems (incl. transfigured) | `gems vortex`, `gems cold --tags --support` |
| The "Greater" support gems (exceptional, low max level: Greater Multistrike, Greater Spell Echo, Greater Unleash, ...) | `gems greater --support` |
| Uniques (exact name prints full text) | `uniques "Shavronne's Wrappings"`, `uniques "reservation" --full` |
| Item bases | `bases "Regalia"` |
| What can roll on a base at an item level | `mods "Vaal Regalia" --ilvl 86 --query life` |
| Searing Exarch / Eater of Worlds implicits for a slot | `mods "Slink Gloves" --eldritch` |
| Corruption (Vaal) implicits for an item class | `mods "Coral Ring" --corrupted` |
| PoB config options (boss, charges, buffs) | `config charges` |
| Side-by-side PoB numbers for variations of a spec | `compare spec.json variants.json` |
| Value per point of each tree target | `tree-audit spec.json` |
| Equipment that may suit the build, found from PoB's data and measured as drop-ins | `candidates spec.json` |
| Normalised analysis of a reference build (optionally vs another) | `reference "<code>" [--against spec.json]` |
| Mechanic sweep: the standard mechanics this archetype could add, each measured (rage, frenzy, onslaught, intimidate, exposure, ...) | `sweep <name>.filled.json` |
| Shareable pobb.in link for a finished build (internet) | `upload <name>.pob.txt` or `build ... --share` |

## 3. Generating a build

1. **Pin down what the user wants** if it isn't clear: main skill or playstyle, league starter
   vs. endgame/budget (trade or SSF), Hardcore or Softcore, target content (mapping, bossing),
   character level. Ask at most once; otherwise assume a softcore trade endgame build at level
   ~92 and say so. A stated budget maps to the profile's `investment`: "league starter" / first
   day → `league_starter`; up to ~1-2 Divines → `budget` (rares with mid-tier rolls, cheap
   uniques, no Awakened/Empower 4, +levels from rare affixes rather than Enlighten 4); ~5-20 →
   `medium`; above → `high`. Say which you assumed. Note any hard requirements the user gave (a DPS target, a unique, a budget):
   no later change may silently break one.
   **Requirements are what the user explicitly named, and they are binding.** Write them into
   the spec's `"requirements"` block (`references/spec-format.md`): main skill, class,
   ascendancy, other named gems, uniques, passives, and a weapon type only if the user named one.
   - **A named gem is that exact gem.** "Elemental Hit" is ordinary Elemental Hit. A transfigured
     version (Elemental Hit of the Spectrum), a Vaal version or an Awakened version is a
     different gem.
   - **No Awakened gems.** They are drop-disabled since 3.29 and exist only as legacy items in
     Standard; PoB still has their data, so nothing stops you writing them except this rule. The
     preflight repairs `Awakened X` to `X` (`--repair`) and names the `Greater X` gem when one
     exists; `"legacy_gems": true` allows them for a Standard build the user asks for. The
     gems that replaced them are the **Greater** gems (`gems greater --support`): exceptional,
     low max level, each a stronger version of one support (Greater Multistrike, Greater Spell
     Echo, Greater Unleash, Greater Chain/Fork/Spell Cascade/Ancestral Call/Devour). They are
     candidates to `compare` against the regular support, never assumed.
   - **Alternatives are allowed only when the user permits them by name** (`also_accept`), and
     they are reported as alternatives. There is no general "any variant" switch.
   - **What you infer is not a requirement.** That includes the weapon a skill is usually used
     with, a playstyle or gear. Put inferences in `"assumptions"`, which are shown and not
     enforced. Mechanical needs (a melee skill needs a melee weapon) are enforced by PoB itself.
   - **If a requirement looks infeasible or weaker than an alternative,** build the requested
     version anyway and offer the alternative as a separately labelled variant, or ask. Never
     substitute it.
   - **Every step checks the requirements:**
     - the preflight;
     - PoB's result;
     - the exported file;
     - every `compare` candidate and every `tree-audit` removal.
   - A build that breaks one is INVALID and is not exported as the valid version. A candidate that
     breaks one is `rejected`, whatever its DPS. Without a requirements block (the user named
     nothing specific), comparing alternatives works exactly as before.
2. **Fix the build concept before writing the spec.** Do this in your own reasoning (a few lines;
   no separate document, no extra call):
   - **Damage:** the main mechanic (hit or DoT, element or conversion, crit or non-crit) and what
     scales it.
   - **Delivery:** self-cast / attack, **mines**, traps, totems, or a trigger. For a spell this is
     the biggest single decision at low budget: High-Impact Mine + Minefield (+ Charged Mines,
     Trap and Mine Damage, Swift Assembly) routinely double or triple a self-cast spell's DPS for
     no currency, at the cost of throw-and-detonate play (Detonate Mines on Automation). Decide it
     here; the `sweep` tests the mine version of a self-cast spell if you didn't.
   - **Event-triggered main skill** (Cast when Stunned, Cast when Damage Taken): PoB's DPS and
     mana cost assume the trigger cooldown ceiling. Set `"trigger_events_per_second"` to the rate
     you can defend (stuns or damage events per second) and quote the `TRIGGER RATE` line, never the
     sidebar. Remove every "cannot be stunned / ignore stuns / avoid stun" source from a Cast when
     Stunned build (Vile Bastion, Unwavering Stance, Practical Application, Conjured Barrier).
   - **Gem names are exact.** Write PoB's full gem name (`gems <word>` lists them). The driver no
     longer accepts PoB's abbreviation matching, which turned a renamed gem ("Onslaught", now
     Momentum) into a different gem ("Frenzy of Onslaught") without a word; an unknown name is an
     error with suggestions. "X Support" and "X" both resolve to the support gem X when both exist.
   - **Gem levels from gear:** for a spell, `+1 to Level of all Spell Skill Gems` / `+N to Level
     of all <element> Spell Skill Gems` on the weapon, `+1 to all Skill Gems` or a skill-specific
     `+3` (Replica Dragonfang's Flight) on the amulet, `+2 to Socketed/Minion Gems` on the helmet
     are the strongest single-item multipliers and cheap on rares (`mods <base> --query "Level of
     all"`). `candidates` builds a **rare template** for the weapon and amulet (the best legal
     affixes for this build on the equipped base, each measured by PoB, tiers capped for the
     investment), so compare the unique it shortlists against that template rather than naming
     rare mods from memory. Other slots are not templated: name their key rare mods yourself.
   - **Defence:** the defensive design (armour, evasion/suppression, block, ES, MoM...) and how
     the weakest max-hit type is covered. A guard skill is temporary (PoB's uptime is in the
     audit). Choose its setup by comparing the options the character can use, not by habit.
   - **Sustain:** how mana, life or other costs are paid and how life or ES recovers (regen,
     leech, recoup, flasks).
   - **Gear:** essential items (build-enabling uniques, key rare mods) versus optional upgrades,
     at the stated **investment** and **content**. Don't pick gear from memory of what the skill
     usually wears: once the build is valid, `candidates` (step 6) searches PoB's item data against
     this build's own mechanics.
   - **Uptime:** the buffs, charges, flasks, enemy states, skill counts and **stage counts** the
     numbers rely on. For a staged skill, set `"stages"` on the gem (and `"skill_part"` where PoB
     offers parts) to what the build sustains; unset means PoB's maximum. See
     `references/spec-format.md`. **Minion builds**: set `"minion_skill"` on the minion gem to the
     attack the minion sustains (PoB's default can be a cooldown skill: Herald of Agony's crawler
     reads Mortar at ~2% of its Tail Spike DPS); the report names the minion's skills. See
     `references/spec-format.md`. The audit lists the evidence it found for each conditional
     option and gives configured, evidence-supported and floor numbers; none of them is an
     uptime estimate. For random-element skills, report the range across elements rather than
     one part (`references/quality-review.md` §1).
   - **Charges are part of the concept, not an afterthought.** Decide for each of frenzy, power
     and endurance: *generator* (which gem, item, ascendancy node or passive makes them), *cap*
     (+1 maximum charge notables, gear, Ralakesh-type items) and *payoff* (per-charge scaling).
     A build with a generator and no cap or payoff, or a payoff with no generator, is
     unfinished. If the archetype has a cheap generator (Blood Rage / Frenzy for attacks, Power
     Charge On Critical or Assassin's Mark for crit, Enduring Cry / charge-on-stun for melee,
     the ascendancy's own charge node), put it in the spec now; the sweep (step 6) only tests
     what you left out.
   
   Every gem, notable, ascendancy node and item should serve that concept. Something that serves
   none of it is a candidate to replace.
3. **Draft the spec** (JSON; full format in `references/spec-format.md`). Start from the closest
   example if one fits: `examples/burning_arrow_vigour_deadeye.json` (uniques, Exarch/Eater
   implicits, masteries) or `examples/rf_chieftain.json`. They are starting points, not finished
   builds. Put the lookups you need into one `batch` and reuse their results; don't repeat a
   lookup whose answer you already have. Write rares as plain item text with realistic, rollable
   mods (`mods` shows the tiers). Set `profile` (investment, content, mode).
   **Fill the cheap slots first**: anoint the amulet (`"anoint"`), give rare helmet/body/gloves/boots
   Exarch/Eater implicits (`mods <base> --eldritch`), and write flasks as magic items with a charge
   or duration prefix and an immunity suffix. The audit lists any of these left empty.
   Pick eldritch implicits for the concept, not the slot's default: gloves **Unnerve on hit**
   (spells: enemies take 10% increased spell damage) or **Intimidate on hit** (attacks), helmet
   **Mana Reservation Efficiency** when an aura doesn't fit, body **phys taken as chaos** on a CI
   or chaos-immune character (free mitigation), boots action speed. The `sweep` tests these.
   Flasks on a 1-Divine budget are automated: an instilling-orb enchant (`Used when Charges reach
   full`, written as an implicit line) with a `Flagellant's` / `Masochist's` charge-gain prefix, a
   Rumi's Concoction on a block build, and a **mana flask** (`Enduring`) when the main skill's
   cost outruns regen (mines, channelling). PoB counts flasks as always on; say which are automated.
   **Curse automation** (any build that wants a hex up without pressing it): Profane Proxy ring +
   Summon Skitterbots (the aura applies the socketed hex; +3 to its level), Blasphemy, Hextouch on
   a trigger, or a Mark-on-hit ring; the inference pass sets `conditionEnemyCursed` when one is in
   the build.
   **Name the targets, not the travel.** List keystones, notables, masteries and jewel sockets
   that serve the concept, and set `"tree": {"fill": "balanced"}` (or `dps` / `defence`) so the
   leftover points go to small nodes, nearby notables and mastery effects PoB measures as worth
   the most (it finishes suppression, takes the right life/elemental masteries, stops at caps).
   **Conditional config (charges, rage, onslaught, enemy states) is set in three layers; know
   which one you're in:**
   - **Source already in the spec** (a gem such as Blood Rage, Frenzy, Power Charge On Critical
     or Enduring Cry; an item; an ascendancy node; a named passive): `build` detects it and
     switches the option on before the fill runs (report line `Config set from sources already in
     the build`). You may also set it yourself in `config`; the result is the same. Only a
     *sustaining* source counts (on hit, skill in build, build mechanic); an on-kill or
     while-moving source is reported but left off — set it yourself for mapping content and say so.
     Opt out of a detected option with `"<option>": false`.
   - **Source the fill might add from the tree** (frenzy charges from the Mark Mastery, rage from
     a "Gain 1 Rage on Melee Hit" node): leave the option unset. The fill measures the node with
     the option on, switches it on if the node wins, names the source, and re-measures the
     "+1 to Maximum ... Charges" notables once charges are on. Pre-setting the option here would
     make the source score nothing.
   - **No source anywhere**: the option stays off. Never set it to make the numbers look better;
     add a source (step 6 `sweep` proposes the standard ones) or accept the lower figure.
   
   With a charge option on, name the cap and payoff targets (`+1 to Maximum Frenzy Charges`
   notables and the like) when they're more than 3 points from the tree — the fill only sees
   notables within `max_cost` (default 3) steps and will otherwise never reach them.
   List only the masteries you insist on. Don't list small life/damage nodes by hand, and never
   leave points unspent at the target level: an
   unfilled tree is an unfinished build. The report's `Tree fill` line shows what was added and
   what each node measured; override a choice by naming a node yourself.
   **Implicits follow rarity rules**:
   - Normal/Magic/Rare items get their base implicit (added automatically), or Exarch/Eater
     implicits that replace it (helmet/body/gloves/boots only).
   - Uniques keep their own implicits.
   - Only corrupted items may have corruption implicits.
   - Uniques and corrupted items can never have Exarch/Eater implicits.
   
   Details and item keys: the Implicits section of `references/spec-format.md`.
4. **Run it**: `build spec.json -o <outdir> --repair`. Exit code 0 = valid, 2 = invalid. A
   preflight checks the spec before PoB starts: structure, misspelt keys, and names of the class,
   gems, uniques, bases, passives and config options. `--repair` applies only unambiguous fixes:
   - gem level above the maximum;
   - quality above 23;
   - a gem level too high for the character level;
   - a gem name that differs only in case or punctuation.
   
   They go to a `<name>.repaired.json` copy and are listed in the report; your spec file is not
   changed. A valid build of a spec with `tree.fill` also writes `<name>.filled.json`, the spec
   with the filled tree pinned by node id: **run `candidates`, `compare` and `tree-audit` on that
   file**, so variants test one idea on the exact tree delivered (and skip the fill cost). Edit the
   original spec for the next revision; a tree-editing variant re-fills automatically. Preflight findings are shown together with PoB's own errors, so one revision fixes
   everything. An invalid result is saved as `<name>.invalid.*` and never overwrites the last
   valid export, which stays your best valid candidate.
5. **Fix every ERROR with a targeted edit** to the parts the messages name, then rerun. Don't
   regenerate the whole spec. Don't silence a check by removing the thing the build needs; find a
   legal way to get it.
   - **Attribute requirements have an order.** "You do not meet the X requirement" is met in this
     order, cheapest first, and the report's `ATTRIBUTES` section lists the concrete options for
     the build with what each displaces: (1) the gem level that drives the need (one level less is
     often enough; `compare` measures the cost); (2) an amulet base whose implicit is the attribute
     (Jade = Dex, Amber = Str, Lapis = Int, Onyx = all; free unless the current implicit matters);
     (3) a free suffix on an equipped rare or jewel at the best tier its item level allows, which
     competes with resistance; (4) a +30 attribute node one point off the tree, which is the right
     answer when the shortfall is large or every suffix is spoken for, because one point for +30
     beats a suffix that could hold 45% resistance. **Never spend fill points on attributes**: the
     fill scores power only, and a node chosen for attributes is named in `tree.nodes`. Keep a
     margin of about 10 over each requirement so a later gem-level or support change doesn't
     break the build; the audit flags a thinner margin.
   - **Never edit `requirements` to make an error go away.** If a requirement can't be met (for
     example the gem doesn't exist in this PoB version), tell the user and ask.
   - The quality audit runs once the build is valid.
6. **Improve it in priority order, then stop.** The audit sorts its findings into three groups:
   - **CRITICAL**: must fix.
   - **MATERIAL**: fix when a reasonable alternative exists; otherwise explain the trade-off.
   - **ADVISORY**: explain or accept.
   
   Work down this list and stop at the first level that still has open items you can fix:
   1. validity;
   2. main skill works;
   3. critical resource sustain;
   4. essential defences (resists, weakest max hit, recovery for the content);
   5. honest damage configuration;
   6. gear realism and budget;
   7. targeted damage and tree optimisation.
   
   Tools for the work:
   - `candidates spec.json`: run it once on the first valid build (and again after a major
     change, below). It finds equipment from PoB's data that fits this build's skill, weapon,
     damage types, ailments, ascendancy and tree, and measures a capped number of them as drop-in
     replacements. All of them are measured against one baseline at the current config.
     - **What it reports:** a shortlist per slot, with:
       - PoB's DPS/EHP/defence changes;
       - the conditions an item needs, and whether the build has a source for them;
       - a separate "if the condition were set" test;
       - what PoB can't model;
       - "different configuration" items and passives that would enable an interaction.
     - **Rare templates** (`Template <base>`, `kind: rare`): for Weapon 1 and Amulet it also
       assembles a crafted rare on the equipped base (and up to two or three sibling bases) from
       PoB's affix pool: every candidate affix is measured by PoB on this build, the best are kept
       one per mod group within 3 prefixes / 3 suffixes, the equipped item's life, ES, resistance
       and attribute lines are copied, and costly groups are capped at their best tier only
       `top_tier_limit` times (league starter 1, budget 2, medium 3, high unlimited; the rest drop
       to the second tier). It is measured like any other candidate and ranked with them, so a
       unique that loses to the template is not an upgrade. A template is a **shopping/crafting
       target**, not an item the build owns: say so, and say cost is not verified (a 5-6 affix
       rare at these tiers is a trade or a multi-step craft). `--rare-slots none` disables it,
       `--rare-evals N` bounds its PoB evaluations (default 60, separate from the item limit).
       See `references/candidates-rare-templates.md`.
     - **Next step:** it writes up to 4 `compare` variants. Discovered or shortlisted is not
       accepted: only a `compare` result, the profile targets and the user's requirements put an
       item in the build.
     - **Cost:** it never prices anything. Say "cost not verified".
     - **What to record:** print its `GEAR BASIS` line into the spec as `"gear_basis"`.
   - **Re-derive after a major change.** When the main skill, the weapon class or off-hand, the
     ascendancy or the damage archetype changes, `build` flags it against `gear_basis`. Rerun
     `candidates` then. Keep the items whose reason still holds, and replace the ones whose reason
     depended on what changed. Never carry gear over unexamined.
   - `sweep <name>.filled.json`: run it once on the first valid build. It measures the standard
     mechanics the archetype could still add (`references/mechanics.json`: rage, frenzy, power
     and endurance charge sources, onslaught, intimidate, exposure, Precision level, culling) as one `compare`,
     switches the matching config on for each with its source named, and retries attribute-gated
     gems at lower levels. **A source the build's attributes can't meet is reported, never bought
     with off-tree attributes** (a Strength warcry or Berserk on a Dex/Int character stays out
     unless the user asks). Apply the `better` rows you accept, then rebuild.
   - `compare spec.json variants.json`: 2–4 variants, each testing one specific idea. For
     endgame builds, try at least two meaningful alternatives. Each variant gets a verdict;
     `rejected` means it breaks a requirement (the profile's targets or the user's requirements),
     whatever its DPS. A config-only variant is labelled an `assumption test` and is never
     recommended; a user-permitted alternative main skill is labelled separately.
   - Tree: start from the report's **costliest targets** and test a nearby alternative in
     `compare`. The `Pathing` line gives the total the targets cost and whether the search found
     anything cheaper than PoB's route; the travel is already minimal for the targets you named,
     so the lever is the target list, not the route. What actually wastes points, in order:
     (1) a far notable named for one stat (`Lightning Walker +14`): a closer notable or a mastery
     with the same stat usually costs a third; (2) two targets in opposite corners of the tree,
     each paying its own corridor: drop one and let the fill buy the stat nearby; (3) a jewel
     socket or keystone named without checking its cost. Check the `Tree fill` line: if it
     spent many points on one stat, the named targets may be missing a cheaper notable for it; if
     it found nothing worth taking, the tree is over-extended. Run `tree-audit` (optionally
     `--targets a,b`) only when the tree is large, clearly inefficient, or a real optimisation
     problem remains. Removing one node is a sensitivity test, not that node's exact value.
   - **Attribute repair.** Tighter routes no longer wander through +10 attribute nodes, so a
     build that met a Dex/Int/Str requirement by accident falls short. The driver spends the
     points the pathing saved on the cheapest attribute nodes for it before the fill (`Attribute
     repair` line), so the build stays valid and is never worse off than the old routing; treat
     those nodes as a placeholder and apply step 5's order (gem level, amulet base, suffix) in
     the next revision when that frees a point. A shortfall the saving can't pay for is the
     spec's own and stays an ERROR with the ordered options; the error says what the pathing
     changed. `"tree": {"attribute_repair": false}` makes every shortfall an error again.
   
   **Stop** when there are no critical items, every material item is fixed or explained, and
   the user's requirements are met. Also stop when the next round only promises marginal gains
   for added complexity. Advisory items never need to be cleared. Keep a change only if the
   build stays valid and gets better for the intended content.
   
   Details: `references/quality-review.md`.
7. **Deliver** (see section 5).

When the user asks about the current meta or what's strong this league, search the web
(e.g. poe.ninja builds, patch notes) rather than relying on memory.

## 4. Checking or improving an existing build

Ask the user for the PoB code itself (PoB: Import/Export Build > Generate > copy). Links like
pobb.in or pastebin can't be fetched from the sandbox.

```bash
python3 pob.py check "<code>" -o <outdir>     # or a .xml file / a file containing the code
```

Report the errors, warnings, and the stat picture in plain language, then suggest concrete
changes. In `check` mode, item-mod problems are warnings rather than errors, because imported
items can be legacy items from older patches. Old tree versions are flagged; PoB converts them on import.

To apply changes to an existing build: `decode "<code>" > build.xml`, edit the XML (gems are
`<Gem nameSpec=...>`, items are plain item text inside `<Item>`, the tree is the `nodes=`
list on `<Spec>`), then `check build.xml`. For large reworks, rebuilding it as a spec is
usually easier.

## 4b. Reference build analysis (improving this skill from a good build)

When the user gives a PoB code or `.xml` and asks to learn from it (e.g. "analyse this build
and improve the skill so it makes builds of comparable quality"), follow
`references/reference-analysis.md`:

1. Run `reference "<code>" --content <c> --investment <i>`: PoB facts, what each support and
   leaf passive is worth, conditions, gear dependencies, and plain signals. An invalid build
   is still measured as given, and its problems and unavailable values are listed. Equipped
   flasks switched off stay off unless `--adjust flasks-on` is given, which adds a separately
   labelled copy. Read the `Driver and PoB messages` and the saved-versus-recalculated
   lines: the export's own stats can differ from this PoB's (Auto level, an unset boss
   setting, another PoB version).
2. Normalise the reference: level, budget, content, config, floor DPS, max hits, recovery.
3. Find the few design decisions that explain its strengths.
4. Classify each as a skill deficiency, or as a difference explained by budget, level or
   content (no change).
5. Test one targeted change against a baseline generated with the current skill
   (`reference ... --against new.json`).
6. Label each change general, archetype-specific or reference-specific.

**Change the skill files only if the user asked for an update; otherwise report the proposed
changes.** Don't copy the reference or rebuild it unnecessarily. Never treat its headline DPS as
proof of quality. Keep PoB numbers and your interpretation clearly apart.

## 5. What to give the user

- The **user requirements line** from the report ("all met in PoB's result and in the exported
  build"), and any assumptions you made. If a permitted alternative was used, say so first.
- The **PoB import code** (printed at the end of the report, also saved as `<name>.pob.txt`
  next to `<name>.xml`). Put the code in a code block so it's easy to copy.
- **A pobb.in link** only when the user asks for a shareable link (or has said to always share).
  Upload the *final* version, after validation, audit and any optimisation: add `--share` to that
  last `build`/`check` run, or run `upload <name>.pob.txt`. Give the link only if the command
  prints `POBB.IN LINK`. Exit code 3 means the build is fine but the upload failed: say why, and give
  the import code instead. A rebuild after sharing makes the earlier link stale (the report says so).
  Re-share, and never present an old link as the current build. If the user wants local-only
  output, never use `--share` or `upload`. Details: `references/sharing.md`.
- A short overview: concept, main skill and links, key notables/keystones (say which were named
  and that the rest were filled by measured value), ascendancy order,
  the headline numbers from PoB: configured DPS **and** the absolute sustained-floor DPS/EHP,
  life/ES, resists, the weakest max hit, defensive layers and recovery.
- The material weaknesses left and why each is acceptable (or what would fix it). Don't present
  EHP or a layer count as overall survivability.
- The profile assumed (investment, content, mode) and the config assumptions that matter.
- Gear priorities: which uniques are build-enabling, major or replaceable (from the audit), with
  a tested cheaper alternative where it matters. No prices: say cost is not verified.
- Variations tried with `compare` and why they were kept or rejected.
- Equipment candidates. Report:
  - how many `candidates` measured, against its limit, and how many rare-template affix
    evaluations it spent;
  - the rare templates for weapon and amulet (lines with tiers), and whether the equipped item,
    a unique or the template won the slot;
  - the shortlist that matters and which ones went to `compare`;
  - what was accepted and why;
  - for each, the conditions it needs and what PoB can't model about it.
  
  Keep discovered, measured and accepted apart.
- For league starters: the progression section of `references/quality-review.md`.
- Be upfront about anything the tools couldn't verify (see limits below).

## Limits to keep in mind

- **Cluster jewels**: equip the jewel in an allocated cluster socket and name its notables/keystones in
  `tree.cluster` (spec reference); they are allocated after the jewels load. Timeless jewels and
  Forbidden Flame/Flesh pairs are not validated.
- **Special mods**: essence-only, veiled and fractured mods are only partly checked, and the
  `mods` listing can include some special-source tiers (keep rare rolls conservative). Influence:
  include e.g. `Shaper Item` in the item text. Eldritch implicits are fully checked (see the spec
  reference). Unverified lines show up as notes, not errors.
- **Uniques with conditional or random effects** (e.g. Bound Fate, Annihilation's Approach,
  ranged corruptions like The Utmost) get optimistic PoB numbers. Read the item before trusting a
  benchmark that ranks it first.
- **Resource sustain**: the report flags a main skill whose mana/life cost per second outpaces
  recovery. Channelled skills and Blood Magic builds are where this bites.
- **Calculations** are Path of Building's: excellent but not perfect for some minion, trigger
  and uptime mechanics.
- **Audit thresholds** are guidance, tuned in `references/audit-thresholds.json`; no item price
  data exists here, so affordability is judgment and cost is reported as not verified.
- **pobb.in** needs outbound access to pobb.in, which some sandboxes block. Pastes are public to anyone
  with the link, can't be edited, and are limited to 75 KB of code.
- **Speed**:
  - `--timing` prints the time per stage and the PoB call counts.
  - `--no-cache` forces a fresh calculation.
  - `--legacy` uses one PoB process per request and no cache, for comparing against older runs.
  - Details: `references/performance.md`.
- **Data freshness** = the installed PoB release. Run `setup.sh --update` when a new league
  launches.
