"""Tests for osm_resolve — find the OSM object of a shop that already exists.

No test touches the network: ``overpass_query`` is monkeypatched, and everything
else (query building, distance, ranking, output, cache write) is pure.
"""

import json

import pytest

from purchase_pipeline import osm_resolve
from purchase_pipeline.osm_resolve import (
    build_query,
    element_point,
    haversine_m,
    main,
    name_score,
    rank_candidates,
)
from purchase_pipeline.shop_osm import load_cache

# The 2026-07-24 Sozopol survey point (the fish shop).
LAT, LON = 42.41934022085787, 27.692148284820842

# A plausible Overpass answer: a supermarket right there, a Cyrillic-named shop
# next door, and an unnamed one. Ways carry `center`, nodes carry lat/lon.
ELEMENTS = [
    {
        "type": "way",
        "id": 1016681733,
        "center": {"lat": 42.419350, "lon": 27.692160},
        "tags": {"name": "Billa", "shop": "supermarket", "brand": "Billa"},
    },
    {
        "type": "node",
        "id": 55,
        "lat": 42.419300,
        "lon": 27.692100,
        "tags": {"name": "магазин за риба", "name:en": "Sozopol Fish", "shop": "seafood"},
    },
    {
        "type": "node",
        "id": 66,
        "lat": 42.419600,
        "lon": 27.692600,
        "tags": {"shop": "wine"},
    },
]


class TestBuildQuery:
    def test_asks_overpass_not_nominatim(self):
        q = build_query(LAT, LON, 50)
        assert "[out:json]" in q
        assert f"around:50,{LAT},{LON}" in q
        # `out tags center` is what makes ways/relations usable — without `center`
        # a way comes back with no coordinates and cannot be distance-ranked.
        assert "out tags center" in q

    def test_covers_more_than_shop_equals(self):
        """A fish shop may be tagged amenity/craft rather than shop=*."""
        q = build_query(LAT, LON, 50)
        for key in ("shop", "amenity", "craft"):
            assert f'["{key}"]' in q

    def test_radius_is_honoured(self):
        assert "around:30," in build_query(LAT, LON, 30)


class TestGeometry:
    def test_haversine_known_short_distance(self):
        # ~1 arc-second of latitude is ~31 m.
        d = haversine_m(LAT, LON, LAT + 1 / 3600, LON)
        assert 29 < d < 32

    def test_haversine_is_zero_for_the_same_point(self):
        assert haversine_m(LAT, LON, LAT, LON) == pytest.approx(0.0, abs=1e-6)

    def test_node_point(self):
        assert element_point(ELEMENTS[1]) == (42.419300, 27.692100)

    def test_way_uses_center(self):
        assert element_point(ELEMENTS[0]) == (42.419350, 27.692160)

    def test_missing_geometry_is_none(self):
        assert element_point({"type": "way", "id": 1, "tags": {}}) is None


class TestNameScore:
    def test_exact_name(self):
        assert name_score("Billa", {"name": "Billa"}) == pytest.approx(1.0)

    def test_case_and_whitespace_insensitive(self):
        assert name_score("  billa ", {"name": "Billa"}) == pytest.approx(1.0)

    def test_substring_scores_high(self):
        assert name_score("Billa", {"name": "Billa Sozopol"}) >= 0.9

    def test_matches_the_latin_transliteration_tag(self):
        """Bulgarian shops carry a Cyrillic ``name`` and a Latin ``name:en``.

        The name comes off the shopfront, or out of a receipt, in whichever
        script that shop uses; scoring only against ``name`` would score the
        Sozopol fish shop at ~0 against 'Sozopol Fish'.
        """
        tags = {"name": "магазин за риба", "name:en": "Sozopol Fish"}
        assert name_score("Sozopol Fish", tags) >= 0.9

    def test_matches_brand_and_operator(self):
        assert name_score("Billa", {"operator": "Billa Bulgaria EOOD"}) >= 0.5
        assert name_score("Billa", {"brand": "Billa"}) == pytest.approx(1.0)

    def test_unrelated_name_scores_low(self):
        assert name_score("Billa", {"name": "Praktiker"}) < 0.5

    def test_unnamed_element_scores_zero(self):
        assert name_score("Billa", {"shop": "wine"}) == 0.0


