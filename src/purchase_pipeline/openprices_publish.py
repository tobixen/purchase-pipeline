#!/usr/bin/env python3
"""Publish receipt prices to Open Food Facts Open Prices.

For one shop+date in the purchases ledger: upload the receipt photo once as a
RECEIPT proof, attach the shop's confirmed OSM location, then POST one price per
line item that has an EAN.

Shop location is an explicit, human-confirmed OSM object (``--osm TYPE:ID``,
cached per branch in :mod:`purchase_pipeline.shop_osm`) — never auto-geocoded,
because receipt photos are often taken away from the shop. Find the object with
``osm-resolve``; ``--coords-from-photo`` only prints a photo's EXIF GPS so that
``osm-resolve`` has a point to search around.

This used to reverse-geocode with Nominatim instead. That is the wrong tool: a
geocoder answers "what address is this point", so on 2026-07-24 it returned a
neighbouring wine shop for the Sozopol fish shop's coordinates.

Auth: token from $OPENPRICES_TOKEN or ~/.config/inventory-md/openprices-token
(run op_auth.py once to create it). Dry-run by default; --commit publishes.
Open Prices writes are public but reversible (you own your rows).

Usage:
    openprices_publish.py --shop "Billa Varna" --date 2026-06-06 \\
        --proof RECEIPT.jpg --osm WAY:1016681733 [--ledger purchases.jsonl] [--commit]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import niquests as requests

from purchase_pipeline.shop_osm import load_cache, match_shop_osm, parse_osm_spec, save_entry, shop_osm_candidates
from purchase_pipeline.tingbok_push import off_code

BASES = {"org": "https://prices.openfoodfacts.org", "net": "https://prices.openfoodfacts.net"}
TOKEN_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "inventory-md" / "openprices-token"


def _dms_to_deg(dms, ref: str) -> float:
    """Convert EXIF GPS (degrees, minutes, seconds) + hemisphere ref to decimal."""
    deg, minute, sec = (float(x) for x in dms)
    value = deg + minute / 60 + sec / 3600
    return -value if ref in ("S", "W") else value


def photo_latlon(path: str | Path) -> tuple[float, float] | None:
    """Read decimal (lat, lon) from a photo's EXIF GPS, or None."""
    from PIL import Image
    from PIL.ExifTags import GPSTAGS

    exif = Image.open(path)._getexif() or {}
    gps = {GPSTAGS.get(k, k): v for k, v in (exif.get(34853) or {}).items()}
    if "GPSLatitude" not in gps or "GPSLongitude" not in gps:
        return None
    lat = _dms_to_deg(gps["GPSLatitude"], gps.get("GPSLatitudeRef", "N"))
    lon = _dms_to_deg(gps["GPSLongitude"], gps.get("GPSLongitudeRef", "E"))
    return (lat, lon)


def build_price(row: dict[str, Any], *, proof_id: int, osm_type: str, osm_id: int) -> dict[str, Any]:
    """Build an Open Prices PriceCreate payload for a ledger row with an EAN."""
    # PRODUCT prices (EAN) must NOT carry price_per — that field is only for
    # barcodeless CATEGORY prices (loose produce priced per kg).
    payload = {
        "proof_id": proof_id,
        "type": "PRODUCT",
        "product_code": off_code(str(row["ean"])),
        "price": row["unit_price"],
        "currency": row.get("currency", "EUR"),
        "date": row["date"],
        "location_osm_id": osm_id,
        "location_osm_type": osm_type,
    }
    if row.get("name"):
        payload["product_name"] = row["name"]
    pwd = row.get("price_without_discount")
    if pwd is not None and float(pwd) > float(row["unit_price"]):
        payload["price_is_discounted"] = True
        payload["price_without_discount"] = float(pwd)
        payload["discount_type"] = row.get("discount_type", "SALE")
    return payload


