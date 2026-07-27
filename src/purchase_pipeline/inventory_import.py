#!/usr/bin/env python3
"""Import reviewed shopping staging items into ``inventory.md``.

Reads a reviewed staging YAML (the flat single-shop schema from
``staging.py`` / ``shop_import.py``) and appends one item line per
``add_to_inventory`` row to ``inventory.md`` via
:mod:`inventory_md.additem`.  This is the Stage-3 *Inventory* step of the
process-shopping skill, scripted: it folds in the quality checks
(duplicate ``ID:``, food-without-``bb:``, category resolution) and removes the
need to hand-edit the markdown item by item.

The script imports ``inventory_md`` directly rather than shelling out to the
``inventory-md add`` CLI.

Usage::

    inventory_import.py STAGING.yaml                       # dry run — show plan
    inventory_import.py STAGING.yaml --commit              # write to inventory.md
    inventory_import.py STAGING.yaml --inventory path/to/inventory.md --commit
    inventory_import.py STAGING.yaml --no-bb-check --commit

Each item is routed by its ``location`` (→ container ID); rows with no
``location`` go to ``--default-container`` (``floating``, per the convention of
keeping location-less items in the ``ID:floating`` section).  Re-running is safe:
rows whose ``inventory_id`` already exists are reported as ``exists`` and skipped
rather than failing on a duplicate ID.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from inventory_md import additem
from inventory_md.parser import parse_inventory

from purchase_pipeline.staging import require_flat

# Units that map to a per-unit mass / volume field rather than a piece count.
_MASS_UNITS = {"kg", "g"}
_VOLUME_UNITS = {"l", "ml", "cl", "dl"}


def _num(value: Any) -> str:
    """Render a number without a redundant trailing ``.0`` (1.0 → '1', 1.768 → '1.768')."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def staging_item_to_kwargs(
    item: dict[str, Any],
    currency: str,
    default_container: str = "floating",
) -> dict[str, Any] | None:
    """Map a reviewed staging item to :func:`inventory_md.additem.add_item` kwargs.

    Returns ``None`` when the item is flagged ``add_to_inventory: false`` (e.g.
    fast-consumed goods, bags) and should be skipped silently.
    """
    if not item.get("add_to_inventory", True):
        return None

    # Best-before: an estimate may be spelled inline (``bb: 2026-09:EST``) or as
    # the separate ``bb_est: true`` key.  Both are honoured and must agree —
    # ``resolve_bb_est`` raises on a contradiction rather than silently picking
    # one, since dropping the estimate marker records a guess as a hard fact.
    explicit_est = item.get("bb_est")
    bb, bb_est = additem.resolve_bb_est(item.get("bb"), explicit_est)

    # ``bb_source`` is free text, so it is only a heuristic: it can promote an
    # unflagged date to an estimate, but never overrides an explicit ``bb_est``.
    if explicit_est is None and not bb_est:
        source = (item.get("bb_source") or "").lower()
        # Whole words: "est" inside "best-before" or "latest" is not the marker.
        if re.search(r"\b(est|estimat\w*|shelf|inferred)\b", source):
            bb_est = True

    # quantity routing by unit
    unit = (item.get("unit") or "pcs").lower()
    qty = item.get("qty")
    mass_total = item.get("mass")  # explicit TOTAL mass, e.g. "543g" / "0.5kg"
    volume_total = item.get("volume")  # explicit TOTAL volume, e.g. "3l"
    out_qty = mass = volume = None
    if qty is not None:
        if unit in _MASS_UNITS:
            mass = f"{_num(qty)}{unit}"
        elif unit in _VOLUME_UNITS:
            volume = f"{_num(qty)}{unit}"
        else:
            out_qty = _num(qty)

    # A piece count combined with an explicit TOTAL mass/volume is written as
    # "<total>/<count>" so the per-piece size stays recoverable — e.g. 3 peppers
    # weighing 543 g → "qty:3 mass:543g/3", 6 cans totalling 3 l → "volume:3l/6".
    # A single piece keeps the bare total.
    def _per_count(total: str) -> str:
        return f"{total}/{out_qty}" if out_qty and out_qty != "1" else total

    if mass is None and mass_total:
        mass = _per_count(mass_total)
    if volume is None and volume_total:
        volume = _per_count(volume_total)

    price = item.get("price")
    price_unit = (item.get("price_unit") or unit).lower()
    price_str = f"{currency}:{_num(price)}/{price_unit}" if price is not None else None

    return {
        "container_id": item.get("location") or default_container,
        "category": item.get("category"),
        "item_id": item.get("inventory_id"),
        "ean": item.get("ean"),
        "isbn": item.get("isbn"),
        "bb": bb,
        "bb_est": bb_est,
        "qty": out_qty,
        "mass": mass,
        "volume": volume,
        "price": price_str,
        "name": item.get("name") or item.get("receipt_name"),
    }


