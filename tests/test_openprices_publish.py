"""Tests for openprices_publish pure helpers."""

import pytest

from purchase_pipeline.openprices_publish import (
    _dms_to_deg,
    _parse_category_price,
    _parse_discount,
    build_category_price,
    build_price,
    product_rows,
    receipt_currency,
)


class TestCategoryPrice:
    def test_parse_minimal(self):
        assert _parse_category_price("en:baguettes=0.45") == {
            "category_tag": "en:baguettes",
            "price": 0.45,
            "price_per": "UNIT",
        }

    def test_parse_full(self):
        s = _parse_category_price("en:baguettes=0.17,was=0.45,type=SALE,per=UNIT")
        assert s["category_tag"] == "en:baguettes"
        assert s["price"] == 0.17
        assert s["price_without_discount"] == 0.45
        assert s["discount_type"] == "SALE"

    def test_build_category_no_product_code(self):
        p = build_category_price(
            {"category_tag": "en:baguettes", "price": 0.17, "price_per": "UNIT", "price_without_discount": 0.45},
            proof_id=9,
            osm_type="WAY",
            osm_id=2,
            date="2026-06-06",
        )
        assert p["type"] == "CATEGORY"
        assert "product_code" not in p
        assert p["category_tag"] == "en:baguettes"
        assert p["price_per"] == "UNIT"
        assert p["price_is_discounted"] is True
        assert p["price_without_discount"] == 0.45


class TestDiscount:
    ROW = {"ean": "x", "unit_price": 0.79, "currency": "EUR", "date": "2026-06-06", "unit": "pcs"}

    def test_parse_discount(self):
        assert _parse_discount("3800050405919=1.15:SALE") == ("3800050405919", 1.15, "SALE")

    def test_parse_discount_default_type(self):
        assert _parse_discount("123=2.00") == ("123", 2.0, "SALE")

    def test_build_price_discounted(self):
        p = build_price(
            {**self.ROW, "price_without_discount": 1.15, "discount_type": "SALE"}, proof_id=1, osm_type="WAY", osm_id=1
        )
        assert p["price"] == 0.79
        assert p["price_is_discounted"] is True
        assert p["price_without_discount"] == 1.15
        assert p["discount_type"] == "SALE"

    def test_no_discount_when_gross_not_higher(self):
        p = build_price({**self.ROW, "price_without_discount": 0.79}, proof_id=1, osm_type="WAY", osm_id=1)
        assert "price_is_discounted" not in p


# The TYPE:ID parser moved to shop_osm (one cache, one parser) — see
# tests/test_shop_osm.py::TestParseOsmSpec.


class TestDmsToDeg:
    def test_north(self):
        assert _dms_to_deg((43.0, 13.0, 11.475), "N") == pytest.approx(43.21985, abs=1e-4)

    def test_east(self):
        assert _dms_to_deg((27.0, 52.0, 58.25), "E") == pytest.approx(27.88285, abs=1e-4)

    def test_south_is_negative(self):
        assert _dms_to_deg((10.0, 0.0, 0.0), "S") == pytest.approx(-10.0)


class TestBuildPrice:
    ROW = {
        "ean": "3800856095703",
        "unit_price": 1.78,
        "currency": "EUR",
        "date": "2026-06-06",
        "unit": "pcs",
        "name": "Billa rice",
    }

    def test_product_price_fields(self):
        p = build_price(self.ROW, proof_id=42, osm_type="NODE", osm_id=123)
        assert p["proof_id"] == 42
        assert p["type"] == "PRODUCT"
        assert p["product_code"] == "3800856095703"
        assert p["price"] == 1.78
        assert p["currency"] == "EUR"
        assert "price_per" not in p  # PRODUCT prices omit price_per
        assert p["location_osm_id"] == 123
        assert p["location_osm_type"] == "NODE"
        assert p["product_name"] == "Billa rice"

    def test_no_price_per_for_product(self):
        # price_per is only for CATEGORY prices; PRODUCT (EAN) must omit it
        row = {**self.ROW, "unit": "kg"}
        assert "price_per" not in build_price(row, proof_id=1, osm_type="NODE", osm_id=1)

    def test_ean_coerced_to_str(self):
        row = {**self.ROW, "ean": 3800856095703}
        assert build_price(row, proof_id=1, osm_type="NODE", osm_id=1)["product_code"] == "3800856095703"


class TestReceiptCurrency:
    """Category prices and the proof carry the receipt's currency, which is
    only known from the ledger — including rows without an EAN."""

    ROWS = [
        {"shop": "Holdbart Oslo", "date": "2026-08-29", "currency": "NOK", "name": "ice cream"},
        {"shop": "Odesos Varna", "date": "2026-09-03", "currency": "EUR", "ean": "3800207823016"},
    ]

    def test_from_a_row_without_ean(self):
        assert receipt_currency(self.ROWS, "Holdbart Oslo", "2026-08-29") == "NOK"

    def test_no_rows_for_the_shop_day_is_an_error(self):
        with pytest.raises(ValueError, match="no ledger rows"):
            receipt_currency(self.ROWS, "Holdbart Oslo", "2026-08-30")

    def test_mixed_currencies_is_an_error(self):
        rows = [*self.ROWS, {"shop": "Holdbart Oslo", "date": "2026-08-29", "currency": "EUR"}]
        with pytest.raises(ValueError, match="several currencies"):
            receipt_currency(rows, "Holdbart Oslo", "2026-08-29")


def test_build_price_strips_tingbok_shop_prefix():
    """The ledger carries tingbok's lidl-<code> key; Open Prices wants the bare code."""
    row = {"ean": "lidl-20358037", "unit_price": 1.29, "date": "2026-10-01"}
    payload = build_price(row, proof_id=1, osm_type="NODE", osm_id=2)
    assert payload["product_code"] == "20358037"


class TestProductRows:
    """Which ledger rows become PRODUCT prices — the whole receipt, or a retry subset."""

    LEDGER = [
        {"shop": "Lidl Varna", "date": "2026-10-04", "ean": "lidl-20358037"},
        {"shop": "Lidl Varna", "date": "2026-10-04", "ean": "4056489080510"},
        {"shop": "Lidl Varna", "date": "2026-10-04", "ean": None},
        {"shop": "Lidl Varna", "date": "2026-10-03", "ean": "4056489080510"},
    ]

    def test_all_ean_rows_of_the_receipt(self):
        rows = product_rows(self.LEDGER, "Lidl Varna", "2026-10-04")
        assert [r["ean"] for r in rows] == ["lidl-20358037", "4056489080510"]

    def test_only_retries_a_failed_line_by_bare_or_prefixed_code(self):
        """A 400 on one line must be retryable without duplicating the lines that went through."""
        for code in ("20358037", "lidl-20358037"):
            rows = product_rows(self.LEDGER, "Lidl Varna", "2026-10-04", only=[code])
            assert [r["ean"] for r in rows] == ["lidl-20358037"]
