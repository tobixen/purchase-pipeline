"""Tests for inventory_import.py — pushes reviewed staging items into inventory.md.

The importer maps each `add_to_inventory` staging row to an
``inventory_md.additem.add_item`` call, so the process-shopping skill can write
a whole shop's items with one command instead of editing markdown by hand.
"""

from datetime import date
from pathlib import Path

import pytest

from purchase_pipeline.inventory_import import import_staging, staging_item_to_kwargs

_MD = """# Intro

Demo

# ID:floating Items without a fixed location

* category:rice ID:rice-1 bb:2027-01 Rice 1kg

# ID:food1 Pantry

* category:pasta ID:pasta-1 bb:2027-03 Pasta
"""


# --- staging_item_to_kwargs -------------------------------------------------


def test_kwargs_pcs_line():
    item = {
        "receipt_name": "MILK",
        "name": "Whole milk 1l",
        "category": "milk",
        "ean": "7038010068980",
        "bb": "2026-07",
        "qty": 2.0,
        "unit": "pcs",
        "price": 1.29,
        "location": "food1",
        "inventory_id": "milk-1",
        "add_to_inventory": True,
    }
    kw = staging_item_to_kwargs(item, "EUR")
    assert kw["container_id"] == "food1"
    assert kw["category"] == "milk"
    assert kw["item_id"] == "milk-1"
    assert kw["ean"] == "7038010068980"
    assert kw["bb"] == "2026-07"
    assert kw["bb_est"] is False
    assert kw["qty"] == "2"
    assert kw["mass"] is None
    assert kw["price"] == "EUR:1.29/pcs"
    assert kw["name"] == "Whole milk 1l"


def test_kwargs_weighed_line_becomes_mass():
    item = {
        "receipt_name": "NEKTARINI",
        "category": "nectarines",
        "qty": 1.768,
        "unit": "kg",
        "price": 2.79,
        "location": "floating",
        "bb": "2026-06",
        "add_to_inventory": True,
    }
    kw = staging_item_to_kwargs(item, "EUR")
    assert kw["mass"] == "1.768kg"
    assert kw["qty"] is None
    assert kw["price"] == "EUR:2.79/kg"


def test_kwargs_count_with_total_mass_and_price_unit():
    # by-weight produce sold as a piece count: qty=count, mass=total → total/count,
    # priced per kg via price_unit
    item = {
        "category": "tomatoes",
        "qty": 3,
        "unit": "pcs",
        "mass": "543g",
        "price": 4.59,
        "price_unit": "kg",
        "location": "floating",
        "bb": "2026-06:EST",
    }
    kw = staging_item_to_kwargs(item, "EUR")
    assert kw["qty"] == "3"
    assert kw["mass"] == "543g/3"
    assert kw["price"] == "EUR:4.59/kg"


def test_kwargs_single_piece_total_mass_is_bare():
    item = {"category": "onions", "qty": 1, "unit": "pcs", "mass": "436g", "price": 0.66, "price_unit": "kg"}
    kw = staging_item_to_kwargs(item, "EUR")
    assert kw["qty"] == "1"
    assert kw["mass"] == "436g"  # single piece → no "/1"
    assert kw["price"] == "EUR:0.66/kg"


def test_kwargs_count_with_total_volume():
    item = {"category": "beer", "qty": 6, "unit": "pcs", "volume": "3l", "price": 0.51}
    kw = staging_item_to_kwargs(item, "EUR")
    assert kw["qty"] == "6"
    assert kw["volume"] == "3l/6"
    assert kw["price"] == "EUR:0.51/pcs"  # price_unit defaults to unit


def test_kwargs_bb_est_suffix_split():
    item = {"category": "potatoes", "bb": "2026-09:EST", "unit": "kg", "qty": 1.2, "location": "floating"}
    kw = staging_item_to_kwargs(item, "EUR")
    assert kw["bb"] == "2026-09"
    assert kw["bb_est"] is True


