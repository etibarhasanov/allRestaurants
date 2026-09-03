"""Compare a curated list against a collected dataset and explain every miss.

Runs entirely offline against the sweep's own database. No API calls.

    python3 tools/report_coverage.py <curated.json> <collected.csv> [sweep.db]
"""

from __future__ import annotations

import collections
import sqlite3
import sys

from crosscheck import (compare, diagnose, load_collected_csv,
                        load_curated_json, name_score)


def load_cells(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return [{"lat": r["latitude"], "lng": r["longitude"],
             "radius_m": r["radius_m"], "saturated": r["saturated"]}
            for r in conn.execute("SELECT * FROM search_cells")]


def main(argv):
    curated_path = argv[1] if len(argv) > 1 else None
    collected_path = argv[2] if len(argv) > 2 else None
    db_path = argv[3] if len(argv) > 3 else "data/restaurants.db"
    if not curated_path or not collected_path:
        print(__doc__)
        return 2

    curated = load_curated_json(curated_path)
    collected = load_collected_csv(collected_path)
    matches, misses, closed = compare(curated, collected)
    comparable = len(curated) - len(closed)

    print(f"  curated list   : {len(curated)}")
    print(f"  closed         : {len(closed)}  (excluded - Google never returns them)")
    print(f"  comparable     : {comparable}")
    print(f"  matched        : {len(matches)}  "
          f"({100 * len(matches) / comparable:.0f}%)")
    print(f"  missing        : {len(misses)}")

    evidence = collections.Counter(m.evidence for m in matches)
    print("\n  what identified each match:")
    for name, n in evidence.most_common():
        print(f"    {name:<16}{n:>4}")

    weak = [m for m in matches
            if name_score(m.target["name"], m.candidate["name"]) < 0.55]
    if weak:
        print(f"\n  {len(weak)} match(es) a name-only comparison would have missed:")
        for m in weak:
            print(f"    {m.target['name'][:30]:<30} -> {m.candidate['name'][:30]:<30} "
                  f"via {m.evidence}")

    if misses:
        cells = load_cells(db_path)
        print("\n  why each missing place is missing:")
        grouped = collections.defaultdict(list)
        for m in misses:
            reason, detail = diagnose(m.target, cells)
            grouped[reason].append((m.target["name"], detail))
        for reason, items in grouped.items():
            print(f"\n    {reason}:")
            for name, detail in sorted(items):
                bits = ", ".join(f"{k}={v:.0f}" if isinstance(v, float) else f"{k}={v}"
                                 for k, v in detail.items())
                print(f"      {name[:34]:<34} {bits}")

    if closed:
        print("\n  excluded as closed: " + ", ".join(c["name"] for c in closed))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
