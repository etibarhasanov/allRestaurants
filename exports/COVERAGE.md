# Tallinn dataset — what is in it, and what is not

`tallinn_restaurants.csv` holds **750 restaurants**, collected 2026-09-03 in
**318 Google Places API calls** (~$11.13), keeping places with **25 or more
reviews** and excluding shops, hotels and malls.

## How it was collected

| pass | area | circle size | calls | new |
|---|---|---|---|---|
| 1 | 5 km around centre | 1383 m | 51 | 358 |
| 2 | Old Town, 0.5 km | 200 m | 90 | 246 |
| 3 | Kesklinn, 1.5 km | 300 m | 80 | 149 |
| 4 | Kalamaja, 1.0 km | 300 m | 50 | 27 |
| 5 | Telliskivi, 0.5 km | 250 m | 35 | 12 |
| — | density probes | — | 12 | — |

The first pass used 1383 m circles across the whole city and found only 43
places within a kilometre of the centre. Circle size, not area, was the
constraint: a circle returns at most 20 places, so a coarse one in a dense
district returns its 20 best and hides the rest. Re-running the centre at
200-300 m is what took the dataset from 358 to 750.

## Cross-check against known Tallinn restaurants

`tools/crosscheck.py` against `tools/tallinn_reference.csv` — 41 names from
Michelin Guide Estonia 2026, Tallinn Tastebuds picks recoverable via search,
and long-standing local landmarks.

**34 of 41 present (83%)**, up from 16 of 41 (39%) after the first pass.

Includes 180 Degrees, FUME, Fotografiska, Vesta, Gianni, Koyo, Härg,
Tchaikovsky, Mantel & Korsten, 5Senses, MEKK, Pull, Salt, Rado, Farm, Frenchy,
Cru, Kaerajaan, Von Krahl, Rataskaevu 16, Olde Hansa, Põhjala.

**Not found (7):**

- **NOA Chef's Hall** (1 Michelin star) — in Pirita, roughly 7 km north-east,
  outside every district scanned. A separate pass would pick it up.
- **Leib Resto ja Aed** — Old Town, which stopped at its 90-call ceiling with
  splitting still queued. Most likely recoverable by re-running that pass.
- **Juur, Ribe, Sfäär, Umami, Kaks Kokka** — absent from the raw database under
  any spelling. Several of these are long-standing names that may have closed;
  the reference list was assembled partly from general knowledge and is not
  guaranteed current.

One match was rejected by hand: "Kaks Kokka" fuzzy-matched "Kaks Kokapoissi",
a different restaurant. The 83% is after removing it.

## Known limits

- All five passes stopped at a spending ceiling rather than at exhaustion, so
  the data is a floor, not a complete census. Re-running any command continues
  from its resume log rather than starting over.
- Places with fewer than 25 reviews are excluded by design.
- tallinntastebuds.ee could not be fetched (blocked by the collecting
  environment's network policy), so only the picks that surfaced through search
  are represented in the reference list.
- Name matching is conservative: a name reported missing may still be present
  under a spelling too different to match. Each was additionally checked by
  substring against the raw database.

## Terms of service

This data is Google Places content. Google's terms allow caching place IDs
indefinitely but generally not the rest beyond 30 days, and restrict building
a standalone database from it. `allrestaurants prune --older-than-days 30`
clears stale content while keeping IDs. See the README before republishing.
