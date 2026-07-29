"""Tests for osm_add_shop — create ONE surveyed shop node.

No test touches OpenStreetMap. The Overpass duplicate query is monkeypatched and
the upload goes to a fake api object, so the guards can be exercised without the
one thing that must never happen by accident: a write to the live map.
"""

import pytest

from purchase_pipeline import osm_add_shop
from purchase_pipeline.osm_add_shop import (
    CREATED_BY,
    blocking_duplicates,
    build_node_tags,
    changeset_tags,
    decimal_places,
    main,
    parse_tag,
    upload_node,
)
from purchase_pipeline.osm_resolve import OverpassError, rank_candidates

LAT, LON = 42.41934022085787, 27.692148284820842

# What Overpass really returns around the Sozopol fish shop (see TODO task 4).
NEARBY = [
    {
        "type": "node",
        "id": 14048113335,
        "lat": 42.419350,
        "lon": 27.692160,
        "tags": {"name": "Sozopol Fish", "name:en": "Sozopol Fish", "shop": "seafood"},
    },
    {
        "type": "way",
        "id": 301280221,
        "center": {"lat": 42.419330, "lon": 27.692100},
        "tags": {"name": "Sozopol marketplace", "amenity": "marketplace"},
    },
    {
        "type": "node",
        "id": 5002039921,
        "lat": 42.419500,
        "lon": 27.692300,
        "tags": {"name": "Грив-56", "shop": "wine"},
    },
]


def _ranked(elements, name):
    return rank_candidates(elements, name=name, lat=LAT, lon=LON)


class TestDecimalPlaces:
    @pytest.mark.parametrize(
        ("text", "want"),
        [("42.41934022085787", 14), ("42.41934", 5), ("42.4", 1), ("42", 0), ("-27.69215", 5)],
    )
    def test_counts_digits_after_the_point(self, text, want):
        assert decimal_places(text) == want


class TestParseTag:
    def test_splits_on_the_first_equals(self):
        assert parse_tag("opening_hours=Mo-Sa 08:00-18:00") == ("opening_hours", "Mo-Sa 08:00-18:00")

    @pytest.mark.parametrize("spec", ["novalue", "=novalue", "key=", ""])
    def test_rejects_half_a_tag(self, spec):
        with pytest.raises(ValueError):
            parse_tag(spec)


class TestBuildNodeTags:
    def test_name_and_shop(self):
        assert build_node_tags(name="Sozopol Fish", shop="seafood") == {
            "name": "Sozopol Fish",
            "shop": "seafood",
            "source": "survey",
        }

    def test_source_is_survey_and_not_overridable(self):
        """The node claims to come from a survey; a caller must not relabel that."""
        with pytest.raises(ValueError, match="source"):
            build_node_tags(name="X", shop="seafood", extra=[("source", "Bing")])

    def test_extra_tags_are_kept(self):
        tags = build_node_tags(name="X", shop="seafood", extra=[("opening_hours", "24/7")])
        assert tags["opening_hours"] == "24/7"

    def test_a_poi_key_is_required(self):
        """A node with a name and no kind is not a shop; refuse rather than guess."""
        with pytest.raises(ValueError, match="shop"):
            build_node_tags(name="X")

    def test_a_poi_key_may_arrive_through_extra(self):
        tags = build_node_tags(name="Аптека", extra=[("amenity", "pharmacy")])
        assert tags["amenity"] == "pharmacy"

    def test_empty_name_is_refused(self):
        with pytest.raises(ValueError, match="name"):
            build_node_tags(name="   ", shop="seafood")


class TestChangesetTags:
    def test_is_honest_about_who_and_how(self):
        tags = changeset_tags(comment="Add Sozopol Fish")
        assert tags["created_by"] == CREATED_BY
        assert tags["source"] == "survey"
        assert tags["comment"] == "Add Sozopol Fish"

    def test_comment_is_required(self):
        with pytest.raises(ValueError):
            changeset_tags(comment="  ")


