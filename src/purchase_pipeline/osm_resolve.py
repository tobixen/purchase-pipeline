#!/usr/bin/env python3
"""Find the OSM object of a shop that already exists — read-only, no auth.

Usage::

    osm-resolve --lat 42.41934 --lon 27.69215 --name "Billa" [--radius 50]
    osm-resolve --lat … --lon … --name "Billa" --save-as "Billa Sozopol ул. Републиканска 5" \\
                --pick WAY:1016681733
    osm-resolve --save-as "Billa Sozopol ул. Републиканска 5" --pick WAY:1016681733

It queries **Overpass** for POIs within ``--radius`` metres of a surveyed point,
ranks them by name similarity, and prints each with an ``openstreetmap.org``
link. Confirming a candidate is a human act performed in a browser; this command
only records the outcome, and only when told to with ``--save-as`` + ``--pick``.

Why Overpass and not Nominatim: on 2026-07-24 the friction was not mapping a
shop but *finding the node id of one that already existed*, and that took four
hand-rolled Nominatim round-trips. Nominatim is a geocoder — reverse-geocoding
the Sozopol fish shop's coordinates returned a neighbouring wine shop, because a
geocoder answers "what address is this point" and not "what is here". A radius
query is the right question.

Unnamed POIs are listed too, and rank last rather than being filtered out. An
unnamed ``shop=seafood`` five metres from the survey point is the single most
important thing to see before ``osm_add_shop`` creates a duplicate.

Coordinates come from a survey or a photo's GPS. This tool never invents them,
and neither may an agent driving it.

Deliberately stdlib-only for HTTP: it is read-only and useful without the
``publish`` extra (``niquests``) installed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from purchase_pipeline.shop_osm import SHOP_OSM_PATH, osm_url, parse_osm_spec, save_entry

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
USER_AGENT = "purchase-pipeline/osm_resolve (tobixen)"
DEFAULT_RADIUS_M = 50

# Tag keys that can carry a shop. `shop=*` is not enough: the Sozopol fish shop
# is a магазин за риба, which mappers write as shop=seafood, but a bakery may be
# craft=bakery and a pharmacy amenity=pharmacy.
POI_KEYS = ("shop", "amenity", "craft", "office", "healthcare", "leisure", "tourism")

# `amenity` is the noisy one — a 50 m radius in a town centre is full of street
# furniture. These values cannot be a shop, so they are dropped even when named.
NOT_A_SHOP = frozenset(
    {
        "bench",
        "bicycle_parking",
        "bicycle_repair_station",
        "bbq",
        "clock",
        "drinking_water",
        "fountain",
        "grit_bin",
        "motorcycle_parking",
        "parking",
        "parking_entrance",
        "parking_space",
        "post_box",
        "recycling",
        "shelter",
        "street_lamp",
        "telephone",
        "toilets",
        "waste_basket",
        "waste_disposal",
        "watering_place",
    }
)

# Name tags worth scoring against. A Bulgarian shopfront and a Bulgarian receipt
# may disagree about script, so `name` alone is not enough: the fish shop's
# `name` is Cyrillic and its Latin form only ever appears in `name:en`.
NAME_TAGS = ("name", "name:en", "int_name", "official_name", "alt_name", "brand", "operator")


@dataclass
class Candidate:
    """One ranked Overpass POI."""

    osm_type: str
    osm_id: int
    name: str
    tags: dict[str, str] = field(default_factory=dict)
    lat: float | None = None
    lon: float | None = None
    distance_m: float | None = None
    score: float = 0.0

    @property
    def spec(self) -> str:
        return f"{self.osm_type}:{self.osm_id}"

    @property
    def url(self) -> str:
        return osm_url(self.osm_type, self.osm_id)

    def poi_tags(self) -> str:
        """The tags that say what this is, e.g. ``shop=supermarket``."""
        return " ".join(f"{k}={self.tags[k]}" for k in POI_KEYS if k in self.tags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "osm_type": self.osm_type,
            "osm_id": self.osm_id,
            "name": self.name,
            "score": round(self.score, 3),
            "distance_m": None if self.distance_m is None else round(self.distance_m, 1),
            "url": self.url,
            "tags": self.tags,
        }


def build_query(lat: float, lon: float, radius_m: int = DEFAULT_RADIUS_M) -> str:
    """Overpass QL for every POI-ish object within *radius_m* of (lat, lon).

    ``nwr`` covers nodes, ways and relations — a supermarket is usually a
    building way, a kiosk a node. ``out tags center`` is what makes the
    non-nodes usable: without ``center`` a way comes back with no coordinates
    and cannot be distance-ranked.
    """
    around = f"around:{radius_m},{lat},{lon}"
    clauses = "\n".join(f'  nwr({around})["{key}"];' for key in POI_KEYS)
    return f"[out:json][timeout:25];\n(\n{clauses}\n);\nout tags center;"


def overpass_query(
    query: str, *, endpoint: str = OVERPASS_ENDPOINT, timeout: int = 60
) -> list[dict[str, Any]]:  # pragma: no cover - network
    """POST *query* to Overpass and return its ``elements`` list."""
    request = urllib.request.Request(
        endpoint,
        data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - fixed https endpoint
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("elements", [])


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    radius = 6371008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def element_point(element: dict[str, Any]) -> tuple[float, float] | None:
    """(lat, lon) of an Overpass element — its own for a node, ``center`` otherwise."""
    if "lat" in element and "lon" in element:
        return (float(element["lat"]), float(element["lon"]))
    center = element.get("center")
    if isinstance(center, dict) and "lat" in center and "lon" in center:
        return (float(center["lat"]), float(center["lon"]))
    return None


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def display_name(tags: dict[str, str]) -> str:
    """The best human label for a POI, or ``""`` for an unnamed one."""
    for key in NAME_TAGS:
        if tags.get(key):
            return str(tags[key])
    return ""


def name_score(want: str | None, tags: dict[str, str]) -> float:
    """How well *want* matches any of the POI's name tags, in ``0.0..1.0``.

    A substring match in either direction floors the score at 0.9: "Billa" is a
    strong match for "Billa Sozopol" even though the character ratio is not.
    """
    want_n = _norm(want or "")
    if not want_n:
        return 0.0
    best = 0.0
    for key in NAME_TAGS:
        value = _norm(str(tags.get(key) or ""))
        if not value:
            continue
        ratio = SequenceMatcher(None, want_n, value).ratio()
        if want_n in value or value in want_n:
            ratio = max(ratio, 0.9)
        best = max(best, ratio)
    return best


def is_shoplike(tags: dict[str, str]) -> bool:
    """Whether the tags could describe a shop, dropping street furniture."""
    keys = [k for k in POI_KEYS if k in tags]
    if not keys:
        return False
    if keys == ["amenity"] and tags["amenity"] in NOT_A_SHOP:
        return False
    return True


def rank_candidates(elements: list[dict[str, Any]], *, name: str | None, lat: float, lon: float) -> list[Candidate]:
    """Rank Overpass elements by name similarity, then by distance from (lat, lon).

    Name beats distance, deliberately: an exact name match 40 m away is a better
    answer than an unrelated POI 3 m away, and the survey point is a phone GPS
    fix outside a shopfront, not a centroid.
    """
    candidates: list[Candidate] = []
    for element in elements:
        tags = {str(k): str(v) for k, v in (element.get("tags") or {}).items()}
        if not is_shoplike(tags):
            continue
        point = element_point(element)
        candidates.append(
            Candidate(
                osm_type=str(element.get("type", "node")).upper(),
                osm_id=int(element["id"]),
                name=display_name(tags),
                tags=tags,
                lat=None if point is None else point[0],
                lon=None if point is None else point[1],
                distance_m=None if point is None else haversine_m(lat, lon, *point),
                score=name_score(name, tags),
            )
        )
    return sorted(candidates, key=lambda c: (-c.score, math.inf if c.distance_m is None else c.distance_m))


def format_candidates(candidates: list[Candidate]) -> str:
    """Human-readable ranked list, one candidate per two lines."""
    lines: list[str] = []
    for rank, cand in enumerate(candidates, start=1):
        distance = "?" if cand.distance_m is None else f"{cand.distance_m:.0f} m"
        label = cand.name or "(unnamed)"
        lines.append(f"  {rank}. score {cand.score:.2f}  {cand.spec:>18}  {distance:>6}  {label}  [{cand.poi_tags()}]")
        lines.append(f"        {cand.url}")
    return "\n".join(lines)


def _save(args: argparse.Namespace, candidates: list[Candidate], parser: argparse.ArgumentParser) -> int:
    """Record the confirmed pick. Returns a process exit status."""
    try:
        osm_type, osm_id = parse_osm_spec(args.pick)
    except ValueError as exc:
        parser.error(str(exc))
    out = sys.stderr if args.json else sys.stdout
    picked = next((c for c in candidates if (c.osm_type, c.osm_id) == (osm_type, osm_id)), None)
    if candidates and picked is None and not args.force:
        print(
            f"\n❌ {osm_type}:{osm_id} is not one of the candidates listed above. A --pick the query\n"
            f"   never returned is a typo rather than a confirmation. Re-run with --force if the\n"
            f"   object really is the right one (e.g. it sits outside --radius).",
            file=out,
        )
        return 1
    extra = {"name": picked.name} if picked and picked.name else None
    try:
        entry = save_entry(args.save_as, osm_type, osm_id, path=args.osm_cache, extra=extra, force=args.force)
    except ValueError as exc:
        print(f"\n❌ {exc}", file=out)
        return 1
    print(f"\n✓ cached {args.save_as!r} → {entry['osm_type']}:{entry['osm_id']} in {args.osm_cache}", file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lat", type=float, help="Surveyed latitude (from GPS or a photo) — never guessed")
    parser.add_argument("--lon", type=float, help="Surveyed longitude")
    parser.add_argument("--name", help="Shop name off the shopfront or receipt, for ranking")
    parser.add_argument("--radius", type=int, default=DEFAULT_RADIUS_M, metavar="M", help="Search radius in metres")
    parser.add_argument("--json", action="store_true", help="Print the ranked candidates as JSON")
    parser.add_argument("--save-as", metavar="KEY", help="Branch key to cache the confirmed pick under")
    parser.add_argument("--pick", metavar="TYPE:ID", help="The confirmed OSM object, e.g. WAY:1016681733")
    parser.add_argument("--osm-cache", type=Path, default=SHOP_OSM_PATH)
    parser.add_argument("--force", action="store_true", help="Override the --save-as / --pick sanity checks")
    parser.add_argument("--endpoint", default=OVERPASS_ENDPOINT)
    parser.add_argument("--timeout", type=int, default=60, help="Overpass HTTP timeout in seconds")
    args = parser.parse_args(argv)

    if (args.lat is None) != (args.lon is None):
        parser.error("--lat and --lon go together")
    if bool(args.save_as) != bool(args.pick):
        parser.error("--save-as and --pick go together: a key without an object records nothing")
    has_query = args.lat is not None
    if not has_query and not args.save_as:
        parser.error("give --lat/--lon to search, or --save-as/--pick to record a confirmed object")

    candidates: list[Candidate] = []
    if has_query:
        elements = overpass_query(
            build_query(args.lat, args.lon, args.radius), endpoint=args.endpoint, timeout=args.timeout
        )
        candidates = rank_candidates(elements, name=args.name, lat=args.lat, lon=args.lon)

        if args.json:
            print(json.dumps([c.to_dict() for c in candidates], ensure_ascii=False, indent=2))
        else:
            focus = f" matching {args.name!r}" if args.name else ""
            print(f"## POIs within {args.radius} m of {args.lat},{args.lon}{focus}\n")
            if candidates:
                print(format_candidates(candidates))
            else:
                print(
                    f"  no POI found. Either the shop is unmapped — then survey it and see osm_add_shop —\n"
                    f"  or it sits outside {args.radius} m; try --radius {args.radius * 2}."
                )

        if not candidates:
            return 1

    if args.save_as:
        return _save(args, candidates, parser)

    if not args.json:
        print(
            "\nConfirm one in the browser before trusting it — a chain has many branches and a name\n"
            "match is not confirmation. Then record it:\n"
            "  osm-resolve --save-as 'CHAIN TOWN STREET' --pick TYPE:ID"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
