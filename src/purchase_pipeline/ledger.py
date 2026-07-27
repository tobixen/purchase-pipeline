#!/usr/bin/env python3
"""Append-only purchases ledger (``purchases.jsonl``).

One JSON line per receipt line-item, the single source of truth for spending.
New purchases are only ever added, so it never loses data: items consumed and
removed from ``inventory.md`` still have their purchase recorded here. The one
exception to append-only is *enrichment* — an existing raw row may have its
``ean``/``category``/``inventory_id`` filled in later, when its purchase is
reviewed (see :func:`upsert_rows`).

Row schema::

    {"date", "shop", "receipt_name", "ean", "name", "category",
     "qty", "unit", "unit_price", "currency", "total", "inventory_id", "source"}

``inventory_id`` is the join key. Combined with git history of ``inventory.md``
(see :func:`detect_removals`) it answers questions like "what did the food I
consumed in August cost?" — by finding when each ``ID:`` disappeared from the
inventory and joining back to its purchase row.

Importers:

* :func:`lidl_receipt_to_rows`  — raw Lidl receipt JSON (ean/inventory_id unknown)
* :func:`decathlon_purchase_to_rows` — Decathlon purchase JSON (carries the EAN)
* :func:`staging_to_rows` — a reviewed shop_import staging dict (fully populated)

Usage::

    ledger.py import-lidl    [--receipt FILE] [--all] [--ledger purchases.jsonl]
    ledger.py import-decathlon [--file FILE] [--ledger purchases.jsonl]
    ledger.py import-staging  STAGING.yaml   [--ledger purchases.jsonl]
    ledger.py query   [--category food] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--shop X]
    ledger.py consumed --inventory inventory.md [--since ...] [--until ...]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from purchase_pipeline.shop_import import parse_lidl_receipt
from purchase_pipeline.staging import require_flat

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

#: Fields that identify the same purchased line across re-imports. Deliberately
#: excludes the enrichable fields below, so a raw receipt import and the later
#: reviewed staging import for the same line resolve to one row.
_IDENTITY_FIELDS = ("date", "shop", "receipt_name", "qty", "unit_price", "total")

#: Fields filled in later, when a purchase is reviewed (matching/categorising).
#: An incoming non-null value fills or replaces an existing one; nulls never
#: overwrite. This is the controlled exception to append-only behaviour.
_ENRICHABLE_FIELDS = ("ean", "name", "category", "inventory_id")

_ID_RE = re.compile(r"\bID:([A-Za-z0-9_-]+)")


def _row(
    *,
    date: str,
    shop: str,
    receipt_name: str | None,
    qty: float,
    unit: str,
    unit_price: float,
    total_: float,
    currency: str = "EUR",
    ean: str | None = None,
    name: str | None = None,
    category: str | None = None,
    inventory_id: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    return {
        "date": date,
        "shop": shop,
        "receipt_name": receipt_name,
        "ean": ean,
        "name": name,
        "category": category,
        "qty": qty,
        "unit": unit,
        "unit_price": unit_price,
        "currency": currency,
        "total": total_,
        "inventory_id": inventory_id,
        "source": source,
    }


# --------------------------------------------------------------------------- #
# Importers
# --------------------------------------------------------------------------- #
def combine_duplicate_lines(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge receipt lines for the same product into one row with summed qty/total.

    Receipts often print the same product on two separate qty-1 lines instead of
    one qty-2 line. Without this, :func:`upsert_rows` would collapse them by
    identity and silently drop the duplicate (undercount). Lines are merged when
    ``(date, shop, receipt_name, ean, unit_price)`` match; first-seen order is
    preserved.
    """
    combined: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for r in rows:
        key = (r.get("date"), r.get("shop"), r.get("receipt_name"), r.get("ean"), r.get("unit_price"))
        if key in combined:
            combined[key]["qty"] += r.get("qty") or 0
            combined[key]["total"] = round((combined[key].get("total") or 0) + (r.get("total") or 0), 2)
        else:
            combined[key] = dict(r)
            order.append(key)
    return [combined[k] for k in order]


def lidl_receipt_to_rows(
    receipt: dict[str, Any], shop: str = "Lidl Varna", source: str | None = None
) -> list[dict[str, Any]]:
    """Convert a raw Lidl receipt dict to ledger rows (EAN/inventory_id unknown)."""
    staging = parse_lidl_receipt(receipt, shop=shop)
    date = staging["session"]
    if source is None:
        source = f"lidl#{date}"
    rows = [
        _row(
            date=date,
            shop=shop,
            receipt_name=item["receipt_name"],
            qty=item["qty"],
            unit=item["unit"],
            # Net per-unit price on a discounted line, else the plain unit price.
            unit_price=item.get("price_net", item["price"]),
            # line_total is the net (charged) amount — see parse_lidl_receipt.
            total_=item["line_total"],
            source=source,
        )
        for item in staging["items"]
    ]
    return combine_duplicate_lines(rows)


