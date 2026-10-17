"""Relics page view-models (docs/port/menus.md §6, §19 get_relic_view / push_relic_overlay_filters).

RPCs
  relics.view(...)           filtered, sorted, paged relic groups with EV, owned counts, rewards
  relics.detail(name, squad) one relic: rewards per refinement (rarity, chance, price, ducats, owned)
                             and EV per refinement / squad size
  relics.top(limit, squad, era)  owned relics ranked by EV at the best owned refinement (QAM)
  relics.push_filters(push)  store the page filters for the planner overlay (RelicOverlayFilterPush)
  relics.clear_filters()     back to the overlay's own EV ranking (squad relic_planner.squad_size)
Planner RPCs (relics.planner_state/show/hide, relics.prefetch_prices) live in features/relic_planner.py.

All prices are read from the market cache only (no network on these calls);
relics.prefetch_prices queues the missing ones in the background.
"""

from __future__ import annotations

import time

from .. import log, rpc
from .. import relic_value as rv
from ..app import App, get_app

_log = log.get("menus.relics")

TIERS = ("Lith", "Meso", "Neo", "Axi", "Requiem")
MAX_LIMIT = 200


def _inventory():
    from .. import inventory as inv_mod

    inv = inv_mod.inventory()
    if inv is None:
        return None, None
    return inv, inv_mod.owned_relics()


def _inv_meta(inv) -> dict | None:
    if inv is None:
        return None
    return {"origin": inv.origin, "syncTime": inv.sync_time, "fetchedAt": int(inv.fetched_at * 1000)}


def _era_of(tier: str | None) -> str | None:
    if not tier or str(tier).lower() == "all":
        return None
    return rv.normalize_era(tier)


def build_view(*, tier: str = "all", ownership: str = "owned", quality: str = "owned", squad: int = 1,
               sort: str = "tier", direction: str = "asc", vaulted: str = "all", owned_above: int = 0,
               search: str = "", needed: bool = False, offset: int = 0, limit: int = 40,
               pinned: dict | None = None, rewards: bool = True) -> dict:
    from ..data.relics import relics

    db = relics()
    inv, owned = _inventory()
    has_inv = inv is not None
    squad = min(4, max(1, int(squad or 1)))
    if ownership not in ("owned", "all"):
        ownership = "owned"
    if quality not in rv.QUALITY_MODES:
        quality = "owned" if ownership == "owned" else "intact"
    valuer = rv.default_valuer()
    rows = rv.build_rows(db, owned if has_inv else None, squad=squad, era=None, ownership=ownership,
                         quality_mode=quality, pinned=pinned, valuer=valuer, with_rewards=rewards)
    tier_counts = {t: 0 for t in TIERS}
    tier_counts["all"] = len(rows)
    for r in rows:
        if r["tier"] in tier_counts:
            tier_counts[r["tier"]] += 1
    era = _era_of(tier)
    if era:
        rows = [r for r in rows if rv.era_matches(r["tier"], era)]
    filters = {"vaultedMode": vaulted if vaulted in rv.VAULTED_MODES else "all",
               "ownedAbove": int(owned_above or 0) if has_inv else 0, "search": (search or "")[:200],
               "containsNeededReward": bool(needed) and has_inv, "neededRewardKeys": None,
               "sortMode": sort if sort in rv.SORT_MODES else "tier",
               "sortDirection": direction if direction in ("asc", "desc") else "asc", "qualityMode": quality}
    rows = rv.select_rows(rows, filters, valuer, has_inventory=has_inv)
    total = len(rows)
    offset = max(0, int(offset or 0))
    limit = min(MAX_LIMIT, max(1, int(limit or 40)))
    page = [rv.public(r) for r in rows[offset:offset + limit]]
    owned_rows = [r for r in rows if r["ownedTotal"] > 0]
    totals = {
        "relics": sum(r["ownedTotal"] for r in owned_rows),
        "groups": len(owned_rows),
        # what cracking every owned copy at its shown refinement is worth (solo EV)
        "plat": round(sum((r["plat"] or 0) * r["count"] for r in owned_rows), 1) if squad == 1 else None,
        "ducat": round(sum((r["ducat"] or 0) * r["count"] for r in owned_rows), 1) if squad == 1 else None,
    }
    return {
        "stale": False, "generatedAt": int(time.time() * 1000), "hasInventory": has_inv,
        "inventory": _inv_meta(inv), "dataVersion": db.version, "squad": squad, "quality": quality,
        "total": total, "offset": offset, "limit": limit, "groups": page, "tierCounts": tier_counts,
        "totals": totals, "pricesMissing": len(valuer.missing),
    }


