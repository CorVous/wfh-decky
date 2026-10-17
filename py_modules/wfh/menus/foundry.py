"""Foundry page view-models (docs/port/menus.md §4; WFHelper FoundryView.svelte).

Rows come from wfh.inventory_view.parse_foundry (PendingRecipes -> claimable / in progress,
Recipes -> ready to build / missing parts, with the chain-buildable check), cached per inventory
sync; statuses depend on the clock so they are recomputed on every call.

RPCs
  foundry.view(tab, search, prime, state, vaulted, sort, direction, offset, limit)
      tab: all | status:in-progress | cat:<Warframe|Primary|...>; state: all | claimable | not_ready |
      buildable | buildable_sets; sort: count | time | name. Pinned blueprints first, then status.
  foundry.pin(unique_name, pinned)   pin/unpin a blueprint (setting foundry.pinned, max 60)
  foundry.qam(limit)                 counts, the actionable rows and the pinned totals for the QAM
Settings: foundry.pinned (list of blueprint uniqueNames).
"""

from __future__ import annotations

import time
from typing import Any

from .. import inventory_view as iv
from .. import log, rpc
from ..app import App, get_app
from . import inventory as inv_menu

_log = log.get("menus.foundry")

STATES = ("all", "claimable", "not_ready", "buildable", "buildable_sets")
PAGE_MAX = 100
PAGE_DEFAULT = 40


def _now() -> int:
    return int(time.time() * 1000)


def pinned_list(settings) -> list[str]:
    raw = settings.get("foundry.pinned", []) if settings else []
    return [x for x in raw if isinstance(x, str)][: iv.PINNED_MAX] if isinstance(raw, list) else []


def counts_of(rows: list[dict]) -> dict:
    c = {"claimable": 0, "building": 0, "ready": 0, "missing": 0}
    for r in rows:
        c[{"claimable": "claimable", "in-progress": "building", "ready-to-build": "ready",
           "not-ready": "missing"}[r["status"]]] += 1
    return c


def pinned_summary(rows: list[dict], pinned: list[str], snap: iv.Snapshot) -> dict:
    t = iv.compute_pinned_totals(rows, set(pinned), lambda u: int(snap.owned.get(u, 0)))
    name = snap.db.name_of
    for key in ("resources", "missing"):
        t[key] = [{**r, "name": name(r["uniqueName"])} for r in t[key]]
    t["names"] = [r["name"] for r in rows if r["source"] == "blueprint" and r.get("uniqueName") in set(pinned)][:20]
    return t


def build_view(snap: iv.Snapshot | None, now: int, pinned: list[str], *, tab: str = "all", search: str = "",
               prime: str = "all", state: str = "all", vaulted: str = "all", sort: str = "count",
               direction: str | None = None, offset: int = 0, limit: int = PAGE_DEFAULT) -> dict:
    if snap is None:
        return {"hasInventory": False, "rows": [], "total": 0}
    rows = iv.foundry_entries(snap.foundry, snap.owned, snap.db, now, snap.product_owned)
    sort = sort if sort in iv.FOUNDRY_SORTS else "count"
    direction = direction if direction in ("asc", "desc") else iv.default_sort_direction(sort)
    tabs = [{"id": "all", "label": "All", "count": len(rows)},
            {"id": "status:in-progress", "label": "In progress",
             "count": sum(1 for r in rows if r["status"] in ("in-progress", "claimable"))}]
    for cat in iv.EQUIPMENT_CATEGORY_ORDER:
        n = sum(1 for r in rows if r["category"] == cat)
        if n:
            tabs.append({"id": f"cat:{cat}", "label": cat, "count": n})
    valid_tabs = {t["id"] for t in tabs}
    tab = tab if tab in valid_tabs else "all"
    f = dict(iv.DEFAULT_FILTERS)
    f.update({"search": (search or "")[:200], "primeMode": prime if prime in ("all", "prime", "non_prime") else "all",
              "foundryState": state if state in STATES else "all",
              "vaulted": vaulted if vaulted in inv_menu.YES_NO else "all"})
    shown = [r for r in rows if iv.passes_foundry_tab(r, tab) and iv.matches_shared_filters(r, f)]
    pin_set = set(pinned)
    shown = iv.sort_foundry(shown, sort, direction, pin_set)
    total = len(shown)
    offset = max(0, int(offset or 0))
    limit = min(PAGE_MAX, max(1, int(limit or PAGE_DEFAULT)))
    page = []
    for r in shown[offset:offset + limit]:
        pr = iv.public_foundry_row(r)
        pr["pinned"] = r["source"] == "blueprint" and r.get("uniqueName") in pin_set
        page.append(pr)
    return {"hasInventory": True, "now": now, "tab": tab, "sort": sort, "direction": direction, "tabs": tabs,
            "counts": counts_of(rows), "total": total, "offset": offset, "limit": limit, "rows": page,
            "pinned": pinned, "pinnedTotals": pinned_summary(rows, pinned, snap),
            "recipeData": bool(snap.db.extra)}


