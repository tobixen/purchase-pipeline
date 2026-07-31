"""Tests for photo_match.py — photo evidence → receipt line.

Two jobs, both mechanical, and the split matters: *extraction* (what does this
photo say?) belongs to inventory-md's ``extract_barcodes.py``; *association*
(which receipt line does it say it about?) belongs here.

The goal is that a reviewed staging file arrives with ``ean`` and ``bb`` already
filled wherever the evidence settles it, and with everything else visibly
flagged — never quietly resolved.
"""

from purchase_pipeline.photo_match import (
    associate_photos,
    build_loose_photos,
    classify_photo_result,
    fill_eans_from_candidates,
    find_date_candidates,
)


def _item(receipt_name, candidates=(), **overrides):
    """A staging row as shop_import scaffolds it, with EAN candidates filled in."""
    row = {
        "receipt_name": receipt_name,
        "ean": None,
        "ean_source": None,
        "ean_candidates": [{"ean": e, "score": s, "source": "tingbok_receipt_name"} for e, s in candidates],
        "bb": None,
        "bb_source": None,
        "photos": [],
        "needs_review": True,
        "notes": "",
    }
    row.update(overrides)
    return row


def _barcode(file, ean, **extra):
    """One ``extract_barcodes.py --json`` result for a decoded barcode photo."""
    return {"file": f"/p/{file}", "type": "EAN13", "data": ean, "product": None, "status": "ok", **extra}


def _ocr(file, text, **extra):
    return {
        "file": f"/p/{file}",
        "type": "OCR",
        "data": text,
        "ocr_results": [{"text": text}],
        "ocr_title": text,
        **extra,
    }


class TestFindDateCandidates:
    def test_iso_date(self):
        assert "2026-06-12" in find_date_candidates("Best before 2026-06-12")

    def test_dotted_full_date(self):
        assert "2026-06-12" in find_date_candidates("12.06.2026")

    def test_month_year_only(self):
        assert "2026-08" in find_date_candidates("08.2026")

    def test_no_date(self):
        assert find_date_candidates("Pilos Mlyako") == []


class TestClassifyPhotoResult:
    def test_barcode_photo(self):
        photo = classify_photo_result(_barcode("IMG_1.jpg", "4056489080510", product={"name": "Pilos Milk 3% 1l"}))
        assert photo == {
            "file": "IMG_1.jpg",
            "kind": "barcode",
            "ean": "4056489080510",
            "product": "Pilos Milk 3% 1l",
        }

    def test_expiry_photo_from_ocr_date(self):
        photo = classify_photo_result(_ocr("IMG_2.jpg", "12.06.2026", ocr_title=None))
        assert photo["kind"] == "expiry"
        assert "2026-06-12" in photo["ocr_date_candidates"]

    def test_barcode_photo_surfaces_best_before(self):
        photo = classify_photo_result(_barcode("IMG_1.jpg", "4056489693307", best_before="2026-07-25"))
        assert photo["kind"] == "barcode"
        assert photo["bb"] == "2026-07-25"

    def test_label_photo_without_date(self):
        photo = classify_photo_result(_ocr("IMG_3.jpg", "Pilos Mlyako"))
        assert photo["kind"] == "label"
        assert photo["ocr_title"] == "Pilos Mlyako"


class TestExtractorReviewBlocks:
    """The extractor's own ``status`` must survive the trip into the staging file.

    ``extract_barcodes.py`` marks a photo whose barcode decoded two
    parity-confusable ways as ``needs_review`` and emits it as a single
    ``tag:TODO`` block, precisely because two plausible EANs side by side invite
    picking the wrong one — both carry a valid check digit. Reading only ``data``
    turns that block back into one confident answer, which is the mistake the
    extractor went out of its way not to make.
    """

    def test_conflicting_reads_never_yield_an_ean(self):
        photo = classify_photo_result(
            _barcode(
                "IMG_1.jpg",
                "4056489080510",
                status="needs_review",
                alternatives=["4056489080510", "4056489080503"],
                status_reason="conflicting barcode reads; cannot tell which one is the misdecode",
            )
        )
        assert photo["kind"] == "barcode_conflict"
        assert "ean" not in photo
        assert photo["ean_candidates"] == ["4056489080510", "4056489080503"]
        assert "conflicting" in photo["review"]

    def test_undecoded_barcode_is_flagged_not_dropped(self):
        photo = classify_photo_result(
            {
                "file": "/p/IMG_9.jpg",
                "type": "NO_DECODE",
                "data": "",
                "product": None,
                "status": "needs_review",
                "status_reason": "barcode-like pattern present, but nothing decoded",
            }
        )
        assert photo["kind"] == "undecoded"
        assert "ean" not in photo
        assert photo["review"]

    def test_rejected_losing_read_is_dropped(self):
        """A losing read is kept in --json only to show what happened."""
        results = [
            _barcode("IMG_1.jpg", "4056489080510", corroboration=3),
            _barcode("IMG_1.jpg", "4056489080503", status="rejected", status_reason="conflicts with 4056489080510"),
        ]
        loose = build_loose_photos(results)
        assert [p["kind"] for p in loose] == ["barcode"]
        assert loose[0]["ean"] == "4056489080510"


