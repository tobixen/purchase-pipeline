"""Turn photo evidence into filled-in staging rows — the association half.

Two projects share the job of getting an ``ean`` and a ``bb`` onto a staging
line without a human opening a single photo, and the split is deliberate:

* **extraction** — *what does this photo say?* — is inventory-md's
  ``extract_barcodes.py``: decoding barcodes, corroborating a read against
  rescaled variants, OCR-ing a best-before off a dot-matrix print.
* **association** — *which receipt line does it say it about?* — is this
  module. A scan proves an EAN was in the basket; it says nothing about which
  till string it was rung up as.

The rule for associating is corroboration, and nothing weaker: a photo's EAN
fills a line only when that line already listed the EAN among its
``ean_candidates`` (tingbok's reverse receipt-name lookup — "this till string
has been that EAN before"). Two independent sources agreeing settles it; either
one alone is a guess, and a guess written into ``ean:`` is indistinguishable
from a fact by the time anything downstream reads it.

Everything that does not settle stays visible: unmatched photos remain in
``loose_photos`` carrying a ``review`` reason, and the line keeps ``ean: null``
with a note. That is the honest answer for a new purchase, where tingbok has
never seen the till string and no algorithm can settle it (the process-shopping
guide's "new purchase" regime).

The extractor's own review verdicts are carried through rather than flattened.
A photo whose barcode decoded two parity-confusable ways comes back as
``status: needs_review`` with ``alternatives``, and is emitted as **one**
``barcode_conflict`` photo with no ``ean`` at all — reading ``data`` and moving
on would turn a deliberate "a human must look at this" back into one confident
answer. Both candidates carry a valid check digit; an EAN-13 parity misdecode
recomputes the checksum over the corrupted digits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inventory_md.bb_dates import find_dates  # date parsing shared with extract_barcodes

#: Photo kinds that exist to be looked at by a human; never associated to a line.
REVIEW_KINDS = ("barcode_conflict", "undecoded")

#: A tingbok candidate score meaning "this exact till string was this EAN
#: before" — an observation, not a similarity guess. See the process-shopping
#: guide: 1.0 is trusted, anything below it is a fuzzy suggestion.
EXACT_SCORE = 1.0


def find_date_candidates(text: str) -> list[str]:
    """Extract ISO date candidates from free text (shared bb_dates parser)."""
    return find_dates(text)


def classify_photo_result(result: dict[str, Any]) -> dict[str, Any]:
    """Classify one ``extract_barcodes.py`` result as a staging photo entry.

    Kinds: ``barcode`` (a usable read), ``barcode_conflict`` / ``undecoded``
    (the extractor wants a human), ``rejected`` (a losing read of a resolved
    conflict, kept only so the file shows what happened),  ``expiry``, ``label``.

    A photo-derived best-before is surfaced as ``bb`` regardless of kind, since
    it often rides the barcode shot.
    """
    filename = Path(result["file"]).name
    bb = result.get("best_before")
    status = result.get("status")

    if result.get("type") == "NO_DECODE":
        return {
            "file": filename,
            "kind": "undecoded",
            "review": result.get("status_reason") or "barcode-like pattern present, but nothing decoded",
        }

    if result.get("type") != "OCR":
        if status == "rejected":
            # Kept, not silently dropped: build_loose_photos filters these out,
            # but a caller reading raw results should see why the read is gone.
            return {
                "file": filename,
                "kind": "rejected",
                "ean": result.get("data"),
                "review": result.get("status_reason"),
            }
        if status == "needs_review":
            return {
                "file": filename,
                "kind": "barcode_conflict",
                "ean_candidates": list(result.get("alternatives") or [result.get("data")]),
                "review": result.get("status_reason") or "conflicting barcode reads",
            }
        product = result.get("product") or {}
        photo = {"file": filename, "kind": "barcode", "ean": result.get("data"), "product": product.get("name")}
        if bb:
            photo["bb"] = bb
        return photo

    texts = [result.get("ocr_title") or "", result.get("data") or ""]
    texts += [r.get("text", "") for r in result.get("ocr_results", [])]
    dates: list[str] = []
    for text in texts:
        for iso in find_date_candidates(text):
            if iso not in dates:
                dates.append(iso)
    if bb or dates:
        photo = {"file": filename, "kind": "expiry", "ocr_date_candidates": dates}
        if bb:
            photo["bb"] = bb
        return photo
    return {"file": filename, "kind": "label", "ocr_title": result.get("ocr_title") or result.get("data")}


def _expiry_date(photo: dict[str, Any]) -> str | None:
    """Best best-before date an expiry photo offers, or None.

    Prefers the OCR pass's own ``bb`` pick; else the latest date candidate
    (best-before is usually the furthest-out date on a pack — lot/production
    dates are earlier).
    """
    if photo.get("bb"):
        return photo["bb"]
    dates = photo.get("ocr_date_candidates") or []
    return max(dates) if dates else None


def _pair_following_expiry(photos: list[dict[str, Any]]) -> None:
    """Carry an expiry-only photo's date back onto the preceding barcode photo.

    A best-before is often shot in the frame *immediately after* the barcode
    rather than on the barcode itself. When a barcode photo has no ``bb`` of its
    own and is directly followed by an ``expiry`` photo, attach that date as the
    barcode's ``bb`` and record the source frame in ``bb_from`` (the pairing is
    a positional guess, so the reviewer can see where it came from). Mutates in
    place.

    Only a ``barcode`` photo is paired: a conflicting or undecoded read is not a
    product yet, and hanging a date on it would suggest it were one.
    """
    for prev, cur in zip(photos, photos[1:], strict=False):
        if prev.get("kind") != "barcode" or prev.get("bb"):
            continue
        if cur.get("kind") != "expiry":
            continue
        date = _expiry_date(cur)
        if date:
            prev["bb"] = date
            prev["bb_from"] = cur["file"]


def build_loose_photos(barcode_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify every barcode-extraction result into a loose_photos list.

    Losing reads of a resolved conflict are dropped here: the extractor reports
    them for transparency, but as a staging entry a ``rejected`` read is an EAN
    that was decided against, sitting next to the one that won.
    """
    photos = [classify_photo_result(r) for r in barcode_results]
    photos = [p for p in photos if p["kind"] != "rejected"]
    _pair_following_expiry(photos)
    return photos