class TestBlockingDuplicates:
    def test_the_same_shop_blocks_on_name(self):
        blockers = blocking_duplicates(
            _ranked(NEARBY, "Sozopol Fish"), name="Sozopol Fish", poi_tags={"shop": "seafood"}, acknowledged=set()
        )
        assert [c.osm_id for c, _ in blockers] == [14048113335]

    def test_an_enclosing_marketplace_does_not_block(self):
        """A marketplace way around a fish stall is not a duplicate of the stall.

        This is the distinction that makes the check usable at all: the Sozopol
        shop sits inside `WAY:301280221 amenity=marketplace`, 10 m from the survey
        point — closer than the shop itself. Blocking on proximity alone would
        make every market stall unmappable.
        """
        blockers = blocking_duplicates(
            _ranked(NEARBY, "Some New Stall"),
            name="Some New Stall",
            poi_tags={"shop": "vegetables"},
            acknowledged=set(),
        )
        assert 301280221 not in [c.osm_id for c, _ in blockers]

    def test_a_different_shop_of_another_kind_does_not_block(self):
        blockers = blocking_duplicates(
            _ranked(NEARBY, "Some New Stall"),
            name="Some New Stall",
            poi_tags={"shop": "vegetables"},
            acknowledged=set(),
        )
        assert 5002039921 not in [c.osm_id for c, _ in blockers]

    def test_same_kind_blocks_even_with_an_unrelated_name(self):
        """The important case: someone else mapped it under a name you don't use."""
        blockers = blocking_duplicates(
            _ranked(NEARBY, "Рибарски магазин"),
            name="Рибарски магазин",
            poi_tags={"shop": "seafood"},
            acknowledged=set(),
        )
        assert 14048113335 in [c.osm_id for c, _ in blockers]

    def test_same_kind_blocks_when_unnamed(self):
        elements = [{"type": "node", "id": 7, "lat": LAT, "lon": LON, "tags": {"shop": "seafood"}}]
        blockers = blocking_duplicates(
            _ranked(elements, "Anything"), name="Anything", poi_tags={"shop": "seafood"}, acknowledged=set()
        )
        assert [c.osm_id for c, _ in blockers] == [7]

    def test_acknowledgement_clears_a_specific_blocker(self):
        blockers = blocking_duplicates(
            _ranked(NEARBY, "Sozopol Fish"),
            name="Sozopol Fish",
            poi_tags={"shop": "seafood"},
            acknowledged={("NODE", 14048113335)},
        )
        assert blockers == []

    def test_acknowledging_one_does_not_clear_another(self):
        elements = NEARBY + [{"type": "node", "id": 8, "lat": LAT, "lon": LON, "tags": {"shop": "seafood"}}]
        blockers = blocking_duplicates(
            _ranked(elements, "Sozopol Fish"),
            name="Sozopol Fish",
            poi_tags={"shop": "seafood"},
            acknowledged={("NODE", 14048113335)},
        )
        assert [c.osm_id for c, _ in blockers] == [8]

    def test_each_blocker_carries_a_reason(self):
        blockers = blocking_duplicates(
            _ranked(NEARBY, "Sozopol Fish"), name="Sozopol Fish", poi_tags={"shop": "seafood"}, acknowledged=set()
        )
        assert blockers[0][1]


class FakeApi:
    """Records the changeset dance instead of performing it."""

    def __init__(self, node_id: int = 999):
        self.node_id = node_id
        self.calls: list[tuple] = []
        self.open_changeset: int | None = None

    def changeset_create(self, tags):
        self.calls.append(("changeset_create", tags))
        self.open_changeset = 4242
        return 4242

    def node_create(self, node_data):
        assert self.open_changeset is not None, "node_create outside an open changeset"
        self.calls.append(("node_create", node_data))
        return {**node_data, "id": self.node_id, "version": 1}

    def changeset_close(self):
        self.calls.append(("changeset_close",))
        self.open_changeset = None


class TestUploadNode:
    def test_opens_creates_and_closes_in_order(self):
        api = FakeApi()
        node = upload_node(
            LAT, LON, {"name": "X", "shop": "seafood", "source": "survey"}, changeset={"comment": "c"}, api=api
        )
        assert [c[0] for c in api.calls] == ["changeset_create", "node_create", "changeset_close"]
        assert node["id"] == 999

    def test_sends_the_coordinates_and_tags_verbatim(self):
        api = FakeApi()
        tags = {"name": "X", "shop": "seafood", "source": "survey"}
        upload_node(LAT, LON, tags, changeset={"comment": "c"}, api=api)
        _, node_data = api.calls[1]
        assert node_data["lat"] == LAT
        assert node_data["lon"] == LON
        assert node_data["tag"] == tags

    def test_the_changeset_is_closed_even_when_the_create_fails(self):
        """A dangling open changeset is litter on someone else's database."""

        class Boom(FakeApi):
            def node_create(self, node_data):
                raise RuntimeError("api down")

        api = Boom()
        with pytest.raises(RuntimeError):
            upload_node(LAT, LON, {"shop": "seafood"}, changeset={"comment": "c"}, api=api)
        assert api.calls[-1] == ("changeset_close",)


