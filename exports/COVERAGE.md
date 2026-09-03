# Tallinn sweep — what is in this data, and what is not

`tallinn_restaurants.csv` holds **316 restaurants** collected on 2026-09-03 in
**51 Google Places API calls** (~$1.79), centred on 59.4370, 24.7536 with a 5 km
radius, keeping places with **25 or more reviews**.

## It is incomplete, and not evenly so

The sweep hit its 50-call budget partway through its first round of splitting,
with 15 circles still queued. The shortfall is concentrated in exactly the wrong
place — the city centre:

| distance from centre | restaurants found |
|---|---|
| within 1 km | 43 |
| within 2 km | 68 |
| within 3 km | 140 |
| within 5 km | 238 |
| total (edge circles reach ~7.6 km) | 316 |

43 places inside the central kilometre is far short of reality. Density is the
cause: each search circle returns at most 20 places, so sparse outskirts are
covered by a single call while Old Town needs repeated splitting — and the
budget ran out before that happened.

## Cross-check against known Tallinn restaurants

`tools/crosscheck.py` against `tools/tallinn_reference.csv` (41 names: Michelin
Guide Estonia 2026 entries, Tallinn Tastebuds picks that could be recovered from
search, and long-standing local landmarks):

- **present: 16 of 41 (39%)**
- missing: 25

Present includes 180 Degrees, Härg, Vesta, Rataskaevu 16, Olde Hansa, Põhjala,
F-Hoone, Vegan Restoran V, Kompressor, Peppersack, III Draakon, Maiasmokk,
Moon, Tuljak, Lore Bistroo, Barbarea.

Missing includes **NOA Chef's Hall** (1 Michelin star), **Fotografiska**,
**FUME**, **Gianni**, **Koyo**, Tchaikovsky, Leib Resto ja Aed, Von Krahli Aed,
MEKK, Sfäär, Juur, Ribe, Salt, Siga, Farm, Cru, Frenchy, Kaks Kokka,
Kaerajaan, Umami, Mantel & Korsten, 5Senses, Argo, Pull, Rado.

None of these appear in the raw database under any spelling, so they were never
returned by a search — not collected and then filtered out.

## What would fix it

The gap is central, so spend there rather than widening:

```bash
allrestaurants scan --center "59.4370,24.7536" --radius-km 2 --budget 60
```

Being resumable, this adds to the same database rather than starting over. A
wider `--radius-km 8` pass would add outer suburbs, but that is not where the
missing names are.

## Caveats on the cross-check

- tallinntastebuds.ee could not be fetched (blocked by this environment's
  network policy), so only the three of its picks that surfaced through search
  are represented. The reference list is therefore indicative, not their list.
- Name matching is deliberately conservative: a name reported missing may still
  be present under a spelling too different to match. Every "missing" entry
  above was additionally checked by substring against the raw database.
