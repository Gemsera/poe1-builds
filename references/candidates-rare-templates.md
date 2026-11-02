# Rare affix templates in `candidates`

`candidates` searches PoB's unique and flask data as drop-in replacements. A rare item can beat
most uniques in a weapon or amulet slot, but a rare is not in PoB's item list: it has to be
assembled. The rare template does that for **Weapon 1** and **Amulet** (`--rare-slots`, default
`Weapon 1,Amulet`; `none` disables it) and ranks the result alongside the uniques.

A template is a **crafting or shopping target**, not an item the build owns. Everything it
reports is measured by PoB; nothing it reports is a price.

## How a template is built

1. **Bases.** The equipped base's item class (One Handed Sword, Staff, Amulet...), restricted to
   bases the character's level allows. Weapons: the equipped base first, then the two highest
   level-requirement bases of the class (3 in all). Amulets: all non-talisman bases, 4 at most.
   The affix measurements are made on the first base only and reused for the others, so the
   per-base difference in the result is the base itself (implicit, base damage, speed, crit).
2. **Kept lines.** The equipped rare's or magic item's resistance, attribute, life and ES lines are
   copied as they are (`kept`) so the template never costs the build a resist cap or a gem
   requirement. A non-weapon slot with no life or ES line gets a life prefix from the pool.
3. **Pool.** Every prefix and suffix PoB's affix tables allow on the base at the item level,
   grouped by mod group with tiers in level order, plus the bench crafts that can go on the slot.
   `+N to Level of ...` lines are filtered against the main skill's gem tags (a cold spell gem
   line is not measured on a lightning attack build).
4. **Measurement.** Each candidate affix is written at its best tier (mid-roll) onto the base with
   the kept lines and measured once by PoB (`counts.rare_evals`). The gain is the DPS % change
   plus the EHP % change against the candidates baseline. The budget is `--rare-evals` (default
   60), split evenly between the slots and separate from the item evaluation limit; when the
   budget is short the groups are measured in static-score order and the rest are skipped.
5. **Assembly.** Greedy by measured gain: an affix is taken when its gain is above 0.3 % (a bench
   craft above 1.0 %), its mod group is not used yet, and the prefix or suffix count is under 3.
   One bench craft at most.
6. **Tier caps.** The investment decides how many *costly* groups keep their best tier:
   league starter 1, budget 2, medium 3, high unlimited (`top_tier_limit`). Costly means not a kept
   line, not a bench craft, not a cheap group (resistances, attributes, rarity) and a group with
   more than one tier. The groups beyond the limit, lowest gain first, drop to their second tier
   (`T2*`). Cheap groups and kept lines are never demoted.
7. **Final measurement.** The assembled item is measured by PoB like any other candidate
   (`evalItem`), goes to the top of the slot's list as `Template <base>` with `kind: rare`, carries
   its `template` detail (lines with tiers, kept count, demoted count, trials) and is ranked,
   classified (promising / trade-off / worse) and written into `compare` variants exactly like a
   unique.

## Reading the report

```
1. Template Highborn Staff (crafted rare template, tiers capped for budget) - promising: DPS +25.7%, EHP +5.8% ...
   lines: +4 to Level of Socketed Chaos Gems (T1*) | +2 to Level of all Spell Skill Gems (T1*) |
          162% increased Spell Damage (T2*) | 127% increased Critical Strike Chance for Spells (T2*) |
          +25% to Chaos Resistance (kept) | Hits can't be Evaded (bench craft)
   T* = a costly group at its best tier (2 allowed at this budget); 'kept' = copied from the equipped item;
   cost not verified - a 5-6 affix rare at these tiers is a trade or a multi-step craft
```

- `T1*` costly group at its best tier; `T2*` demoted by the tier cap; `T1` (no star) a cheap group;
  `kept` copied from the equipped item; `pool, T1` the life line added for a slot without one;
  `bench craft` a crafting bench mod.
- The summary line `Rare templates: Weapon 1: <bases>; Amulet: <bases> (... N evaluations; tiers
  capped for <investment>)` says what was templated and how much PoB time it took.
- A template that loses to the equipped item or to a unique is still listed with its measured
  change: that is the answer ("the rare you'd craft is not better than Replica Dragonfang here").

## What to do with it

- A unique that beats the template is a real upgrade. A unique that loses to it is not: say so.
- To accept a template, run `compare` with the variant `candidates` wrote (or paste the
  `spec_item.text` into the spec), then `build`. The driver's affix legality check runs on it like
  on any rare, and the gear audit's tier count sees it as an ordinary item.
- Say in the write-up that the item is a crafting target and that cost is not verified. Name the
  craft route only if you know it (essence, fossil, harvest reforge, bench finish); a tier is not
  a price.
- Rings, belt, body armour, helmet, gloves and boots are not templated: their key rare mods are
  still named from the build's needs (resists, life, suppression, `+2 Socketed Gems` on the
  helmet...) and checked by the gear audit.

## Limits

- Mid-roll values, so the DPS of a template is a typical outcome, not the best roll.
- Measured per affix then combined: two affixes that interact (crit chance and crit multi) are
  each measured alone, so the combined result can differ from the sum; the final measurement is
  what counts and is what the ranking uses.
- Influenced, synthesised, fractured and veiled mods are out of the pool; eldritch implicits are
  the `sweep`'s job (`$set_exarch`, `$set_eater`).
- Weapon 2 / off-hand, two-handed to one-handed changes and swapping the weapon class are not
  explored: the template stays on the equipped class. A different class is a design decision made
  in the spec, then `candidates` runs again.
- The budget is deliberately small (60 PoB evaluations by default, ~3 s): a long pool is measured
  in static-score order and the tail is not measured.
