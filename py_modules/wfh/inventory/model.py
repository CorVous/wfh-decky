"""The player's inventory snapshot and the counts features ask for.

Counting rules are WFHelper's: config/shared/componentOwnership.ts (+ the
foundry-pending adjustment in foundryPending.ts and the Blueprint/Component
aliases in componentNames.ts) for owned parts, config/shared/relicCounts.ts for
relics. The raw DE JSON is kept as-is in `data` for menus that need more.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

STACKED_COLLECTIONS = ("MiscItems", "Recipes")
EQUIPMENT_COLLECTIONS = ("Suits", "LongGuns", "Pistols", "Melee", "Sentinels", "SentinelWeapons", "SpaceSuits",
                         "SpaceGuns", "SpaceMelee", "OperatorAmps", "MechSuits")
MODULAR_COLLECTIONS = ("Hoverboards", "MoaPets", "KubrowPets")
BUILT_GEAR_COLLECTIONS = EQUIPMENT_COLLECTIONS + MODULAR_COLLECTIONS + ("SpecialItems",)
RELIC_COLLECTIONS = ("LevelKeys", "MiscItems", "Recipes")

REFINEMENT_BY_SUFFIX = {"Bronze": "intact", "Silver": "exceptional", "Gold": "flawless", "Platinum": "radiant"}
_RELIC_NAME = re.compile(r"^(Lith|Meso|Neo|Axi|Requiem|Vanguard) (\S+) (Intact|Exceptional|Flawless|Radiant)$")
_RELIC_TYPE = re.compile(r"VoidProjection|/Lotus/Relics/", re.I)


def _item_type(entry: Any) -> str:
    if isinstance(entry, dict) and isinstance(entry.get("ItemType"), str):
        return entry["ItemType"]
    return ""


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")


def pending_recipe_counts(pending: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    if isinstance(pending, list):
        for entry in pending:
            t = _item_type(entry)
            if t:
                counts[t] = counts.get(t, 0) + 1
    return counts


def without_foundry_pending(data: dict, is_reusable: Callable[[str], bool] | None = None) -> dict:
    """Same inventory minus Recipes copies the foundry already consumed."""
    pending = pending_recipe_counts(data.get("PendingRecipes"))
    recipes = data.get("Recipes")
    if not pending or not isinstance(recipes, list):
        return data
    kept = []
    for entry in recipes:
        t = _item_type(entry)
        spent = pending.get(t, 0) if t and not (is_reusable and is_reusable(t)) else 0
        if spent == 0:
            kept.append(entry)
            continue
        raw = entry.get("ItemCount") if isinstance(entry, dict) else None
        held = int(raw // 1) if _finite(raw) else 1
        # Later stacks of the same blueprint absorb whatever this one could not.
        pending[t] = max(0, spent - held)
        if held > spent:
            kept.append({**entry, "ItemCount": held - spent})
    return {**data, "Recipes": kept}


def aggregate_component_ownership(data: dict) -> dict[str, float]:
    owned: dict[str, float] = {}
    for key in STACKED_COLLECTIONS:
        sl = data.get(key)
        if not isinstance(sl, list):
            continue
        for entry in sl:
            t = _item_type(entry)
            if not t:
                continue
            raw = entry.get("ItemCount")
            owned[t] = owned.get(t, 0) + (raw if _finite(raw) else 1)
    # Built gear: one row is one copy; an ItemCount there is not a stack.
    for key in BUILT_GEAR_COLLECTIONS:
        sl = data.get(key)
        if not isinstance(sl, list):
            continue
        for entry in sl:
            t = _item_type(entry)
            if t:
                owned[t] = owned.get(t, 0) + 1
    return owned


def component_aliases(unique_name: str) -> list[str]:
    aliases = [unique_name]
    if re.search(r"Blueprint$", unique_name, re.I):
        aliases.append(re.sub(r"Blueprint$", "Component", unique_name, flags=re.I))
    if re.search(r"Component$", unique_name, re.I):
        aliases.append(re.sub(r"Component$", "Blueprint", unique_name, flags=re.I))
    # Weapon parts: the set lists .../AkbroncoPrimeLink, the inventory ...LinkBlueprint.
    if not re.search(r"Blueprint$", unique_name, re.I) and re.search(r"/Types/Recipes/", unique_name, re.I):
        aliases.append(unique_name + "Blueprint")
    return aliases


def _normalized_count(v: Any) -> int:
    if v is None:
        return 1
    if not _finite(v) or v <= 0:
        return 0
    return int(v // 1)


def collect_relic_counts(data: dict, includes: Callable[[str], bool] | None = None) -> dict[str, int]:
    """Per ItemType: summed within a collection, max across collections; if the
    usual collections hold none, every top-level array is tried."""
    includes = includes or (lambda t: bool(_RELIC_TYPE.search(t)))
    counts: dict[str, int] = {}

    def add(entries: Any) -> None:
        if not isinstance(entries, list):
            return
        local: dict[str, int] = {}
        for entry in entries:
            t = _item_type(entry)
            if not t or not includes(t):
                continue
            local[t] = local.get(t, 0) + _normalized_count(entry.get("ItemCount"))
        for t, c in local.items():
            counts[t] = max(counts.get(t, 0), c)

    for key in RELIC_COLLECTIONS:
        add(data.get(key))
    if not counts:
        for v in data.values():
            add(v)
    return counts


def relic_key(unique_name: str, display_name: str | None) -> tuple[str, str] | None:
    """("Lith V1", "radiant") from an item db name like "Lith V1 Radiant"."""
    if display_name:
        m = _RELIC_NAME.match(display_name.strip())
        if m:
            return f"{m.group(1)} {m.group(2)}", m.group(3).lower()
    return None


@dataclass
class Inventory:
    data: dict
    origin: str  # api | memory | file
    fetched_at: float = field(default_factory=time.time)
    path: str = ""
    _owned: dict[str, float] | None = field(default=None, repr=False)
    _relics: dict[str, int] | None = field(default=None, repr=False)

    @property
    def sync_id(self) -> str | None:
        sync = self.data.get("LastInventorySync")
        oid = sync.get("$oid") if isinstance(sync, dict) else None
        return oid if isinstance(oid, str) and re.fullmatch(r"[0-9a-f]{24}", oid) else None

    @property
    def sync_time(self) -> int | None:
        """Epoch ms the game last synced this inventory (from the ObjectId)."""
        sid = self.sync_id
        return int(sid[:8], 16) * 1000 if sid else None

    def owned_counts(self) -> dict[str, float]:
        if self._owned is None:
            self._owned = aggregate_component_ownership(without_foundry_pending(self.data))
        return self._owned

    def owned_count(self, unique_name: str) -> int:
        """Copies owned of a part/blueprint/item, max over Blueprint/Component aliases."""
        if not unique_name:
            return 0
        owned = self.owned_counts()
        return int(max(owned.get(a, 0) for a in component_aliases(unique_name)))

    def relic_counts(self) -> dict[str, int]:
        if self._relics is None:
            self._relics = collect_relic_counts(self.data)
        return self._relics

    def owned_relics(self, name_of: Callable[[str], str | None] | None = None) -> dict[tuple[str, str], int]:
        """{(relic name, refinement): count}; name_of maps a projection uniqueName
        to its display name ("Lith V1 Radiant"), normally from the item db."""
        out: dict[tuple[str, str], int] = {}
        for unique, count in self.relic_counts().items():
            if count <= 0:
                continue
            key = relic_key(unique, name_of(unique) if name_of else None)
            if key is None:
                continue
            out[key] = out.get(key, 0) + count
        return out

    def pending_recipes(self) -> dict[str, int]:
        return pending_recipe_counts(self.data.get("PendingRecipes"))

    def summary(self) -> dict:
        counts = {k: len(v) for k, v in self.data.items() if isinstance(v, list) and k in (
            "Suits", "LongGuns", "Pistols", "Melee", "MiscItems", "Recipes", "RawUpgrades", "Upgrades")}
        return {"origin": self.origin, "fetched_at": round(self.fetched_at, 3), "path": self.path,
                "sync_id": self.sync_id, "sync_time": self.sync_time,
                "relics": sum(self.relic_counts().values()), "collections": counts}

    def to_json(self) -> dict:
        return self.summary()
