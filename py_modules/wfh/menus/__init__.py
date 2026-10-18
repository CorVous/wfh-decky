"""RPC handlers backing the Quick Access Menu panels and full-screen pages.

Every module in this package with a setup(app) is loaded automatically.
"""

import importlib
import pkgutil

from .. import log
from ..app import App

_log = log.get("menus")


def setup(app: App) -> list[str]:
    """Load every page module; return the dotted names of those that failed."""
    failed: list[str] = []
    for info in pkgutil.iter_modules(__path__):
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
            if hasattr(mod, "setup"):
                mod.setup(app)
        except Exception:  # noqa: BLE001 - one broken page must not take the rest down
            _log.exception("menu module %s failed", info.name)
            failed.append(f"{__name__}.{info.name}")
    return failed
