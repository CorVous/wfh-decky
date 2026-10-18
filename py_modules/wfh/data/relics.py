"""Relic database: contents, per-refinement chances and vault status.

Loaded lazily from ``relics.json.gz`` in the same data directory items.py picked
(bundle or the weekly runtime refresh). One Relic per "<Tier> <Code>"; the four
refinements share the reward list and differ only in chances.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

from .. import log, rpc
from . import items as items_mod
from .items import REFINEMENTS, TIERS, relic_reward_rarity  # noqa: F401 - re-exported

_log = log.get("relics")

_NAME_RE = re.compile(
    r"^\s*(lith|meso|neo|axi|requiem|vanguard)\s+(\S+)(?:\s+(intact|exceptional|flawless|radiant))?(?:\s+relic)?\s*$",
    re.IGNORECASE,
)


@dataclass
class Reward:
    item_unique: str
    item_name: str
    rarity: str  # Common | Uncommon | Rare (from the intact chance)
    chance_by_refinement: dict[str, float] = field(default_factory=dict)
    wfm_slug: str | None = None

    def chance(self, refinement: str = "intact") -> float:
        return self.chance_by_refinement.get(refinement.lower(), 0.0)

    def to_json(self) -> dict:
        return {"item_unique": self.item_unique, "item_name": self.item_name, "rarity": self.rarity,
                "chance_by_refinement": self.chance_by_refinement, "wfm_slug": self.wfm_slug}


@dataclass
class Relic:
    name: str            # "Lith A1"
    tier: str            # "Lith"
    vaulted: bool
    rewards: list[Reward] = field(default_factory=list)
    code: str = ""
    unique_names: dict[str, str] = field(default_factory=dict)  # refinement -> uniqueName
    wfm_slug: str | None = None   # "lith_a1_relic"
    image_name: str | None = None

    def to_json(self) -> dict:
        return {"name": self.name, "tier": self.tier, "code": self.code, "vaulted": self.vaulted,
                "wfm_slug": self.wfm_slug, "image_name": self.image_name, "unique_names": self.unique_names,
                "rewards": [r.to_json() for r in self.rewards]}


def parse_relic_name(text: str) -> tuple[str, str, str | None] | None:
    """"Lith A1", "lith a1 relic", "Lith A1 Radiant" -> ("Lith", "A1", refinement|None)."""
    m = _NAME_RE.match(text or "")
    if not m:
        return None
    return m.group(1).capitalize(), m.group(2).upper(), (m.group(3) or "").lower() or None


class RelicDb:
    def __init__(self, data: dict, source: str = "bundle"):
        self.version = str(data.get("version") or "")
        self.source = source
        self._relics: dict[str, Relic] = {}
        self._by_unique: dict[str, tuple[Relic, str]] = {}
        self._by_item: dict[str, list[Relic]] = {}
        for raw in data.get("relics") or []:
            rewards = [Reward(r.get("uniqueName") or "", r.get("name") or "", r.get("rarity") or "Common",
                              {k: float(v) for k, v in (r.get("chances") or {}).items()}, r.get("wfmSlug"))
                       for r in raw.get("rewards") or []]
            relic = Relic(raw["name"], raw.get("tier") or raw["name"].split(" ")[0], bool(raw.get("vaulted")),
                          rewards, raw.get("code") or "", dict(raw.get("uniqueNames") or {}),
                          raw.get("wfmSlug"), raw.get("imageName"))
            self._relics[relic.name.lower()] = relic
            for ref, u in relic.unique_names.items():
                if u:
                    self._by_unique[u] = (relic, ref)
            for r in rewards:
                self._by_item.setdefault(r.item_name.lower(), []).append(relic)

    def __len__(self) -> int:
        return len(self._relics)

    def get(self, name: str) -> Relic | None:
        """Accepts "Lith A1", "Lith A1 Relic", "Lith A1 Radiant" or a relic uniqueName."""
        if name and name.startswith("/Lotus/"):
            hit = self._by_unique.get(name)
            return hit[0] if hit else None
        parsed = parse_relic_name(name)
        if not parsed:
            return None
        return self._relics.get(f"{parsed[0]} {parsed[1]}".lower())

    def by_unique(self, unique_name: str) -> tuple[Relic, str] | None:
        """(Relic, refinement) for an inventory uniqueName like .../T1VoidProjectionGBronze."""
        return self._by_unique.get(unique_name or "")

    def all(self) -> list[Relic]:
        return list(self._relics.values())

    def unvaulted(self) -> list[Relic]:
        return [r for r in self._relics.values() if not r.vaulted]

    def containing(self, item_name: str) -> list[Relic]:
        """Relics whose pool holds this reward name (case-insensitive)."""
        return list(self._by_item.get((item_name or "").lower(), []))


_lock = threading.Lock()
_db: RelicDb | None = None


def _reset() -> None:
    global _db
    with _lock:
        _db = None


items_mod.on_reset(_reset)


def relics() -> RelicDb:
    global _db
    with _lock:
        if _db is None:
            d, src = items_mod.active_data_dir()
            if d is None:
                _log.warning("no relic data found (run scripts/fetch-data.sh)")
                _db = RelicDb({}, "none")
            else:
                t0 = time.monotonic()
                _db = RelicDb(items_mod.load_json_gz(d, items_mod.RELICS_FILE), src)
                _log.info("relic db %s (%s): %d relics in %.2fs", _db.version, src, len(_db), time.monotonic() - t0)
        return _db


def setup(app) -> None:
    app.services["relics"] = relics


@rpc.method("relics.get")
async def _rpc_get(name: str) -> dict | None:
    from ..app import get_app

    db = await get_app().run_blocking(relics)
    relic = db.get(name)
    return relic.to_json() if relic else None
