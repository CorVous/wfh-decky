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
    "relic_rewards": {"enabled": True, "duration_s": 12, "show_ducats": True, "show_owned": True,
                      # EE.log auto-trigger; EE.cfg UI scale (else ui_scale); blocks below|above the cards
                      "auto_trigger": True, "ui_scale_auto": True, "ui_scale": 0.99, "position": "below",
                      "toast_fallback": True, "prefetch_prices": True},
    "relic_planner": {"enabled": True, "duration_s": 20,
                      # auto_trigger: open on the EE.log picker lines; ocr_fallback: read the era
                      # label when EE.log has no fissure tag; squad_size: overlay squad before the
                      # Relics page pushes filters; filters: pushed RelicOverlayFilterPush or None
                      "auto_trigger": True, "ocr_fallback": True, "squad_size": 4, "show_rewards": False,
                      "max_height": 720, "prefetch_prices": True, "filters": None},
    "riven_scanner": {"enabled": True,
                      # auctions_shown: include auctions (not only direct sales) in similar listings;
                      # ui_scale_auto: EE.cfg interface scale (else ui_scale); canvas: "16:9" menu band
                      # or "full" frame for the crops; debug_dumps: scan crops under the log dir;
                      # compare_arrows: per-stat better/worse marks on the new-roll panel
                      "auctions_shown": True, "ui_scale_auto": True, "ui_scale": 0.99, "canvas": "16:9",
                      "debug_dumps": True, "compare_arrows": True},
    "arbitration": {"enabled": True, "duration_s": 25, "notify_favorites": True, "favorite_nodes": [],
                    # tracking: record runs from EE.log; enabled/duration_s: summary overlay;
                    # summary_position: top-center | top-right | top-left | left-middle; scale 0.75-1.25
                    "tracking": True, "summary_position": "top-center", "summary_scale": 1.0},
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
    # World page/QAM fissure filters (menus/world.py): mode all|normal|steel|railjack, shown tiers, QAM rows
    "world_ui": {"fissure_mode": "normal", "fissure_tiers": ["Lith", "Meso", "Neo", "Axi", "Requiem", "Omnia"],
                 "qam_limit": 5},
    # Dashboard (menus/dashboard.py): row limits 1-20, hidden widget ids
    "dashboard": {"fissure_limit": 5, "foundry_limit": 5, "hidden": []},
    # Market page (menus/market.py): recent items in the QAM (1-10), status hold minutes (0 = until changed)
    "market": {"qam_recent": 4, "status_hold_min": 0},
    # Inventory page (menus/inventory.py): value strip scope prime|tradable, per-unit floor 0|5|10|15,
    # hide blueprints already in the foundry, queue prime part prices after each inventory sync
    "inventory_ui": {"value_scope": "prime", "value_min_plat": 0, "hide_foundry_claims": True,
                     "auto_prefetch": True},
    # Foundry page (menus/foundry.py): pinned blueprint uniqueNames (max 60)
    "foundry": {"pinned": []},
    # Mastery page (menus/mastery.py): hide founder items, pinned "Planned" uniqueNames (max 40)
    "mastery": {"hide_founder": False, "pinned": []},
    # Syndicates page (menus/syndicates.py): rank-up goals [[tag, level], ...], QAM rows
    "syndicates": {"goals": [], "qam_limit": 6},
    # Missions page (menus/missions.py): record rewards from inventory diffs, toast after each mission
    "missions": {"enabled": True, "toast": False},
    # Stats page (menus/stats.py): chart resource ids, chart days, record trades from the EE.log dialog
    "stats": {"chart": ["plat", "ducats", "aya", "credits", "endo", "vitus", "kuva"], "days": 30,
              "trade_tracking": True},
    # Wiki page (menus/wiki.py): recent searches [{q, mode}] (max 8)
    "wiki": {"recent": []},
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
