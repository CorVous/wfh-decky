"""Real-time EE.log lines from Proton's debug log (PROTON_LOG=1).

EE.log is flushed lazily by the engine: a reward or relic-picker line can reach
the file 10-26 s after the event. Warframe also sends every log line through
OutputDebugString, and with `PROTON_LOG=1 %command%` in the launch options
Proton writes those calls to ~/steam-230410.log at once, as wine trace lines:

  1234.567:0048:004c:warn:seh:OutputDebugStringA "Sys [Info]: Pause countdown done\\n"

This module ports WFHelper's services/protonDebugstrMonitor.ts (tail + payload
parse) and services/debugLineFilter.ts (which payloads are worth forwarding).
eelog.EELogTailer drives the tail on its own thread and merges the payloads
with the file lines, dropping the file's late echoes.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from . import log
from .session import WARFRAME_STEAM_APPID, deck_user, find_warframe_pid

_log = log.get("protonlog")

LAUNCH_OPTION = "PROTON_LOG=1 %command%"

MAX_READ_BYTES = 256 * 1024
MAX_READ_LOOPS_PER_TICK = 8
# Require recent growth so a stale Proton log cannot claim real-time delivery.
ACTIVE_WINDOW_S = 30.0

# Modern Proton logs `warn:seh:OutputDebugStringA`; older ones `trace:debugstr:`.
_DEBUGSTR_OPEN_RE = re.compile(r'(?:trace|warn):(?:debugstr|seh):OutputDebugString[AW]\s+(L?)"')
_HEX = set("0123456789abcdefABCDEF")
_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}


@dataclass
class Debugstr:
    text: str
    # Wine cuts messages at ~290 chars and appends "..." after the closing quote.
    truncated: bool


def _unescape_ansi(esc: str) -> str:
    """ANSI payloads escape non-printables per byte; decode the bytes as UTF-8 so
    multi-byte glyphs (the U+E000 platform marker) match the file's text."""
    out = bytearray()
    i = 0
    n = len(esc)
    while i < n:
        c = esc[i]
        if c != "\\":
            # Lines are decoded latin-1, so every char maps back to its byte.
            out += c.encode("latin-1") if ord(c) < 256 else c.encode("utf-8")
            i += 1
            continue
        nxt = esc[i + 1] if i + 1 < n else ""
        if nxt in _SIMPLE_ESCAPES:
            out += _SIMPLE_ESCAPES[nxt].encode()
            i += 2
        elif nxt == "x" and esc[i + 2:i + 3] in _HEX and esc[i + 3:i + 4] in _HEX:
            out.append(int(esc[i + 2:i + 4], 16))
            i += 4
        else:
            out.append(0x5C)  # stray backslash: keep literally
            i += 1
    return out.decode("utf-8", errors="replace")


def _unescape_wide(esc: str) -> str:
    """Wide payloads escape non-printables as UTF-16 code units (up to 4 hex digits)."""
    out: list[str] = []
    i = 0
    n = len(esc)
    while i < n:
        c = esc[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        nxt = esc[i + 1] if i + 1 < n else ""
        if nxt in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[nxt])
            i += 2
        elif nxt == "x" and esc[i + 2:i + 3] in _HEX and esc[i + 3:i + 4] in _HEX:
            digits = 4 if esc[i + 4:i + 5] in _HEX and esc[i + 5:i + 6] in _HEX else 2
            out.append(chr(int(esc[i + 2:i + 2 + digits], 16)))
            i += 2 + digits
        else:
            out.append("\\")
            i += 1
    return "".join(out)


def parse_wine_debugstr(raw: str) -> Debugstr | None:
    """The OutputDebugString payload of one wine trace line, or None."""
    m = _DEBUGSTR_OPEN_RE.search(raw)
    if not m:
        return None
    start = m.end()
    trimmed = raw.rstrip()
    close = trimmed.rfind('"')
    if close < start:
        return None  # no closing quote: malformed
    payload = trimmed[start:close]
    text = _unescape_wide(payload) if m.group(1) == "L" else _unescape_ansi(payload)
    return Debugstr(text, trimmed[close + 1:] == "...")


# ---- which payloads are forwarded (debugLineFilter.ts) --------------------

