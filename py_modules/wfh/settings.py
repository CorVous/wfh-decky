"""Persistent settings stored as JSON in the plugin settings dir."""

import json
import os
import threading
from typing import Any

from . import log

_log = log.get("settings")

DEFAULTS: dict[str, Any] = {
    "overlays_enabled": True,
    "overlay_opacity": 1.0,
    "relic_rewards": {"enabled": True, "duration_s": 12, "show_ducats": True, "show_owned": True},
    "relic_planner": {"enabled": True, "duration_s": 20},
    "riven_scanner": {"enabled": True},
    "arbitration": {"enabled": True, "duration_s": 25, "notify_favorites": True, "favorite_nodes": []},
    "ee_log_path": "",
    "wfm_platform": "pc",
    "wfm_crossplay": True,
    "inventory_source": "memory",  # memory | file
    "inventory_file": "",
    "inventory_auto_sync": True,  # memory source: refresh from DE every inventory_refresh_min
    "inventory_refresh_min": 10,
    "inventory_mission_scan": True,  # read the resident inventory after each mission
    "debug_save_captures": False,
    "data_auto_refresh": True,  # weekly @wfcd item/relic refresh from GitHub
    "world": {"enrich": True},  # oracle.browse.wf + warframestat.us cycle/bounty enrichment
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data = dict(DEFAULTS)
        self._listeners: list = []
        try:
            with open(path, encoding="utf-8") as f:
                self._data = _merge(DEFAULTS, json.load(f))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            _log.warning("settings unreadable, using defaults: %s", e)

    def get(self, key: str, default: Any = None) -> Any:
        """Dotted keys reach into sections: get("relic_rewards.enabled")."""
        cur: Any = self._data
        for part in key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def all(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def update(self, patch: dict) -> dict:
        with self._lock:
            self._data = _merge(self._data, patch)
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self.path)
            data = json.loads(json.dumps(self._data))
        for fn in list(self._listeners):
            try:
                fn(data)
            except Exception:  # noqa: BLE001
                _log.exception("settings listener failed")
        return data

    def on_change(self, fn) -> None:
        self._listeners.append(fn)
