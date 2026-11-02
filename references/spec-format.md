# Build spec format (input to `pob.py build`)

A JSON object. Only `class` is strictly required; everything else has a default.
Complete, valid examples: `examples/burning_arrow_vigour_deadeye.json` (uniques, Exarch/Eater implicits, masteries) and `examples/rf_chieftain.json`.

```json
{
  "_meta": {"source": "...", "status": "..."},  // optional notes (examples use it); ignored by PoB
  "name": "Occultist Vortex",          // used for output file names
  "class": "Witch",                     // Marauder Ranger Witch Duelist Templar Shadow Scion
  "ascendancy": "Occultist",            // must belong to the class ("classes" lists them)
  "bloodline": "Chaos Bloodline",       // optional secondary ascendancy, if the tree has them
  "level": 92,                          // default 90; drives point budget & requirements
  "bandit": "None",                     // None (kill all, +1 point) | Alira | Oak | Kraityn
  "pantheon_major": "Lunaris",          // Solaris | Lunaris | Arakaali | TheBrineKing | None
  "pantheon_minor": "Abberath",         // Gruthkul Yugul Abberath Tukohama Garukhan Ralakesh Ryslatha Shakari | None
  "profile": {                          // quality-audit targets (references/quality-review.md)
    "investment": "medium",             //   league_starter | budget | medium | high
    "content": "bossing",               //   campaign | mapping | bossing | uber
    "mode": "softcore",                 //   softcore | hardcore
    "league_starter": false,            //   true adds the progression review
    "thresholds": {"min_chaos_res": 20} //   optional overrides of references/audit-thresholds.json
  },

  "tree": {
    "nodes": [                          // notables, keystones, jewel sockets, any node
      "Elemental Overload",             //   by exact name ...
      "Heart of Ice",
      26725                             //   ... or by node id (from "nodes" search), e.g. jewel sockets
    ],
    "ascendancy": ["Profane Bloom", "Void Beacon", "Frigid Wake", "Vile Bastion"],
    "masteries": [
      {"mastery": "Life Mastery", "effect": "+30 to maximum Life"}
    ],
    "fill": "balanced"                  // spend the points the targets leave over (balanced | dps | defence); see Tree fill
  },

  "skills": [                           // one entry per socket group (link)
    {"slot": "Body Armour", "main": true, "gems": [
      {"name": "Vortex", "level": 20, "quality": 20},
      {"name": "Controlled Destruction"},          // level defaults to max, quality to 20
      "Swift Affliction"                           // plain string also works
    ]},
    {"slot": "Gloves", "gems": [
      {"name": "Incinerate", "stages": 4,          // stage count PoB calculates (staged skills only)
       "skill_part": "Release"}                    // which PoB skill part (number or name)
    ]},
    {"slot": "Helmet", "gems": ["Malevolence", {"name": "Enlighten", "level": 3}]}
  ],

  "items": [
    {"slot": "Body Armour", "text": "Rarity: RARE\nDoom Wrap\nVaal Regalia\nItem Level: 86\n+92 to maximum Energy Shield\n..."},
    {"slot": "Belt", "unique": "Darkness Enthroned"},
    {"slot": "Helmet", "unique": "Starkonja's Head", "variant": "Current"},
    {"slot": "Gloves", "text": "Rarity: RARE\n...", "exarch": "6% increased Damage per Frenzy Charge",
     "eater": "Inflict Fire Exposure on Hit, applying -16% to Fire Resistance"},
    {"slot": "Flask 1", "base": "Divine Life Flask"},
    {"slot": "Jewel", "text": "Rarity: RARE\n..."},   // auto-placed in an allocated socket
    {"slot": "Jewel 26725", "text": "..."}           // or a specific socket node id
  ],

  "config": {                           // any PoB config option ("config" command lists them)
    "enemyIsBoss": "Pinnacle",
    "usePowerCharges": true,            // explicit true or false is never overridden by the inference pass
    "useFrenzyCharges": false           // opt-out: a Blood Rage in the gems would otherwise be weighed as a source
  },
  "infer_config": true                  // default; false disables setting conditions from sources in the build
}
```

Conditional options you leave out are **inferred before the fill** from sources already in the build
(enabled gems, equipped items, allocated passives, ascendancy nodes), using the audit's evidence rules:
a *sustaining* source (on hit, skill in build, build mechanic) switches the option on and the report
says which source did it; an on-kill / while-moving / minimum-count source is listed but leaves the
option off. See the Tree fill section for how this interacts with sources the fill may add.

## Gear basis (when the equipment was chosen)

