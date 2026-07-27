"""Tests for the per-chain receipt-format registry (purchase_pipeline/receipt_formats.py).

The registry records the layout quirks a human (or an agent) must know to
transcribe a photographed receipt correctly. It is keyed by **chain**, not by
branch: a Billa receipt is laid out the same way in Varna and in Sozopol, so
unlike the branch-keyed OSM cache a chain-prefix match is the correct rule here.
"""

import json

import pytest

from purchase_pipeline.receipt_formats import (
    DEFAULT_REGISTRY,
    AmbiguousChainError,
    describe_format,
    format_for,
    load_formats,
)

FORMATS = {
    "Billa": {"branch_address": "footer", "multiplier_line": "precedes"},
    "Lidl": {"branch_address": "header", "multiplier_line": "follows"},
}


class TestFormatFor:
    def test_exact_chain_name(self):
        assert format_for(FORMATS, "Billa")["multiplier_line"] == "precedes"

    def test_branch_key_resolves_to_its_chain(self):
        # The whole point: the shop is recorded per branch, the format per chain.
        assert format_for(FORMATS, "Billa Sozopol")["multiplier_line"] == "precedes"
        assert format_for(FORMATS, "Billa Varna ул. Андрей Сахаров")["branch_address"] == "footer"

    def test_case_insensitive(self):
        assert format_for(FORMATS, "lidl varna")["multiplier_line"] == "follows"

    def test_unknown_chain_returns_none(self):
        assert format_for(FORMATS, "Sozopol Fish") is None

    def test_empty_shop_returns_none(self):
        assert format_for(FORMATS, "") is None

    def test_a_chain_name_alone_is_not_matched_by_a_prefix_of_it(self):
        # "Bil" is not the chain "Billa"; only the chain being a prefix of the
        # *shop* counts, not the other way round.
        assert format_for(FORMATS, "Bil") is None

    def test_ambiguous_chain_prefixes_refuse_to_guess(self):
        formats = {"Coop": {"multiplier_line": "follows"}, "Coop Extra": {"multiplier_line": "precedes"}}
        with pytest.raises(AmbiguousChainError) as exc:
            format_for(formats, "Coop Extra Varna")
        assert "Coop Extra" in str(exc.value)

    def test_longest_match_is_not_silently_preferred(self):
        # Deliberately NOT resolving to the more specific key: if two registry
        # entries claim a shop, that is a registry bug to fix, not a tie to break.
        formats = {"Coop": {}, "Coop Extra": {}}
        with pytest.raises(AmbiguousChainError):
            format_for(formats, "Coop Extra Sozopol")


class TestDescribeFormat:
    def test_renders_the_known_quirks(self):
        text = describe_format("Billa Sozopol", FORMATS["Billa"])
        assert "Billa Sozopol" in text
        assert "precedes" in text

    def test_renders_a_fields_note(self):
        # The note key is `<field>_note`. Getting that wrong drops the note
        # silently, which is how the Billa multiplier explanation went missing
        # from the checklist while the bare value "precedes" still showed.
        text = describe_format("X", {"multiplier_line": "precedes", "multiplier_line_note": "ABOVE its item"})
        assert "ABOVE its item" in text

    def test_every_note_in_the_shipped_registry_is_actually_rendered(self):
        for chain, fmt in load_formats(DEFAULT_REGISTRY).items():
            text = describe_format(chain, fmt)
            for key, val in fmt.items():
                if key.endswith("_note"):
                    assert val in text, f"{chain}.{key} is never rendered — check the field name"

    def test_unknown_chain_says_so_without_inventing_rules(self):
        text = describe_format("Sozopol Fish", None)
        assert "no receipt format recorded" in text.lower()
        # It must not imply any particular layout.
        assert "precedes" not in text
        assert "follows" not in text


class TestShippedRegistry:
    """The registry file that ships with the repo must be loadable and honest."""

    def test_default_registry_exists_and_parses(self):
        assert DEFAULT_REGISTRY.exists(), f"{DEFAULT_REGISTRY} is missing"
        data = json.loads(DEFAULT_REGISTRY.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert data

    def test_load_formats_reads_it(self):
        assert "Billa" in load_formats(DEFAULT_REGISTRY)

    def test_missing_file_is_an_empty_registry_not_a_crash(self, tmp_path):
        assert load_formats(tmp_path / "nope.json") == {}

    def test_underscore_keys_are_comments_not_chains(self):
        # The file documents its own schema in _README; that must never look
        # like a chain to format_for().
        assert not any(k.startswith("_") for k in load_formats(DEFAULT_REGISTRY))
        assert format_for(load_formats(DEFAULT_REGISTRY), "_README") is None

    def test_billa_records_the_2026_07_24_findings(self):
        billa = load_formats(DEFAULT_REGISTRY)["Billa"]
        # Both facts were established from the real receipt; see TODO task 2.
        assert billa["multiplier_line"] == "precedes"
        assert billa["branch_address"] == "footer"

    def test_every_entry_declares_its_multiplier_position(self):
        # An entry that omits this is worse than no entry: a transcriber would
        # read the registry, find the chain listed, and assume the default.
        for chain, fmt in load_formats(DEFAULT_REGISTRY).items():
            assert fmt.get("multiplier_line") in {"precedes", "follows", "unknown"}, chain

    def test_claims_carry_a_note_saying_where_they_came_from(self):
        for chain, fmt in load_formats(DEFAULT_REGISTRY).items():
            assert fmt.get("source"), f"{chain} states layout rules with no provenance"
