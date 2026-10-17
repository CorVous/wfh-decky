"""warframe.market prices, orders and catalog (public, anonymous v2 API).

Which price, and why
--------------------
WFHelper's overlay price is the v1 ``/items/{slug}/statistics`` 48 h
closed-volume average. v1 is deprecated and v2 has no statistics endpoint, so
this port asks v2 first:

1. ``GET /v2/orders/item/{slug}/top`` - the five cheapest sell and five highest
   buy orders from players who are online or in game. One ~5 KB request gives
   what the overlay and the market page both need: ``low_sell`` (what you can
   buy for now), ``median`` (median of those sell orders, which damps a single
   troll listing) and ``high_buy``. It reflects the live market rather than
   yesterday's trades, which is what a player choosing a relic reward acts on.
2. Fallback ``GET /v1/items/{slug}/statistics`` when /top has no sell orders or
   fails with anything but 404: WFHelper's own algorithm
   (``closed-volume-average-48h-v1``, config/shared/wfmStats.ts), which also
   yields ``volume``. Prices from it carry ``source == "stats"``.

Which orders count: with ``rank=None`` only unranked or rank-0 orders are
priced (WFHelper's pooled-rank rule, the same one the statistics fallback
uses), and ``/top`` is asked for ``?rank=0`` when the catalog says the item is
rankable. ``*_relic`` slugs price the intact refinement: a missing subtype
counts as intact (data-sources.md §1.4.2), radiant listings are dropped.

Pacing: every request goes through net.fetch, whose per-host throttle keeps
api.warframe.market at <= 3 req/s (0.35 s gap) and backs off on 429.
Transport errors are not retried per request; instead the first timeout,
refused connection or 5xx marks the host down for ERROR_COOLDOWN and every
request in that window answers from cache at once (WFHelper's scheduler-wide
degraded state, data-sources.md §1.3). That keeps a 100-slug relic-planner
lookup from costing minutes while the Deck is offline.
Caching: prices are fresh for 30 min in memory and on disk
(``<runtime>/market/prices.json``) so a restart does not refetch; the item
catalog (``/v2/items``, a few MB) is cached 24 h by net.get_json. An expired
price is still served when a refetch fails.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import statistics
import tempfile
import threading
import time
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import quote, urlencode

from .. import log, net, rpc
from . import names

_log = log.get("market")

API = "https://api.warframe.market"
SITE = "https://warframe.market"
ASSET_BASE = "https://warframe.market/static/assets/"
ICON_MIRROR = "https://assets.wfhelper.com"
PRICE_TTL = 30 * 60
CATALOG_TTL = 24 * 3600
ERROR_COOLDOWN = 60          # WFM_BACKEND_ERROR_COOLDOWN_MS
CATALOG_RETRY_BLOCK = 15     # wfmCatalog.ts: retry blocked 15 s after a failure
STATS_WINDOW = 48 * 3600
PRICE_OUTLIER_FACTOR = 10
EXCLUDED_SLUGS = {"vendor_relic"}
REQUEST_TIMEOUT = 10
SAVE_DEBOUNCE = 5
PRICES_RPC_DEADLINE = 20     # market.prices answers within ~this; the rest arrive as events
_now = time.time  # patched by tests to replay recorded statistics


@dataclass
class Price:
    median: float | None
    low_sell: int | None
    volume: int | None
    fetched_at: float
    slug: str = ""
    rank: int | None = None
    high_buy: int | None = None
    source: str = "top"   # top | stats
    sellers: int = 0      # online sell orders seen (top only)

    @property
    def plat(self) -> int | None:
        """The one number overlays show: rounded median, else lowest sell."""
        v = self.median if self.median else self.low_sell
        return int(round(v)) if v else None

    def to_json(self) -> dict:
        return {"slug": self.slug, "rank": self.rank, "median": self.median, "low_sell": self.low_sell,
                "high_buy": self.high_buy, "volume": self.volume, "fetched_at": self.fetched_at,
                "source": self.source, "sellers": self.sellers, "plat": self.plat}

    @classmethod
    def from_json(cls, d: dict) -> "Price":
        return cls(d.get("median"), d.get("low_sell"), d.get("volume"), float(d.get("fetched_at") or 0),
                   d.get("slug") or "", d.get("rank"), d.get("high_buy"), d.get("source") or "top",
                   int(d.get("sellers") or 0))


class MarketUnavailable(RuntimeError):
    """api.warframe.market is marked down; no request was made."""


@dataclass
class CatalogItem:
    slug: str
    name: str
    game_ref: str = ""
    id: str = ""
    thumb: str | None = None
    icon: str | None = None
    max_rank: int | None = None
    tags: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"slug": self.slug, "name": self.name, "game_ref": self.game_ref, "thumb": self.thumb,
                "icon": self.icon, "max_rank": self.max_rank, "tags": self.tags,
                "url": f"{SITE}/items/{self.slug}"}


# ---- payload helpers -----------------------------------------------------

def unwrap(obj: Any) -> Any:
    """v2 wraps in {"data": ...}, v1 in {"payload": ...}."""
    if isinstance(obj, dict):
        if "data" in obj:
            return obj["data"]
        if "payload" in obj:
            return obj["payload"]
    return obj


def is_excluded(slug: str) -> bool:
    return slug in EXCLUDED_SLUGS or slug.endswith("_scene")


def format_asset_url(path: Any) -> str | None:
    """formatWfmAssetUrl: thumbs via the WFHelper mirror (WFM's CDN is behind a challenge)."""
    if not isinstance(path, str) or not path.strip():
        return None
    p = path.strip()
    absolute = p if p.lower().startswith(("http://", "https://")) else ASSET_BASE + p
    if not absolute.startswith(ASSET_BASE):
        return absolute
    asset = absolute[len(ASSET_BASE):].split("?", 1)[0]
    if not ("/" + asset).count("/thumbs/"):
        return absolute
    segs = asset.split("/")
    fname = ".".join(part for part in segs.pop().split(".")
                     if not (len(part) == 32 and all(c in "0123456789abcdefABCDEF" for c in part)))
    return f"{ICON_MIRROR}/wfm/{'/'.join(segs + [fname])}"


def _norm_rank(v: Any) -> int | None:
    if v is None or (isinstance(v, str) and not v.strip()) or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f < 0 or f == float("inf"):
        return None
    return int(f)


def normalize_catalog_item(raw: dict) -> CatalogItem | None:
    """wfmCatalog.ts:43-74."""
    slug = raw.get("slug") or raw.get("url_name") or raw.get("_slug")
    if not isinstance(slug, str) or not slug:
        return None
    en = ((raw.get("i18n") or {}).get("en") or {}) if isinstance(raw.get("i18n"), dict) else {}
    name = (en.get("name") or en.get("itemName") or en.get("item_name") or raw.get("item_name")
            or raw.get("itemName") or raw.get("name") or slug.replace("_", " ").title())
    mr = _norm_rank(raw.get("maxRank", raw.get("max_rank")))
    return CatalogItem(
        slug=slug, name=str(name), game_ref=str(raw.get("gameRef") or ""), id=str(raw.get("id") or ""),
        thumb=format_asset_url(en.get("thumb") or raw.get("thumb")),
        icon=format_asset_url(en.get("icon") or raw.get("icon")),
        max_rank=mr if mr else None, tags=list(raw.get("tags") or []),
    )


def normalize_orders(payload: Any, side: str, rank: int | None = None, limit: int = 500,
                     subtype: str | None = None) -> list[dict]:
    """wfmOrders.ts normalizeWfmOrderBookSide for v1 or v2 order rows.

    With a subtype filter (relics) an order without a subtype counts as intact.
    """
    rows: Any = payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get(side), list):
            rows = data[side]  # /top: {"data": {"sell": [...], "buy": [...]}}
        elif isinstance(data, list):
            rows = data
        elif isinstance((payload.get("payload") or {}).get("orders"), list):
            rows = payload["payload"]["orders"]
        elif isinstance(data, dict) and isinstance(data.get("orders"), list):
            rows = data["orders"]
        elif isinstance(payload.get("orders"), list):
            rows = payload["orders"]
        elif isinstance(payload.get(side), list):
            rows = payload[side]
    if not isinstance(rows, list):
        return []
    out = []
    for o in rows:
        if not isinstance(o, dict):
            continue
        typ = o.get("type") or o.get("order_type")
        if typ != side or o.get("visible") is False:
            continue
        r = o.get("rank", o.get("mod_rank"))
        r = int(r) if isinstance(r, (int, float)) and not isinstance(r, bool) and r >= 0 else None
        if rank is not None and r != rank:
            continue
        sub = str(o.get("subtype") or "").lower()
        if subtype is not None:
            if (sub or "intact") != subtype:
                continue
        elif rank is None and sub == "atragraph":
            continue
        user = o.get("user") or {}
        uname = user.get("ingameName") or user.get("ingame_name") or ""
        if not uname:
            continue
        try:
            plat = float(o.get("platinum"))
        except (TypeError, ValueError):
            continue
        if not plat > 0:
            continue
        try:
            qty = int(o.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        qty = qty if qty > 0 else 1
        try:
            per = int(o.get("perTrade", o.get("per_trade")) or 1)
        except (TypeError, ValueError):
            per = 1
        per = min(max(per, 1), qty)
        plat_i = int(round(plat))
        out.append({
            "user": uname, "status": str(user.get("status") or "").lower(), "platinum": plat_i,
            "quantity": qty, "per_trade": per, "unit": round(plat_i / per, 2), "rank": r,
            "platform": user.get("platform") or o.get("platform"), "subtype": o.get("subtype"),
            "reputation": user.get("reputation"),
        })
    sign = 1 if side == "sell" else -1
    out.sort(key=lambda e: (sign * e["unit"], -e["quantity"], e["user"].lower()))
    return out[:limit]


def _parse_time(v: Any) -> float | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def unranked(orders: list[dict]) -> list[dict]:
    """Price pool for rank=None: unranked or rank-0 orders only (WFHelper _pooled_rank)."""
    return [o for o in orders if o.get("rank") in (None, 0)]


def _pooled_rank(row: dict, rank: int | None) -> tuple[bool, int | None]:
    raw = row.get("mod_rank", row.get("rank"))
    rr = _norm_rank(raw)
    if raw is not None and rr is None:
        return False, None
    if rank is not None:
        return rr == rank, rr
    return rr is None or rr == 0, rr


def _pooled_medians(rows: Any, rank: int | None, keep) -> list[float]:
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not keep(row) or not _pooled_rank(row, rank)[0]:
            continue
        try:
            m = float(row.get("median"))
        except (TypeError, ValueError):
            continue
        if m > 0:
            out.append(m)
    return out


def stats_average(payload: Any, rank: int | None = None, now: float | None = None) -> tuple[int, float, int] | None:
    """extractAverageFromStatsPayload -> (average, newest row time, volume) or None."""
    p = (payload or {}).get("payload") if isinstance(payload, dict) else None
    if not isinstance(p, dict):
        return None
    closed = p.get("statistics_closed") or {}
    rows = closed.get("48hours", closed.get("48_hours"))
    if not isinstance(rows, list):
        return None
    now = _now() if now is None else now
    start = now - STATS_WINDOW
    samples: dict[str, tuple[float, float, float]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("order_type") not in (None, "sell"):
            continue
        ok, rr = _pooled_rank(row, rank)
        if not ok:
            continue
        t = _parse_time(row.get("datetime"))
        try:
            price, vol = float(row.get("wa_price")), float(row.get("volume"))
        except (TypeError, ValueError):
            continue
        if t is None or t < start or t > now or price <= 0 or vol <= 0:
            continue
        samples[f"{t}:{rr}"] = (price, vol, t)
    if not samples:
        return None
    live = p.get("statistics_live") or {}
    book = _pooled_medians(live.get("48hours", live.get("48_hours")), rank, lambda r: r.get("order_type") == "sell")
    if book:
        ref = statistics.median(book)
    else:
        hist = _pooled_medians(closed.get("90days"), rank, lambda r: r.get("order_type") in (None, "sell")
                               and (_parse_time(r.get("datetime")) or now) < start)
        ref = statistics.median(hist) if hist else None
    total = volume = 0.0
    newest = 0.0
    for price, vol, t in samples.values():
        if ref and price > ref * PRICE_OUTLIER_FACTOR:
            continue
        total += price * vol
        volume += vol
        newest = max(newest, t)
    if volume <= 0:
        return None
    avg = int(round(total / volume))
    return (avg, newest, int(volume)) if avg > 0 else None


# ---- service -------------------------------------------------------------

class Market:
    def __init__(self, cache_dir: str | None = None, settings=None):
        self.cache_dir = cache_dir
        self.settings = settings
        self._lock = threading.Lock()
        self._prices: dict[str, tuple[float, Price | None]] = {}
        self._orders: dict[str, tuple[float, dict]] = {}
        self._cooldown: dict[str, float] = {}
        self._inflight: dict[str, threading.Event] = {}
        self._disk_loaded = False
        self._last_save = 0.0
        self._dirty = False
        self._catalog: list[CatalogItem] | None = None
        self._cat_slug: dict[str, CatalogItem] = {}
        self._cat_name: dict[str, CatalogItem] = {}
        self._cat_ref: dict[str, CatalogItem] = {}
        self._cat_failed_at = 0.0
        self._cat_lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._queued: set[str] = set()
        self._worker: threading.Thread | None = None
        self._host_down_until = 0.0
        self._save_lock = threading.Lock()
        self._flush_timer: threading.Timer | None = None

    # headers / keys
    def headers(self) -> dict:
        s = self.settings
        platform = str(s.get("wfm_platform", "pc") if s else "pc") or "pc"
        cross = bool(s.get("wfm_crossplay", True)) if s else True
        return {"Accept": "application/json", "Platform": platform, "Language": "en",
                "Crossplay": "true" if cross else "false"}

    def _key(self, slug: str, rank: int | None) -> str:
        h = self.headers()
        return f"{h['Platform']}|{h['Crossplay']}|{slug}|{'' if rank is None else rank}"

    def host_down(self) -> bool:
        return time.time() < self._host_down_until

    def _mark_down(self, err: Exception) -> None:
        if not self.host_down():
            _log.warning("warframe.market unreachable (%s); serving cache for %ds", err, ERROR_COOLDOWN)
        self._host_down_until = time.time() + ERROR_COOLDOWN

    @staticmethod
    def _is_transport_error(e: Exception, http: bool = True) -> bool:
        """Unreachable host (always), or with http also a 5xx / exhausted 429."""
        if isinstance(e, net.HttpError):
            return http and (e.status >= 500 or e.status == 429)
        return isinstance(e, (urllib.error.URLError, TimeoutError, OSError))

    def _get(self, url: str, trip_on_http: bool = True) -> Any:
        """One request, no transport retries; a transport error trips the host-wide circuit.

        trip_on_http=False lets one endpoint's 5xx through to its fallback (/top -> v1
        statistics) without marking the whole host down.
        """
        if self.host_down():
            raise MarketUnavailable(url)
        try:
            body = net.fetch(url, headers=self.headers(), timeout=REQUEST_TIMEOUT, retry_network=False)
        except Exception as e:
            if self._is_transport_error(e, trip_on_http):
                self._mark_down(e)
            raise
        return json.loads(body)

    # disk cache
    def _disk_path(self) -> str | None:
        return os.path.join(self.cache_dir, "prices.json") if self.cache_dir else None

    def _load_disk(self) -> None:
        if self._disk_loaded:
            return
        self._disk_loaded = True
        path = self._disk_path()
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return
        cutoff = time.time() - PRICE_TTL
        for key, v in (raw or {}).items():
            ts = float(v.get("t") or 0)
            if ts >= cutoff and key not in self._prices:
                self._prices[key] = (ts, Price.from_json(v["p"]) if v.get("p") else None)

    def _save_disk(self, force: bool = False) -> None:
        """Debounced: a prefetch of hundreds of slugs rewrites the file every 5 s, not per slug.

        A skipped write arms a timer so the last prices of a burst still reach disk.
        """
        path = self._disk_path()
        if not path:
            return
        with self._save_lock:
            now = time.time()
            if not force and now - self._last_save < SAVE_DEBOUNCE:
                self._dirty = True
                if self._flush_timer is None or not self._flush_timer.is_alive():
                    self._flush_timer = threading.Timer(SAVE_DEBOUNCE, self.flush)
                    self._flush_timer.daemon = True
                    self._flush_timer.start()
                return
            self._last_save, self._dirty = now, False
            cutoff = now - PRICE_TTL
            with self._lock:
                data = {k: {"t": ts, "p": p.to_json() if p else None}
                        for k, (ts, p) in self._prices.items() if ts >= cutoff}
            tmp = None
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".prices.", suffix=".tmp", dir=self.cache_dir)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, separators=(",", ":"))
                os.replace(tmp, path)
                tmp = None
            except OSError as e:
                _log.warning("price cache write failed: %s", e)
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    # catalog
    def catalog(self) -> list[CatalogItem]:
        """/v2/items, cached 24 h on disk; [] while unreachable (retry after 15 s)."""
        with self._cat_lock:
            if self._catalog is not None:
                return self._catalog
            if time.time() - self._cat_failed_at < CATALOG_RETRY_BLOCK:
                return []
            try:
                if self.host_down():
                    raise MarketUnavailable("host marked down")
                raw = net.get_json(f"{API}/v2/items", max_age=CATALOG_TTL,
                                   headers={"Accept": "application/json", "Language": "en"},
                                   timeout=REQUEST_TIMEOUT, retry_network=False)
                rows = unwrap(raw)
                if not isinstance(rows, list) or not rows:
                    raise ValueError("empty catalog")
            except Exception as e:  # noqa: BLE001 - offline is normal on a Deck
                if self._is_transport_error(e):
                    self._mark_down(e)
                _log.warning("WFM catalog unavailable: %s", e)
                self._cat_failed_at = time.time()
                return []
            self._set_catalog([c for c in (normalize_catalog_item(r) for r in rows if isinstance(r, dict)) if c])
            return self._catalog or []

    def catalog_if_loaded(self) -> list[CatalogItem] | None:
        """The catalog if it is already in memory, else None (never blocks or hits the network)."""
        return self._catalog

    def _set_catalog(self, cat: list[CatalogItem]) -> None:
        by_slug, by_name, by_ref = {}, {}, {}
        for c in cat:
            by_slug[c.slug] = c
            if c.name:
                by_name.setdefault(c.name.lower(), c)
            if c.game_ref:
                by_ref.setdefault(c.game_ref.lower(), c)
        for c in cat:
            alias = c.slug[:-4] if c.slug.endswith("_set") else c.slug
            by_name.setdefault(alias.replace("_", " ").lower(), c)
            low = c.name.lower()
            for suffix in (" (key)", " (veiled)"):
                if low.endswith(suffix):
                    by_name.setdefault(low[: -len(suffix)].strip(), c)
        self._cat_slug, self._cat_name, self._cat_ref = by_slug, by_name, by_ref
        self._catalog = cat
        _log.info("WFM catalog: %d items", len(cat))

    def lookup(self, key: str) -> CatalogItem | None:
        """Slug, exact name ("<name> set" and bare variants too) or DE uniqueName (gameRef)."""
        self.catalog()
        k = (key or "").strip()
        if not k:
            return None
        if k.startswith("/Lotus/"):
            return self._cat_ref.get(k.lower())
        low = k.lower()
        hit = self._cat_slug.get(low) or self._cat_name.get(low)
        if hit is None:
            hit = self._cat_name.get(low[:-4]) if low.endswith(" set") else self._cat_name.get(low + " set")
        if hit is None:
            hit = self._cat_slug.get(names.normalize_wfm_slug(k) or "")
        return hit

    def slug_for(self, key: str) -> str | None:
        """Best slug for a uniqueName or name: catalog first, then the item DB."""
        hit = self.lookup(key)
        if hit:
            return hit.slug
        try:
            from .items import items

            db = items()
            item = db.by_unique(key) if key.startswith("/Lotus/") else db.by_name(key)
            return item.wfm_slug if item else None
        except Exception:  # noqa: BLE001 - no app/data in some contexts
            return None

    def search(self, query: str, limit: int = 20) -> list[dict]:
        q = names.normalize_for_search(query)
        if len(q) < 2:
            return []
        cat = self.catalog()
        if cat:
            starts = [c for c in cat if c.name.lower().startswith(q)]
            contains = [c for c in cat if q in c.name.lower() and not c.name.lower().startswith(q)]
            return [c.to_json() for c in (starts + contains)[:limit]]
        # Offline: tradable items from the bundled DB, so search still answers.
        from .items import items

        return [{"slug": i.wfm_slug, "name": i.name, "game_ref": i.unique_name, "thumb": None, "icon": None,
                 "max_rank": None, "tags": [], "url": f"{SITE}/items/{i.wfm_slug}"}
                for i in items().search(query, limit * 2, tradable_only=True) if i.wfm_slug][:limit]

    # prices
    def cached(self, slug: str, rank: int | None = None, max_age: float = PRICE_TTL) -> tuple[bool, Price | None]:
        """(hit, price) from memory/disk without network."""
        with self._lock:
            self._load_disk()
            ent = self._prices.get(self._key(slug, rank))
        if ent and time.time() - ent[0] < max_age:
            return True, ent[1]
        return False, None

    def price(self, slug: str, rank: int | None = None, max_age: float = PRICE_TTL,
              force: bool = False) -> Price | None:
        """Cached price, else one network read; on failure the last known (expired) price.

        force skips the freshness check (orders() uses it so orders and price agree) but
        never discards the cached price: a failed refetch still answers with it.
        """
        slug = (names.normalize_wfm_slug(slug) or "") if slug else ""
        if not slug or is_excluded(slug):
            return None
        if not force:
            hit, p = self.cached(slug, rank, max_age)
            if hit:
                return p
        key = self._key(slug, rank)
        with self._lock:
            ev = self._inflight.get(key)
            owner = ev is None
            if owner:
                ev = self._inflight[key] = threading.Event()
        if not owner:
            ev.wait(45)
            hit, p = self.cached(slug, rank, max_age)
            return p if hit else self._stale(key)
        try:
            if time.time() < self._cooldown.get(key, 0) or self.host_down():
                return self._stale(key)
            return self._fetch_price(slug, rank, key)
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            ev.set()

    def _stale(self, key: str) -> Price | None:
        with self._lock:
            ent = self._prices.get(key)
        return ent[1] if ent else None

    def _store(self, key: str, p: Price | None, orders: dict | None = None) -> None:
        now = time.time()
        with self._lock:
            self._prices[key] = (now, p)
            if orders is not None:
                self._orders[key] = (now, orders)
            self._cooldown.pop(key, None)
        self._save_disk()

    def _fetch_price(self, slug: str, rank: int | None, key: str) -> Price | None:
        q_rank = rank
        if rank is None:
            cat = self._cat_slug.get(slug) if self._catalog is not None else None
            if cat is not None and cat.max_rank:
                q_rank = 0  # rankable: ask for R0 listings rather than filtering a mixed top 5
        q = "?" + urlencode({"rank": q_rank}) if q_rank is not None else ""
        subtype = "intact" if slug.endswith("_relic") else None
        top_err: Exception | None = None
        try:
            raw = self._get(f"{API}/v2/orders/item/{quote(slug)}/top{q}", trip_on_http=False)
            sells = normalize_orders(raw, "sell", rank, subtype=subtype)
            buys = normalize_orders(raw, "buy", rank, subtype=subtype)
            if rank is None:
                sells, buys = unranked(sells), unranked(buys)
            orders = {"sell": sells, "buy": buys}
            if sells:
                units = [e["unit"] for e in sells]
                p = Price(median=round(statistics.median(units), 1), low_sell=max(1, int(round(min(units)))),
                          volume=None, fetched_at=time.time(), slug=slug, rank=rank,
                          high_buy=max(1, int(round(max(e["unit"] for e in buys)))) if buys else None,
                          source="top", sellers=len(sells))
                self._store(key, p, orders)
                return p
            with self._lock:
                self._orders[key] = (time.time(), orders)
        except net.HttpError as e:
            if e.status == 404:
                self._store(key, None, {"sell": [], "buy": []})
                return None
            top_err = e
        except Exception as e:  # noqa: BLE001 - network down, bad JSON
            top_err = e
        try:
            raw = self._get(f"{API}/v1/items/{quote(slug)}/statistics")
            avg = stats_average(raw, rank)
            p = Price(median=float(avg[0]), low_sell=None, volume=avg[2], fetched_at=time.time(), slug=slug,
                      rank=rank, source="stats") if avg else None
            self._store(key, p)
            return p
        except net.HttpError as e:
            if e.status == 404:
                self._store(key, None)
                return None
            err: Exception = e
        except Exception as e:  # noqa: BLE001
            err = e
        _log.warning("price %s failed (top: %s; stats: %s)", slug, top_err, err)
        with self._lock:
            self._cooldown[key] = time.time() + ERROR_COOLDOWN
        return self._stale(key)

    def prices(self, slugs: Iterable[str], rank: int | None = None,
               deadline: float | None = None) -> dict[str, Price | None]:
        """Prices keyed by the slugs as given.

        With a deadline (seconds) slugs not reached in time answer with their last known
        price (or None) and are queued for prefetch, which emits "market.price" events.
        """
        end = None if deadline is None else time.monotonic() + deadline
        out: dict[str, Price | None] = {}
        late: list[str] = []
        for s in dict.fromkeys(s for s in slugs if s):
            ns = names.normalize_wfm_slug(s) or ""
            if ns and not is_excluded(ns):
                hit, p = self.cached(ns, rank)
                if hit:
                    out[s] = p
                    continue
                if end is not None and time.monotonic() >= end:
                    out[s] = self._stale(self._key(ns, rank))
                    late.append(ns)
                    continue
            out[s] = self.price(s, rank)
        if late:
            self.prefetch(late, rank, notify=True)
        return out

    def orders(self, slug: str, rank: int | None = None) -> dict:
        """Top online orders {"sell": [...], "buy": [...]} (fetched with the price)."""
        slug = names.normalize_wfm_slug(slug) or ""
        if not slug:
            return {"sell": [], "buy": []}
        key = self._key(slug, rank)
        with self._lock:
            ent = self._orders.get(key)
        if not ent or time.time() - ent[0] >= PRICE_TTL:
            self.price(slug, rank, force=True)  # orders and price from the same read
            with self._lock:
                ent = self._orders.get(key)
        return ent[1] if ent else {"sell": [], "buy": []}

    # background
    def prefetch(self, slugs: Iterable[str], rank: int | None = None, notify: bool = False) -> int:
        """Queue slugs that are not fresh; one daemon thread drains them at the throttle rate.

        notify emits a "market.price" event {slug, rank, price} as each one lands.
        """
        n = 0
        for s in dict.fromkeys(names.normalize_wfm_slug(x) or "" for x in slugs if x):
            if not s or is_excluded(s) or self.cached(s, rank)[0]:
                continue
            k = self._key(s, rank)
            with self._lock:
                if k in self._queued:
                    continue
                self._queued.add(k)
            self._queue.put((s, rank, k, notify))
            n += 1
        if n:
            self._ensure_worker()
        return n

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return
            self._worker = threading.Thread(target=self._drain, name="wfh-wfm-prefetch", daemon=True)
            self._worker.start()

    def _drain(self) -> None:
        while True:
            try:
                slug, rank, key, notify = self._queue.get(timeout=0.5 if self._dirty else 30)
            except queue.Empty:
                if self._dirty:
                    self._save_disk(force=True)
                    continue
                return
            try:
                p = self.price(slug, rank)
                if notify:
                    rpc.emit("market.price", {"slug": slug, "rank": rank, "price": p.to_json() if p else None})
            except Exception:  # noqa: BLE001
                _log.exception("prefetch %s failed", slug)
            finally:
                with self._lock:
                    self._queued.discard(key)
                self._queue.task_done()

    def flush(self) -> None:
        """Write pending prices now (debounce timer, prefetch idle, worker exit)."""
        if self._dirty:
            self._save_disk(force=True)

    def wait_idle(self, timeout: float = 10) -> bool:
        """Tests: block until the prefetch queue is drained."""
        end = time.time() + timeout
        while time.time() < end:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False


