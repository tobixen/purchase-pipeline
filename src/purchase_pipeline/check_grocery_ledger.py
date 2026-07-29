#!/usr/bin/env python3
"""Personal bookkeeping check: every *grocery* (and *equipment*) diary expense
must have a matching ledger entry; dining and most other classes are exempt.

It reads BOTH the diary (~/solveig) and the purchases ledger (~/regnskap), and
imports neither the diary-md nor the inventory-md package: those are independent
projects that must not depend on each other, nor on one person's bookkeeping
policy.

That combination is why this lived loose in ~/bin for a while — it belonged to
neither project it reads from. It belongs here: the ledger is this project's,
and reconciling it against the diary is a question about purchases, which is
what this project answers.

Rule (per tobixen, 2026-07-21):
  - `groceries`  -> MUST have a ledger entry (always).
  - `equipment`  -> MUST have a ledger entry (always).
  - `maintenance`-> ledger optional (not flagged).
  - everything else (dining, child-support, family, harbour due, ...) -> exempt.

A grocery/equipment diary line is considered "covered" when the ledger holds, on
the same date, a shop whose line items sum to the diary amount (± a small
tolerance). Shop names are not matched (the diary description and the ledger
`shop` field are worded differently); date + summed total is enough and robust.

Lines whose amount is a placeholder (`xx.xx`, `XX`, `???`, `5?`) can't be
verified and are flagged separately as "unverifiable" — they usually mark a
still-open reconciliation.

Exit status: 0 if everything required is covered, 1 if anything is flagged.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Expense classes that MUST carry a ledger entry.
REQUIRE_LEDGER = {"groceries", "equipment"}

# EUR tolerance when matching a diary total to a ledger shop-day sum.
TOLERANCE = 0.02

DEFAULT_DIARY = Path.home() / "solveig" / "diary-2026.md"
DEFAULT_LEDGER = Path.home() / "regnskap" / "purchases.jsonl"

# "## Friday 2026-07-17 …", "### 2026-07-17", etc. — any header carrying a date.
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_HEADER_RE = re.compile(r"^#{1,6}\s")
# "* EUR 41.33 - groceries - ПаркМарт (…)"  (amount may be a placeholder)
_EXPENSE_RE = re.compile(r"^\*\s*([A-Za-z]{3})\s+(\S+)\s*-\s*([\w-]+)\s*-\s*(.*)$")


def parse_amount(raw: str) -> float | None:
    """Return the numeric EUR amount, or None for a placeholder (xx.xx/XX/???)."""
    cleaned = raw.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_diary(path: Path) -> list[dict]:
    """Extract expense lines with the date of the enclosing day header."""
    expenses: list[dict] = []
    current_date: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if _HEADER_RE.match(line):
            m = _DATE_RE.search(line)
            if m:
                current_date = m.group(1)
            continue
        m = _EXPENSE_RE.match(line)
        if not m:
            continue
        currency, amount_raw, etype, desc = m.groups()
        expenses.append(
            {
                "date": current_date,
                "currency": currency.upper(),
                "amount": parse_amount(amount_raw),
                "amount_raw": amount_raw,
                "type": etype.lower(),
                "description": desc.strip(),
                "line": line.strip(),
            }
        )
    return expenses


def ledger_cents_by_date(path: Path) -> dict[str, list[int]]:
    """Ledger row totals (in integer cents) grouped by purchase date.

    Grouped by date only, not shop: a diary line is one card charge / receipt,
    but a shop can have several charges in a day (Lidl 40.29 + 1.02) and a day
    can span several shops. So we ask whether the diary amount is a *subset sum*
    of the day's ledger rows — which a bare per-(date,shop) sum can't answer.
    """
    by_date: dict[str, list[int]] = defaultdict(list)
    if not path.exists():
        return by_date
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        row = json.loads(raw)
        date = (row.get("date") or "")[:10]
        total = row.get("total")
        if date and total is not None:
            by_date[date].append(round(float(total) * 100))
    return by_date


def _reachable_sums(cents: list[int]) -> set[int]:
    """All non-empty subset sums of *cents* (boolean DP)."""
    reachable = {0}
    for c in cents:
        reachable |= {r + c for r in reachable}
    reachable.discard(0)
    return reachable


def is_covered(exp: dict, by_date: dict[str, list[int]]) -> bool:
    """The diary amount is a subset sum of that date's ledger rows (± tolerance)."""
    if exp["amount"] is None or exp["currency"] != "EUR":
        return False
    rows = by_date.get(exp["date"])
    if not rows:
        return False
    target = round(exp["amount"] * 100)
    tol = round(TOLERANCE * 100)
    reachable = _reachable_sums(rows)
    return any(abs(r - target) <= tol for r in reachable)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--diary", type=Path, default=DEFAULT_DIARY)
    ap.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    ap.add_argument(
        "--require",
        default=",".join(sorted(REQUIRE_LEDGER)),
        help="comma-separated expense types that must have a ledger",
    )
    ap.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="only check diary lines dated on/after this (the per-item "
        "ledger workflow is recent; older trips were bank-reconciled "
        "without itemisation)",
    )
    ap.add_argument("-q", "--quiet", action="store_true", help="only print problems")
    ns = ap.parse_args()

    require = {t.strip().lower() for t in ns.require.split(",") if t.strip()}
    expenses = parse_diary(ns.diary)
    if ns.since:
        expenses = [e for e in expenses if e["date"] and e["date"] >= ns.since]
    by_date = ledger_cents_by_date(ns.ledger)

    missing: list[dict] = []
    unverifiable: list[dict] = []
    for exp in expenses:
        if exp["type"] not in require:
            continue
        if exp["amount"] is None:
            unverifiable.append(exp)
        elif not is_covered(exp, by_date):
            missing.append(exp)

    checked = sum(1 for e in expenses if e["type"] in require)
    if not ns.quiet:
        print(f"Checked {checked} '{'/'.join(sorted(require))}' diary lines against {len(by_date)} ledger days.")

    if missing:
        print(f"\n❌ {len(missing)} required expense(s) not covered by the ledger:")
        for e in missing:
            rows = by_date.get(e["date"]) or []
            if not rows:
                reason = "no ledger rows on this date"
            else:
                reason = f"{len(rows)} ledger row(s) on this date sum to EUR {sum(rows) / 100:.2f} — no subset matches"
            print(f"  {e['date']}  EUR {e['amount']:.2f}  [{e['type']}]  {e['description']}")
            print(f"       ↳ {reason}")
    if unverifiable:
        print(
            f"\n⚠️  {len(unverifiable)} required expense(s) with an unverifiable amount "
            f"(placeholder — likely an open reconciliation):"
        )
        for e in unverifiable:
            print(f"  {e['date']}  EUR {e['amount_raw']}  [{e['type']}]  {e['description']}")

    if not missing and not unverifiable:
        if not ns.quiet:
            print("✓ all required expenses have a ledger entry.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