class TestBuildLoosePhotos:
    def test_maps_each_result(self):
        loose = build_loose_photos([_barcode("IMG_1.jpg", "4056489080510"), _ocr("IMG_2.jpg", "12.06.2026")])
        assert [p["kind"] for p in loose] == ["barcode", "expiry"]

    def test_following_expiry_photo_paired_to_barcode(self):
        loose = build_loose_photos([_barcode("IMG_1.jpg", "4056489080510"), _ocr("IMG_2.jpg", "12.06.2026")])
        assert loose[0]["bb"] == "2026-06-12"
        assert loose[0]["bb_from"] == "IMG_2.jpg"

    def test_own_best_before_not_overwritten_by_following(self):
        loose = build_loose_photos(
            [_barcode("IMG_1.jpg", "4056489080510", best_before="2026-07-25"), _ocr("IMG_2.jpg", "12.06.2026")]
        )
        assert loose[0]["bb"] == "2026-07-25"
        assert "bb_from" not in loose[0]

    def test_barcode_without_following_expiry_unpaired(self):
        loose = build_loose_photos([_barcode("IMG_1.jpg", "4056489080510"), _barcode("IMG_2.jpg", "4056489693307")])
        assert "bb" not in loose[0]
        assert "bb" not in loose[1]

    def test_expiry_is_not_paired_onto_an_unreadable_barcode(self):
        """A photo that needs review is not a line to hang a date on."""
        results = [
            {"file": "/p/IMG_1.jpg", "type": "NO_DECODE", "data": "", "status": "needs_review"},
            _ocr("IMG_2.jpg", "12.06.2026"),
        ]
        loose = build_loose_photos(results)
        assert "bb" not in loose[0]
        assert loose[1]["kind"] == "expiry"  # still there for the reviewer


