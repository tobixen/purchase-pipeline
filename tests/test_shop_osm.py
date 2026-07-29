"""Tests for shop_osm — the one place that knows the branch-keyed OSM cache."""

import json

import pytest

from purchase_pipeline.shop_osm import (
    is_branch_key,
    load_cache,
    match_shop_osm,
    osm_url,
    parse_osm_spec,
    save_entry,
    shop_osm_candidates,
)


class TestLoadCache:
    def test_missing_file_is_empty(self, tmp_path):
        assert load_cache(tmp_path / "nope.json") == {}

    def test_unparseable_file_is_empty(self, tmp_path):
        f = tmp_path / "shop-osm.json"
        f.write_text("{ not json", encoding="utf-8")
        assert load_cache(f) == {}

    def test_reads_entries(self, tmp_path):
        f = tmp_path / "shop-osm.json"
        f.write_text(json.dumps({"Lidl Varna": {"osm_type": "WAY", "osm_id": 1}}), encoding="utf-8")
        assert load_cache(f)["Lidl Varna"]["osm_id"] == 1


class TestParseOsmSpec:
    @pytest.mark.parametrize(
        ("spec", "want"),
        [
            ("WAY:1016681733", ("WAY", 1016681733)),
            ("way:1016681733", ("WAY", 1016681733)),
            (" node : 42 ", ("NODE", 42)),
            ("RELATION:7", ("RELATION", 7)),
        ],
    )
    def test_accepts(self, spec, want):
        assert parse_osm_spec(spec) == want

    @pytest.mark.parametrize("spec", ["1016681733", "WAY:", "WAY:abc", "shop:1", "WAY-1", ""])
    def test_rejects(self, spec):
        with pytest.raises(ValueError):
            parse_osm_spec(spec)


class TestIsBranchKey:
    def test_chain_plus_town_is_a_branch_key(self):
        assert is_branch_key("Billa Sozopol")
        assert is_branch_key("Billa Sozopol ул. Републиканска 5")

    def test_bare_chain_name_is_not(self):
        assert not is_branch_key("Billa")
        assert not is_branch_key("  Lidl  ")

    def test_empty_is_not(self):
        assert not is_branch_key("")


class TestSaveEntry:
    def test_repointing_a_differently_cased_key_is_refused(self, tmp_path):
        """match_shop_osm finds keys case- and whitespace-insensitively, so the
        repoint guard must too — or a second, shadowed key slips in."""
        path = tmp_path / "shop-osm.json"
        save_entry("Billa Sozopol ул. Републиканска 5", "WAY", 1, path=path)
        with pytest.raises(ValueError, match="already cached"):
            save_entry(" billa sozopol ул. републиканска 5", "WAY", 2, path=path)
        assert list(load_cache(path)) == ["Billa Sozopol ул. Републиканска 5"]

    def test_forced_repoint_replaces_the_existing_key(self, tmp_path):
        path = tmp_path / "shop-osm.json"
        save_entry("Billa Sozopol ул. Републиканска 5", "WAY", 1, path=path)
        save_entry("billa sozopol ул. републиканска 5", "WAY", 2, path=path, force=True)
        assert load_cache(path) == {"billa sozopol ул. републиканска 5": {"osm_type": "WAY", "osm_id": 2}}

    def test_creates_the_file_and_parent_dirs(self, tmp_path):
        path = tmp_path / "config" / "inventory-md" / "shop-osm.json"
        save_entry("Billa Sozopol", "WAY", 123, path=path)
        assert json.loads(path.read_text(encoding="utf-8")) == {"Billa Sozopol": {"osm_type": "WAY", "osm_id": 123}}

    def test_keeps_other_entries(self, tmp_path):
        path = tmp_path / "shop-osm.json"
        path.write_text(json.dumps({"Lidl Varna": {"osm_type": "WAY", "osm_id": 1}}), encoding="utf-8")
        save_entry("Billa Sozopol", "NODE", 2, path=path)
        assert set(load_cache(path)) == {"Lidl Varna", "Billa Sozopol"}

    def test_stores_extra_fields(self, tmp_path):
        path = tmp_path / "shop-osm.json"
        save_entry("Billa Sozopol", "WAY", 123, path=path, extra={"name": "Billa"})
        assert load_cache(path)["Billa Sozopol"]["name"] == "Billa"

    def test_rewriting_the_same_object_is_idempotent(self, tmp_path):
        path = tmp_path / "shop-osm.json"
        save_entry("Billa Sozopol", "WAY", 123, path=path)
        save_entry("Billa Sozopol", "WAY", 123, path=path)
        assert load_cache(path)["Billa Sozopol"]["osm_id"] == 123

    def test_refuses_to_repoint_an_existing_key(self, tmp_path):
        """Silently repointing a key is how a published price moves shop."""
        path = tmp_path / "shop-osm.json"
        save_entry("Billa Sozopol", "WAY", 123, path=path)
        with pytest.raises(ValueError, match="already"):
            save_entry("Billa Sozopol", "WAY", 999, path=path)
        assert load_cache(path)["Billa Sozopol"]["osm_id"] == 123

    def test_force_repoints(self, tmp_path):
        path = tmp_path / "shop-osm.json"
        save_entry("Billa Sozopol", "WAY", 123, path=path)
        save_entry("Billa Sozopol", "WAY", 999, path=path, force=True)
        assert load_cache(path)["Billa Sozopol"]["osm_id"] == 999

    def test_refuses_a_bare_chain_key(self, tmp_path):
        """A bare chain key would *exactly* match, so it defeats the task-1 fix.

        ``match_shop_osm`` was hardened to require an exact key precisely so that
        ``"Billa"`` resolves to nothing. Storing ``"Billa"`` as a key makes it
        resolve again — silently, and to whichever branch happened to be saved
        first. That is the 2026-07-24 Sozopol bug reintroduced through the cache
        rather than through the matcher.
        """
        path = tmp_path / "shop-osm.json"
        with pytest.raises(ValueError, match="branch"):
            save_entry("Billa", "WAY", 123, path=path)
        assert not path.exists()

    def test_force_allows_a_single_word_key(self, tmp_path):
        # A genuinely single-word, one-of-a-kind shop is the caller's call.
        path = tmp_path / "shop-osm.json"
        save_entry("Praktiker", "WAY", 5, path=path, force=True)
        assert load_cache(path)["Praktiker"]["osm_id"] == 5

    def test_saved_entry_resolves_through_the_matcher(self, tmp_path):
        path = tmp_path / "shop-osm.json"
        save_entry("Billa Sozopol", "WAY", 123, path=path)
        assert match_shop_osm(load_cache(path), "billa sozopol")["osm_id"] == 123
        assert match_shop_osm(load_cache(path), "Billa") is None
        assert shop_osm_candidates(load_cache(path), "Billa") == ["Billa Sozopol"]


class TestOsmUrl:
    @pytest.mark.parametrize(
        ("osm_type", "osm_id", "want"),
        [
            ("NODE", 1, "https://www.openstreetmap.org/node/1"),
            ("WAY", 1016681733, "https://www.openstreetmap.org/way/1016681733"),
            ("RELATION", 7, "https://www.openstreetmap.org/relation/7"),
        ],
    )
    def test_links_are_browsable(self, osm_type, osm_id, want):
        assert osm_url(osm_type, osm_id) == want