# ---- module API (the contract other modules use) -------------------------

_market: Market | None = None
_mlock = threading.Lock()


def get() -> Market:
    global _market
    with _mlock:
        if _market is None:
            try:
                from ..app import get_app

                app = get_app()
                _market = Market(os.path.join(app.runtime_dir, "market"), app.settings)
            except RuntimeError:
                _market = Market(None, None)
        return _market


def set_market(m: Market | None) -> None:
    global _market
    with _mlock:
        _market = m


def price(slug: str, rank: int | None = None) -> Price | None:
    return get().price(slug, rank)


def prices(slugs: Iterable[str], rank: int | None = None) -> dict[str, Price | None]:
    return get().prices(slugs, rank)


def prefetch(slugs: Iterable[str], rank: int | None = None) -> int:
    return get().prefetch(slugs, rank)


def search(query: str, limit: int = 20) -> list[dict]:
    return get().search(query, limit)


def orders(slug: str, rank: int | None = None) -> dict:
    return get().orders(slug, rank)


def slug_for(key: str) -> str | None:
    return get().slug_for(key)


def setup(app) -> None:
    m = Market(os.path.join(app.runtime_dir, "market"), app.settings)
    set_market(m)
    app.services["market"] = m
    atexit.register(m.flush)  # the worker exits when main.py closes stdin


@rpc.method("market.search")
async def _rpc_search(query: str, limit: int = 20) -> list[dict]:
    from ..app import get_app

    return await get_app().run_blocking(search, query, limit)


@rpc.method("market.item")
async def _rpc_item(slug: str, rank: int | None = None) -> dict:
    from ..app import get_app

    def work() -> dict:
        m = get()
        o = m.orders(slug, rank)  # first: a forced orders read also refreshes the price
        p = m.price(slug, rank)
        cat = m.lookup(slug)
        return {"slug": slug, "name": cat.name if cat else slug.replace("_", " ").title(),
                "item": cat.to_json() if cat else None, "price": p.to_json() if p else None,
                "orders": {"sell": o["sell"][:5], "buy": o["buy"][:5]}, "url": f"{SITE}/items/{slug}"}

    return await get_app().run_blocking(work)


@rpc.method("market.prices")
async def _rpc_prices(slugs: list[str], rank: int | None = None) -> dict:
    from ..app import get_app

    out = await get_app().run_blocking(get().prices, slugs, rank, PRICES_RPC_DEADLINE)
    return {s: (p.to_json() if p else None) for s, p in out.items()}
