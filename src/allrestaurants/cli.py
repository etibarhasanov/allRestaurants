"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import math
import sys
from typing import List, Optional, Sequence

from . import __version__
from .config import DEFAULT_DB_PATH, load_env
from .geo import (
    METRES_PER_DEGREE_LAT,
    Circle,
    cell_radius_for_budget,
    cover_bbox,
    cover_radius,
    metres_per_degree_lng,
    parse_bbox,
    parse_latlng,
)
from .models import normalize_place
from .places import TIER_ORDER, PlacesClient, PlacesError, resolve_types
from .scrape import Sweeper
from .store import Store, export_csv, export_json

# Rough published list price per Nearby Search call, by field tier.  Google
# changes these; treat them as an order-of-magnitude guide and check
# https://developers.google.com/maps/billing-and-pricing/pricing before a
# large run.  Override with --price-per-call.
APPROX_PRICE_PER_CALL = {
    "ids": 0.0,
    "standard": 0.032,
    "ratings": 0.035,
    "full": 0.040,
}


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _tile(args, cell_radius_m: float) -> List[Circle]:
    if args.bbox:
        south, west, north, east = parse_bbox(args.bbox)
        return cover_bbox(south, west, north, east, cell_radius_m)
    lat, lng = parse_latlng(args.center)
    return cover_radius(lat, lng, args.radius_km * 1000.0, cell_radius_m)


def _area_m2(args) -> float:
    if args.bbox:
        south, west, north, east = parse_bbox(args.bbox)
        mid = (south + north) / 2
        return ((north - south) * METRES_PER_DEGREE_LAT) * (
            (east - west) * metres_per_degree_lng(mid)
        )
    return math.pi * (args.radius_km * 1000.0) ** 2


def _build_circles(args) -> List[Circle]:
    """Turn --center/--radius-km or --bbox into the level-0 search circles.

    With --budget, the starting radius is solved for rather than computed once:
    the closed form ignores how the grid actually lands on the area (and the
    ring of edge circles a circular area keeps), which overshot badly enough
    that a 50-call budget produced 52 starting circles -- a sweep that could
    not finish even its first level, let alone split anything.
    """
    if not getattr(args, "budget", None):
        return _tile(args, args.cell_radius_m)

    target = max(1, int(args.budget * 0.7))
    radius = cell_radius_for_budget(_area_m2(args), args.budget)
    circles = _tile(args, radius)
    for _ in range(12):
        if len(circles) <= target:
            break
        # Too many circles: grow each one to cover proportionally more ground.
        radius *= math.sqrt(len(circles) / target) * 1.05
        circles = _tile(args, radius)

    logging.info(
        "--budget %d: %d starting circle(s) of radius %.0fm, "
        "leaving %d call(s) for splitting",
        args.budget,
        len(circles),
        radius,
        max(0, args.budget - len(circles)),
    )
    return circles


def _add_area_arguments(parser: argparse.ArgumentParser) -> None:
    area = parser.add_mutually_exclusive_group(required=True)
    area.add_argument(
        "--center",
        help="Centre of a circular area, as 'lat,lng' (e.g. '40.4093,49.8671').",
    )
    area.add_argument(
        "--bbox",
        help="Bounding box as 'south,west,north,east' in decimal degrees.",
    )
    parser.add_argument(
        "--radius-km",
        type=float,
        default=5.0,
        help="Radius of the area of interest, in km. Used with --center. Default: 5.",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        help=(
            "Aim to finish within roughly this many API calls. Sizes the"
            " starting circles to cover the area, keeps ~30%% back for"
            " splitting dense spots, and caps the run there. Overrides"
            " --cell-radius-m."
        ),
    )
    parser.add_argument(
        "--cell-radius-m",
        type=float,
        default=1000.0,
        help=(
            "Starting radius of each search circle, in metres. Smaller means"
            " more API calls but fewer saturated circles to split. Default:"
            " 1000, which suits the default review bar; drop it for a census."
        ),
    )


# -- scan -------------------------------------------------------------------


