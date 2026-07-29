#!/usr/bin/env python3
"""The branch-keyed shop→OSM cache: one place that knows where it lives.

An Open Prices price has to point at one real store, so this cache is keyed by
*branch* (``"Billa Varna ул. Андрей Сахаров"``), never by chain. Three modules
need it — ``shopping_context`` reports it, ``osm_resolve`` fills it,
``openprices_publish`` consumes it — and before this module they each carried
their own copy of the path, two of them disagreeing about ``XDG_CONFIG_HOME``.

The two invariants below are the same guard seen from both ends. The matcher
refuses anything but an exact key, so ``"Billa"`` resolves to nothing (the
2026-07-24 Sozopol bug: one Billa cached, so the old substring fallback returned
the Varna branch for a Sozopol trip). :func:`save_entry` refuses to *store* a
bare chain key, because such a key would then match exactly and resolve
silently — the same bug let back in through the cache rather than the matcher.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SHOP_OSM_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "inventory-md" / "shop-osm.json"

OSM_TYPES = ("NODE", "WAY", "RELATION")


def load_cache(path: Path | None = None) -> dict[str, Any]:
    """Load the shop→OSM cache, returning ``{}`` if it is missing or unreadable."""
    try:
        return json.loads((path or SHOP_OSM_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def parse_osm_spec(spec: str) -> tuple[str, int]:
    """Parse a ``TYPE:ID`` spec, e.g. ``"WAY:1016681733"`` → ``("WAY", 1016681733)``."""
    kind, sep, num = spec.partition(":")
    kind, num = kind.strip().upper(), num.strip()
    if not sep or kind not in OSM_TYPES or not num.isdigit():
        raise ValueError(f"bad OSM spec {spec!r}; expected {'|'.join(t.lower() for t in OSM_TYPES)}:<id>")
    return kind, int(num)


def is_branch_key(key: str) -> bool:
    """Whether *key* names a branch rather than a bare chain.

    The test is deliberately crude — at least two whitespace-separated words —
    because the only thing it has to catch is ``"Billa"``. A chain name plus a
    town is the minimum that identifies a store.
    """
    return len(key.split()) >= 2


def match_shop_osm(cache: dict[str, Any], shop: str) -> dict[str, Any] | None:
    """Find the cached entry keyed exactly by *shop* (case- and whitespace-insensitive).

    Nothing else resolves. Uniqueness in the cache is a fact about what has been
    visited before, not about which shop the caller means: on 2026-07-24 a trip
    to Billa **Sozopol** asked for ``"Billa"``, found the one cached Billa, and
    confidently returned the **Varna** branch. Callers that get ``None`` should
    offer :func:`shop_osm_candidates` so a human can pick the exact branch key.
    """
    if not shop:
        return None
    want = shop.strip().casefold()
    for key, val in cache.items():
        if key.strip().casefold() == want:
            return val
    return None


def shop_osm_candidates(cache: dict[str, Any], shop: str) -> list[str]:
    """Cache keys whose name overlaps *shop* by substring (either direction)."""
    if not shop:
        return []
    want = shop.casefold()
    return [key for key in cache if want in key.casefold() or key.casefold() in want]


def save_entry(
    key: str,
    osm_type: str,
    osm_id: int,
    *,
    path: Path | None = None,
    extra: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Record *key* → OSM object, returning the stored entry.

    Refuses, unless *force*:

    * a *key* that is not a branch key (see :func:`is_branch_key`);
    * repointing an existing key at a different object — a published price
      quietly changing shop is not something a cache write should be able to do.

    Re-saving the same object under the same key is a no-op, so callers may be
    re-run safely.
    """
    path = path or SHOP_OSM_PATH
    if not force and not is_branch_key(key):
        raise ValueError(
            f"{key!r} looks like a bare chain name, not a branch. The cache is branch-keyed "
            f"(e.g. 'Billa Sozopol ул. Републиканска 5') — a chain-only key resolves silently to "
            f"whichever branch was saved first. Add the town/address, or pass force to override."
        )
    entry = {"osm_type": osm_type.upper(), "osm_id": int(osm_id), **(extra or {})}
    cache = load_cache(path)
    # Found the way match_shop_osm finds it, or a differently-cased key would
    # slip past the repoint guard and be shadowed by the existing one.
    existing = next((k for k in cache if k.strip().casefold() == key.strip().casefold()), None)
    old = cache.get(existing) if existing is not None else None
    if old and not force:
        if (str(old.get("osm_type", "")).upper(), int(old.get("osm_id", -1))) != (entry["osm_type"], entry["osm_id"]):
            raise ValueError(
                f"{key!r} is already cached as {old.get('osm_type')}:{old.get('osm_id')}, "
                f"not {entry['osm_type']}:{entry['osm_id']}. Pass force to repoint it."
            )
        return old
    if existing is not None:
        del cache[existing]
    cache[key] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return entry


def osm_url(osm_type: str, osm_id: int) -> str:
    """A browsable openstreetmap.org link — the only way to confirm a candidate."""
    return f"https://www.openstreetmap.org/{osm_type.lower()}/{osm_id}"
