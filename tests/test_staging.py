"""Tests for the canonical staging schema guard (purchase_pipeline/staging.py).

Canonical schema is *flat single-shop*: one staging file per shop visit, with
top-level ``session`` / ``shop`` / ``currency`` / ``items``. The retired
multi-shop ``shops:`` wrapper must be rejected loudly (it used to import 0 rows
silently — see docs/shopping-pipeline-issues-2026-06-07.md issue 1).

The other guard here is money: a staging file's line items must sum to the
receipt total it claims. A hand-transcribed receipt is the one place in the
pipeline where a human reads numbers off a photograph, so it is the one place
where a transposed or misattributed line can enter — and the sum is the only
cross-check that exists.
"""

import pytest

from purchase_pipeline.staging import ReconciliationError, reconcile_total, require_flat


def _staging(items, total=None, **extra):
    s = {"session": "2026-07-24", "shop": "Billa Sozopol", "currency": "EUR", "items": items, **extra}
    if total is not None:
        s["receipt_total"] = total
    return s


def test_accepts_flat_single_shop():
    staging = {"session": "2026-06-07", "shop": "Lidl Varna", "currency": "EUR", "items": []}
    assert require_flat(staging) is staging


def test_rejects_multishop_shops_wrapper():
    staging = {"session": "2026-06-07", "shops": [{"shop": "Lidl", "items": []}]}
    with pytest.raises(ValueError, match="shops"):
        require_flat(staging)


def test_rejects_non_mapping():
    with pytest.raises(ValueError):
        require_flat([{"shop": "Lidl"}])


class TestReconcileTotal:
    def test_matching_sum_is_accepted(self):
        s = _staging([{"line_total": 2.13}, {"line_total": 1.99}], total=4.12)
        assert reconcile_total(s) == pytest.approx(0.0, abs=1e-9)

    def test_mismatch_raises_with_both_figures(self):
        s = _staging([{"line_total": 2.13}, {"line_total": 1.99}], total=5.00)
        with pytest.raises(ReconciliationError) as exc:
            reconcile_total(s)
        msg = str(exc.value)
        assert "4.12" in msg
        assert "5.0" in msg

    def test_one_cent_rounding_is_tolerated(self):
        # Weighed lines round per line; a single cent of drift is arithmetic, not error.
        s = _staging([{"line_total": 2.37}, {"line_total": 1.99}], total=4.37)
        assert reconcile_total(s) == pytest.approx(0.01, abs=1e-9)

    def test_two_cents_is_not_tolerated(self):
        s = _staging([{"line_total": 2.37}, {"line_total": 1.99}], total=4.38)
        with pytest.raises(ReconciliationError):
            reconcile_total(s)

    def test_falls_back_to_price_times_qty_when_line_total_absent(self):
        # staging.py documents line_total as authoritative *when present*.
        s = _staging([{"price": 0.71, "qty": 3}], total=2.13)
        assert reconcile_total(s) == pytest.approx(0.0, abs=1e-9)

    def test_items_without_a_receipt_total_is_an_error(self):
        # Omitting the total must not be a way to skip the check.
        with pytest.raises(ReconciliationError, match="receipt_total"):
            reconcile_total(_staging([{"line_total": 2.13}]))

    def test_empty_items_needs_no_total(self):
        assert reconcile_total(_staging([])) == pytest.approx(0.0, abs=1e-9)

    def test_inventory_only_file_with_no_money_needs_no_total(self):
        # Recording things acquired rather than bought. Demanding a
        # `receipt_total: 0` here would only teach writing one to silence the gate.
        s = _staging([{"name": "Plastic bag", "qty": 1}, {"name": "Rice", "qty": 2}])
        assert reconcile_total(s) == pytest.approx(0.0, abs=1e-9)

    def test_one_priced_line_among_unpriced_ones_still_requires_a_total(self):
        with pytest.raises(ReconciliationError, match="receipt_total"):
            reconcile_total(_staging([{"name": "Bag"}, {"name": "Milk", "price": 1.29, "qty": 1}]))

    def test_an_unpriced_line_counts_as_zero(self):
        # Right for a free carrier bag; for a price left out by mistake it
        # unbalances the sum, which is the point.
        s = _staging([{"name": "Free bag"}, {"name": "Milk", "price": 1.29, "qty": 1}], total=1.29)
        assert reconcile_total(s) == pytest.approx(0.0, abs=1e-9)


class TestRequireFlatEnforcesReconciliation:
    """The gate must be automatic — every consumer already calls require_flat."""

    def test_require_flat_rejects_an_unbalanced_file(self):
        with pytest.raises(ReconciliationError):
            require_flat(_staging([{"line_total": 2.13}], total=9.99))

    def test_require_flat_accepts_a_balanced_file(self):
        s = _staging([{"line_total": 2.13}], total=2.13)
        assert require_flat(s) is s


class TestBillaMultiplierReproducer:
    """The 2026-07-24 Billa Sozopol quirk: `N x unit_price` precedes its item.

    Billa prints the multiplier line *above* the item it belongs to. On that
    receipt `3 x 0.71` sits under `BILLA ПОП КЪРПИ 5Б` but belongs to the
    `ШУМЕНСКО` line below it, so a naive top-down reading bills three beers'
    worth (2.13) to a pack of cleaning cloths and leaves the beer at its own
    single-unit price.

    Only the two product names and the `3 x 0.71` multiplier are from the real
    receipt; the cloths' price and the total here are synthetic, reduced to the
    two lines that interact. What is being tested is that the sum catches the
    misattribution — which on the real receipt it did, the line items coming to
    18.12 exactly under the correct reading and not under the naive one.
    """

    CLOTHS = 1.85
    BEER_UNIT = 0.71
    BEER_QTY = 3
    TOTAL = 1.85 + 3 * 0.71  # 3.98

    def test_correct_reading_reconciles(self):
        s = _staging(
            [
                {"receipt_name": "BILLA ПОП КЪРПИ 5Б", "price": self.CLOTHS, "qty": 1, "line_total": self.CLOTHS},
                {"receipt_name": "ШУМЕНСКО", "price": self.BEER_UNIT, "qty": self.BEER_QTY, "line_total": 2.13},
            ],
            total=self.TOTAL,
        )
        assert require_flat(s) is s

    def test_naive_reading_is_caught_by_the_sum(self):
        # The multiplier applied to the line above it: cloths ×3, beer ×1.
        s = _staging(
            [
                {
                    "receipt_name": "BILLA ПОП КЪРПИ 5Б",
                    "price": self.BEER_UNIT,
                    "qty": self.BEER_QTY,
                    "line_total": 2.13,
                },
                {"receipt_name": "ШУМЕНСКО", "price": self.BEER_UNIT, "qty": 1, "line_total": self.BEER_UNIT},
            ],
            total=self.TOTAL,
        )
        with pytest.raises(ReconciliationError):
            require_flat(s)
