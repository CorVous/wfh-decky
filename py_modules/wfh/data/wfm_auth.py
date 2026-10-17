"""warframe.market sign-in, my orders and online status (docs/port/data-sources.md §1.5-1.7).

Header auth only (§1.5, "Decky"): sign-in asks for the JWT in the ``Authorization``
response header (``auth_type: header``), every later call sends it back as
``Bearer`` (v2) / ``JWT`` (v1) with ``auth_type: header``. The cookie/CSRF mode and
its Cloudflare challenge window are not ported: the Python backend has no browser.

Persistence (§1.5.1): ``<settings_dir>/wfm.session`` holds
``{"token", "userName", "platform", "slug"}`` with mode 0600 (the worker is root);
the password is never stored. ``<settings_dir>/wfm.device-id`` keeps the random
device id WFM ties the session to. Token rotation: a 2xx answer carrying a new
``Authorization`` header replaces the stored token. A 401 signs the user out.

Status (§1.7): WFM has no REST setter; ``set_status`` opens a one-shot websocket
(``wss://ws.warframe.market/socket``, subprotocol ``wfm``), signs in with the JWT,
sends ``@wfm|cmd/status/set`` and closes, exactly like WFHelper's
setStatusViaWebSocket. The websocket client is a small stdlib RFC 6455
implementation (no ``websockets`` package in the runtime).

Every HTTP request goes through one injectable transport ``(method, url, headers,
body, timeout) -> HttpResponse`` so tests replay recorded answers; the default one
uses urllib without following redirects and the shared per-host throttle in net.py.
"""

from __future__ import annotations

import base64
import json
import os
import random
import socket
import ssl
import string
import struct
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote, urlparse

from .. import log, net

_log = log.get("wfm_auth")

API = "https://api.warframe.market"
SITE = "https://warframe.market"
WS_URL = "wss://ws.warframe.market/socket"
WS_ORIGIN = "https://warframe.market"
SESSION_FILE = "wfm.session"
DEVICE_FILE = "wfm.device-id"
REQUEST_TIMEOUT = 20
WS_TIMEOUT = 15
MAX_RESPONSE = 8 * 1024 * 1024
STATUSES = ("ingame", "online", "invisible")
HOLD_MINUTES = (0, 30, 60, 120, 240)  # config/shared/wfm.ts:8, 0 = until changed


class WfmError(RuntimeError):
    """A user-facing warframe.market failure (message is safe to show)."""

    def __init__(self, message: str, status: int | None = None, code: str = "WFM_ERROR"):
        super().__init__(message)
        self.status = status
        self.code = code


class WfmUnauthorized(WfmError):
    def __init__(self, message: str = "Signed out of warframe.market (session expired).", status: int | None = 401):
        super().__init__(message, status, "WFM_UNAUTHORIZED")


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)  # lower-case names
    body: bytes = b""

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


Transport = Callable[[str, str, dict, "bytes | None", float], HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # noqa: ANN002 - urllib signature
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> HttpResponse:
    """Default transport: urllib, redirects returned (not followed), net.py host throttle."""
    net._throttle(urlparse(url).netloc)
    hdrs = {"User-Agent": net.USER_AGENT}
    hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            data = resp.read(MAX_RESPONSE + 1)
            return HttpResponse(resp.status, _lower(resp.headers.items()), data[:MAX_RESPONSE])
    except urllib.error.HTTPError as e:
        try:
            data = e.read(MAX_RESPONSE)
        except Exception:  # noqa: BLE001
            data = b""
        return HttpResponse(e.code, _lower((e.headers or {}).items()), data)


def _lower(items) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in items:
        k = k.lower()
        out[k] = f"{out[k]}, {v}" if k in out and k == "set-cookie" else v
    return out


# ---- pure helpers ----------------------------------------------------------------------------

def unwrap(obj: Any) -> Any:
    if isinstance(obj, dict):
        if "data" in obj:
            return obj["data"]
        if "payload" in obj:
            return obj["payload"]
    return obj


def error_detail(body: Any) -> str:
    """wfmClient.ts:567-601: error string, error.message, message, else flattened fields."""
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, str) and err:
        return err
    if isinstance(err, dict) and isinstance(err.get("message"), str):
        return err["message"]
    if isinstance(body.get("message"), str):
        return body["message"]
    parts: list[str] = []

    def walk(prefix: str, v: Any) -> None:
        if len(parts) >= 4:
            return
        if isinstance(v, dict):
            for k, sub in v.items():
                walk(k if prefix in ("", "inputs", "request") else f"{prefix}.{k}", sub)
        elif isinstance(v, list):
            for sub in v:
                walk(prefix, sub)
        elif v not in (None, ""):
            parts.append(f"{prefix}: {v}" if prefix else str(v))

    if isinstance(err, dict):
        walk("", err)
    return "; ".join(parts[:4])


