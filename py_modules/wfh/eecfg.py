"""Warframe's interface scale from EE.cfg (next to EE.log in the Proton prefix).

Port of WFHelper services/eeLogPath.ts parseWarframeUiScaleFromEeCfg /
resolveWarframeUiScale. The file is re-read on every call so a change made in
the game's settings applies to the next scan; the game rewrites it lazily and
logs `Saved package: /Configs/EE.cfg` when it does (is_cfg_saved_line).
"""

from __future__ import annotations

import os
import re

from . import log
from .eelog import find_ee_log

_log = log.get("eecfg")

_MODE_CUSTOM_RE = re.compile(r"^\s*Flash\.FlashDrawScaleMode\s*=\s*[DM]SM_CUSTOM\s*$", re.M)
_MODE_RE = re.compile(r"^\s*Flash\.FlashDrawScaleMode\s*=\s*(\S+)\s*$", re.M)
_SCALE_RE = re.compile(r"^\s*Flash\.FlashDrawScale\s*=\s*([0-9.]+)\s*$", re.M)
_SAVED_RE = re.compile(r"Saved package: /Configs/EE\.cfg\s*$")


def parse_ui_scale(text: str) -> float | None:
    """The custom menu scale (0.5..1), or None when the slider is not in effect.

    The stored value only means what the slider shows while the mode is custom
    (DSM_CUSTOM; MSM_CUSTOM is a documented variant). Legacy writes
    DSM_MATCH_SCREEN and draws menus at a fixed pixel size.
    """
    if not _MODE_CUSTOM_RE.search(text):
        return None
    matches = _SCALE_RE.findall(text)
    if not matches:
        return None
    try:
        value = float(matches[-1])  # the last occurrence wins
    except ValueError:
        return None
    if not value > 0 or value != value or value == float("inf"):
        return None
    return min(1.0, max(0.5, float(f"{value:.2f}")))


def parse_mode(text: str) -> str:
    m = _MODE_RE.search(text)
    return m.group(1) if m else "unset"


def is_cfg_saved_line(line: str) -> bool:
    return bool(_SAVED_RE.search(line))


def cfg_path() -> str | None:
    """EE.cfg beside the EE.log the tailer follows (or discovery finds)."""
    ee_log = None
    try:
        from .app import get_app

        app = get_app()
        ee_log = app.eelog.path or find_ee_log(app.settings.get("ee_log_path", "") or "")
    except RuntimeError:
        ee_log = find_ee_log()
    return os.path.join(os.path.dirname(ee_log), "EE.cfg") if ee_log else None


_last_note = ""


def _note(msg: str) -> None:
    global _last_note
    if msg != _last_note:
        _last_note = msg
        _log.info(msg)


def ui_scale_info(path: str | None = None) -> dict:
    """{scale, mode, path, reason}; scale None means "use the manual setting"."""
    path = path or cfg_path()
    if not path:
        _note("no EE.log path, using the manual interface scale")
        return {"scale": None, "mode": None, "path": None, "reason": "no-ee-log"}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        _note(f"EE.cfg unreadable at {path}, using the manual interface scale")
        return {"scale": None, "mode": None, "path": path, "reason": "unreadable"}
    scale = parse_ui_scale(text)
    mode = parse_mode(text)
    if scale is not None:
        reason = "custom"
        _note(f"Warframe interface scale from EE.cfg: {scale}")
    elif mode == "DSM_MATCH_SCREEN":
        reason = "legacy"
        _note(f"EE.cfg menu scale is Legacy (mode {mode}), a fixed pixel size")
    else:
        reason = "not-custom"
        _note(f"EE.cfg has no custom interface scale (mode {mode}), using the manual slider")
    return {"scale": scale, "mode": mode, "path": path, "reason": reason}


def ui_scale(path: str | None = None) -> float | None:
    """Interface scale from EE.cfg, read fresh; None when missing or not custom."""
    return ui_scale_info(path)["scale"]
