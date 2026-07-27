#!/usr/bin/env python3
"""Push price + receipt-name observations from a shopping staging file to tingbok.

`inventory-md parse` already upserts product *names/vocabulary* to tingbok, but it
has no notion of **shop** or **date**, so it cannot record the price/receipt-name
observations that a shopping trip produces. This script fills that gap: it reads a
reviewed staging YAML (the same file the ledger/inventory steps consume) and, for
every item flagged ``to_tingbok: true`` with an ``ean``, PUTs a merge containing:

  * one ``prices`` row     (date, shop, price, currency, unit)
  * one ``receipt_names`` row (the as-printed name, shop, first/last seen = date)

tingbok's PUT is a merge: prices and receipt_names are appended, so re-running is
safe (tingbok de-dupes identical observations).

Optional per-item overrides — useful when tingbok has no entry yet, or the name
``inventory-md`` derived is poor (e.g. carries a location note):

    tingbok_name: Балканско светло пиво
    tingbok_categories: [beverages, alcoholic beverages, beers]
    tingbok_quantity: 500ml

These are sent verbatim (overriding the current tingbok name) whenever present.
With ``--fill-missing`` the item's plain ``name`` is used as the tingbok name
*only* for EANs tingbok doesn't know yet (it never overwrites an existing name).

The staging file is the canonical flat single-shop schema (one file per shop
visit, top-level ``shop`` + ``items``); the retired multi-shop ``shops:`` list
is rejected (see purchase_pipeline/staging.py).

This script is *not* coupled to the rest of the shopping pipeline — it only reads
a staging YAML. To push a single found item (no receipt, no price) without running
the whole pipeline, hand-write a minimal staging file and run it directly::

    session: 2026-06-15
    shop: ""
    currency: EUR
    items:
      - ean: "3800214924577"
        to_tingbok: true
        tingbok_name: ...
        tingbok_categories: [meats, dry sausages]
        tingbok_quantity: 550g

Items without a ``receipt_name`` push no ``receipt_names`` row, and items without
a ``price`` push no ``prices`` row, so an ad-hoc push carries only name/category
/quantity data.

Usage:
    tingbok_push.py STAGING.yaml              # dry run — show what would be sent
    tingbok_push.py STAGING.yaml --commit     # actually PUT
    tingbok_push.py STAGING.yaml --fill-missing --commit
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import niquests as requests
except ImportError:
    import requests

from purchase_pipeline.staging import require_flat

DEFAULT_TINGBOK = "https://tingbok.plann.no"


def chain_slug(shop: str) -> str | None:
    """Derive a chain slug from a free-text shop name (its first token).

    ``"Lidl Varna бул. ..."`` → ``"lidl"``; ``"Mercadona"`` → ``"mercadona"``.
    Returns ``None`` when the first token has no ASCII-alphanumeric characters
    (a Cyrillic-only or empty name), so a non-Latin prefix is never built.
    """
    tokens = shop.strip().split()
    if not tokens:
        return None
    slug = re.sub(r"[^a-z0-9]", "", tokens[0].casefold())
    return slug or None


def is_local_instore_code(code: str) -> bool:
    """True for short in-store / GS1 restricted-distribution article numbers.

    These codes — Lidl's 8-digit ``2x`` PLUs, Mercadona's ``0x`` EAN-8 range,
    7-digit shop article numbers — are not globally unique, so tingbok stores
    them under a ``<chain>-<code>`` key. Genuine global EAN-8/12/13 codes (and
    13-digit ``2x`` random-weight barcodes) are left bare.
    """
    if not code.isdigit():
        return False
    if len(code) == 7:
        return True
    return len(code) == 8 and code[0] in ("0", "2")


def canonical_ean(ean: str, shop: str) -> str:
    """Return the tingbok key for *ean*, shop-prefixing bare local codes.

    A local in-store code is prefixed with the shop's chain slug
    (``20004132`` @ ``"Lidl Varna"`` → ``"lidl-20004132"``) so it does not
    collide with other chains reusing the same number. Already-prefixed codes
    (containing a hyphen) and global EANs are returned unchanged.
    """
    if "-" in ean:
        return ean
    slug = chain_slug(shop)
    if slug and is_local_instore_code(ean):
        return f"{slug}-{ean}"
    return ean


def _get(base: str, ean: str) -> dict[str, Any]:
    try:
        r = requests.get(f"{base}/api/ean/{ean}", timeout=15)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()
    except Exception as e:  # noqa: BLE001 — network best-effort
        return {"_error": str(e)}


def _put(base: str, ean: str, payload: dict[str, Any]) -> tuple[bool, str]:
    try:
        r = requests.put(
            f"{base}/api/ean/{ean}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        return True, str(r.status_code)
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _shops(staging: dict[str, Any]) -> list[dict[str, Any]]:
    """Wrap the flat single-shop staging in a one-element {shop, currency, date, items} list.

    Raises ``ValueError`` on the retired multi-shop ``shops:`` schema (see
    purchase_pipeline/staging.py).
    """
    require_flat(staging)
    session = staging.get("session", "")
    # PyYAML parses a bare ``session: 2024-11-18`` as a datetime.date, which is
    # not JSON-serialisable when embedded in the price/receipt-name payload.
    # Coerce to an ISO string (a quoted/string session passes through unchanged).
    if isinstance(session, datetime.date):  # also covers datetime.datetime
        session = session.isoformat()
    return [
        {
            "shop": staging.get("shop", ""),
            "currency": staging.get("currency", "EUR"),
            "date": session,
            "items": staging.get("items", []),
        }
    ]


def build_payload(
    item: dict[str, Any],
    shop: str,
    date: str,
    currency: str,
    current: dict[str, Any],
    fill_missing: bool,
) -> dict[str, Any]:
    price = item.get("price")
    payload: dict[str, Any] = {}
    # A receipt-name observation only makes sense when there is a receipt name;
    # an ad-hoc found item (no receipt) must not push a null-named row.
    receipt_name = item.get("receipt_name")
    if receipt_name:
        payload["receipt_names"] = [
            {
                "name": receipt_name,
                "shop": shop,
                "first_seen": date,
                "last_seen": date,
            }
        ]
    if price is not None:
        payload["prices"] = [
            {
                "date": date,
                "shop": shop,
                "price": price,
                "currency": currency,
                "unit": item.get("unit", "pcs"),
            }
        ]
    # Explicit overrides always win.
    if item.get("tingbok_name"):
        payload["name"] = item["tingbok_name"]
    if item.get("tingbok_categories"):
        payload["categories"] = item["tingbok_categories"]
    if item.get("tingbok_quantity"):
        payload["quantity"] = item["tingbok_quantity"]
    # Fill name from plain `name` only when tingbok has none yet.
    if fill_missing and "name" not in payload and not current.get("name") and item.get("name"):
        payload["name"] = item["name"]
    # tingbok requires at least one of name/categories on every PUT. If we have
    # neither yet, preserve what tingbok already holds (echo it back, so the
    # price/receipt-name merge doesn't wipe it); fall back to the staging item.
    if "name" not in payload and "categories" not in payload:
        if current.get("name"):
            payload["name"] = current["name"]
        elif current.get("categories"):
            payload["categories"] = current["categories"]
        elif item.get("name"):
            payload["name"] = item["name"]
        elif item.get("category"):
            payload["categories"] = [item["category"]]
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("staging", type=Path, help="reviewed shopping staging YAML")
    ap.add_argument("--commit", action="store_true", help="actually PUT (default: dry run)")
    ap.add_argument(
        "--fill-missing",
        action="store_true",
        help="set tingbok name from item `name` for EANs tingbok doesn't know yet",
    )
    ap.add_argument("--tingbok-url", default=DEFAULT_TINGBOK)
    args = ap.parse_args()

    try:
        import yaml
    except ImportError:
        sys.exit("pyyaml required")

    staging = yaml.safe_load(args.staging.read_text(encoding="utf-8"))
    base = args.tingbok_url.rstrip("/")

    try:
        shop_blocks = _shops(staging)
    except ValueError as exc:
        sys.exit(f"tingbok_push: {exc}")

    pushed = skipped = failed = candidates = 0
    for shop_block in shop_blocks:
        shop, currency, date = shop_block["shop"], shop_block["currency"], shop_block["date"]
        for item in shop_block["items"]:
            ean = item.get("ean")
            if not item.get("to_tingbok") or not ean:
                skipped += 1
                continue
            ean = canonical_ean(str(ean), shop)
            current = _get(base, ean)
            payload = build_payload(item, shop, date, currency, current, args.fill_missing)
            extra = [k for k in ("name", "categories", "quantity") if k in payload]
            cur_name = current.get("name") or "(empty)"
            note = f"  +{','.join(extra)}" if extra else ""
            print(f"{ean:<14} {cur_name[:48]:<48} ← {item.get('receipt_name')} @ {item.get('price')}{note}")
            candidates += 1
            if not args.commit:
                continue
            ok, msg = _put(base, ean, payload)
            if ok:
                pushed += 1
            else:
                failed += 1
                print(f"               PUT FAILED: {msg}")

    verb = "pushed" if args.commit else "would push"
    print(
        f"\n{verb}: {pushed if args.commit else candidates}  failed: {failed}  skipped (no ean / not to_tingbok): {skipped}"
    )
    if candidates == 0 and skipped > 0 and failed == 0:
        print("WARNING: no items to push — did you set to_tingbok: true in the staging file?", file=sys.stderr)
    if not args.commit:
        print("DRY RUN — pass --commit to push")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