`"gear_basis"` records the build profile the equipment was chosen for. `candidates` prints it as
`GEAR BASIS`:

```json
"gear_basis": {"main_skill": "Elemental Hit", "weapon_class": "Claw (dual wield)", "ascendancy": "Warden",
               "archetype": "attack, hit, elemental"}
```

`build` compares it with the current build. If any of the four differ, it adds a note to re-run
`candidates` and re-check the items whose reason depended on what changed. It is not a requirement
and never makes a build invalid.

A rare template accepted from `candidates` is pasted into the spec as an ordinary rare item (the
`spec_item.text` it prints: `Rarity: RARE`, name, base, `Item Level`, the mod lines, bench craft
marked ` (crafted)`). Nothing in the spec distinguishes it from a found item afterwards, so the
write-up has to say it is a crafting target. See `candidates-rare-templates.md`.

## Event-triggered main skills (Cast when Stunned, Cast when Damage Taken)

PoB calculates a skill fired by an event the player doesn't control at the trigger's cooldown ceiling
(Cast when Stunned: 10 stuns a second), so its DPS and mana cost per second are not play. Give the event
rate you expect and the report evaluates both at it:

```json
"trigger_events_per_second": 3      // e.g. stuns per second inside a pack; triggers = events x the trigger chance
```

The report prints a `TRIGGER RATE` line (hit / combined / full DPS at that rate) and the sustain check uses
it; KEY STATS and the sidebar stay PoB's ceiling numbers. Without the key the report warns and gives the
ceiling rate. Remember that having any energy shield gives 50% chance to avoid being stunned (PoB applies
it), so a stun rate of 3/s means about 6 hits taken per second on an ES character.

## Requirements (what the user explicitly asked for)

```json
"requirements": {
  "main_skill": "Elemental Hit",            // the exact gem the user named
  "also_accept": ["Elemental Hit of the Spectrum"],  // only alternatives the user said are acceptable
  "class": "Ranger",
  "ascendancy": "Warden",
  "gems": ["Trinity"],                      // other gems the user named: present and enabled
  "uniques": ["Mortem Morsu"],              // equipped
  "passives": ["Elemental Equilibrium"],    // keystones / notables / ascendancy notables allocated
  "weapon_type": "Claw"                     // only when the user named one (PoB base type: Claw, Bow, Wand...)
},
"assumptions": ["melee Elemental Hit needs a melee weapon: claws chosen"]   // inferred; shown, never enforced
```

- **Fill it only from the user's words.** Everything is optional; leave out what the user didn't
  specify.
- **Names must be PoB's exact names.** The preflight reports unknown keys, wrong types, names
  that don't exist in this PoB version, an ascendancy of another class, and a spec that already
  breaks a requirement.
- **Gem identity is PoB's `variantId`, which is unique per gem.** A transfigured gem shares its
  base gem's `gameId`, and the `gemId` written into exported builds is that shared id: Elemental
  Hit and Elemental Hit of the Spectrum are both `Metadata/Items/Gems/SkillGemElementalHit`. So
  those are never used to decide a match. Vaal and Awakened gems are separate gems too (and Awakened
  gems are rejected by the preflight unless the spec sets `"legacy_gems": true`: drop-disabled since
  3.29, Standard only).
- **A requirement is checked:**
  - before PoB starts;
  - on PoB's result: main skill (`main_skill_gem`), class, ascendancy, gems, uniques, passives,
    weapon base type;
  - on the exported XML: main skill by `variantId`/`skillId`, class, ascendancy, gems, uniques,
    passives;
  - on every `compare` candidate and `tree-audit` removal.
- **What a broken requirement does:**
  - the build is INVALID and saved only as `<name>.invalid.*`;
  - a candidate is `rejected: breaks user requirement`, whatever its numbers;
  - a variant may not change the requirements;
  - `tree-audit` never removes a required passive.
- **A user-permitted alternative (`also_accept`)** is valid, but is reported as an alternative and
  labelled separately in `compare`.
- **To check an existing build against requirements:** run
  `check <build> --requirements req.json`.

## Details

**Tree.** Targets are allocated greedily, nearest first, using Path of Building's shortest
paths, so travel nodes are added automatically. The report lists each target with the points it
cost. Names must match exactly (case-insensitive); unknown names get suggestions. Mastery
`effect` is a substring of the effect text; `mastery` (optional) narrows which mastery type.
A mastery needs an allocated notable in its cluster: list that notable in `nodes`.

