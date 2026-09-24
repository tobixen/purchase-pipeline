"""Tests for check_grocery_ledger (the diary↔ledger coverage gate)."""

import json
from pathlib import Path

import pytest

from purchase_pipeline.check_grocery_ledger import (
    is_covered,
    ledger_cents_by_date,
    main,
    parse_amount,
    parse_diary,
)

DIARY = """\
## Friday 2026-07-17

* EUR 41.33 - groceries - ПаркМарт (weekly shop)
* EUR 12.00 - dining - some tavern
* EUR xx.xx - groceries - Lidl Varna (receipt not transcribed yet)

## Saturday 2026-07-18

* EUR 40.29 - groceries - Lidl Varna
* EUR 19.75 - maintenance - Praktiker Varna (paint brushes)
"""


class TestParseAmount:
    def test_number(self):
        assert parse_amount("41.33") == 41.33

    def test_comma_decimal(self):
        assert parse_amount("41,33") == 41.33

    @pytest.mark.parametrize("raw", ["xx.xx", "XX", "???", "5?"])
    def test_placeholder_is_none(self, raw):
        assert parse_amount(raw) is None


class TestParseDiary:
    def test_reads_lines_with_enclosing_date(self, tmp_path):
        f = tmp_path / "diary-2026.md"
        f.write_text(DIARY, encoding="utf-8")
        expenses = parse_diary(f)
        assert [e["type"] for e in expenses] == ["groceries", "dining", "groceries", "groceries", "maintenance"]
        assert expenses[0]["date"] == "2026-07-17"
        assert expenses[3]["date"] == "2026-07-18"
        assert expenses[2]["amount"] is None

    def test_accepts_a_directory_of_diary_files(self, tmp_path):
        """The carried-over bug: ``--diary ~/solveig`` crashed with IsADirectoryError.

        diary-md keeps one ``diary-YYYY.md`` per year and its own CLI takes
        ``--directory``, so a directory is what a caller naturally has at hand —
        and ``shopping_context.read_diary_text`` in this same package has
        accepted either since it was written. Two commands in one project must
        not disagree about what ``--diary`` means.
        """
        (tmp_path / "diary-2025.md").write_text(
            "## 2025-01-01\n\n* EUR 1.00 - groceries - old shop\n", encoding="utf-8"
        )
        (tmp_path / "diary-2026.md").write_text(DIARY, encoding="utf-8")
        expenses = parse_diary(tmp_path)
        # The newest diary file is the one read.
        assert [e["date"] for e in expenses] == ["2026-07-17"] * 3 + ["2026-07-18"] * 2

    def test_directory_without_diary_files_raises(self, tmp_path):
        with pytest.raises(OSError):
            parse_diary(tmp_path)


class TestLedger:
    ROWS = [
        {"date": "2026-07-17", "shop": "ПаркМарт", "total": 41.33},
        {"date": "2026-07-18", "shop": "Lidl Varna", "total": 40.29},
        {"date": "2026-07-18", "shop": "Lidl Varna", "total": 1.02},
    ]

    def _ledger(self, tmp_path: Path) -> Path:
        f = tmp_path / "purchases.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in self.ROWS) + "\n", encoding="utf-8")
        return f

    def test_groups_cents_by_date(self, tmp_path):
        by_date = ledger_cents_by_date(self._ledger(tmp_path))
        assert by_date[("2026-07-17", "EUR")] == [4133]
        assert sorted(by_date[("2026-07-18", "EUR")]) == [102, 4029]

    def test_missing_file_is_empty(self, tmp_path):
        assert ledger_cents_by_date(tmp_path / "nope.jsonl") == {}

    def test_covered_by_a_single_row(self, tmp_path):
        by_date = ledger_cents_by_date(self._ledger(tmp_path))
        assert is_covered({"amount": 40.29, "currency": "EUR", "date": "2026-07-18"}, by_date)

    def test_covered_by_a_subset_sum(self, tmp_path):
        # One diary line, two card charges in the same shop the same day.
        by_date = ledger_cents_by_date(self._ledger(tmp_path))
        assert is_covered({"amount": 41.31, "currency": "EUR", "date": "2026-07-18"}, by_date)

    def test_not_covered(self, tmp_path):
        by_date = ledger_cents_by_date(self._ledger(tmp_path))
        assert not is_covered({"amount": 7.00, "currency": "EUR", "date": "2026-07-18"}, by_date)

    def test_currency_must_match(self, tmp_path):
        """Same date, same number, different currency is not a match."""
        by_date = ledger_cents_by_date(self._ledger(tmp_path))
        assert not is_covered({"amount": 40.29, "currency": "BGN", "date": "2026-07-18"}, by_date)

    def test_non_eur_covered_by_same_currency_row(self, tmp_path):
        """The 2026-08 bug: a NOK diary line with a matching NOK ledger row was flagged."""
        f = tmp_path / "purchases.jsonl"
        f.write_text(
            json.dumps({"date": "2026-08-15", "shop": "Lyreco Tromsø", "total": 205.0, "currency": "NOK"}) + "\n",
            encoding="utf-8",
        )
        by_date = ledger_cents_by_date(f)
        assert is_covered({"amount": 205.0, "currency": "NOK", "date": "2026-08-15"}, by_date)
        assert not is_covered({"amount": 205.0, "currency": "EUR", "date": "2026-08-15"}, by_date)


class TestMain:
    def _run(self, tmp_path: Path, *extra: str, diary: str = DIARY) -> tuple[int, str]:
        import io
        from contextlib import redirect_stdout

        (tmp_path / "diary-2026.md").write_text(diary, encoding="utf-8")
        rows = [
            {"date": "2026-07-17", "shop": "ПаркМарт", "total": 41.33},
            {"date": "2026-07-18", "shop": "Lidl Varna", "total": 40.29},
        ]
        ledger = tmp_path / "purchases.jsonl"
        ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["--diary", str(tmp_path), "--ledger", str(ledger), *extra])
        return rc, buf.getvalue()

    def test_directory_diary_runs_end_to_end(self, tmp_path):
        """Both covered; only the xx.xx placeholder is left, so exit 1 with a warning."""
        rc, out = self._run(tmp_path)
        assert "not covered" not in out
        assert "unverifiable" in out
        assert rc == 1

    def test_all_covered_exits_zero(self, tmp_path):
        diary = "## 2026-07-18\n\n* EUR 40.29 - groceries - Lidl Varna\n"
        rc, out = self._run(tmp_path, diary=diary)
        assert rc == 0
        assert "all required expenses" in out

    def test_uncovered_is_reported(self, tmp_path):
        diary = "## 2026-07-18\n\n* EUR 99.99 - groceries - Lidl Varna\n"
        rc, out = self._run(tmp_path, diary=diary)
        assert rc == 1
        assert "no subset matches" in out

    def test_report_uses_the_diary_currency(self, tmp_path):
        diary = "## 2026-07-18\n\n* NOK 99.99 - equipment - Lyreco\n"
        rc, out = self._run(tmp_path, diary=diary)
        assert rc == 1
        assert "NOK 99.99" in out
        assert "EUR 99.99" not in out

    def test_since_filters_older_lines(self, tmp_path):
        rc, out = self._run(tmp_path, "--since", "2026-07-18")
        assert "unverifiable" not in out
        assert rc == 0
