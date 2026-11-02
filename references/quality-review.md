# Quality review (after the build is VALID, before delivering)

`build` and `check` run a quality audit once the build is valid (`--no-audit` skips it, about 2 s
faster). Every number in it is measured by Path of Building: the driver switches one thing off,
recalculates and restores it. Thresholds live in `references/audit-thresholds.json` and are
advisory; a spec can override them under `profile.thresholds`.

The report groups findings by severity, ordered by priority:
- **CRITICAL**: must fix. Examples: uncapped elemental resistance for endgame content, or a
  weakest max hit under half its target.
- **MATERIAL**: fix when a reasonable alternative exists, otherwise explain the trade-off.
  Examples: a max hit below target, recovery that only works with a flask up, an over-budget rare.
- **ADVISORY**: explain or accept. Examples: config options that need a confirmed source, flask
  uptime, utility groups with no PoB-visible effect.

Passed checks are summarised on one line (`--audit-detail` lists them). The final `STATUS` line
says whether the build is ready. Audit findings never make a build invalid; only PoB errors do.
Don't chase every advisory line.

Set the profile in the spec (CLI `--investment/--content/--mode` override it):

```json
"profile": {"investment": "league_starter|budget|medium|high", "content": "campaign|mapping|bossing|uber",
            "mode": "softcore|hardcore", "league_starter": true}
```

If the user didn't say, pick the most plausible values and state them in the answer. The audit warns
when they're missing.

## 1. Honest configuration

- The audit lists every non-default config option with what it is worth (`without`).
- It also gives a **sustained floor** line: the configured DPS and EHP, then the absolute DPS and
  EHP with all flasks off and all conditional options reset, and the % difference. For example:
  `Configured: DPS 2.60M, EHP 75.4k. Sustained floor: DPS 1.86M (-28.3%), EHP 61.4k (-18.6%)`.
- The floor comes from the same recalculation the audit already makes. Report both the
  configured and the floor numbers.
- For each REVIEW line, check that the build has a real source and realistic uptime. Charges need a
  generator; "enemy is shocked/ignited/intimidated" needs something that applies it; kill conditions
  don't hold on single-target bosses. If there is no source, turn the option off. If uptime is
  partial, say so.
- **Evidence for each conditional option** (advisory; it never estimates uptime).
  - The audit lists what it found:
    - PoB's own calculations, e.g. "main skill: 93% chance to Shock per hit". A 0% chance is
      shown as counter-evidence.
    - PoB's option gates and flags, e.g. "Trinity is in an enabled skill group" or
      "Condition:CanGainRage".
    - Source lines from allocated passives, equipped gear (active set) and enabled skill gems.
      A line counts only if it generates the condition ("chance to Blind Enemies on Hit"), not
      if it uses it ("against Blinded Enemies", "per Frenzy Charge"). Support gems count by name
      only.
  - Each source is tagged with how it triggers:
    - sustaining: on hit, skill in build, build mechanic;
    - not sustaining: on kill, steal (needs an enemy with charges), while moving, minimum count only,
      some hits (random element), other.
  - A status of `no source found` means nothing was found, which isn't proof there is no source.
    `not assessed` means the option has no search rule.
  - Count options (Rage, resonance, …) show PoB's maximum where PoB reports one. When doubling the
    value changes nothing, it is "PoB's cap". The DPS at half the value is also shown.
  - **Conditional performance** adds two PoB recalculations between configured and the floor
    (the floor line above is unchanged):
    - conditions with no source turned off;
    - conditions without a sustaining source turned off.
  - Both still assume full uptime for what they keep. Present them as "what the evidence
    supports", never as expected uptime. The floor removes everything, so it is a lower bound,
    not a prediction.
  - The search is text-based (English mod text). A source worded unusually can be missed, and a
    source's conditions (an enemy that has charges to steal, a boss that dies rarely) aren't
    judged. Read the evidence lines before relying on a status.
- **Random-element skills** (PoB skill type RandomElement: Elemental Hit, Wild Strike, …).
  - PoB calculates one selected part. The audit gives every part, grouped by element, and the
    range across the parts PoB doesn't flag as area.
  - It never blends them. A blend needs:
    - the skill's selection rule, which sets how often each element occurs;
    - each element calculated in its own consistent state.
  - Settings that depend on the element, such as Elemental Equilibrium's "Enemy was hit by X
    damage", can't be true the same way for every element.
  - To give a blended figure, calculate each element with its consistent settings, then weight
    by frequency. Say how, and label it as derived.
  - Worked example: Elemental Hit never picks the same element twice in a row.
    - If the choice between the other two is uniform, each element is used 1/3 of the time.
    - Each element then follows a different element, so its "hit by" setting must name another
      element.
    - On a reference build this gave 3.06M. Averaging PoB's parts under one config gave 2.79M,
      and the headline part alone was 2.96M.