**Tree fill.** Named targets rarely add up to the level's point budget, and listing every small
life or damage node by hand is slow and error-prone. `"fill"` spends what is left over after the
targets, ascendancy and masteries are allocated and the build has been calculated once:

```json
"fill": "balanced"
"fill": {"objective": "dps", "reserve": 2, "max_cost": 3, "max_recalcs": 400}
```

- `objective`: `balanced` (1 × DPS% + 1 × defence%), `dps` (DPS weighted 4:1), `defence` (defence
  weighted 4:1). Defence% combines life+ES%, weakest-max-hit% (physical/fire/cold/lightning), hits
  avoided% (evade and attack block for attacks; suppression and spell block for spells, as the
  reduction in hits that land) and recovery% (leech, regen and on-hit per second relative to life,
  capped), so avoidance and recovery masteries count; PoB's stepped TotalEHP is not used. Each candidate is measured by PoB on the real build (the calculator
  PoB's tree-power display uses), so a node is worth what it changes for *this* build: an attribute
  node that unlocks nothing scores 0, a fire node on a cold build scores 0.
- Candidates: small nodes one step from the tree, notables up to `max_cost` steps (default 5,
  travel included; was 3, which hid 4–5 point clusters from the fill), and **mastery effects** on mastery nodes next to an allocated notable (each
  effect measured separately; an effect already selected elsewhere is skipped). Never keystones,
  jewel sockets, cluster or ascendancy nodes.
- **Conditions.** A candidate whose stat text is a sustaining source (on hit, skill in build) for a
  boolean config option that is off (`useFrenzyCharges`, `buffOnslaught`, `conditionEnemyIntimidated`,
  ...) is measured with that option on and, if it wins, the option is switched on with it and
  reported (`Config set by the fill`); the pinned `.filled.json` carries the flag. The same holds for
  the numeric `multiplierRage`: a node granting Rage on (melee) hit in a melee-attack build is
  measured with Rage at PoB's maximum and, if it wins, the fill sets that number. A value you pre-set
  in `config` hides the source's value from the fill, so leave such options to it unless the source
  is already in the build. **Sources already in the build are detected automatically**: before the
  fill, `build` scans enabled gems, equipped items, allocated passives and ascendancy nodes with the
  audit's evidence rules and switches on every boolean option in this list (and `multiplierRage` in a
  melee build) that has a *sustaining* source; the report lists them under `Config set from sources
  already in the build` and the `.filled.json` carries them. A non-sustaining source (on kill, while
  moving, minimum count) is listed but not set. A key you write in `config` yourself — including an
  explicit `false` — is never overridden, so `"useFrenzyCharges": false` opts out.
  An ailment condition (`conditionEnemyShocked`, `...Ignited`, `...Chilled`) also needs the main skill
  to deal that element: "10% chance to Freeze, Shock and Ignite" is a shock source for a lightning
  build and nothing else. When the fill switches an option on, cached scores whose stat text names it (`+1 to Maximum
  Frenzy Charges` after `useFrenzyCharges`) are dropped and measured again, so the charge notables
  near the tree are valued on the build that now has charges.
- **Defence score.** PoB's effective hit pool % (pool, every mitigation layer and avoidance against
  the configured hit mix), the weakest max hit %, avoidance % and recovery %, averaged. The life
  pool is not scored on its own: it is inside both EHP and the max hits, and scoring it separately
  made a life small outrank +1 endurance charge or a taken-as implicit.
- Greedy by value per point with lazy re-verification: scores are cached from the tree as it was
  when first measured, and the winner is re-measured on the current tree before it is committed
  (so a suppression node is not taken after the cap is reached). Still a heuristic, not an optimum;
  the report lists every node and mastery added with its measured value.
- `reserve` (default 0) keeps that many points unspent. `max_recalcs` (default 400) caps the
  work; about 150–200 PoB calculations (2–3 s) spend 18 points.
