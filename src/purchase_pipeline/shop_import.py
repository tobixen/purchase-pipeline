#!/usr/bin/env python3
"""Build a human-correctable staging file from a shopping receipt + barcode scan.

This is the deterministic first stage of the shopping pipeline. It does only
mechanical work:

* parse a shop receipt (currently Lidl JSON) into one staging row per line item;
* classify barcode-extraction output photos as barcode / expiry / label;
* gather candidate EANs per receipt line via tingbok's reverse receipt-name
  lookup (``GET /api/ean/search``);
* fill ``ean``/``bb`` on the lines where two independent sources agree, and
  flag every photo and line where they do not
  (:mod:`purchase_pipeline.photo_match`).

It still decides nothing on its own evidence: an EAN scanned but never before
seen under this till string, a conflicting barcode read, a best-before nobody
can pair to a line — all of those arrive as review work with a reason attached,
not as a filled-in field. Where a field *is* filled, ``ean_source`` /
``bb_source`` record what filled it. The staging file remains the correction
checkpoint before any irreversible action (tingbok PUT, inventory edit, commit).

The receipts file holds many trips; by default the **newest by purchase date**
is imported. It is not the last array element: ``lidl_receipts.json`` is sorted
by receipt id, and the id is not chronological. Use ``--receipt-id`` or
``--date`` to pick a specific trip; an ambiguous selector (two visits on one
day) fails and lists the candidates rather than guessing.

Usage:
    shop_import.py --receipt ~/regnskap/lidl_receipts.json \\
        --barcodes-json barcodes.json --out staging/shopping-2026-05-28.yaml

    # a specific trip rather than the newest:
    shop_import.py --receipt ~/regnskap/lidl_receipts.json --date 2026-07-17 ...
    shop_import.py --receipt ~/regnskap/lidl_receipts.json --receipt-id 0300019290... ...

    # produce barcodes.json first with:
    #   extract_barcodes.py --json PHOTOS... > barcodes.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from purchase_pipeline.photo_match import (
    REVIEW_KINDS,
    associate_photos,
    build_loose_photos,
    fill_eans_from_candidates,
)

try:
    import niquests as requests
except ImportError:  # pragma: no cover - fallback
    try:
        import requests
    except ImportError:  # pragma: no cover
        requests = None  # type: ignore[assignment]

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

DEFAULT_TINGBOK_URL = "https://tingbok.plann.no"

#: The Lidl receipts file, produced by ``lidl-history --fetch`` in
#: `order-scrapers <https://github.com/tobixen/order-scrapers>`_ — see the README.
DEFAULT_RECEIPTS = Path.home() / "regnskap" / "lidl_receipts.json"

#: Receipt name suffix Lidl prints for weighed (per-kilogram) goods.
_KG_SUFFIX = "НА КГ"

#: Map a parser discount ``type`` to an Open Prices ``discount_type`` enum value
#: (QUANTITY/SALE/SEASONAL/LOYALTY_PROGRAM/EXPIRES_SOON/PICK_IT_YOURSELF/…).
#: A Lidl Plus coupon is a loyalty-programme discount; an in-store markdown
#: sticker is a short-shelf-life reduction. Anything unknown falls back to SALE.
_OPENPRICES_DISCOUNT_TYPE = {
    "lidlplus_coupon": "LOYALTY_PROGRAM",
    "markdown": "EXPIRES_SOON",
}

Searcher = Callable[..., list[dict[str, Any]]]


def openprices_discount_type(discount_type: str | None) -> str:
    """Open Prices ``discount_type`` for a parser discount ``type`` (default SALE)."""
    return _OPENPRICES_DISCOUNT_TYPE.get(discount_type or "", "SALE")


def parse_price(value: str | float) -> float:
    """Parse a receipt price/quantity, accepting comma or dot decimals."""
    if isinstance(value, int | float):
        return float(value)
    return float(str(value).strip().replace(",", "."))


def _iso_date(value: str) -> str:
    """Normalise a Lidl ``YYYY.MM.DD`` purchase date to ``YYYY-MM-DD``."""
    return value.strip().replace(".", "-")


def receipt_date(receipt: dict[str, Any]) -> str:
    """ISO purchase date of a receipt (``''`` when it carries none).

    Accepts the Lidl ``purchase_date`` (``2026.07.21``) and the generic ``date``
    key used by hand-transcribed receipts.
    """
    return _iso_date(str(receipt.get("purchase_date") or receipt.get("date") or ""))


def _describe(receipt: dict[str, Any]) -> str:
    """One-line receipt summary for disambiguation messages."""
    return f"{receipt.get('id', '?')} ({receipt_date(receipt) or 'no date'}, {len(receipt.get('items', []))} items)"


def select_receipt(
    receipts: list[dict[str, Any]] | dict[str, Any],
    *,
    receipt_id: str | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """Pick one receipt out of a receipts file.

    Selection is by ``receipt_id`` and/or ``date``; with neither, the **newest
    by purchase date** wins.  Never by array position: ``lidl_receipts.json`` is
    sorted by receipt *id*, and the id is not chronological — the 2026-07-21
    trip sorts *before* the 2026-07-17 one, so "the last entry" silently handed
    back the older trip.

    A selector matching several receipts (two visits in one day is normal)
    raises :class:`ValueError` listing them, rather than guessing.
    """
    if isinstance(receipts, dict):
        return receipts
    if not receipts:
        raise ValueError("receipt file contains no receipts")

    candidates = receipts
    if receipt_id is not None:
        candidates = [r for r in candidates if str(r.get("id")) == receipt_id]
    if date is not None:
        want = _iso_date(date)
        candidates = [r for r in candidates if receipt_date(r) == want]

    if not candidates:
        wanted = ", ".join(filter(None, [f"id={receipt_id}" if receipt_id else "", f"date={date}" if date else ""]))
        known = ", ".join(sorted({receipt_date(r) for r in receipts if receipt_date(r)})[-8:])
        raise ValueError(f"no receipt matches {wanted}. Most recent dates in the file: {known}")

    if receipt_id is None and date is None:
        newest = max(receipt_date(r) for r in candidates)
        candidates = [r for r in candidates if receipt_date(r) == newest]

    if len(candidates) > 1:
        listing = "; ".join(_describe(r) for r in candidates)
        raise ValueError(
            f"{len(candidates)} receipts match — refusing to guess: {listing}. Pass --receipt-id to pick one."
        )
    return candidates[0]


def _new_item_row(receipt_name: str, price: float, qty: float, unit: str) -> dict[str, Any]:
    """A staging row with the review scaffold fields defaulted to unfilled.

    ``price`` is the unit price in ``unit`` (per-kg for weighed lines); ``line_total``
    is the receipt's printed line amount and is authoritative downstream. See
    purchase_pipeline/staging.py for the full field semantics.
    """
    return {
        "receipt_name": receipt_name,
        "price": price,
        "qty": qty,
        "unit": unit,
        "line_total": round(price * qty, 2),
        # ---- filled during review (AI/human), or by photo_match where the
        #      evidence settles it — ``ean_source``/``bb_source`` say which ----
        "ean": None,
        "ean_source": None,
        "ean_candidates": [],
        "name": None,
        "category": None,
        "bb": None,
        "bb_source": None,
        "location": None,
        "inventory_id": None,
        "add_to_inventory": True,
        "to_tingbok": None,
        "photos": [],
        "needs_review": True,
        "notes": "",
    }


def _staging_discounts(raw_discounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise a receipt line's discount list for the staging file.

    Keeps the human-meaningful fields (amount, kind, label) and adds the mapped
    Open Prices ``discount_type``; drops the internal ``promotion_id``. A line can
    carry several discounts of different kinds (a Lidl Plus coupon *and* a
    short-expiry markdown on the same line), so this is always a list.
    """
    out: list[dict[str, Any]] = []
    for d in raw_discounts:
        entry: dict[str, Any] = {
            "amount": parse_price(d["amount"]),
            "type": d.get("type"),
            "openprices_type": openprices_discount_type(d.get("type")),
            "label": d.get("label"),
        }
        if d.get("percent") is not None:
            entry["percent"] = d["percent"]
        out.append(entry)
    return out


