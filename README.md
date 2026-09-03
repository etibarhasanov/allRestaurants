# allRestaurants

Collect every restaurant in an area from Google Maps, then push them into
Salesforce.

This is the "moving pin" approach, automated. Google's Nearby Search returns at
most **20 places per call**, no matter how many actually sit inside the circle
you asked about. So you cover an area by tiling it with overlapping search
circles, and where a circle is clearly still hiding good places you split it
into four smaller circles and look again.

By default it collects **places with at least 25 reviews**, which is what keeps
it cheap. Every circle comes back ranked by popularity, so it hands over the 20
most established restaurants in it — and the weakest of those 20 is the signal
to stop: if even that one clears the review bar, better places are hiding
behind it and the circle is worth splitting; the moment a circle returns a
place with 6 reviews, its tail is in view and there is nothing left worth
finding.

That one rule is the difference between **2,176 API calls and about 50**. Set
`--min-reviews 0` if you really do want every hole-in-the-wall.

For each restaurant you get its name, Google Maps profile link, address,
coordinates, **average rating and review count**, price level, phone, website,
opening hours and service attributes.

---

## What you need

**1. A Google Maps Platform API key**

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create
   or select a project.
2. Enable **billing** on the project. There is a recurring monthly credit, but
   the Places API is not usable without a billing account attached.
3. **APIs & Services → Library →** enable **Places API (New)**.
4. **APIs & Services → Credentials → Create credentials → API key**.
5. Restrict the key: under *API restrictions* pick **Places API (New)** only.
   Leave *Application restrictions* as **None** for a server-side script (or
   restrict by IP if you run it from a fixed host).

**2. Salesforce credentials** — either a username + password + security token,
or a Connected App using the OAuth client credentials flow. Both are described
in `.env.example`.

## Setup

```bash
git clone <this repo> && cd allRestaurants
python3 -m venv .venv && source .venv/bin/activate
pip install -e .[salesforce]

cp .env.example .env      # then fill in your keys
```

## Use it

### 1. Check the cost first

```bash
allrestaurants estimate --center "40.4093,49.8671" --radius-km 5 --budget 100
```

### 2. Sweep the area

Name a call budget and the starting circles get sized to fit it:

```bash
allrestaurants scan --center "40.4093,49.8671" --radius-km 5 --budget 100
```

Or set the geometry yourself:

```bash
allrestaurants scan --center "40.4093,49.8671" --radius-km 5 --cell-radius-m 400
allrestaurants scan --bbox "40.32,49.79,40.45,49.95"
```

**Spend a little, look, then spend more.** The sweep is resumable: every circle
it finishes is logged, so re-running the *identical* command picks up where it
left off rather than paying Google twice. Ctrl-C is safe, and so is hitting the
budget. Measured on the test fixture, running the same `--budget 50` command
repeatedly:

| run | calls | cumulative | recall of places with 25+ reviews |
|---|---|---|---|
| #1 | 50 | 50 | 77% |
| #2 | 50 | 100 | 100% |
| #3 | 16 | 116 | 100% |
| #4 | 0 | 116 | complete — nothing left to search |

So a first look costs about 50 calls, and you decide whether to keep going.

### 3. Look at what you got

```bash
allrestaurants stats
allrestaurants export --format csv --out exports/restaurants.csv
allrestaurants export --format json --out exports/top.json --where "rating >= 4.5"
```

### 4. Send it to Salesforce

Deploy the `Restaurant__c` object first (once):

```bash
sf project deploy start --metadata-dir salesforce/metadata --target-org my-org
# or, older CLI:  sfdx force:mdapi:deploy -d salesforce/metadata -w 10
```

Then:

```bash
allrestaurants sync --dry-run --limit 5   # inspect the payload, send nothing
allrestaurants sync --limit 50            # a real but small first batch
allrestaurants sync                       # everything
allrestaurants sync --only-new            # just what has not been sent before
```

Records are **upserted** on `Google_Place_Id__c`, an External Id field, so
re-running updates existing rows rather than creating duplicates.

Prefer standard Accounts? Add a `Google_Place_Id__c` External Id text field to
Account, then `allrestaurants sync --object Account`.

---

## Controlling what it costs

This is the part worth understanding, because a careless sweep of a dense city
centre can make thousands of billable calls.

| Lever | Effect |
|---|---|
| `--min-reviews N` | **Biggest lever by far.** Raising the bar both discards junk and stops circles splitting sooner. Default 25. |
| `--budget N` | Sizes the starting grid to fit N calls, keeps ~30% back for splitting, and caps the run there. Resume to go further. |
| `--cell-radius-m` | Starting circle size, if you'd rather set it directly. |
| `--split-only-if-new` | Also skip splitting a circle when everything it returned was already known. |
| `--max-depth`, `--min-radius-m` | How far splitting may recurse. |
| `--tier` | Which fields to request. `standard` drops ratings, phone and hours for a cheaper billing SKU; `ratings` (default) includes them; `full` adds the editorial blurb and service attributes. |

Measured on the test fixture — 400 restaurants in a 1 km circle, 115 of them
with 25+ reviews, a long tail of tiny places below that:

| mode | calls | recall of 25+ review places | rows stored |
|---|---|---|---|
| `--min-reviews 0` (census) | 2,176 | 100% | 357 |
| `--min-reviews 25`, 1 km circles | 358 | 98% | 135 |
| `--min-reviews 25 --split-only-if-new` | 42 | 90% | 125 |
| `--min-reviews 50`, 1.5 km circles | 174 | 100% (of 64) | 78 |
| `--min-reviews 25 --budget 100` | 100 | 92% | — |

