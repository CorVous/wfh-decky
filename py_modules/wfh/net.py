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
    gap = _HOST_INTERVAL.get(host)
    if not gap:
        return
    with _host_lock:
        now = time.monotonic()
        wait = _host_last.get(host, 0) + gap - now
        _host_last[host] = max(now, _host_last.get(host, 0) + gap)
    if wait > 0:
        time.sleep(wait)


def _cache_path(key: str) -> str:
    return os.path.join(cache_dir, hashlib.sha1(key.encode()).hexdigest()[:20])


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.body = body


def fetch(url: str, headers: dict | None = None, timeout: float = 20, data: bytes | None = None,
          method: str | None = None) -> bytes:
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
            if e.code == 429 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise HttpError(e.code, url, e.read().decode(errors="replace")[:500]) from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def get_json(url: str, max_age: float = 0, headers: dict | None = None, timeout: float = 20,
             stale_ok: bool = True) -> Any:
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
        body = fetch(url, headers=headers, timeout=timeout)
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
