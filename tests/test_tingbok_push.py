"""Tests for purchase_pipeline/tingbok_push.py — the standalone tingbok observation pusher.

The same script serves the shopping pipeline and ad-hoc single found-item pushes
(a minimal hand-written staging file). A found item typically has no receipt and
no price, so the payload must not carry empty ``receipt_names``/``prices`` rows.
"""

import datetime

from purchase_pipeline import tingbok_push


def test_shops_coerces_date_session_to_iso_string() -> None:
    """A bare YAML ``session:`` date is parsed by PyYAML as ``datetime.date``;
    it must be coerced to an ISO string so the JSON-serialised payload (which
    embeds it as price/receipt-name dates) doesn't crash with
    "Object of type date is not JSON serializable"."""
    staging = {
        "shop": "SVB24",
        "currency": "EUR",
        "session": datetime.date(2024, 11, 18),
        "items": [],
    }
    block = tingbok_push._shops(staging)[0]
    assert block["date"] == "2024-11-18"
    assert isinstance(block["date"], str)


def test_shops_leaves_string_session_unchanged() -> None:
    """A quoted/string session date passes through untouched."""
    block = tingbok_push._shops({"session": "2025-06-02", "items": []})[0]
    assert block["date"] == "2025-06-02"


def test_canonical_ean_prefixes_lidl_instore_code() -> None:
    """An 8-digit 2x in-store code is prefixed with the chain slug from the shop name."""
    assert tingbok_push.canonical_ean("20004132", "Lidl Varna бул. Вл. Варненчик 257") == "lidl-20004132"


def test_canonical_ean_prefixes_mercadona_ean8() -> None:
    """A 0x EAN-8 (Mercadona house-brand range) is prefixed too."""
    assert tingbok_push.canonical_ean("00501163", "Mercadona") == "mercadona-00501163"


def test_canonical_ean_leaves_global_ean_bare() -> None:
    """A real 13-digit EAN is globally unique and must not be prefixed."""
    assert tingbok_push.canonical_ean("3800214924577", "Lidl Varna") == "3800214924577"


def test_canonical_ean_leaves_weight_code_bare() -> None:
    """A 13-digit 2x weight/in-store code is not prefixed (matches tingbok's choice)."""
    assert tingbok_push.canonical_ean("2420520002823", "Lidl Varna") == "2420520002823"


def test_canonical_ean_passes_through_already_prefixed() -> None:
    """A hand-written prefixed key (contains a hyphen) is returned unchanged."""
    assert tingbok_push.canonical_ean("lidl-20004132", "Lidl Varna") == "lidl-20004132"


def test_canonical_ean_no_shop_leaves_bare() -> None:
    """Ad-hoc push with no shop can't derive a slug → leave the bare code."""
    assert tingbok_push.canonical_ean("20004132", "") == "20004132"


def test_canonical_ean_non_latin_shop_leaves_bare() -> None:
    """A Cyrillic-only shop name yields no ASCII slug → don't build a non-Latin prefix."""
    assert tingbok_push.canonical_ean("20004132", "Кауфланд") == "20004132"


def test_found_item_no_receipt_no_price() -> None:
    """A found item (no receipt_name, no price) pushes name/categories/quantity only."""
    item = {
        "ean": "3800214924577",
        "to_tingbok": True,
        "tingbok_name": "Bulgarian XXL dry-cured sausage",
        "tingbok_categories": ["meats", "dry sausages"],
        "tingbok_quantity": "550g",
    }
    payload = tingbok_push.build_payload(
        item, shop="", date="2026-06-15", currency="EUR", current={}, fill_missing=False
    )
    assert payload["name"] == "Bulgarian XXL dry-cured sausage"
    assert payload["categories"] == ["meats", "dry sausages"]
    assert payload["quantity"] == "550g"
    # No receipt → no receipt_names row; no price → no prices row.
    assert "receipt_names" not in payload
    assert "prices" not in payload


def test_shopping_item_keeps_receipt_and_price() -> None:
    """A normal receipt-derived item still records receipt_names + prices."""
    item = {
        "ean": "5000213101872",
        "to_tingbok": True,
        "receipt_name": "GUINNESS DRAUGHT",
        "name": "Guinness Draught Stout 440ml",
        "price": 2.52,
        "unit": "pcs",
    }
    payload = tingbok_push.build_payload(
        item, shop="Lidl", date="2026-06-13", currency="EUR", current={}, fill_missing=True
    )
    assert payload["receipt_names"] == [
        {"name": "GUINNESS DRAUGHT", "shop": "Lidl", "first_seen": "2026-06-13", "last_seen": "2026-06-13"}
    ]
    assert payload["prices"] == [
        {"date": "2026-06-13", "shop": "Lidl", "price": 2.52, "currency": "EUR", "unit": "pcs"}
    ]
