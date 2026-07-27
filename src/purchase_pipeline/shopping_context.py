#!/usr/bin/env python3
"""Print the situational context for a shopping run — read-only.

The process-shopping skill needs, at the start of every trip, the same handful
of facts it otherwise rediscovers by grepping markdown and config: the shop's
cached Open Prices OSM object, one or two recent staging files for that shop (as
a schema/convention example to copy), and the shop's recent diary expense lines
(to mirror the wording/category convention). Gathering them with ad-hoc
``grep``/``cat``/``awk`` defeats the allowlist — a chained shell command can't be
pre-approved — so this single read-only command replaces all of it.

Usage::

    shopping_context.py "Praktiker"                       # everything for a shop
    shopping_context.py "Praktiker" --diary ~/solveig/diary-2026.md
    shopping_context.py "Praktiker" --diary ~/solveig    # directory also works
    shopping_context.py                                   # just list recent staging

It writes nothing and touches no network. Pair it with ``pipeline.py`` (which
runs the commit stages) — between them the only thing left for a human is to
review the staging file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_OSM_CACHE = Path.home() / ".config" / "inventory-md" / "shop-osm.json"


def load_osm_cache(path: Path) -> dict[str, Any]:
    """Load the shop→OSM cache, returning ``{}`` if it does not exist."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def shop_osm_candidates(cache: dict[str, Any], shop: str) -> list[str]:
    """Cache keys whose name overlaps *shop* by substring (either direction)."""
    if not shop:
        return []
    want = shop.casefold()
    return [key for key in cache if want in key.casefold() or key.casefold() in want]


def match_shop_osm(cache: dict[str, Any], shop: str) -> dict[str, Any] | None:
    """Find the cached OSM entry keyed exactly by *shop* (case-insensitive).

    Nothing else resolves. The cache is **branch-keyed** (``"Billa Varna ул.
    Андрей Сахаров"``), because an Open Prices price has to point at one real
    store — so a bare chain name like ``"Billa"`` is not an under-specified key,
    it is not a key at all.

    This used to fall back to an unambiguous substring match, on the theory that
    a single cached ``"Lidl Varna"`` makes ``"lidl"`` unambiguous. It does not:
    uniqueness in the cache is a fact about what has been visited before, not
    about which shop the caller means. On 2026-07-24 a trip to Billa **Sozopol**
    asked for ``"Billa"``, found the one cached Billa, and confidently returned
    the **Varna** branch's WAY:1016681733 — a wrong location, published
    silently, with no ambiguity for the old guard to trip on.

    Callers that get ``None`` should offer :func:`shop_osm_candidates` so the
    human can pick the exact branch key.
    """
    if not shop:
        return None
    want = shop.strip().casefold()
    for key, val in cache.items():
        if key.strip().casefold() == want:
            return val
    return None


def shop_of(path: Path) -> str | None:
    """Return the ``shop:`` field of a staging file without a YAML dependency."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"^shop:\s*(.+?)\s*$", text, re.MULTILINE)
    return m.group(1).strip().strip("'\"") if m else None


def find_staging_files(staging_dir: Path, shop: str | None, limit: int) -> list[Path]:
    """Recent ``shopping-*.yaml`` files (newest first), optionally filtered by shop.

    Filtering is a case-insensitive substring test against each file's ``shop:``
    field. Ordering is by filename descending — the files are date-stamped
    (``shopping-YYYY-MM-DD[-shop].yaml``), so lexical order is chronological.
    """
    files = sorted(staging_dir.glob("shopping-*.yaml"), key=lambda p: p.name, reverse=True)
    if shop:
        want = shop.casefold()
        files = [f for f in files if (s := shop_of(f)) and want in s.casefold()]
    return files[:limit]


def read_diary_text(diary_path: Path) -> str:
    """Read diary text from a file *or* a directory of diary-md files.

    diary-md keeps one ``diary-YYYY.md`` (or ``diary-YYYYMM.md``) file per period
    and its own CLI takes ``--directory``, so callers naturally have a directory
    at hand.  Accept either: a directory reads the newest diary file in it.
    """
    if diary_path.is_dir():
        files = sorted(diary_path.glob("diary*.md"))
        if not files:
            raise OSError(f"no diary*.md files in {diary_path}")
        return files[-1].read_text(encoding="utf-8")
    return diary_path.read_text(encoding="utf-8")


def grep_diary_lines(diary_text: str, shop: str) -> list[str]:
    """Return diary lines mentioning *shop* (case-insensitive), stripped."""
    want = shop.casefold()
    return [line.strip() for line in diary_text.splitlines() if want in line.casefold()]


def recent_ledger_rows(ledger_text: str, shop: str, limit: int) -> list[dict[str, Any]]:
    """Most recent ledger rows (JSONL) whose ``shop`` matches *shop* (substring).

    Surfacing these in the context command removes the reason an agent reaches
    for ``tail``/``grep`` on ``purchases.jsonl``: prior prices, EANs and the
    naming/category convention for this shop are right here, allowlisted.
    """
    want = shop.casefold()
    rows: list[dict[str, Any]] = []
    for line in ledger_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if want in str(row.get("shop", "")).casefold():
            rows.append(row)
    return rows[-limit:]


NEXT_COMMANDS = """\
Canonical next commands (run ONE per shell call — never chain with && / | / ;):
  0. receipt_formats.py "SHOP"   # BEFORE transcribing a photographed receipt:
         this chain's layout quirks (multiplier-line position, which address is
         the branch, how discounts/deposits print). Skipping it is how the
         2026-07-24 Billa trip billed three beers to a pack of cleaning cloths.
  1. extract_barcodes.py --best-before PHOTOS --json --out staging/barcodes-DATE.json
  2. shop_import.py --receipt R.json --barcodes-json staging/barcodes-DATE.json \\
         --out staging/shopping-DATE[-shop].yaml
  3. (review the staging file by hand — the one human gate; the line items MUST
      sum to the printed receipt_total or every consumer will refuse the file)
  4. pipeline.py staging/shopping-DATE[-shop].yaml            # dry run
  5. pipeline.py staging/shopping-DATE[-shop].yaml --commit   # ledger+inventory+tingbok+validate
  6. diary-update --directory DIARY_DIR -d DATE -a AMOUNT -c CUR -t TYPE --description "..."
  7. (optional, public) off_upload.py / openprices_publish.py --commit

