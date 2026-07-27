#!/usr/bin/env python3
"""Publish receipt prices to Open Food Facts Open Prices.

For one shop+date in the purchases ledger: upload the receipt photo once as a
RECEIPT proof, attach the shop's confirmed OSM location, then POST one price per
line item that has an EAN.

Shop location is an explicit, human-confirmed OSM object (``--osm TYPE:ID``,
cached per shop) — never auto-geocoded, because receipt photos are often taken
away from the shop. ``--suggest-from-photo`` only prints an EXIF-GPS hint.

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

BASES = {"org": "https://prices.openfoodfacts.org", "net": "https://prices.openfoodfacts.net"}
TOKEN_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "inventory-md" / "openprices-token"
OSM_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "inventory-md" / "osm-geocode-cache.json"
USER_AGENT = "solveig-inventory/openprices_publish (tobixen)"


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
        "product_code": str(row["ean"]),
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


def _osm_cache_key(lat: float, lon: float) -> str:
    return f"{round(lat, 4)},{round(lon, 4)}"


def nominatim_reverse(lat: float, lon: float) -> dict[str, Any] | None:  # pragma: no cover - network
    """Reverse-geocode (lat, lon) to an OSM object, cached. Returns {osm_type, osm_id, name}."""
    cache: dict[str, Any] = {}
    if OSM_CACHE.exists():
        cache = json.loads(OSM_CACHE.read_text(encoding="utf-8"))
    key = _osm_cache_key(lat, lon)
    if key in cache:
        return cache[key]
    resp = requests.get(
        "https://nominatim.openstreetmap.org/reverse",
        params={"lat": lat, "lon": lon, "format": "jsonv2"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    d = resp.json()
    if "osm_id" not in d:
        return None
    result = {
        "osm_type": d["osm_type"].upper(),
        "osm_id": d["osm_id"],
        "name": d.get("name") or d.get("display_name", "")[:60],
    }
    cache[key] = result
    OSM_CACHE.parent.mkdir(parents=True, exist_ok=True)
    OSM_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


SHOP_OSM = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "inventory-md" / "shop-osm.json"


def _parse_osm(spec: str) -> tuple[str, int]:
    """Parse a 'TYPE:ID' OSM spec, e.g. 'WAY:1016681733' -> ('WAY', 1016681733)."""
    kind, _, num = spec.partition(":")
    kind = kind.strip().upper()
    if kind not in ("NODE", "WAY", "RELATION") or not num.strip().isdigit():
        raise ValueError(f"bad --osm {spec!r}; expected NODE|WAY|RELATION:<id>")
    return kind, int(num)


def _resolve_location(shop: str, osm_arg: str | None) -> tuple[str, int]:
    """Resolve a shop's confirmed OSM location, caching it per shop.

    Explicit ``--osm`` wins and is persisted; otherwise a previously-confirmed
    entry in ``shop-osm.json`` is used. Never auto-geocodes (receipt photos may
    be taken away from the shop).
    """
    table: dict[str, Any] = json.loads(SHOP_OSM.read_text(encoding="utf-8")) if SHOP_OSM.exists() else {}
    if osm_arg:
        kind, num = _parse_osm(osm_arg)
        table[shop] = {"osm_type": kind, "osm_id": num}
        SHOP_OSM.parent.mkdir(parents=True, exist_ok=True)
        SHOP_OSM.write_text(json.dumps(table, ensure_ascii=False, indent=2), encoding="utf-8")
        return kind, num
    if shop in table:
        return table[shop]["osm_type"], table[shop]["osm_id"]
    sys.exit(
        f"No confirmed OSM location for {shop!r}. Find it on openstreetmap.org and re-run with "
        f"--osm TYPE:ID (e.g. --osm WAY:1016681733), or --suggest-from-photo PHOTO for a hint."
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
        "--suggest-from-photo", type=Path, default=None, help="Print an OSM suggestion from a photo's GPS, then exit"
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
    parser.add_argument("--env", choices=["org", "net"], default="org")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()

    # --suggest-from-photo: standalone mode — no shop/date/proof needed.
    # (Unreliable: the photo may be taken away from the shop — confirm before use.)
    if args.suggest_from_photo:
        latlon = photo_latlon(args.suggest_from_photo)
        if not latlon:
            sys.exit(f"No GPS in {args.suggest_from_photo}")
        hint = nominatim_reverse(*latlon)
        print(f"Suggestion from {latlon}: {hint}")
        print("Verify it's the right shop, then re-run with --osm TYPE:ID")
        return

    # Validate args required for normal publish flow.
    missing = [f"--{f}" for f, v in [("shop", args.shop), ("date", args.date), ("proof", args.proof)] if v is None]
    if missing:
        parser.error(f"{', '.join(missing)} required for publishing")

    discounts = {ean: (gross, dtype) for ean, gross, dtype in (_parse_discount(s) for s in args.discount)}
    category_specs = [_parse_category_price(s) for s in args.category_price]

    base = BASES[args.env]
    rows = [json.loads(line) for line in args.ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = (
        []
        if args.no_products
        else [r for r in rows if r.get("shop") == args.shop and r.get("date") == args.date and r.get("ean")]
    )
    if not rows and not category_specs:
        sys.exit(f"Nothing to publish for shop={args.shop!r} date={args.date!r} (no EAN rows, no --category-price)")

    # Location must be explicitly confirmed (per shop), never auto-geocoded:
    # receipt photos are often taken away from the shop.
    osm_type, osm_id = _resolve_location(args.shop, args.osm)
    print(f"Location: OSM {osm_type}/{osm_id} for {args.shop!r}")
    print(f"{len(rows)} priced line items; proof={args.proof.name}")

    headers = {"Authorization": f"Bearer {_token()}"} if args.commit else {}
    if args.commit and not _token():
        sys.exit("No token — run op_auth.py first (or set OPENPRICES_TOKEN).")

    # The proof carries the receipt's own currency/date/location; Open Prices
    # reconciles each price to its proof, so a proof missing these blanks out the
    # prices' currency/date in the UI. Stamp them at upload time.
    proof_currency = (rows[0].get("currency") if rows else None) or "EUR"

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
        if r["ean"] in discounts:
            gross, dtype = discounts[r["ean"]]
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
        payload = build_category_price(spec, proof_id=proof_id or 0, osm_type=osm_type, osm_id=osm_id, date=args.date)
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
