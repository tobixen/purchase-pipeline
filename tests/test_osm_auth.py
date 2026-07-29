"""Tests for osm_auth — the one-time OSM OAuth 2.0 bootstrap.

Nothing here talks to openstreetmap.org: the PKCE maths, the authorize URL and
the token file are all local, and the code→token exchange is monkeypatched.
"""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from purchase_pipeline import osm_auth
from purchase_pipeline.osm_auth import (
    OOB_REDIRECT,
    authorize_url,
    load_token,
    main,
    pkce_pair,
    save_token,
)


class TestPkce:
    def test_challenge_is_the_sha256_of_the_verifier(self):
        verifier, challenge = pkce_pair()
        want = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
        assert challenge == want

    def test_verifier_length_is_within_the_rfc_range(self):
        # RFC 7636 §4.1: 43..128 characters.
        verifier, _ = pkce_pair()
        assert 43 <= len(verifier) <= 128

    def test_no_padding_or_unsafe_characters(self):
        for value in pkce_pair():
            assert "=" not in value
            assert "+" not in value
            assert "/" not in value

    def test_each_call_is_fresh(self):
        assert pkce_pair()[0] != pkce_pair()[0]


class TestAuthorizeUrl:
    def _params(self, **kwargs):
        url = authorize_url("myclientid", challenge="thechallenge", **kwargs)
        return parse_qs(urlparse(url).query)

    def test_carries_the_pkce_challenge_with_s256(self):
        params = self._params()
        assert params["code_challenge"] == ["thechallenge"]
        assert params["code_challenge_method"] == ["S256"]

    def test_is_an_authorization_code_request(self):
        params = self._params()
        assert params["response_type"] == ["code"]
        assert params["client_id"] == ["myclientid"]

    def test_defaults_to_the_out_of_band_redirect(self):
        """A CLI has no callback server, so OSM shows the code for copy-paste."""
        assert self._params()["redirect_uri"] == [OOB_REDIRECT]

    def test_asks_only_for_write_api(self):
        """Least privilege: this token creates nodes and nothing else."""
        assert self._params()["scope"] == ["write_api"]

    def test_points_at_openstreetmap(self):
        assert urlparse(authorize_url("x", challenge="y")).netloc == "www.openstreetmap.org"

    def test_scopes_are_overridable(self):
        assert self._params(scopes=("write_api", "read_prefs"))["scope"] == ["write_api read_prefs"]


class TestTokenFile:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "osm-token"
        save_token("abc123", path=path)
        assert load_token(path=path) == "abc123"

    def test_saved_private(self, tmp_path):
        path = tmp_path / "osm-token"
        save_token("abc123", path=path)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_never_readable_by_others_even_briefly(self, tmp_path, monkeypatch):
        """Created 0600, not written world-readable and chmodded afterwards."""
        import os
        from pathlib import Path

        monkeypatch.setattr(Path, "chmod", lambda *a, **k: None)
        old = os.umask(0)
        try:
            path = tmp_path / "osm-token"
            save_token("abc123", path=path)
        finally:
            os.umask(old)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_whitespace_is_stripped(self, tmp_path):
        path = tmp_path / "osm-token"
        path.write_text("abc123\n", encoding="utf-8")
        assert load_token(path=path) == "abc123"

    def test_missing_is_none(self, tmp_path):
        assert load_token(path=tmp_path / "nope") is None

    def test_environment_wins(self, tmp_path, monkeypatch):
        path = tmp_path / "osm-token"
        save_token("from-file", path=path)
        monkeypatch.setenv("OSM_TOKEN", "from-env")
        assert load_token(path=path) == "from-env"

    def test_creates_parent_directories(self, tmp_path):
        path = tmp_path / "config" / "inventory-md" / "osm-token"
        save_token("abc123", path=path)
        assert load_token(path=path) == "abc123"


class TestMain:
    def test_prints_the_url_then_saves_the_exchanged_token(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "osm-token"
        seen = {}

        def _exchange(code, *, client_id, verifier, **kwargs):
            seen.update(code=code, client_id=client_id, verifier=verifier)
            return "tok-xyz"

        monkeypatch.setattr(osm_auth, "exchange_code", _exchange)
        monkeypatch.setattr("builtins.input", lambda *a: "  the-code  ")
        rc = main(["--client-id", "cid", "--token-path", str(path)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "oauth2/authorize" in out
        # The pasted code is stripped before use — a copy-paste carries spaces.
        assert seen["code"] == "the-code"
        assert seen["client_id"] == "cid"
        assert load_token(path=path) == "tok-xyz"

    def test_verifier_sent_to_the_exchange_matches_the_url_challenge(self, tmp_path, monkeypatch, capsys):
        """PKCE is pointless if the two halves come from different pairs."""
        path = tmp_path / "osm-token"
        seen = {}
        monkeypatch.setattr(osm_auth, "exchange_code", lambda code, **kw: seen.update(kw) or "tok")
        monkeypatch.setattr("builtins.input", lambda *a: "code")
        main(["--client-id", "cid", "--token-path", str(path)])
        out = capsys.readouterr().out

        url = next(line.strip() for line in out.splitlines() if "oauth2/authorize" in line)
        challenge = parse_qs(urlparse(url).query)["code_challenge"][0]
        expected = base64.urlsafe_b64encode(hashlib.sha256(seen["verifier"].encode("ascii")).digest())
        assert challenge == expected.decode().rstrip("=")

    def test_empty_code_is_refused(self, tmp_path, monkeypatch):
        path = tmp_path / "osm-token"
        monkeypatch.setattr("builtins.input", lambda *a: "   ")
        with pytest.raises(SystemExit):
            main(["--client-id", "cid", "--token-path", str(path)])
        assert not path.exists()

    def test_client_id_is_required(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OSM_CLIENT_ID", raising=False)
        with pytest.raises(SystemExit):
            main(["--token-path", str(tmp_path / "osm-token")])

    def test_client_id_may_come_from_the_environment(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "osm-token"
        monkeypatch.setenv("OSM_CLIENT_ID", "env-cid")
        monkeypatch.setattr(osm_auth, "exchange_code", lambda code, **kw: "tok")
        monkeypatch.setattr("builtins.input", lambda *a: "code")
        assert main(["--token-path", str(path)]) == 0
        assert "env-cid" in capsys.readouterr().out
