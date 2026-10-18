"""Settings page view-model and diagnostics (docs/port/menus.md §16-17).

The page is generic: SCHEMA lists every user-facing setting (overlay toggles, durations,
opacity, interface scale, EE.log override, inventory source/file, WFM platform, tracking,
debug). settings.view returns it with current values; settings.set validates one key
against its field (clamps numbers, checks options) before writing. A field with `also`
writes the same value to several keys (one "interface scale" for rewards and rivens).

The Steam launch option itself is written by the frontend (SteamClient.Apps.
SetAppLaunchOptions) after a ConfirmModal; compose_launch_options keeps whatever the user
already had there.

RPCs
  settings.view()                         {sections: [{id, title, fields: [...value]}]}
  settings.set(key, value)                validated write; returns the field with its new value
  settings.reset(section)                 restore DEFAULTS for that section's fields
  settings.check_path(kind, path)         ee_log | inventory_file: exists/readable/size/mtime
  settings.diagnostics()                  core/eelog/inventory/data status in one call
  settings.proton_plan(current?)          {current, proposed, alreadySet, hint}
"""

from __future__ import annotations

import os
import re
import shlex
import time
from typing import Any

from .. import log, rpc
from ..settings import DEFAULTS

_log = log.get("menus.settings")

LAUNCH_OPTION_VAR = "PROTON_LOG=1"
WARFRAME_APPID = 230410


def _toggle(key: str, label: str, desc: str = "", also: list[str] | None = None) -> dict:
    return {"key": key, "label": label, "kind": "toggle", "description": desc, "also": also or []}


def _slider(key: str, label: str, lo: float, hi: float, step: float, unit: str = "", desc: str = "",
            also: list[str] | None = None) -> dict:
    return {"key": key, "label": label, "kind": "slider", "min": lo, "max": hi, "step": step, "unit": unit,
            "description": desc, "also": also or []}


def _choice(key: str, label: str, options: list[tuple[Any, str]], desc: str = "") -> dict:
    return {"key": key, "label": label, "kind": "dropdown", "description": desc, "also": [],
            "options": [{"value": v, "label": lbl} for v, lbl in options]}


def _text(key: str, label: str, desc: str = "", check: str | None = None) -> dict:
    return {"key": key, "label": label, "kind": "text", "description": desc, "also": [], "check": check}


