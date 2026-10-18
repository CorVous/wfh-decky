"""Dev-only HTTP bridge for the page preview harness (never packaged).

Runs the real Python worker in-process (wfh.app.create + load_modules, exactly as
wfh/worker.py does) and exposes it to the browser preview:

  POST /rpc      {"method": "...", "params": {...}} -> {"result": ...} | {"error": "..."}
  GET  /events   ?since=N -> {"seq": M, "events": [[seq, name, data], ...]} (worker rpc.emit)
  GET  /clock    {"now_ms": ...} the pinned clock the worker sees (browser uses the same)
  GET  /*        static files from --static (the esbuild bundle + index.html)

Offline by construction: every urllib request is answered by a router that serves the
recorded fixtures (tests/fixtures/world, tests/fixtures/data/wfm) or synthesized
warframe.market data (preview/seed.py); anything else fails at once with URLError.
The clock is shifted so "now" matches the recorded world state (2026-10-17T12:00Z) plus
elapsed time; pass --real-time to keep the wall clock.

Usage: bin/python/bin/python3.12 -s preview/bridge.py --tmp DIR --static DIR [--port 0]
Prints "READY port=<n>" on stdout once modules are loaded and the server listens.
"""

from __future__ import annotations

import argparse
import asyncio
import email.message
import io
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(ROOT, "tests", "fixtures")
FIXTURE_NOW_MS = int(datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)

ap = argparse.ArgumentParser()
ap.add_argument("--tmp", required=True, help="scratch dir for HOME, settings, runtime and logs")
ap.add_argument("--static", required=True, help="directory served at /")
ap.add_argument("--port", type=int, default=0)
ap.add_argument("--real-time", action="store_true", help="do not pin the clock to the fixture time")
ap.add_argument("--empty", action="store_true", help="no seeded inventory/stats/arbi runs (first-run look)")
ARGS = ap.parse_args()

TMP = os.path.abspath(ARGS.tmp)
for sub in ("home", "settings", "runtime", "log"):
    os.makedirs(os.path.join(TMP, sub), exist_ok=True)
os.environ["HOME"] = os.path.join(TMP, "home")
os.environ["XDG_CACHE_HOME"] = os.path.join(TMP, "home", ".cache")
os.environ["XDG_CONFIG_HOME"] = os.path.join(TMP, "home", ".config")
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "home", ".local", "share")
os.environ.pop("DISPLAY", None)
os.environ.pop("WAYLAND_DISPLAY", None)

# ---- clock -------------------------------------------------------------------------------------
_real_time = time.time
_OFFSET = 0.0 if ARGS.real_time else _real_time() - FIXTURE_NOW_MS / 1000


def _shifted_time() -> float:
    return _real_time() - _OFFSET


time.time = _shifted_time  # before any wfh import, so module-level `from time import time` sees it too


def now_ms() -> int:
    return int(time.time() * 1000)


sys.path.insert(0, os.path.join(ROOT, "py_modules"))
sys.path.insert(0, HERE)
import seed  # noqa: E402

# ---- offline network router --------------------------------------------------------------------
NET_LOG: list[str] = []


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


class _Resp(io.BytesIO):
    def __init__(self, url: str, body: bytes, status: int = 200, ctype: str = "application/json"):
        super().__init__(body)
        self.url = url
        self.status = self.code = status
        msg = email.message.Message()
        msg["Content-Type"] = ctype
        msg["Content-Length"] = str(len(body))
        self.headers = msg

    def getcode(self):
        return self.status

    def geturl(self):
        return self.url

    def info(self):
        return self.headers

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


_wfm_items_cache: bytes | None = None


def route(url: str, method: str = "GET") -> bytes | None:
    """Fixture body for url, or None when the harness has nothing (-> offline error)."""
    global _wfm_items_cache
    from wfh.data import worldstate as ws

    world = os.path.join(FIX, "world")
    if url in ws.FETCH_URLS:
        return _read(os.path.join(world, "worldState.json"))
    if url.startswith(ws.ORACLE_BOUNTY_CYCLE_URL):
        return _read(os.path.join(world, "oracle_bounty_cycle.json"))
    if url.startswith(ws.WARFRAMESTAT_URL):
        return _read(os.path.join(world, "warframestat_pc.json"))
    if url.startswith("https://browse.wf/arbys.txt"):
        return _read(os.path.join(world, "arbys.txt"))
    p = urllib.parse.urlparse(url)
    if p.netloc == "api.warframe.market" and method.upper() == "GET":
        path, q = p.path, urllib.parse.parse_qs(p.query)
        if path == "/v2/items":
            if _wfm_items_cache is None:
                _wfm_items_cache = json.dumps(seed.wfm_items()).encode()
            return _wfm_items_cache
        m = re.fullmatch(r"/v2/orders/item/([^/]+)/top", path)
        if m:
            slug = urllib.parse.unquote(m.group(1))
            rank = int(q["rank"][0]) if "rank" in q else None
            fx = os.path.join(FIX, "data", "wfm", f"top_{slug}" + (f"_rank{rank}" if rank else "") + ".json")
            if os.path.isfile(fx):
                return _read(fx)
            return json.dumps(seed.wfm_top(slug, rank, now_ms())).encode()
        m = re.fullmatch(r"/v1/items/([^/]+)/statistics", path)
        if m:
            slug = urllib.parse.unquote(m.group(1))
            fx = os.path.join(FIX, "data", "wfm", f"stats_{slug}.json")
            if os.path.isfile(fx):
                return _read(fx)
            return json.dumps(seed.wfm_statistics(slug, now_ms())).encode()
    return None