def _apply_line_money(row: dict[str, Any], raw: dict[str, Any], price: float, qty: float, gross_total: float) -> None:
    """Fill a staging row's money fields, honouring any per-line discount.

    ``gross_total`` is the pre-discount line amount (the receipt's printed
    ``line_total`` for Lidl, or ``price * qty`` otherwise). When the receipt
    records a ``net_total`` below it, the row is booked at the **net** amount and
    the gross/discount are surfaced alongside; ``price_net`` is the net per-unit
    price the Open Prices publisher should post. Undiscounted lines get *no*
    discount fields at all, so hand-transcribed receipts stay clean.
    """
    net_total = parse_price(raw["net_total"]) if raw.get("net_total") is not None else gross_total
    discounts = _staging_discounts(raw.get("discounts") or [])
    row["line_total"] = net_total  # net = what was actually charged; authoritative
    if not discounts and round(gross_total - net_total, 2) == 0:
        return
    row["line_total_gross"] = gross_total
    row["line_discount"] = round(gross_total - net_total, 2)
    if qty:
        row["price_net"] = round(net_total / qty, 2)
    if discounts:
        row["discounts"] = discounts


def parse_lidl_receipt(
    receipt: dict[str, Any], shop: str = "Lidl Varna", source: str = "lidl_receipts.json"
) -> dict[str, Any]:
    """Parse a receipt dict into a staging structure (no candidates).

    Accepts both the Lidl JSON schema (``purchase_date``/``total_price``/
    ``total_price_gross``, item ``price`` = unit price, optional per-line
    ``net_total``/``discounts``) and a hand-transcribed receipt (``date``/``shop``/
    ``currency``/``total``, item rows with explicit ``unit``, ``unit_price`` and
    ``price`` = the printed line total).  Header keys present in the receipt win
    over the ``shop``/``source`` arguments.

    Money is booked **net** (what the card was charged). ``receipt_total`` is the
    net total; ``receipt_total_gross`` and ``receipt_discount_total`` surface the
    pre-discount figure and the saving. Per-item, ``line_total`` is net;
    discounted lines additionally carry ``line_total_gross``, ``line_discount``,
    ``price_net`` and a ``discounts`` list. Receipts with no discount data (the
    old Lidl schema, hand-transcribed trips) book gross == net and gain no
    discount fields.
    """
    items: list[dict[str, Any]] = []
    for raw in receipt.get("items", []):
        name = raw["name"]
        qty = parse_price(raw.get("quantity", "1"))
        if "unit_price" in raw:
            price = parse_price(raw["unit_price"])
            gross_total = parse_price(raw["price"])
        else:
            price = parse_price(raw["price"])
            # Lidl prints the gross line amount as ``line_total``; fall back to
            # price*qty for the old schema that lacked it.
            gross_total = parse_price(raw["line_total"]) if raw.get("line_total") is not None else round(price * qty, 2)
        unit = raw.get("unit") or ("kg" if name.upper().rstrip().endswith(_KG_SUFFIX) else "pcs")
        row = _new_item_row(name, price, qty, unit)
        _apply_line_money(row, raw, price, qty, gross_total)
        items.append(row)

    gross = parse_price(
        receipt.get("total_price_gross", receipt.get("total_price_no_saving", receipt.get("total", "0")))
    )
    net = parse_price(receipt["total_price"]) if receipt.get("total_price") is not None else gross
    if receipt.get("discount_total") is not None:
        discount_total = parse_price(receipt["discount_total"])
    else:
        discount_total = round(gross - net, 2)

    return {
        "session": receipt_date(receipt),
        "shop": receipt.get("shop") or shop,
        "store": receipt.get("store"),
        "currency": receipt.get("currency", "EUR"),
        "receipt_total": net,
        "receipt_total_gross": gross,
        "receipt_discount_total": discount_total,
        # A receipt fetched by ``lidl-history`` carries its own provenance stamp,
        # which is more specific than the name of the file it sits in.
        "source": receipt.get("source") or source,
        "items": items,
        "loose_photos": [],
    }


