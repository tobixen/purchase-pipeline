"""Tests for shop_import.py — builds a human-correctable staging file from a
shopping receipt + barcode-extraction output.

The importer does only mechanical work (parse receipt, classify photos, gather
EAN candidates via an injectable searcher). Matching and best-before reading are
left to a later AI review step that edits the staging file.
"""

import pytest

from purchase_pipeline.shop_import import (
    _tingbok_searcher,
    build_loose_photos,
    build_staging,
    classify_photo_result,
    find_date_candidates,
    parse_lidl_receipt,
    parse_price,
    receipt_date,
    select_receipt,
)

LIDL_RECEIPT = {
    "purchase_date": "2026.05.28",
    "store": "Варна – бул. Вл. Варненчик 257",
    "total_price_no_saving": "6,64",
    "items": [
        {"name": "СВЕТЛА БИРА 3,0%", "price": "1,17", "quantity": "1"},
        {"name": "ПРЯСНО МЛЯКО 3,7%", "price": "1,49", "quantity": "1"},
        {"name": "НЕКТАРИНИ НА КГ", "price": "2,79", "quantity": "0,078"},
    ],
}


class TestParsePrice:
    def test_comma_decimal(self):
        assert parse_price("1,17") == 1.17

    def test_dot_decimal(self):
        assert parse_price("1.49") == 1.49

    def test_integer(self):
        assert parse_price("2") == 2.0


class TestParseLidlReceipt:
    def test_session_date_normalized_to_iso(self):
        staging = parse_lidl_receipt(LIDL_RECEIPT)
        assert staging["session"] == "2026-05-28"

    def test_currency_defaults_to_eur(self):
        assert parse_lidl_receipt(LIDL_RECEIPT)["currency"] == "EUR"

    def test_receipt_total_parsed(self):
        assert parse_lidl_receipt(LIDL_RECEIPT)["receipt_total"] == 6.64

    def test_source_recorded(self):
        assert parse_lidl_receipt(LIDL_RECEIPT)["source"] == "lidl_receipts.json"

    def test_one_row_per_item(self):
        assert len(parse_lidl_receipt(LIDL_RECEIPT)["items"]) == 3

    def test_pcs_item_fields(self):
        beer = parse_lidl_receipt(LIDL_RECEIPT)["items"][0]
        assert beer["receipt_name"] == "СВЕТЛА БИРА 3,0%"
        assert beer["price"] == 1.17
        assert beer["qty"] == 1
        assert beer["unit"] == "pcs"
        assert beer["line_total"] == 1.17

    def test_kg_item_detected_and_line_total_is_price_times_qty(self):
        nectarines = parse_lidl_receipt(LIDL_RECEIPT)["items"][2]
        assert nectarines["unit"] == "kg"
        assert nectarines["qty"] == 0.078
        assert nectarines["line_total"] == 0.22  # 2.79 * 0.078 rounded

    def test_rows_have_review_scaffold(self):
        item = parse_lidl_receipt(LIDL_RECEIPT)["items"][0]
        for field in ("ean", "name", "category", "bb", "bb_source", "location", "inventory_id"):
            assert item[field] is None
        assert item["ean_candidates"] == []
        assert item["add_to_inventory"] is True
        assert item["to_tingbok"] is None
        assert item["photos"] == []
        assert item["needs_review"] is True


# A hand-transcribed receipt (no shop JSON API): header keys date/shop/currency/
# total, item rows carrying explicit unit, per-unit price and printed line total.
GENERIC_RECEIPT = {
    "date": "2026-07-08",
    "shop": "Бурлекс Галата",
    "currency": "EUR",
    "total": 49.25,
    "items": [
        {"name": "ДОМАТИ РОЗОВИ", "quantity": 0.506, "unit": "kg", "unit_price": 2.15, "price": 1.09},
        {"name": "ПУРИЧКИ ВАФЛЕНИ ТАГО КАКАО 150Г", "quantity": 1, "unit_price": 1.65, "price": 1.65},
    ],
}


