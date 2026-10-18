"""Market page / QAM view-models (docs/port/menus.md §9, data-sources.md §1.4-1.7).

Browse: an item's price summary and its top online sell/buy orders, each with the whisper
WFHelper copies (orderRows.ts orderWhisper, "/w <user> Hi! I want to buy: ..."). Viewed items
go to a recently-viewed list (<runtime>/market-recent.json) that the QAM prices from cache.

Account (wfm_auth.py): sign-in (e-mail/password, header JWT kept in <settings>/wfm.session,
0600), my orders (30 s cache, MarketView ORDERS_STALE_MS) with sold-one/close, delete,
visibility and price/quantity edits, and the online status (ingame/online/invisible).

RPCs
  market.detail(slug, rank?, record=True, force=False)
                                              price + top 5 orders per side with whispers; force
                                              re-reads the order book (once per FORCE_MIN_S per item)
  market.recent()                             recently viewed with cached prices (+ prefetch)
  market.recent_remove(slug) / market.recent_clear()
  market.qam()                                recent (qam_recent rows) + account + my order counts
  market.account(refresh=False)               sign-in state (+ /v2/me and status when refresh)
  market.sign_in(email, password) / market.sign_out()
  market.set_status(status, hold_min?)      hold minutes (0 = until changed), remembered
  market.set_hold(hold_min)                   remember the hold minutes alone (no status known yet)
  market.my_orders(force=False)
  market.order_close(id, quantity?)           one trade (perTrade) unless quantity given
  market.order_delete(id)
  market.order_visible(id, visible) / market.orders_visible(visible, type?)
  market.order_update(id, platinum?, quantity?)
Events
  market.orders   {orders, counts, fetchedAt, error}   after any order change or refresh
  market.account  account state after sign-in/out/status change
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from .. import log, rpc
from ..app import App, get_app
from ..data import market as mk
from ..data import wfm_auth

_log = log.get("menus.market")

RECENT_FILE = "market-recent.json"
RECENT_MAX = 20
ORDERS_STALE_S = 30
TOP_ROWS = 5
QAM_RECENT_DEFAULT = 4
FORCE_MIN_S = 45            # MarketBrowseView auto refresh: at most one forced order-book read per item
PREFETCH_RETRY_S = mk.ERROR_COOLDOWN  # recent_rows queues a slug at most once per this window

_auth: wfm_auth.WfmAuth | None = None
_auth_lock = threading.Lock()
_recent: "RecentStore | None" = None
# at: last successful fetch; tried: last attempt (success or failure), the back-off clock
_orders_cache: dict[str, Any] = {"at": 0.0, "tried": 0.0, "orders": None, "error": None}
_orders_lock = threading.Lock()
_prefetched: dict[tuple[str, Any], float] = {}
_forced: dict[tuple[str, Any], float] = {}
_ts_lock = threading.Lock()


# ---- pure ------------------------------------------------------------------------------------

def whisper(order_side: str, entry: dict, item_text: str) -> str:
    """orderRows.ts orderWhisper: a sell order gets a buy message and back."""
    verb = "buy" if order_side == "sell" else "sell"
    per = int(entry.get("per_trade") or entry.get("perTrade") or 1)
    plat = entry.get("platinum")
    user = entry.get("user") or ""
    count = f" x{per}" if per > 1 else ""
    return f"/w {user} Hi! I want to {verb}: {item_text}{count} for {plat} platinum. (warframe.market via WFHelper)"


def item_text(name: str, rank: int | None, ranked: bool, subtype: str | None = None) -> str:
    """MarketBrowseView buildWhisper item text: name + (Atragraph) + (Rank N) for ranked items."""
    out = name
    if subtype == "atragraph":
        out += " (Atragraph)"
    if ranked and rank is not None:
        out += f" (Rank {rank})"
    return out


def order_rows(orders: list[dict], side: str, name: str, ranked: bool, limit: int = TOP_ROWS) -> list[dict]:
    out = []
    for e in orders[:limit]:
        out.append({
            "user": e.get("user"), "status": e.get("status") or "", "platinum": e.get("platinum"),
            "unit": e.get("unit"), "quantity": e.get("quantity"), "perTrade": e.get("per_trade") or 1,
            "rank": e.get("rank"), "reputation": e.get("reputation"),
            "whisper": whisper(side, e, item_text(name, e.get("rank"), ranked, e.get("subtype"))),
        })
    return out


def price_summary(p: mk.Price | None, now: float) -> dict | None:
    if p is None:
        return None
    return {"plat": p.plat, "median": p.median, "lowSell": p.low_sell, "highBuy": p.high_buy, "volume": p.volume,
            "sellers": p.sellers, "source": p.source, "fetchedAt": int(p.fetched_at * 1000),
            "ageS": max(0, int(now - p.fetched_at))}


def order_counts(orders: list[dict] | None) -> dict:
    rows = orders or []
    sell = [o for o in rows if o.get("type") == "sell"]
    buy = [o for o in rows if o.get("type") == "buy"]
    return {"sell": len(sell), "buy": len(buy), "total": len(rows),
            "visible": sum(1 for o in rows if o.get("visible")),
            "sellPlat": sum(int(o.get("platinum") or 0) * int(o.get("quantity") or 1) for o in sell if o.get("visible"))}


def close_quantity(order: dict, quantity: int | None = None) -> int:
    """normalizePerTrade: one trade closes perTrade pieces, never more than listed."""
    q = quantity if isinstance(quantity, int) and not isinstance(quantity, bool) and quantity > 0 \
        else int(order.get("perTrade") or 1)
    return max(1, min(q, int(order.get("quantity") or 1)))


def apply_close(orders: list[dict], order_id: str, qty: int) -> list[dict]:
    """Optimistic local update after a close (MarketView :718-754): subtract, drop at zero."""
    out = []
    for o in orders:
        if o.get("id") == order_id:
            left = int(o.get("quantity") or 1) - qty
            if left <= 0:
                continue
            o = {**o, "quantity": left, "perTrade": min(int(o.get("perTrade") or 1), left)}
        out.append(o)
    return out


# ---- recently viewed ---------------------------------------------------------------------------

class RecentStore:
    def __init__(self, base_dir: str | None, limit: int = RECENT_MAX):
        self.path = os.path.join(base_dir, RECENT_FILE) if base_dir else None
        self.limit = limit
        self._lock = threading.Lock()
        self._items: list[dict] = []
        if self.path:
            try:
                with open(self.path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, list):
                    self._items = [e for e in d if isinstance(e, dict) and isinstance(e.get("slug"), str)][:limit]
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                _log.warning("%s unreadable: %s", RECENT_FILE, e)

    def items(self) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._items]

    def add(self, slug: str, name: str, rank: int | None = None, at: float | None = None) -> None:
        with self._lock:
            self._items = [e for e in self._items if not (e["slug"] == slug and e.get("rank") == rank)]
            self._items.insert(0, {"slug": slug, "name": name, "rank": rank, "at": int((at or time.time()) * 1000)})
            del self._items[self.limit:]
            self._save()

    def remove(self, slug: str) -> None:
        with self._lock:
            self._items = [e for e in self._items if e["slug"] != slug]
            self._save()

    def clear(self) -> None:
        with self._lock:
            self._items = []
            self._save()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._items, f)
            os.replace(tmp, self.path)
        except OSError as e:
            _log.warning("could not save %s: %s", RECENT_FILE, e)


def recent_rows(store: RecentStore, m: mk.Market, now: float, limit: int | None = None,
                prefetch: bool = True) -> list[dict]:
    """Recent items with cached prices only (never blocks on the network)."""
    items = store.items()[:limit] if limit else store.items()
    rows, stale = [], []
    for e in items:
        hit, p = m.cached(e["slug"], e.get("rank"))
        if not hit:
            _, p = m.cached(e["slug"], e.get("rank"), max_age=float("inf"))  # last known, expired
            stale.append(e)
        rows.append({**e, "price": price_summary(p, now), "fresh": hit})
    if prefetch and stale and not m.host_down():
        # Each prefetch emits market.price, which makes the page reload this list; queue a slug
        # at most once per PREFETCH_RETRY_S so an item in its error cool-down cannot loop.
        mono = time.monotonic()
        by_rank: dict[Any, list[str]] = {}
        with _ts_lock:
            for e in stale:
                k = (e["slug"], e.get("rank"))
                if mono - _prefetched.get(k, -PREFETCH_RETRY_S) < PREFETCH_RETRY_S:
                    continue
                _prefetched[k] = mono
                by_rank.setdefault(e.get("rank"), []).append(e["slug"])
        for rank, slugs in by_rank.items():
            m.prefetch(slugs, rank, notify=True)
    return rows


# ---- item detail -----------------------------------------------------------------------------

def _owned(game_ref: str | None) -> int | None:
    if not game_ref:
        return None
    try:
        from .. import inventory

        if inventory.inventory() is None:
            return None
        return inventory.owned_count(game_ref)
    except Exception:  # noqa: BLE001
        return None


def _may_force(slug: str, rank: int | None) -> bool:
    """One forced order-book read per item per FORCE_MIN_S (Refresh button + 45 s auto refresh)."""
    k, mono = (slug, rank), time.monotonic()
    with _ts_lock:
        if mono - _forced.get(k, -FORCE_MIN_S) < FORCE_MIN_S:
            return False
        _forced[k] = mono
        return True


def detail(m: mk.Market, slug: str, rank: int | None = None, now: float | None = None,
           force: bool = False) -> dict:
    now = time.time() if now is None else now
    raw = (slug or "").strip()
    cat = m.lookup(raw)
    s = cat.slug if cat else (mk.names.normalize_wfm_slug(raw) or "")
    if not s:
        return {"slug": raw, "error": "Unknown item."}
    if force and _may_force(s, rank):
        m.price(s, rank, force=True)  # one /top read refreshes both the price and the order book
    o = m.orders(s, rank)  # refetches by itself once the order book is older than PRICE_TTL
    p = m.price(s, rank)
    name = cat.name if cat else s.replace("_", " ").title()
    max_rank = cat.max_rank if cat else None
    ranked = bool(max_rank)
    return {
        "slug": s, "name": name, "rank": rank, "maxRank": max_rank, "thumb": cat.thumb if cat else None,
        "gameRef": cat.game_ref if cat else None, "owned": _owned(cat.game_ref if cat else None),
        "url": f"{mk.SITE}/items/{s}", "price": price_summary(p, now),
        "sell": order_rows(o.get("sell") or [], "sell", name, ranked),
        "buy": order_rows(o.get("buy") or [], "buy", name, ranked),
        "offline": m.host_down(), "excluded": mk.is_excluded(s),
    }


# ---- account / orders --------------------------------------------------------------------------

def auth() -> wfm_auth.WfmAuth:
    global _auth
    a = _auth
    if a is not None:
        return a
    with _auth_lock:  # RPC threads and background refetches: exactly one session object
        if _auth is None:
            try:
                app = get_app()
                _auth = wfm_auth.WfmAuth(app.settings_dir,
                                         platform=lambda: str(app.settings.get("wfm_platform", "pc")))
            except RuntimeError:
                _auth = wfm_auth.WfmAuth(None)
        return _auth


def set_auth(a: wfm_auth.WfmAuth | None) -> None:
    global _auth
    with _auth_lock:
        _auth = a
    reset_orders_cache()


def account_state() -> dict:
    st = auth().state()
    try:
        st["holdMin"] = _rank(get_app().settings.get("market.status_hold_min", 0)) or 0
    except RuntimeError:
        st["holdMin"] = 0
    st["holdChoices"] = list(wfm_auth.HOLD_MINUTES)
    return st


def reset_orders_cache() -> None:
    with _orders_lock:
        _orders_cache.update(at=0.0, tried=0.0, orders=None, error=None)


def _item_by_id_factory(m: mk.Market):
    by_id: dict[str, Any] | None = None

    def get(item_id: str) -> Any:
        nonlocal by_id
        if by_id is None:
            by_id = {c.id: c for c in m.catalog() if c.id}
        return by_id.get(item_id)

    return get


def orders_payload(orders: list[dict] | None, at: float, error: str | None) -> dict:
    rows = orders or []
    for o in rows:
        if "owned" not in o:
            o["owned"] = _owned(o.get("gameRef"))
    return {"orders": rows, "counts": order_counts(rows), "fetchedAt": int(at * 1000) if at else None,
            "error": error, "signedIn": auth().signed_in}


def my_orders(force: bool = False) -> dict:
    a = auth()
    if not a.signed_in:
        return orders_payload([], 0, None)
    with _orders_lock:
        fresh = _orders_cache["orders"] is not None and time.time() - _orders_cache["at"] < ORDERS_STALE_S
        if fresh and not force:
            return orders_payload(_orders_cache["orders"], _orders_cache["at"], _orders_cache["error"])
    with _orders_lock:
        _orders_cache["tried"] = time.time()
    try:
        rows = a.my_orders(_item_by_id_factory(mk.get()))
    except wfm_auth.WfmUnauthorized as e:
        reset_orders_cache()
        rpc.emit("market.account", account_state())
        return orders_payload([], 0, str(e))
    except wfm_auth.WfmError as e:
        # keep the last good orders; "tried" stays set so market.qam backs off ORDERS_STALE_S
        with _orders_lock:
            _orders_cache["error"] = str(e)
            return orders_payload(_orders_cache["orders"] or [], _orders_cache["at"], str(e))
    with _orders_lock:
        _orders_cache.update(at=time.time(), orders=rows, error=None)
        return orders_payload(rows, _orders_cache["at"], None)


def orders_due() -> bool:
    """Signed in and no orders fetch attempted within ORDERS_STALE_S (success or failure)."""
    if not auth().signed_in:
        return False
    with _orders_lock:
        return time.time() - _orders_cache["tried"] >= ORDERS_STALE_S


def cached_orders() -> dict:
    with _orders_lock:
        return orders_payload(_orders_cache["orders"], _orders_cache["at"], _orders_cache["error"])


def _find_order(order_id: str) -> dict | None:
    with _orders_lock:
        return next((o for o in (_orders_cache["orders"] or []) if o.get("id") == order_id), None)


def _after_write(local_update=None) -> dict:
    """Apply an optimistic change, emit it, then refetch in the background."""
    if local_update is not None:
        with _orders_lock:
            if _orders_cache["orders"] is not None:
                _orders_cache["orders"] = local_update(_orders_cache["orders"])
    payload = cached_orders()
    rpc.emit("market.orders", payload)

    def refetch():
        rpc.emit("market.orders", my_orders(force=True))

    try:
        get_app().submit(refetch)
    except RuntimeError:
        pass
    return payload


def _err(e: Exception) -> dict:
    if isinstance(e, wfm_auth.WfmUnauthorized):
        reset_orders_cache()
        try:
            rpc.emit("market.account", account_state())
        except Exception:  # noqa: BLE001
            pass
    return {"ok": False, "error": str(e), "code": getattr(e, "code", "WFM_ERROR")}


def qam_view(limit: int) -> dict:
    m = mk.get()
    orders = cached_orders()
    return {"recent": recent_rows(_recent or RecentStore(None), m, time.time(), limit),
            "account": account_state(), "counts": orders["counts"] if orders["fetchedAt"] else None,
            "ordersError": orders["error"], "offline": m.host_down()}


# ---- RPC -------------------------------------------------------------------------------------

def setup(app: App) -> None:
    global _recent
    _recent = RecentStore(app.runtime_dir)
    app.services["wfm_auth"] = auth


def _rank(rank: Any) -> int | None:
    if rank is None or rank == "" or isinstance(rank, bool):
        return None
    try:
        r = int(rank)
    except (TypeError, ValueError):
        return None
    return r if r >= 0 else None


@rpc.method("market.detail")
async def _rpc_detail(slug: str, rank: int | None = None, record: bool = True, force: bool = False) -> dict:
    def run() -> dict:
        d = detail(mk.get(), slug, _rank(rank), force=bool(force))
        if record and not d.get("error") and _recent is not None:
            _recent.add(d["slug"], d["name"], d["rank"])
        return d

    return await get_app().run_blocking(run)


@rpc.method("market.recent")
async def _rpc_recent() -> list[dict]:
    return await get_app().run_blocking(lambda: recent_rows(_recent or RecentStore(None), mk.get(), time.time()))


@rpc.method("market.recent_remove")
def _rpc_recent_remove(slug: str) -> bool:
    if _recent:
        _recent.remove(slug)
    return True


@rpc.method("market.recent_clear")
def _rpc_recent_clear() -> bool:
    if _recent:
        _recent.clear()
    return True


@rpc.method("market.qam")
async def _rpc_qam() -> dict:
    app = get_app()
    limit = app.settings.get("market.qam_recent", QAM_RECENT_DEFAULT)
    try:
        limit = max(1, min(10, int(limit)))
    except (TypeError, ValueError):
        limit = QAM_RECENT_DEFAULT

    def run() -> dict:
        # only when nothing was tried lately: a failed fetch emits market.orders too, and the
        # QAM reloads on that event, so fetching on "never succeeded" alone would loop
        if not cached_orders()["fetchedAt"] and orders_due():
            with _orders_lock:
                _orders_cache["tried"] = time.time()  # claim the slot before the worker runs
            app.submit(lambda: rpc.emit("market.orders", my_orders(force=True)))
        return qam_view(limit)

    return await app.run_blocking(run)


@rpc.method("market.account")
async def _rpc_account(refresh: bool = False) -> dict:
    def run() -> dict:
        a = auth()
        if refresh and a.signed_in:
            try:
                a.refresh_me()
            except wfm_auth.WfmError as e:
                return {**account_state(), "error": str(e)}
        return account_state()

    return await get_app().run_blocking(run)


@rpc.method("market.sign_in")
async def _rpc_sign_in(email: str, password: str) -> dict:
    def run() -> dict:
        try:
            auth().sign_in(email, password)
            st = account_state()
        except wfm_auth.WfmError as e:
            return {**account_state(), "error": str(e)}
        reset_orders_cache()
        rpc.emit("market.account", st)
        return st

    return await get_app().run_blocking(run)


@rpc.method("market.sign_out")
async def _rpc_sign_out() -> dict:
    auth().sign_out()
    st = account_state()
    reset_orders_cache()
    rpc.emit("market.account", st)
    rpc.emit("market.orders", cached_orders())
    return st


@rpc.method("market.set_status")
async def _rpc_set_status(status: str, hold_min: int | None = None) -> dict:
    """hold_min None uses (and a value updates) setting market.status_hold_min."""
    app = get_app()
    if hold_min is None:
        hold = _rank(app.settings.get("market.status_hold_min", 0)) or 0
    else:
        hold = _rank(hold_min) or 0
        app.settings.update({"market": {"status_hold_min": hold}})

    def run() -> dict:
        try:
            auth().set_status(status, hold)
            st = account_state()
        except wfm_auth.WfmError as e:
            _err(e)
            return {**account_state(), "error": str(e)}
        rpc.emit("market.account", st)
        return st

    return await get_app().run_blocking(run)


@rpc.method("market.set_hold")
def _rpc_set_hold(hold_min: int) -> dict:
    """Store the hold minutes without a status change (status unknown, e.g. profile lookup failed)."""
    get_app().settings.update({"market": {"status_hold_min": _rank(hold_min) or 0}})
    st = account_state()
    rpc.emit("market.account", st)
    return st


@rpc.method("market.my_orders")
async def _rpc_my_orders(force: bool = False) -> dict:
    return await get_app().run_blocking(my_orders, bool(force))


@rpc.method("market.order_close")
async def _rpc_order_close(id: str, quantity: int | None = None) -> dict:  # noqa: A002 - RPC param name
    def run() -> dict:
        order = _find_order(id) or {"quantity": quantity or 1, "perTrade": 1}
        qty = close_quantity(order, quantity)
        try:
            auth().close_order(id, qty)
        except wfm_auth.WfmError as e:
            return _err(e)
        return {"ok": True, "closed": qty, **_after_write(lambda rows: apply_close(rows, id, qty))}

    return await get_app().run_blocking(run)


@rpc.method("market.order_delete")
async def _rpc_order_delete(id: str) -> dict:  # noqa: A002
    def run() -> dict:
        try:
            auth().delete_order(id)
        except wfm_auth.WfmError as e:
            return _err(e)
        return {"ok": True, **_after_write(lambda rows: [o for o in rows if o.get("id") != id])}

    return await get_app().run_blocking(run)


def _patch_local(order_id: str, **fields):
    return lambda rows: [({**o, **fields} if o.get("id") == order_id else o) for o in rows]


@rpc.method("market.order_visible")
async def _rpc_order_visible(id: str, visible: bool) -> dict:  # noqa: A002
    def run() -> dict:
        try:
            auth().update_order(id, visible=bool(visible))
        except wfm_auth.WfmError as e:
            return _err(e)
        return {"ok": True, **_after_write(_patch_local(id, visible=bool(visible)))}

    return await get_app().run_blocking(run)


@rpc.method("market.orders_visible")
async def _rpc_orders_visible(visible: bool, type: str | None = None) -> dict:  # noqa: A002
    """Bulk show/hide (sequential PATCH per order, wfmOrders.ts setOrdersVisible)."""
    def run() -> dict:
        rows = my_orders()["orders"]
        targets = [o for o in rows if o.get("visible") != bool(visible) and (type is None or o.get("type") == type)]
        done, failed = [], []
        for i, o in enumerate(targets):
            try:
                auth().update_order(o["id"], visible=bool(visible))
                done.append(o["id"])
            except wfm_auth.WfmUnauthorized as e:
                return _err(e)
            except wfm_auth.WfmError as e:
                failed.append({"id": o["id"], "name": o.get("name"), "error": str(e)})
                if e.code == "WFM_RATE_LIMITED":  # stop sending; skip the rest
                    failed += [{"id": r["id"], "name": r.get("name"), "error": "Skipped (rate limited)."}
                               for r in targets[i + 1:]]
                    break
        ids = set(done)
        payload = _after_write(lambda rs: [({**o, "visible": bool(visible)} if o.get("id") in ids else o) for o in rs])
        return {"ok": not failed, "changed": len(done), "failed": failed, **payload}

    return await get_app().run_blocking(run)


@rpc.method("market.order_update")
async def _rpc_order_update(id: str, platinum: int | None = None, quantity: int | None = None) -> dict:  # noqa: A002
    def run() -> dict:
        try:
            auth().update_order(id, platinum=_rank(platinum), quantity=_rank(quantity))
        except wfm_auth.WfmError as e:
            return _err(e)
        local = {k: v for k, v in (("platinum", _rank(platinum)), ("quantity", _rank(quantity))) if v}
        return {"ok": True, **_after_write(_patch_local(id, **local))}

    return await get_app().run_blocking(run)