- `enemyIsBoss` must match the content: Pinnacle for bossing, Uber for uber bosses. PoB's default is
  Pinnacle.
- Flasks count as always on in PoB. Mention flask-dependent numbers as such, or make sure the build
  sustains the flasks (charge recovery, duration).
- **Stages and skill parts are reported by the audit.**
  - The report shows the stage count PoB used, the DPS at the minimum stage count, the selected
    skill part, and the DPS of every other part.
  - An unset stage count means PoB used the **maximum**. When that inflates DPS by 10% or more,
    it's a MATERIAL finding: set `"stages"` on the gem to what the build sustains and say why
    (for example "channels to full stages for bosses"). The maximum is fine when the build can
    hold it; stating it is what clears the finding.
  - Fewer stages isn't always lower damage. Release-type parts can do more at 1 stage, so read
    the signed change.
  - For staged skills, report both numbers to the user.
- Other skill-level assumptions are still invisible to the audit: the number of Mirage Archers,
  totems or minions, and "projectiles hitting the target". Check them in the sidebar and state
  them.

## 2. Defence

The audit keeps these separate, because neither EHP nor a layer count is overall survivability:

- **Max-hit protection.** Every damage type's max hit, then the **weakest relative to its target**
  (scaled by level and HC).
- **Resistances.** Lowest elemental and chaos.
- **Layers, each mechanism counted once.**
  - Mitigation: phys damage reduction.
  - Avoidance: evade; block (attack and spell together count as one); suppression; damage
    avoidance.
  - Hit buffers: mind over matter; guard or aegis, only when the pool is at least 5% of life/ES.
  
  Resistances, max res above 75 and recoup aren't counted as layers, because other checks
  already cover them. A token Steelskin (86 absorb on an 11k life pool) is listed as "not
  counted".
- **Guard skills are temporary protection.**
  - The audit reports:
    - the guard setup (gem, level, trigger or self-cast);
    - PoB's own uptime for it, from duration and cooldown;
    - max hit for every damage type **with the guard down**, and with it up.
  - A guard is never counted as a persistent layer, and the max-hit check uses the guard-down
    numbers. PoB's EHP and headline max hit assume the guard is up, so don't present them as
    permanent.
  - A guard under 5% of the life/ES pool gets an ADVISORY with facts:
    - the trigger's level limit (Cast when Damage Taken N only triggers skills up to a level
      requirement; PoB doesn't enforce it, but the build check does);
    - the highest guard level the character level allows;
    - attribute shortfalls;
    - when Automation becomes available.
  - **Choosing a guard:** compare the options that fit the character with `compare`: a
    higher-level trigger setup, self-cast, Automation, a different guard (e.g. Arcane Cloak for
    int builds), or no guard and the sockets used elsewhere. Weigh level and attribute
    requirements, sockets, cooldown and uptime, and what the guard adds while up compared with
    max hit while down.
  - No single setup is right for every archetype. Keep the weak guard if the sockets have no
    better use, and say so.
- **Recovery for the target content.**
  - **Net** life regen after degens: gross and degen are shown. Blood Rage, RF and similar
    degens can make a high gross number misleading.
  - Leech while hitting, and recoup.
  - For ES builds, recharge counts in campaign and mapping but not bossing, where constant hits
    interrupt it (configurable).
  - Chaos immunity (Chaos Inoculation) replaces the chaos resistance check.
  - If the requirement is met only because a flask is up, it's flagged as material.
- **Main-skill cost sustain.**
- **Ailments.** Freeze for bossing (noted when the immunity comes only from a flask), plus the
  other avoidances.

PoB's TotalEHP can jump in steps (about 10–15%) on small changes, because it is built from a whole
number of hits. When EHP moves against life and every max hit, judge the change on life and max hits.

A different defensive design is fine if it meets the essential checks. Explain material
weaknesses and accepted trade-offs in the answer instead of trying to clear every line. Something
PoB doesn't show (a Pantheon power, a playstyle habit) is an explanation, not a number to invent.

## 3. Gear realism