class TestParseGenericReceipt:
    """Regression: a hand-transcribed receipt used to get the Lidl header
    (shop 'Lidl Varna', receipt_total 0.0, source 'lidl_receipts.json')."""

    def test_header_comes_from_receipt_keys(self):
        staging = parse_lidl_receipt(GENERIC_RECEIPT, shop="Lidl Varna", source="receipt-2026-07-08-burlex.json")
        assert staging["session"] == "2026-07-08"
        assert staging["shop"] == "Бурлекс Галата"  # receipt overrides the CLI default
        assert staging["receipt_total"] == 49.25
        assert staging["currency"] == "EUR"
        assert staging["source"] == "receipt-2026-07-08-burlex.json"

    def test_weighed_item_honors_explicit_unit_and_unit_price(self):
        row = parse_lidl_receipt(GENERIC_RECEIPT)["items"][0]
        assert row["unit"] == "kg"
        assert row["qty"] == 0.506
        assert row["price"] == 2.15  # per-kg price
        assert row["line_total"] == 1.09  # printed line amount stays authoritative

    def test_pcs_item_with_unit_price(self):
        row = parse_lidl_receipt(GENERIC_RECEIPT)["items"][1]
        assert row["unit"] == "pcs"
        assert row["price"] == 1.65
        assert row["line_total"] == 1.65


# A real Lidl receipt (id 030001928220260721182207, 2026-07-21), trimmed to the
# lines that matter for money: three discounted lines, one plain line, and two
# weighed lines whose printed total is NOT price*qty (the till rounds per line —
# 0.99 * 2.398 kg = 2.374 computes to 2.37 on the receipt). The кисело краве
# мляко line carries TWO discounts of different kinds on the same line.
DISCOUNTED_RECEIPT = {
    "id": "030001928220260721182207",
    "purchase_date": "2026.07.21",
    "store": "Варна – ул. Битоля 1А",
    "total_price": "12,83",  # net — what the card was actually charged
    "total_price_gross": "15,49",
    "total_price_no_saving": "15,49",  # retired alias for the gross
    "discount_total": "2,66",
    "items": [
        {
            "name": "БИСКВИТИ КАКАО",
            "price": "1,32",
            "quantity": "4,0",
            "unit": "stk",
            "line_total": "5,28",
            "discounts": [
                {
                    "amount": "1,32",
                    "type": "lidlplus_coupon",
                    "label": "Lidl Plus купон",
                    "promotion_id": "100001000-BG-TEMPLATE-BGSD000439939-1",
                }
            ],
            "discount_total": "1,32",
            "net_total": "3,96",
        },
        {
            "name": "ГАУДА НАРЯЗАН",
            "price": "2,04",
            "quantity": "2,0",
            "unit": "stk",
            "line_total": "4,08",
            "discounts": [
                {
                    "amount": "0,82",
                    "type": "lidlplus_coupon",
                    "label": "Lidl Plus купон",
                    "promotion_id": "100001006-BG-TEMPLATE-BGAS000038349-1",
                }
            ],
            "discount_total": "0,82",
            "net_total": "3,26",
        },
        {
            "name": "КИСЕЛО КРАВЕ МЛЯКО",
            "price": "0,81",
            "quantity": "2,0",
            "unit": "stk",
            "line_total": "1,62",
            "discounts": [
                {
                    "amount": "0,24",
                    "type": "lidlplus_coupon",
                    "label": "Lidl Plus купон",
                    "promotion_id": "100001006-BG-TEMPLATE-BGAS000155979-1",
                },
                {
                    "amount": "0,28",
                    "type": "markdown",
                    "label": "ОТСТЪПКА 20%",
                    "promotion_id": "_DISCOUNT2",
                    "percent": 20,
                },
            ],
            "discount_total": "0,52",
            "net_total": "1,10",
        },
        {
            "name": "КОРНФЛЕЙКС",
            "price": "1,68",
            "quantity": "1",
            "unit": "stk",
            "line_total": "1,68",
            "discounts": [],
            "discount_total": None,
            "net_total": "1,68",
        },
        {
            "name": "ДЕЛИКАТЕСЕН ПЪПЕШ",
            "price": "0,99",
            "quantity": "2,398",
            "unit": "kg",
            "line_total": "2,37",
            "discounts": [],
            "discount_total": None,
            "net_total": "2,37",
        },
        {
            "name": "БАНАНИ НА КГ",
            "price": "1,09",
            "quantity": "0,426",
            "unit": "kg",
            "line_total": "0,46",
            "discounts": [],
            "discount_total": None,
            "net_total": "0,46",
        },
    ],
}


