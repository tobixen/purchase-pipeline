"""Shop-name comparison that ignores case and Cyrillic/Latin script.

Shop names arrive in either script — the same Kaufland is "Кауфланд
Варна-Одесос" on one receipt and "Kaufland Varna" on the next — so lookups
fold both sides to Latin before comparing. These are for *finding* context and
for telling chains apart; resolving a shop to its OSM object stays an exact
branch-key match (:mod:`purchase_pipeline.shop_osm`).
"""

from __future__ import annotations

import re

# Bulgarian Streamlined System, the one the staging filenames already follow
# (Пармаксъзов → ``parmaksazov``).  Shop names arrive in either script — the
# same Kaufland is "Кауфланд Варна-Одесос" on one receipt and "Kaufland Varna"
# on the next — so both sides of a match are folded to Latin first.
_BG_TO_LATIN = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
        "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
        "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sht", "ъ": "a",
        "ь": "y", "ю": "yu", "я": "ya",
    }
)  # fmt: skip


def fold_shop(text: str) -> str:
    """Casefold *text* and transliterate Bulgarian Cyrillic to Latin."""
    return text.casefold().translate(_BG_TO_LATIN)


def shop_matches(query: str, text: str) -> bool:
    """Whether *query* occurs in *text*, ignoring case and Cyrillic/Latin script.

    Context lookup only: OSM resolution stays an exact branch-key match.
    """
    return fold_shop(query) in fold_shop(text)


def chain_key(shop: str | None) -> str | None:
    """The chain a free-text shop name belongs to: its first word, folded.

    ``"Kaufland Varna ул. ..."`` and ``"Кауфланд Варна-Одесос"`` both give
    ``"kaufland"``. ``None`` for an empty or missing name — an unknown shop is
    never the same chain as anything.
    """
    tokens = fold_shop(shop or "").split()
    key = re.sub(r"[\W_]", "", tokens[0]) if tokens else ""
    return key or None


def same_chain(a: str | None, b: str | None) -> bool:
    """Whether two shop names belong to the same, known chain."""
    key = chain_key(a)
    return key is not None and key == chain_key(b)