def _tingbok_searcher(base_url: str = DEFAULT_TINGBOK_URL) -> Searcher:
    """Return a searcher that queries tingbok's reverse receipt-name endpoint."""

    def search(receipt_name: str, shop: str | None = None) -> list[dict[str, Any]]:
        if requests is None:  # pragma: no cover
            return []
        try:
            params = {"receipt_name": receipt_name}
            if shop:
                params["shop"] = shop
            resp = requests.get(f"{base_url}/api/ean/search", params=params, timeout=15)
            resp.raise_for_status()
            return resp.json().get("results", [])
        except Exception as exc:  # pragma: no cover - network best-effort
            print(f"Warning: tingbok search failed for {receipt_name!r}: {exc}", file=sys.stderr)
            return []

    return search


def build_staging(
    receipt: dict[str, Any],
    *,
    shop: str = "Lidl Varna",
    source: str = "lidl_receipts.json",
    searcher: Searcher,
    barcode_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the full staging structure: parsed rows + candidates + photos.

    Candidates are gathered first, then used as the corroborating half of photo
    association, then as the fallback for a repeat purchase with no photo. So a
    line arrives with its ``ean``/``bb`` already filled whenever the evidence
    settles it, and with ``ean_source`` saying which evidence — see
    :mod:`purchase_pipeline.photo_match`.
    """
    staging = parse_lidl_receipt(receipt, shop=shop, source=source)
    photos = build_loose_photos(barcode_results or [])

    for item in staging["items"]:
        # No shop filter: receipt-name observations are often recorded with no
        # shop, and the same product seen at another shop is still a valid
        # candidate. The reviewer picks; recall matters more than precision here.
        matches = searcher(item["receipt_name"])
        item["ean_candidates"] = [
            {
                "ean": m["ean"],
                "name": m.get("name"),
                "source": "tingbok_receipt_name",
                "score": m.get("score"),
            }
            for m in matches
        ]

    staging["loose_photos"] = associate_photos(staging["items"], photos)
    fill_eans_from_candidates(staging["items"])
    return staging


def dump_yaml(staging: dict[str, Any]) -> str:
    """Serialise the staging structure to human-editable YAML."""
    if yaml is None:  # pragma: no cover
        return json.dumps(staging, ensure_ascii=False, indent=2)
    return yaml.safe_dump(staging, allow_unicode=True, sort_keys=False, width=100)


def main() -> None:  # pragma: no cover - thin CLI wiring
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--receipt",
        type=Path,
        default=DEFAULT_RECEIPTS,
        help="Path to lidl_receipts.json (newest receipt by purchase date is used)",
    )
    parser.add_argument("--receipt-id", default=None, help="Import the receipt with this id instead of the newest")
    parser.add_argument("--date", default=None, help="Import the receipt purchased on this date (YYYY-MM-DD)")
    parser.add_argument("--barcodes-json", type=Path, default=None, help="JSON output from extract_barcodes.py --json")
    parser.add_argument("--shop", default="Lidl Varna", help="Shop name recorded in the staging file")
    parser.add_argument("--tingbok-url", default=DEFAULT_TINGBOK_URL)
    parser.add_argument("--no-tingbok", action="store_true", help="Skip tingbok candidate lookup")
    parser.add_argument("--out", type=Path, default=None, help="Staging file to write (default: stdout)")
    args = parser.parse_args()

    receipts = json.loads(args.receipt.read_text(encoding="utf-8"))
    try:
        receipt = select_receipt(receipts, receipt_id=args.receipt_id, date=args.date)
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    barcode_results: list[dict[str, Any]] = []
    if args.barcodes_json:
        barcode_results = json.loads(args.barcodes_json.read_text(encoding="utf-8"))

    searcher: Searcher = (lambda name, shop=None: []) if args.no_tingbok else _tingbok_searcher(args.tingbok_url)
    staging = build_staging(
        receipt, shop=args.shop, source=args.receipt.name, searcher=searcher, barcode_results=barcode_results
    )

    text = dump_yaml(staging)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        items = staging["items"]
        filled = sum(1 for i in items if i["ean"])
        with_bb = sum(1 for i in items if i["bb"])
        review = [p for p in staging["loose_photos"] if p.get("review") or p["kind"] in REVIEW_KINDS]
        print(
            f"Wrote {args.out} — {len(items)} items, {filled} with an ean ({with_bb} with a bb); "
            f"{len(items) - filled} still to match by hand."
        )
        if review:
            print(f"{len(review)} photo(s) need a look — see loose_photos[].review:")
            for photo in review:
                print(f"  · {photo['file']} [{photo['kind']}] {photo.get('review', '')}")
    else:
        print(text)


if __name__ == "__main__":
    main()