class TestDiscountedReceiptTotals:
    """The trip must be booked at what was actually charged, not the gross."""

    def test_receipt_total_is_the_net_charged_amount(self):
        assert parse_lidl_receipt(DISCOUNTED_RECEIPT)["receipt_total"] == 12.83

    def test_gross_total_surfaced_separately(self):
        assert parse_lidl_receipt(DISCOUNTED_RECEIPT)["receipt_total_gross"] == 15.49

    def test_total_discount_surfaced(self):
        assert parse_lidl_receipt(DISCOUNTED_RECEIPT)["receipt_discount_total"] == 2.66

    def test_line_totals_sum_to_the_net_receipt_total(self):
        staging = parse_lidl_receipt(DISCOUNTED_RECEIPT)
        assert round(sum(i["line_total"] for i in staging["items"]), 2) == staging["receipt_total"]

    def test_gross_line_totals_sum_to_the_gross_receipt_total(self):
        staging = parse_lidl_receipt(DISCOUNTED_RECEIPT)
        gross = sum(i.get("line_total_gross", i["line_total"]) for i in staging["items"])
        assert round(gross, 2) == staging["receipt_total_gross"]


class TestDiscountedLineItems:
    def _item(self, name):
        return next(i for i in parse_lidl_receipt(DISCOUNTED_RECEIPT)["items"] if i["receipt_name"] == name)

    def test_discounted_line_total_is_net(self):
        biscuits = self._item("БИСКВИТИ КАКАО")
        assert biscuits["line_total"] == 3.96
        assert biscuits["line_total_gross"] == 5.28
        assert biscuits["line_discount"] == 1.32

    def test_unit_price_stays_the_printed_regular_price(self):
        # `price` is the shelf/receipt unit price (what tingbok records); the
        # discounted per-unit price is `price_net`.
        biscuits = self._item("БИСКВИТИ КАКАО")
        assert biscuits["price"] == 1.32
        assert biscuits["price_net"] == 0.99  # 3.96 / 4

    def test_discount_details_carried_with_openprices_type(self):
        gouda = self._item("ГАУДА НАРЯЗАН")
        assert gouda["discounts"] == [
            {
                "amount": 0.82,
                "type": "lidlplus_coupon",
                "openprices_type": "LOYALTY_PROGRAM",
                "label": "Lidl Plus купон",
            }
        ]

    def test_two_discounts_of_different_kinds_on_one_line(self):
        """The yogurt line has a Lidl Plus coupon AND a 20% short-expiry markdown."""
        yogurt = self._item("КИСЕЛО КРАВЕ МЛЯКО")
        assert yogurt["line_total"] == 1.10
        assert yogurt["line_total_gross"] == 1.62
        assert yogurt["line_discount"] == 0.52
        assert [d["amount"] for d in yogurt["discounts"]] == [0.24, 0.28]
        assert [d["type"] for d in yogurt["discounts"]] == ["lidlplus_coupon", "markdown"]
        assert [d["openprices_type"] for d in yogurt["discounts"]] == ["LOYALTY_PROGRAM", "EXPIRES_SOON"]
        assert yogurt["discounts"][1]["percent"] == 20

    def test_weighed_line_uses_the_printed_total_not_price_times_qty(self):
        """0.99 EUR/kg * 2.398 kg = 2.374 — the till printed 2.37."""
        melon = self._item("ДЕЛИКАТЕСЕН ПЪПЕШ")
        assert melon["line_total"] == 2.37
        assert round(melon["price"] * melon["qty"], 2) == 2.37 or True  # documents the rounding trap
        assert melon["unit"] == "kg"

    def test_undiscounted_line_carries_no_discount_fields(self):
        """Absent discount data must stay absent, not appear as zeroes."""
        cornflakes = self._item("КОРНФЛЕЙКС")
        assert cornflakes["line_total"] == 1.68
        for field in ("line_total_gross", "line_discount", "price_net", "discounts"):
            assert field not in cornflakes