To look something up in the inventory, DON'T grep inventory.md — use the parsed
JSON via the purpose-built commands (allowlisted, exact):
  · inventory-md lookup --match TERM   # find existing items by id/name (e.g. an EAN already stocked)
  · inventory-md container ID          # what's in a section/container (e.g. 'floating')
  · jq ... inventory.json              # anything else structured"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("shop", nargs="?", help="Shop name (or substring) to focus on")
    ap.add_argument("--staging-dir", type=Path, default=Path("staging"))
    ap.add_argument("--osm-cache", type=Path, default=DEFAULT_OSM_CACHE)
    ap.add_argument(
        "--diary", type=Path, help="Diary file, or a directory of diary-md files, to grep for prior expense lines"
    )
    ap.add_argument("--ledger", type=Path, help="Ledger JSONL to show recent rows for this shop")
    ap.add_argument("--limit", type=int, default=2, help="How many recent staging files to show")
    ap.add_argument("--no-content", action="store_true", help="List staging files without dumping their content")
    args = ap.parse_args(argv)

    print(f"# Shopping context{f' — {args.shop}' if args.shop else ''}\n")

    print("## Shop OSM (Open Prices --osm)")
    if args.shop:
        cache = load_osm_cache(args.osm_cache)
        hit = match_shop_osm(cache, args.shop)
        if hit:
            print(f"  {args.shop} → {hit.get('osm_type')}:{hit.get('osm_id')}")
        else:
            cands = shop_osm_candidates(cache, args.shop)
            if cands:
                print(f"  '{args.shop}' is not an exact cache key. Candidate branches:")
                for c in cands:
                    print(f"    - {c}")
                print(
                    "  The cache is keyed by branch, so re-run with the exact name above — but only after "
                    "checking it is the branch you actually visited. If it is a new branch of a known chain, "
                    "pass --osm TYPE:ID to openprices_publish once (it caches under the name you give)."
                )
            else:
                print(f"  not cached for '{args.shop}' — pass --osm TYPE:ID to openprices_publish once (it caches).")
    else:
        cache = load_osm_cache(args.osm_cache)
        for k, v in cache.items():
            print(f"  {k} → {v.get('osm_type')}:{v.get('osm_id')}")
        if not cache:
            print("  (cache empty)")
    print()

    print(f"## Recent staging files{f' for {args.shop}' if args.shop else ''}")
    files = find_staging_files(args.staging_dir, args.shop, args.limit)
    if not files:
        print("  (none found)")
    for f in files:
        print(f"\n### {f}")
        if not args.no_content:
            print(f.read_text(encoding="utf-8").rstrip())
    print()

    if args.ledger and args.shop:
        print(f"## Recent ledger rows for '{args.shop}' (prices / EANs / naming)")
        try:
            rows = recent_ledger_rows(args.ledger.read_text(encoding="utf-8"), args.shop, args.limit * 4)
            for r in rows:
                ean = r.get("ean") or "-"
                print(
                    f"  {r.get('date')}  {r.get('total')} {r.get('currency', '')}"
                    f"  ean:{ean}  {r.get('receipt_name', '')}"
                )
            if not rows:
                print("  (none)")
        except OSError as exc:
            print(f"  (could not read ledger: {exc})")
        print()

    if args.diary and args.shop:
        print(f"## Recent diary lines mentioning '{args.shop}'")
        try:
            lines = grep_diary_lines(read_diary_text(args.diary), args.shop)
            for line in lines[-8:]:
                print(f"  {line}")
            if not lines:
                print("  (none)")
        except OSError as exc:
            print(f"  (could not read diary: {exc})")
        print()

    print(NEXT_COMMANDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