def cmd_scan(args) -> int:
    env = load_env(args.env_file)
    api_key = args.api_key or env.get("GOOGLE_MAPS_API_KEY")

    if args.min_reviews and args.tier in ("ids", "standard"):
        print(
            f"error: --min-reviews needs review counts, which the {args.tier!r} "
            "field tier does not request. Use --tier ratings (the default), or "
            "--min-reviews 0 for a full census.",
            file=sys.stderr,
        )
        return 1

    # Popularity ranking is what makes the review bar a usable stopping signal;
    # distance ranking is what makes a census possible. Pick to match the mode
    # unless the user overrode it.
    rank = args.rank or ("POPULARITY" if args.min_reviews else "DISTANCE")

    circles = _build_circles(args)
    logging.info(
        "area tiled into %d starting circle(s) of radius %.0fm",
        len(circles),
        circles[0].radius_m if circles else args.cell_radius_m,
    )
    if args.min_reviews:
        logging.info(
            "keeping places with >= %d reviews, ranked by %s",
            args.min_reviews,
            rank,
        )
    else:
        logging.info("census mode: keeping every place, ranked by %s", rank)

    client = PlacesClient(
        api_key=api_key,
        tier=args.tier,
        qps=args.qps,
        max_requests=args.max_requests or args.budget,
    )
    store = Store(args.db)
    if args.no_resume:
        store.reset_cells()

    sweeper = Sweeper(
        client=client,
        store=store,
        included_types=resolve_types(args.types),
        min_radius_m=args.min_radius_m,
        max_depth=args.max_depth,
        workers=args.workers,
        language_code=args.language,
        region_code=args.region,
        rank_preference=rank,
        min_reviews=args.min_reviews,
        resume=not args.no_resume,
        split_only_if_new=args.split_only_if_new,
    )

    try:
        stats = sweeper.run(circles)
    finally:
        total = store.count()
        store.close()

    print()
    print(f"  circles searched : {stats.cells_searched}")
    print(f"  circles skipped  : {stats.cells_skipped} (already done)")
    print(f"  circles split    : {stats.cells_split} (came back full)")
    if args.min_reviews:
        print(f"  circles finished : {stats.cells_below_bar} (reached the review bar)")
    if args.split_only_if_new:
        print(f"  circles pruned   : {stats.cells_pruned} (full, but nothing new)")
    print(f"  circles failed   : {stats.cells_failed}")
    print(f"  deepest split    : level {stats.max_depth}")
    print(f"  API calls made   : {client.request_count}")
    print(f"  results returned : {stats.results_seen}")
    print(f"  new restaurants  : {stats.new_places}")
    if args.min_reviews:
        print(
            f"  ignored          : {stats.skipped_below_bar} result(s) under "
            f"{args.min_reviews} reviews"
        )
    print(f"  total in db      : {total}")
    print(f"  elapsed          : {stats.elapsed_s:.0f}s")
    if stats.stopped_early:
        print(f"\n  stopped early: {stats.stopped_early}")
        print("  re-run the same command to resume where it left off.")
        return 2
    return 0


# -- estimate ---------------------------------------------------------------