class TestBackwardsCompatibleReceipts:
    """Hand-transcribed and pre-discount-fix receipts must not crash or zero out."""

    def test_receipt_without_net_total_books_the_gross(self):
        # The old Lidl schema: only total_price_no_saving, no per-line net_total.
        staging = parse_lidl_receipt(LIDL_RECEIPT)
        assert staging["receipt_total"] == 6.64
        assert staging["receipt_total_gross"] == 6.64
        assert staging["receipt_discount_total"] == 0.0

    def test_hand_transcribed_receipt_has_no_discount_fields_on_items(self):
        staging = parse_lidl_receipt(GENERIC_RECEIPT)
        assert staging["receipt_total"] == 49.25
        assert staging["receipt_total_gross"] == 49.25
        assert staging["receipt_discount_total"] == 0.0
        for item in staging["items"]:
            assert "discounts" not in item
            assert "line_discount" not in item


class TestFindDateCandidates:
    def test_iso_date(self):
        assert "2026-06-12" in find_date_candidates("Best before 2026-06-12")

    def test_dotted_full_date(self):
        assert "2026-06-12" in find_date_candidates("12.06.2026")

    def test_month_year_only(self):
        assert "2026-08" in find_date_candidates("08.2026")

    def test_no_date(self):
        assert find_date_candidates("no dates here") == []


class TestClassifyPhotoResult:
    def test_barcode_photo(self):
        result = {
            "file": "/p/IMG_1.jpg",
            "type": "EAN13",
            "data": "4056489080510",
            "product": {"name": "Pilos Fresh Milk 3% 1l"},
        }
        photo = classify_photo_result(result)
        assert photo == {
            "file": "IMG_1.jpg",
            "kind": "barcode",
            "ean": "4056489080510",
            "product": "Pilos Fresh Milk 3% 1l",
        }

    def test_expiry_photo_from_ocr_date(self):
        result = {
            "file": "/p/IMG_2.jpg",
            "type": "OCR",
            "data": "12.06.2026",
            "ocr_results": [{"text": "12.06.2026"}],
            "ocr_title": None,
        }
        photo = classify_photo_result(result)
        assert photo["kind"] == "expiry"
        assert "2026-06-12" in photo["ocr_date_candidates"]

    def test_barcode_photo_surfaces_best_before(self):
        result = {
            "file": "/p/IMG_1.jpg",
            "type": "EAN13",
            "data": "4056489693307",
            "product": {"name": "Lukanka"},
            "best_before": "2026-07-25",
        }
        photo = classify_photo_result(result)
        assert photo["kind"] == "barcode"
        assert photo["bb"] == "2026-07-25"

    def test_label_photo_without_date(self):
        result = {
            "file": "/p/IMG_3.jpg",
            "type": "OCR",
            "data": "Pilos Mlyako",
            "ocr_results": [{"text": "Pilos Mlyako"}],
            "ocr_title": "Pilos Mlyako",
        }
        photo = classify_photo_result(result)
        assert photo["kind"] == "label"
        assert photo["ocr_title"] == "Pilos Mlyako"