def _parse_discount(spec: str) -> tuple[str, float, str]:
    """Parse 'EAN=GROSS[:TYPE]', e.g. '3800050405919=1.15:SALE'."""
    ean, _, rest = spec.partition("=")
    gross, _, dtype = rest.partition(":")
    return ean.strip(), float(gross), (dtype.strip().upper() or "SALE")


def _parse_category_price(spec: str) -> dict[str, Any]:
    """Parse 'TAG=PRICE[,was=GROSS][,type=SALE][,per=UNIT|KILOGRAM]'.

    e.g. 'en:baguettes=0.17,was=0.45,type=SALE' for barcodeless items.
    """
    items = [s.strip() for s in spec.split(",")]
    tag, _, price = items[0].partition("=")
    out: dict[str, Any] = {"category_tag": tag.strip(), "price": float(price), "price_per": "UNIT"}
    for kv in items[1:]:
        key, _, val = kv.partition("=")
        key = key.strip().lower()
        if key == "was":
            out["price_without_discount"] = float(val)
        elif key == "type":
            out["discount_type"] = val.strip().upper()
        elif key == "per":
            out["price_per"] = val.strip().upper()
    return out


def build_category_price(
    spec: dict[str, Any], *, proof_id: int, osm_type: str, osm_id: int, date: str, currency: str = "EUR"
) -> dict[str, Any]:
    """Build an Open Prices CATEGORY price (barcodeless item) from a parsed spec."""
    payload = {
        "proof_id": proof_id,
        "type": "CATEGORY",
        "category_tag": spec["category_tag"],
        "price": spec["price"],
        "currency": currency,
        "date": date,
        "price_per": spec.get("price_per", "UNIT"),
        "location_osm_id": osm_id,
        "location_osm_type": osm_type,
    }
    pwd = spec.get("price_without_discount")
    if pwd is not None and float(pwd) > float(spec["price"]):
        payload["price_is_discounted"] = True
        payload["price_without_discount"] = float(pwd)
        payload["discount_type"] = spec.get("discount_type", "SALE")
    return payload


def receipt_currency(rows: list[dict[str, Any]], shop: str, date: str) -> str:
    """The currency of *shop*'s receipt on *date*, from all its ledger rows.

    Category prices and the proof have no EAN row of their own to take it from,
    and a silent EUR default publishes a NOK or RON receipt in the wrong
    currency.
    """
    currencies = {r.get("currency") or "EUR" for r in rows if r.get("shop") == shop and r.get("date") == date}
    if not currencies:
        raise ValueError(f"no ledger rows for shop={shop!r} date={date!r}, so its currency is unknown")
    if len(currencies) > 1:
        raise ValueError(f"shop={shop!r} date={date!r} has several currencies in the ledger: {sorted(currencies)}")
    return currencies.pop()


def product_rows(
    ledger: list[dict[str, Any]], shop: str, date: str, only: list[str] | None = None
) -> list[dict[str, Any]]:
    """The ledger rows of *shop*'s receipt on *date* that become PRODUCT prices.

    *only* narrows them to the given codes, bare or tingbok-prefixed, so a line
    that failed can be re-sent without duplicating the ones that went through.
    """
    rows = [r for r in ledger if r.get("shop") == shop and r.get("date") == date and r.get("ean")]
    if only:
        wanted = {off_code(c) for c in only}
        rows = [r for r in rows if off_code(str(r["ean"])) in wanted]
    return rows


