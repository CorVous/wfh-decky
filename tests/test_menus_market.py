"""Market page view-models (menus/market.py) and warframe.market sign-in/orders (data/wfm_auth.py).

Offline: anonymous reads replay tests/fixtures/data/wfm through a fake net.fetch; the
authenticated API is a fake transport answering recorded-shape v1/v2 payloads; the websocket
is a fake socket that plays the server side of the @wfm|cmd protocol.
"""

import asyncio
import base64
import json
import os
import stat
import struct
import threading

import pytest

from wfh import net, rpc
from wfh.data import market as mk
from wfh.data import wfm_auth as wa
from wfh.menus import market as mm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WFM = os.path.join(ROOT, "tests", "fixtures", "data", "wfm")

ROUTES = {
    "/v2/items": "v2_items.json",
    "/v2/orders/item/ash_prime_chassis_blueprint/top": "top_ash_prime_chassis_blueprint.json",
    "/v2/orders/item/serration/top?rank=10": "top_serration_rank10.json",
    "/v2/orders/item/serration/top?rank=0": "top_serration_mixed.json",
}


def _fixture(name):
    with open(os.path.join(WFM, name), "rb") as f:
        return f.read()


class FakeFetch:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers=None, timeout=20, data=None, method=None, retry_network=True):
        path = url.replace(mk.API, "")
        self.calls.append(path)
        name = ROUTES.get(path)
        if name is None:
            raise net.HttpError(404, url, '{"error":"not found"}')
        return _fixture(name)


class FakeSettings:
    def __init__(self, **kw):
        self.data = {"wfm_platform": "pc", "wfm_crossplay": True, **kw}

    def get(self, key, default=None):
        return self.data.get(key, default)


@pytest.fixture
def m(monkeypatch, tmp_path):
    fake = FakeFetch()
    monkeypatch.setattr(net, "fetch", fake)
    monkeypatch.setattr(net, "cache_dir", str(tmp_path / "netcache"))
    market = mk.Market(str(tmp_path / "market"), FakeSettings())
    market.fake = fake
    mk.set_market(market)
    yield market
    mk.set_market(None)


# ---- whispers / pure ---------------------------------------------------------------------------

def test_whisper_matches_wfhelper_text():
    sell = {"user": "Glaive_Tenno", "platinum": 12, "per_trade": 1}
    assert mm.whisper("sell", sell, "Ash Prime Chassis Blueprint") == (
        "/w Glaive_Tenno Hi! I want to buy: Ash Prime Chassis Blueprint for 12 platinum. "
        "(warframe.market via WFHelper)")
    buy = {"user": "BulkBuyer", "platinum": 24, "per_trade": 3}
    assert mm.whisper("buy", buy, "Ash Prime Chassis Blueprint") == (
        "/w BulkBuyer Hi! I want to sell: Ash Prime Chassis Blueprint x3 for 24 platinum. "
        "(warframe.market via WFHelper)")
    assert mm.item_text("Serration", 10, True) == "Serration (Rank 10)"
    assert mm.item_text("Serration", 10, False) == "Serration"
    assert mm.item_text("Primed Flow", 0, True, "atragraph") == "Primed Flow (Atragraph) (Rank 0)"


def test_close_quantity_and_optimistic_update():
    o = {"id": "a", "quantity": 5, "perTrade": 2}
    assert mm.close_quantity(o) == 2
    assert mm.close_quantity(o, 9) == 5
    assert mm.close_quantity({"quantity": 1, "perTrade": 1}, 0) == 1
    rows = [{"id": "a", "quantity": 2, "perTrade": 2}, {"id": "b", "quantity": 1, "perTrade": 1}]
    assert mm.apply_close(rows, "a", 1) == [{"id": "a", "quantity": 1, "perTrade": 1}, rows[1]]
    assert mm.apply_close(rows, "b", 1) == [rows[0]]
    counts = mm.order_counts([{"type": "sell", "visible": True, "platinum": 10, "quantity": 2},
                              {"type": "sell", "visible": False, "platinum": 99, "quantity": 1},
                              {"type": "buy", "visible": True, "platinum": 5, "quantity": 1}])
    assert counts == {"sell": 2, "buy": 1, "total": 3, "visible": 2, "sellPlat": 20}


