"""Shared worker context handed to every feature's setup(app)."""

import asyncio
import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from . import log, net, rpc
from .eelog import EELogTailer
from .overlay.manager import OverlayManager
from .settings import Settings

_log = log.get("app")

# Feature and menu modules; each exposes setup(app) and registers RPC methods.
MODULES = [
    "wfh.features.core",
    "wfh.data.items",
    "wfh.data.market",
    "wfh.data.worldstate",
    "wfh.data.relics",
    "wfh.inventory",
    "wfh.features.relic_rewards",
    "wfh.features.relic_planner",
    "wfh.features.riven_scanner",
    "wfh.features.arbitration",
    "wfh.menus",
]


@dataclass
class App:
    plugin_dir: str
    settings_dir: str
    runtime_dir: str
    log_dir: str
    settings: Settings
    overlays: OverlayManager
    eelog: EELogTailer
    loop: asyncio.AbstractEventLoop | None = None
    pool: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=6, thread_name_prefix="wfh"))
    services: dict[str, Any] = field(default_factory=dict)

    @property
    def assets_dir(self) -> str:
        return os.path.join(os.path.dirname(__file__), "assets")

    @property
    def data_dir(self) -> str:
        """Bundled game data built by scripts/fetch-data.sh."""
        return os.path.join(self.plugin_dir, "data")

    @property
    def models_dir(self) -> str:
        return os.path.join(self.plugin_dir, "models")

    def emit(self, event: str, data: Any = None) -> None:
        rpc.emit(event, data)

    def toast(self, title: str, body: str = "") -> None:
        """Steam toast through the frontend (works even when overlays cannot draw)."""
        rpc.emit("toast", {"title": title, "body": body})

    async def run_blocking(self, fn: Callable, *args) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.pool, fn, *args)

    def submit(self, fn: Callable, *args) -> None:
        """Run fn in the worker pool from any thread, logging failures."""
        def wrapped():
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                _log.exception("background task %s failed", getattr(fn, "__qualname__", fn))
        self.pool.submit(wrapped)


_app: App | None = None


def get_app() -> App:
    if _app is None:
        raise RuntimeError("app not initialised")
    return _app


def create(plugin_dir: str, settings_dir: str, runtime_dir: str, log_dir: str, headless: bool = False) -> App:
    global _app
    settings = Settings(os.path.join(settings_dir, "settings.json"))
    net.set_cache_dir(os.path.join(runtime_dir, "cache"))
    overlays = OverlayManager(headless=headless)
    overlays.enabled = bool(settings.get("overlays_enabled", True))
    overlays.opacity = float(settings.get("overlay_opacity", 1.0))
    eelog = EELogTailer(lambda: settings.get("ee_log_path", ""))
    _app = App(plugin_dir, settings_dir, runtime_dir, log_dir, settings, overlays, eelog)
    return _app


def load_modules(app: App, modules: list[str] | None = None) -> list[str]:
    failed = []
    for name in modules or MODULES:
        try:
            mod = importlib.import_module(name)
            if hasattr(mod, "setup"):
                sub_failed = mod.setup(app)
                if isinstance(sub_failed, list):  # package loaders report their own broken submodules
                    failed.extend(sub_failed)
        except Exception:  # noqa: BLE001 - one broken feature must not take the rest down
            _log.exception("module %s failed to load", name)
            failed.append(name)
    return failed