SCHEMA: list[dict] = [
    {"id": "overlays", "title": "Overlays", "fields": [
        _toggle("overlays_enabled", "Overlays", "Master switch for everything drawn over the game"),
        _slider("overlay_opacity", "Overlay opacity", 0.3, 1.0, 0.05, "", "Applies to every overlay"),
    ]},
    {"id": "relic_rewards", "title": "Relic reward prices", "fields": [
        _toggle("relic_rewards.enabled", "Show reward overlay"),
        _toggle("relic_rewards.auto_trigger", "Scan automatically", "Start the scan from EE.log when the reward screen opens"),
        _slider("relic_rewards.duration_s", "Show for", 4, 30, 1, "s"),
        _choice("relic_rewards.position", "Position", [("below", "Below the cards"), ("above", "Above the cards")]),
        _toggle("relic_rewards.show_ducats", "Show ducats"),
        _toggle("relic_rewards.show_owned", "Show owned counts"),
        _toggle("relic_rewards.prioritize_mastery", "Prioritize mastery", "Best pick goes to parts of gear you have never crafted or mastered, then platinum"),
        _toggle("relic_rewards.toast_fallback", "Toast when overlays cannot draw"),
        _toggle("relic_rewards.prefetch_prices", "Prefetch prices", "Warm prices of relic contents in the background"),
    ]},
    {"id": "relic_planner", "title": "Relic recommendations", "fields": [
        _toggle("relic_planner.enabled", "Show relic picker overlay"),
        _toggle("relic_planner.auto_trigger", "Open automatically", "When the relic selection screen opens"),
        _toggle("relic_planner.prioritize_mastery", "Prioritize mastery", "Relics with a part of gear you have never crafted or mastered rank first"),
        _toggle("relic_planner.ocr_fallback", "Read the era from the screen", "When EE.log does not name the fissure tier"),
        _slider("relic_planner.duration_s", "Show for", 5, 60, 1, "s"),
        _slider("relic_planner.squad_size", "Squad size", 1, 4, 1, "", "Used until the Relics page pushes filters"),
        _toggle("relic_planner.show_rewards", "List rewards per relic"),
    ]},
    {"id": "riven_scanner", "title": "Riven scanner", "fields": [
        _toggle("riven_scanner.enabled", "Show riven overlay"),
        _toggle("riven_scanner.compare_arrows", "Compare new roll", "Better/worse marks per stat"),
        _toggle("riven_scanner.auctions_shown", "Include auctions in similar listings"),
        _choice("riven_scanner.canvas", "Capture area", [("16:9", "16:9 menu band"), ("full", "Full frame")]),
    ]},
    {"id": "arbitration", "title": "Arbitrations", "fields": [
        _toggle("arbitration.tracking", "Track arbitration runs", "Parse runs from EE.log"),
        _toggle("arbitration.enabled", "Show end-of-mission summary"),
        _slider("arbitration.duration_s", "Show for", 5, 60, 1, "s"),
        _choice("arbitration.summary_position", "Summary position", [
            ("top-center", "Top center"), ("top-right", "Top right"), ("top-left", "Top left"),
            ("left-middle", "Left middle")]),
        _slider("arbitration.summary_scale", "Summary size", 0.75, 1.25, 0.05, "x"),
        _toggle("arbitration.notify_favorites", "Notify for favourite nodes"),
    ]},
    {"id": "ui_scale", "title": "Interface scale", "fields": [
        _toggle("relic_rewards.ui_scale_auto", "Read from EE.cfg",
                "Uses Warframe's saved interface scale (Options > Interface > HUD scale)",
                also=["riven_scanner.ui_scale_auto"]),
        _slider("relic_rewards.ui_scale", "Manual scale", 0.5, 1.0, 0.01, "", "Used when EE.cfg has no custom scale",
                also=["riven_scanner.ui_scale"]),
    ]},
    {"id": "missions", "title": "Tracking", "fields": [
        _toggle("missions.enabled", "Record mission rewards", "From inventory reads after each mission"),
        _toggle("missions.toast", "Toast after each mission"),
        _toggle("stats.trade_tracking", "Record trades", "From the in-game trade dialog in EE.log"),
    ]},
    {"id": "inventory", "title": "Inventory", "fields": [
        _choice("inventory_source", "Inventory source", [
            ("memory", "Game (automatic)"), ("file", "JSON file"), ("none", "None")]),
        _text("inventory_file", "Inventory file", "Path to an exported inventory JSON (file source)", check="inventory_file"),
        _toggle("inventory_auto_sync", "Refresh while playing"),
        _slider("inventory_refresh_min", "Refresh every", 5, 60, 5, "min"),
        _toggle("inventory_mission_scan", "Read after each mission"),
        _toggle("inventory_ui.hide_foundry_claims", "Hide blueprints in the foundry",
                "Not counted as owned on overlays and lists"),
    ]},
    {"id": "market", "title": "warframe.market", "fields": [
        _choice("wfm_platform", "Platform", [("pc", "PC"), ("ps4", "PlayStation"), ("xbox", "Xbox"),
                                             ("switch", "Switch"), ("mobile", "Mobile")]),
        _toggle("wfm_crossplay", "Crossplay orders"),
    ]},
    {"id": "game", "title": "Game files", "fields": [
        _text("ee_log_path", "EE.log path",
              "Leave empty to find it automatically. A new path is used once the game or plugin restarts",
              check="ee_log"),
    ]},
    {"id": "advanced", "title": "Advanced", "fields": [
        _toggle("debug_save_captures", "Save scan captures", "Keep screenshots of failed scans in the log folder"),
        _toggle("riven_scanner.debug_dumps", "Save riven scan crops"),
        _toggle("data_auto_refresh", "Weekly game data refresh", "Items and relics from WFCD on GitHub"),
        _toggle("world.enrich", "Extra world sources", "Cycle and bounty details from browse.wf / warframestat.us"),
    ]},
]

