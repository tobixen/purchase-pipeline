#!/usr/bin/env python3
"""Per-chain receipt layout quirks, for transcribing a photographed receipt.

A receipt photo is transcribed by a human or an agent reading it top to bottom.
That reading is not obvious: chains disagree about where the branch address is,
which side of a line its `N x unit_price` multiplier sits on, how a discount is
signed, and whether a deposit line is part of the total. Get one wrong and the
staging file is quietly wrong about what was bought and for how much.

So the quirks are recorded per chain, machine-readable, in ``receipt-formats.json``
next to this script — and printed as a checklist before transcription::

    receipt_formats.py "Billa Sozopol"

Keyed by **chain**, deliberately unlike the branch-keyed shop→OSM cache: a Billa
receipt is laid out the same way in Varna and in Sozopol, while a Billa *price*
is not. A shop name resolves by chain prefix, and an ambiguous prefix raises
rather than guessing.

Nothing here is inferred. An entry exists only for a chain whose receipt has
actually been read, and every entry carries a ``source`` saying which one. An
unrecorded chain prints as unrecorded — inventing a plausible layout is the
exact failure this file exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY = Path(__file__).resolve().parent / "data" / "receipt-formats.json"

#: Human-readable prompt per registry field, used to render the checklist.
_FIELD_LABELS = {
    "branch_address": "Which address line identifies the branch",
    "multiplier_line": "Where the `N x unit_price` multiplier sits",
    "discounts": "How discounts appear",
    "deposits": "Deposit / returnable-container lines",
    "dual_currency": "Dual-currency totals and exchange rate",
    "weighed_items": "How weighed (per-unit-of-measure) lines are marked",
}


class AmbiguousChainError(ValueError):
    """Several registry chains claim the same shop name."""


def load_formats(path: Path = DEFAULT_REGISTRY) -> dict[str, Any]:
    """Load the chain→format registry, returning ``{}`` if it does not exist.

    Keys starting with ``_`` are comments (the file documents its own schema in
    ``_README``) and are dropped, so no caller can mistake one for a chain.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def chain_candidates(formats: dict[str, Any], shop: str) -> list[str]:
    """Registry chains that *shop* belongs to (chain is a prefix of the shop)."""
    if not shop:
        return []
    want = shop.strip().casefold()
    return [
        chain
        for chain in formats
        if want == chain.strip().casefold() or want.startswith(chain.strip().casefold() + " ")
    ]


def format_for(formats: dict[str, Any], shop: str) -> dict[str, Any] | None:
    """The recorded format for *shop*'s chain, or ``None`` if unrecorded.

    A shop is matched to a chain when the chain name is the shop name, or is a
    whole-word prefix of it (``"Billa Sozopol"`` → ``"Billa"``). ``"Bil"`` does
    not match ``"Billa"``: only the chain being a prefix of the shop counts.

    Two chains claiming one shop (``"Coop"`` and ``"Coop Extra"`` for ``"Coop
    Extra Varna"``) raises :class:`AmbiguousChainError`. Preferring the longer
    key would resolve it, but a registry where two entries claim one shop is a
    registry to fix, not a tie to break silently.
    """
    hits = chain_candidates(formats, shop)
    if not hits:
        return None
    if len(hits) > 1:
        raise AmbiguousChainError(
            f"{shop!r} matches several registry chains ({', '.join(sorted(hits))}) — make the registry keys disjoint"
        )
    return formats[hits[0]]


def describe_format(shop: str, fmt: dict[str, Any] | None) -> str:
    """Render a transcription checklist for *shop* (``None`` = unrecorded chain)."""
    if fmt is None:
        return (
            f"# Receipt format — {shop}\n\n"
            "  no receipt format recorded for this chain.\n\n"
            "  Transcribe conservatively and verify against the printed total: the line\n"
            "  items must sum to it, or the staging file will be refused. Once the layout\n"
            "  is known, add an entry to receipt-formats.json with a `source` naming the\n"
            "  receipt it came from — do not guess at the quirks below.\n"
            "  Unknowns to settle: " + "; ".join(_FIELD_LABELS.values()) + "."
        )
    lines = [f"# Receipt format — {shop}", ""]
    for key, label in _FIELD_LABELS.items():
        val = fmt.get(key)
        if val is None:
            lines.append(f"  {label}: (not recorded)")
        elif isinstance(val, dict):
            lines.append(f"  {label}:")
            lines.extend(f"    - {k}: {v}" for k, v in val.items())
        else:
            lines.append(f"  {label}: {val}")
        note = fmt.get(f"{key}_note")
        if note:
            lines.append(f"      note: {note}")
    if fmt.get("source"):
        lines += ["", f"  recorded from: {fmt['source']}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shop", nargs="?", help="Shop name (branch keys fine — matched to its chain)")
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    args = ap.parse_args(argv)

    formats = load_formats(args.registry)
    if not args.shop:
        print("# Recorded receipt formats\n")
        for chain in sorted(formats):
            print(f"  {chain}")
        if not formats:
            print("  (registry empty)")
        return 0
    print(describe_format(args.shop, format_for(formats, args.shop)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