# ---- item detail -------------------------------------------------------------------------------

def test_detail_price_and_top_orders(m):
    d = mm.detail(m, "Ash Prime Chassis Blueprint")
    assert d["slug"] == "ash_prime_chassis_blueprint" and d["name"] == "Ash Prime Chassis Blueprint"
    assert d["price"]["lowSell"] == 12 and d["price"]["source"] == "top" and d["price"]["highBuy"] == 10
    assert [r["user"] for r in d["sell"]] == ["Glaive_Tenno", "RelicRunner", "Baro_Fan", "ash_main", "Lotus_Kid"]
    assert d["sell"][0]["whisper"].startswith("/w Glaive_Tenno Hi! I want to buy: Ash Prime Chassis Blueprint for 12")
    assert d["buy"][0]["user"] == "CheapSkate" and "I want to sell:" in d["buy"][0]["whisper"]
    assert d["maxRank"] is None and d["url"].endswith("/items/ash_prime_chassis_blueprint")
    json.dumps(d)


def test_detail_ranked_item_whisper_has_rank(m):
    d = mm.detail(m, "serration", 10)
    assert d["maxRank"] == 10 and d["rank"] == 10
    assert d["sell"][0]["user"] == "ModMaxer" and d["sell"][0]["rank"] == 10
    assert "Serration (Rank 10) for 45 platinum" in d["sell"][0]["whisper"]


def test_detail_force_rereads_orders_at_most_every_45s(m, monkeypatch):
    monkeypatch.setattr(mm, "_forced", {})
    mm.detail(m, "ash_prime_chassis_blueprint")
    top = "/v2/orders/item/ash_prime_chassis_blueprint/top"
    assert m.fake.calls.count(top) == 1
    mm.detail(m, "ash_prime_chassis_blueprint")  # plain reload: order book from cache
    assert m.fake.calls.count(top) == 1
    mm.detail(m, "ash_prime_chassis_blueprint", force=True)  # Refresh: one fresh read
    assert m.fake.calls.count(top) == 2
    mm.detail(m, "ash_prime_chassis_blueprint", force=True)  # again within 45 s: rate-limited
    assert m.fake.calls.count(top) == 2
    clock = mm.time.monotonic() + mm.FORCE_MIN_S + 1
    monkeypatch.setattr(mm.time, "monotonic", lambda: clock)
    mm.detail(m, "ash_prime_chassis_blueprint", force=True)
    assert m.fake.calls.count(top) == 3


def test_detail_unknown_item(m):
    d = mm.detail(m, "no_such_item")
    assert d["price"] is None and d["sell"] == [] and d["buy"] == []


# ---- recently viewed -----------------------------------------------------------------------------

def test_recent_store_dedupes_limits_and_persists(tmp_path):
    s = mm.RecentStore(str(tmp_path), limit=3)
    for slug in ("a", "b", "c", "a", "d"):
        s.add(slug, slug.upper(), None, at=1.0)
    assert [e["slug"] for e in s.items()] == ["d", "a", "c"]
    s.add("a", "A", 10, at=2.0)  # another rank is another entry
    assert [(e["slug"], e["rank"]) for e in s.items()] == [("a", 10), ("d", None), ("a", None)]
    again = mm.RecentStore(str(tmp_path), limit=3)
    assert again.items() == s.items()
    again.remove("a")
    assert [e["slug"] for e in again.items()] == ["d"]
    again.clear()
    assert mm.RecentStore(str(tmp_path)).items() == []


