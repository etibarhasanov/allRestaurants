# allRestaurants

Collect every restaurant in an area from Google Maps, then push them into
Salesforce.

This is the "moving pin" approach, automated. Google's Nearby Search returns at
most **20 places per call**, no matter how many actually sit inside the circle
you asked about. So you cover an area by tiling it with overlapping search
circles, and whenever a circle comes back with a full 20 results — meaning more
are hiding behind them — you split it into four smaller circles and look again.
Repeat until nothing comes back full.

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
allrestaurants estimate --center "40.4093,49.8671" --radius-km 5
```

Do run this. The number of API calls depends on how densely packed the
restaurants are, which you cannot know before you look, so the range is wide.

### 2. Sweep the area

```bash
# A circle around a point
allrestaurants scan --center "40.4093,49.8671" --radius-km 5

# Or a bounding box
allrestaurants scan --bbox "40.32,49.79,40.45,49.95"

# A first run, kept deliberately cheap
allrestaurants scan --center "40.4093,49.8671" --radius-km 2 \
    --max-requests 300 --split-only-if-new
```

The sweep is **resumable**. Every circle it finishes is logged, so if you stop
it — or it hits `--max-requests` — re-running the identical command picks up
where it left off instead of paying Google twice. Ctrl-C is safe.

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
| `--max-requests N` | Hard cap. The sweep stops cleanly and resumes later. **Use this on your first run.** |
| `--split-only-if-new` | Skip splitting a full circle when everything it returned was already known. Measured at ~13x fewer calls on a dense test fixture, with no loss of recall — but see the caveat below. |
| `--cell-radius-m` | Starting circle size. Larger means fewer initial calls but more splitting in dense areas. |
| `--max-depth`, `--min-radius-m` | How far splitting may recurse. Lower means cheaper, and risks missing places in dense clusters. |
| `--tier` | Which fields to request. `standard` drops ratings, phone and hours for a cheaper billing SKU; `ratings` (default) includes them; `full` adds the editorial blurb and service attributes. |

**The `--split-only-if-new` caveat**: it is a heuristic, not a guarantee. A
circle can return 20 already-known places while still hiding an unknown one
behind them. It is off by default for that reason. On the test fixture it lost
nothing; on real data, treat it as a cost/completeness trade you are choosing.

### Why ranking is set to DISTANCE

The sweep asks the API for results ranked by **distance**, not Google's usual
popularity. This is what makes the whole approach work: ranked by distance, a
smaller circle returns its 20 nearest places and so reliably reveals what the
bigger circle hid. Ranked by popularity, the same well-known restaurants come
back however far you split, and you can never drill past them. `--rank` can
change it, but there is rarely a good reason to.

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
    search each circle          Nearby Search, ranked by distance,
              │                 max 20 results.
              ▼
      got 20 results?  ──no──►  done, this circle is fully enumerated
              │
             yes                more are hiding behind those 20
              │
              ▼
      split into 4 circles      each at (±r/2, ±r/2) with radius r/√2,
      and search those          which is exactly enough to cover the parent
              │
              └──► repeat until nothing comes back full,
                   or --max-depth / --min-radius-m stops it
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

55 tests, no network calls. The interesting ones:

- `test_geo.py` samples points across generated grids and asserts every one
  falls inside some circle — the gap-free coverage claim, checked with real
  haversine distances rather than the flat-earth approximation used to build
  the grid.
- `test_scrape.py` runs the sweep against a fake API holding a known set of
  restaurants, and asserts it finds **all 60** where a single call would have
  returned 20. It also covers resume, budget exhaustion, per-circle failures,
  and that concurrent workers agree with serial ones.

Measured end to end on a 400-restaurant fixture (327 inside the search area):
**100% recall**, in 2,136 calls exhaustively or 160 calls with
`--split-only-if-new`.

## Limits worth knowing

- Nearby Search caps at a 50 km radius per call; larger circles are clamped.
- Google's own index is the ceiling. A restaurant not on Google Maps, or one
  Google does not classify as a `restaurant` type, will not be found — widen
  `--types` (e.g. `restaurant,cafe,bar,bakery,meal_takeaway`) if that matters.
- Reviews themselves are not collected, only the average and the count. Review
  text carries extra display and attribution obligations.
