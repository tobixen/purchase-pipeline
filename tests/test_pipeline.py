"""Tests for the process-shopping pipeline driver."""

import pytest

from purchase_pipeline import pipeline
from purchase_pipeline.pipeline import (
    STAGES,
    main,
    next_pending,
    read_status,
    set_status_in_text,
)

STAGING = """\
# header comment
session: '2026-06-18'

status:
  ledger: pending
  diary: pending          # SPLIT note
  inventory: pending
  tingbok_push: skipped
  off_upload: skipped
  open_prices: skipped

shop: Praktiker Varna
inventory: should-not-be-touched   # decoy: not under status block
"""


class TestReadStatus:
    def test_reads_status_block(self):
        import yaml

        st = read_status(yaml.safe_load(STAGING))
        assert st["ledger"] == "pending"
        assert st["tingbok_push"] == "skipped"

    def test_missing_status_is_empty(self):
        assert read_status({"shop": "x"}) == {}


class TestNextPending:
    def test_pending_stages_in_order(self):
        status = {"ledger": "pending", "inventory": "done", "tingbok_push": "skipped"}
        names = [s.name for s in next_pending(status, STAGES)]
        # ledger pending -> included; inventory done -> excluded; tingbok skipped -> excluded
        assert "ledger" in names
        assert "inventory" not in names
        assert "tingbok" not in names

    def test_missing_key_treated_as_pending(self):
        names = [s.name for s in next_pending({}, STAGES)]
        assert "ledger" in names
        assert "inventory" in names


class TestSetStatus:
    def test_updates_value_within_block(self):
        out = set_status_in_text(STAGING, "ledger", "done")
        assert "  ledger: done" in out
        # other lines untouched
        assert "  inventory: pending" in out

    def test_preserves_inline_comment(self):
        out = set_status_in_text(STAGING, "diary", "done")
        assert "diary: done" in out
        assert "# SPLIT note" in out

    def test_does_not_touch_decoy_outside_block(self):
        out = set_status_in_text(STAGING, "inventory", "done")
        # the top-level decoy `inventory:` line stays put
        assert "inventory: should-not-be-touched" in out
        # the status-block inventory got flipped
        assert "  inventory: done" in out

    def test_roundtrip_yaml_still_valid(self):
        import yaml

        out = set_status_in_text(STAGING, "ledger", "done")
        data = yaml.safe_load(out)
        assert data["status"]["ledger"] == "done"
        assert data["inventory"] == "should-not-be-touched"


def test_stages_cover_commit_pipeline():
    names = {s.name for s in STAGES}
    assert {"ledger", "inventory", "tingbok"} <= names
    # diary and publishing are deliberately NOT auto-driven
    assert "diary" not in names
    assert "off_upload" not in names


# --- batch mode ------------------------------------------------------------
#
# Three shops in one day (2026-07-24: Billa, a beach kiosk, a fish shop) means
# three staging files, and validating each one costs about two minutes of
# `inventory-md parse` + quality gate over the *whole* inventory — work that
# says the same thing three times over. Batch mode runs the commit stages per
# file and validates once, at the end.

MINIMAL_STAGING = """\
session: '2026-07-24'

status:
  ledger: pending
  inventory: pending
  tingbok_push: skipped
  open_prices: pending

shop: {shop}
"""


@pytest.fixture
def staging_files(tmp_path):
    """Three reviewed staging files, as one day's three shops would leave behind."""
    paths = []
    for shop in ("Billa Sozopol", "Beach kiosk", "Sozopol Fish"):
        p = tmp_path / f"shopping-{shop.split()[0].lower()}.yaml"
        p.write_text(MINIMAL_STAGING.format(shop=shop), encoding="utf-8")
        paths.append(p)
    return paths


class Runs(list):
    """The commands the driver would have run, plus a way to make one fail."""

    def __init__(self):
        super().__init__()
        self.failures: dict[str, int] = {}


@pytest.fixture
def runs(monkeypatch):
    """Record every command the driver would run; nothing is executed."""
    recorded = Runs()

    def fake_run(cmd):
        recorded.append(cmd)
        for needle, rc in recorded.failures.items():
            if any(needle in part for part in cmd):
                return rc
        return 0

    monkeypatch.setattr(pipeline, "_run", fake_run)
    return recorded


def _stage_runs(recorded):
    """(module, staging file) for each stage sub-process that was run."""
    return [(cmd[2].rsplit(".", 1)[-1], next(a for a in cmd if a.endswith(".yaml"))) for cmd in recorded if "-m" in cmd]


def _validations(recorded):
    return [cmd for cmd in recorded if cmd[0] in ("inventory-md", pipeline.CHECK_QUALITY_CMD)]