def test_recent_rows_use_cache_and_prefetch_the_rest(m, tmp_path, monkeypatch):
    s = mm.RecentStore(str(tmp_path))
    s.add("serration", "Serration", 10)
    s.add("ash_prime_chassis_blueprint", "Ash Prime Chassis Blueprint")
    m.price("ash_prime_chassis_blueprint")
    queued = []
    monkeypatch.setattr(m, "prefetch", lambda slugs, rank=None, notify=False: queued.append((list(slugs), rank, notify)))
    monkeypatch.setattr(mm, "_prefetched", {})
    n = len(m.fake.calls)
    rows = mm.recent_rows(s, m, 0, limit=5)
    assert len(m.fake.calls) == n  # never touches the network
    assert rows[0]["slug"] == "ash_prime_chassis_blueprint" and rows[0]["fresh"] and rows[0]["price"]["plat"]
    assert rows[1]["slug"] == "serration" and rows[1]["price"] is None and not rows[1]["fresh"]
    assert queued == [(["serration"], 10, True)]
    # each prefetch emits market.price and the page reloads: the same slug is not queued again
    # inside the retry window (an item in its error cool-down would otherwise loop)
    for _ in range(5):
        mm.recent_rows(s, m, 0, limit=5)
    assert queued == [(["serration"], 10, True)]


def test_recent_rows_show_expired_price_and_skip_prefetch_while_host_down(m, tmp_path, monkeypatch):
    s = mm.RecentStore(str(tmp_path))
    s.add("ash_prime_chassis_blueprint", "Ash Prime Chassis Blueprint")
    p = m.price("ash_prime_chassis_blueprint")
    queued = []
    monkeypatch.setattr(m, "prefetch", lambda slugs, rank=None, notify=False: queued.append(list(slugs)))
    monkeypatch.setattr(mm, "_prefetched", {})
    later = mk.time.time() + mk.PRICE_TTL + 5
    monkeypatch.setattr(mk.time, "time", lambda: later)
    monkeypatch.setattr(m, "host_down", lambda: True)
    rows = mm.recent_rows(s, m, later)
    assert not rows[0]["fresh"] and rows[0]["price"]["plat"] == p.plat  # last known, expired
    assert queued == []
    monkeypatch.setattr(m, "host_down", lambda: False)
    mm.recent_rows(s, m, later)
    assert queued == [["ash_prime_chassis_blueprint"]]


# ---- warframe.market account -----------------------------------------------------------------------

def _jwt(claims):
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'HS256'})}.{enc(claims)}.sig"


TOKEN = _jwt({"sub": "u1", "csrf_token": "abc"})
TOKEN2 = _jwt({"sub": "u1", "n": 2})

MY_ORDERS = {"apiVersion": "0.25.0", "data": [
    {"id": "o_buy1", "type": "buy", "platinum": 30, "quantity": 1, "visible": True, "rank": 10,
     "itemId": "54e644ffe779897594fa68d2", "updatedAt": "2026-10-16T10:00:00Z"},
    {"id": "o_sell2", "type": "sell", "platinum": 15, "quantity": 4, "perTrade": 2, "visible": False,
     "itemId": "5751ebf43a3eba7b41a0a2c3"},
    {"id": "o_sell1", "type": "sell", "platinum": 65, "quantity": 2, "visible": True,
     "itemId": "5751ebf43a3eba7b41a0a2c5"},
    {"id": "o_unknown", "type": "sell", "platinum": 5, "quantity": 1, "visible": True, "itemId": "deadbeef"},
]}


class FakeApi:
    """Transport for wfm_auth: routes (method, path) to (status, headers, body)."""

    def __init__(self):
        self.calls = []
        self.routes = {
            ("POST", "/v1/auth/signin"): (200, {"authorization": f"JWT {TOKEN}"},
                                          {"payload": {"user": {"ingame_name": "Glaive_Tenno", "platform": "pc",
                                                                "id": "u1", "slug": "glaive_tenno"}}}),
            ("GET", "/v2/me"): (200, {}, {"data": {"id": "u1", "ingameName": "Glaive_Tenno", "slug": "glaive-tenno",
                                                   "platform": "pc"}}),
            ("GET", "/v2/user/glaive-tenno"): (200, {}, {"data": {"slug": "glaive-tenno", "status": "offline"}}),
            ("GET", "/v2/orders/my"): (200, {}, MY_ORDERS),
        }
        self.default = (200, {}, {"data": {}})

    def __call__(self, method, url, headers, body, timeout):
        path = url.replace(wa.API, "")
        self.calls.append((method, path, dict(headers), json.loads(body) if body else None))
        status, hdrs, payload = self.routes.get((method, path), self.default)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return wa.HttpResponse(status, dict(hdrs), raw)

    def last(self, method=None):
        return [c for c in self.calls if method is None or c[0] == method][-1]