FILTER_SUBSTRINGS_LOWER = (
    "loadingcompleteend",  # relic selection screen ready (primary trigger)
    "populateinventorygrid",  # relic selection screen ready (fallback trigger)
    "initmapping",  # relic picker close (returns to gameplay)
    "dialog::sendresult",  # relic/riven dialog closing
    "pause countdown done",  # mission reward trigger
    "got rewards",  # mission reward trigger
    "projectionrewardchoice.lua",  # reward cards rendering (early-scan signal)
    "omegarerollselection.swf",  # riven rolling screen opened
    "diorama setup",  # riven diorama ready
    "npcmanager::clearagents",  # riven session close
    "recycled effects",  # riven session close (alt signal)
    "dialog::createokcancel",  # riven cycle confirm / choice confirm
    "themeddetailedpurchasedialog",  # chat riven HudVis + PopulateInfo detection
    "tradingpost.lua",  # trade partner detection
    "you are offering",  # trade dialog buffering start
    "the trade was successful",  # trade dialog success
    "chatredux::addtab",  # incoming whisper opens a private chat tab
    "mainmenu::logindone",  # inventory refresh after login
    "eom missionlocationunlocked=",  # mission end: post-mission inventory is in memory briefly
    "topmenu.lua: abort:",  # mission abort
    "syncautopopulatedconsumables for mission",  # mission type and node
    "onstatestarted, mission type=",  # mission type
)

# The game repeats picker lines; match eeLogMonitor's picker cooldown.
RELIC_PICKER_SUPPRESS_S = 7.5
CHAT_TAB_SUPPRESS_S = 2.0
CHAT_RIVEN_WEAPON_WINDOW_S = 2.0
REROLL_WEAPON_WINDOW_S = 15.0

FORWARD = "forward"
SUPPRESS = "suppress"  # relevant, but a repeat inside a cooldown
IGNORE = "ignore"


class DebugLineGate:
    """Prefilter for real-time lines; the file stream stays authoritative."""

    def __init__(self) -> None:
        self._relic_until = 0.0
        self._chat_tab_until = 0.0
        self._riven_weapon_until = 0.0

    def classify(self, msg: str, now: float) -> str:
        low = msg.lower()
        weapon_load = "/lotus/weapons/" in low and (
            "resourceloader" in low or "resloader" in low or "resource load completed" in low)
        if "omegarerollselection.swf" in low:
            self._riven_weapon_until = now + REROLL_WEAPON_WINDOW_S
        elif "themeddetailedpurchasedialog" in low and "hudvis" in low:
            self._riven_weapon_until = now + CHAT_RIVEN_WEAPON_WINDOW_S
        if weapon_load and now >= self._riven_weapon_until:
            return IGNORE
        if not weapon_load and not any(s in low for s in FILTER_SUBSTRINGS_LOWER):
            return IGNORE
        if "loadingcompleteend" in low or "populateinventorygrid" in low:
            if now < self._relic_until:
                return SUPPRESS
            self._relic_until = now + RELIC_PICKER_SUPPRESS_S
        if "chatredux::addtab" in low:
            if now < self._chat_tab_until:
                return SUPPRESS
            self._chat_tab_until = now + CHAT_TAB_SUPPRESS_S
        return FORWARD

    def wants(self, msg: str, now: float) -> bool:
        return self.classify(msg, now) == FORWARD


# ---- the log file ------------------------------------------------------------