Two things that surprised me while measuring, both worth knowing:

- **A finer starting grid is cheaper than splitting a coarse one.** 350 m
  circles cost 122 calls for full recall; 700 m circles cost 309. Splitting
  re-covers ground the parent call already paid for, so the parent is wasted
  work. If a run feels expensive, shrink `--cell-radius-m` before anything else.
- **The numbers are non-monotonic.** 300 m cost more than 250 m *and* more than
  350 m, purely from how the grid happened to land on the clusters. Treat any
  single figure here as indicative, not a law.

**The `--split-only-if-new` caveat**: it is a heuristic. A circle can return 20
already-known places while still hiding an unknown one behind them — that is
exactly the 90% row above. Off by default for that reason.

### Why ranking follows the mode

Ranking is chosen to match what you asked for, and the two modes want opposite
things:

- **With a review bar** (default), results are ranked by **popularity**. Each
  circle hands back its most established places, so the weakest of the 20 is a
  usable "have we reached the tail yet" signal.
- **In census mode** (`--min-reviews 0`), results are ranked by **distance**.
  Popularity would return the same famous restaurants however far you split,
  and you could never drill past them; ranked by distance, a smaller circle
  reliably reveals what a bigger one hid.

`--rank` overrides this, but the defaults are the right pairing.

---

## Terms of service, and what you may keep

Google's Places API terms are stricter than people expect, and this matters if
the data is going into a CRM:

- **Place IDs** may be cached indefinitely.
- **Everything else** — names, ratings, addresses, phone numbers — may generally
  not be cached beyond **30 days**, and content must not be used to build a
  competing service or a standalone database sold to others.

`allrestaurants prune` exists for this: it blanks every cached Google field
older than N days while keeping the place IDs (and their Salesforce links), so
you can refresh rather than hoard.

```bash
allrestaurants prune --older-than-days 30
```

Whether your Salesforce copy is compliant depends on how you use it. Read
[Google Maps Platform Terms](https://cloud.google.com/maps-platform/terms) —
sections 3.2.3 and 3.2.4 — and if the data is going to drive commercial sales
outreach at scale, get that reviewed. The tooling here does not make that call
for you.

---

## How it works

```
       area of interest
              │
              ▼
  ┌───────────────────────┐     Tile it with overlapping circles.
  │  ◯   ◯   ◯   ◯   ◯    │     Spacing is r·√2, less a safety margin,
  │  ◯   ◯   ◯   ◯   ◯    │     which is the exact bound for gap-free
  │  ◯   ◯   ◯   ◯   ◯    │     coverage of the plane.
  └───────────────────────┘
              │
              ▼
    search each circle          Nearby Search, ranked by popularity,
              │                 max 20 results.
              ▼
      got a full 20?   ──no──►  done, this circle is exhausted
              │
             yes
              ▼
      did any of them   ──yes─► done: the tail is in view, everything
      fall below the            else in this circle ranks below places
      review bar?               we have already rejected
              │
              no                the good ones are still hiding
              ▼
      split into 4 circles      each at (±r/2, ±r/2) with radius r/√2,
      and search those          which is exactly enough to cover the parent
              │
              └──► repeat until every circle stops,
                   or --budget / --max-depth / --min-radius-m ends it
```

| Module | Role |
|---|---|
| `geo.py` | Tiling an area, splitting a circle, distances |
| `places.py` | Places API client: field tiers, retries, rate limit, request budget |
| `scrape.py` | The sweep: breadth-first, splits saturated circles |
| `models.py` | Flattens an API result into a stored row |
| `store.py` | SQLite storage, resume log, CSV/JSON export, pruning |
| `salesforce_sync.py` | Bulk API upsert on the External Id |

Storage is a single SQLite file (`data/restaurants.db` by default). Updates use
`COALESCE`, so a later cheap-tier sweep never blanks a field an earlier richer
sweep collected.

## Tests

```bash
pip install -e .[dev]
pytest
```

57 tests, no network calls. The interesting ones:

- `test_geo.py` samples points across generated grids and asserts every one
  falls inside some circle — the gap-free coverage claim, checked with real
  haversine distances rather than the flat-earth approximation used to build
  the grid.
- `test_scrape.py` runs the sweep against a fake API holding a known set of
  restaurants, and asserts it finds **all 60** where a single call would have
  returned 20. It also covers resume, budget exhaustion, per-circle failures,
  the review-bar stopping rule, and that concurrent workers agree with serial
  ones.
- `test_resume_rebuilds_the_pending_split_frontier` is a regression test for a
  real bug: a run stopped by its budget mid-split used to resume, find every
  circle already logged, and report a truncated sweep as complete. Resume now
  re-queues the children of circles that were split.

## Limits worth knowing

- Nearby Search caps at a 50 km radius per call; larger circles are clamped.
- Google's own index is the ceiling. A restaurant not on Google Maps, or one
  Google does not classify as a `restaurant` type, will not be found — widen
  `--types` (e.g. `restaurant,cafe,bar,bakery,meal_takeaway`) if that matters.
- Reviews themselves are not collected, only the average and the count. Review
  text carries extra display and attribution obligations.
- A review bar is a proxy for "worth selling to", not the same thing. A good
  restaurant that opened last month has few reviews. If that matters, sweep
  once at a low bar to establish a baseline, then re-sweep periodically at a
  higher one.
