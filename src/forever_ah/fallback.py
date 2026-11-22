"""Reservkälla för itemdata när Forevers item-API svarar 404 (gäller bland annat vanilla-vapen).

Data från nexus-devs/wow-classic-items (MIT), se data/classic_items.LICENSE.md.
Täcker klassiska item-id:n, inte Forevers nya items.
"""

from __future__ import annotations

import gzip
import json
from functools import lru_cache
from importlib import resources
from typing import Any

# Blizzards klass-id:n, så att filtrering på id fungerar även för reservdata
CLASS_IDS = {
    "Consumable": 0, "Container": 1, "Weapon": 2, "Gem": 3, "Armor": 4, "Reagent": 5, "Projectile": 6,
    "Trade Goods": 7, "Recipe": 9, "Money": 10, "Quiver": 11, "Quest": 12, "Key": 13, "Miscellaneous": 15,
    "Glyph": 16,
}


@lru_cache(maxsize=1)
def _load() -> dict[int, dict[str, Any]]:
    raw = resources.files("forever_ah.data").joinpath("classic_items.json.gz").read_bytes()
    doc = json.loads(gzip.decompress(raw))
    fields = doc["fields"]
    return {row[0]: dict(zip(fields, row)) for row in doc["items"]}


def lookup(item_id: int) -> dict[str, Any] | None:
    return _load().get(int(item_id))


def as_row(item_id: int, now: int) -> tuple | None:
    """Rad för INSERT OR REPLACE INTO items i samma kolumnordning som insamlaren använder."""
    d = lookup(item_id)
    if not d:
        return None
    name = d["name"]
    return (item_id, name, (name or "").lower(), d["quality"], d["class"], d["subclass"], d["item_level"],
            d["sell_price"], now, 0, CLASS_IDS.get(d["class"], -1), None, d["required_level"] or 0, "classic")
