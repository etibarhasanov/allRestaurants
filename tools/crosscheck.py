"""Check a list of expected restaurant names against a collected CSV.

Name matching across sources is the whole difficulty here: the same place is
"Olde Hansa" in a guide and "Restoran Olde Hansa" on Google, and short names
like Cru, Salt or Juur collide with anything. Scoring is deliberately
conservative -- a miss you can check by hand beats a false match that hides a
genuine gap.
"""

from __future__ import annotations

import csv
import difflib
import re
import sys
import unicodedata
from typing import Dict, List, Optional, Tuple

# Words that describe what a place is rather than which place it is.
GENERIC = {
    "restaurant", "restoran", "resto", "ravintola", "cafe", "kohvik", "kohv",
    "bar", "baar", "pub", "bistro", "bistroo", "kitchen", "koogikoda",
    "the", "by", "and", "ja", "tallinn", "eesti", "estonia", "ou", "as",
}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", (name or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def tokens(name: str) -> List[str]:
    """Identity-bearing tokens, keeping generics only if nothing else remains."""
    words = norm(name).split()
    kept = [w for w in words if w not in GENERIC]
    return kept or words


def score(query: str, candidate: str) -> float:
    """0-1 similarity, biased against flattering a short candidate.

    Containment is measured against the *query*, never against whichever side
    is shorter: dividing by the shorter one lets any single-token candidate
    score a perfect match on a longer query, which is exactly how "Kitchen
    Room" once matched "Pohjala Tap Room".
    """
    qt, ct = set(tokens(query)), set(tokens(candidate))
    if not qt or not ct:
        return 0.0

    jaccard = len(qt & ct) / len(qt | ct)
    covered = len(qt & ct) / len(qt)
    ratio = difflib.SequenceMatcher(None, norm(query), norm(candidate)).ratio()

    # A single distinctive token matching in full is strong evidence, but only
    # when that token is long enough to be distinctive at all.
    shared_long = {t for t in qt & ct if len(t) >= 5}
    s = max(jaccard, ratio, 0.9 if shared_long else 0.0)

    # Every query token present, in order, inside the candidate.
    if covered == 1.0 and len(qt) >= 2:
        s = max(s, 0.95)
    return s


class Catalogue:
    def __init__(self, rows: List[dict], name_field: str = "name"):
        self.rows = rows
        self.name_field = name_field

    def best(self, query: str) -> Tuple[Optional[dict], float]:
        best_row, best_score = None, 0.0
        for row in self.rows:
            s = score(query, row[self.name_field])
            if s > best_score:
                best_row, best_score = row, s
        return best_row, best_score

    def find(self, query: str, threshold: float = 0.72) -> Tuple[Optional[dict], float]:
        row, s = self.best(query)
        # Very short names (Cru, Salt, Juur, Moon) collide with too much;
        # demand an exact normalised hit rather than a fuzzy one.
        if row is not None and len(norm(query)) <= 5:
            exact = [r for r in self.rows
                     if norm(query) in norm(r[self.name_field]).split()]
            return (exact[0], 1.0) if exact else (row, 0.0)
        return (row, s) if s >= threshold else (row, s)


def load(path: str) -> List[dict]:
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def run(csv_path: str, reference: List[Tuple[str, str]], threshold: float = 0.72):
    cat = Catalogue(load(csv_path))
    found, missing = [], []
    for name, tag in reference:
        row, s = cat.find(name, threshold)
        (found if s >= threshold else missing).append((name, tag, row, s))
    return found, missing