def strip_scheme(token: str | None) -> str | None:
    if not token:
        return None
    t = token.strip()
    for p in ("Bearer ", "JWT ", "bearer ", "jwt "):
        if t.startswith(p):
            t = t[len(p):].strip()
    return t or None


def jwt_claims(token: str) -> dict:
    """Decode a JWT payload without verification (base64url)."""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        v = json.loads(base64.urlsafe_b64decode(seg))
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def extract_token(resp: HttpResponse) -> str | None:
    """wfmSession.ts: Authorization header, else Set-Cookie JWT=, else body token."""
    tok = strip_scheme(resp.header("authorization"))
    if tok:
        return tok
    cookie = resp.header("set-cookie") or ""
    for part in cookie.replace(",", ";").split(";"):
        part = part.strip()
        if part.startswith("JWT="):
            return part[4:] or None
    body = resp.json()
    if isinstance(body, dict):
        p = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        d = body.get("data") if isinstance(body.get("data"), dict) else {}
        for v in (p.get("token"), d.get("token"), body.get("token")):
            if isinstance(v, str) and v:
                return v
    return None


def normalize_slug(name: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_" else "_" for c in (name or "").strip().lower())
    return out.strip("_")


def normalize_my_order(raw: dict, item: Any = None) -> dict:
    """wfmOrders.ts normalise(): one of my orders, item fields from the catalog (by itemId)."""
    q = raw.get("quantity")
    qty = q if isinstance(q, int) and not isinstance(q, bool) and q > 0 else 1
    try:
        per = int(raw.get("perTrade", raw.get("per_trade")) or 1)
    except (TypeError, ValueError):
        per = 1
    per = min(max(per, 1), qty)
    rank = raw.get("rank", raw.get("mod_rank"))
    rank = rank if isinstance(rank, int) and not isinstance(rank, bool) else None
    sub = raw.get("subtype")
    item_raw = raw.get("item") if isinstance(raw.get("item"), dict) else {}
    item_id = raw.get("itemId") or item_raw.get("id")
    name = slug = thumb = game_ref = None
    max_rank = None
    if item is not None:
        name, slug, thumb = getattr(item, "name", None), getattr(item, "slug", None), getattr(item, "thumb", None)
        game_ref, max_rank = getattr(item, "game_ref", None) or None, getattr(item, "max_rank", None)
    elif item_raw:
        it = item_raw
        en = it.get("en") or (it.get("i18n") or {}).get("en") or {}
        name = en.get("item_name") or en.get("name") or it.get("item_name") or it.get("name")
        slug = it.get("url_name") or it.get("slug")
    try:
        plat = int(round(float(raw.get("platinum") or 0)))
    except (TypeError, ValueError):
        plat = 0
    return {
        "id": str(raw.get("id") or ""),
        "type": raw.get("type") or raw.get("order_type") or "sell",
        "platinum": plat,
        "quantity": qty,
        "perTrade": per,
        "visible": raw.get("visible", True) is not False,
        "rank": rank,
        "subtype": sub if isinstance(sub, str) and sub else None,
        "itemId": item_id,
        "name": name or (slug.replace("_", " ").title() if slug else "(unknown item)"),
        "slug": slug,
        "thumb": thumb,
        "gameRef": game_ref,
        "maxRank": max_rank,
        "updatedAt": raw.get("updatedAt") or raw.get("last_update"),
    }


def sort_orders(rows: list[dict]) -> list[dict]:
    """Sell first, then visible first, then name."""
    return sorted(rows, key=lambda o: (o["type"] != "sell", not o["visible"], (o["name"] or "").lower(), o["id"]))


# ---- minimal websocket client (RFC 6455, client side) ---------------------------------------

class WsError(WfmError):
    def __init__(self, message: str):
        super().__init__(message, None, "WFM_WS_ERROR")


def ws_frame(payload: bytes, opcode: int = 1, mask: bytes | None = None) -> bytes:
    """One final, masked client frame."""
    mask = mask if mask is not None else os.urandom(4)
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class WsConn:
    """Blocking client over an already-upgraded socket (anything with sendall/recv)."""

    def __init__(self, sock, buffered: bytes = b""):
        self.sock = sock
        self.buf = bytearray(buffered)

    def _need(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WsError("websocket closed")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def send_text(self, text: str) -> None:
        self.sock.sendall(ws_frame(text.encode()))

    def close(self) -> None:
        try:
            self.sock.sendall(ws_frame(struct.pack(">H", 1000), opcode=8))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def recv_text(self) -> str:
        parts: list[bytes] = []
        while True:
            b0, b1 = self._need(2)
            fin, op = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._need(8))[0]
            if n > 1024 * 1024:
                raise WsError("websocket frame too large")
            mask = self._need(4) if b1 & 0x80 else None
            data = self._need(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 8:
                raise WsError("websocket closed by server")
            if op == 9:
                self.sock.sendall(ws_frame(data, opcode=10))
                continue
            if op == 10:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8", "replace")


def _ws_id() -> str:
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(11))


def ws_message(route: str, payload: dict) -> str:
    return json.dumps({"route": route, "payload": payload, "id": _ws_id()}, separators=(",", ":"))


def ws_connect(url: str = WS_URL, timeout: float = WS_TIMEOUT) -> WsConn:
    """TLS + HTTP upgrade (honours HTTPS_PROXY with CONNECT)."""
    u = urlparse(url)
    host, port = u.hostname or "", u.port or 443
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        p = urlparse(proxy)
        raw = socket.create_connection((p.hostname, p.port or 80), timeout=timeout)
        raw.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = raw.recv(4096)
            if not chunk:
                raise WsError("proxy closed the connection")
            resp += chunk
        if b" 200" not in resp.split(b"\r\n", 1)[0]:
            raise WsError("proxy refused the websocket connection")
    else:
        raw = socket.create_connection((host, port), timeout=timeout)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    sock.settimeout(timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {u.path or '/'} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: wfm\r\n"
           f"Origin: {WS_ORIGIN}\r\nUser-Agent: {net.USER_AGENT}\r\n\r\n")
    sock.sendall(req.encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = sock.recv(4096)
        if not chunk:
            raise WsError("websocket handshake failed")
        head += chunk
        if len(head) > 65536:
            raise WsError("websocket handshake too large")
    head, rest = head.split(b"\r\n\r\n", 1)
    status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 101" not in status_line:
        raise WsError(f"websocket handshake refused: {status_line}")
    return WsConn(sock, rest)


def ws_set_status(conn: WsConn, token: str, status: str, duration: int | None) -> str | None:
    """wfmWebSocket.ts setStatusViaWebSocket over an open connection; returns statusUntil."""
    try:
        conn.send_text(ws_message("@wfm|cmd/auth/signIn", {"token": token}))
        sent_status = False
        deadline = time.monotonic() + WS_TIMEOUT  # one overall timer, like WFHelper's 15 s
        for _ in range(50):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WsError("timeout waiting for warframe.market")
            settimeout = getattr(conn.sock, "settimeout", None)
            if settimeout is not None:
                settimeout(remaining)
            try:
                msg = json.loads(conn.recv_text())
            except ValueError:  # parseWfmWsMessage skips frames that are not JSON
                continue
            route = str(msg.get("route") or "") if isinstance(msg, dict) else ""
            if route.endswith(":error"):
                raise WsError(f"warframe.market refused ({route.split('|')[-1]})")
            if "auth/signIn:ok" in route and not sent_status:
                conn.send_text(ws_message("@wfm|cmd/status/set", {"status": status, "duration": duration}))
                sent_status = True
            elif "status/set:ok" in route:
                until = (msg.get("payload") or {}).get("statusUntil")
                return until if isinstance(until, str) else None
        raise WsError("no answer to the status change")
    finally:
        conn.close()


# ---- the session -----------------------------------------------------------------------------

class WfmAuth:
    def __init__(self, settings_dir: str | None, transport: Transport | None = None,
                 platform: Callable[[], str] | None = None,
                 ws_factory: Callable[[], WsConn] | None = None):
        self.settings_dir = settings_dir
        self.transport = transport or urllib_transport
        self.ws_factory = ws_factory or ws_connect
        self._platform = platform or (lambda: "pc")
        self._lock = threading.RLock()
        self._order_locks: set[str] = set()
        self.token: str | None = None
        self.user_name: str | None = None
        self.platform: str = "pc"
        self.slug: str | None = None
        self.user_id: str | None = None
        self.status: str | None = None
        self.status_until: str | None = None
        self.persisted = False
        self._load()

    # persistence
    def _path(self, name: str) -> str | None:
        return os.path.join(self.settings_dir, name) if self.settings_dir else None

    def _load(self) -> None:
        p = self._path(SESSION_FILE)
        if not p:
            return
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            _log.warning("wfm.session unreadable: %s", e)
            return
        if isinstance(d, dict) and isinstance(d.get("token"), str) and d["token"]:
            self.token = d["token"]
            self.user_name = d.get("userName") or None
            self.platform = d.get("platform") or "pc"
            self.slug = d.get("slug") or None
            self.user_id = d.get("userId") or None
            self.persisted = True

    def _save(self) -> None:
        p = self._path(SESSION_FILE)
        if not p:
            return
        data = json.dumps({"token": self.token, "userName": self.user_name, "platform": self.platform,
                           "slug": self.slug, "userId": self.user_id}).encode()
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, p)
            self.persisted = True
        except OSError as e:
            _log.warning("could not store the warframe.market session: %s", e)
            self.persisted = False

    def device_id(self) -> str:
        p = self._path(DEVICE_FILE)
        if p:
            try:
                with open(p, encoding="utf-8") as f:
                    v = f.read().strip()
                if v:
                    return v
            except OSError:
                pass
        v = str(uuid.uuid4())
        if p:
            try:
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(v)
            except OSError:
                return "wfhelper"
        return v

    @property
    def signed_in(self) -> bool:
        return bool(self.token)

    def state(self) -> dict:
        return {"signedIn": self.signed_in, "userName": self.user_name, "platform": self.platform,
                "slug": self.slug, "status": self.status, "statusUntil": self.status_until,
                "persisted": self.persisted,
                "profileUrl": f"{SITE}/profile/{quote(self.slug)}" if self.slug else None}

    # sign-in / out
    def sign_in(self, email: str, password: str) -> dict:
        email = (email or "").strip()
        if not email or "@" not in email or not password:
            raise WfmError("Enter the e-mail and password of your warframe.market account.", 400, "WFM_BAD_INPUT")
        body = json.dumps({"email": email, "password": password, "device_id": self.device_id(),
                           "auth_type": "header"}).encode()
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "Platform": self._platform(), "Language": "en", "Authorization": "JWT", "auth_type": "header"}
        try:
            resp = self.transport("POST", f"{API}/v1/auth/signin", headers, body, REQUEST_TIMEOUT)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise WfmError(f"warframe.market is unreachable ({type(e).__name__}).", None, "WFM_OFFLINE") from None
        if resp.status == 429:
            raise WfmError(_rate_text(resp), 429, "WFM_RATE_LIMITED")
        if not 200 <= resp.status < 300:
            detail = error_detail(resp.json())
            if resp.status in (400, 401) or "app.account." in detail:
                raise WfmError(_signin_text(detail), resp.status, "WFM_SIGNIN_REJECTED")
            if _is_challenge(resp):
                raise WfmError("warframe.market asked for a browser check (Cloudflare); sign-in from the Deck "
                               "is blocked right now. Try again later.", resp.status, "WFM_CHALLENGE")
            raise WfmError(f"Sign-in failed (HTTP {resp.status}{': ' + detail if detail else ''}).", resp.status)
        token = extract_token(resp)
        if not token:
            raise WfmError("warframe.market did not return a session token.", resp.status, "WFM_NO_TOKEN")
        b = resp.json()
        b = b if isinstance(b, dict) else {}
        user: Any = None
        for holder in (b.get("payload"), b.get("data"), b):
            if isinstance(holder, dict) and isinstance(holder.get("user"), dict):
                user = holder["user"]
                break
        user = user or {}
        with self._lock:
            self.token = token
            self.user_name = (user.get("ingame_name") or user.get("ingameName") or user.get("name")
                              or email.split("@", 1)[0])
            self.platform = user.get("platform") or "pc"
            self.slug = user.get("slug") or None
            self.user_id = user.get("id") or None
            self.status = None
            self._save()
        try:
            self.refresh_me()
        except WfmUnauthorized:
            raise
        except WfmError as e:
            _log.info("profile lookup after sign-in failed: %s", e)
        return self.state()

    def sign_out(self) -> dict:
        with self._lock:
            self.token = self.user_name = self.slug = self.user_id = None
            self.status = self.status_until = None
            self.persisted = False
            p = self._path(SESSION_FILE)
            if p:
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    _log.warning("could not remove wfm.session: %s", e)
        return self.state()

    # authenticated requests
    def request(self, method: str, path: str, body: Any = None, v: int = 2) -> Any:
        """One authenticated call; returns the unwrapped data. 401 signs out."""
        token = self.token
        if not token:
            raise WfmUnauthorized("Sign in to warframe.market first.", None)
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "Platform": self.platform or self._platform(), "Language": "en", "auth_type": "header",
                   "Authorization": f"Bearer {token}" if v == 2 else f"JWT {token}"}
        if v == 2:
            headers["Crossplay"] = "true"
        data = json.dumps(body).encode() if body is not None else None
        try:
            resp = self.transport(method, f"{API}/v{v}{path}", headers, data, REQUEST_TIMEOUT)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise WfmError(f"warframe.market is unreachable ({type(e).__name__}).", None, "WFM_OFFLINE") from None
        if resp.status == 401:
            self.sign_out()
            raise WfmUnauthorized()
        if resp.status == 429:
            raise WfmError(_rate_text(resp), 429, "WFM_RATE_LIMITED")
        if not 200 <= resp.status < 300:
            if _is_challenge(resp):
                raise WfmError("warframe.market is asking for a browser check (Cloudflare). Try again later.",
                               resp.status, "WFM_CHALLENGE")
            detail = error_detail(resp.json())
            raise WfmError(f"warframe.market answered HTTP {resp.status}{': ' + detail if detail else ''}.",
                           resp.status, "WFM_HTTP")
        rotated = strip_scheme(resp.header("authorization"))
        if rotated and rotated != token and rotated.count(".") == 2:
            with self._lock:
                self.token = rotated
                self._save()
        return unwrap(resp.json())

    def refresh_me(self) -> dict:
        """/v2/me: authoritative slug and name; then the public status from /v2/user/{slug}."""
        me = self.request("GET", "/me")
        if isinstance(me, dict):
            with self._lock:
                self.slug = me.get("slug") or self.slug or normalize_slug(self.user_name or "")
                self.user_name = me.get("ingameName") or me.get("ingame_name") or self.user_name
                self.user_id = me.get("id") or self.user_id
                if isinstance(me.get("platform"), str):
                    self.platform = me["platform"]
                self._save()
        self.refresh_status()
        return self.state()

    def refresh_status(self) -> str | None:
        """wfmSession.ts:348-364: status from the public profile; offline means invisible."""
        if not self.slug:
            return self.status
        try:
            resp = self.transport("GET", f"{API}/v2/user/{quote(self.slug)}",
                                  {"Accept": "application/json", "Platform": self.platform, "Language": "en"},
                                  None, REQUEST_TIMEOUT)
        except (urllib.error.URLError, TimeoutError, OSError):
            return self.status
        if resp.status == 200:
            d = unwrap(resp.json())
            st = str((d or {}).get("status") or "").lower() if isinstance(d, dict) else ""
            self.status = "invisible" if st == "offline" else (st if st in STATUSES else self.status)
        return self.status

    def set_status(self, status: str, hold_min: int = 0) -> dict:
        if status not in STATUSES:
            raise WfmError(f"Unknown status {status!r}.", 400, "WFM_BAD_INPUT")
        if not self.token:
            raise WfmUnauthorized("Sign in to warframe.market first.", None)
        duration = int(hold_min) * 60 if hold_min and int(hold_min) > 0 else None
        try:
            conn = self.ws_factory()
        except WfmError:
            raise
        except (OSError, ssl.SSLError) as e:
            raise WfmError(f"Could not reach the warframe.market websocket ({type(e).__name__}).", None,
                           "WFM_OFFLINE") from None
        try:
            until = ws_set_status(conn, self.token, status, duration)
        except (OSError, ValueError) as e:
            raise WfmError(f"Status change failed ({type(e).__name__}).", None, "WFM_WS_ERROR") from None
        self.status, self.status_until = status, until
        return self.state()

    # orders
    def my_orders(self, item_by_id: Callable[[str], Any] | None = None) -> list[dict]:
        if not self.token:
            raise WfmUnauthorized("Sign in to warframe.market first.", None)
        data = self.request("GET", "/orders/my")
        rows = data if isinstance(data, list) else (data.get("orders") if isinstance(data, dict) else None) or []
        out = []
        for r in rows:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            item = None
            if item_by_id and r.get("itemId"):
                try:
                    item = item_by_id(str(r["itemId"]))
                except Exception:  # noqa: BLE001 - catalog offline: keep the row
                    item = None
            out.append(normalize_my_order(r, item))
        return sort_orders(out)

    def _locked(self, order_id: str):
        auth = self

        class _L:
            def __enter__(self_inner):
                with auth._lock:
                    if order_id in auth._order_locks:
                        raise WfmError("That order is still being updated.", 409, "WFM_ORDER_BUSY")
                    auth._order_locks.add(order_id)

            def __exit__(self_inner, *exc):
                with auth._lock:
                    auth._order_locks.discard(order_id)

        return _L()

    @staticmethod
    def _check_id(order_id: str) -> str:
        oid = str(order_id or "").strip()
        if not oid or not all(c.isalnum() or c in "-_" for c in oid):
            raise WfmError("Bad order id.", 400, "WFM_BAD_INPUT")
        return oid

    def close_order(self, order_id: str, quantity: int = 1) -> None:
        oid = self._check_id(order_id)
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 1:
            raise WfmError("Quantity must be a positive whole number.", 400, "WFM_BAD_INPUT")
        with self._locked(oid):
            self.request("POST", f"/order/{oid}/close", {"quantity": quantity})

    def delete_order(self, order_id: str) -> None:
        oid = self._check_id(order_id)
        with self._locked(oid):
            self.request("DELETE", f"/order/{oid}")

    def update_order(self, order_id: str, **fields: Any) -> Any:
        oid = self._check_id(order_id)
        patch = {k: v for k, v in fields.items() if k in ("platinum", "quantity", "visible", "rank", "subtype")
                 and v is not None}
        for k in ("platinum", "quantity"):
            if k in patch and (not isinstance(patch[k], int) or isinstance(patch[k], bool) or patch[k] < 1):
                raise WfmError(f"{k.title()} must be a positive whole number.", 400, "WFM_BAD_INPUT")
        if not patch:
            raise WfmError("Nothing to change.", 400, "WFM_BAD_INPUT")
        with self._locked(oid):
            return self.request("PATCH", f"/order/{oid}", patch)


def _rate_text(resp: HttpResponse) -> str:
    try:
        wait = max(1, int(float(resp.header("retry-after") or 30)))
    except ValueError:
        wait = 30
    return f"Warframe.market rate limit hit. Please wait {wait}s before trying again."


def _signin_text(detail: str) -> str:
    low = detail.lower()
    if "email_not_exist" in low or ("email" in low and "exist" in low):
        return "No warframe.market account uses that e-mail."
    if "password" in low:
        return "Wrong password."
    if "app.account." in detail:
        return f"warframe.market refused the sign-in ({detail})."
    return f"Sign-in rejected{': ' + detail if detail else ''}."


def _is_challenge(resp: HttpResponse) -> bool:
    if (resp.header("cf-mitigated") or "").lower() == "challenge":
        return True
    if resp.status == 403:
        low = resp.body[:4096].decode("utf-8", "replace").lower()
        return any(k in low for k in ("just a moment", "cf-chl", "challenge-platform"))
    return False