def qam_view(snap: iv.Snapshot | None, now: int, pinned: list[str], limit: int = 6) -> dict:
    if snap is None:
        return {"hasInventory": False}
    rows = iv.foundry_entries(snap.foundry, snap.owned, snap.db, now, snap.product_owned)
    claim = sorted((r for r in rows if r["status"] == "claimable"), key=lambda r: r.get("endMs") or 0)
    building = sorted((r for r in rows if r["status"] == "in-progress"), key=lambda r: r.get("endMs") or 0)
    ready = iv.sort_rows([r for r in rows if r["status"] == "ready-to-build" and not r["looseComponent"]],
                         "name", "asc") + iv.sort_rows(
        [r for r in rows if r["status"] == "ready-to-build" and r["looseComponent"]], "name", "asc")
    pick = (claim + building + ready)[: max(1, min(20, int(limit or 6)))]
    return {"hasInventory": True, "now": now, "counts": counts_of(rows),
            "rows": [{"key": r["key"], "name": r["name"], "status": r["status"], "endMs": r.get("endMs"),
                      "count": r.get("count"), "chainOnly": r.get("chainOnly")} for r in pick],
            "nextReadyMs": building[0]["endMs"] if building else None,
            "pinnedTotals": pinned_summary(rows, pinned, snap)}


# ---- RPCs -----------------------------------------------------------------------------------

@rpc.method("foundry.view")
async def _rpc_view(tab: str = "all", search: str = "", prime: str = "all", state: str = "all",
                    vaulted: str = "all", sort: str = "count", direction: str | None = None, offset: int = 0,
                    limit: int = PAGE_DEFAULT) -> dict:
    app = get_app()

    def work() -> dict:
        from .. import inventory as inv_mod

        inv = inv_mod.inventory()
        snap = inv_menu.current_snapshot(inv) if inv is not None else None
        out = build_view(snap, _now(), pinned_list(app.settings), tab=tab, search=search, prime=prime,
                         state=state, vaulted=vaulted, sort=sort, direction=direction, offset=offset, limit=limit)
        out["inventory"] = inv_menu.inv_meta(inv)
        if inv is None:
            out["status"] = inv_menu._status()  # noqa: SLF001 - same package
            out["home"] = inv_menu.deck_home()
        return out

    return await app.run_blocking(work)


@rpc.method("foundry.pin")
async def _rpc_pin(unique_name: str, pinned: bool = True) -> dict:
    app = get_app()
    cur = pinned_list(app.settings)
    if pinned and unique_name and unique_name not in cur:
        cur = (cur + [unique_name])[-iv.PINNED_MAX:]
    elif not pinned:
        cur = [u for u in cur if u != unique_name]
    await app.run_blocking(app.settings.update, {"foundry": {"pinned": cur}})
    return {"pinned": cur}


@rpc.method("foundry.qam")
async def _rpc_qam(limit: int = 6) -> dict:
    app = get_app()

    def work() -> dict[str, Any]:
        from .. import inventory as inv_mod

        inv = inv_mod.inventory()
        snap = inv_menu.current_snapshot(inv) if inv is not None else None
        return qam_view(snap, _now(), pinned_list(app.settings), limit)

    return await app.run_blocking(work)


def setup(app: App) -> None:
    pass