@pytest.fixture
def api(tmp_path):
    fake = FakeApi()
    a = wa.WfmAuth(str(tmp_path / "settings"), transport=fake)
    a.fake = fake
    return a


def test_sign_in_stores_token_0600_and_never_the_password(api, tmp_path):
    st = api.sign_in("tenno@example.com", "hunter2")
    method, path, headers, body = api.fake.calls[0]
    assert (method, path) == ("POST", "/v1/auth/signin")
    assert headers["auth_type"] == "header" and headers["Authorization"] == "JWT" and headers["Platform"] == "pc"
    assert body["auth_type"] == "header" and body["email"] == "tenno@example.com" and body["device_id"]
    assert st["signedIn"] and st["userName"] == "Glaive_Tenno" and st["slug"] == "glaive-tenno"
    assert st["status"] == "invisible"  # public profile "offline" maps to invisible
    p = tmp_path / "settings" / wa.SESSION_FILE
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    raw = p.read_text()
    assert TOKEN in raw and "hunter2" not in raw and "tenno@example.com" not in raw
    # device id is stable across sessions
    assert wa.WfmAuth(str(tmp_path / "settings"), transport=api.fake).device_id() == body["device_id"]
    # a new process picks the session up
    again = wa.WfmAuth(str(tmp_path / "settings"), transport=api.fake)
    assert again.signed_in and again.token == TOKEN and again.slug == "glaive-tenno"


def test_sign_in_token_from_body_and_cookie():
    r = wa.HttpResponse(200, {"set-cookie": f"JWT={TOKEN}; Path=/; HttpOnly"}, b"{}")
    assert wa.extract_token(r) == TOKEN
    r = wa.HttpResponse(200, {}, json.dumps({"payload": {"token": TOKEN2}}).encode())
    assert wa.extract_token(r) == TOKEN2
    assert wa.jwt_claims(TOKEN)["csrf_token"] == "abc"


@pytest.mark.parametrize("status,headers,body,code,text", [
    (401, {}, {"error": {"password": "app.account.password_invalid"}}, "WFM_SIGNIN_REJECTED", "Wrong password."),
    (400, {}, {"error": {"email": "app.account.email_not_exist"}}, "WFM_SIGNIN_REJECTED", "No warframe.market"),
    (429, {"retry-after": "12"}, {}, "WFM_RATE_LIMITED", "wait 12s"),
    (403, {"cf-mitigated": "challenge"}, b"<html>Just a moment...</html>", "WFM_CHALLENGE", "Cloudflare"),
])
def test_sign_in_errors(api, status, headers, body, code, text):
    api.fake.routes[("POST", "/v1/auth/signin")] = (status, headers, body)
    with pytest.raises(wa.WfmError) as e:
        api.sign_in("tenno@example.com", "hunter2")
    assert e.value.code == code and text in str(e.value)
    assert "hunter2" not in str(e.value)
    assert not api.signed_in and len(api.fake.calls) == 1  # never retried


def test_sign_in_validates_input(api):
    with pytest.raises(wa.WfmError):
        api.sign_in("not-an-email", "x")
    assert api.fake.calls == []


def test_error_detail_flattening():
    assert wa.error_detail({"error": {"inputs": {"perTrade": "app.field.required"}}}) == "perTrade: app.field.required"
    assert wa.error_detail({"error": "boom"}) == "boom"
    assert wa.error_detail({"message": "m"}) == "m"


