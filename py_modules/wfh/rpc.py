"""JSON-lines RPC between the Decky backend (main.py) and this worker.

Requests:  {"id": 1, "method": "world.state", "params": {...}}
Responses: {"id": 1, "result": ...} or {"id": 1, "error": "message"}
Events:    {"event": "overlay.status", "data": ...}

Handlers register with @method("name"). A handler may be sync or async and
receives the params dict as keyword arguments.
"""

import asyncio
import inspect
import json
import sys
import threading
from typing import Any, Awaitable, Callable

from . import log

_log = log.get("rpc")
_methods: dict[str, Callable[..., Any]] = {}
_write_lock = threading.Lock()


def method(name: str):
    def deco(fn: Callable[..., Any]):
        if name in _methods:
            raise ValueError(f"duplicate RPC method {name}")
        _methods[name] = fn
        return fn

    return deco


def methods() -> list[str]:
    return sorted(_methods)


async def call_local(name: str, params: dict | None = None) -> Any:
    """Invoke a registered handler directly (used by tests and the CLI)."""
    fn = _methods[name]
    result = fn(**(params or {}))
    if inspect.isawaitable(result):
        result = await result
    return result


def _write(obj: dict) -> None:
    line = json.dumps(obj, separators=(",", ":"), default=_json_default)
    with _write_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _json_default(o: Any) -> Any:
    if hasattr(o, "to_json"):
        return o.to_json()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def emit(event: str, data: Any = None) -> None:
    """Send an event to the frontend. Safe to call from any thread."""
    try:
        _write({"event": event, "data": data})
    except Exception:  # noqa: BLE001 - never let a dead pipe kill a feature
        _log.exception("emit %s failed", event)


async def _handle(req: dict) -> None:
    rid = req.get("id")
    name = req.get("method", "")
    params = req.get("params") or {}
    fn = _methods.get(name)
    if fn is None:
        _write({"id": rid, "error": f"unknown method {name}"})
        return
    try:
        result = fn(**params)
        if inspect.isawaitable(result):
            result = await result
        _write({"id": rid, "result": result})
    except Exception as e:  # noqa: BLE001
        _log.exception("method %s failed", name)
        _write({"id": rid, "error": f"{type(e).__name__}: {e}"})


async def serve(on_stop: Callable[[], Awaitable[None]] | None = None) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    tasks: set[asyncio.Task] = set()
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _log.warning("bad request line: %r", line[:200])
            continue
        t = asyncio.create_task(_handle(req))
        tasks.add(t)
        t.add_done_callback(tasks.discard)
    if on_stop:
        await on_stop()