def test_kwargs_bb_source_marks_estimate():
    item = {"category": "potatoes", "bb": "2026-09", "bb_source": "shelf-life estimate", "location": "floating"}
    kw = staging_item_to_kwargs(item, "EUR")
    assert kw["bb_est"] is True


@pytest.mark.parametrize("source", ["best-before printed on the lid", "latest photo", "photo:IMG_20260731_114413.jpg"])
def test_kwargs_bb_source_mentioning_best_before_is_not_an_estimate(source):
    # "est" inside a word ("best-before") is not the estimate marker.
    item = {"category": "potatoes", "bb": "2026-09", "bb_source": source, "location": "floating"}
    assert staging_item_to_kwargs(item, "EUR")["bb_est"] is False


@pytest.mark.parametrize("source", ["est", "estimated by the user", "EST from shelf life"])
def test_kwargs_bb_source_estimate_words_mark_estimate(source):
    item = {"category": "potatoes", "bb": "2026-09", "bb_source": source, "location": "floating"}
    assert staging_item_to_kwargs(item, "EUR")["bb_est"] is True


def test_kwargs_bb_est_key_marks_estimate():
    # The separate ``bb_est: true`` key is the form shop_import/review writes when
    # the date is a shelf-life guess; it used to be dropped silently, recording the
    # guess as a hard fact.
    item = {"category": "rice", "bb": "2027-01-01", "bb_est": True, "location": "floating"}
    kw = staging_item_to_kwargs(item, "EUR")
    assert kw["bb"] == "2027-01-01"
    assert kw["bb_est"] is True


def test_kwargs_bb_est_key_and_suffix_agree():
    item = {"category": "rice", "bb": "2027-01-01:EST", "bb_est": True, "location": "floating"}
    kw = staging_item_to_kwargs(item, "EUR")
    assert kw["bb"] == "2027-01-01"
    assert kw["bb_est"] is True


def test_kwargs_bb_est_false_conflicting_with_suffix_raises():
    item = {"category": "rice", "bb": "2027-01-01:EST", "bb_est": False, "location": "floating"}
    with pytest.raises(ValueError, match="EST"):
        staging_item_to_kwargs(item, "EUR")


def test_kwargs_explicit_bb_est_false_beats_bb_source_guess():
    # bb_source is a free-text heuristic; an explicit flag is authoritative.
    item = {
        "category": "potatoes",
        "bb": "2026-09",
        "bb_est": False,
        "bb_source": "shelf-life estimate",
        "location": "floating",
    }
    kw = staging_item_to_kwargs(item, "EUR")
    assert kw["bb_est"] is False


def test_kwargs_non_boolean_bb_est_raises():
    item = {"category": "rice", "bb": "2027-01-01", "bb_est": "yes", "location": "floating"}
    with pytest.raises(ValueError, match="bb_est"):
        staging_item_to_kwargs(item, "EUR")


def test_kwargs_skip_when_add_to_inventory_false():
    item = {"category": "chewing-gum", "add_to_inventory": False, "location": "floating"}
    assert staging_item_to_kwargs(item, "EUR") is None


def test_kwargs_default_container_when_no_location():
    item = {"category": "milk", "bb": "2026-07", "add_to_inventory": True}
    kw = staging_item_to_kwargs(item, "EUR", default_container="floating")
    assert kw["container_id"] == "floating"


def test_kwargs_name_falls_back_to_receipt_name():
    item = {"category": "milk", "bb": "2026-07", "receipt_name": "PRYASNO MLYAKO", "location": "food1"}
    kw = staging_item_to_kwargs(item, "EUR")
    assert kw["name"] == "PRYASNO MLYAKO"


# --- import_staging ---------------------------------------------------------


@pytest.fixture
def md_path(tmp_path: Path) -> Path:
    p = tmp_path / "inventory.md"
    p.write_text(_MD, encoding="utf-8")
    return p


