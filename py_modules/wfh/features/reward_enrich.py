"""Reward enrichment (ducats, vaulted, owned/set counts, mastered, foundry) and pricing.

Port of WFHelper ipc/overlay/scan.ts enrichRewardItems (relic-rewards.md §9) and
the overlay's price/best-pick rules (§10). Items come from the relic reward pool
(wfh.data.items prime_parts()), the inventory from wfh.inventory.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from ..data import names
from ..inventory.model import component_aliases

MIN_TRADABLE_SET_PARTS = 2
# Rank-30 affinity (mastery counts the item once it reaches 30).
_XP_FRAME = 900_000
_XP_WEAPON = 450_000
_FRAME_CATEGORIES = {"Warframes", "Sentinels", "Archwing", "Pets"}
_MASTERABLE = _FRAME_CATEGORIES | {"Primary", "Secondary", "Melee", "SentinelWeapons", "Arch-Gun", "Arch-Melee"}
_XP_COLLECTIONS = ("Suits", "LongGuns", "Pistols", "Melee", "Sentinels", "SentinelWeapons", "SpaceSuits",
                   "SpaceGuns", "SpaceMelee", "KubrowPets", "MoaPets")


@dataclass
class SetPart:
    name: str
    owned: int
    required: int
    is_reward: bool = False
    building: bool = False


@dataclass
class RewardEntry:
    slot: int
    name: str
    unique_name: str | None = None
    slug: str | None = None
    rarity: str | None = None
    ducats: int | None = None
    vaulted: bool | None = None
    owned: int | None = None
    required: int = 1
    set_owned: int | None = None
    set_required: int | None = None
    complete_sets: int | None = None
    set_parts: list[SetPart] = field(default_factory=list)
    set_name: str | None = None
    set_slug: str | None = None
    mastered: bool | None = None
    building: bool = False
    confidence: float = 0.0
    mode: str = ""
    text: str = ""
    plat: int | None = None          # None: not known yet (pending) or missing (see price_state)
    set_plat: int | None = None
    price_state: str = "pending"     # pending | ok | missing
    set_price_state: str = "pending"

    def to_json(self) -> dict:
        d = asdict(self)
        d["confidence"] = round(self.confidence, 3)
        return d


def _is_forma(name: str) -> bool:
    return names.strip_quantity_prefix(name or "") == "Forma Blueprint"


def _positive_int(v: Any) -> int | None:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 and n == v else None


def _owned(inv, unique: str | None) -> int:
    if inv is None or not unique:
        return 0
    try:
        return int(inv.owned_count(unique))
    except Exception:  # noqa: BLE001 - a malformed inventory must not break the overlay
        return 0


def _pending(inv) -> set[str]:
    if inv is None:
        return set()
    try:
        return set(inv.pending_recipes().keys())
    except Exception:  # noqa: BLE001
        return set()


def _in_foundry(unique: str | None, pending: set[str]) -> bool:
    return bool(unique and pending and any(a in pending for a in component_aliases(unique)))


def _xp_by_type(inv) -> dict[str, float]:
    out: dict[str, float] = {}
    data = getattr(inv, "data", None) or {}
    rows = list(data.get("XPInfo") or [])
    for key in _XP_COLLECTIONS:
        rows += list(data.get(key) or [])
    for r in rows:
        if not isinstance(r, dict):
            continue
        t, xp = r.get("ItemType"), r.get("XP")
        if isinstance(t, str) and isinstance(xp, (int, float)):
            out[t] = max(out.get(t, 0), float(xp))
    return out


def mastered_of(parent, xp: dict[str, float] | None) -> bool | None:
    if parent is None or xp is None or parent.category not in _MASTERABLE:
        return None
    need = _XP_FRAME if parent.category in _FRAME_CATEGORIES else _XP_WEAPON
    return xp.get(parent.unique_name, 0) >= need


def enrich(scan_items: Iterable, db=None, inv=None) -> list[RewardEntry]:
    """scan_items: reward_scan.ScanItem (item is an items.Item from the reward pool)."""
    pending = _pending(inv)
    xp = _xp_by_type(inv) if inv is not None else None
    out: list[RewardEntry] = []
    for si in scan_items:
        item = si.item
        name = names.item_name(item)
        entry = None
        if db is not None:
            slug = getattr(item, "wfm_slug", None)
            entry = db.by_name(name) or (db.by_slug(slug) if slug else None)
        unique = (entry.unique_name if entry else None) or getattr(item, "unique_name", None) or None
        parent_unique = (entry.parent if entry else None) or getattr(item, "parent", None)
        parent = db.by_unique(parent_unique) if (db is not None and parent_unique) else None
        reward_aliases = component_aliases(unique) if unique else []
        required = 1
        set_parts: list[SetPart] = []
        set_owned = set_required = complete = None
        if parent is not None and parent.components:
            comp = next((c for c in parent.components if c.unique_name in reward_aliases), None)
            required = _positive_int(comp.item_count) if comp else None
            required = required or 1
            owned_sum = req_sum = 0
            sets = None
            for c in parent.components:
                if not c.unique_name or not c.tradable:
                    continue
                need = _positive_int(c.item_count) or 1
                count = _owned(inv, c.unique_name)
                req_sum += need
                owned_sum += min(count, need)
                sets = count // need if sets is None else min(sets, count // need)
                set_parts.append(SetPart(c.name or "Part", count, need, c.unique_name in reward_aliases,
                                         _in_foundry(c.unique_name, pending)))
            if req_sum > 0:
                set_owned, set_required, complete = owned_sum, req_sum, sets or 0
        set_name = f"{parent.name} Set" if parent is not None and len(set_parts) >= MIN_TRADABLE_SET_PARTS else None
        ducats = _positive_int(getattr(item, "ducats", None)) or (entry.ducats if entry else None)
        raw_vaulted = getattr(item, "vaulted", None)
        vaulted = raw_vaulted if isinstance(raw_vaulted, bool) and not _is_forma(name) else None
        slug = names.normalize_wfm_slug(getattr(item, "wfm_slug", None) or (entry.wfm_slug if entry else None))
        out.append(RewardEntry(
            slot=si.slot, name=name, unique_name=unique, slug=slug,
            rarity=getattr(item, "rarity", None), ducats=ducats, vaulted=vaulted,
            owned=_owned(inv, unique) if inv is not None else None, required=required,
            set_owned=set_owned if inv is not None else None, set_required=set_required,
            complete_sets=complete if inv is not None else None, set_parts=set_parts if inv is not None else [],
            set_name=set_name, set_slug=names.normalize_wfm_slug(set_name) if set_name else None,
            mastered=mastered_of(parent, xp), building=_in_foundry(unique, pending),
            confidence=float(si.confidence), mode=si.mode, text=getattr(si, "text", ""),
            price_state="pending" if slug else "missing",
            set_price_state="pending" if set_name else "missing",
        ))
    return out


# ---- prices -----------------------------------------------------------------------

class PriceSource:
    """What the overlay needs from a price feed. Values are whole platinum."""

    def cached(self, slug: str) -> tuple[bool, int | None]:
        raise NotImplementedError

    def fetch(self, slugs: list[str], deadline: float | None) -> dict[str, int | None]:
        raise NotImplementedError

    def prefetch(self, slugs: list[str]) -> int:
        return 0


class MarketPrices(PriceSource):
    """wfh.data.market (warframe.market, cached, rate limited)."""

    def _m(self):
        from ..data import market

        return market.get()

    def cached(self, slug: str) -> tuple[bool, int | None]:
        hit, p = self._m().cached(slug)
        return hit, (p.plat if p else None)

    def fetch(self, slugs: list[str], deadline: float | None) -> dict[str, int | None]:
        out = self._m().prices(slugs, None, deadline)
        return {s: (p.plat if p else None) for s, p in out.items()}

    def prefetch(self, slugs: list[str]) -> int:
        return self._m().prefetch(slugs)


def _clean_plat(v: Any) -> int | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    n = round(abs(f))
    return n if n > 0 else None


def apply_prices(entries: list[RewardEntry], lookup: Callable[[str], tuple[bool, int | None]]) -> int:
    """Fill plat/set_plat from lookup(slug) -> (known, plat). Returns how many are still pending."""
    pending = 0
    for e in entries:
        if e.slug and e.price_state == "pending":
            known, v = lookup(e.slug)
            if known:
                e.plat = _clean_plat(v)
                e.price_state = "ok" if e.plat else "missing"
            else:
                pending += 1
        if e.set_slug and e.set_price_state == "pending":
            known, v = lookup(e.set_slug)
            if known:
                e.set_plat = _clean_plat(v)
                e.set_price_state = "ok" if e.set_plat else "missing"
            else:
                pending += 1
    return pending


def pending_slugs(entries: list[RewardEntry]) -> list[str]:
    out = []
    for e in entries:
        if e.slug and e.price_state == "pending":
            out.append(e.slug)
        if e.set_slug and e.set_price_state == "pending":
            out.append(e.set_slug)
    return list(dict.fromkeys(out))


def best_plat(entries: list[RewardEntry]) -> int | None:
    """Slot with the highest platinum; ties go to the lower slot (overlay.js:345-378)."""
    best = None
    for e in sorted(entries, key=lambda e: e.slot):
        if e.plat and (best is None or e.plat > best.plat):
            best = e
    return best.slot if best else None


def best_ducats(entries: list[RewardEntry]) -> int | None:
    best = None
    for e in sorted(entries, key=lambda e: e.slot):
        if e.ducats and (best is None or e.ducats > best.ducats):
            best = e
    return best.slot if best else None


def now_ms() -> int:
    return int(time.time() * 1000)