def _normalize_ean(serial: dict[str, Any]) -> str | None:
    """Pick an EAN-13 from a Decathlon serial_number block."""
    code = serial.get("dkt_item_lookup_code")
    if code:
        return code
    gtin = serial.get("gtin")
    if gtin:
        return gtin[-13:]
    return None


def _decathlon_order_to_rows(order: dict[str, Any], source: str | None = None) -> list[dict[str, Any]]:
    """Convert one Decathlon order-*detail* dict (the format written to
    ``decathlon-history.jsonl`` by ``decathlon-history.py``) to ledger rows.

    Products are nested under ``orderProducts[].products[]`` and carry no
    barcode/gtin, so rows have ``ean=None``.
    """
    date = order.get("orderDate", "")[:10]
    shop = f"Decathlon {order.get('orderOrigin', '')}".strip()
    currency = order.get("orderCurrencyCode", "EUR")
    if source is None:
        source = f"decathlon#{order.get('orderTransactionId') or order.get('orderNumber') or date}"
    rows = []
    for parcel in order.get("orderProducts", []):
        for item in parcel.get("products", []):
            qty = item.get("productQuantity", 1)
            unit_price = item.get("productCurrentPrice")
            if unit_price is None:
                unit_price = item.get("productUnitPrice")
            rows.append(
                _row(
                    date=date,
                    shop=shop,
                    receipt_name=item.get("productName"),
                    qty=qty,
                    unit="pcs",
                    unit_price=unit_price,
                    total_=round((unit_price or 0) * qty, 2),
                    currency=currency,
                    name=item.get("productName"),
                    source=source,
                )
            )
    return combine_duplicate_lines(rows)


def load_decathlon_records(path: Path) -> list[dict[str, Any]]:
    """Read a Decathlon history file into a list of order/purchase dicts.

    Accepts JSON Lines (one order per line, as written by
    ``decathlon-history.py``), a single JSON object, or a JSON array.
    """
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def decathlon_purchase_to_rows(purchase: dict[str, Any], source: str | None = None) -> list[dict[str, Any]]:
    """Convert one Decathlon purchase dict to ledger rows.

    Dispatches between the current order-detail format (``orderProducts``, see
    :func:`_decathlon_order_to_rows`) and the legacy
    ``{purchase: {transaction: ...}}`` receipt format.
    """
    if "orderProducts" in purchase:
        return _decathlon_order_to_rows(purchase, source)
    txn = purchase.get("purchase", purchase).get("transaction", {})
    date = txn.get("transaction_date_time_iso", "")[:10]
    shop = f"Decathlon {txn.get('business_unit_name', '')}".strip()
    currency = txn.get("currency", "EUR")
    if source is None:
        source = f"decathlon#{txn.get('transaction_id', date)}"
    rows = []
    for item in txn.get("sale_items", []):
        qty = item.get("quantity", 1)
        rows.append(
            _row(
                date=date,
                shop=shop,
                receipt_name=item.get("product_name"),
                qty=qty,
                unit="pcs",
                unit_price=item.get("unit_price"),
                total_=item.get("amount"),
                currency=currency,
                ean=_normalize_ean(item.get("serial_number", {})),
                name=item.get("product_name"),
                source=source,
            )
        )
    return combine_duplicate_lines(rows)