def test_my_orders_enriched_from_catalog(api, m):
    api.sign_in("tenno@example.com", "pw")
    rows = api.my_orders(mm._item_by_id_factory(m))
    # sell before buy, visible before hidden, then by name ("(unknown item)" sorts first)
    assert [o["id"] for o in rows] == ["o_unknown", "o_sell1", "o_sell2", "o_buy1"]
    s1 = rows[1]
    assert s1["name"] == "Ash Prime Set" and s1["slug"] == "ash_prime_set" and s1["type"] == "sell"
    assert s1["gameRef"] == "/Lotus/Powersuits/Ninja/AshPrime" and s1["perTrade"] == 1
    assert rows[0]["name"] == "(unknown item)"
    assert rows[2]["visible"] is False and rows[2]["perTrade"] == 2 and rows[2]["quantity"] == 4
    assert rows[3]["rank"] == 10 and rows[3]["maxRank"] == 10 and rows[3]["name"] == "Serration"
    _, path, headers, _ = api.fake.last("GET")
    assert headers["Authorization"] == f"Bearer {TOKEN}" and headers["auth_type"] == "header"


def test_order_writes_shapes_and_rotation(api, tmp_path):
    api.sign_in("tenno@example.com", "pw")
    api.fake.default = (200, {"authorization": f"Bearer {TOKEN2}"}, {"data": {}})
    api.close_order("o_sell1", 2)
    assert api.fake.last()[:2] == ("POST", "/v2/order/o_sell1/close") and api.fake.last()[3] == {"quantity": 2}
    assert api.token == TOKEN2  # rotated and persisted
    assert TOKEN2 in (tmp_path / "settings" / wa.SESSION_FILE).read_text()
    api.delete_order("o_sell2")
    assert api.fake.last()[:2] == ("DELETE", "/v2/order/o_sell2")
    api.update_order("o_buy1", visible=False)
    assert api.fake.last()[:2] == ("PATCH", "/v2/order/o_buy1") and api.fake.last()[3] == {"visible": False}
    api.update_order("o_buy1", platinum=31, quantity=None)
    assert api.fake.last()[3] == {"platinum": 31}
    with pytest.raises(wa.WfmError):
        api.update_order("o_buy1", platinum=0)
    with pytest.raises(wa.WfmError):
        api.close_order("../../me", 1)
    with pytest.raises(wa.WfmError):
        api.close_order("o_sell1", 0)


def test_unauthorized_signs_out(api, tmp_path):
    api.sign_in("tenno@example.com", "pw")
    api.fake.routes[("GET", "/v2/orders/my")] = (401, {}, {"error": "unauthorized"})
    with pytest.raises(wa.WfmUnauthorized):
        api.my_orders()
    assert not api.signed_in and not (tmp_path / "settings" / wa.SESSION_FILE).exists()
    with pytest.raises(wa.WfmUnauthorized):
        api.delete_order("o_sell1")


def test_per_order_write_lock(api):
    api.sign_in("tenno@example.com", "pw")
    gate, entered = threading.Event(), threading.Event()
    inner = api.transport

    def slow(method, url, headers, body, timeout):
        if method == "DELETE":
            entered.set()
            gate.wait(5)
        return inner(method, url, headers, body, timeout)

    api.transport = slow
    t = threading.Thread(target=api.delete_order, args=("o_sell1",))
    t.start()
    assert entered.wait(5)
    with pytest.raises(wa.WfmError) as e:
        api.close_order("o_sell1", 1)
    assert e.value.code == "WFM_ORDER_BUSY"
    api.close_order("o_sell2", 1)  # other orders are not blocked
    gate.set()
    t.join(5)
    api.close_order("o_sell1", 1)  # lock released


# ---- websocket status --------------------------------------------------------------------------------

def _server_frame(text: str, opcode: int = 1) -> bytes:
    data = text.encode()
    assert len(data) < 65536
    head = bytes([0x80 | opcode]) + (bytes([len(data)]) if len(data) < 126 else bytes([126]) + struct.pack(">H", len(data)))
    return head + data


def _decode_client(buf: bytes):
    out = []
    while buf:
        op, n = buf[0] & 0x0F, buf[1] & 0x7F
        assert buf[1] & 0x80, "client frames must be masked"
        i = 2
        if n == 126:
            n, i = struct.unpack(">H", buf[2:4])[0], 4
        mask, data = buf[i:i + 4], buf[i + 4:i + 4 + n]
        out.append((op, bytes(b ^ mask[k % 4] for k, b in enumerate(data))))
        buf = buf[i + 4 + n:]
    return out


