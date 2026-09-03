"""Match a curated list of places against a collected dataset.

Deciding whether two records are the same real place is a record-linkage
problem, not a string-similarity one. Names disagree far more than they agree:
a bar at Laboratooriumi 23 is listed as "Lb23", "180 deg by Matthias Diether"
is collected as "180 Degrees Restaurant", "KotKot" as "kot.NOBLESSNER". Names
also *falsely* agree on position -- "Pilsneri baar" and "Uus Laine" are 13
metres apart and are different businesses, as are "The Brick Coffee Roastery"
and "Melt Froyo" at 10 metres.

So identity is decided on evidence, strongest first:

  1. phone number   - an exact key; two businesses do not share one
  2. street address - street name plus house number, near each other
  3. name + position - the weak fallback, needing both to agree

Anything the curated source marks as closed is excluded from the comparison
rather than counted as a miss: a permanently closed place is not returned by
Google's nearby search, so no sweep can ever find it.
"""

from __future__ import annotations

import csv
import difflib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

EARTH_RADIUS_M = 6_371_008.8

# Words naming what a place is rather than which place it is.
GENERIC = {
    "restaurant", "restoran", "resto", "ravintola", "cafe", "kohvik", "kohv",
    "bar", "baar", "pub", "bistro", "bistroo", "kitchen", "koogikoda", "tn",
    "the", "by", "and", "ja", "tallinn", "eesti", "estonia", "ou", "as",
}

# Written forms that mean the same thing in a venue name.
_NAME_SUBSTITUTIONS = [
    ("°", " degrees "),   # 180deg -> 180 degrees
    ("&", " and "),
    ("+", " and "),
]


def _fold(text: str) -> str:
    s = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def norm(name: str) -> str:
    s = _fold(name)
    for a, b in _NAME_SUBSTITUTIONS:
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def tokens(name: str) -> List[str]:
    words = norm(name).split()
    kept = [w for w in words if w not in GENERIC]
    return kept or words


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


# -- identity keys ---------------------------------------------------------


# Dialling codes stripped before comparing numbers. One source writes
# "+372 661 0180" and another "661 0180"; both name the same line. Extend this
# when comparing lists from another country.
COUNTRY_CODES: Tuple[str, ...] = ("372",)


def phone_key(phone: str, country_codes: Sequence[str] = COUNTRY_CODES) -> Optional[str]:
    """The local part of a phone number: dialling code and formatting removed.

    Trimming a fixed number of trailing digits does not work -- an Estonian
    landline has seven digits and a mobile eight, so the same slice keeps part
    of the country code on one and drops a real digit from the other.
    """
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return None
    digits = digits.lstrip("0")          # 00-prefixed international form
    for code in country_codes:
        if digits.startswith(code) and len(digits) - len(code) >= 6:
            digits = digits[len(code):]
            break
    return digits if len(digits) >= 6 else None


def address_key(address: str) -> Optional[str]:
    """Street name plus house number, e.g. "laboratooriumi 23".

    Estonian addresses put the number after the street, optionally with a
    "tn"/"tee"/"pst" marker between: "Laboratooriumi tn 23", "Laboratooriumi 23".
    A trailing unit ("60a-5", "60-2") is kept, since it distinguishes separate
    businesses in one building.
    """
    if not address:
        return None
    head = _fold(address).split(",")[0]
    head = re.sub(r"\b(tn|tee|pst|mnt|tanav|street|road)\b", " ", head)
    head = re.sub(r"[^a-z0-9\- ]+", " ", head)
    words = head.split()
    street = [w for w in words if not re.match(r"^\d", w)]
    number = [w for w in words if re.match(r"^\d", w)]
    if not street or not number:
        return None
    return f"{' '.join(street)} {number[0]}"


def name_score(a: str, b: str) -> float:
    """0-1 name similarity, never flattering a shorter candidate."""
    at, bt = set(tokens(a)), set(tokens(b))
    if not at or not bt:
        return 0.0
    jaccard = len(at & bt) / len(at | bt)
    covered = len(at & bt) / len(at)
    ratio = difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
    shared_long = {t for t in at & bt if len(t) >= 5}
    score = max(jaccard, ratio, 0.9 if shared_long else 0.0)
    if covered == 1.0 and len(at) >= 2:
        score = max(score, 0.95)
    return score


# -- linkage ---------------------------------------------------------------


@dataclass
class Match:
    target: dict
    candidate: Optional[dict]
    evidence: str          # "phone" | "address" | "name+position" | ""
    distance_m: Optional[float]

    @property
    def matched(self) -> bool:
        return self.candidate is not None


# How far apart two records may sit and still be one place. Sources disagree
# about a venue's coordinates by a surprising margin -- a curated list may
# point at the street entrance while Google points at the unit -- so the
# tolerance is generous where an exact key already agrees, and tight where
# the only evidence is a similar name.
PHONE_MAX_M = 2000.0
ADDRESS_MAX_M = 400.0
NAME_MAX_M = 120.0
NAME_MIN_SCORE = 0.55

# A name this similar vouches for a match on its own, overriding a phone number
# that disagrees -- see Linker._phones_contradict.
NAME_VOUCH_SCORE = 0.8