def _fake_open(self, fullurl, data=None, timeout=None, *a, **kw):  # noqa: ARG001
    if isinstance(fullurl, urllib.request.Request):
        url, method = fullurl.full_url, fullurl.get_method()
    else:
        url, method = str(fullurl), "POST" if data else "GET"
    try:
        body = route(url, method)
    except Exception as e:  # noqa: BLE001
        NET_LOG.append(f"route error {url}: {e}")
        body = None
    if body is None:
        NET_LOG.append(f"offline {method} {url}")
        raise urllib.error.URLError(f"preview harness is offline ({urllib.parse.urlparse(url).netloc})")
    NET_LOG.append(f"fixture {method} {url}")
    return _Resp(url, body)


urllib.request.OpenerDirector.open = _fake_open  # urlopen() and every build_opener() instance

# Raw sockets (warframe.market websocket, anything else) must not hang either.
import socket  # noqa: E402

_real_create_connection = socket.create_connection


def _no_connect(address, *a, **kw):
    host = address[0] if isinstance(address, tuple) else address
    if host in ("127.0.0.1", "localhost", "::1"):
        return _real_create_connection(address, *a, **kw)
    raise OSError(f"preview harness is offline ({host})")


socket.create_connection = _no_connect

# ---- worker ------------------------------------------------------------------------------------
from wfh import app as app_mod, log, rpc  # noqa: E402

EVENTS: list[list] = []
_ev_lock = threading.Lock()


def _emit(event: str, data=None) -> None:
    try:
        data = json.loads(json.dumps(data, default=rpc._json_default))
    except Exception:  # noqa: BLE001
        data = None
    with _ev_lock:
        EVENTS.append([len(EVENTS) + 1, event, data])
        del EVENTS[:-500]


rpc.emit = _emit

settings_dir = os.path.join(TMP, "settings")
runtime_dir = os.path.join(TMP, "runtime")
log_dir = os.path.join(TMP, "log")
inv_path = os.path.join(TMP, "home", "inventory-export.json")
ee_path = os.path.join(TMP, "home", "EE.log")

if not ARGS.empty:
    with open(inv_path, "w", encoding="utf-8") as f:
        json.dump(seed.build_inventory(now_ms()), f)
    hist, trades = seed.stats_history(time.time())
    with open(os.path.join(runtime_dir, "stats-history.json"), "w") as f:
        json.dump(hist, f)
    with open(os.path.join(runtime_dir, "stats-trades.json"), "w") as f:
        json.dump(trades, f)
    with open(os.path.join(runtime_dir, "mission-rewards.json"), "w") as f:
        json.dump(seed.mission_history(time.time()), f)
with open(ee_path, "w", encoding="utf-8") as f:
    f.write(open(os.path.join(FIX, "inventory", "EE_tail.txt"), encoding="utf-8").read())
settings_file = os.path.join(settings_dir, "settings.json")
if not os.path.exists(settings_file):
    with open(settings_file, "w") as f:
        json.dump({"inventory_source": "none" if ARGS.empty else "file", "inventory_file": "" if ARGS.empty else inv_path,
                   "ee_log_path": ee_path, "data_auto_refresh": False}, f)

log.setup(log_dir)
APP = app_mod.create(ROOT, settings_dir, runtime_dir, log_dir, headless=True)
FAILED = app_mod.load_modules(APP)
APP.services["failed_modules"] = FAILED

LOOP = asyncio.new_event_loop()


def _loop_main() -> None:
    asyncio.set_event_loop(LOOP)
    LOOP.run_forever()


threading.Thread(target=_loop_main, name="bridge-loop", daemon=True).start()