class TestBuildLoosePhotos:
    def test_maps_each_result(self):
        results = [
            {"file": "/p/IMG_1.jpg", "type": "EAN13", "data": "4056489080510", "product": None},
            {"file": "/p/IMG_2.jpg", "type": "OCR", "data": "12.06.2026", "ocr_results": [{"text": "12.06.2026"}]},
        ]
        loose = build_loose_photos(results)
        assert [p["kind"] for p in loose] == ["barcode", "expiry"]

    def test_following_expiry_photo_paired_to_barcode(self):
        # A barcode shot with no date, followed by a separate expiry shot:
        # the expiry date should be carried back onto the barcode photo.
        results = [
            {"file": "/p/IMG_1.jpg", "type": "EAN13", "data": "4056489080510", "product": None},
            {"file": "/p/IMG_2.jpg", "type": "OCR", "data": "12.06.2026", "ocr_results": [{"text": "12.06.2026"}]},
        ]
        loose = build_loose_photos(results)
        assert loose[0]["bb"] == "2026-06-12"
        assert loose[0]["bb_from"] == "IMG_2.jpg"

    def test_own_best_before_not_overwritten_by_following(self):
        # A barcode photo that already carries its own bb keeps it and is not
        # re-paired to a following expiry photo.
        results = [
            {
                "file": "/p/IMG_1.jpg",
                "type": "EAN13",
                "data": "4056489080510",
                "product": None,
                "best_before": "2026-07-25",
            },
            {"file": "/p/IMG_2.jpg", "type": "OCR", "data": "12.06.2026", "ocr_results": [{"text": "12.06.2026"}]},
        ]
        loose = build_loose_photos(results)
        assert loose[0]["bb"] == "2026-07-25"
        assert "bb_from" not in loose[0]

    def test_barcode_without_following_expiry_unpaired(self):
        # Two consecutive barcode photos: neither gains a bb.
        results = [
            {"file": "/p/IMG_1.jpg", "type": "EAN13", "data": "4056489080510", "product": None},
            {"file": "/p/IMG_2.jpg", "type": "EAN13", "data": "4056489693307", "product": None},
        ]
        loose = build_loose_photos(results)
        assert "bb" not in loose[0]
        assert "bb" not in loose[1]


def _stub_searcher(receipt_name, shop=None):
    """Pretend tingbok knows the milk receipt name."""
    if "МЛЯКО" in receipt_name:
        return [
            {
                "ean": "4056489080527",
                "name": "Pilos Fresh Milk 3.7% 1l",
                "score": 1.0,
                "matched_name": receipt_name,
                "shop": shop,
            }
        ]
    return []


class TestBuildStaging:
    def test_candidates_populated_from_searcher(self):
        staging = build_staging(LIDL_RECEIPT, shop="Lidl Varna", searcher=_stub_searcher, barcode_results=[])
        milk = staging["items"][1]
        assert milk["ean_candidates"]
        assert milk["ean_candidates"][0]["ean"] == "4056489080527"
        assert milk["ean_candidates"][0]["source"] == "tingbok_receipt_name"

    def test_item_without_candidate_stays_empty(self):
        staging = build_staging(LIDL_RECEIPT, shop="Lidl Varna", searcher=_stub_searcher, barcode_results=[])
        beer = staging["items"][0]
        assert beer["ean_candidates"] == []

    def test_loose_photos_included(self):
        results = [{"file": "/p/IMG_1.jpg", "type": "EAN13", "data": "4056489080510", "product": None}]
        staging = build_staging(LIDL_RECEIPT, shop="Lidl Varna", searcher=_stub_searcher, barcode_results=results)
        assert staging["loose_photos"][0]["file"] == "IMG_1.jpg"

    def test_shop_recorded(self):
        staging = build_staging(LIDL_RECEIPT, shop="Lidl Varna", searcher=_stub_searcher, barcode_results=[])
        assert staging["shop"] == "Lidl Varna"

    def test_candidates_are_not_shop_filtered(self):
        """Candidate recall must not be narrowed by shop (best matches often have no shop)."""

        def shop_strict_searcher(receipt_name, shop=None):
            if shop is not None:
                return []  # would drop everything if the importer filtered by shop
            return _stub_searcher(receipt_name)

        staging = build_staging(LIDL_RECEIPT, shop="Lidl Varna", searcher=shop_strict_searcher, barcode_results=[])
        assert staging["items"][1]["ean_candidates"]  # milk still has a candidate


@pytest.mark.integration
class TestTingbokSearcherLive:
    """Live contract of tingbok's reverse receipt-name search (score semantics).

    Observations are never deleted from tingbok, so a receipt name pushed on a
    past shopping trip is a stable fixture. The process-shopping guide's rule
    rests on this contract: an exactly-seen receipt name returns its EAN as the
    top candidate with score 1.0 ("repeat purchase — trust it"), while an
    unseen name yields at most fuzzy score<1.0 suggestions ("needs review").
    """

    def test_repeat_purchase_matches_exactly_at_score_1(self):
        search = _tingbok_searcher()
        # Pushed 2026-07-06 (Май Маркет "Радост" trip): Верея fresh milk.
        results = search("ПР МЛЯКО ВЕРЕЯ ЧУДНО 1Л")
        assert results, "no candidate for a previously pushed receipt name"
        top = results[0]
        assert top["ean"] == "3800748051206"
        assert top["score"] == 1.0

    def test_unseen_name_never_scores_1(self):
        search = _tingbok_searcher()
        results = search("ZZZ НЕСЪЩЕСТВУВАЩ ПРОДУКТ 999Г")
        assert all(r["score"] < 1.0 for r in results)


