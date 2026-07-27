#!/usr/bin/env python3
"""Create/update Open Food Facts products for EANs missing from OFF.

Reads a curated YAML of products (the reviewable artifact), builds OFF write
bodies, and — only with --commit — writes them to OFF and uploads the front
image, then verifies. Defaults to a dry run.

Auth: uses the logged-in OFF session cookie read from the browser via
browser_cookie3 (no password in the script). Pass --env net to target staging
(needs a separate staging account + HTTP basic off:off).

Products YAML (one entry per EAN)::

    products:
      - code: "3800225663700"
        lang: bg
        product_name_bg: НАХУТ КРИНА
        product_name_en: Krina chickpeas
        brands: Krina
        quantity: 250 g
        categories: Chickpeas, Legumes
        stores: Billa
        countries: Bulgaria
        front_image: /path/to/front.jpg     # optional

Usage:
    off_upload.py --products staging/off-products-2026-06-06.yaml            # dry run
    off_upload.py --products ... --commit                                    # write to prod
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# Fields copied straight through to the OFF write body when present.
_PASSTHROUGH = (
    "lang",
    "product_name_bg",
    "product_name_en",
    "product_name",
    "brands",
    "quantity",
    "categories",
    "stores",
    "countries",
    "labels",
    "packaging",
)


def build_body(product: dict[str, Any]) -> dict[str, Any]:
    """Build an OFF product write body from a curated product dict.

    ``code`` is required. Only known, non-empty fields are included so we never
    blank an existing field on update.
    """
    code = str(product.get("code") or "").strip()
    if not code:
        raise ValueError("product needs a 'code' (EAN)")
    body: dict[str, Any] = {"code": code}
    for field in _PASSTHROUGH:
        value = product.get(field)
        if value is not None and str(value).strip() != "":
            body[field] = str(value).strip()
    return body


_IMAGE_FIELDS = ("front", "ingredients", "nutrition", "packaging")


def _images(product: dict[str, Any]) -> dict[str, str]:
    """Collect {role: path} image uploads, merging legacy ``front_image``.

    Roles are the OFF image fields: front, ingredients, nutrition, packaging.
    """
    images: dict[str, str] = {}
    raw = product.get("images") or {}
    for field in _IMAGE_FIELDS:
        path = raw.get(field) or (product.get("front_image") if field == "front" else None)
        if path:
            images[field] = str(path)
    return images


def get_session_cookie() -> str | None:  # pragma: no cover - reads browser store
    """Read the logged-in OFF session cookie from the browser (browser_cookie3)."""
    try:
        import browser_cookie3
    except ImportError:
        print("browser_cookie3 not installed", file=sys.stderr)
        return None
    cj = browser_cookie3.chromium(domain_name="openfoodfacts.org")
    return next((c.value for c in cj if c.name == "session"), None)


def _load_products(path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return data.get("products", []) if isinstance(data, dict) else data


def main() -> None:  # pragma: no cover - thin CLI / network wiring
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--products", type=Path, required=True, help="Curated products YAML")
    parser.add_argument("--env", choices=["org", "net"], default="org", help="org=production, net=staging")
    parser.add_argument("--commit", action="store_true", help="Actually write to OFF (default: dry run)")
    parser.add_argument("--no-image", action="store_true", help="Skip front-image upload")
    args = parser.parse_args()

    if yaml is None:
        sys.exit("pyyaml required")
    products = _load_products(args.products)

    from openfoodfacts import API, APIVersion, Environment

    env = Environment.net if args.env == "net" else Environment.org
    cookie = None
    if args.commit:
        cookie = get_session_cookie()
        if not cookie:
            sys.exit("No OFF session cookie found — log in to openfoodfacts.org in the browser first.")
    api = API(
        user_agent="solveig-inventory/off_upload",
        environment=env,
        version=APIVersion.v2,
        session_cookie=cookie,
        timeout=180,
    )

    for p in products:
        body = build_body(p)
        images = _images(p)
        print(
            f"\n{'WRITE' if args.commit else 'DRY-RUN'} {body['code']}  {body.get('product_name_en') or body.get('product_name_bg', '')}"
        )
        for k, v in body.items():
            if k != "code":
                print(f"    {k}: {v}")
        if images:
            for role, path in images.items():
                print(f"    image[{role}]: {path}")
        else:
            print("    images: (none)")
        if not args.commit:
            continue
        resp = api.product.update(body)
        print(f"    update -> {resp}")
        lang = body.get("lang", "en")
        for field, path in images.items():
            if args.no_image:
                break
            try:
                img = api.product.upload_image(body["code"], image_path=path, selected={field: {lang: {}}})
                print(f"    image[{field}] -> {getattr(img, 'status_code', img)}")
            except Exception as exc:  # noqa: BLE001 - image upload is best-effort
                print(f"    image[{field}] -> FAILED ({exc}); retry later")
        check = api.product.get(body["code"], fields=["code", "product_name", "quantity", "categories_tags"])
        print(f"    verify -> name={check.get('product_name')!r} cats={check.get('categories_tags')}")


if __name__ == "__main__":
    main()
