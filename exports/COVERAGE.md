# Tallinn dataset — coverage and what is still missing

`tallinn_restaurants.csv` holds **1,110 places** collected 2026-09-03, keeping
anything with **25 or more reviews**.

## Cross-check against Tallinn Tastebuds

The comparison runs against `tallinntastebuds`' own data, which carries phone
numbers, addresses and coordinates. An earlier check used a hand-written list of
41 names with none of those keys; it has been removed. A name-only list cannot
be matched reliably in either direction — "Siga" and "Siga la Vaca" are one
restaurant, "Uus Laine" and "Pilsneri baar" are two — so it produced figures
that needed correcting by hand, which is not a check.

Checked against the 74 curated places in
`etibarhasanov/tallinntastebuds` → `data/restaurants.json`.

Run it yourself — no API calls, no manual steps:

```bash
PYTHONPATH=tools python3 tools/report_coverage.py \
    ../etibarhasanov/tallinntastebuds/data/restaurants.json \
    exports/tallinn_restaurants.csv data/restaurants.db
```

| pass | matched |
|---|---|
| restaurants only, 5 km, coarse circles | 49 / 70 (70%) |
| **after widening the type filter** | **66 / 70 (94%)** |

Four curated places are marked `closed` in the source and are excluded from the
comparison: Google's nearby search never returns a closed place, so counting
them as misses would be counting an impossibility.

### How two records are judged to be the same place

Name similarity cannot do this job. `Lb23` and `Laboratooriumi 23` share no
words and are one bar; `Uus Laine` and `Pilsneri baar` sit 13 metres apart and
are two. Identity is therefore decided on evidence, strongest first:

| evidence | matches | note |
|---|---|---|
| phone number | 55 | an exact key — two businesses do not share a line |
| street + house number | 10 | with positions in agreement |
| name + position | 1 | the weak fallback, needing both |

A phone number that *disagrees* vetoes a weaker match. Kokomo Coffee Roasters
and KIOSK NO3 are two businesses sharing a doorway at Ankru 10, 32 m apart:
address and position say one place, and only the differing numbers separate
them.

An identical name overrides that veto, because a stale number on one side is far
likelier than two identically-named businesses on one doorstep. Two independent
cases, each a curated record matched to its own counterpart in the collected
data — not to each other:

- *Crustum Bakery*, Iva 12 in Mustamäe — same name, same address, 0 m apart,
  two different numbers on record.
- *Lokaal Tilk*, Pärnu mnt 66 — likewise. The two are 5.9 km apart and are
  never compared.

Three matches would have been missed by name comparison alone: `KoHo` →
`Telliskivi KoHo`, `KotKot` → `kot.NOBLESSNER`, `Laboratooriumi 23` → `Lb23`.

## Why places were missing — four separate causes

**1. Google's type taxonomy (the big one, 22 of the original 25).**
Every search sent `includedTypes: ["restaurant"]`. Google returns a place only
if *restaurant* appears in its own type list, and Google does not consider a
café, bakery, pub or bar to be a restaurant. Lb23 was covered by nine search
circles and returned by none of them; it is typed
`bar, coffee_shop, cafe, food_store, store`. Miss rates by category before the
fix: coffee 75%, cafés 71%, bakery 56%, pub 46% — against restaurant 7% and
fine dining 0%.

Widening to `restaurant,cafe,coffee_shop,bakery,bar,pub,wine_bar,meal_takeaway,`
`ice_cream_shop,dessert_shop,sandwich_shop,tea_house,brewery` recovered most of
them. A second widening was needed for places Google types as shops rather than
eateries — `chocolate_shop` (Chocolala, 487 reviews), `confectionery`,
`donut_shop`, `deli`.

**2. Areas never searched (3).** Baklažaan in Mustamäe, Shaurma Kebab in
Lasnamäe, Buxhöwden pagar in Viimsi all sat outside every circle. Fixed with
small targeted passes.

**3. Below the review bar (1).** Nullijook has 19 reviews, under the 25-review
threshold, so it is excluded by design. Balta Chill (9) and Pilsneri baar (5)
are the same.

**4. Permanently closed (2).** Maison François and Lendav Maaler are both
`CLOSED_PERMANENTLY` in Google. Google's nearby search does not return them, so
no sweep will ever find them. The curated list is ahead of reality here.

## Still missing (4 of 70)

`report_coverage.py` classifies each miss from the sweep log automatically,
separating the three failures that need different fixes: an area never covered
needs another pass, a circle that came back full needs splitting, and a circle
that had room and still did not return a place was filtering it out.

All four fall in the last category — searched, with room to spare, and excluded:

| place | reviews | why |
|---|---|---|
| Kokomo Coffee Roasters | 233 | typed `coffee_roastery`; no type filter tried returns it |
| Nullijook | 19 | under the 25-review bar, by design |
| Balta Chill | 9 | under the 25-review bar, by design |
| Pilsneri baar | 5 | under the 25-review bar, by design |

## How to search Google Places properly, in short

- `includedTypes` is matched against a place's own type list, not against what a
  human would call it. Ask for `restaurant` alone and you get no cafés,
  bakeries, bars or pubs — a systematic hole, not a sampling gap.
- A nearby search returns at most 20 places per call, so circle size must suit
  density. A 1383 m circle over Old Town found 43 places in the central
  kilometre; 200 m circles found several hundred.
- Widening the types makes each circle saturate sooner, so a wider sweep needs a
  bigger budget for the same area, not the same one.
- Permanently closed places are never returned. Absence is not always a bug.

## Reproducing

```bash
TYPES="restaurant,cafe,coffee_shop,bakery,bar,pub,wine_bar,meal_takeaway,\
ice_cream_shop,dessert_shop,sandwich_shop,tea_house,brewery,chocolate_shop,\
confectionery,donut_shop,deli"
allrestaurants scan --center "59.4372,24.7453" --radius-km 0.5 \
    --cell-radius-m 200 --types "$TYPES"
```

The resume log is keyed by type filter as well as position, so re-running an
area with a wider `--types` searches it again rather than skipping it as done.

## Terms of service

This is Google Places content. Google allows caching place IDs indefinitely but
generally not the rest beyond 30 days. `allrestaurants prune --older-than-days 30`
clears stale content while keeping IDs.