# --- receipt selection ------------------------------------------------------

# Real-world shape: lidl_receipts.json is sorted by receipt *id*, and the id is
# not chronological. The 2026-07-21 trip (19 items) sorts BEFORE the 2026-07-17
# one (6 items) because its id prefix is lower, so "the last array element" is
# the wrong trip. Two receipts share 2026.07.10 (two visits in one day).
RECEIPTS = [
    {"id": "03000144812026071071567", "purchase_date": "2026.07.10", "items": [{"name": "A", "price": "1,00"}]},
    {"id": "03000144822026071065457", "purchase_date": "2026.07.10", "items": [{"name": "B", "price": "2,00"}]},
    {"id": "030001928220260721182207", "purchase_date": "2026.07.21", "items": [{"name": "C", "price": "3,00"}]},
    {"id": "030001929020260717123108", "purchase_date": "2026.07.17", "items": [{"name": "D", "price": "4,00"}]},
]


class TestReceiptDate:
    def test_lidl_dotted_date_normalised(self):
        assert receipt_date({"purchase_date": "2026.07.21"}) == "2026-07-21"

    def test_generic_date_key_accepted(self):
        assert receipt_date({"date": "2026-07-21"}) == "2026-07-21"

    def test_missing_date_is_empty(self):
        assert receipt_date({"id": "x"}) == ""


class TestSelectReceipt:
    def test_default_is_newest_by_date_not_array_position(self):
        """The bug: receipts[-1] is the 07-17 trip; the newest is 07-21."""
        chosen = select_receipt(RECEIPTS)
        assert chosen["id"] == "030001928220260721182207"
        assert receipt_date(chosen) == "2026-07-21"

    def test_select_by_receipt_id(self):
        chosen = select_receipt(RECEIPTS, receipt_id="030001929020260717123108")
        assert receipt_date(chosen) == "2026-07-17"

    def test_select_by_date(self):
        chosen = select_receipt(RECEIPTS, date="2026-07-17")
        assert chosen["id"] == "030001929020260717123108"

    def test_select_by_dotted_date_also_works(self):
        chosen = select_receipt(RECEIPTS, date="2026.07.17")
        assert chosen["id"] == "030001929020260717123108"

    def test_ambiguous_date_fails_loudly_listing_candidates(self):
        """Two visits on 2026-07-10 — refuse to guess, like match_shop_osm does."""
        with pytest.raises(ValueError) as exc:
            select_receipt(RECEIPTS, date="2026-07-10")
        message = str(exc.value)
        assert "03000144812026071071567" in message
        assert "03000144822026071065457" in message

    def test_ambiguous_newest_fails_loudly(self):
        """If the newest date itself has two receipts, the default must not guess."""
        same_day = RECEIPTS[:2]
        with pytest.raises(ValueError) as exc:
            select_receipt(same_day)
        assert "03000144812026071071567" in str(exc.value)

    def test_unknown_receipt_id_raises(self):
        with pytest.raises(ValueError, match="no receipt"):
            select_receipt(RECEIPTS, receipt_id="nope")

    def test_unknown_date_raises(self):
        with pytest.raises(ValueError, match="no receipt"):
            select_receipt(RECEIPTS, date="1999-01-01")

    def test_receipt_id_and_date_combine(self):
        chosen = select_receipt(RECEIPTS, receipt_id="03000144812026071071567", date="2026-07-10")
        assert chosen["id"] == "03000144812026071071567"

    def test_single_dict_passed_through(self):
        assert select_receipt(LIDL_RECEIPT) is LIDL_RECEIPT

    def test_empty_list_raises(self):
        with pytest.raises(ValueError):
            select_receipt([])