def cmd_estimate(args) -> int:
    circles = _build_circles(args)
    price = (
        args.price_per_call
        if args.price_per_call is not None
        else APPROX_PRICE_PER_CALL.get(args.tier, 0.035)
    )
    base = len(circles)
    if args.budget:
        print(f"  budget               : {args.budget} calls")
    # Every saturated circle becomes four, so the real total depends entirely
    # on density, and the spread is wide: a quiet suburb barely splits at all,
    # while a dense centre can run an order of magnitude over the starting
    # grid. These multipliers come from measured sweeps; treat the top of the
    # range as the number to budget for, not the bottom.
    if args.min_reviews:
        # The review bar caps how deep splitting goes: a circle stops the first
        # time it returns anything below the bar, which in practice is after
        # one or two splits even downtown.
        low, high = base, int(base * 2.5)
    else:
        low, high = base, base * 12
    if args.budget:
        # --budget also caps the run, so the estimate must not exceed it.
        high = min(high, args.budget)
        low = min(low, high)

    mode = (
        f"places with >= {args.min_reviews} reviews"
        if args.min_reviews
        else "census: every place, however few reviews"
    )
    print(f"  mode                 : {mode}")
    print(f"  starting circles     : {base}")
    if low == high:
        print(f"  API calls (estimate) : {high} (capped by --budget)")
    else:
        print(f"  API calls (estimate) : {low} (sparse area) - {high} (dense centre)")
    print(f"  price per call       : ${price:.4f} ({args.tier} tier)")
    if low == high:
        print(f"  cost (estimate)      : ${high * price:.2f}")
    else:
        print(f"  cost (estimate)      : ${low * price:.2f} - ${high * price:.2f}")
    print()
    print("  The spread is real: splitting is driven by how many restaurants")
    print("  are packed together, which cannot be known before you look.")
    print()
    print("  To keep the bill down:")
    print("    --min-reviews 50        raise the bar; biggest lever by far")
    print("    --cell-radius-m 1500    fewer, larger starting circles")
    print("    --max-requests N        hard cap; the sweep stops cleanly and resumes")
    print("    --split-only-if-new     skip splitting circles that found nothing new")
    print()
    print("  Prices are indicative only - check Google's current pricing page,")
    print("  and note the recurring monthly credit on Google Maps Platform.")
    return 0


# -- check ------------------------------------------------------------------


def cmd_check(args) -> int:
    """Spend one API call to prove the key works before committing a budget."""
    env = load_env(args.env_file)
    api_key = args.api_key or env.get("GOOGLE_MAPS_API_KEY")
    if not api_key:
        print(
            "error: no API key. Put GOOGLE_MAPS_API_KEY in .env or pass --api-key.",
            file=sys.stderr,
        )
        return 1

    lat, lng = parse_latlng(args.center)
    client = PlacesClient(api_key=api_key, tier=args.tier, qps=0, max_requests=1)
    print(f"  probing {args.tier!r} fields at {lat},{lng} ...")
    try:
        places = client.search_nearby(
            Circle(lat, lng, 1000.0),
            included_types=resolve_types(args.types),
            rank_preference="POPULARITY",
        )
    except PlacesError as exc:
        message = str(exc)
        print(f"\n  FAILED: {message}\n", file=sys.stderr)
        if "403" in message or "PERMISSION_DENIED" in message:
            print("  Likely causes:", file=sys.stderr)
            print("   - 'Places API (New)' is not enabled on the project", file=sys.stderr)
            print("   - the key's API restrictions exclude Places API (New)", file=sys.stderr)
            print("   - an application restriction (HTTP referrer / IP) blocks"
                  " server-side use", file=sys.stderr)
        elif "billing" in message.lower():
            print("  Billing is not enabled on the Google Cloud project.", file=sys.stderr)
        elif "400" in message:
            print("  The key was rejected as malformed - check for stray"
                  " whitespace when pasting.", file=sys.stderr)
        return 1

    rated = [p for p in places if p.get("userRatingCount") is not None]
    print(f"\n  OK - the key works. {len(places)} place(s) returned, 1 call used.\n")
    for raw in places[:5]:
        row = normalize_place(raw)
        reviews = row["user_rating_count"]
        rating = row["rating"]
        print(f"    {row['name']}"
              f"  {rating if rating is not None else '?'}*"
              f"  ({reviews if reviews is not None else '?'} reviews)")
    if len(places) > 5:
        print(f"    ... and {len(places) - 5} more")

    if not rated:
        print("\n  WARNING: no review counts came back, so --min-reviews cannot")
        print("  work. The key may be limited to a cheaper SKU.")
        return 1
    print("\n  Review counts are present, so --min-reviews will work.")
    print("  Ready to scan.")
    return 0


# -- export / stats / prune -------------------------------------------------


def cmd_export(args) -> int:
    store = Store(args.db)
    try:
        if args.format == "csv":
            count = export_csv(store, args.out, args.where or "")
        else:
            count = export_json(store, args.out, args.where or "")
    finally:
        store.close()
    print(f"wrote {count} restaurant(s) to {args.out}")
    return 0


