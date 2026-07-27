"""Canonical staging-file schema, shared by every consumer.

A reviewed shopping staging file is **flat single-shop**: one file per shop
visit, with top-level ``session`` (date), ``shop``, ``currency`` and ``items``.
This is what ``shop_import.py`` emits and what ``ledger.py`` / ``tingbok_push.py``
consume.

An earlier design wrapped several shops in a top-level ``shops:`` list. That is
retired — a shopping trip spanning two shops is two independent staging files.
Feeding the old layout to a flat consumer used to import 0 rows *silently*
(docs/shopping-pipeline-issues-2026-06-07.md, issue 1), so consumers now reject
it loudly via :func:`require_flat`.

Per-item money/quantity fields (each entry of ``items:``):

* ``price``      — the **unit price in the line's ``unit``**. For ``pcs`` it is
  the price of one piece; for a weighed line (``unit: kg``) it is the per-kg
  price, *not* the price paid for the line.
* ``qty``        — quantity in the same ``unit`` (e.g. ``1.768`` for 1.768 kg).
* ``line_total`` — the **net** amount actually charged for the line, as printed
  on the receipt. **This is authoritative** and should be trusted over
  ``price * qty``: for weighed goods ``price * qty`` re-derives the total from
  rounded inputs and can be off by a cent. ``shop_import`` always emits
  ``line_total``; consumers fall back to ``round(price * qty, 2)`` only when it
  is missing.

Per-item discount fields — present **only on a discounted line**, absent
otherwise (so hand-transcribed and undiscounted receipts stay clean; a consumer
must treat their absence as "no discount", never as zero):

* ``line_total_gross`` — the pre-discount line amount.
* ``line_discount``    — ``line_total_gross - line_total`` (the saving).
* ``price_net``        — the net per-unit price (``line_total / qty``); this is
  what the Open Prices publisher posts as the paid price, with
  ``line_total_gross`` supplied as ``--discount EAN=GROSS``.
* ``discounts``        — a list (a line can carry several discounts of different
  kinds — e.g. a Lidl Plus coupon *and* a short-expiry markdown), each
  ``{amount, type, openprices_type, label[, percent]}``. ``type`` is the raw kind
  (``lidlplus_coupon`` / ``markdown``); ``openprices_type`` is the mapped Open
  Prices ``discount_type`` (``LOYALTY_PROGRAM`` / ``EXPIRES_SOON`` / ``SALE``).

Top-level money fields: ``receipt_total`` is the **net** charged total;
``receipt_total_gross`` and ``receipt_discount_total`` surface the pre-discount
total and the saving so a reviewer sees both.

``receipt_total`` is not decoration: :func:`require_flat` refuses any file whose
line items do not sum to it (within one cent of per-line rounding). A file with
items must carry one. See :func:`reconcile_total`, and ``receipt-formats.json``
for the per-chain layout quirks that make a hand transcription go wrong in the
first place.

Per-item routing flags:

* ``to_tingbok`` — set ``true`` to push a price/receipt-name observation to
  tingbok, ``false`` to skip (e.g. no barcode, by-weight produce). Must be set
  **explicitly** during the review step; ``shop_import`` emits ``null`` as a
  deliberate reminder. ``tingbok_push.py`` skips items where this is falsy and
  warns loudly if *every* item was skipped (which usually means the flag was
  never filled in).
"""

from __future__ import annotations

from typing import Any

#: Largest sum-vs-total discrepancy accepted as arithmetic rather than error.
#: One cent: weighed lines are rounded per line, so a receipt can legitimately
#: be a cent away from the sum of its own printed line totals. Two cents is a
#: missing or misattributed line, not rounding.
RECONCILE_TOLERANCE = 0.01


class ReconciliationError(ValueError):
    """A staging file's line items do not sum to the receipt total it claims."""


def line_total_of(item: dict[str, Any]) -> float:
    """The net amount charged for one staging line.

    ``line_total`` is authoritative when present (the till's own printed figure);
    otherwise fall back to ``price * qty``, which re-derives it from rounded
    inputs and may be a cent off — see the module docstring.
    """
    if item.get("line_total") is not None:
        return float(item["line_total"])
    return round(float(item.get("price", 0) or 0) * float(item.get("qty", 1) or 0), 2)


def reconcile_total(staging: dict[str, Any], tolerance: float = RECONCILE_TOLERANCE) -> float:
    """Check the line items sum to ``receipt_total``; return the discrepancy.

    A hand-transcribed receipt is the one point in this pipeline where a human
    reads numbers off a photograph, and the sum is the only cross-check that
    exists for that reading. So a mismatch is an **error**, not a warning: on
    2026-07-24 it was the only thing that caught the Billa multiplier-line quirk
    (``3 x 0.71`` printed *above* the item it belongs to, so a naive top-down
    reading billed three beers to a pack of cleaning cloths). Both readings look
    equally plausible on the photo; only one of them adds up.

    A file whose items carry money must state a ``receipt_total`` — omitting it
    is not a way to opt out of the check. A file with no money on any line (an
    inventory-only staging file, recording things acquired rather than bought)
    has nothing to reconcile and needs no total; requiring a ``receipt_total: 0``
    there would only teach the habit of writing one to silence the gate.

    A line that carries no money among lines that do counts as 0.00 — right for
    a free carrier bag, and for a price left out by mistake it unbalances the
    sum, which is the point.

    Returns ``abs(sum - total)`` so a caller can report near-misses;
    raises :class:`ReconciliationError` when that exceeds *tolerance*.
    """
    items = staging.get("items") or []
    priced = [i for i in items if i.get("line_total") is not None or i.get("price") is not None]
    if not priced:
        return 0.0
    if staging.get("receipt_total") is None:
        raise ReconciliationError(
            f"staging file for {staging.get('shop', '?')} has {len(priced)} priced line items but no "
            "receipt_total; the printed total is what the line items are checked against and must be transcribed"
        )
    total = float(staging["receipt_total"])
    line_sum = round(sum(line_total_of(i) for i in items), 2)
    delta = round(abs(line_sum - total), 2)
    if delta > tolerance:
        raise ReconciliationError(
            f"staging file for {staging.get('shop', '?')} does not reconcile: "
            f"{len(items)} line items sum to {line_sum}, receipt_total is {total} "
            f"(off by {delta}). Re-read the receipt — a misattributed multiplier line, "
            f"a dropped line or a deposit/discount line not transcribed. "
            f"See receipt-formats.json for this chain's known layout quirks."
        )
    return delta


def require_flat(staging: Any) -> dict[str, Any]:
    """Validate a staging file: canonical flat schema *and* balanced money.

    Raises ``ValueError`` if *staging* is not a mapping or still carries the
    retired multi-shop ``shops:`` wrapper, and :class:`ReconciliationError` if
    its line items do not sum to its ``receipt_total``.

    The money check lives here rather than in each consumer because every
    consumer (``ledger.py``, ``tingbok_push.py``, ``inventory_import.py``)
    already calls this function — a gate a caller has to remember to open is a
    gate that eventually stays shut.
    """
    if not isinstance(staging, dict):
        raise ValueError("staging must be a mapping (flat single-shop schema)")
    if "shops" in staging:
        raise ValueError(
            "multi-shop 'shops:' staging is no longer supported; split the trip into one flat file per shop visit"
        )
    reconcile_total(staging)
    return staging
