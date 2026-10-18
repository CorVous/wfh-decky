"""Small HTTP client with a disk cache and per-host rate limiting (stdlib only)."""

import gzip
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from . import __version__, log

_log = log.get("http")

USER_AGENT = f"wfh-decky/{__version__} (+https://github.com/CorVous/wfh-decky)"

# Minimum seconds between requests per host. warframe.market asks for <= 3 req/s.
_HOST_INTERVAL = {"api.warframe.market": 0.35}
_host_last: dict[str, float] = {}
_host_lock = threading.Lock()

cache_dir: str = "/tmp/wfh-decky/cache"


def set_cache_dir(path: str) -> None:
    global cache_dir
    cache_dir = path
    os.makedirs(path, exist_ok=True)


def _throttle(host: str) -> None:
    gap = _HOST_INTERVAL.get(host, 0.0)
    if not gap and host not in _host_last:
        return
    with _host_lock:
        now = time.monotonic()
        wait = _host_last.get(host, 0) + gap - now
        _host_last[host] = max(now, _host_last.get(host, 0) + gap)
    if wait > 0:
        time.sleep(wait)


# 429 handling: the whole host is held back (every thread), not just the caller.
_RATE_LIMIT_BASE = 2.0      # first backoff without Retry-After; doubles per attempt
_RATE_LIMIT_MAX = 60.0      # cap on any host-wide hold
_RATE_LIMIT_INLINE = 10.0   # longer holds raise instead of blocking the calling thread


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    try:
        v = float((e.headers.get("Retry-After") or "").strip()) if e.headers is not None else None
    except (TypeError, ValueError):
        return None  # HTTP-date form or junk: use our own backoff
    return v if v is not None and v >= 0 else None


def _hold_host(host: str, delay: float) -> None:
    """Push the host's next request slot out so no thread sends for `delay` seconds."""
    gap = _HOST_INTERVAL.get(host, 0.0)
    with _host_lock:
        until = time.monotonic() + delay - gap  # _throttle adds gap back
        _host_last[host] = max(_host_last.get(host, 0), until)


def _cache_path(key: str) -> str:
    return os.path.join(cache_dir, hashlib.sha1(key.encode()).hexdigest()[:20])


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.body = body


def fetch(url: str, headers: dict | None = None, timeout: float = 20, data: bytes | None = None,
          method: str | None = None, retry_network: bool = True) -> bytes:
    """retry_network=False raises a transport error at once (callers with their own backoff)."""
    host = urlparse(url).netloc
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    hdrs.update(headers or {})
    for attempt in range(3):
        _throttle(host)
        req = urllib.request.Request(url, headers=hdrs, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return body
        except urllib.error.HTTPError as e:
            if e.code == 429:
                ra = _retry_after(e)
                delay = min(_RATE_LIMIT_MAX, ra if ra is not None else _RATE_LIMIT_BASE * 2 ** attempt)
                _hold_host(host, delay)  # every thread on this host waits, not just us
                _log.warning("HTTP 429 from %s; holding host for %.1fs", host, delay)
                if attempt < 2 and delay <= _RATE_LIMIT_INLINE:
                    continue  # _throttle sleeps until the hold ends
            raise HttpError(e.code, url, e.read().decode(errors="replace")[:500]) from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 2 and retry_network:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def get_json(url: str, max_age: float = 0, headers: dict | None = None, timeout: float = 20,
             stale_ok: bool = True, retry_network: bool = True) -> Any:
    """GET JSON, served from the disk cache when younger than max_age seconds.

    On network failure a stale cached copy is returned when stale_ok.
    """
    path = _cache_path(url + json.dumps(headers or {}, sort_keys=True))
    if max_age > 0:
        try:
            if time.time() - os.path.getmtime(path) < max_age:
                with open(path, "rb") as f:
                    return json.loads(f.read())
        except (OSError, ValueError):
            pass
    try:
        kw = {} if retry_network else {"retry_network": False}
        body = fetch(url, headers=headers, timeout=timeout, **kw)
        value = json.loads(body)
    except Exception:
        if stale_ok and os.path.exists(path):
            _log.warning("using stale cache for %s", url)
            with open(path, "rb") as f:
                return json.loads(f.read())
        raise
    if max_age > 0:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, path)
        except OSError:
            pass
    return value
