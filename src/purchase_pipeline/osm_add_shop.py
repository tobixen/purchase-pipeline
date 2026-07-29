#!/usr/bin/env python3
"""Create ONE surveyed shop node in OpenStreetMap.

Usage::

    osm-add-shop --lat 42.41934022085787 --lon 27.692148284820842 \\
                 --name "Sozopol Fish" --shop seafood [--tag k=v]… [--commit]

Dry run by default. ``--commit`` opens a changeset, creates the node, closes the
changeset — and that write is **public and attributed to the user**, so unlike
most of this pipeline it is not merely irreversible-ish.

Three constraints are requirements, not options:

**The duplicate check is mandatory and cannot be waived wholesale.** Duplicate
POIs are the main way well-intentioned scripts damage OSM, so this queries
Overpass first and refuses if anything looks like the same shop. There is no
``--force``: to proceed you must name each blocker with
``--not-a-duplicate-of TYPE:ID``, which is a claim that you looked at that
specific object and it is a different business. An agent cannot honestly produce
those flags on its own — it has not looked at anything.

The check distinguishes "a POI of the same kind is here" from "this shop is
here", because the Sozopol fish shop sits inside an ``amenity=marketplace`` way
10 m from the survey point. Blocking on proximity alone would make every market
stall unmappable; blocking on name alone would miss the shop someone else already
mapped under a name you do not use.

An empty duplicate-check answer is corroborated before it is trusted: a
``--overpass-endpoint`` pointed at a **regional extract** reports "nothing here"
for the whole rest of the planet, which reads exactly like a clear site. So when
nothing is found, the endpoint is asked whether it holds any road within a
kilometre, and a no refuses the run.

**Never generate the data.** Coordinates come from the user's survey or GPS, the
name off the shopfront. This tool formats tags; it must not invent a shop, and
neither may an AI agent driving it. One mechanical guard backs that up:
coordinates need at least 5 decimal places, because an invented or hand-rounded
figure is round and a GPS fix is not.

**Honest changesets:** ``source=survey``, ``created_by=purchase-pipeline/osm_add_shop``.
That stays within the `Automated Edits code of conduct
<https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct>`_ because it
is a human survey with a scripted upload. It stops being true the moment this is
pointed at a batch — hence one shop per invocation, and no batch mode. Do not add
one.

Auth: a token from ``osm-auth`` (or ``$OSM_TOKEN``).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from purchase_pipeline.osm_auth import load_token
from purchase_pipeline.osm_resolve import (
    COVERAGE_RADIUS_M,
    OVERPASS_ENDPOINT,
    POI_KEYS,
    Candidate,
    OverpassError,
    build_query,
    has_coverage,
    name_score,
    overpass_query,
    rank_candidates,
)
from purchase_pipeline.shop_osm import osm_url, parse_osm_spec

CREATED_BY = "purchase-pipeline/osm_add_shop"
DUPLICATE_RADIUS_M = 30
# Above this, two names are close enough that a human must look before adding.
NAME_MATCH_THRESHOLD = 0.6
# 5 decimal places is ~1 m; 2 is ~1 km. A GPS fix is never round.
MIN_COORD_DECIMALS = 5


def decimal_places(text: str) -> int:
    """Digits after the decimal point in *text* as typed."""
    _, _, frac = text.strip().partition(".")
    return len(frac)


def parse_tag(spec: str) -> tuple[str, str]:
    """Parse a ``key=value`` tag; both halves must be non-empty."""
    key, sep, value = spec.partition("=")
    if not sep or not key.strip() or not value.strip():
        raise ValueError(f"bad --tag {spec!r}; expected key=value")
    return key.strip(), value.strip()


def build_node_tags(
    *, name: str, shop: str | None = None, extra: list[tuple[str, str]] | None = None
) -> dict[str, str]:
    """The tags for the new node, including ``source=survey``.

    Refuses a nameless node, a node with no POI key (a name with no kind is not a
    shop), and any attempt to relabel ``source`` — the node claims to come from a
    survey, and that claim is the tool's, not the caller's, to make.
    """
    extra = list(extra or [])
    if not name.strip():
        raise ValueError("a name is required; it comes off the shopfront")
    tags: dict[str, str] = {"name": name.strip()}
    if shop:
        tags["shop"] = shop.strip()
    for key, value in extra:
        if key == "source":
            raise ValueError("source is always 'survey' here and cannot be overridden")
        tags[key] = value
    if not any(key in tags for key in POI_KEYS):
        raise ValueError(f"no POI key among {'/'.join(POI_KEYS)} — pass --shop VALUE, or --tag amenity=… etc.")
    tags["source"] = "survey"
    return tags


def changeset_tags(*, comment: str) -> dict[str, str]:
    """Changeset tags that say plainly what made the edit and where it came from."""
    if not comment.strip():
        raise ValueError("a changeset comment is required")
    return {"comment": comment.strip(), "created_by": CREATED_BY, "source": "survey"}


def poi_tags_of(tags: dict[str, str]) -> dict[str, str]:
    """Just the tags that say what kind of thing this is."""
    return {key: tags[key] for key in POI_KEYS if key in tags}


def blocking_duplicates(
    candidates: list[Candidate],
    *,
    name: str,
    poi_tags: dict[str, str],
    acknowledged: set[tuple[str, int]],
) -> list[tuple[Candidate, str]]:
    """Nearby objects that must be looked at before creating this node.

    A candidate blocks if its name is close to *name*, or if it already carries
    one of the exact ``key=value`` pairs we are about to create — the latter
    catching the unnamed stall and the one mapped under another name. A POI of a
    *different* kind with a *different* name does not block, which is what lets a
    new stall be added inside a mapped marketplace.
    """
    blockers: list[tuple[Candidate, str]] = []
    for cand in candidates:
        if (cand.osm_type, cand.osm_id) in acknowledged:
            continue
        score = name_score(name, cand.tags)
        same_kind = [f"{k}={v}" for k, v in poi_tags.items() if cand.tags.get(k) == v]
        if score >= NAME_MATCH_THRESHOLD:
            blockers.append((cand, f"name similarity {score:.2f} to {cand.name!r}"))
        elif same_kind:
            blockers.append((cand, f"already tagged {', '.join(same_kind)}"))
    return blockers


def upload_node(lat: float, lon: float, tags: dict[str, str], *, changeset: dict[str, str], api: Any) -> dict[str, Any]:
    """Open a changeset, create the node, close the changeset.

    The close is in a ``finally``: an open changeset left dangling is litter in
    somebody else's database, and it also blocks the next edit for 24 hours.
    """
    api.changeset_create(changeset)
    try:
        return api.node_create({"lat": lat, "lon": lon, "tag": tags})
    finally:
        api.changeset_close()


def _api(token: str, *, api_base: str) -> Any:  # pragma: no cover - network
    """An authenticated ``osmapi.OsmApi``. Imported late: osmapi is an extra."""
    import niquests as requests
    import osmapi

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    return osmapi.OsmApi(api=api_base, session=session, created_by=CREATED_BY)


def _coord(parser: argparse.ArgumentParser, flag: str, raw: str) -> tuple[float, int]:
    try:
        return float(raw), decimal_places(raw)
    except ValueError:
        parser.error(f"{flag} {raw!r} is not a number")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lat", required=True, help="Surveyed latitude — from GPS, never rounded or guessed")
    parser.add_argument("--lon", required=True, help="Surveyed longitude")
    parser.add_argument("--name", required=True, help="Name as written on the shopfront")
    parser.add_argument("--shop", help="The shop=* value, e.g. seafood, bakery, convenience")
    parser.add_argument("--tag", action="append", default=[], metavar="K=V", help="Extra tag (repeatable)")
    parser.add_argument(
        "--not-a-duplicate-of",
        action="append",
        default=[],
        metavar="TYPE:ID",
        help="Attest that you looked at this nearby object and it is a different business (repeatable)",
    )
    parser.add_argument("--radius", type=int, default=DUPLICATE_RADIUS_M, metavar="M", help="Duplicate-check radius")
    parser.add_argument("--overpass-endpoint", default=OVERPASS_ENDPOINT, help="Use a mirror if the main one is busy")
    parser.add_argument("--comment", help="Changeset comment (default: Add NAME, surveyed on the ground)")
    parser.add_argument("--api", default="https://www.openstreetmap.org", help="Use the dev server to rehearse")
    parser.add_argument("--commit", action="store_true", help="Actually create the node (public, attributed to you)")
    args = parser.parse_args(argv)

    lat, lat_decimals = _coord(parser, "--lat", args.lat)
    lon, lon_decimals = _coord(parser, "--lon", args.lon)
    if min(lat_decimals, lon_decimals) < MIN_COORD_DECIMALS:
        print(
            f"❌ --lat/--lon have {lat_decimals}/{lon_decimals} decimal places; at least "
            f"{MIN_COORD_DECIMALS} are required (~1 m).\n"
            f"   A rounded coordinate is a typed or invented one, and a surveyed shop node has to sit "
            f"where the shop is. Use the GPS reading, or `openprices-publish --coords-from-photo PHOTO`."
        )
        return 1

    try:
        tags = build_node_tags(name=args.name, shop=args.shop, extra=[parse_tag(t) for t in args.tag])
        changeset = changeset_tags(comment=args.comment or f"Add {args.name.strip()}, surveyed on the ground")
        acknowledged = {parse_osm_spec(spec) for spec in args.not_a_duplicate_of}
    except ValueError as exc:
        print(f"❌ {exc}")
        return 1

    poi = poi_tags_of(tags)
    print(f"## Duplicate check — {args.radius} m around {lat},{lon}\n")
    try:
        elements = overpass_query(build_query(lat, lon, args.radius), endpoint=args.overpass_endpoint)
    except OverpassError as exc:
        # "Could not check" must never be treated as "nothing found": the whole
        # protection against creating a duplicate is this query having answered.
        print(f"❌ the duplicate check could not run: {exc}")
        print(
            "   Refusing to create anything. Overpass is a free public service and rate-limits\n"
            "   under load — retry in a minute, or try --overpass-endpoint MIRROR (one that\n"
            "   carries the whole planet; see the coverage note below)."
        )
        return 1

    if not elements:
        # An empty answer is the only result that permits creating a node, so it is
        # the one that has to be corroborated. A regional-extract mirror reports
        # "nothing here" about the entire rest of the planet.
        try:
            covered = has_coverage(lat, lon, endpoint=args.overpass_endpoint)
        except OverpassError as exc:
            print(f"❌ the coverage probe could not run: {exc}")
            print("   Refusing to create anything: an unverified empty answer is not a clear site.")
            return 1
        if not covered:
            print(
                f"❌ {args.overpass_endpoint} reports nothing whatsoever within {COVERAGE_RADIUS_M} m of\n"
                f"   these coordinates — not even a road. That is what a regional **extract** looks\n"
                f"   like from outside its region, so the empty duplicate-check answer proves nothing.\n"
                f"   Refusing to create anything. Use {OVERPASS_ENDPOINT}, or a mirror with full\n"
                f"   planet coverage."
            )
            return 1

    candidates = rank_candidates(elements, name=args.name, lat=lat, lon=lon)
    blockers = blocking_duplicates(candidates, name=args.name, poi_tags=poi, acknowledged=acknowledged)

    stale = acknowledged - {(c.osm_type, c.osm_id) for c in candidates}
    for osm_type, osm_id in sorted(stale):
        print(
            f"  ⚠️  --not-a-duplicate-of {osm_type}:{osm_id} is not among the objects found now. "
            f"Either the radius changed or the map did; nothing is being waived by that flag."
        )

    if blockers:
        print(f"\n❌ {len(blockers)} nearby object(s) may already be this shop. Refusing to create a duplicate.\n")
        for cand, reason in blockers:
            distance = "?" if cand.distance_m is None else f"{cand.distance_m:.0f} m"
            print(f"  {cand.spec}  {distance}  {cand.name or '(unnamed)'}  [{cand.poi_tags()}] — {reason}")
            print(f"        {cand.url}")
        print(
            "\nOpen each link. If it IS the shop, you want `osm-resolve --save-as KEY --pick TYPE:ID`,\n"
            "not a new node. If it genuinely is a different business, say so per object:\n"
            + "".join(f"  --not-a-duplicate-of {c.spec} \\\n" for c, _ in blockers).rstrip(" \\\n")
        )
        return 1

    print(f"  {len(candidates)} POI(s) nearby, none of them this shop.\n")
    print("## Node to create\n")
    print(f"  {lat},{lon}")
    for key, value in sorted(tags.items()):
        print(f"  {key}={value}")
    print("\n## Changeset\n")
    for key, value in sorted(changeset.items()):
        print(f"  {key}={value}")

    if not args.commit:
        print("\nDry run. Re-run with --commit to create it — that write is public and attributed to you.")
        return 0

    token = load_token()
    if not token:
        print("\n❌ no OSM token. Run `osm-auth --client-id YOUR_CLIENT_ID` once, or set $OSM_TOKEN.")
        return 1

    node = upload_node(lat, lon, tags, changeset=changeset, api=_api(token, api_base=args.api))
    node_id = node["id"]
    print(f"\n✓ created NODE:{node_id}\n  {osm_url('NODE', node_id)}")
    print(
        "\nCache it against the branch you visited, then prices can point at it:\n"
        f"  osm-resolve --save-as '{args.name.strip()} TOWN STREET' --pick NODE:{node_id}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