- **After the fill**, travel nodes that no longer connect anything (a fill notable's route made a
  target's earlier route redundant) are pruned and the freed points spent in a second fill round
  (`Pruned after the fill` line). Fill-chosen nodes themselves are never pruned.

## Pathing (how `tree.nodes` are connected)

Connecting named targets to the class start is a Steiner-tree problem. The driver first lets PoB
allocate nearest-first (its own behaviour), then simulates three connection orders on the tree graph
(nearest-first, farthest-first, one-step lookahead on the total), prunes redundant travel from each
(travel notables included), re-routes each target from the finished tree while that saves points,
and keeps the cheapest connected result. A result cheaper than PoB's replaces it; a tie keeps PoB's
route, because equal-cost routes differ in what they bring within the fill's reach and no static
rule can judge that. Measured on the bundled fixtures and on random target sets, the saving is
0–2 points (about 1%): nearest-first is already close to optimal on this tree, so the report's
`Pathing` line is mainly a confirmation that the route is not where the points go. A pinned tree
(`tree.allocated`) is never re-pathed or pruned. `"tree": {"prune": false}` disables both prune
passes and the attribute repair; `POE1_BUILDS_GREEDY_PATHING=1` skips the search entirely.

**Attribute repair** (`"tree": {"attribute_repair": true}`, the default). The old routes supplied
Str/Dex/Int by accident; the tight ones don't. Before the fill, the driver spends **the points the
search saved** on the attribute nodes that cover an unmet requirement at the fewest points (and, with
points to spare, the audit's margin of 10), so the build is never worse off than nearest-first left
it; beyond the saving it covers at most what the old route's travel supplied. The nodes are reported
(`Attribute repair` line) and never pruned. Anything left is the spec's own shortfall and stays an
ERROR with the ordered options; the error names what the pathing changed. `attribute_repair: false`
restores the strict policy (every shortfall is an error the model fixes by naming a node, a suffix, an
amulet base or a gem level).
- A node that would drop an elemental resistance below the cap, or disable the main skill, is
  never chosen. Nothing is ever removed, so requirements can't be broken.
- Attribute nodes score 0 unless they change DPS or life, so the fill never spends points on
  attributes; an attribute shortfall is reported in the fill line and met through the order in
  `quality-review.md` §3b (gem level, amulet base, a free suffix, then a named node).
- The report's `Tree fill` line shows the nodes, their cost and value. Hand-pick a node instead
  (list it in `nodes`) when the fill's choice doesn't suit the concept, and keep `fill` for the rest.
- **Pinned tree.** A valid `build` of a spec with `fill` also writes `<name>.filled.json`: the same
  spec plus `"tree": {"allocated": [node ids...]}`, the whole tree exactly as built. With
  `allocated` present the driver sets those nodes directly (as PoB's import does), skips pathing
  and ignores `fill`, so the build is reproduced exactly and about 3 s faster. Use that file for
  `compare`, `tree-audit` and `candidates`: a variant that changes items, gems or config then
  tests one idea on the delivered tree. A variant that edits the tree (`tree_add`/`tree_remove`,
  masteries, ascendancy nodes, a level change) drops `allocated` and re-derives the tree from the
  targets, with `fill` re-spending the points, so `tree-audit` on such a spec measures each target
  against the fill's best alternative use of its points (it says so).

**Cluster jewels.** Socket a cluster jewel in an allocated cluster socket (`"slot": "Jewel <socket id>"`,
the outer *Large Jewel Socket* ids from `nodes "Jewel Socket" --type Socket`), then name the notables or
keystones inside it in `"tree": {"cluster": ["Hollow Palm Technique", ...]}`. They exist only after the
jewels load, so the driver allocates them through the socket after the first calculation and reports
them (`Cluster jewel nodes allocated after loading the jewels`). A name not found in a socketed cluster
jewel is an error. Cluster node ids (65536+) never appear in `allocated`.

**Skills.** `slot` decides which item the gems sit in. Valid slots: `Weapon 1`, `Weapon 2`
(offhand/shield/quiver), `Helmet`, `Body Armour`, `Gloves`, `Boots`, `Amulet`, `Ring 1`, `Ring 2`,
`Belt`. Common aliases (`body`, `helm`, `offhand`...) are accepted. Mark exactly one group
`"main": true`; it is the skill PoB calculates DPS for. Gem names are the in-game names; a
" Support" suffix is optional. Use `"enabled": false` for swap gems you don't want counted.

**Stages and skill parts.** Some skills have *stages*: Incinerate, Blade Blast of Unloading,
Charged Dash, Scourge Arrow, Flameblast, Winter Orb, Divine Ire and others, 50 in this PoB
version. Without `"stages"`, PoB calculates them at the **maximum** stage count.
- Set `"stages": n` on the gem to the count the build actually sustains. The preflight rejects
  `stages` on skills and support gems without stages, and values below 1.
- After calculating, the build is invalid when the count is outside this build's real range (PoB
  would otherwise clamp it silently). The report then shows what PoB used.

Other skills have *parts* (e.g. Incinerate "Channelling" / "Release", Explosive Arrow "Maximum
Sustainable Fuses", Blade Blast of Unloading "Blade Hits Per Cast" / "Per Sec").
- `"skill_part"` takes the part number or name, and is checked against the skill's parts.
- `"minion_skill"` (minion gems) picks which of the summoned minion's skills PoB calculates, by number
  or name; the report lists the minion's skills. PoB defaults to the first, which can be a cooldown
  skill: Herald of Agony's crawler shows Mortar (6 s cooldown, ~2% of the real DPS) unless
  `"minion_skill": "Tail Spike"` is set. Compare the minion's skills once and set the sustained one.
- `gems <name>` shows `stages` and the numbered parts. The build report and the audit list them
  again, with each part's DPS.

**Sockets.** If an item's text has no `Sockets:` line, sockets are generated from the gems in
that slot (each group becomes its own linked group, colours matching the gems). Write a
`Sockets: B-B-B-B-B-B` line yourself when you want specific colours (e.g. a unique with fixed
white sockets).

**Item text** is Path of Exile's own copy format (Ctrl+C on an item):

```
Rarity: RARE
Doom Wrap
Vaal Regalia
Item Level: 86
+92 to maximum Energy Shield
95% increased Energy Shield
+64 to maximum Life
+42% to Cold Resistance
+18% to Chaos Resistance (crafted)
```

- List only the explicit mods; implicits are handled as described below.
- Mark bench crafts ` (crafted)`, fractured mods ` (fractured)`.
- Influenced items: add a line such as `Shaper Item` or `Hunter Item` after the base line.
- `Item Level:` matters for which tiers are legal; it defaults to 86 for checking.
- Anoint an amulet: `"anoint": "<Notable Name>"` on the item entry (works for uniques too), or the
  line `Allocates <Notable Name> (enchant)` in rare item text.

## Implicits

What an item may carry depends on its rarity, and the validator enforces it:

| Item | Allowed implicits |
|---|---|
| Normal / Magic / Rare | Its **base implicit** (e.g. Coral Ring `+(20-30) to maximum Life`), **or** Searing Exarch / Eater of Worlds implicits (helmet, body armour, gloves, boots only; they *replace* the base implicit), **or**, if Corrupted, 1-2 corruption implicits (they replace the base implicit) |
| Unique | Its **own implicits** from the unique database (selected variant only; can differ from the base type's), **or**, if Corrupted, 1-2 corruption implicits replacing them. **Never** Exarch/Eater implicits |
| Corrupted / mirrored | No Exarch/Eater implicits |

**Base implicits are added automatically** to Normal/Magic/Rare items that list none (Path of
Building itself does not add them to hand-written items). They roll at the midpoint; set
`"implicit_roll": 0..1` on the spec or on an item to change that. The report notes which were added.
Items with `exarch`/`eater` keys don't get the base implicit, because eldritch implicits replace it.

Item keys for implicits:

```json
{"slot": "Ring 1", "text": "...", "implicits": ["+29 to maximum Life"]},                  // exact roll
{"slot": "Ring 2", "text": "...", "corrupted_implicits": ["+16% to all Elemental Resistances"]}, // Vaal implicit, marks Corrupted
{"slot": "Body Armour", "unique": "Kaom's Heart", "corrupted_implicits": ["6% increased maximum Life"]},
{"slot": "Amulet", "unique": "Defiance of Destiny", "corrupted": true},                   // corrupted, implicit unchanged
{"slot": "Gloves", "text": "...", "exarch": "6% increased Damage per Frenzy Charge",
 "eater": "Inflict Fire Exposure on Hit, applying -16% to Fire Resistance"}
```

Lookups: `bases "<base>"` shows the base implicit, `uniques "<name>"` the unique's implicits,
`mods "<base>" --corrupted` the corruption implicits for that item class, `mods "<base>" --eldritch`
the Exarch/Eater implicits.

**Searing Exarch / Eater of Worlds implicits.** Add `"exarch"` and/or `"eater"` to a Normal, Magic or
Rare body armour / helmet / gloves / boots entry. The item markers and implicit flags are added for you,
and each line is checked: right type (Exarch vs Eater), allowed on that slot, value within the highest
tier, at most one of each, not on uniques, not on corrupted/mirrored items, no base implicit left
alongside. "While a Pinnacle Atlas Boss is in your Presence" versions only apply near pinnacle bosses;
prefer the plain versions for honest numbers. In raw item text the equivalent is a `Searing Exarch Item`
/ `Eater of Worlds Item` line plus `{exarch}` / `{eater}` before the implicit line.

**Uniques** come from Path of Building's database with mid-roll values; `variant` picks a
legacy/alternate version by substring of its variant name (`uniques "<name>"` shows them).