class TestMain:
    BASE = ["--lat", str(LAT), "--lon", str(LON), "--name", "Sozopol Fish", "--shop", "seafood"]

    def _run(self, monkeypatch, capsys, *argv, elements=None, api=None, coverage=True):
        monkeypatch.setattr(osm_add_shop, "overpass_query", lambda *a, **k: NEARBY if elements is None else elements)
        monkeypatch.setattr(osm_add_shop, "_api", lambda *a, **k: api or FakeApi())
        if coverage is not None:
            monkeypatch.setattr(osm_add_shop, "has_coverage", lambda *a, **k: coverage)
        rc = main(list(argv))
        return rc, capsys.readouterr().out

    def test_refuses_when_a_duplicate_is_found(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys, *self.BASE)
        assert rc == 1
        assert "NODE:14048113335" in out
        assert "https://www.openstreetmap.org/node/14048113335" in out

    def test_refusal_happens_before_any_upload(self, monkeypatch, capsys):
        api = FakeApi()
        rc, _ = self._run(monkeypatch, capsys, *self.BASE, "--commit", api=api)
        assert rc == 1
        assert api.calls == []

    def test_dry_run_on_a_clear_site_writes_nothing(self, monkeypatch, capsys):
        api = FakeApi()
        rc, out = self._run(monkeypatch, capsys, *self.BASE, elements=[], api=api)
        assert rc == 0
        assert api.calls == []
        assert "shop=seafood" in out
        assert "source=survey" in out
        assert "--commit" in out

    def test_commit_on_a_clear_site_creates_the_node(self, monkeypatch, capsys):
        api = FakeApi(node_id=12345)
        monkeypatch.setattr(osm_add_shop, "load_token", lambda **k: "tok")
        rc, out = self._run(monkeypatch, capsys, *self.BASE, "--commit", elements=[], api=api)
        assert rc == 0
        assert [c[0] for c in api.calls] == ["changeset_create", "node_create", "changeset_close"]
        assert "NODE:12345" in out
        # The whole reason for adding a shop is to publish a price against it.
        assert "osm-resolve" in out
        assert "--pick NODE:12345" in out

    def test_commit_without_a_token_refuses(self, monkeypatch, capsys):
        api = FakeApi()
        monkeypatch.setattr(osm_add_shop, "load_token", lambda **k: None)
        rc, out = self._run(monkeypatch, capsys, *self.BASE, "--commit", elements=[], api=api)
        assert rc == 1
        assert "osm-auth" in out
        assert api.calls == []

    def test_acknowledging_the_blocker_allows_the_dry_run(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys, *self.BASE, "--not-a-duplicate-of", "NODE:14048113335")
        assert rc == 0
        assert "shop=seafood" in out

    def test_a_stale_acknowledgement_is_reported(self, monkeypatch, capsys):
        """An id that is no longer a blocker means the review was of a different world."""
        rc, out = self._run(
            monkeypatch, capsys, *self.BASE, "--not-a-duplicate-of", "NODE:14048113335", "--not-a-duplicate-of", "WAY:1"
        )
        assert "WAY:1" in out
        assert rc == 0

    def test_an_empty_answer_from_a_regional_extract_refuses(self, monkeypatch, capsys):
        """A mirror carrying only part of the planet answers "nothing here" for
        everywhere else, and that is indistinguishable from a clear site.

        Found for real on 2026-07-29: `--overpass-endpoint https://overpass.osm.ch/`
        (added minutes earlier as a 504 workaround) returned `[]` for the Sozopol
        coordinates and full data for Zurich. It would have waved the mandatory
        duplicate check through for a shop that is mapped. So an empty answer is
        only trusted once the endpoint has been shown to know about the area at
        all — the probe asks whether it has any roads within a kilometre.
        """
        api = FakeApi()
        rc, out = self._run(monkeypatch, capsys, *self.BASE, "--commit", elements=[], api=api, coverage=False)
        assert rc == 1
        assert api.calls == []
        assert "coverage" in out.lower()
        assert "extract" in out.lower()

    def test_coverage_is_only_probed_when_nothing_was_found(self, monkeypatch, capsys):
        """If the query returned POIs, the database plainly covers the area."""
        probed = []
        monkeypatch.setattr(osm_add_shop, "has_coverage", lambda *a, **k: probed.append(1) or True)
        monkeypatch.setattr(osm_add_shop, "overpass_query", lambda *a, **k: NEARBY)
        monkeypatch.setattr(osm_add_shop, "_api", lambda *a, **k: FakeApi())
        main([*self.BASE, "--not-a-duplicate-of", "NODE:14048113335"])
        assert probed == []

    def test_a_failed_coverage_probe_refuses(self, monkeypatch, capsys):
        api = FakeApi()

        def _boom(*a, **k):
            raise OverpassError("HTTP 504 Gateway Timeout")

        monkeypatch.setattr(osm_add_shop, "has_coverage", _boom)
        rc, out = self._run(monkeypatch, capsys, *self.BASE, "--commit", elements=[], api=api, coverage=None)
        assert rc == 1
        assert api.calls == []
        assert "504" in out

    def test_a_failed_duplicate_check_refuses_and_never_uploads(self, monkeypatch, capsys):
        """Overpass 504s. "Could not check" must never read as "nothing found".

        Seen for real on 2026-07-29: the first live run of this command hit an
        Overpass Gateway Timeout. It happened to be safe — the exception aborted
        the run — but by traceback rather than by design, which is not a guard.
        """
        api = FakeApi()

        def _boom(*a, **k):
            raise OverpassError("HTTP 504 Gateway Timeout")

        monkeypatch.setattr(osm_add_shop, "overpass_query", _boom)
        monkeypatch.setattr(osm_add_shop, "_api", lambda *a, **k: api)
        monkeypatch.setattr(osm_add_shop, "load_token", lambda **k: "tok")
        rc = main([*self.BASE, "--commit"])
        out = capsys.readouterr().out
        assert rc == 1
        assert api.calls == []
        assert "504" in out
        assert "could not" in out.lower()
        assert "Traceback" not in out

    def test_rounded_coordinates_are_refused(self, monkeypatch, capsys):
        """A 2-decimal coordinate is ~1 km wide — typed or invented, not surveyed."""
        rc, out = self._run(
            monkeypatch, capsys, "--lat", "42.42", "--lon", "27.69", "--name", "X", "--shop", "seafood", elements=[]
        )
        assert rc == 1
        assert "decimal" in out.lower()

    def test_missing_poi_tag_is_refused(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys, "--lat", str(LAT), "--lon", str(LON), "--name", "X", elements=[])
        assert rc == 1
        assert "shop" in out.lower()

    def test_non_numeric_coordinates_are_an_argument_error(self, monkeypatch):
        monkeypatch.setattr(osm_add_shop, "overpass_query", lambda *a, **k: [])
        with pytest.raises(SystemExit):
            main(["--lat", "north", "--lon", "27.692148", "--name", "X", "--shop", "seafood"])

    def test_there_is_no_batch_mode(self):
        """Deliberate: a human survey with a scripted upload stays within the
        Automated Edits code of conduct; a batch does not. The absence is a
        feature, so it is asserted rather than left to be noticed."""
        with pytest.raises(SystemExit):
            main(["--lat", str(LAT), "--lon", str(LON), "--name", "X", "--shop", "seafood", "--batch", "f.yaml"])
        # …and the reason is written down where whoever is tempted will read it.
        assert "no batch mode" in (osm_add_shop.__doc__ or "").lower()

    def test_one_shop_per_invocation(self, monkeypatch, capsys):
        """--lat/--lon are single-valued: a second pair replaces, never appends."""
        api = FakeApi()
        rc, out = self._run(
            monkeypatch, capsys, *self.BASE, "--lat", "42.11111111", "--lon", "27.22222222", elements=[], api=api
        )
        assert rc == 0
        assert "42.11111111,27.22222222" in out
        assert str(LAT) not in out
