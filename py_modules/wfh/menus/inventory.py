"""Inventory page view-models (docs/port/menus.md §3; WFHelper InventoryView.svelte).

All parsing lives in wfh.inventory_view (parseInventory / full sets / resources ports), cached per
inventory sync; this module adds tabs, the value strip, filters, sorting and paging. Prices are
read from the warframe.market cache only; inventory.prefetch_prices queues the missing ones (and,
with `inventory_ui.auto_prefetch`, every new snapshot queues its prime parts in the background).

RPCs
  inventory.view(tab, search, sort, direction, filters, offset, limit, everything_hidden,
                 full_sets_hidden, show_incomplete)    one page of rows + tab counts + value strip
  inventory.item(key)                 one row with its set components / parent set progress
  inventory.qam()                     value totals for the QAM (scope + floor from settings)
  inventory.prefetch_prices(tab?, limit?)   queue missing prices of a tab (default all_parts)
  inventory.ui_set(value_scope?, value_min_plat?, hide_foundry_claims?, auto_prefetch?)
Existing: inventory.status / inventory.refresh / inventory.import_file (wfh.inventory).
Settings: inventory_ui.{value_scope prime|tradable, value_min_plat 0|5|10|15, hide_foundry_claims,
auto_prefetch}.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from .. import inventory_view as iv
from .. import log, rpc
from ..app import App, get_app

_log = log.get("menus.inventory")

PAGE_MAX = 120
PAGE_DEFAULT = 30
PREFETCH_MAX = 400
AUTO_PREFETCH_MAX_AGE = 6 * 3600  # sync-triggered refresh: re-queue prices older than this
SCOPES = ("prime", "tradable")
YES_NO = ("all", "yes", "no")


# ---- context --------------------------------------------------------------------------------

_mi_lock = threading.Lock()
_mi_cache: tuple[int, iv.MarketIndex] | None = None
_catalog_loading = False


def market_index() -> iv.MarketIndex:
    """WFM catalog index; empty while the catalog is unreachable (the views still work)."""
    global _mi_cache, _catalog_loading
    cat: list = []
    try:
        from ..data import market

        m = market.get()
        loaded = m.catalog_if_loaded()  # never block a page on the 24 h catalog download
        if loaded is not None:
            cat = loaded
        elif not _catalog_loading:
            _catalog_loading = True

            def load() -> None:
                global _catalog_loading
                try:
                    m.catalog()
                finally:
                    _catalog_loading = False
            threading.Thread(target=load, name="wfh-wfm-catalog", daemon=True).start()
    except Exception:  # noqa: BLE001 - offline
        cat = []
    with _mi_lock:
        if _mi_cache is not None and _mi_cache[0] == id(cat) and len(cat) > 0:
            return _mi_cache[1]
        mi = iv.MarketIndex(cat)
        _mi_cache = (id(cat), mi)
        return mi


def _relic_db():
    try:
        from ..data.relics import relics

        return relics()
    except Exception:  # noqa: BLE001 - relic data missing
        return None


def prefs(settings) -> dict:
    scope = settings.get("inventory_ui.value_scope", "prime") if settings else "prime"
    try:
        floor = float(settings.get("inventory_ui.value_min_plat", 0) or 0) if settings else 0.0
    except (TypeError, ValueError):
        floor = 0.0
    return {"valueScope": scope if scope in SCOPES else "prime",
            "valueMinPlat": floor if floor in iv.VALUE_MIN_PLATINUM_PRESETS else 0,
            "hideFoundryClaims": bool(settings.get("inventory_ui.hide_foundry_claims", True)) if settings else True,
            "autoPrefetch": bool(settings.get("inventory_ui.auto_prefetch", True)) if settings else True}


def _settings():
    try:
        return get_app().settings
    except RuntimeError:
        return None


def current_snapshot(inv=None, p: dict | None = None) -> iv.Snapshot | None:
    from .. import inventory as inv_mod

    inv = inv if inv is not None else inv_mod.inventory()
    if inv is None:
        return None
    p = p or prefs(_settings())
    return iv.snapshot(inv, market_index(), p["hideFoundryClaims"], _relic_db())


def inv_meta(inv) -> dict | None:
    if inv is None:
        return None
    return {"origin": inv.origin, "syncTime": inv.sync_time, "fetchedAt": int(inv.fetched_at * 1000),
            "path": inv.path or None}


def deck_home() -> str:
    try:
        from .. import session

        return session.deck_user().pw_dir
    except Exception:  # noqa: BLE001
        return "/home/deck"


def _status() -> dict:
    try:
        from .. import inventory as inv_mod

        return inv_mod.status()
    except Exception:  # noqa: BLE001
        return {}


# ---- pure view building ---------------------------------------------------------------------

def normalize_filters(raw: dict | None) -> dict:
    f = dict(iv.DEFAULT_FILTERS)
    raw = raw or {}
    for k in ("vaulted", "favorite", "equipped", "leveledUp"):
        if raw.get(k) in YES_NO:
            f[k] = raw[k]
    if raw.get("partType") in ("all", "normal", "prime"):
        f["partType"] = raw["partType"]
    try:
        f["minimumPlatinum"] = max(0.0, float(raw.get("minimumPlatinum") or 0))
    except (TypeError, ValueError):
        pass
    try:
        f["minimumAmount"] = max(0, int(raw.get("minimumAmount") or 0))
    except (TypeError, ValueError):
        pass
    return f


def sorts_for(tab: str) -> tuple[str, ...]:
    return iv.SORTS_BY_TAB.get(tab, iv.DEFAULT_SORTS)


def full_set_category(row: dict, db: iv.Db) -> str:
    root = iv.set_root_of(row["internalName"])
    return iv.classify_for_foundry(root, root, db)


def build_view(snap: iv.Snapshot | None, price_of, *, tab: str = "all_parts", search: str = "",
               sort: str | None = None, direction: str | None = None, filters: dict | None = None,
               offset: int = 0, limit: int = PAGE_DEFAULT, everything_hidden: list | None = None,
               full_sets_hidden: list | None = None, show_incomplete: bool = False,
               scope: str = "prime", min_plat: float = 0) -> dict:
    tab = tab if tab in iv.TABS else "all_parts"
    allowed = sorts_for(tab)
    if tab == "full_sets" and show_incomplete and not sort:
        sort = "missing_parts"
    sort = sort if sort in allowed or (tab == "full_sets" and sort == "missing_parts") else "name"
    direction = direction if direction in ("asc", "desc") else iv.default_sort_direction(sort)
    if snap is None:
        return {"hasInventory": False, "tab": tab, "rows": [], "total": 0}
    hidden = [s for s in (everything_hidden if everything_hidden is not None else ["full_sets"])
              if s in iv.EVERYTHING_SOURCES]
    rows = iv.attach_prices(snap.base, price_of)
    counts = {t: 0 for t in iv.TABS}
    for r in rows:
        g = r["inventoryGroup"]
        if g in counts:
            counts[g] += 1
        if iv.matches_tab(r, "everything", hidden):
            counts["everything"] += 1
    if tab == "full_sets":
        tab_rows = [r for r in rows if r["inventoryGroup"] == "full_sets"
                    or (show_incomplete and r["inventoryGroup"] == "incomplete_sets")]
    else:
        tab_rows = [r for r in rows if iv.matches_tab(r, tab, hidden)]
    set_cats: dict[str, int] = {}
    if tab == "full_sets":
        for r in tab_rows:
            r["setCategory"] = full_set_category(r, snap.db)
            set_cats[r["setCategory"]] = set_cats.get(r["setCategory"], 0) + 1
        hidden_cats = set(full_sets_hidden or [])
        tab_rows = [r for r in tab_rows if r["setCategory"] not in hidden_cats]
    f = normalize_filters(filters)
    f["search"] = (search or "")[:200]
    shown = [r for r in tab_rows if iv.matches_shared_filters(r, f)]
    shown = iv.sort_rows(shown, sort, direction)
    total = len(shown)
    offset = max(0, int(offset or 0))
    limit = min(PAGE_MAX, max(1, int(limit or PAGE_DEFAULT)))
    page = []
    for r in shown[offset:offset + limit]:
        pr = iv.public_row(r)
        if tab == "full_sets":
            pr["setCategory"] = r.get("setCategory")
        page.append(pr)
    everything = [r for r in rows if r["inventoryGroup"] != "resources"]
    in_view = iv.compute_value_totals(shown, scope, min_plat)
    whole = iv.compute_value_totals(everything, scope, min_plat)
    missing_prices = sum(1 for r in shown if r.get("marketSlug") and r.get("platinum") is None)
    return {
        "hasInventory": True, "tab": tab, "sort": sort, "direction": direction, "sorts": list(allowed) + (
            ["missing_parts"] if tab == "full_sets" and "missing_parts" not in allowed else []),
        "total": total, "offset": offset, "limit": limit, "rows": page,
        "tabs": [{"id": t, "label": iv.TAB_LABELS[t], "count": counts[t]} for t in iv.TABS],
        "everythingHidden": hidden, "setCategories": [{"id": c, "count": set_cats[c]}
                                                      for c in iv.EQUIPMENT_CATEGORY_ORDER if c in set_cats],
        "value": {"scope": scope, "minPlat": min_plat, "inView": in_view, "inventory": whole,
                  "rows": iv.select_value_strip_rows(in_view, whole)},
        "pricesMissing": missing_prices,
    }


def build_item(snap: iv.Snapshot, price_of, key: str) -> dict | None:
    rows = iv.attach_prices(snap.base, price_of)
    row = next((r for r in rows if r.get("key") == key), None)
    if row is None:
        return None
    out = iv.public_row(row)
    out["equippedIn"] = row.get("equippedIn") or []
    out["modularParts"] = row.get("modularParts") or []
    out["priceRank"] = iv.price_rank(row)
    comps = row.get("components")
    if isinstance(comps, list) and row["inventoryGroup"] in ("full_sets", "incomplete_sets"):
        out["components"] = [{"name": c["name"], "uniqueName": c["uniqueName"], "needed": c["itemCount"],
                              "owned": c["ownedCount"], "ducats": c.get("ducats")} for c in comps]
    # parent set progress for a part
    u = row["internalName"]
    aliases = set(iv.component_aliases(u))
    for s in rows:
        if s["inventoryGroup"] not in ("full_sets", "incomplete_sets"):
            continue
        if any(c["uniqueName"] in aliases or aliases & set(iv.component_aliases(c["uniqueName"]))
               for c in s.get("components") or []):
            out["parentSet"] = {"key": s["key"], "name": s["name"], "completeSets": s.get("completeSets"),
                                "missingParts": s.get("missingParts"), "platinum": s.get("platinum")}
            break
    # recipe of what this blueprint builds
    rec = snap.db.extra.recipes.get(u)
    if rec is not None:
        out["recipe"] = {"product": snap.db.name_of(rec.result), "credits": rec.build_price,
                         "buildTime": rec.build_time,
                         "ingredients": [{"name": snap.db.name_of(i), "needed": c, "owned": int(snap.owned.get(i, 0))}
                                         for i, c in rec.ingredients]}
    return out


def qam_view(snap: iv.Snapshot | None, price_of, p: dict) -> dict:
    if snap is None:
        return {"hasInventory": False, "scope": p["valueScope"], "minPlat": p["valueMinPlat"],
                "presets": list(iv.VALUE_MIN_PLATINUM_PRESETS)}
    rows = [r for r in iv.attach_prices(snap.base, price_of) if r["inventoryGroup"] != "resources"]
    totals = iv.compute_value_totals(rows, p["valueScope"], p["valueMinPlat"])
    counted = [r for r in rows if iv.is_counted_for_value(r, p["valueScope"])]
    top = sorted((r for r in counted if r.get("platinum")), key=lambda r: -(r["platinum"] * max(1, r.get("amount") or 1)))
    return {"hasInventory": True, "scope": p["valueScope"], "minPlat": p["valueMinPlat"],
            "presets": list(iv.VALUE_MIN_PLATINUM_PRESETS), "totals": totals,
            "top": [{"name": r["name"], "amount": r.get("amount"), "platinum": r["platinum"],
                     "ducats": r.get("ducats")} for r in top[:3]]}


def prefetch_slugs(snap: iv.Snapshot, tab: str = "all_parts", limit: int = PREFETCH_MAX,
                   max_age: float | None = None, market=None) -> dict[Any, list[str]]:
    """{rank: [slug]} of sellable rows in `tab` (or the prime scope) whose cached price is
    missing or older than `max_age` (default: the market's PRICE_TTL)."""
    from ..data import market as market_mod

    m = market or market_mod.get()
    age = market_mod.PRICE_TTL if max_age is None else max_age
    by_rank: dict[Any, list[str]] = {}
    n = 0
    for r in snap.base:
        if tab == "prime":
            if not iv.is_counted_for_value(r, "prime"):
                continue
        elif not iv.matches_tab(r, tab) or r.get("tradable") is False:
            continue
        slug = r.get("marketSlug")
        if not slug:
            continue
        rank = iv.price_rank(r)
        lst = by_rank.setdefault(rank, [])
        if slug in lst or m.cached(slug, rank, max_age=age)[0]:
            continue
        lst.append(slug)
        n += 1
        if n >= limit:
            break
    return {k: v for k, v in by_rank.items() if v}


def queue_prefetch(snap: iv.Snapshot, tab: str, limit: int = PREFETCH_MAX, max_age: float | None = None) -> int:
    from ..data import market

    queued = 0
    for rank, slugs in prefetch_slugs(snap, tab, limit, max_age).items():
        queued += market.get().prefetch(slugs, rank)
    return queued


# ---- RPCs -----------------------------------------------------------------------------------

def _view_work(params: dict) -> dict:
    from .. import inventory as inv_mod

    p = prefs(_settings())
    inv = inv_mod.inventory()
    if inv is None:
        return {"hasInventory": False, "status": _status(), "home": deck_home(), "prefs": p, "rows": [],
                "total": 0, "tab": params.get("tab") or "all_parts"}
    snap = current_snapshot(inv, p)
    price_of, _ = iv.market_price_of()
    out = build_view(snap, price_of, scope=p["valueScope"], min_plat=p["valueMinPlat"], **params)
    out.update({"inventory": inv_meta(inv), "home": deck_home(), "prefs": p, "generatedAt": int(time.time() * 1000)})
    return out


@rpc.method("inventory.view")
async def _rpc_view(tab: str = "all_parts", search: str = "", sort: str | None = None, direction: str | None = None,
                    filters: dict | None = None, offset: int = 0, limit: int = PAGE_DEFAULT,
                    everything_hidden: list | None = None, full_sets_hidden: list | None = None,
                    show_incomplete: bool = False) -> dict:
    params = {"tab": tab, "search": search, "sort": sort, "direction": direction, "filters": filters,
              "offset": offset, "limit": limit, "everything_hidden": everything_hidden,
              "full_sets_hidden": full_sets_hidden, "show_incomplete": bool(show_incomplete)}
    return await get_app().run_blocking(_view_work, params)


@rpc.method("inventory.item")
async def _rpc_item(key: str) -> dict | None:
    def work() -> dict | None:
        snap = current_snapshot()
        if snap is None:
            return None
        price_of, _ = iv.market_price_of()
        return build_item(snap, price_of, key)

    return await get_app().run_blocking(work)


@rpc.method("inventory.qam")
async def _rpc_qam() -> dict:
    def work() -> dict:
        from .. import inventory as inv_mod

        p = prefs(_settings())
        inv = inv_mod.inventory()
        snap = current_snapshot(inv, p) if inv is not None else None
        price_of, _ = iv.market_price_of()
        out = qam_view(snap, price_of, p)
        out["inventory"] = inv_meta(inv)
        return out

    return await get_app().run_blocking(work)


@rpc.method("inventory.prefetch_prices")
async def _rpc_prefetch(tab: str = "all_parts", limit: int = PREFETCH_MAX) -> dict:
    def work() -> dict:
        snap = current_snapshot()
        if snap is None:
            return {"queued": 0}
        t = tab if tab in iv.TABS or tab == "prime" else "all_parts"
        return {"queued": queue_prefetch(snap, t, min(PREFETCH_MAX, max(1, int(limit or PREFETCH_MAX))))}

    return await get_app().run_blocking(work)


@rpc.method("inventory.ui_set")
async def _rpc_ui_set(value_scope: str | None = None, value_min_plat: float | None = None,
                      hide_foundry_claims: bool | None = None, auto_prefetch: bool | None = None) -> dict:
    app = get_app()
    patch: dict[str, Any] = {}
    if value_scope in SCOPES:
        patch["value_scope"] = value_scope
    if value_min_plat is not None:
        try:
            v = float(value_min_plat)
        except (TypeError, ValueError):
            v = 0.0
        patch["value_min_plat"] = v if v in iv.VALUE_MIN_PLATINUM_PRESETS else 0
    if hide_foundry_claims is not None:
        patch["hide_foundry_claims"] = bool(hide_foundry_claims)
    if auto_prefetch is not None:
        patch["auto_prefetch"] = bool(auto_prefetch)
    if patch:
        await app.run_blocking(app.settings.update, {"inventory_ui": patch})
    return prefs(app.settings)


def setup(app: App) -> None:
    """Background: a new snapshot warms its view cache and queues its prime part prices."""
    if os.environ.get("WFH_HEADLESS"):
        return

    def warm(inv) -> None:
        try:
            p = prefs(app.settings)
            snap = current_snapshot(inv, p)
            if snap is not None and p["autoPrefetch"]:
                n = queue_prefetch(snap, "prime", max_age=AUTO_PREFETCH_MAX_AGE)
                if n:
                    _log.info("queued %d prime part prices", n)
        except Exception:  # noqa: BLE001 - never break the inventory thread
            _log.exception("inventory view warm-up failed")

    try:
        from .. import inventory as inv_mod

        inv_mod.on_update(lambda inv: app.submit(warm, inv))
    except Exception:  # noqa: BLE001
        _log.exception("inventory listener not attached")