def import_staging(
    staging: dict[str, Any],
    md_path: Path,
    *,
    commit: bool,
    check_bb: bool = True,
    strict: bool = False,
    lang: str | None = None,
    default_container: str = "floating",
    today: date | None = None,
    tingbok_url: str | None = None,
) -> list[tuple[dict[str, Any], str, Any]]:
    """Import all add-to-inventory rows; return one ``(item, action, detail)`` per row.

    ``action`` is one of ``"add"`` (detail is the :class:`additem.AddResult`),
    ``"skip"`` (detail is a reason string) or ``"exists"`` (detail is the
    duplicate ``inventory_id``).  With ``commit=False`` nothing is written.
    """
    require_flat(staging)
    currency = staging.get("currency", "EUR")

    data = parse_inventory(md_path)
    existing = additem.collect_existing_ids(data)

    results: list[tuple[dict[str, Any], str, Any]] = []
    for item in staging.get("items", []):
        kwargs = staging_item_to_kwargs(item, currency, default_container)
        if kwargs is None:
            results.append((item, "skip", "add_to_inventory is false"))
            continue

        item_id = kwargs.get("item_id")
        if item_id and item_id in existing:
            results.append((item, "exists", item_id))
            continue

        res = additem.add_item(
            md_path,
            check_bb=check_bb,
            strict=strict,
            lang=lang,
            today=today,
            dry_run=not commit,
            tingbok_url=tingbok_url,
            **kwargs,
        )
        # Reserve the id so later rows in the same batch see it (matters for the
        # dry-run preview, where the file is not actually updated between rows).
        if res.item_id and not res.errors:
            existing.add(res.item_id)
        results.append((item, "add", res))

    return results


def _print_report(results: list[tuple[dict[str, Any], str, Any]], commit: bool) -> int:
    """Print a per-row report; return process exit code (1 if any row errored)."""
    added = skipped = existed = errored = 0
    for item, action, detail in results:
        label = item.get("name") or item.get("receipt_name") or item.get("category") or "?"
        if action == "skip":
            skipped += 1
            print(f"  · skip   {label} ({detail})")
        elif action == "exists":
            existed += 1
            print(f"  = exists {label} (ID:{detail} already present)")
        else:  # add
            res = detail
            if res.errors:
                errored += 1
                print(f"  ✗ ERROR  {label}: {'; '.join(res.errors)}")
            else:
                added += 1
                for warning in res.warnings:
                    print(f"    ⚠️  {warning}")
                print(f"  + add    {res.item_line}")

    verb = "Added" if commit else "Would add"
    print(f"\n{verb} {added}, skipped {skipped}, already present {existed}, errors {errored}.")
    if not commit:
        print("DRY RUN — pass --commit to write inventory.md")
    return 1 if errored else 0


def main() -> int:  # pragma: no cover - thin CLI wiring
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("staging", type=Path, help="reviewed shopping staging YAML")
    ap.add_argument("--inventory", type=Path, default=Path("inventory.md"), help="inventory.md to edit")
    ap.add_argument("--commit", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--no-bb-check", action="store_true", help="skip the food-without-best-before check")
    ap.add_argument("--strict", action="store_true", help="treat unresolved categories as errors")
    ap.add_argument("--lang", default=None, help="vocabulary language (default: en)")
    ap.add_argument("--default-container", default="floating", help="container for rows without a location")
    args = ap.parse_args()

    if not args.inventory.exists():
        print(f"❌ {args.inventory} not found", file=sys.stderr)
        return 2

    staging = yaml.safe_load(args.staging.read_text(encoding="utf-8"))
    from inventory_md.config import Config

    try:
        results = import_staging(
            staging,
            args.inventory,
            commit=args.commit,
            check_bb=not args.no_bb_check,
            strict=args.strict,
            lang=args.lang,
            default_container=args.default_container,
            tingbok_url=Config().tingbok_url,
        )
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2

    return _print_report(results, args.commit)


if __name__ == "__main__":
    raise SystemExit(main())