- `investment` sets how many top-tier affixes one rare may have. This is a **coarse realism
  heuristic, not a price estimate**. Over the limit is legal but a demanding combination of
  rolls, so check whether lower tiers would do.
  - "Top tier" means the best tier the game has for that mod on that base: the highest required
    item level in PoB's mod data. It is not measured against the item's own item level, so an
    ordinary roll on a low-level item doesn't count.
  - Mod groups with only one tier are not counted.
  - Flasks are excluded; their mods are still checked for legality.
  - The finding gives the item level, and says when those tiers need a higher item level than
    the character's level.
  - The item data also carries `top_tier_at_ilvl`: the best tier the item's own level allows.
    That is information only.
  - A high tier is a rarity signal, not a price.
- For **league_starter / budget** builds, the audit also lists each rare's crafting steps
  (Exarch/Eater implicits, influence, bench crafts, multimod, fractured, corrupted). Assess:
  - which uniques are mandatory, and whether the build works without them;
  - whether the rare mod combinations are plausible together;
  - how complex the crafting is;
  - whether the premium rolls are actually needed;
  - what fallback gear exists.
- Never claim an item fits a price (a Divine budget, "cheap") without market evidence. If
  affordability can't be established, say so.
- Unique dependency: the audit removes each unique (slot left empty) and classifies it:
  - **build-enabling**: the main skill stops working without it;
  - **major dependency**: removing it costs 25% or more of DPS or EHP;
  - **replaceable**.
  
  For build-enabling and major uniques on league_starter or budget builds, test a rare stand-in with
  `compare` and report what the substitution costs.
- **There is no price data.** Never quote prices from memory as facts. Say "cost not verified" and,
  if useful, describe the item's rarity in general terms (for example, a common league-start unique
  or a chase item), flagged as your judgment.

### Equipment candidates (`candidates spec.json`)

Gear should come from the build's mechanics, not from memory of what a skill usually wears.
`candidates` runs one PoB calculation of the build, then:

1. **Profile.** From PoB's calculation:
   - the main skill's types and flags, and the weapon types it can use;
   - the equipped weapon class and off-hand;
   - the damage types of every skill part (a random-element skill counts all its elements);
   - the per-hit ailment chances;
   - hit or damage-over-time, and attack, spell or minion.
2. **Slot contributions.** Each gear and flask slot has its item removed, one calculation each,
   against the same baseline. The weakest slot of interchangeable slots (rings, flasks) is the
   target. A slot whose item changes movement speed, recovery, ailment avoidance, attributes or
   resistances is not treated as empty.
3. **Discovery.** Every unique, plus the craftable flask-slot bases, is matched to the slots it
   fits. Each line is read through PoB's own parser and tested the way PoB applies it:
   - the line's weapon and skill flags against the main skill;
   - its conditions against the current state;
   - the config option that would set a missing condition, with the audit's evidence for it.
   
   An item is *compatible* when at least one line applies. Uniques PoB marks "no longer
   obtainable" are skipped, and so are slots holding a unique the user required.
4. **Ordering, not acceptance.** A static score orders compatible items:
   - lines restricted to what this skill is count most;
   - lines needing a condition the build has a source for count somewhat less;
   - general stats count little and are capped;
   - lines with an unused stat, or a damage type the skill doesn't deal, count least;
   - several conditions on one line add up;
   - a line's size is compared with what the build already has of that stat.
   
   A passive that names the item type adds to the rank but never makes an item compatible.
5. **Measurement, capped** (`--max-evals`, default 60):
   - the best few per slot, plus up to 10 craftable bases, as drop-in replacements;
   - the best measured craftable base with its best single affixes, then their pair;
   - a condition scenario for items that need a condition the build has a sustaining source
     for. The config change alone and the item on top of it are reported separately.
6. **Ranking** (no PoB):
   - **rejected**: the main skill deals no damage, or the item breaks a profile target the
     baseline meets;
   - otherwise: promising, trade-off, no measured gain or worse.
   
   A loss on the build's weakest metric counts double. Attribute shortfalls and other changed
   effects are noted. Under a low or medium budget, a craftable item goes first on a near tie;
   its cost is still not verified.
7. **Beyond drop-ins.** Two more lists:
   - **different configuration**: items whose lines need another weapon class, or something
     the main skill doesn't use;
   - **passives** (ascendancy, or nearby notables) that name a promising item's type or supply
     a condition it needs, with their point cost.

What to do with it:
- Put the shortlisted ones that fit the budget into `compare` (the variants file holds up to 4).
  Present a configured DPS gain as configured, not guaranteed.