class TestRankCandidates:
    def _ranked(self, name: str):
        return rank_candidates(ELEMENTS, name=name, lat=LAT, lon=LON)

    def test_best_name_match_wins_regardless_of_distance(self):
        top = self._ranked("Sozopol Fish")[0]
        assert (top.osm_type, top.osm_id) == ("NODE", 55)

    def test_all_candidates_are_returned(self):
        assert len(self._ranked("Billa")) == 3

    def test_unnamed_pois_are_kept(self):
        """They are the whole point of the duplicate check task 4 depends on.

        An unnamed ``shop=seafood`` five metres away is exactly what must be seen
        before creating a node; filtering to named POIs would hide it.
        """
        ranked = self._ranked("Sozopol Fish")
        assert 66 in [c.osm_id for c in ranked]

    def test_distance_breaks_a_name_tie(self):
        elements = [
            {"type": "node", "id": 1, "lat": LAT + 0.001, "lon": LON, "tags": {"name": "Billa", "shop": "convenience"}},
            {"type": "node", "id": 2, "lat": LAT, "lon": LON, "tags": {"name": "Billa", "shop": "convenience"}},
        ]
        ranked = rank_candidates(elements, name="Billa", lat=LAT, lon=LON)
        assert [c.osm_id for c in ranked] == [2, 1]

    def test_distance_is_computed(self):
        top = self._ranked("Billa")[0]
        assert top.distance_m is not None
        assert top.distance_m < 5

    def test_type_is_upper_case_like_the_cache(self):
        assert {c.osm_type for c in self._ranked("Billa")} == {"WAY", "NODE"}

    def test_street_furniture_is_dropped(self):
        """A 50 m radius in a town centre is full of amenity=* that cannot be a shop."""
        elements = ELEMENTS + [
            {"type": "node", "id": 77, "lat": LAT, "lon": LON, "tags": {"amenity": "bench"}},
            {"type": "node", "id": 88, "lat": LAT, "lon": LON, "tags": {"amenity": "waste_basket"}},
            # …but a named amenity that *is* a business stays.
            {"type": "node", "id": 99, "lat": LAT, "lon": LON, "tags": {"amenity": "pharmacy", "name": "Аптека"}},
        ]
        ids = [c.osm_id for c in rank_candidates(elements, name="Billa", lat=LAT, lon=LON)]
        assert 77 not in ids
        assert 88 not in ids
        assert 99 in ids

    def test_untagged_element_is_dropped(self):
        elements = [{"type": "node", "id": 1, "lat": LAT, "lon": LON, "tags": {"barrier": "gate"}}]
        assert rank_candidates(elements, name="Billa", lat=LAT, lon=LON) == []

    def test_no_name_ranks_by_distance_alone(self):
        ranked = rank_candidates(ELEMENTS, name=None, lat=LAT, lon=LON)
        assert [c.osm_id for c in ranked] == [1016681733, 55, 66]