def staging_to_rows(staging: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a reviewed shop_import staging dict to ledger rows (fully populated).

    Accepts only the canonical flat single-shop schema; raises ``ValueError`` on
    the retired multi-shop ``shops:`` wrapper (see purchase_pipeline/staging.py).
    """
    require_flat(staging)
    date = staging.get("session", "")
    shop = staging.get("shop", "")
    currency = staging.get("currency", "EUR")
    source = staging.get("source") or f"staging#{date}"
    rows = []
    for item in staging.get("items", []):
        rows.append(
            _row(
                date=date,
                shop=shop,
                receipt_name=item.get("receipt_name"),
                qty=item.get("qty"),
                unit=item.get("unit", "pcs"),
                # Net per-unit price on a discounted line (``price_net``), else the
                # plain unit price. The ledger books the net paid price so the Open
                # Prices publisher posts what was actually paid (gross via --discount).
                unit_price=item.get("price_net") if item.get("price_net") is not None else item.get("price"),
                # line_total is the net (charged) amount and is authoritative; price*qty
                # is only a fallback and re-derives from rounded inputs — wrong by a cent
                # on weighed goods. See purchase_pipeline/staging.py for the field semantics.
                total_=item.get("line_total", round(item.get("price", 0) * item.get("qty", 0), 2)),
                currency=currency,
                ean=item.get("ean"),
                name=item.get("name"),
                category=item.get("category"),
                inventory_id=item.get("inventory_id"),
                source=source,
            )
        )
    # Same as the Lidl/Decathlon importers: a receipt that prints the same product
    # on two qty-1 lines must merge to one qty-2 row, else upsert_rows collapses
    # them by identity and silently drops one (undercount — bit us on the Бурлекс
    # 2026-07-08 trip: two 4.09 Lurpak butters booked as one, 4.09 short).
    return combine_duplicate_lines(rows)


# --------------------------------------------------------------------------- #
# Storage (append-or-enrich)
# --------------------------------------------------------------------------- #
def row_identity(row: dict[str, Any]) -> tuple[Any, ...]:
    """Identity of a purchased line, ignoring enrichable fields.

    Two byte-identical lines on the *same* receipt collapse to one (rare for
    Lidl, which aggregates identical items into a quantity).
    """
    return tuple(row.get(f) for f in _IDENTITY_FIELDS)


def load_rows(path: Path) -> list[dict[str, Any]]:
    """Read all ledger rows from a JSONL file (empty list if absent)."""
    if not Path(path).exists():
        return []
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def upsert_rows(path: Path, rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Add new rows and enrich existing ones in place. Returns ``(added, enriched)``.

    A row is matched to an existing one by :func:`row_identity`. On a match, the
    incoming non-null :data:`_ENRICHABLE_FIELDS` (``ean``/``name``/``category``/
    ``inventory_id``) fill or replace the stored values; nulls never overwrite.
    Unmatched rows are appended. Re-importing the same raw receipt is therefore a
    no-op, while importing the reviewed staging file fills in the gaps.

    This rewrites the file — the deliberate relaxation of strict append-only that
    lets a later review enrich an earlier raw import.
    """
    path = Path(path)
    existing = load_rows(path)
    index = {row_identity(r): r for r in existing}
    added = enriched = 0
    for incoming in rows:
        target = index.get(row_identity(incoming))
        if target is None:
            existing.append(incoming)
            index[row_identity(incoming)] = incoming
            added += 1
            continue
        changed = False
        for field in _ENRICHABLE_FIELDS:
            value = incoming.get(field)
            if value is not None and target.get(field) != value:
                target[field] = value
                changed = True
        enriched += changed
    if added or enriched:
        _write_rows(path, existing)
    return added, enriched


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def filter_rows(
    rows: list[dict[str, Any]],
    category: str | None = None,
    since: str | None = None,
    until: str | None = None,
    shop: str | None = None,
) -> list[dict[str, Any]]:
    """Filter rows by category prefix, ISO date range (inclusive), and shop."""
    out = []
    for r in rows:
        if category is not None:
            rc = r.get("category") or ""
            if not (rc == category or rc.startswith(category + "/")):
                continue
        date = r.get("date") or ""
        if since is not None and date < since:
            continue
        if until is not None and date > until:
            continue
        if shop is not None and r.get("shop") != shop:
            continue
        out.append(r)
    return out


def total(rows: list[dict[str, Any]]) -> float:
    """Sum of ``total`` across rows, rounded to cents."""
    return round(sum(r.get("total") or 0 for r in rows), 2)


# --------------------------------------------------------------------------- #
# Consumption (git history of inventory.md)
# --------------------------------------------------------------------------- #
def extract_ids(text: str) -> set[str]:
    """Extract every ``ID:token`` from inventory markdown text."""
    return set(_ID_RE.findall(text))


def detect_removals(revisions: list[tuple[str, set[str]]]) -> dict[str, str]:
    """Map inventory_id -> date it disappeared, given revisions oldest-to-newest.

    *revisions* is ``[(commit_date, ids_present), ...]``. An ID is "removed" at
    the first revision where it is absent after having been present, **and** it
    is still absent in the final revision (re-added items don't count).
    """
    if not revisions:
        return {}
    removals: dict[str, str] = {}
    prev_ids = revisions[0][1]
    for date, ids in revisions[1:]:
        for gone in prev_ids - ids:
            removals.setdefault(gone, date)
        for back in ids:  # re-added: cancel any earlier removal
            removals.pop(back, None)
        prev_ids = ids
    return removals


def consumed_rows(
    rows: list[dict[str, Any]],
    removals: dict[str, str],
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Ledger rows whose item was removed from inventory within the date range.

    Each returned row gains a ``consumed_date`` field (the removal commit date).
    """
    out = []
    for r in rows:
        inv_id = r.get("inventory_id")
        if inv_id is None or inv_id not in removals:
            continue
        cdate = removals[inv_id]
        if since is not None and cdate < since:
            continue
        if until is not None and cdate > until:
            continue
        out.append({**r, "consumed_date": cdate})
    return out


def file_revisions(repo: Path, relpath: str) -> list[tuple[str, set[str]]]:  # pragma: no cover - git I/O
    """Return ``[(commit_date, ids_present), ...]`` for *relpath*, oldest first."""
    repo = Path(repo)
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--reverse", "--format=%H\t%cd", "--date=short", "--", relpath],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    revisions = []
    for line in log.splitlines():
        commit, date = line.split("\t", 1)
        content = subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:{relpath}"],
            capture_output=True,
            text=True,
        ).stdout
        revisions.append((date, extract_ids(content)))
    return revisions


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> None:  # pragma: no cover - thin CLI wiring
    default_ledger = Path.home() / "regnskap" / "purchases.jsonl"
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_lidl = sub.add_parser("import-lidl")
    p_lidl.add_argument("--receipt", type=Path, default=Path.home() / "regnskap" / "lidl_receipts.json")
    p_lidl.add_argument("--all", action="store_true", help="Import every receipt, not just the latest")
    p_lidl.add_argument("--shop", default="Lidl Varna")
    p_lidl.add_argument("--ledger", type=Path, default=default_ledger)

    p_dec = sub.add_parser("import-decathlon")
    p_dec.add_argument("--file", type=Path, default=Path.home() / "regnskap" / "decathlon.json")
    p_dec.add_argument("--ledger", type=Path, default=default_ledger)

    p_stage = sub.add_parser("import-staging")
    p_stage.add_argument("staging", type=Path)
    p_stage.add_argument("--ledger", type=Path, default=default_ledger)

    p_q = sub.add_parser("query")
    p_q.add_argument("--category")
    p_q.add_argument("--since")
    p_q.add_argument("--until")
    p_q.add_argument("--shop")
    p_q.add_argument("--ledger", type=Path, default=default_ledger)

    p_c = sub.add_parser("consumed")
    p_c.add_argument("--inventory", type=Path, required=True, help="Path to inventory.md")
    p_c.add_argument("--since")
    p_c.add_argument("--until")
    p_c.add_argument("--category")
    p_c.add_argument("--ledger", type=Path, default=default_ledger)

    args = parser.parse_args()

    def _report(rows: list[dict[str, Any]]) -> None:
        added, enriched = upsert_rows(args.ledger, rows)
        print(f"{args.ledger}: {added} added, {enriched} enriched (of {len(rows)} rows)")

    if args.cmd == "import-lidl":
        data = json.loads(args.receipt.read_text(encoding="utf-8"))
        receipts = data if (args.all and isinstance(data, list)) else [data[-1] if isinstance(data, list) else data]
        _report([r for rec in receipts for r in lidl_receipt_to_rows(rec, shop=args.shop)])
    elif args.cmd == "import-decathlon":
        purchases = load_decathlon_records(args.file)
        _report([r for p in purchases for r in decathlon_purchase_to_rows(p)])
    elif args.cmd == "import-staging":
        if yaml is None:
            sys.exit("pyyaml required for import-staging")
        staging = yaml.safe_load(args.staging.read_text(encoding="utf-8"))
        try:
            rows = staging_to_rows(staging)
        except ValueError as exc:
            sys.exit(f"import-staging: {exc}")
        if not rows:
            sys.exit(f"import-staging: 0 rows parsed from {args.staging} — nothing imported (check the staging file)")
        _report(rows)
    elif args.cmd == "query":
        rows = filter_rows(
            load_rows(args.ledger), category=args.category, since=args.since, until=args.until, shop=args.shop
        )
        for r in rows:
            print(
                f"{r['date']}  {r['total']:>7.2f} {r['currency']}  {r.get('category') or '-':<22}  "
                f"{r.get('name') or r.get('receipt_name')}"
            )
        cur = rows[0]["currency"] if rows else ""
        print(f"\n{len(rows)} rows, total {total(rows):.2f} {cur}")
    elif args.cmd == "consumed":
        repo = args.inventory.resolve().parent
        revisions = file_revisions(repo, args.inventory.name)
        removals = detect_removals(revisions)
        rows = consumed_rows(load_rows(args.ledger), removals, since=args.since, until=args.until)
        if args.category:
            rows = filter_rows(rows, category=args.category)
        for r in rows:
            print(
                f"consumed {r['consumed_date']}  bought {r['date']}  {r['total']:>7.2f} {r['currency']}  "
                f"{r.get('name') or r.get('receipt_name')}"
            )
        cur = rows[0]["currency"] if rows else ""
        print(f"\n{len(rows)} items consumed, purchase cost {total(rows):.2f} {cur}")


if __name__ == "__main__":
    main()