class FakeWsSocket:
    """Plays the WFM server: ok to signIn (after a ping), ok to status/set."""

    def __init__(self, fail_auth=False):
        self.sent = b""
        self.inbox = b""
        self.fail_auth = fail_auth
        self.closed = False

    def sendall(self, data):
        self.sent += data
        for op, payload in _decode_client(data):
            if op != 1:
                continue
            msg = json.loads(payload)
            if msg["route"] == "@wfm|cmd/auth/signIn":
                self.inbox += _server_frame("", opcode=9)
                route = "@wfm|cmd/auth/signIn:error" if self.fail_auth else "@wfm|cmd/auth/signIn:ok"
                self.inbox += _server_frame(json.dumps({"route": route, "payload": {}}))
            elif msg["route"] == "@wfm|cmd/status/set":
                self.inbox += _server_frame(json.dumps({"route": "@wfm|event/reports/online", "payload": {"x": "y" * 300}}))
                self.inbox += _server_frame(json.dumps({"route": "@wfm|cmd/status/set:ok",
                                                        "payload": {"status": msg["payload"]["status"],
                                                                    "statusUntil": "2026-10-17T13:00:00Z"}}))

    def recv(self, n):
        out, self.inbox = self.inbox[:n], self.inbox[n:]
        return out

    def close(self):
        self.closed = True


def test_set_status_over_websocket(api):
    api.sign_in("tenno@example.com", "pw")
    sock = FakeWsSocket()
    api.ws_factory = lambda: wa.WsConn(sock)
    st = api.set_status("ingame", 60)
    assert st["status"] == "ingame" and st["statusUntil"] == "2026-10-17T13:00:00Z"
    frames = _decode_client(sock.sent)
    texts = [json.loads(p) for op, p in frames if op == 1]
    assert texts[0]["route"] == "@wfm|cmd/auth/signIn" and texts[0]["payload"] == {"token": TOKEN}
    assert len(texts[0]["id"]) == 11
    assert texts[1] == {"route": "@wfm|cmd/status/set", "payload": {"status": "ingame", "duration": 3600},
                        "id": texts[1]["id"]}
    assert [op for op, _ in frames] == [1, 10, 1, 8]  # text, pong, text, close
    assert sock.closed


def test_set_status_errors(api):
    with pytest.raises(wa.WfmUnauthorized):
        api.set_status("online")
    api.sign_in("tenno@example.com", "pw")
    with pytest.raises(wa.WfmError):
        api.set_status("busy")
    api.ws_factory = lambda: wa.WsConn(FakeWsSocket(fail_auth=True))
    with pytest.raises(wa.WfmError, match="refused"):
        api.set_status("online")

    def offline():
        raise OSError("Network is unreachable")

    api.ws_factory = offline
    with pytest.raises(wa.WfmError) as e:
        api.set_status("online")
    assert e.value.code == "WFM_OFFLINE"


# ---- RPCs ----------------------------------------------------------------------------------------