_STAGING = {
    "session": "2026-06-14",
    "shop": "Lidl",
    "currency": "EUR",
    "receipt_total": 3.27,  # 1.29 milk + 2 × 0.99 chocolate; the bag is free
    "items": [
        {
            "category": "milk",
            "name": "Whole milk",
            "bb": "2026-07",
            "qty": 1.0,
            "unit": "pcs",
            "price": 1.29,
            "location": "food1",
            "inventory_id": "milk-2026-06-14",
            "add_to_inventory": True,
        },
        {
            "category": "chocolate",
            "name": "Dark chocolate",
            "bb": "2026-12",
            "qty": 2.0,
            "unit": "pcs",
            "price": 0.99,
            "location": "floating",
            "inventory_id": "chocolate-2026-06-14",
            "add_to_inventory": True,
        },
        {
            "category": "shopping-bag",
            "name": "Plastic bag",
            "add_to_inventory": False,
            "location": "floating",
        },
    ],
}


def test_import_staging_dry_run_writes_nothing(md_path: Path):
    results = import_staging(_STAGING, md_path, commit=False)
    assert md_path.read_text(encoding="utf-8") == _MD
    actions = [action for _item, action, _res in results]
    assert actions.count("add") == 2
    assert actions.count("skip") == 1


def test_import_staging_commit_writes_all(md_path: Path):
    results = import_staging(_STAGING, md_path, commit=True)
    text = md_path.read_text(encoding="utf-8")
    assert "ID:milk-2026-06-14" in text
    assert "ID:chocolate-2026-06-14" in text
    assert "Plastic bag" not in text
    # milk landed in food1, chocolate in floating
    assert "milk-2026-06-14" in text.split("# ID:food1")[1]
    add_results = [res for _i, action, res in results if action == "add"]
    assert all(not r.errors for r in add_results)


def test_import_staging_skips_existing_id(md_path: Path):
    staging = {
        "session": "2026-06-14",
        "currency": "EUR",
        "items": [
            {
                "category": "rice",
                "bb": "2027-01",
                "location": "floating",
                "inventory_id": "rice-1",
                "add_to_inventory": True,
            },
        ],
    }
    results = import_staging(staging, md_path, commit=True)
    assert [a for _i, a, _r in results] == ["exists"]
    assert md_path.read_text(encoding="utf-8") == _MD


def test_import_staging_writes_est_marker_for_bb_est_key(md_path: Path):
    # End-to-end regression for the silently-dropped ``bb_est: true``: the written
    # line must carry the :EST marker, not a bare (asserted) date.
    staging = {
        "session": "2026-07-21",
        "currency": "EUR",
        "items": [
            {
                "category": "rice",
                "name": "Rice",
                "bb": "2027-01-01",
                "bb_est": True,
                "location": "food1",
                "inventory_id": "rice-2026-07-21",
                "add_to_inventory": True,
            },
        ],
    }
    import_staging(staging, md_path, commit=True)
    text = md_path.read_text(encoding="utf-8")
    assert "bb:2027-01-01:EST" in text


def test_import_staging_rejects_multishop(md_path: Path):
    with pytest.raises(ValueError, match="shops"):
        import_staging({"shops": []}, md_path, commit=False)


def test_import_staging_autogen_id_for_food(md_path: Path):
    staging = {
        "session": "2026-06-14",
        "currency": "EUR",
        "items": [
            {
                "category": "milk",
                "bb": "2026-07",
                "unit": "pcs",
                "qty": 1.0,
                "location": "food1",
                "add_to_inventory": True,
            },
        ],
    }
    results = import_staging(staging, md_path, commit=True, today=date(2026, 6, 14))
    _i, action, res = results[0]
    assert action == "add"
    assert res.item_id == "milk-2026-06-14"
    assert "ID:milk-2026-06-14" in md_path.read_text(encoding="utf-8")