async def _start() -> None:
    APP.loop = asyncio.get_running_loop()
    for start in APP.services.get("on_start", []):
        try:
            start()
        except Exception:  # noqa: BLE001
            traceback.print_exc()


asyncio.run_coroutine_threadsafe(_start(), LOOP).result(30)


def call(method: str, params: dict | None, timeout: float = 60):
    return asyncio.run_coroutine_threadsafe(rpc.call_local(method, params or {}), LOOP).result(timeout)


def _wait_inventory(limit: float = 15) -> None:
    end = _real_time() + limit
    while _real_time() < end:
        svc = APP.services.get("inventory")
        if svc is None or svc.current is not None:
            return
        time.sleep(0.1)


def _warm(method: str, params: dict | None = None) -> None:
    try:
        call(method, params)
    except Exception as e:  # noqa: BLE001
        print(f"[bridge] warm-up {method} failed: {type(e).__name__}: {e}", file=sys.stderr)


def _drain_market(limit: float = 30) -> None:
    from wfh.data import market as mk

    m = mk.get()
    end = _real_time() + limit
    while _real_time() < end:
        with m._lock:
            if not m._queued:
                return
        time.sleep(0.1)
    print("[bridge] market prefetch queue did not drain", file=sys.stderr)


# Warm the caches the real worker fills in its first minutes (world state on start, price
# prefetch after the inventory sync, a few recently viewed market items), so pages show the
# steady state instead of "not loaded yet".
from wfh import net  # noqa: E402

net._HOST_INTERVAL["api.warframe.market"] = 0.0  # local fixtures: no need to pace
_warm("world.view")
_warm("world.arbi_schedule")
if not ARGS.empty:
    _wait_inventory()
    arbi_dir = os.path.join(FIX, "arbi")
    for name in sorted(os.listdir(arbi_dir)):  # only the Stöfler one is a complete run
        _warm("arbi.import_log", {"path": os.path.join(arbi_dir, name)})
    for name, text in seed.arbi_logs(time.time()):  # plus synthetic runs over the last two weeks
        p = os.path.join(TMP, "home", name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        _warm("arbi.import_log", {"path": p})
    for slug, rank in (("mesa_prime_set", None), ("ash_prime_chassis_blueprint", None), ("serration", 10),
                       ("lith_a1_relic", None), ("braton_prime_barrel", None)):
        _warm("market.detail", {"slug": slug, "rank": rank})
    _warm("inventory.prefetch_prices")
    _warm("relics.prefetch_prices")
    _drain_market()

# ---- HTTP --------------------------------------------------------------------------------------
STATIC = os.path.abspath(ARGS.static)
QUIET = os.environ.get("WFH_BRIDGE_VERBOSE") != "1"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # noqa: D102
        if not QUIET:
            sys.stderr.write("[bridge] " + fmt % a + "\n")

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=rpc._json_default).encode())

    def do_POST(self):  # noqa: N802
        if self.path != "/rpc":
            return self._json({"error": "not found"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
            method, params = req.get("method", ""), req.get("params") or {}
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        if method not in rpc.methods():
            sys.stderr.write(f"[bridge] RPC_ERROR {method}: unknown method\n")
            return self._json({"error": f"unknown method {method}"})
        try:
            result = call(method, params)
            body = json.dumps({"result": result}, default=rpc._json_default).encode()
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            sys.stderr.write(f"[bridge] RPC_ERROR {method}: {msg}\n")
            return self._json({"error": msg})
        self._send(200, body)

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        if u.path == "/events":
            since = int((urllib.parse.parse_qs(u.query).get("since") or ["0"])[0])
            with _ev_lock:
                evs = [e for e in EVENTS if e[0] > since]
                seq = EVENTS[-1][0] if EVENTS else 0
            return self._json({"seq": seq, "events": evs})
        if u.path == "/clock":
            return self._json({"now_ms": now_ms(), "pinned": not ARGS.real_time})
        if u.path == "/health":
            return self._json({"ok": True, "failed_modules": FAILED, "methods": len(rpc.methods())})
        if u.path == "/netlog":
            return self._json(NET_LOG[-300:])
        rel = urllib.parse.unquote(u.path).lstrip("/") or "index.html"
        path = os.path.normpath(os.path.join(STATIC, rel))
        if not path.startswith(STATIC + os.sep) or not os.path.isfile(path):
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        self._send(200, _read(path), ctype)


server = ThreadingHTTPServer(("127.0.0.1", ARGS.port), Handler)
server.daemon_threads = True
print(f"READY port={server.server_address[1]} failed_modules={','.join(FAILED) or '-'}", flush=True)
try:
    server.serve_forever()
except KeyboardInterrupt:
    pass