class ProtonLogTail:
    """Tails one Proton log. Ignores what is already in the file on attach and
    treats truncation or re-creation as a fresh game launch (read from 0)."""

    def __init__(self, path: str):
        self.path = path
        self._offset = 0
        self._remainder = b""
        self._fd: int | None = None
        self._state = "init"  # init | tailing | missing
        self._last_growth = 0.0
        self.bytes_read = 0

    def is_fresh(self, now: float) -> bool:
        return self._last_growth > 0 and now - self._last_growth < ACTIVE_WINDOW_S

    def exists(self) -> bool:
        return self._state == "tailing"

    def poll(self, now: float) -> list[str]:
        """New complete raw lines since the last poll."""
        try:
            size = os.stat(self.path).st_size
        except OSError:
            # Absent or deleted: a later appearance is a fresh Proton launch.
            self._close_fd()
            self._state = "missing"
            self._offset = 0
            self._remainder = b""
            return []
        if self._state == "init":
            self._offset = size
            self._state = "tailing"
            return []
        self._state = "tailing"
        if size < self._offset:
            self._close_fd()
            self._offset = 0
            self._remainder = b""
        if size == self._offset:
            return []
        out: list[str] = []
        try:
            if self._fd is None:
                self._fd = os.open(self.path, os.O_RDONLY)
            for _ in range(MAX_READ_LOOPS_PER_TICK):
                chunk = os.pread(self._fd, MAX_READ_BYTES, self._offset)
                if not chunk:
                    # stat says there is more but the fd is at EOF: replaced under us.
                    self._close_fd()
                    break
                self._offset += len(chunk)
                self.bytes_read += len(chunk)
                self._last_growth = now
                out.extend(self._consume(chunk))
                if len(chunk) < MAX_READ_BYTES:
                    break
        except OSError as e:
            self._close_fd()
            _log.error("proton log read error: %s", e)
        return out

    def close(self) -> None:
        self._close_fd()
        self._remainder = b""

    def _close_fd(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def _consume(self, chunk: bytes) -> list[str]:
        merged = self._remainder + chunk
        parts = merged.split(b"\n")
        self._remainder = parts.pop()
        lines = []
        for raw in parts:
            # Wine writes the escaped payload as ASCII; latin-1 keeps any odd byte 1:1.
            line = raw.rstrip(b"\r").decode("latin-1")
            if line:
                lines.append(line)
        return lines


# ---- where the log is and whether the game was started with it ----------------

def _read_environ(pid: int) -> dict[str, str]:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    env = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if sep:
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env


_game_env_cache: tuple[float, int | None, dict[str, str]] = (0.0, None, {})


def game_env(max_age: float = 10.0) -> dict[str, str] | None:
    """Environment of the running Warframe process (Proton passes its env down),
    or None while the game is not running. Cached for max_age seconds."""
    global _game_env_cache
    now = time.monotonic()
    at, pid, env = _game_env_cache
    if now - at > max_age:
        pid = find_warframe_pid()
        env = _read_environ(pid) if pid else {}
        _game_env_cache = (now, pid, env)
    return env if pid else None


def proton_log_env_active(env: dict[str, str] | None) -> bool | None:
    """True/False from the game's environment; None while the game is not running."""
    if env is None:
        return None
    value = env.get("PROTON_LOG", "").strip()
    return bool(value) and value != "0"


def resolve_proton_log_path(home: str | None = None, env: dict[str, str] | None = None) -> str:
    """WFHELPER_PROTON_LOG overrides; else Proton's own rule,
    $PROTON_LOG_DIR|$HOME/steam-$SteamGameId.log, from the running game's env.
    A non-Steam shortcut has its own SteamGameId; 230410 while the game is closed."""
    override = os.environ.get("WFHELPER_PROTON_LOG", "").strip()
    if override:
        return override
    env = env or {}
    log_dir = env.get("PROTON_LOG_DIR", "").strip() or env.get("HOME", "").strip()
    if not log_dir:
        log_dir = home or deck_user().pw_dir
    game_id = env.get("SteamGameId", "").strip() or WARFRAME_STEAM_APPID
    return os.path.join(log_dir, f"steam-{game_id}.log")


_LAUNCH_OPTIONS_RE = re.compile(r'"LaunchOptions"\s+"((?:[^"\\]|\\.)*)"', re.IGNORECASE)


def _vdf_blocks(text: str, key: str):
    """Bodies of every `"key" { ... }` block in a VDF text (brace matched)."""
    needle = f'"{key}"'
    pos = 0
    while True:
        at = text.find(needle, pos)
        if at < 0:
            return
        pos = at + len(needle)
        j = pos
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if j >= len(text) or text[j] != "{":
            continue
        depth = 0
        in_str = False
        k = j
        while k < len(text):
            c = text[k]
            if in_str:
                if c == "\\":
                    k += 1
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[j + 1:k]
                    break
            k += 1
        pos = j + 1


def parse_launch_options(localconfig_text: str, appid: str = WARFRAME_STEAM_APPID) -> str | None:
    """Steam's per-user launch options for an app from localconfig.vdf."""
    for body in _vdf_blocks(localconfig_text, appid):
        m = _LAUNCH_OPTIONS_RE.search(body)
        if m:
            return m.group(1).replace('\\"', '"').replace("\\\\", "\\")
    return None


def steam_launch_options(home: str | None = None) -> str | None:
    """Warframe's launch options as Steam stores them (newest localconfig.vdf wins);
    None when no Steam user config mentions them."""
    from .eelog import steam_roots  # local import: eelog imports this module

    home = home or deck_user().pw_dir
    found: list[tuple[float, str]] = []
    for root in steam_roots(home):
        userdata = os.path.join(root, "userdata")
        try:
            users = os.listdir(userdata)
        except OSError:
            continue
        for uid in users:
            path = os.path.join(userdata, uid, "config", "localconfig.vdf")
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    opts = parse_launch_options(f.read())
                if opts is not None:
                    found.append((os.path.getmtime(path), opts))
            except OSError:
                continue
    if not found:
        return None
    return max(found)[1]


def launch_option_has_proton_log(opts: str | None) -> bool:
    return bool(opts) and re.search(r"(?:^|\s)PROTON_LOG=(?!0\b)\S+", opts or "") is not None