FIELDS: dict[str, dict] = {f["key"]: f for s in SCHEMA for f in s["fields"]}


def default_of(key: str) -> Any:
    cur: Any = DEFAULTS
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def nested_patch(key: str, value: Any) -> dict:
    parts = key.split(".")
    patch: dict = {}
    cur = patch
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value
    return patch


def merge_patch(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = merge_patch(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def validate(field: dict, value: Any) -> Any:
    kind = field["kind"]
    if kind == "toggle":
        if not isinstance(value, bool):
            raise ValueError(f"{field['key']} expects true/false")
        return value
    if kind == "slider":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field['key']} expects a number")
        v = min(field["max"], max(field["min"], float(value)))
        step = field["step"]
        v = round(round((v - field["min"]) / step) * step + field["min"], 4)
        return int(v) if float(step).is_integer() and float(field["min"]).is_integer() else v
    if kind == "dropdown":
        allowed = [o["value"] for o in field["options"]]
        if value not in allowed:
            raise ValueError(f"{field['key']} must be one of {allowed}")
        return value
    if kind == "text":
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise ValueError(f"{field['key']} expects text")
        v = value.strip()
        if len(v) > 1024 or "\0" in v:
            raise ValueError("path too long")
        return v
    raise ValueError(f"unknown field kind {kind}")


def build_view(settings) -> dict:
    sections = []
    for s in SCHEMA:
        fields = []
        for f in s["fields"]:
            v = settings.get(f["key"], default_of(f["key"]))
            fields.append({**f, "value": v, "default": default_of(f["key"])})
        sections.append({"id": s["id"], "title": s["title"], "fields": fields})
    return {"sections": sections}


def expand_home(path: str, home: str) -> str:
    """'~' / '~/x' against the player's home (the worker runs as root)."""
    if path == "~" or path.startswith("~/"):
        return os.path.join(home, path[2:])
    return path


def _deck_home() -> str:
    from .. import session

    try:
        return session.deck_user().pw_dir
    except Exception:  # noqa: BLE001
        return "/home/deck"


def set_value(settings, key: str, value: Any, home: str | None = None) -> dict:
    field = FIELDS.get(key)
    if field is None:
        raise ValueError(f"unknown setting {key}")
    v = validate(field, value)
    if field.get("check") == "ee_log" and v:
        # find_ee_log does not expand ~, so store the absolute path it can open.
        v = expand_home(v, home if home is not None else _deck_home())
    patch: dict = {}
    for k in [key, *field.get("also", [])]:
        patch = merge_patch(patch, nested_patch(k, v))
    settings.update(patch)
    return {**field, "value": settings.get(key)}


def reset_section(settings, section_id: str) -> dict:
    sec = next((s for s in SCHEMA if s["id"] == section_id), None)
    if sec is None:
        raise ValueError(f"unknown section {section_id}")
    patch: dict = {}
    for f in sec["fields"]:
        for k in [f["key"], *f.get("also", [])]:
            patch = merge_patch(patch, nested_patch(k, default_of(k)))
    settings.update(patch)
    return build_view(settings)


def check_path(path: str, home: str) -> dict:
    p = expand_home((path or "").strip(), home)  # root's ~ is not the player's
    out: dict = {"path": p, "exists": False, "isFile": False, "readable": False, "size": None, "mtime": None}
    if not p:
        return out
    try:
        st = os.stat(p)
    except OSError as e:
        out["error"] = e.strerror or str(e)
        return out
    out.update(exists=True, isFile=os.path.isfile(p), readable=os.access(p, os.R_OK),
               size=st.st_size, mtime=st.st_mtime)
    return out


_PROTON_RE = re.compile(r"(?:^|\s)PROTON_LOG=(\S*)")


def compose_launch_options(current: str | None) -> dict:
    """Adds PROTON_LOG=1 while keeping the user's options.

    "" -> "PROTON_LOG=1 %command%"; "gamemoderun %command% -dx11" ->
    "PROTON_LOG=1 gamemoderun %command% -dx11"; "-dx11" (args only, Steam appends them)
    -> "PROTON_LOG=1 %command% -dx11"; PROTON_LOG=0 is switched to 1 in place.
    """
    cur = (current or "").strip()
    m = _PROTON_RE.search(cur)
    if m and m.group(1) not in ("", "0"):
        return {"current": cur, "proposed": cur, "alreadySet": True}
    if m:
        proposed = _PROTON_RE.sub(lambda mm: mm.group(0).replace(f"PROTON_LOG={mm.group(1)}", LAUNCH_OPTION_VAR), cur, 1)
        if "%command%" not in proposed:
            proposed = f"{proposed} %command%".strip()
    elif not cur:
        proposed = f"{LAUNCH_OPTION_VAR} %command%"
    elif "%command%" in cur:
        proposed = f"{LAUNCH_OPTION_VAR} {cur}"
    else:
        proposed = f"{LAUNCH_OPTION_VAR} %command% {cur}"
    return {"current": cur, "proposed": proposed, "alreadySet": False}


def setup(app) -> None:
    app.services["settings_schema"] = SCHEMA
    # QAM sections cache settings.get; this lets them refresh when any writer changes them.
    app.settings.on_change(lambda _data: app.emit("settings.changed", None))


def _app():
    from ..app import get_app

    return get_app()


@rpc.method("settings.view")
def _rpc_view() -> dict:
    return build_view(_app().settings)


@rpc.method("settings.set")
def _rpc_set(key: str, value: Any = None) -> dict:
    return set_value(_app().settings, key, value)


@rpc.method("settings.reset")
def _rpc_reset(section: str) -> dict:
    return reset_section(_app().settings, section)


@rpc.method("settings.check_path")
async def _rpc_check_path(kind: str = "", path: str = "") -> dict:
    app = _app()

    def work() -> dict:
        home = _deck_home()
        out = check_path(path, home)
        if kind == "ee_log" and not path.strip():
            from ..eelog import find_ee_log

            found = find_ee_log("")
            out = check_path(found or "", home)
            out["auto"] = True
        return out

    return await app.run_blocking(work)


async def _safe(name: str, params: dict | None = None) -> Any:
    try:
        return await rpc.call_local(name, params or {})
    except KeyError:
        return {"error": f"{name} unavailable"}
    except Exception as e:  # noqa: BLE001 - diagnostics must always render
        return {"error": str(e)}


@rpc.method("settings.diagnostics")
async def _rpc_diagnostics() -> dict:
    t0 = time.monotonic()
    out = {
        "core": await _safe("core.status"),
        "eelog": await _safe("eelog.status"),
        "inventory": await _safe("inventory.status"),
        "data": await _safe("data.status"),
    }
    out["ms"] = round((time.monotonic() - t0) * 1000)
    return out


@rpc.method("settings.proton_plan")
async def _rpc_proton_plan(current: str | None = None) -> dict:
    """current: the options the Steam client reports (preferred); else localconfig.vdf."""
    from .. import protonlog

    app = _app()
    source = "steam"
    if current is None:
        source = "localconfig"
        current = await app.run_blocking(protonlog.steam_launch_options)
    plan = compose_launch_options(current)
    plan.update(source=source, appId=WARFRAME_APPID, hint=protonlog.LAUNCH_OPTION)
    try:
        shlex.split(plan["proposed"])
        plan["valid"] = True
    except ValueError:
        plan["valid"] = False  # unbalanced quotes in the user's options: let them edit by hand
    return plan
