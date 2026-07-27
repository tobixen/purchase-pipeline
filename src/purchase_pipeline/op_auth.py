#!/usr/bin/env python3
"""One-time Open Prices auth bootstrap.

Prompts for an Open Food Facts password (via getpass — never echoed, never
stored, never passed on the command line), exchanges it at
``POST /api/v1/auth`` for a session token, and writes ONLY the token to
``~/.config/inventory-md/openprices-token`` (mode 0600). The token has no
intrinsic expiry, so this is run once; the publisher reads the file.

Run interactively:  openprices-auth [--username tobixen] [--env org]
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

BASES = {"org": "https://prices.openfoodfacts.org", "net": "https://prices.openfoodfacts.net"}
TOKEN_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "inventory-md" / "openprices-token"


def write_private(path: Path, text: str) -> None:
    """Write *text* to *path* as a file only its owner can read.

    Created 0600 rather than written and chmodded afterwards, which would leave
    it readable by everyone under the usual umask until the chmod.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # an existing file keeps its old mode through os.open
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)


def main() -> None:
    import niquests as requests  # the "publish" extra; not needed for write_private

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--username", default="tobixen")
    parser.add_argument("--env", choices=["org", "net"], default="org")
    args = parser.parse_args()

    password = getpass.getpass(f"Open Prices password for {args.username} ({args.env}): ")
    if not password:
        sys.exit("no password entered")

    resp = requests.post(
        f"{BASES[args.env]}/api/v1/auth",
        data={"username": args.username, "password": password},
        timeout=30,
    )
    if resp.status_code != 200:
        sys.exit(f"auth failed: HTTP {resp.status_code} {resp.text[:200]}")
    token = resp.json().get("access_token")
    if not token:
        sys.exit(f"no access_token in response: {resp.text[:200]}")

    write_private(TOKEN_PATH, token)
    print(f"OK — token saved to {TOKEN_PATH} (user {token.split('__')[0]})")


if __name__ == "__main__":
    main()
