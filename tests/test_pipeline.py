"""Tests for the process-shopping pipeline driver (pure helpers)."""

from purchase_pipeline.pipeline import (
    STAGES,
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
