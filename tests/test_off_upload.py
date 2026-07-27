"""Tests for off_upload.build_body (pure OFF write-body construction)."""

import pytest

from purchase_pipeline.off_upload import _images, build_body


def test_code_required():
    with pytest.raises(ValueError):
        build_body({"product_name_en": "x"})


def test_known_fields_passed_through():
    body = build_body(
        {
            "code": "3800225663700",
            "lang": "bg",
            "product_name_bg": "НАХУТ КРИНА",
            "product_name_en": "Krina chickpeas",
            "brands": "Krina",
            "quantity": "250 g",
            "categories": "Chickpeas, Legumes",
            "stores": "Billa",
            "countries": "Bulgaria",
        }
    )
    assert body["code"] == "3800225663700"
    assert body["product_name_bg"] == "НАХУТ КРИНА"
    assert body["brands"] == "Krina"
    assert body["quantity"] == "250 g"


def test_empty_and_unknown_fields_dropped():
    body = build_body(
        {
            "code": "1",
            "brands": "",
            "quantity": None,
            "front_image": "/x.jpg",  # not an OFF field
            "notes": "ignore me",
        }
    )
    assert body == {"code": "1"}  # nothing blank or extraneous gets written


def test_code_coerced_to_str_and_stripped():
    assert build_body({"code": " 123 "})["code"] == "123"


def test_images_collects_all_roles():
    imgs = _images({"images": {"front": "f.jpg", "ingredients": "i.jpg", "nutrition": "n.jpg", "packaging": "p.jpg"}})
    assert imgs == {"front": "f.jpg", "ingredients": "i.jpg", "nutrition": "n.jpg", "packaging": "p.jpg"}


def test_images_legacy_front_image_maps_to_front():
    assert _images({"front_image": "f.jpg"}) == {"front": "f.jpg"}


def test_images_empty_when_none():
    assert _images({"code": "1"}) == {}
