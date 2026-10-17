"""inventory.php with the auth query read from game memory (apiHelperRunner.ts
fetchInventoryWithAuthz): GET https://<host>/api/inventory.php?accountId=..&nonce=..
trying api.warframe.com first, then mobile.warframe.com."""

from __future__ import annotations

from typing import Callable

from .. import log, net
from .memory import AUTHZ_RE

_log = log.get("inventory.api")

HOSTS = ("api.warframe.com", "mobile.warframe.com")
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,*/*"}
NETWORK_TIMEOUT_S = 30
# Real inventories are ~0.9 MiB and only grow; keep this well above them.
MAX_INVENTORY_RESPONSE_BYTES = 16 * 1024 * 1024


class InventoryFetchError(RuntimeError):
    pass


def inventory_url(host: str, authz: str) -> str:
    if not AUTHZ_RE.match(authz):
        raise ValueError("malformed auth query")
    return f"https://{host}/api/inventory.php{authz}"


def fetch_inventory(authz: str, fetch: Callable[..., bytes] = net.fetch) -> tuple[bytes, str]:
    """(body, host) of the first host answering 200 with a body; raises
    InventoryFetchError naming the last failure otherwise."""
    last: Exception | None = None
    for host in HOSTS:
        url = inventory_url(host, authz)
        try:
            body = fetch(url, headers=dict(HEADERS), timeout=NETWORK_TIMEOUT_S)
        except net.HttpError as e:
            last = InventoryFetchError(f"{host} returned HTTP {e.status} ({len(e.body)} bytes)")
            _log.warning("%s", last)
            continue
        except Exception as e:  # noqa: BLE001 - URLError, timeouts, TLS
            last = e
            _log.warning("%s request error: %s", host, e)
            continue
        if not body:
            last = InventoryFetchError(f"{host} returned an empty body")
            _log.warning("%s", last)
            continue
        if len(body) > MAX_INVENTORY_RESPONSE_BYTES:
            last = InventoryFetchError(f"{host} response exceeded {MAX_INVENTORY_RESPONSE_BYTES} bytes")
            continue
        _log.info("inventory fetched from %s (%d bytes)", host, len(body))
        return body, host
    if isinstance(last, InventoryFetchError):
        raise last
    raise InventoryFetchError(str(last) if last else "all inventory hosts failed") from last