def _resolve_location(shop: str, osm_arg: str | None) -> tuple[str, int]:
    """Resolve a shop's confirmed OSM location from the branch-keyed cache.

    Explicit ``--osm`` wins and is persisted; otherwise a previously-confirmed
    entry in ``shop-osm.json`` is used. Never auto-geocodes (receipt photos may
    be taken away from the shop). The cache itself, and the guards on writing to
    it, live in :mod:`purchase_pipeline.shop_osm`.
    """
    if osm_arg:
        kind, num = parse_osm_spec(osm_arg)
        # force: --osm is an explicit human confirmation, so it may repoint a key
        # and may name a single-word shop. shop_osm's guards exist to stop a
        # *derived* key being written silently; this one was typed.
        save_entry(shop, kind, num, force=True)
        return kind, num
    hit = match_shop_osm(load_cache(), shop)
    if hit:
        return hit["osm_type"], hit["osm_id"]
    cands = shop_osm_candidates(load_cache(), shop)
    hint = f" Cached branches that look similar: {', '.join(cands)}." if cands else ""
    sys.exit(
        f"No confirmed OSM location for {shop!r}.{hint} Find it with "
        f"`osm-resolve --lat LAT --lon LON --name {shop!r}`, confirm it in the browser, and re-run "
        f"with --osm TYPE:ID."
    )


def _token() -> str | None:
    return os.environ.get("OPENPRICES_TOKEN") or (
        TOKEN_PATH.read_text(encoding="utf-8").strip() if TOKEN_PATH.exists() else None
    )


