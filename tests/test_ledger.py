"""Tests for ledger.py — the append-only purchases.jsonl spending ledger.

One line per receipt line-item, keyed by inventory_id so spending can be sliced
by category/time and joined to consumption (items removed from inventory.md).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from purchase_pipeline.ledger import (
    combine_duplicate_lines,
    consumed_rows,
    decathlon_purchase_to_rows,
    detect_removals,
    extract_ids,
    filter_rows,
    lidl_receipt_to_rows,
    load_decathlon_records,
    load_rows,
    row_identity,
    staging_to_rows,
    total,
    upsert_rows,
)

LIDL_RECEIPT = {
    "purchase_date": "2026.05.28",
    "store": "Варна",
    "total_price_no_saving": "6,64",
    "items": [
        {"name": "СВЕТЛА БИРА 3,0%", "price": "1,17", "quantity": "1"},
        {"name": "НЕКТАРИНИ НА КГ", "price": "2,79", "quantity": "0,078"},
    ],
}

DECATHLON_PURCHASE = {
    "purchase": {
        "transaction": {
            "transaction_id": "7-881-881-XYZ",
            "business_unit_name": "VARNA",
            "currency": "EUR",
            "transaction_date_time_iso": "2026-01-18T14:45:17Z",
            "amount": 5.11,
            "sale_items": [
                {
                    "unit_price": 5.11,
                    "quantity": 1,
                    "amount": 5.11,
                    "product_name": "ВСЕСЕЗОННА СМАЗКА ЗА ВЕРИГА",
                    "serial_number": {"gtin": "03583787174722", "dkt_item_lookup_code": "3583787174722"},
                }
            ],
        }
    }
}

# New Decathlon format: the rich order *detail* written one-per-line to
# decathlon-history.jsonl by ~/bin/decathlon-history.py. No barcode/gtin is
# carried; products are nested under orderProducts[].products[].
DECATHLON_ORDER = {
    "orderNumber": "122-1040",
    "orderTransactionId": "7-881-881-20260607175702-122-1040",
    "orderDate": "2026-06-07T17:57:02+0300",
    "orderOrigin": "VARNA",
    "orderCurrencyCode": "EUR",
    "orderTotalPrice": 33.73,
    "orderProducts": [
        {
            "products": [
                {
                    "productSku": "2903903",
                    "productQuantity": 1,
                    "productUnitPrice": 3.06,
                    "productCurrentPrice": 3.06,
                    "productName": "КОМПЛЕКТ ЗА ЛЕПЕНЕ НА ВЪТРЕШНИ ГУМИ",
                },
                {
                    "productSku": "5757667",
                    "productQuantity": 1,
                    "productUnitPrice": 30.67,
                    "productCurrentPrice": 30.67,
                    "productName": "ПОЛЯРИЗИРАНИ СЛЪНЧЕВИ ОЧИЛА EXPLORE 500 SQR, КАТЕГОРИЯ 3",
                },
            ],
        }
    ],
}


class TestLidlReceiptToRows:
    def test_one_row_per_item(self):
        rows = lidl_receipt_to_rows(LIDL_RECEIPT)
        assert len(rows) == 2

    def test_row_fields(self):
        beer = lidl_receipt_to_rows(LIDL_RECEIPT, shop="Lidl Varna")[0]
        assert beer["date"] == "2026-05-28"
        assert beer["shop"] == "Lidl Varna"
        assert beer["receipt_name"] == "СВЕТЛА БИРА 3,0%"
        assert beer["qty"] == 1.0
        assert beer["unit"] == "pcs"
        assert beer["unit_price"] == 1.17
        assert beer["total"] == 1.17
        assert beer["currency"] == "EUR"
        assert beer["ean"] is None
        assert beer["inventory_id"] is None

    def test_kg_total_is_price_times_qty(self):
        nectarines = lidl_receipt_to_rows(LIDL_RECEIPT)[1]
        assert nectarines["unit"] == "kg"
        assert nectarines["total"] == 0.22

    def test_source_recorded(self):
        rows = lidl_receipt_to_rows(LIDL_RECEIPT, source="lidl#2026-05-28")
        assert all(r["source"] == "lidl#2026-05-28" for r in rows)


class TestDecathlonPurchaseToRows:
    def test_row_has_ean_and_price(self):
        row = decathlon_purchase_to_rows(DECATHLON_PURCHASE)[0]
        assert row["date"] == "2026-01-18"
        assert row["shop"] == "Decathlon VARNA"
        assert row["ean"] == "3583787174722"
        assert row["name"] == "ВСЕСЕЗОННА СМАЗКА ЗА ВЕРИГА"
        assert row["qty"] == 1
        assert row["unit_price"] == 5.11
        assert row["total"] == 5.11
        assert row["currency"] == "EUR"

    def test_gtin_normalized_when_no_lookup_code(self):
        p = {
            "purchase": {
                "transaction": {
                    "business_unit_name": "VARNA",
                    "currency": "EUR",
                    "transaction_date_time_iso": "2026-01-18T14:45:17Z",
                    "sale_items": [
                        {
                            "unit_price": 1.0,
                            "quantity": 1,
                            "amount": 1.0,
                            "product_name": "x",
                            "serial_number": {"gtin": "03583787174722"},
                        }
                    ],
                }
            }
        }
        assert decathlon_purchase_to_rows(p)[0]["ean"] == "3583787174722"

    def test_gtin_preserves_legitimate_leading_zero(self):
        # UPC-A encoded as EAN-13 has a genuine leading zero; lstrip("0") must not eat it.
        p = {
            "purchase": {
                "transaction": {
                    "business_unit_name": "VARNA",
                    "currency": "EUR",
                    "transaction_date_time_iso": "2026-01-18T14:45:17Z",
                    "sale_items": [
                        {
                            "unit_price": 1.0,
                            "quantity": 1,
                            "amount": 1.0,
                            "product_name": "x",
                            # GTIN-14 where the inner EAN-13 starts with 0
                            "serial_number": {"gtin": "00012345678905"},
                        }
                    ],
                }
            }
        }
        assert decathlon_purchase_to_rows(p)[0]["ean"] == "0012345678905"


class TestDecathlonOrderToRows:
    """The new order-detail format (orderProducts[].products[], no barcode)."""

    def test_one_row_per_product(self):
        rows = decathlon_purchase_to_rows(DECATHLON_ORDER)
        assert len(rows) == 2

    def test_row_fields(self):
        row = decathlon_purchase_to_rows(DECATHLON_ORDER)[0]
        assert row["date"] == "2026-06-07"
        assert row["shop"] == "Decathlon VARNA"
        assert row["receipt_name"] == "КОМПЛЕКТ ЗА ЛЕПЕНЕ НА ВЪТРЕШНИ ГУМИ"
        assert row["name"] == "КОМПЛЕКТ ЗА ЛЕПЕНЕ НА ВЪТРЕШНИ ГУМИ"
        assert row["qty"] == 1
        assert row["unit"] == "pcs"
        assert row["unit_price"] == 3.06
        assert row["total"] == 3.06
        assert row["currency"] == "EUR"
        assert row["ean"] is None

    def test_total_is_unit_price_times_qty(self):
        order = {
            "orderDate": "2026-06-07T00:00:00+0300",
            "orderOrigin": "VARNA",
            "orderCurrencyCode": "EUR",
            "orderProducts": [{"products": [{"productName": "y", "productQuantity": 3, "productCurrentPrice": 2.5}]}],
        }
        assert decathlon_purchase_to_rows(order)[0]["total"] == 7.5

    def test_source_from_transaction_id(self):
        rows = decathlon_purchase_to_rows(DECATHLON_ORDER)
        assert all(r["source"] == "decathlon#7-881-881-20260607175702-122-1040" for r in rows)


class TestLoadDecathlonRecords:
    def test_reads_jsonl(self, tmp_path):
        f = tmp_path / "decathlon-history.jsonl"
        f.write_text(
            json.dumps({"orderNumber": "1"}, ensure_ascii=False)
            + "\n"
            + json.dumps({"orderNumber": "2"}, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        records = load_decathlon_records(f)
        assert [r["orderNumber"] for r in records] == ["1", "2"]

    def test_reads_single_json_object(self, tmp_path):
        f = tmp_path / "one.json"
        f.write_text(json.dumps({"orderNumber": "1"}), encoding="utf-8")
        assert load_decathlon_records(f) == [{"orderNumber": "1"}]

    def test_reads_json_array(self, tmp_path):
        f = tmp_path / "arr.json"
        f.write_text(json.dumps([{"orderNumber": "1"}, {"orderNumber": "2"}]), encoding="utf-8")
        assert len(load_decathlon_records(f)) == 2


class TestStagingToRows:
    def test_uses_reviewed_fields(self):
        staging = {
            "session": "2026-05-28",
            "shop": "Lidl Varna",
            "currency": "EUR",
            "receipt_total": 1.49,
            "items": [
                {
                    "receipt_name": "ПРЯСНО МЛЯКО 3,7%",
                    "price": 1.49,
                    "qty": 1.0,
                    "unit": "pcs",
                    "line_total": 1.49,
                    "ean": "4056489947882",
                    "name": "Pilos Fresh Milk 3.7%",
                    "category": "food/dairy",
                    "inventory_id": "milk-2026-05-28",
                    "add_to_inventory": True,
                },
            ],
        }
        row = staging_to_rows(staging)[0]
        assert row["ean"] == "4056489947882"
        assert row["category"] == "food/dairy"
        assert row["inventory_id"] == "milk-2026-05-28"
        assert row["total"] == 1.49

    def test_combines_identical_lines(self):
        # A receipt with the same product on two qty-1 lines (e.g. two identical
        # butters) must combine into one qty-2 row, or upsert_rows collapses them
        # by identity and silently drops one (undercount by the line total).
        staging = {
            "session": "2026-07-08",
            "shop": "Бурлекс Галата",
            "currency": "EUR",
            "receipt_total": 8.18,
            "items": [
                {"receipt_name": "МАСЛО КРАВЕ ЛУРПАК", "price": 4.09, "qty": 1, "line_total": 4.09},
                {"receipt_name": "МАСЛО КРАВЕ ЛУРПАК", "price": 4.09, "qty": 1, "line_total": 4.09},
            ],
        }
        rows = staging_to_rows(staging)
        assert len(rows) == 1
        assert rows[0]["qty"] == 2
        assert rows[0]["total"] == 8.18

    def test_discounted_line_is_booked_net(self):
        # A reviewed staging row for a discounted line carries the net line_total
        # (what was charged) and the net per-unit price. The ledger must book net,
        # and unit_price must be the net paid price so the Open Prices publisher
        # posts the paid price (with the gross supplied as --discount GROSS).
        staging = {
            "session": "2026-07-21",
            "shop": "Lidl Varna",
            "currency": "EUR",
            "receipt_total": 3.96,
            "items": [
                {
                    "receipt_name": "БИСКВИТИ КАКАО",
                    "price": 1.32,  # regular per-unit price (kept for tingbok)
                    "price_net": 0.99,  # net per-unit after the coupon
                    "qty": 4.0,
                    "unit": "stk",
                    "line_total": 3.96,  # net — authoritative charged amount
                    "line_total_gross": 5.28,
                    "line_discount": 1.32,
                    "ean": "5474146604470",
                },
            ],
        }
        row = staging_to_rows(staging)[0]
        assert row["total"] == 3.96  # booked net, not the 5.28 gross
        assert row["unit_price"] == 0.99  # net paid per-unit for Open Prices

    def test_undiscounted_line_unit_price_is_the_plain_price(self):
        staging = {
            "session": "2026-07-21",
            "shop": "Lidl Varna",
            "currency": "EUR",
            "receipt_total": 1.68,
            "items": [
                {"receipt_name": "КОРНФЛЕЙКС", "price": 1.68, "qty": 1.0, "unit": "stk", "line_total": 1.68},
            ],
        }
        row = staging_to_rows(staging)[0]
        assert row["unit_price"] == 1.68
        assert row["total"] == 1.68

    def test_retired_multishop_schema_raises(self):
        # The multi-shop ``shops:`` wrapper used to import 0 rows silently.
        staging = {
            "session": "2026-06-07",
            "shops": [
                {"shop": "Lidl Varna", "currency": "EUR", "items": [{"receipt_name": "A", "price": 1.0, "qty": 1.0}]},
            ],
        }
        with pytest.raises(ValueError, match="shops"):
            staging_to_rows(staging)


class TestImportStagingCli:
    """The import-staging subcommand must fail loudly, never import 0 rows quietly."""

    def _run(self, staging_text: str, tmp_path: Path) -> subprocess.CompletedProcess:
        staging = tmp_path / "shopping.yaml"
        staging.write_text(staging_text, encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "purchase_pipeline.ledger",
                "import-staging",
                str(staging),
                "--ledger",
                str(tmp_path / "out.jsonl"),
            ],
            capture_output=True,
            text=True,
        )

    def test_zero_rows_exits_nonzero(self, tmp_path):
        result = self._run("session: '2026-06-07'\nshop: X\ncurrency: EUR\nitems: []\n", tmp_path)
        assert result.returncode != 0
        assert "0 rows" in (result.stderr + result.stdout)
        assert not (tmp_path / "out.jsonl").exists()

    def test_multishop_schema_exits_nonzero(self, tmp_path):
        result = self._run(
            "session: '2026-06-07'\nshops:\n  - shop: Lidl\n    items:\n      - {receipt_name: A, price: 1.0, qty: 1.0}\n",
            tmp_path,
        )
        assert result.returncode != 0
        assert "shops" in (result.stderr + result.stdout)
        assert not (tmp_path / "out.jsonl").exists()


class TestCombineDuplicateLines:
    def test_repeated_line_becomes_qty_sum(self):
        # A receipt that lists the same product on two separate qty-1 lines.
        receipt = {
            "purchase_date": "2026.06.06",
            "store": "Billa",
            "total_price_no_saving": "3,56",
            "items": [
                {"name": "ОРИЗ BILLA", "price": "1,78", "quantity": "1"},
                {"name": "ОРИЗ BILLA", "price": "1,78", "quantity": "1"},
            ],
        }
        rows = combine_duplicate_lines(lidl_receipt_to_rows(receipt))
        assert len(rows) == 1
        assert rows[0]["qty"] == 2
        assert rows[0]["total"] == 3.56

    def test_different_products_not_combined(self):
        rows = combine_duplicate_lines(lidl_receipt_to_rows(LIDL_RECEIPT))
        assert len(rows) == 2  # beer + nectarines stay separate

    def test_same_name_different_price_not_combined(self):
        receipt = {
            "purchase_date": "2026.06.06",
            "store": "X",
            "total_price_no_saving": "3",
            "items": [
                {"name": "ITEM", "price": "1,00", "quantity": "1"},
                {"name": "ITEM", "price": "2,00", "quantity": "1"},
            ],
        }
        assert len(combine_duplicate_lines(lidl_receipt_to_rows(receipt))) == 2

    def test_order_preserved(self):
        receipt = {
            "purchase_date": "2026.06.06",
            "store": "X",
            "total_price_no_saving": "5",
            "items": [
                {"name": "A", "price": "1,00", "quantity": "1"},
                {"name": "B", "price": "2,00", "quantity": "1"},
                {"name": "A", "price": "1,00", "quantity": "1"},
            ],
        }
        rows = combine_duplicate_lines(lidl_receipt_to_rows(receipt))
        assert [r["receipt_name"] for r in rows] == ["A", "B"]
        assert rows[0]["qty"] == 2


class TestUpsert:
    def test_reimport_is_noop(self, tmp_path):
        path = tmp_path / "purchases.jsonl"
        rows = lidl_receipt_to_rows(LIDL_RECEIPT)
        assert upsert_rows(path, rows) == (2, 0)
        assert upsert_rows(path, rows) == (0, 0)  # re-import adds and enriches nothing
        assert len(load_rows(path)) == 2

    def test_staging_enriches_raw_row_in_place(self, tmp_path):
        path = tmp_path / "purchases.jsonl"
        upsert_rows(path, lidl_receipt_to_rows(LIDL_RECEIPT))  # raw: no ean/category
        staging = {
            "session": "2026-05-28",
            "shop": "Lidl Varna",
            "currency": "EUR",
            "receipt_total": 1.17,
            "items": [
                {
                    "receipt_name": "СВЕТЛА БИРА 3,0%",
                    "price": 1.17,
                    "qty": 1.0,
                    "unit": "pcs",
                    "line_total": 1.17,
                    "ean": "20431877",
                    "name": "Steam Brew Red",
                    "category": "food/beverages/beer",
                    "inventory_id": "beer-2026-05-28",
                }
            ],
        }
        added, enriched = upsert_rows(path, staging_to_rows(staging))
        assert (added, enriched) == (0, 1)  # matched the raw row, enriched it
        rows = load_rows(path)
        assert len(rows) == 2  # not duplicated
        beer = next(r for r in rows if r["receipt_name"] == "СВЕТЛА БИРА 3,0%")
        assert beer["ean"] == "20431877"
        assert beer["category"] == "food/beverages/beer"
        assert beer["inventory_id"] == "beer-2026-05-28"

    def test_null_incoming_never_overwrites(self, tmp_path):
        path = tmp_path / "purchases.jsonl"
        enriched = lidl_receipt_to_rows(LIDL_RECEIPT)
        enriched[0]["category"] = "food/beverages/beer"
        upsert_rows(path, enriched)
        upsert_rows(path, lidl_receipt_to_rows(LIDL_RECEIPT))  # raw nulls again
        beer = next(r for r in load_rows(path) if r["receipt_name"] == "СВЕТЛА БИРА 3,0%")
        assert beer["category"] == "food/beverages/beer"  # preserved

    def test_identity_ignores_enrichable_fields(self):
        raw = lidl_receipt_to_rows(LIDL_RECEIPT)[0]
        reviewed = dict(raw, ean="20431877", category="food/beverages/beer", inventory_id="x")
        assert row_identity(raw) == row_identity(reviewed)


class TestQuery:
    ROWS = [
        {"date": "2026-08-03", "category": "food/beverages/beer", "total": 1.17, "currency": "EUR", "shop": "Lidl"},
        {"date": "2026-08-20", "category": "food/dairy", "total": 1.49, "currency": "EUR", "shop": "Lidl"},
        {"date": "2026-09-01", "category": "food/beverages/beer", "total": 2.00, "currency": "EUR", "shop": "Lidl"},
    ]

    def test_filter_by_category_prefix(self):
        beer = filter_rows(self.ROWS, category="food/beverages")
        assert len(beer) == 2

    def test_filter_by_date_range(self):
        august = filter_rows(self.ROWS, since="2026-08-01", until="2026-08-31")
        assert len(august) == 2

    def test_total_sums(self):
        beer_august = filter_rows(self.ROWS, category="food/beverages/beer", since="2026-08-01", until="2026-08-31")
        assert total(beer_august) == 1.17


class TestExtractIds:
    def test_extracts_ids(self):
        text = "* category:milk ID:milk-2026 qty:1 Milk\n* category:beer ID:beer-x EAN:123 Beer"
        assert extract_ids(text) == {"milk-2026", "beer-x"}

    def test_ignores_container_headers_only_when_asked(self):
        # plain ID: tokens are captured regardless of context
        assert "food1" in extract_ids("## ID:food1 Pantry")


class TestDetectRemovals:
    def test_removal_recorded_at_disappearance_commit(self):
        revisions = [
            ("2026-08-01", {"milk-1", "beer-1"}),
            ("2026-08-10", {"beer-1"}),  # milk-1 removed here
            ("2026-08-20", set()),  # beer-1 removed here
        ]
        removals = detect_removals(revisions)
        assert removals == {"milk-1": "2026-08-10", "beer-1": "2026-08-20"}

    def test_readded_item_not_marked_removed(self):
        revisions = [
            ("2026-08-01", {"x"}),
            ("2026-08-05", set()),  # removed
            ("2026-08-09", {"x"}),  # re-added
        ]
        # latest state has it present -> not consumed
        assert "x" not in detect_removals(revisions)


class TestConsumedRows:
    def test_join_by_inventory_id_and_date(self):
        rows = [
            {"inventory_id": "milk-1", "total": 1.49, "date": "2026-07-01", "category": "food/dairy"},
            {"inventory_id": "beer-1", "total": 1.17, "date": "2026-07-02", "category": "food/beer"},
            {"inventory_id": None, "total": 9.0, "date": "2026-07-03", "category": "food/x"},
        ]
        removals = {"milk-1": "2026-08-10", "beer-1": "2026-09-20"}
        consumed = consumed_rows(rows, removals, since="2026-08-01", until="2026-08-31")
        assert [r["inventory_id"] for r in consumed] == ["milk-1"]
        assert consumed[0]["consumed_date"] == "2026-08-10"