def test_rpcs_end_to_end(app, m, tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(rpc, "emit", lambda name, data=None: events.append((name, data)))
    fake = FakeApi()
    mm.set_auth(wa.WfmAuth(str(tmp_path / "rpc-settings"), transport=fake))
    mm._recent = mm.RecentStore(str(tmp_path / "rpc-runtime"))
    run = lambda name, params=None: asyncio.run(rpc.call_local(name, params or {}))  # noqa: E731
    try:
        acct = run("market.account")
        assert acct["signedIn"] is False and acct["holdChoices"] == [0, 30, 60, 120, 240]
        assert run("market.my_orders")["orders"] == []

        d = run("market.detail", {"slug": "ash_prime_chassis_blueprint"})
        assert d["sell"][0]["whisper"].startswith("/w Glaive_Tenno")
        run("market.detail", {"slug": "serration", "rank": 10})
        recent = run("market.recent")
        assert [(r["slug"], r["rank"]) for r in recent] == [("serration", 10), ("ash_prime_chassis_blueprint", None)]
        assert recent[1]["price"]["lowSell"] == 12

        bad = run("market.sign_in", {"email": "x", "password": ""})
        assert bad["error"] and not bad["signedIn"]
        st = run("market.sign_in", {"email": "tenno@example.com", "password": "pw"})
        assert st["signedIn"] and st["userName"] == "Glaive_Tenno" and "error" not in st
        assert ("market.account", st) in events

        q = run("market.qam")
        assert q["account"]["signedIn"] and len(q["recent"]) == 2

        orders = run("market.my_orders", {"force": True})
        assert orders["counts"] == {"sell": 3, "buy": 1, "total": 4, "visible": 3, "sellPlat": 135}
        assert orders["orders"][0]["owned"] is None or isinstance(orders["orders"][0]["owned"], int)

        # sold one of a perTrade=2 listing: closes 2, subtracts locally
        r = run("market.order_close", {"id": "o_sell2"})
        assert r["ok"] and r["closed"] == 2
        assert next(o for o in r["orders"] if o["id"] == "o_sell2")["quantity"] == 2
        assert fake.last("POST")[3] == {"quantity": 2}
        r = run("market.order_delete", {"id": "o_unknown"})
        assert r["ok"] and "o_unknown" not in [o["id"] for o in r["orders"]]
        r = run("market.order_visible", {"id": "o_sell1", "visible": False})
        assert r["ok"] and next(o for o in r["orders"] if o["id"] == "o_sell1")["visible"] is False
        r = run("market.order_update", {"id": "o_sell1", "platinum": 60})
        assert r["ok"] and fake.last("PATCH")[3] == {"platinum": 60}
        r = run("market.orders_visible", {"visible": True, "type": "sell"})
        assert r["ok"] and r["changed"] >= 1
        assert any(name == "market.orders" for name, _ in events)

        # a 429 mid-bulk stops the loop: one PATCH sent, the rest reported as skipped
        before = len([c for c in fake.calls if c[0] == "PATCH"])
        fake.default = (429, {"retry-after": "1"}, {})
        r = run("market.orders_visible", {"visible": False})
        fake.default = (200, {}, {"data": {}})
        assert len([c for c in fake.calls if c[0] == "PATCH"]) == before + 1
        assert r["ok"] is False and r["changed"] == 0 and len(r["failed"]) >= 2
        assert all("rate limited" in f["error"].lower() or "wait" in f["error"].lower() for f in r["failed"])

        sock = FakeWsSocket()
        mm.auth().ws_factory = lambda: wa.WsConn(sock)
        st = run("market.set_status", {"status": "online", "hold_min": 30})
        assert st["status"] == "online" and st["holdMin"] == 30 and "error" not in st
        assert app.settings.get("market.status_hold_min") == 30

        fake.routes[("POST", "/v2/order/o_sell1/close")] = (401, {}, {"error": "unauthorized"})
        r = run("market.order_close", {"id": "o_sell1"})
        assert r["ok"] is False and r["code"] == "WFM_UNAUTHORIZED"
        assert run("market.account")["signedIn"] is False

        out = run("market.sign_out")
        assert out["signedIn"] is False
        run("market.recent_remove", {"slug": "serration"})
        assert [r["slug"] for r in run("market.recent")] == ["ash_prime_chassis_blueprint"]
        run("market.recent_clear")
        assert run("market.recent") == []
        json.dumps(events)
    finally:
        app.settings.update({"market": {"status_hold_min": 0}})
        mm.set_auth(None)
        mm._recent = mm.RecentStore(app.runtime_dir)


class NoisyWsSocket(FakeWsSocket):
    """Sends a frame that is not JSON before each answer (skipped, like parseWfmWsMessage)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.timeouts = []

    def settimeout(self, t):
        self.timeouts.append(t)

    def sendall(self, data):
        before = len(self.inbox)
        super().sendall(data)
        if len(self.inbox) > before:
            self.inbox = self.inbox[:before] + _server_frame("not json {") + self.inbox[before:]


def test_set_status_skips_non_json_frames(api):
    api.sign_in("tenno@example.com", "pw")
    sock = NoisyWsSocket()
    api.ws_factory = lambda: wa.WsConn(sock)
    st = api.set_status("online", 0)
    assert st["status"] == "online"
    assert sock.timeouts and all(0 < t <= wa.WS_TIMEOUT for t in sock.timeouts)


def test_set_status_has_one_overall_deadline(api, monkeypatch):
    api.sign_in("tenno@example.com", "pw")

    class Chatty(FakeWsSocket):
        """Never answers the sign-in, only sends unrelated events."""

        def sendall(self, data):
            self.sent += data
            self.inbox += b"".join(_server_frame(json.dumps({"route": "@wfm|event/x", "payload": {}}))
                                   for _ in range(60))

    clock = [0.0]

    def tick():
        clock[0] += 6.0
        return clock[0]

    monkeypatch.setattr(wa.time, "monotonic", tick)
    api.ws_factory = lambda: wa.WsConn(Chatty())
    with pytest.raises(wa.WfmError, match="timeout"):
        api.set_status("online")


def test_qam_backs_off_when_my_orders_fails(app, m, tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(rpc, "emit", lambda name, data=None: events.append((name, data)))
    monkeypatch.setattr(app, "submit", lambda fn, *a: fn(*a))  # run the background fetch inline
    fake = FakeApi()
    fake.routes[("GET", "/v2/orders/my")] = (500, {}, {"error": "server"})
    mm.set_auth(wa.WfmAuth(str(tmp_path / "qam-settings"), transport=fake))
    run = lambda name, params=None: asyncio.run(rpc.call_local(name, params or {}))  # noqa: E731
    try:
        mm.auth().sign_in("tenno@example.com", "pw")
        fetches = lambda: sum(1 for c in fake.calls if c[:2] == ("GET", "/v2/orders/my"))  # noqa: E731
        for _ in range(5):  # the QAM reloads on every market.orders event
            q = run("market.qam")
        assert fetches() == 1
        assert q["ordersError"] and "500" in q["ordersError"] and q["counts"] is None
        assert [n for n, _ in events].count("market.orders") == 1

        def offline(*a, **k):
            raise OSError("Network is unreachable")

        fake_tr = mm.auth().transport
        mm.auth().transport = offline
        later = mm.time.time() + mm.ORDERS_STALE_S + 1
        monkeypatch.setattr(mm.time, "time", lambda: later)
        for _ in range(5):
            q = run("market.qam")
        assert "unreachable" in q["ordersError"]
        mm.auth().transport = fake_tr
        assert fetches() == 1  # the offline attempt never reached the fake API, and only one ran
        assert [n for n, _ in events].count("market.orders") == 2
    finally:
        mm.set_auth(None)


def test_set_hold_without_status(app):
    run = lambda name, params=None: asyncio.run(rpc.call_local(name, params or {}))  # noqa: E731
    try:
        st = run("market.set_hold", {"hold_min": 120})
        assert st["holdMin"] == 120 and app.settings.get("market.status_hold_min") == 120
    finally:
        app.settings.update({"market": {"status_hold_min": 0}})


def test_urllib_transport_429_holds_the_whole_host(monkeypatch):
    import io
    import urllib.error
    from email.message import Message
    hdrs = Message()
    hdrs["Retry-After"] = "7"
    err = urllib.error.HTTPError(wa.API + "/v2/order/x", 429, "Too Many", hdrs, io.BytesIO(b"{}"))
    held = []
    monkeypatch.setattr(net, "_throttle", lambda host: None)
    monkeypatch.setattr(net, "_hold_host", lambda host, d: held.append((host, d)))
    monkeypatch.setattr(wa._opener, "open", lambda *a, **k: (_ for _ in ()).throw(err))
    resp = wa.urllib_transport("PATCH", wa.API + "/v2/order/x", {}, b"{}", 5)
    assert resp.status == 429 and held == [("api.warframe.market", 7.0)]