def main() -> None:  # pragma: no cover - network / CLI wiring
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--shop", default=None, help="Ledger shop name, e.g. 'Billa Varna'")
    parser.add_argument("--date", default=None, help="Ledger date YYYY-MM-DD")
    parser.add_argument("--proof", type=Path, default=None, help="Receipt image to upload as proof")
    parser.add_argument("--osm", help="Shop location as TYPE:ID (e.g. WAY:1016681733); confirmed & cached per shop")
    parser.add_argument("--proof-id", type=int, default=None, help="Reuse an already-uploaded proof id (skip upload)")
    parser.add_argument(
        "--coords-from-photo",
        type=Path,
        default=None,
        help="Print a photo's EXIF GPS as an osm-resolve invocation, then exit",
    )
    parser.add_argument("--ledger", type=Path, default=Path.home() / "regnskap" / "purchases.jsonl")
    parser.add_argument(
        "--discount",
        action="append",
        default=[],
        metavar="EAN=GROSS[:TYPE]",
        help="Mark an EAN discounted: paid price stays, GROSS is the regular price (repeatable)",
    )
    parser.add_argument(
        "--category-price",
        action="append",
        default=[],
        metavar="TAG=PRICE[,was=,type=,per=]",
        help="Publish a barcodeless CATEGORY price, e.g. en:baguettes=0.17,was=0.45,type=SALE (repeatable)",
    )
    parser.add_argument(
        "--no-products", action="store_true", help="Skip the EAN/PRODUCT prices (e.g. category-only run)"
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="EAN",
        help="Publish only these PRODUCT lines (repeatable), e.g. to retry a failed one with --proof-id",
    )
    parser.add_argument("--env", choices=["org", "net"], default="org")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    # --coords-from-photo: standalone mode — no shop/date/proof needed.
    # (Unreliable: the photo may be taken away from the shop — confirm before use.)
    if args.coords_from_photo:
        latlon = photo_latlon(args.coords_from_photo)
        if not latlon:
            sys.exit(f"No GPS in {args.coords_from_photo}")
        lat, lon = latlon
        print(f"EXIF GPS: {lat},{lon}")
        print(f"  osm-resolve --lat {lat} --lon {lon} --name 'SHOP'")
        print("Confirm the candidate in the browser, then re-run this with --osm TYPE:ID")
        return

    # Validate args required for normal publish flow.
    missing = [
        f"--{f}"
        for f, v in [("shop", args.shop), ("date", args.date), ("proof", args.proof or args.proof_id)]
        if v is None
    ]
    if missing:
        parser.error(f"{', '.join(missing)} required for publishing")

    discounts = {off_code(ean): (gross, dtype) for ean, gross, dtype in (_parse_discount(s) for s in args.discount)}
    category_specs = [_parse_category_price(s) for s in args.category_price]

    base = BASES[args.env]
    ledger = [json.loads(line) for line in args.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    try:
        currency = receipt_currency(ledger, args.shop, args.date)
    except ValueError as exc:
        sys.exit(str(exc))
    rows = [] if args.no_products else product_rows(ledger, args.shop, args.date, args.only)
    if not rows and not category_specs:
        sys.exit(f"Nothing to publish for shop={args.shop!r} date={args.date!r} (no EAN rows, no --category-price)")

    # Location must be explicitly confirmed (per shop), never auto-geocoded:
    # receipt photos are often taken away from the shop.
    osm_type, osm_id = _resolve_location(args.shop, args.osm)
    print(f"Location: OSM {osm_type}/{osm_id} for {args.shop!r}")
    proof_label = args.proof.name if args.proof else f"id {args.proof_id}"
    print(f"{len(rows)} priced line items; proof={proof_label}")

    headers = {"Authorization": f"Bearer {_token()}"} if args.commit else {}
    if args.commit and not _token():
        sys.exit("No token — run op_auth.py first (or set OPENPRICES_TOKEN).")

    # The proof carries the receipt's own currency/date/location; Open Prices
    # reconciles each price to its proof, so a proof missing these blanks out the
    # prices' currency/date in the UI. Stamp them at upload time.
    proof_currency = currency

    proof_id = args.proof_id
    if args.commit and proof_id is None:
        with args.proof.open("rb") as f:
            up = requests.post(
                f"{base}/api/v1/proofs/upload",
                files={"file": (args.proof.name, f, "image/jpeg")},
                data={
                    "type": "RECEIPT",
                    "currency": proof_currency,
                    "date": args.date,
                    "location_osm_id": str(osm_id),
                    "location_osm_type": osm_type,
                },
                headers=headers,
                timeout=120,
            )
        if up.status_code not in (200, 201):
            sys.exit(f"proof upload failed: {up.status_code} {up.text[:200]}")
        proof_id = up.json()["id"]
        print(f"proof uploaded -> id={proof_id}")

    for r in rows:
        if off_code(r["ean"]) in discounts:
            gross, dtype = discounts[off_code(r["ean"])]
            r = {**r, "price_without_discount": gross, "discount_type": dtype}
        payload = build_price(r, proof_id=proof_id or 0, osm_type=osm_type, osm_id=osm_id)
        disc = (
            f"  [discounted from {payload['price_without_discount']} {payload['discount_type']}]"
            if payload.get("price_is_discounted")
            else ""
        )
        if not args.commit:
            print(
                f"  DRY-RUN {payload['product_code']}  {payload['price']} {payload['currency']}  {r.get('name', '')[:40]}{disc}"
            )
            continue
        resp = requests.post(f"{base}/api/v1/prices", json=payload, headers=headers, timeout=60)
        ok = resp.status_code in (200, 201)
        print(
            f"  {'OK ' if ok else 'FAIL'} {resp.status_code} {payload['product_code']}  {payload['price']} {payload['currency']}"
            + ("" if ok else f"  {resp.text[:160]}")
        )

    for spec in category_specs:
        payload = build_category_price(
            spec, proof_id=proof_id or 0, osm_type=osm_type, osm_id=osm_id, date=args.date, currency=currency
        )
        disc = (
            f"  [discounted from {payload['price_without_discount']} {payload['discount_type']}]"
            if payload.get("price_is_discounted")
            else ""
        )
        if not args.commit:
            print(
                f"  DRY-RUN [cat] {payload['category_tag']}  {payload['price']} {payload['currency']}/{payload['price_per']}{disc}"
            )
            continue
        resp = requests.post(f"{base}/api/v1/prices", json=payload, headers=headers, timeout=60)
        ok = resp.status_code in (200, 201)
        print(
            f"  {'OK ' if ok else 'FAIL'} {resp.status_code} [cat] {payload['category_tag']}  {payload['price']} {payload['currency']}"
            + ("" if ok else f"  {resp.text[:160]}")
        )


if __name__ == "__main__":
    main()