class TestBatchMode:
    def test_runs_the_stages_of_every_file(self, staging_files, runs, tmp_path):
        rc = main([*map(str, staging_files), "--commit", "--inventory", str(tmp_path / "inventory.md")])
        assert rc == 0
        modules = [m for m, _ in _stage_runs(runs)]
        # ledger + inventory for each of the three files; tingbok is `skipped`
        assert modules == ["ledger", "inventory_import"] * 3

    def test_validates_once_at_the_end(self, staging_files, runs, tmp_path):
        main([*map(str, staging_files), "--commit", "--inventory", str(tmp_path / "inventory.md")])
        validations = _validations(runs)
        assert [v[0] for v in validations] == ["inventory-md", pipeline.CHECK_QUALITY_CMD]
        # and only after every file's stages have run
        assert runs.index(validations[0]) > max(i for i, c in enumerate(runs) if "-m" in c)

    def test_each_file_gets_its_own_status_updated(self, staging_files, runs, tmp_path):
        main([*map(str, staging_files), "--commit", "--inventory", str(tmp_path / "inventory.md")])
        for p in staging_files:
            text = p.read_text(encoding="utf-8")
            assert "  ledger: done" in text
            assert "  inventory: done" in text
            assert "  tingbok_push: skipped" in text

    def test_a_failure_stops_before_the_later_files(self, staging_files, runs, tmp_path):
        runs.failures["purchase_pipeline.inventory_import"] = 3
        rc = main([*map(str, staging_files), "--commit", "--inventory", str(tmp_path / "inventory.md")])
        assert rc == 3
        # first file: ledger ran and was marked done, inventory failed and was not
        assert "  ledger: done" in staging_files[0].read_text(encoding="utf-8")
        assert "  inventory: pending" in staging_files[0].read_text(encoding="utf-8")
        # the untouched files are untouched, and nothing was validated
        assert staging_files[1].read_text(encoding="utf-8") == MINIMAL_STAGING.format(shop="Beach kiosk")
        assert _validations(runs) == []

    def test_no_validate_skips_the_gate(self, staging_files, runs, tmp_path):
        rc = main(
            [*map(str, staging_files), "--commit", "--no-validate", "--inventory", str(tmp_path / "inventory.md")]
        )
        assert rc == 0
        assert _stage_runs(runs)  # the stages themselves still ran
        assert _validations(runs) == []

    def test_dry_run_previews_every_file_and_validates_nothing(self, staging_files, runs, tmp_path, capsys):
        rc = main([*map(str, staging_files), "--inventory", str(tmp_path / "inventory.md")])
        assert rc == 0
        out = capsys.readouterr().out
        for p in staging_files:
            assert str(p) in out
        assert _validations(runs) == []
        assert "  ledger: pending" in staging_files[0].read_text(encoding="utf-8")

    def test_single_file_still_works(self, staging_files, runs, tmp_path):
        rc = main([str(staging_files[0]), "--commit", "--inventory", str(tmp_path / "inventory.md")])
        assert rc == 0
        assert [m for m, _ in _stage_runs(runs)] == ["ledger", "inventory_import"]
        assert [v[0] for v in _validations(runs)] == ["inventory-md", pipeline.CHECK_QUALITY_CMD]

    def test_from_stage_applies_to_every_file(self, staging_files, runs, tmp_path):
        """--from restarts each file at that stage — including the ones already `done`."""
        for p in staging_files:
            p.write_text(
                MINIMAL_STAGING.format(shop="x").replace("ledger: pending", "ledger: done"),
                encoding="utf-8",
            )
        main([*map(str, staging_files), "--commit", "--from", "ledger", "--inventory", str(tmp_path / "inventory.md")])
        assert [m for m, _ in _stage_runs(runs)] == ["ledger", "inventory_import"] * 3

    def test_from_stage_does_not_revive_a_skipped_stage(self, staging_files, runs, tmp_path):
        """`skipped` outranks --from: a restart re-runs work, it does not overrule the reviewer.

        Batch mode is what makes this matter. `--from ledger` over a day's three
        files, one of them a hardware trip marked `tingbok_push: skipped`, would
        otherwise push that trip to tingbok as a side effect of re-running the
        ledger stage on the other two.
        """
        main([*map(str, staging_files), "--commit", "--from", "ledger", "--inventory", str(tmp_path / "inventory.md")])
        assert "tingbok_push" not in [m for m, _ in _stage_runs(runs)]
        for p in staging_files:
            assert "  tingbok_push: skipped" in p.read_text(encoding="utf-8")
