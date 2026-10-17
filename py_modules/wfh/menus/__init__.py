"""RPC handlers backing the Quick Access Menu panels and full-screen pages."""

import importlib

from .. import log
from ..app import App

_log = log.get("menus")

SUBMODULES: list[str] = []


def setup(app: App) -> None:
    for name in SUBMODULES:
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
            if hasattr(mod, "setup"):
                mod.setup(app)
        except Exception:  # noqa: BLE001
            _log.exception("menu module %s failed", name)