def cmd_stats(args) -> int:
    store = Store(args.db)
    try:
        total = store.count()
        cells = store.cell_stats()
        rated = list(
            store.iter_places("rating IS NOT NULL")
        )
        synced = list(store.iter_places("salesforce_synced_at IS NOT NULL"))
        avg = sum(r["rating"] for r in rated) / len(rated) if rated else 0.0
        cities = {}
        for row in store.iter_places():
            cities[row["city"] or "(unknown)"] = cities.get(row["city"] or "(unknown)", 0) + 1
    finally:
        store.close()

    print(f"  restaurants stored : {total}")
    print(f"  with a rating      : {len(rated)}")
    print(f"  mean rating        : {avg:.2f}")
    print(f"  synced to SF       : {len(synced)}")
    print(f"  search circles     : {cells['cells']} "
          f"({cells['saturated']} saturated, deepest level {cells['max_depth']})")
    if cities:
        print("\n  top cities:")
        for city, count in sorted(cities.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {count:6d}  {city}")
    return 0


def cmd_prune(args) -> int:
    store = Store(args.db)
    try:
        affected = store.prune_stale_content(args.older_than_days)
    finally:
        store.close()
    print(
        f"cleared cached Google content on {affected} record(s) older than "
        f"{args.older_than_days} days (place IDs kept)"
    )
    return 0


# -- salesforce -------------------------------------------------------------


def cmd_sync(args) -> int:
    from .salesforce_sync import FIELD_MAPS, SalesforceConfigError, connect, sync

    env = load_env(args.env_file)
    object_name = args.object or env.get("SF_OBJECT") or "Restaurant__c"
    external_id = (
        args.external_id or env.get("SF_EXTERNAL_ID_FIELD") or "Google_Place_Id__c"
    )
    field_map = FIELD_MAPS.get(object_name)
    if field_map is None:
        print(
            f"No field map for object {object_name!r}. Add one to "
            "src/allrestaurants/salesforce_sync.py (FIELD_MAPS).",
            file=sys.stderr,
        )
        return 1

    store = Store(args.db)
    try:
        sf = None if args.dry_run else connect(env)
        summary = sync(
            store,
            sf,
            object_name=object_name,
            external_id_field=external_id,
            field_map=field_map,
            batch_size=args.batch_size,
            only_unsynced=args.only_new,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    except SalesforceConfigError as exc:
        print(f"Salesforce error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    print(f"  records considered : {summary['total']}")
    print(f"  upserted ok        : {summary['success']} ({summary['created']} created)")
    print(f"  failed             : {summary['failed']}")
    return 1 if summary["failed"] else 0


# -- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="allrestaurants",
        description=(
            "Sweep Google Places for every restaurant in an area, store them, "
            "and upsert them into Salesforce."
        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    parser.add_argument(
        "--db", default=DEFAULT_DB_PATH, help=f"SQLite path. Default: {DEFAULT_DB_PATH}"
    )
    parser.add_argument("--env-file", default=None, help="Path to a .env file.")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Sweep an area and collect restaurants.")
    _add_area_arguments(scan)
    scan.add_argument("--api-key", help="Overrides GOOGLE_MAPS_API_KEY.")
    scan.add_argument(
        "--tier",
        choices=TIER_ORDER,
        default="ratings",
        help="Which field set to request. More fields cost more. Default: ratings.",
    )
    scan.add_argument(
        "--types",
        default="restaurant",
        help=(
            "Comma-separated Google place types, e.g."
            " 'restaurant,cafe,bar,bakery'. Default: restaurant."
        ),
    )
    scan.add_argument(
        "--min-reviews",
        type=int,
        default=25,
        help=(
            "Only keep places with at least this many reviews, and stop"
            " splitting a circle as soon as it returns anything below the bar."
            " This is the single biggest cost lever. Set 0 for a full census"
            " of every place regardless of how few reviews it has (far more"
            " expensive). Default: 25."
        ),
    )
    scan.add_argument("--min-radius-m", type=float, default=40.0,
                      help="Stop splitting below this radius. Default: 40.")
    scan.add_argument("--max-depth", type=int, default=6,
                      help="Maximum split levels. Default: 6.")
    scan.add_argument("--workers", type=int, default=5,
                      help="Concurrent API calls. Default: 5.")
    scan.add_argument("--qps", type=float, default=10.0,
                      help="Client-side request rate cap. Default: 10.")
    scan.add_argument("--max-requests", type=int, default=None,
                      help="Hard cap on API calls; the sweep stops cleanly at it.")
    scan.add_argument("--language", default=None, help="languageCode, e.g. 'en'.")
    scan.add_argument("--region", default=None, help="regionCode, e.g. 'AZ'.")
    scan.add_argument(
        "--rank",
        choices=["DISTANCE", "POPULARITY"],
        default=None,
        help=(
            "How the API picks which 20 places to return. Defaults to match the"
            " mode: POPULARITY with --min-reviews (each circle returns its most"
            " established places, so the weakest one tells you when to stop),"
            " DISTANCE for a census (a smaller circle then reveals what a"
            " bigger one hid). Rarely worth overriding."
        ),
    )
    scan.add_argument(
        "--split-only-if-new",
        action="store_true",
        help=(
            "Cost saver: do not split a full circle when every place it"
            " returned was already known. Cuts API calls sharply in dense city"
            " centres, at a small risk of missing places hidden behind 20"
            " already-known ones."
        ),
    )
    scan.add_argument("--no-resume", action="store_true",
                      help="Forget previously searched circles and start over.")
    scan.set_defaults(func=cmd_scan)

    estimate = sub.add_parser(
        "estimate", help="Estimate API calls and cost before running a scan."
    )
    _add_area_arguments(estimate)
    estimate.add_argument("--tier", choices=TIER_ORDER, default="ratings")
    estimate.add_argument("--min-reviews", type=int, default=25)
    estimate.set_defaults(max_requests=None)
    estimate.add_argument("--price-per-call", type=float, default=None)
    estimate.set_defaults(func=cmd_estimate)

    check = sub.add_parser(
        "check",
        help="Spend one API call to verify the key and show sample results.",
    )
    check.add_argument("--center", required=True, help="Somewhere to probe, 'lat,lng'.")
    check.add_argument("--api-key", help="Overrides GOOGLE_MAPS_API_KEY.")
    check.add_argument("--tier", choices=TIER_ORDER, default="ratings")
    check.add_argument("--types", default="restaurant")
    check.set_defaults(func=cmd_check)

    export = sub.add_parser("export", help="Export collected restaurants.")
    export.add_argument("--format", choices=["csv", "json"], default="csv")
    export.add_argument("--out", required=True, help="Output file path.")
    export.add_argument("--where", default=None,
                        help="Optional SQL filter, e.g. \"rating >= 4.5\".")
    export.set_defaults(func=cmd_export)

    stats = sub.add_parser("stats", help="Summarise what is in the database.")
    stats.set_defaults(func=cmd_stats)

    prune = sub.add_parser(
        "prune",
        help="Clear cached Google content older than N days, keeping place IDs.",
    )
    prune.add_argument("--older-than-days", type=int, default=30)
    prune.set_defaults(func=cmd_prune)

    sync_cmd = sub.add_parser("sync", help="Upsert restaurants into Salesforce.")
    sync_cmd.add_argument("--object", default=None,
                          help="Target sObject. Default: Restaurant__c.")
    sync_cmd.add_argument("--external-id", default=None,
                          help="External Id field used for the upsert.")
    sync_cmd.add_argument("--batch-size", type=int, default=5000)
    sync_cmd.add_argument("--only-new", action="store_true",
                          help="Only send records never synced before.")
    sync_cmd.add_argument("--limit", type=int, default=None,
                          help="Send at most N records (handy for a first test).")
    sync_cmd.add_argument("--dry-run", action="store_true",
                          help="Build the payload and print a sample; send nothing.")
    sync_cmd.set_defaults(func=cmd_sync)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return args.func(args)
    except (PlacesError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