def build_detail(name: str, squad: int = 1) -> dict | None:
    from ..data.relics import relics

    db = relics()
    relic = db.get(name)
    if relic is None:
        return None
    inv, owned = _inventory()
    owned_g = rv.owned_by_group(owned or {}).get(relic.name, {q: 0 for q in rv.REFINEMENTS})
    valuer = rv.default_valuer()
    squad = min(4, max(1, int(squad or 1)))
    refinements = {}
    for q in rv.REFINEMENTS:
        rs = rv.sort_rewards([r for r in relic.rewards if r.chance(q) > 0], q)
        refinements[q] = [rv.reward_json(r, q, valuer) for r in rs]
    ev_by_squad = {}
    for n in (1, 2, 3, 4):
        ev_by_squad[str(n)] = {q: dict(zip(("plat", "ducat"), (rv._round(v) for v in rv.relic_ev(relic, q, n, valuer))))  # noqa: SLF001
                               for q in rv.REFINEMENTS}
    best = rv.pick_best_owned_quality(relic, owned_g, squad, valuer) if sum(owned_g.values()) else None
    return {
        "name": relic.name, "tier": relic.tier, "code": relic.code, "vaulted": relic.vaulted,
        "wfmSlug": relic.wfm_slug, "owned": owned_g, "ownedTotal": sum(owned_g.values()),
        "hasInventory": inv is not None, "squad": squad, "bestOwned": best,
        "refinementTable": rv.refinement_table(relic, squad, valuer, owned_g),
        "evBySquad": ev_by_squad, "rewards": refinements, "pricesMissing": len(valuer.missing),
    }


def build_top(limit: int = 10, squad: int | None = None, era: str | None = None) -> dict:
    from ..data.relics import relics

    inv, owned = _inventory()
    if inv is None:
        return {"hasInventory": False, "rows": [], "squad": squad or 1, "total": 0}
    if squad is None:
        p = get_app().services.get("relic_planner")
        squad = p.squad_size() if p else 4
    squad = min(4, max(1, int(squad)))
    valuer = rv.default_valuer()
    rows = rv.rank_by_ev(rv.build_rows(relics(), owned, squad=squad, era=_era_of(era), best_quality=True,
                                       valuer=valuer, with_rewards=False))
    limit = min(50, max(1, int(limit or 10)))
    return {"hasInventory": True, "squad": squad, "total": len(rows), "pricesMissing": len(valuer.missing),
            "rows": [rv.public(r) for r in rows[:limit]]}


def setup(app: App) -> None:
    """RPCs register at import; the page reloads on the inventory.updated event."""


@rpc.method("relics.view")
async def _rpc_view(tier: str = "all", ownership: str = "owned", quality: str = "owned", squad: int = 1,
                    sort: str = "tier", direction: str = "asc", vaulted: str = "all", ownedAbove: int = 0,
                    search: str = "", needed: bool = False, offset: int = 0, limit: int = 40,
                    pinned: dict | None = None, rewards: bool = True) -> dict:
    def run() -> dict:
        out = build_view(
            tier=tier, ownership=ownership, quality=quality, squad=squad, sort=sort, direction=direction,
            vaulted=vaulted, owned_above=ownedAbove, search=search, needed=needed, offset=offset, limit=limit,
            pinned=pinned, rewards=rewards)
        rv.save_price_snapshot()  # keep last-known prices across restarts
        return out

    return await get_app().run_blocking(run)


@rpc.method("relics.detail")
async def _rpc_detail(name: str, squad: int = 1) -> dict | None:
    return await get_app().run_blocking(build_detail, name, squad)


@rpc.method("relics.top")
async def _rpc_top(limit: int = 10, squad: int | None = None, era: str | None = None) -> dict:
    return await get_app().run_blocking(build_top, limit, squad, era)


@rpc.method("relics.push_filters")
def _rpc_push(push: dict) -> dict:
    """Store the page filters for the overlay (normalizeRelicOverlayFilterPush)."""
    app = get_app()
    prev = app.settings.get("relic_planner.filters")
    f = rv.normalize_filters(push, prev if isinstance(prev, dict) else None)
    # settings.update merges dicts; replace pinnedQualities wholesale by clearing first
    app.settings.update({"relic_planner": {"filters": None}})
    app.settings.update({"relic_planner": {"filters": f}})
    return f


@rpc.method("relics.clear_filters")
def _rpc_clear() -> dict:
    get_app().settings.update({"relic_planner": {"filters": None}})
    return {"ok": True}