class TestAssociatePhotos:
    """A photo's EAN attaches to a receipt line only when the line already
    suggested that EAN. Two independent sources — the scan says the EAN was in
    the basket, tingbok's reverse receipt-name lookup says this till string has
    been that EAN before — agreeing is the whole of the evidence; either alone
    is a guess."""

    def test_corroborated_photo_fills_the_line(self):
        items = [_item("ПРЯСНО МЛЯКО 3,7%", [("4056489080527", 1.0)])]
        loose = associate_photos(items, build_loose_photos([_barcode("IMG_1.jpg", "4056489080527")]))
        assert items[0]["ean"] == "4056489080527"
        assert items[0]["ean_source"] == "photo:IMG_1.jpg"
        assert items[0]["photos"] == ["IMG_1.jpg"]
        assert loose == []

    def test_best_before_travels_with_the_match(self):
        items = [_item("ЛУКАНКА", [("4056489693307", 1.0)])]
        associate_photos(items, build_loose_photos([_barcode("IMG_1.jpg", "4056489693307", best_before="2026-07-25")]))
        assert items[0]["bb"] == "2026-07-25"
        assert items[0]["bb_source"] == "photo:IMG_1.jpg"

    def test_paired_expiry_frame_travels_too(self):
        """The date shot in the *next* frame lands on the line, and says so.

        Its file is attached to the item as well: the pairing is a positional
        guess, and the reviewer sanity-checking it needs to know which photo to
        open.
        """
        items = [_item("ЛУКАНКА", [("4056489693307", 1.0)])]
        photos = build_loose_photos([_barcode("IMG_1.jpg", "4056489693307"), _ocr("IMG_2.jpg", "25.07.2026")])
        loose = associate_photos(items, photos)
        assert items[0]["bb"] == "2026-07-25"
        assert "IMG_2.jpg" in items[0]["bb_source"]
        assert items[0]["photos"] == ["IMG_1.jpg", "IMG_2.jpg"]
        assert loose == []

    def test_uncorroborated_photo_stays_loose(self):
        """A new purchase: tingbok has never seen the till string, so nothing
        settles which line the scan belongs to. That is review work, and saying
        so is the correct outcome — not a guess."""
        items = [_item("НОВ ПРОДУКТ")]
        loose = associate_photos(items, build_loose_photos([_barcode("IMG_1.jpg", "4056489080527")]))
        assert items[0]["ean"] is None
        assert len(loose) == 1
        assert loose[0]["review"]

    def test_ean_suggested_by_two_lines_is_ambiguous(self):
        """Two of the same product on one receipt (bought twice, rung up twice)."""
        items = [_item("МЛЯКО", [("4056489080527", 1.0)]), _item("МЛЯКО", [("4056489080527", 1.0)])]
        loose = associate_photos(items, build_loose_photos([_barcode("IMG_1.jpg", "4056489080527")]))
        assert [i["ean"] for i in items] == [None, None]
        assert "several" in loose[0]["review"]

    def test_two_different_eans_matching_one_line_settle_nothing(self):
        items = [_item("СИРЕНЕ", [("4056489080527", 0.7), ("4056489693307", 0.7)])]
        photos = build_loose_photos([_barcode("IMG_1.jpg", "4056489080527"), _barcode("IMG_2.jpg", "4056489693307")])
        loose = associate_photos(items, photos)
        assert items[0]["ean"] is None
        assert len(loose) == 2

    def test_same_ean_twice_with_disagreeing_dates_fills_ean_but_not_bb(self):
        """Two units of one product, two different best-befores: the EAN is
        settled, the date is not, and picking one would be inventing a fact."""
        items = [_item("КИСЕЛО МЛЯКО", [("4056489080527", 1.0)])]
        photos = build_loose_photos(
            [
                _barcode("IMG_1.jpg", "4056489080527", best_before="2026-08-01"),
                _barcode("IMG_2.jpg", "4056489080527", best_before="2026-08-09"),
            ]
        )
        associate_photos(items, photos)
        assert items[0]["ean"] == "4056489080527"
        assert items[0]["bb"] is None
        assert "2026-08-01" in items[0]["notes"]
        assert "2026-08-09" in items[0]["notes"]

    def test_review_photos_are_never_associated(self):
        items = [_item("МЛЯКО", [("4056489080510", 1.0)])]
        photos = build_loose_photos(
            [
                _barcode(
                    "IMG_1.jpg",
                    "4056489080510",
                    status="needs_review",
                    alternatives=["4056489080510", "4056489080503"],
                )
            ]
        )
        loose = associate_photos(items, photos)
        assert items[0]["ean"] is None
        assert loose[0]["kind"] == "barcode_conflict"

    def test_label_and_expiry_photos_stay_loose(self):
        items = [_item("МЛЯКО", [("4056489080510", 1.0)])]
        loose = associate_photos(items, build_loose_photos([_ocr("IMG_3.jpg", "Pilos Mlyako")]))
        assert [p["kind"] for p in loose] == ["label"]

    def test_an_already_filled_ean_is_not_overwritten(self):
        """Re-running the importer over a hand-corrected staging file must not
        undo the correction."""
        items = [_item("МЛЯКО", [("4056489080510", 1.0)], ean="4056489999999", ean_source="user")]
        loose = associate_photos(items, build_loose_photos([_barcode("IMG_1.jpg", "4056489080510")]))
        assert items[0]["ean"] == "4056489999999"
        assert loose[0]["review"]


class TestFillEansFromCandidates:
    """Score 1.0 is an exact prior observation of this till string — the
    process-shopping guide's own rule is to trust it. Anything below 1.0 is a
    fuzzy suggestion that is plausibly wrong (a never-seen name still returns
    somebody else's product at ~0.68), so it is left for the reviewer."""

    def test_unique_exact_prior_observation_fills_the_line(self):
        items = [_item("ПРЯСНО МЛЯКО 3,7%", [("4056489080527", 1.0), ("4056489080510", 0.68)])]
        fill_eans_from_candidates(items)
        assert items[0]["ean"] == "4056489080527"
        assert items[0]["ean_source"] == "tingbok_receipt_name:1.0"

    def test_fuzzy_candidate_never_fills(self):
        items = [_item("НОВ ПРОДУКТ", [("4056489080527", 0.68)])]
        fill_eans_from_candidates(items)
        assert items[0]["ean"] is None

    def test_two_exact_candidates_are_ambiguous(self):
        """One till string, two EANs seen under it — the shop changed supplier."""
        items = [_item("СИРЕНЕ", [("4056489080527", 1.0), ("4056489693307", 1.0)])]
        fill_eans_from_candidates(items)
        assert items[0]["ean"] is None
        assert "1.0" in items[0]["notes"]

    def test_photo_evidence_wins(self):
        items = [_item("МЛЯКО", [("4056489080510", 1.0)])]
        associate_photos(items, build_loose_photos([_barcode("IMG_1.jpg", "4056489080510")]))
        fill_eans_from_candidates(items)
        assert items[0]["ean_source"] == "photo:IMG_1.jpg"