class TestMain:
    def _run(self, monkeypatch, capsys, *argv: str, elements=None):
        monkeypatch.setattr(osm_resolve, "overpass_query", lambda *a, **k: ELEMENTS if elements is None else elements)
        rc = main(list(argv))
        return rc, capsys.readouterr().out

    def test_lists_candidates_with_browsable_links(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys, "--lat", str(LAT), "--lon", str(LON), "--name", "Billa")
        assert rc == 0
        assert "https://www.openstreetmap.org/way/1016681733" in out
        assert "https://www.openstreetmap.org/node/55" in out

    def test_shows_the_tags_that_identify_a_shop(self, monkeypatch, capsys):
        _, out = self._run(monkeypatch, capsys, "--lat", str(LAT), "--lon", str(LON), "--name", "Billa")
        assert "shop=supermarket" in out

    def test_json_output_is_machine_readable(self, monkeypatch, capsys):
        _, out = self._run(monkeypatch, capsys, "--lat", str(LAT), "--lon", str(LON), "--name", "Billa", "--json")
        rows = json.loads(out)
        assert rows[0]["osm_type"] == "WAY"
        assert rows[0]["osm_id"] == 1016681733
        assert "score" in rows[0]
        assert "distance_m" in rows[0]

    def test_nothing_nearby_exits_one(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys, "--lat", str(LAT), "--lon", str(LON), "--name", "Billa", elements=[])
        assert rc == 1
        assert "no poi found" in out.lower()
        assert "unmapped" in out.lower()

    def test_does_not_write_the_cache_without_being_told_to(self, monkeypatch, capsys, tmp_path):
        cache = tmp_path / "shop-osm.json"
        self._run(
            monkeypatch,
            capsys,
            "--lat",
            str(LAT),
            "--lon",
            str(LON),
            "--name",
            "Billa",
            "--osm-cache",
            str(cache),
        )
        assert not cache.exists()

    def test_save_writes_the_confirmed_pick(self, monkeypatch, capsys, tmp_path):
        cache = tmp_path / "shop-osm.json"
        rc, out = self._run(
            monkeypatch,
            capsys,
            "--lat",
            str(LAT),
            "--lon",
            str(LON),
            "--name",
            "Billa",
            "--save-as",
            "Billa Sozopol",
            "--pick",
            "WAY:1016681733",
            "--osm-cache",
            str(cache),
        )
        assert rc == 0
        assert load_cache(cache)["Billa Sozopol"] == {
            "osm_type": "WAY",
            "osm_id": 1016681733,
            "name": "Billa",
        }

    def test_save_needs_no_query(self, monkeypatch, capsys, tmp_path):
        """``--save-as`` + ``--pick`` alone records an id confirmed by eye on osm.org."""
        cache = tmp_path / "shop-osm.json"

        def _boom(*a, **k):
            raise AssertionError("must not hit Overpass when only saving")

        monkeypatch.setattr(osm_resolve, "overpass_query", _boom)
        rc = main(["--save-as", "Billa Sozopol", "--pick", "WAY:123", "--osm-cache", str(cache)])
        assert rc == 0
        assert load_cache(cache)["Billa Sozopol"]["osm_id"] == 123

    def test_pick_must_be_one_of_the_listed_candidates(self, monkeypatch, capsys, tmp_path):
        """A --pick the query never returned is a typo, not a confirmation."""
        cache = tmp_path / "shop-osm.json"
        rc, out = self._run(
            monkeypatch,
            capsys,
            "--lat",
            str(LAT),
            "--lon",
            str(LON),
            "--name",
            "Billa",
            "--save-as",
            "Billa Sozopol",
            "--pick",
            "WAY:404",
            "--osm-cache",
            str(cache),
        )
        assert rc == 1
        assert "WAY:404" in out
        assert not cache.exists()

    def test_pick_without_save_as_is_an_error(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["--pick", "WAY:123", "--osm-cache", str(tmp_path / "c.json")])

    def test_save_as_without_pick_is_an_error(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["--save-as", "Billa Sozopol", "--osm-cache", str(tmp_path / "c.json")])

    def test_saving_a_bare_chain_key_is_refused(self, monkeypatch, capsys, tmp_path):
        cache = tmp_path / "shop-osm.json"
        rc, out = self._run(
            monkeypatch,
            capsys,
            "--save-as",
            "Billa",
            "--pick",
            "WAY:123",
            "--osm-cache",
            str(cache),
        )
        assert rc == 1
        assert "branch" in out.lower()
        assert not cache.exists()

    def test_query_needs_both_coordinates(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["--lat", str(LAT), "--osm-cache", str(tmp_path / "c.json")])

    def test_no_arguments_at_all_is_an_error(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["--osm-cache", str(tmp_path / "c.json")])