- Say what PoB can't model:
  - a flask-slot item counts as always on, so its duration, charges, cooldown and the costs it
    puts on you (Mana Burn, for example) are not simulated;
  - how often a condition holds;
  - lines PoB doesn't calculate;
  - which skill part was measured.
- A matching keyword never makes an item useful: only the measurement and `compare` do.
- **What it doesn't search:** rares (write them from `mods`), jewels and item combinations.

### Cheap slots and breakpoints

The audit lists **cheap slots left unused**: an amulet without an anoint (any notable, allocated
for free with `"anoint"`; the build check errors if the anoint didn't allocate it), rare armour
pieces on their base implicit (Exarch/Eater implicits are standard from mid investment; for a
life/armour build the Eater body (12%) and helmet (8%) "Physical Damage from Hits taken as <element>"
implicits are the defensive pick — `sweep` measures both, choosing the element with spare
resistance, and the over-cap column shows whether the +res implicit they replace was spare), and plain
flask bases (no charge/duration prefix, no immunity suffix). It also reports **breakpoints**: spell
suppression under 100% with the gap and the nearest sources (material under 80%), and block under
its cap. Fill these before spending passive points on the same thing.

## 3b. Attributes

The report's `ATTRIBUTES` line gives have / need / margin per attribute and what sets each need
(a gem at its level, or an item). A shortfall is a PoB error; a margin under the target (10) is an
ADVISORY, because the next gem level or support swap breaks it. On either, the section lists the
ways to meet it for this build, cheapest first, each with what it displaces:

1. **Gem level** of the driving gem: the highest level whose need leaves the margin. `compare`
   measures what the levels cost.
2. **Amulet base** with the attribute as implicit, when the amulet is not a unique and its current
   implicit isn't needed.
3. **A free suffix** on an equipped rare, magic item or jewel that can roll the attribute, with the
   best tier its item level allows. The line says when no equipped item qualifies. A suffix
   competes with resistance, so this is cheap only while suffixes are spare.
4. **Attribute nodes** within two points of the tree (+30 or +20 for one point). Right when the
   gap is large or the suffixes are gone: a point for +30 beats a suffix that could hold 45%
   resistance. Name the node in `tree.nodes`; fill points are never used for attributes.

The order is a default, not a rule: the real trade is suffix slots against passive points, and
the section shows both so the choice is made on this build's numbers.

## 3c. Mechanic sweep (`sweep <name>.filled.json`)

The standard mechanics an archetype can add are not something to recall: `references/mechanics.json`
lists them with the cheapest source each (a support in the main link replacing the weakest measured
support, a gem in a utility group, a mastery, a flask that's already there, an Eater implicit) and
the config option the source switches on. `sweep` turns every applicable entry into a `compare`
variant on the build as it stands, skips entries the build already has, and for gems whose
attribute requirement the build fails, retries at levels 16 / 12 / 8 / 4 and reports the highest
that fits. Nothing is bought with off-tree attributes: a Strength warcry or Berserk on a Dex/Int
build is listed as not pursued. Apply the rows you accept (a config switched on by a variant is
honest only if the audit's evidence for it is sustaining) and rebuild.

## 4. Main skill and supports

1. Start from the gem tags: `gems "<skill>"` shows them, and `gems <tag> --tags --support` lists the
   candidate supports.
2. Test 2–4 alternatives in `compare` with `gems: [{group: "main", replace: {old: new}}]`.
3. Also weigh what PoB doesn't rank:
   - clear (pierce, chain, area);
   - consistency (no reliance on rare conditions);
   - cost and sustain;
   - socket colours (`*` in the report).
4. The audit also checks each non-main skill group. A group with no measurable effect must be
   justified as utility. A group that *raises* DPS when removed is a conflict to fix, such as two
   marks or curses, or a "less damage" support.

## 5. Passive tree

Start cheap. The build report already lists each target's point cost and names the **costliest
targets** (7 or more points), and the `Tree fill` line shows where the leftover points went and
what each node measured (`tree.fill` in `spec-format.md`). A spec without `fill` that leaves
points unspent at the target level is unfinished.
1. Ask whether each of those serves the concept. Test the doubtful ones against a nearby
   alternative in `compare` (`tree_remove` plus `tree_add`).
2. Run `tree-audit spec.json` only when the tree is large, clearly inefficient, or a real
   optimisation problem remains after that. On a spec with `tree.fill`, each target's points are
   re-spent by the fill, so the rows compare the target with the fill's best alternative use of
   its points: a target at or below 0 there is one the fill would rather spend elsewhere. `tree-audit --targets a,b` limits it to the
   suspects. It removes each target in turn (with the travel nodes only it needed) and reports
   points freed, DPS%, EHP%, life and value per point. A full run is about 35 s for 22 targets.
3. Treat a single removal as a **sensitivity test**. Nodes interact (life masteries that need 6
   life masteries, attribute thresholds, multiplicative scaling), so one node's number isn't its
   isolated value.
4. Keep a change only if the build stays valid and gets better for the intended content.
- An "invalid without it" note means the node pays for an attribute or requirement. Count that
  before removing it.

## 6. Variations: at least two for endgame builds

`compare spec.json variants.json` builds each variant and prints valid, DPS, EHP, life, min max hit,
lowest res and points next to the original. It also gives each variant a verdict and prints
`RECOMMENDED`.
- **better**: at least 1% gain on DPS, EHP or min max hit, and no more than a 1% loss on any of
  them.
- **trade-off**, **worse**, **no meaningful change**.
- **rejected**: it breaks a profile requirement the original meets (lowest resistance, chaos res,
  min max hit), whatever its DPS.

`--objective dps|ehp|min_max_hit` picks among the better variants, and `--min-gain` and
`--tolerance` set the thresholds. At most 8 variants are evaluated (`--max-candidates`): list
them in priority order, give each a specific hypothesis, and raise the budget only when the
results point that way. Variants that fail the preflight are skipped without being calculated.

Variant keys:

| Key | Effect |
|---|---|
| `set` | top-level spec keys |
| `config` | config options; `null` resets one to the default |
| `tree_add` / `tree_remove` | add or remove tree targets |
| `ascendancy_nodes` | ascendancy nodes |
| `masteries_add` / `masteries_remove` | masteries |
| `gems` | `[{group: "main" \| n \| "gem name", replace, add, remove, set}]` |
| `skills_add` / `skills_remove` | add or remove whole skill groups |
| `items` | `[{slot, ...entry} \| {slot, remove: true}]` |

```json
[{"name": "rare boots instead of unique", "items": [{"slot": "Boots", "text": "Rarity: RARE\n..."}]},
 {"name": "Elemental Damage with Attacks", "gems": [{"group": "main", "replace": {"Brutality": "Elemental Damage with Attacks"}}]}]
```

- Useful axes: damage vs. defence, an expensive unique vs. a realistic rare, alternative supports,
  another defensive layer, a different tree route.
- Keep the original unless a variant is valid and better overall for the target content. Report the
  variants and why each was kept or rejected.
- If a variation can't be tested, say so instead of implying it was. Examples: cluster-jewel notables,
  timeless jewels, mechanics PoB doesn't model.

## 7. League starters only

Add a progression section. Skip it for fully geared endgame requests.

- **Leveling skill and transition.** Say when the build switches to the main skill and what it
  needs at that point (gem level, links, weapon type).
- **Tree progression.** Give 2–3 stages, for example level ~30, ~60 and final. Each is a subset of
  the final `tree.nodes`, in allocation order. If an early stage matters, check it with
  `compare` using `set: {"level": 60}` and a reduced node list.
- **Ascendancy order.** List the labs in order and say why.
- **Mandatory items.** Use the audit's build-enabling uniques. Say whether the build works before
  getting each one, and test without it in `compare` when practical.
- **Gear transition.** Describe what to wear in the campaign, in early maps (resists and life first),
  and later.

## What is automated and what isn't

| Check | How |
|---|---|
| Config/flask/skill-group/unique contributions, evidence per conditional option and the configured/evidence-supported/floor levels, random-element part range, absolute sustained floor, enemyIsBoss vs content, max hits and the weakest one, resists, distinct layers by category, net recovery and flask dependence, mana/life sustain, freeze/ailment avoid, top-tier affix count and crafting steps per rare, unique dependency class, severity and priority, costliest tree targets | Automated (audit) |
| Equipment candidates: discovery from PoB's parsed item data, capped drop-in measurement, condition scenarios, enabling passives, slots with no measured benefit | Automated (`candidates`, audit). You choose what goes to `compare` and judge the result |
| Support alternatives, tree value per point, variations | Measured by `compare` / `tree-audit`. You choose the candidates and judge the result |
| Whether the evidence for a config condition is real and its uptime realistic (the audit finds candidates, it doesn't judge them), a blended figure for random-element skills, clear speed, playstyle, socket-colour cost, item rarity and affordability, leveling plan | Model judgment |
| Item prices, trade availability, pathing in cluster jewels, timeless jewels, mechanics PoB doesn't model | Not automatable here. Say so |