class Linker:
    def __init__(self, records: Sequence[dict]):
        self.records = list(records)
        self._by_phone: Dict[str, List[dict]] = {}
        self._by_address: Dict[str, List[dict]] = {}
        for r in self.records:
            for key, index in ((phone_key(r.get("phone") or ""), self._by_phone),
                               (address_key(r.get("address") or ""), self._by_address)):
                if key:
                    index.setdefault(key, []).append(r)

    def _distance(self, target: dict, cand: dict) -> Optional[float]:
        try:
            return haversine_m(float(target["lat"]), float(target["lng"]),
                               float(cand["lat"]), float(cand["lng"]))
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _phones_contradict(target: dict, cand: dict) -> bool:
        """True when two records disagree about the phone AND about the name.

        Two businesses can share a doorway -- a coffee roastery and a kiosk both
        at Ankru 10 -- so a matching address alone is not identity, and a
        differing phone number settles it: different lines, different business.

        But a phone number goes stale. Where both sources independently give the
        same *name* at the same address, an old number on one side is the likely
        explanation, not two same-named businesses at one address. So a
        disagreeing phone only vetoes when the name does not vouch for the match.
        """
        a = phone_key(target.get("phone") or "")
        b = phone_key(cand.get("phone") or "")
        if not (a and b and a != b):
            return False
        return name_score(target.get("name", ""), cand.get("name", "")) < NAME_VOUCH_SCORE

    def _nearest(self, target: dict, candidates: Iterable[dict],
                 limit: float) -> Tuple[Optional[dict], Optional[float]]:
        best, best_d = None, None
        for c in candidates:
            if self._phones_contradict(target, c):
                continue
            d = self._distance(target, c)
            if d is None:                       # no coordinates: accept the key
                return c, None
            if d <= limit and (best_d is None or d < best_d):
                best, best_d = c, d
        return best, best_d

    def find(self, target: dict) -> Match:
        # 1. phone: an exact key, so position only guards against reused numbers
        key = phone_key(target.get("phone") or "")
        if key and key in self._by_phone:
            cand, d = self._nearest(target, self._by_phone[key], PHONE_MAX_M)
            if cand:
                return Match(target, cand, "phone", d)

        # 2. street address plus house number
        key = address_key(target.get("address") or "")
        if key and key in self._by_address:
            cand, d = self._nearest(target, self._by_address[key], ADDRESS_MAX_M)
            if cand:
                return Match(target, cand, "address", d)

        # 3. similar name, close by - both required
        best, best_d, best_s = None, None, 0.0
        for c in self.records:
            if self._phones_contradict(target, c):
                continue
            d = self._distance(target, c)
            if d is None or d > NAME_MAX_M:
                continue
            s = name_score(target.get("name", ""), c.get("name", ""))
            if s >= NAME_MIN_SCORE and s > best_s:
                best, best_d, best_s = c, d, s
        if best:
            return Match(target, best, "name+position", best_d)

        return Match(target, None, "", None)


def is_closed(record: dict) -> bool:
    """Whether the source marks this place as closed down."""
    value = record.get("closed")
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def compare(targets: Sequence[dict], records: Sequence[dict]):
    """Link every open target against the collected records.

    Returns (matches, misses, skipped_closed).
    """
    linker = Linker(records)
    matches, misses, closed = [], [], []
    for t in targets:
        if is_closed(t):
            closed.append(t)
            continue
        m = linker.find(t)
        (matches if m.matched else misses).append(m)
    return matches, misses, closed


def load_collected_csv(path: str) -> List[dict]:
    """Read an allrestaurants export into linkage records."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append({
                "name": row.get("name"),
                "lat": row.get("latitude"),
                "lng": row.get("longitude"),
                "phone": row.get("phone_international") or row.get("phone"),
                "address": row.get("formatted_address"),
                "place_id": row.get("place_id"),
            })
    return out


def load_curated_json(path: str) -> List[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# -- why a place is missing ------------------------------------------------

# Reasons a curated place can be absent, in the order they are checked.
NEVER_SEARCHED = "never searched"
SEARCHED_SATURATED = "searched, but every covering circle was full"
SEARCHED_NOT_RETURNED = "searched and not returned (wrong type, or below the review bar)"
NO_POSITION = "no coordinates in the curated record"


def diagnose(target: dict, cells: Sequence[dict]) -> Tuple[str, dict]:
    """Explain a miss from the sweep's own log, without further API calls.

    ``cells`` are search-circle rows: lat, lng, radius_m, saturated.

    Distinguishes the three failures that need different fixes: an area never
    covered needs another pass, a circle that came back full needs splitting,
    and a circle that had room and still did not return the place was filtered
    on type or on the review threshold.
    """
    try:
        lat, lng = float(target["lat"]), float(target["lng"])
    except (KeyError, TypeError, ValueError):
        return NO_POSITION, {}

    covering = [c for c in cells
                if haversine_m(lat, lng, float(c["lat"]), float(c["lng"]))
                <= float(c["radius_m"])]
    if not covering:
        nearest = min((haversine_m(lat, lng, float(c["lat"]), float(c["lng"]))
                       - float(c["radius_m"]) for c in cells), default=None)
        return NEVER_SEARCHED, {"circles": 0, "gap_m": nearest}

    saturated = sum(1 for c in covering if c.get("saturated"))
    detail = {"circles": len(covering), "saturated": saturated,
              "smallest_radius_m": min(float(c["radius_m"]) for c in covering)}
    if saturated == len(covering):
        return SEARCHED_SATURATED, detail
    return SEARCHED_NOT_RETURNED, detail
