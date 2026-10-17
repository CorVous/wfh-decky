"""warframe.market client against recorded payloads (no network)."""

import asyncio
import io
import json
import os
import threading
import time
import urllib.error
from datetime import datetime, timezone

import pytest

from wfh import net, rpc
from wfh.data import market

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WFM = os.path.join(ROOT, "tests", "fixtures", "data", "wfm")
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc).timestamp()


def fixture(name):
    with open(os.path.join(WFM, name), "rb") as f:
        return f.read()


ROUTES = {
    "/v2/items": "v2_items.json",
    "/v2/orders/item/ash_prime_chassis_blueprint/top": "top_ash_prime_chassis_blueprint.json",
    "/v2/orders/item/braton_prime_barrel/top": "top_braton_prime_barrel.json",
    "/v1/items/braton_prime_barrel/statistics": "stats_braton_prime_barrel.json",
    "/v2/orders/item/serration/top?rank=10": "top_serration_rank10.json",
    "/v2/orders/item/serration/top": "top_serration_mixed.json",
    "/v2/orders/item/serration/top?rank=0": "top_serration_mixed.json",
    "/v2/orders/item/lith_a1_relic/top": "top_lith_a1_relic.json",
}


class FakeWfm:
    """Stands in for net.fetch: answers recorded payloads, 404 for anything else."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.fail: set[str] = set()
        self.offline = False
        self.lock = threading.Lock()

    def __call__(self, url, headers=None, timeout=20, data=None, method=None, retry_network=True):
        path = url.replace(market.API, "")
        with self.lock:
            self.calls.append((path, dict(headers or {})))
        if self.offline:
            raise urllib.error.URLError("[Errno 101] Network is unreachable")
        if path in self.fail:
            raise net.HttpError(503, url)
        name = ROUTES.get(path)
        if name is None:
            raise net.HttpError(404, url, '{"error":"not found"}')
        return fixture(name)

    def paths(self):
        return [p for p, _ in self.calls]


class FakeSettings:
    def __init__(self, **kw):
        self.data = {"wfm_platform": "pc", "wfm_crossplay": True, **kw}

    def get(self, key, default=None):
        return self.data.get(key, default)


@pytest.fixture
def wfm(monkeypatch, tmp_path):
    fake = FakeWfm()
    monkeypatch.setattr(net, "fetch", fake)
    monkeypatch.setattr(net, "cache_dir", str(tmp_path / "netcache"))
    monkeypatch.setattr(market, "_now", lambda: NOW)
    return fake


@pytest.fixture
def m(wfm, tmp_path):
    return market.Market(str(tmp_path / "market"), FakeSettings())


# ---- pure helpers ----------------------------------------------------------

def test_rate_limit_is_at_most_three_per_second():
    assert net._HOST_INTERVAL["api.warframe.market"] >= 1 / 3


def test_throttle_spaces_real_requests(monkeypatch):
    """Goes through the real net.fetch with urlopen stubbed: 4 calls need >= 3 gaps."""
    stamps = []

    class Resp(io.BytesIO):
        headers: dict = {}
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        stamps.append(time.monotonic())
        return Resp(b"{}")

    monkeypatch.setattr(net.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(net, "_host_last", {})
    for _ in range(4):
        net.fetch("https://api.warframe.market/v2/items")
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert min(gaps) >= 0.33


def test_stats_average_matches_wfhelper_algorithm():
    raw = json.loads(fixture("stats_braton_prime_barrel.json"))
    # Window keeps 10.5x4 and the deduplicated 14:00 bucket (11x2, last wins); the 900p
    # fake close is > 10x the live sell median (12); the buy row and the 3-day-old row drop.
    assert market.stats_average(raw, now=NOW) == (11, datetime(2026, 10, 16, 14, tzinfo=timezone.utc).timestamp(), 6)
    assert market.stats_average(raw, rank=3, now=NOW) is None
    assert market.stats_average({"payload": {}}, now=NOW) is None
    # Without a live book the reference is the closed 90-day history before the window.
    no_live = json.loads(fixture("stats_braton_prime_barrel.json"))
    no_live["payload"]["statistics_live"]["48hours"] = []
    assert market.stats_average(no_live, now=NOW)[0] == 11


def test_normalize_orders_v1_and_v2():
    v1 = json.loads(fixture("v1_orders_mixed.json"))
    sells = market.normalize_orders(v1, "sell")
    assert [o["user"] for o in sells] == ["OldApiUser", "Offliner"]  # hidden, 0p and atragraph dropped
    assert sells[0]["unit"] == 10.0 and sells[0]["per_trade"] == 3   # 30p per 3
    buys = market.normalize_orders(v1, "buy")
    assert [o["user"] for o in buys] == ["Buyer2", "Buyer1"]          # highest bid first
    top = json.loads(fixture("top_ash_prime_chassis_blueprint.json"))
    sells = market.normalize_orders(top, "sell")
    assert [o["platinum"] for o in sells] == [12, 13, 14, 14, 16]
    assert sells[2]["user"] == "Baro_Fan"  # equal price: bigger quantity first
    assert sells[0]["status"] == "ingame" and sells[0]["platform"] == "pc"
    assert market.normalize_orders(json.loads(fixture("top_serration_rank10.json")), "sell", rank=5) == []


def test_catalog_normalisation_and_asset_mirror():
    raw = json.loads(fixture("v2_items.json"))["data"][1]
    c = market.normalize_catalog_item(raw)
    assert c.slug == "ash_prime_chassis_blueprint" and c.name == "Ash Prime Chassis Blueprint"
    assert c.game_ref == "/Lotus/Types/Recipes/WarframeRecipes/AshPrimeChassisBlueprint"
    assert c.thumb == "https://assets.wfhelper.com/wfm/items/images/en/thumbs/ash_prime_chassis_blueprint.128x128.png"
    assert c.icon.startswith("https://warframe.market/static/assets/items/images/en/ash_prime_chassis_blueprint.")
    assert market.format_asset_url("https://example.com/x.png") == "https://example.com/x.png"
    assert market.format_asset_url("") is None
    assert market.is_excluded("vendor_relic") and market.is_excluded("ash_prime_scene")
    assert market.unwrap({"payload": {"a": 1}}) == {"a": 1} and market.unwrap({"data": [1]}) == [1]


# ---- service ---------------------------------------------------------------

def test_price_from_top_orders(m, wfm):
    p = m.price("ash_prime_chassis_blueprint")
    assert p.source == "top" and p.low_sell == 12 and p.median == 14 and p.high_buy == 10
    assert p.sellers == 5 and p.volume is None and p.plat == 14
    path, headers = wfm.calls[0]
    assert path == "/v2/orders/item/ash_prime_chassis_blueprint/top"
    assert headers["Platform"] == "pc" and headers["Crossplay"] == "true" and headers["Language"] == "en"
    # Fresh for 30 minutes: no second request, orders come from the same call.
    assert m.price("ash_prime_chassis_blueprint") == p
    o = m.orders("ash_prime_chassis_blueprint")
    assert len(o["sell"]) == 5 and len(o["buy"]) == 3
    assert len(wfm.calls) == 1


def test_price_falls_back_to_statistics(m, wfm):
    p = m.price("braton_prime_barrel")
    assert p.source == "stats" and p.median == 11 and p.volume == 6 and p.low_sell is None
    assert wfm.paths() == ["/v2/orders/item/braton_prime_barrel/top", "/v1/items/braton_prime_barrel/statistics"]
    assert m.orders("braton_prime_barrel")["buy"][0]["user"] == "Lowballer"


def test_top_error_falls_back_to_statistics(m, wfm):
    wfm.fail.add("/v2/orders/item/braton_prime_barrel/top")
    assert m.price("braton_prime_barrel").source == "stats"


def test_ranked_price(m, wfm):
    p = m.price("serration", rank=10)
    assert p.rank == 10 and p.low_sell == 45 and p.median == 48
    assert wfm.paths() == ["/v2/orders/item/serration/top?rank=10"]


def test_unknown_slug_is_negative_cached(m, wfm):
    assert m.price("no_such_item") is None
    assert m.price("no_such_item") is None
    assert wfm.paths() == ["/v2/orders/item/no_such_item/top"]


def test_excluded_and_empty_slugs_never_fetch(m, wfm):
    assert m.price("vendor_relic") is None and m.price("ash_prime_scene") is None and m.price("") is None
    assert wfm.calls == []


def test_network_failure_cools_down(m, wfm):
    wfm.fail.update({"/v2/orders/item/braton_prime_barrel/top", "/v1/items/braton_prime_barrel/statistics"})
    assert m.price("braton_prime_barrel") is None
    n = len(wfm.calls)
    assert m.price("braton_prime_barrel") is None  # inside the 60 s cooldown: no request
    assert len(wfm.calls) == n == 2


def test_disk_cache_survives_restart(m, wfm, tmp_path):
    m.price("ash_prime_chassis_blueprint")
    m.flush()
    again = market.Market(str(tmp_path / "market"), FakeSettings())
    p = again.price("ash_prime_chassis_blueprint")
    assert p.low_sell == 12 and len(wfm.calls) == 1


def test_stale_disk_entries_are_ignored(m, wfm, tmp_path):
    m.price("ash_prime_chassis_blueprint")
    m.flush()
    path = os.path.join(str(tmp_path / "market"), "prices.json")
    data = json.load(open(path))
    for v in data.values():
        v["t"] -= market.PRICE_TTL + 1
    json.dump(data, open(path, "w"))
    market.Market(str(tmp_path / "market"), FakeSettings()).price("ash_prime_chassis_blueprint")
    assert len(wfm.calls) == 2


def test_platform_and_crossplay_from_settings(wfm, tmp_path):
    s = FakeSettings(wfm_platform="switch", wfm_crossplay=False)
    mk = market.Market(str(tmp_path / "market"), s)
    mk.price("ash_prime_chassis_blueprint")
    assert wfm.calls[-1][1]["Platform"] == "switch" and wfm.calls[-1][1]["Crossplay"] == "false"
    # A different platform is a different cache entry.
    s.data["wfm_platform"] = "pc"
    mk.price("ash_prime_chassis_blueprint")
    assert len(wfm.calls) == 2


def test_prices_and_prefetch(m, wfm):
    m.prices(["ash_prime_chassis_blueprint"])
    queued = m.prefetch(["ash_prime_chassis_blueprint", "braton_prime_barrel", "braton_prime_barrel",
                         "vendor_relic", "no_such_item"])
    assert queued == 2  # fresh, duplicate and excluded slugs skipped
    assert m.wait_idle(5)
    out = m.prices(["ash_prime_chassis_blueprint", "braton_prime_barrel", "no_such_item"])
    assert out["braton_prime_barrel"].median == 11 and out["no_such_item"] is None
    assert wfm.paths().count("/v2/orders/item/braton_prime_barrel/top") == 1


def test_concurrent_requests_share_one_fetch(m, wfm):
    results = []
    threads = [threading.Thread(target=lambda: results.append(m.price("ash_prime_chassis_blueprint")))
               for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(wfm.calls) == 1 and all(r and r.low_sell == 12 for r in results)


def test_catalog_lookup_and_search(m, wfm):
    assert m.lookup("/Lotus/Types/Recipes/WarframeRecipes/AshPrimeChassisBlueprint").slug == "ash_prime_chassis_blueprint"
    assert m.lookup("Ash Prime").slug == "ash_prime_set"          # "<name> set"
    assert m.lookup("ash prime set").slug == "ash_prime_set"
    assert m.lookup("Axi Vault Key").slug == "axi_a1_relic_key"   # "(Key)" alias
    assert m.lookup("zid-an_asheir").name == "Zid-an Asheir"       # minted slug kept verbatim
    assert m.slug_for("Serration") == "serration"
    names = [r["name"] for r in m.search("prime")]
    assert names[0] == "Ash Prime Set"  # none start with "prime": contains order
    res = m.search("bra")
    assert [r["slug"] for r in res] == ["braton_prime_barrel", "braton_prime_set"]
    assert res[0]["url"] == "https://warframe.market/items/braton_prime_barrel"
    assert m.search("b") == []
    assert wfm.paths().count("/v2/items") == 1


def test_catalog_disk_cache(m, wfm, tmp_path):
    m.catalog()
    m2 = market.Market(str(tmp_path / "market"), FakeSettings())
    assert len(m2.catalog()) == 10
    assert wfm.paths().count("/v2/items") == 1  # second load served by net.get_json's 24 h cache


def test_offline_search_uses_item_db(wfm, tmp_path, monkeypatch):
    from wfh.data import items as items_mod

    wfm.fail.add("/v2/items")
    items_mod.build_bundle(items_mod.dir_reader(os.path.join(ROOT, "tests", "fixtures", "data", "wfcd")),
                           str(tmp_path / "bundle"), "t", "2026-10-08T00:00:00Z")
    items_mod.set_data_dirs(str(tmp_path / "bundle"), None)
    try:
        mk = market.Market(None, FakeSettings())
        res = mk.search("rhino prime")
        assert res and res[0]["slug"].startswith("rhino_prime")
    finally:
        items_mod._dirs_override = None
        items_mod.reset()


def test_unranked_price_ignores_maxed_listings(m, wfm):
    """/top without ?rank= mixes R0 and R10; rank=None prices R0/unranked only, like stats."""
    p = m.price("serration")
    assert p.rank is None and p.low_sell == 8 and p.median == 9 and p.high_buy == 6 and p.sellers == 3
    o = m.orders("serration")
    assert {e["rank"] for e in o["sell"] + o["buy"]} == {0}
    assert wfm.paths() == ["/v2/orders/item/serration/top"]


def test_unranked_price_asks_for_rank_zero_when_catalog_knows(m, wfm):
    m.catalog()
    assert m.price("serration").low_sell == 8
    assert wfm.paths()[-1] == "/v2/orders/item/serration/top?rank=0"


def test_relic_price_is_intact_only(m, wfm):
    p = m.price("lith_a1_relic")
    assert p.low_sell == 3 and p.median == 4 and p.high_buy == 2 and p.sellers == 3
    users = [e["user"] for e in m.orders("lith_a1_relic")["sell"]]
    assert "NoSubtypeNed" in users and not any(u.startswith("Radiant") for u in users)


def test_offline_item_after_restart_keeps_cached_price(m, wfm, tmp_path):
    m.price("ash_prime_chassis_blueprint")
    m.flush()
    wfm.offline = True
    again = market.Market(str(tmp_path / "market"), FakeSettings())
    o = again.orders("ash_prime_chassis_blueprint")  # orders are not on disk: forced read fails
    assert o == {"sell": [], "buy": []}
    p = again.price("ash_prime_chassis_blueprint")
    assert p is not None and p.low_sell == 12
    again._save_disk(force=True)
    third = market.Market(str(tmp_path / "market"), FakeSettings())
    assert third.cached("ash_prime_chassis_blueprint")[1].low_sell == 12


def test_expired_price_served_when_refetch_fails(m, wfm):
    m.price("ash_prime_chassis_blueprint")
    key = m._key("ash_prime_chassis_blueprint", None)
    ts, p = m._prices[key]
    m._prices[key] = (ts - market.PRICE_TTL - 1, p)
    wfm.offline = True
    assert m.price("ash_prime_chassis_blueprint").low_sell == 12


def test_host_down_short_circuits_every_slug(m, wfm):
    wfm.offline = True
    slugs = ["ash_prime_chassis_blueprint", "braton_prime_barrel", "serration", "lith_a1_relic", "a", "b"]
    t0 = time.monotonic()
    out = m.prices(slugs)
    assert time.monotonic() - t0 < 1
    assert all(v is None for v in out.values())
    assert len(wfm.calls) == 1  # first /top fails, everything after answers from cache
    assert m.host_down() and m.catalog() == [] and len(wfm.calls) == 1
    m.prefetch(["ash_prime_chassis_blueprint"])
    assert m.wait_idle(5) and len(wfm.calls) == 1
    # Back online once the cooldown lapses.
    wfm.offline = False
    m._host_down_until = 0
    m._cooldown.clear()
    assert m.price("ash_prime_chassis_blueprint").low_sell == 12


def test_real_fetch_does_not_retry_network_errors(monkeypatch):
    calls = []

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(net.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(net, "_host_last", {})
    monkeypatch.setattr(net.time, "sleep", lambda s: None)
    mk = market.Market(None, FakeSettings())
    assert mk.price("ash_prime_chassis_blueprint") is None
    assert mk.price("braton_prime_barrel") is None
    assert len(calls) == 1


def test_prices_deadline_returns_cache_and_queues_rest(m, wfm, monkeypatch):
    events = []
    monkeypatch.setattr(market.rpc, "emit", lambda ev, data=None: events.append((ev, data)))
    m.price("ash_prime_chassis_blueprint")
    out = m.prices(["ash_prime_chassis_blueprint", "braton_prime_barrel"], deadline=0)
    assert out["ash_prime_chassis_blueprint"].low_sell == 12 and out["braton_prime_barrel"] is None
    assert m.wait_idle(5)
    assert events == [("market.price", {"slug": "braton_prime_barrel", "rank": None,
                                        "price": m.cached("braton_prime_barrel")[1].to_json()})]


def test_waiter_gets_stale_price_when_owner_fails(m, wfm):
    m.price("ash_prime_chassis_blueprint")
    key = m._key("ash_prime_chassis_blueprint", None)
    ts, p = m._prices[key]
    m._prices[key] = (ts - market.PRICE_TTL - 1, p)
    ev = threading.Event()
    m._inflight[key] = ev  # another thread owns a fetch that will fail
    res = []
    t = threading.Thread(target=lambda: res.append(m.price("ash_prime_chassis_blueprint")))
    t.start()
    time.sleep(0.05)
    m._inflight.pop(key)
    ev.set()
    t.join(5)
    assert res and res[0] is p and len(wfm.calls) == 1


def test_debounced_save_reaches_disk_and_concurrent_saves(m, wfm, tmp_path, monkeypatch):
    monkeypatch.setattr(market, "SAVE_DEBOUNCE", 0.1)
    m.price("ash_prime_chassis_blueprint")       # first save writes at once
    m.price("serration", rank=10)                 # inside the debounce: timer flushes it
    path = os.path.join(str(tmp_path / "market"), "prices.json")
    deadline = time.time() + 3
    while time.time() < deadline and len(json.load(open(path))) < 2:
        time.sleep(0.05)
    assert len(json.load(open(path))) == 2
    threads = [threading.Thread(target=m._save_disk, kwargs={"force": True}) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(json.load(open(path))) == 2
    assert [f for f in os.listdir(str(tmp_path / "market")) if f.endswith(".tmp")] == []


# ---- RPCs --------------------------------------------------------------------

def test_rpcs(app, wfm, tmp_path):
    market.set_market(market.Market(str(tmp_path / "rpc-market"), app.settings))
    try:
        item = asyncio.run(rpc.call_local("market.item", {"slug": "ash_prime_chassis_blueprint"}))
        assert item["name"] == "Ash Prime Chassis Blueprint"
        assert item["price"]["low_sell"] == 12 and len(item["orders"]["sell"]) == 5
        assert item["item"]["thumb"].startswith("https://assets.wfhelper.com/wfm/")
        out = asyncio.run(rpc.call_local("market.prices", {"slugs": ["braton_prime_barrel", "no_such_item"]}))
        assert out["braton_prime_barrel"]["source"] == "stats" and out["no_such_item"] is None
        hits = asyncio.run(rpc.call_local("market.search", {"query": "serr"}))
        assert hits[0]["slug"] == "serration" and hits[0]["max_rank"] == 10
        json.dumps([item, out, hits])  # JSON-able
    finally:
        market.set_market(None)
