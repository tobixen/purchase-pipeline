#!/usr/bin/env python3
"""One-time OpenStreetMap auth bootstrap (OAuth 2.0 + PKCE).

Mirrors ``op_auth`` in shape — run once interactively, write only the token to
``~/.config/inventory-md/osm-token`` (mode 0600) — but the mechanism differs:
OSM has no password grant, so this is the authorization-code flow. There is no
callback server, so the redirect is the out-of-band one and OSM displays the code
for copy-paste::

    osm-auth --client-id YOUR_CLIENT_ID

The client id comes from registering an OAuth 2 application once, at
https://www.openstreetmap.org/oauth2/applications — as a **public** client (no
secret; PKCE is what protects the exchange), with redirect URI
``urn:ietf:wg:oauth:2.0:oob`` and the ``write_api`` permission. Registering it is
the user's act, not this tool's: it names *them* as the editor.

Only ``write_api`` is requested. That is enough to create a node and nothing else
— not the user's preferences, not their messages, not their identity.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlencode

from purchase_pipeline.op_auth import write_private

AUTHORIZE_URL = "https://www.openstreetmap.org/oauth2/authorize"
TOKEN_URL = "https://www.openstreetmap.org/oauth2/token"
# RFC 8628 predates this; OSM still supports the classic "show me the code" URI.
OOB_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"
SCOPES = ("write_api",)

TOKEN_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "inventory-md" / "osm-token"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def pkce_pair() -> tuple[str, str]:
    """A fresh (code_verifier, code_challenge) pair, S256 as per RFC 7636."""
    verifier = _b64url(secrets.token_bytes(32))
    return verifier, _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def authorize_url(
    client_id: str,
    *,
    challenge: str,
    redirect_uri: str = OOB_REDIRECT,
    scopes: tuple[str, ...] = SCOPES,
) -> str:
    """The URL the user opens to grant this client ``scopes``."""
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(
    code: str,
    *,
    client_id: str,
    verifier: str,
    redirect_uri: str = OOB_REDIRECT,
    token_url: str = TOKEN_URL,
) -> str:  # pragma: no cover - network
    """Exchange an authorization *code* for an access token."""
    import niquests as requests

    resp = requests.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        sys.exit(f"token exchange failed: HTTP {resp.status_code} {resp.text[:300]}")
    token = resp.json().get("access_token")
    if not token:
        sys.exit(f"no access_token in response: {resp.text[:300]}")
    return str(token)


def load_token(*, path: Path | None = None) -> str | None:
    """The saved OSM token, ``$OSM_TOKEN`` taking precedence, or ``None``."""
    from_env = os.environ.get("OSM_TOKEN")
    if from_env:
        return from_env.strip()
    try:
        return (path or TOKEN_PATH).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def save_token(token: str, *, path: Path | None = None) -> None:
    """Write *token* private to the owner."""
    write_private(path or TOKEN_PATH, token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", default=os.environ.get("OSM_CLIENT_ID"), help="OAuth 2 application client id")
    parser.add_argument("--token-path", type=Path, default=TOKEN_PATH)
    parser.add_argument("--scope", action="append", default=None, help="Override the requested scopes (repeatable)")
    args = parser.parse_args(argv)

    if not args.client_id:
        parser.error(
            "--client-id is required (or $OSM_CLIENT_ID). Register a public OAuth 2 application at "
            "https://www.openstreetmap.org/oauth2/applications with redirect URI "
            f"{OOB_REDIRECT} and the write_api permission."
        )

    verifier, challenge = pkce_pair()
    url = authorize_url(args.client_id, challenge=challenge, scopes=tuple(args.scope or SCOPES))
    print("Open this in a browser, approve, then paste back the code it shows:\n")
    print(f"  {url}\n")
    code = input("code: ").strip()
    if not code:
        sys.exit("no code entered")

    token = exchange_code(code, client_id=args.client_id, verifier=verifier)
    save_token(token, path=args.token_path)
    print(f"OK — token saved to {args.token_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
