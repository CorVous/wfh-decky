"""warframe.market similar riven listings (docs/port/riven-scanner.md §13).

GET /v1/auctions/search?type=riven&weapon_url_name=<slug>&sort_by=price_asc (and
price_desc when the first returned anything), deduped by id, cached in memory
10 min (50 entries). similar_pool keeps the first 30 plus the cheapest direct
sales; similarity() scores a listing's stats against the scanned card.

warframe.market sits behind Cloudflare: a challenge or any failure is a soft
failure that leaves the panel without listings (no solver window on the Deck).
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable
from urllib.parse import urlencode

from .. import log

_log = log.get("riven.market")

API = "https://api.warframe.market/v1"
AUCTION_URL = "https://warframe.market/auction/"
CACHE_TTL = 600.0
CACHE_MAX = 50
SIMILAR_LISTING_COUNT = 30


def unwrap(obj: Any) -> Any:
    if isinstance(obj, dict):
        if "data" in obj and obj["data"] is not None:
            return obj["data"]
        if "payload" in obj:
            return obj["payload"]
    return obj


def _title_words(url_name: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in str(url_name or "").replace("_", " ").split(" ") if w)


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def parse_listing(a: dict) -> dict | None:
    if not isinstance(a, dict) or not a.get("id"):
        return None
    item = a.get("item") or {}
    owner = a.get("owner") or {}
    stats = []
    for at in item.get("attributes") or []:
        if not isinstance(at, dict):
            continue
        stats.append({"name": _title_words(at.get("url_name", "")), "value": _num(at.get("value")) or 0.0,
                      "positive": bool(at.get("positive"))})
    buyout = _num(a.get("buyout_price"))
    starting = _num(a.get("starting_price"))
    plat = buyout if buyout is not None else (starting if starting is not None else 0)
    status = owner.get("status")
    return {
        "id": str(a["id"]),
        "seller": owner.get("ingame_name") or "",
        "sellerStatus": str(status).lower() if status else None,
        "platinum": int(plat),
        "stats": stats,
        "rerolls": int(_num(item.get("re_rolls")) or 0),
        "startingPrice": None if starting is None else int(starting),
        "buyoutPrice": None if buyout is None else int(buyout),
        "isDirectSell": bool(a.get("is_direct_sell")),
        "name": item.get("name") or "",
    }


def parse_auctions(payload: Any) -> list[dict]:
    body = unwrap(payload)
    rows = body.get("auctions") if isinstance(body, dict) else body
    out = []
    for a in rows or []:
        p = parse_listing(a)
        if p:
            out.append(p)
    return out


def similar_pool(listings: list[dict], size: int = SIMILAR_LISTING_COUNT) -> list[dict]:
    """similarListingPool: the first `size`, plus the cheapest direct sales until there are `size` of them."""
    head = listings[:size]
    direct = sum(1 for l in head if l.get("isDirectSell"))
    rest = [l for l in listings[size:] if l.get("isDirectSell")]
    rest.sort(key=lambda l: l["buyoutPrice"] if l.get("buyoutPrice") is not None else l.get("platinum", 0))
    return head + rest[:max(0, size - direct)]


def similarity(my_names: list[str], listing_stats: list[dict]) -> tuple[int, set[str]]:
    """computeRivenStatSimilarity: (percent, lowercased listing stat names that matched)."""
    mine = [str(n).lower() for n in my_names if n]
    names = [str(s.get("name") or "").strip().lower() for s in listing_stats or []]
    names = [n for n in names if n]
    if not mine or not names:
        return 0, set()
    matched: set[str] = set()
    for m in mine:
        hit = next((n for n in names if n == m or m in n or n in m), None)
        if hit:
            matched.add(hit)
    union = len(mine) + len(names) - len(matched)
    return (math.floor(len(matched) / union * 100 + 0.5) if union > 0 else 0), matched


def rank_similar(stats: list[dict], listings: list[dict], auctions_shown: bool = True,
                 limit: int = SIMILAR_LISTING_COUNT) -> list[dict]:
    """Display list: optional direct-sale filter, first `limit`, sorted by similarity (stable)."""
    names = [s["name"] for s in stats or []]
    rows = [l for l in listings if auctions_shown or l.get("isDirectSell") is not False][:limit]
    out = []
    for l in rows:
        pct, matched = similarity(names, l.get("stats") or [])
        out.append({**l, "match": pct,
                    "stats": [{**st, "matched": str(st.get("name", "")).strip().lower() in matched}
                              for st in l.get("stats") or []],
                    "price": l.get("buyoutPrice") or l.get("startingPrice") or l.get("platinum") or 0})
    out.sort(key=lambda r: -r["match"])
    return out


def match_color(pct: int) -> str:
    return "good" if pct >= 75 else "warn" if pct >= 40 else "bad"


def format_listing_value(name: str, value: float) -> str:
    if re.match(r"^Damage Vs ", name):
        return f"x{value:.2f}"
    sign = "−" if value < 0 else "+"
    return f"{sign}{abs(math.floor(value + 0.5))}%"


def auction_url(auction_id: str) -> str:
    return AUCTION_URL + re.sub(r"[^a-zA-Z0-9]", "", str(auction_id or ""))


class SimilarSearch:
    """In-memory cached auction search. fetch(url) -> parsed JSON; raises on failure."""

    def __init__(self, fetch: Callable[[str], Any] | None = None, clock: Callable[[], float] = time.monotonic):
        self._fetch = fetch
        self.clock = clock
        self._cache: OrderedDict[str, tuple[float, list[dict]]] = OrderedDict()
        self._lock = threading.Lock()
        self.last_error: str | None = None

    def _default_fetch(self, url: str) -> Any:
        from .. import net
        from ..data import market

        m = market.get()
        if m.host_down():
            raise RuntimeError("warframe.market is marked down")
        body = net.fetch(url, headers={**m.headers(), "Content-Type": "application/json"}, timeout=15,
                         retry_network=False)
        text = body.decode("utf-8", errors="replace")
        if "just a moment" in text[:2000].lower():
            raise RuntimeError("cloudflare challenge")
        return json.loads(text)

    @staticmethod
    def url(slug: str, sort: str, positive: list[str] | None = None, negative: list[str] | None = None) -> str:
        q = {"type": "riven", "weapon_url_name": slug, "sort_by": sort}
        if positive:
            q["positive_stats"] = ",".join(positive)
        if negative:
            q["negative_stats"] = ",".join(negative)
        return f"{API}/auctions/search?{urlencode(q)}"

    @staticmethod
    def key(slug: str, positive: list[str] | None = None, negative: list[str] | None = None) -> str:
        return f"{slug},quick,{','.join(sorted(positive or []))},|,{','.join(sorted(negative or []))}"

    def cached(self, slug: str) -> list[dict] | None:
        with self._lock:
            hit = self._cache.get(self.key(slug))
            if hit and self.clock() - hit[0] < CACHE_TTL:
                return hit[1]
        return None

    def search(self, slug: str, positive: list[str] | None = None, negative: list[str] | None = None) -> list[dict]:
        """Listings for a weapon family; [] on any failure (soft)."""
        if not slug:
            return []
        key = self.key(slug, positive, negative)
        with self._lock:
            hit = self._cache.get(key)
            if hit and self.clock() - hit[0] < CACHE_TTL:
                return hit[1]
        fetch = self._fetch or self._default_fetch
        seen: dict[str, dict] = {}
        try:
            asc = parse_auctions(fetch(self.url(slug, "price_asc", positive, negative)))
            for l in asc:
                seen.setdefault(l["id"], l)
            if asc:
                for l in parse_auctions(fetch(self.url(slug, "price_desc", positive, negative))):
                    seen.setdefault(l["id"], l)
            self.last_error = None
        except Exception as e:  # noqa: BLE001 - Cloudflare / offline / 4xx: no listings
            self.last_error = str(e)
            _log.info("similar rivens for %s unavailable: %s", slug, e)
            if not seen:
                return []
        rows = list(seen.values())
        with self._lock:
            self._cache[key] = (self.clock(), rows)
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_MAX:
                self._cache.popitem(last=False)
        return rows


_search: SimilarSearch | None = None


def search() -> SimilarSearch:
    global _search
    if _search is None:
        _search = SimilarSearch()
    return _search


def set_search(s: SimilarSearch | None) -> None:
    global _search
    _search = s