def _candidate_eans(item: dict[str, Any]) -> set[str]:
    return {str(c["ean"]) for c in item.get("ean_candidates") or [] if c.get("ean")}


def _note(item: dict[str, Any], text: str) -> None:
    """Append a machine note to the row's free-text ``notes``, keeping any human one."""
    existing = (item.get("notes") or "").strip()
    item["notes"] = f"{existing} {text}".strip() if existing else text


def _attach(item: dict[str, Any], files: list[str]) -> None:
    item["photos"] = list(dict.fromkeys([*(item.get("photos") or []), *files]))


def associate_photos(items: list[dict[str, Any]], photos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill ``ean``/``bb`` on the lines whose photos corroborate them.

    Mutates *items*; returns the photos left over, each carrying a ``review``
    string saying why it could not be placed. A photo is placed only when
    exactly one line lists its EAN as a candidate **and** that line is matched
    by exactly one EAN — an ambiguity in either direction is reported, never
    resolved by picking.
    """
    placed: dict[int, list[dict[str, Any]]] = {}
    for photo in photos:
        if photo.get("kind") != "barcode" or not photo.get("ean"):
            continue
        matches = [i for i, item in enumerate(items) if photo["ean"] in _candidate_eans(item)]
        if len(matches) == 1:
            placed.setdefault(matches[0], []).append(photo)
        elif not matches:
            photo["review"] = (
                f"no receipt line lists EAN {photo['ean']} as a candidate — new purchase, or the wrong basket"
            )
        else:
            names = ", ".join(repr(items[i].get("receipt_name")) for i in matches)
            photo["review"] = f"several receipt lines list EAN {photo['ean']} as a candidate: {names}"

    consumed: set[str] = set()
    for index, group in placed.items():
        item = items[index]
        eans = sorted({p["ean"] for p in group})
        files = [p["file"] for p in group]
        if len(eans) > 1:
            for photo in group:
                photo["review"] = f"line {item.get('receipt_name')!r} is matched by several EANs: {', '.join(eans)}"
            continue
        if item.get("ean") and item["ean"] != eans[0]:
            for photo in group:
                photo["review"] = f"line {item.get('receipt_name')!r} already carries EAN {item['ean']}"
            continue

        item["ean"] = eans[0]
        item["ean_source"] = f"photo:{','.join(files)}"
        consumed.update(files)
        _attach(item, files)

        dates = {p["bb"] for p in group if p.get("bb")}
        if len(dates) == 1:
            source = next(p for p in group if p.get("bb"))
            item["bb"] = source["bb"]
            if source.get("bb_from"):
                # A positional guess, so name the frame it was read off.
                item["bb_source"] = f"photo:{source['bb_from']} (paired with barcode {source['file']})"
                consumed.add(source["bb_from"])
                _attach(item, [source["bb_from"]])
            else:
                item["bb_source"] = f"photo:{source['file']}"
        elif len(dates) > 1:
            _note(item, f"photos disagree on best-before ({', '.join(sorted(dates))}) — pick one.")

    return [p for p in photos if p["file"] not in consumed]


def fill_eans_from_candidates(items: list[dict[str, Any]]) -> None:
    """Fill ``ean`` on lines that have exactly one exact prior observation.

    Score 1.0 from tingbok's reverse receipt-name search is not a similarity
    score: it means this exact till string was pushed for that EAN on an earlier
    trip. The process-shopping guide's rule for a repeat purchase is to trust
    it, and this is that rule, applied mechanically and recorded in
    ``ean_source`` so the reviewer can see it was not a human's decision.

    Two exact candidates (a shop that changed supplier under one till string)
    settle nothing and are noted instead. Anything below 1.0 is left alone: a
    never-seen name still returns somebody else's product at ~0.68.
    """
    for item in items:
        if item.get("ean"):
            continue
        exact = sorted({str(c["ean"]) for c in item.get("ean_candidates") or [] if c.get("score") == EXACT_SCORE})
        if len(exact) == 1:
            item["ean"] = exact[0]
            item["ean_source"] = f"tingbok_receipt_name:{EXACT_SCORE}"
        elif len(exact) > 1:
            _note(item, f"{len(exact)} candidates scored {EXACT_SCORE} ({', '.join(exact)}) — pick one.")
