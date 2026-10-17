"""Tails Warframe's EE.log and fans lines out to subscribers.

Under Proton the log lives in the game's prefix:
  <library>/steamapps/compatdata/230410/pfx/drive_c/users/steamuser/AppData/Local/Warframe/EE.log
Steam libraries come from libraryfolders.vdf, so SD-card installs are found too.
The game truncates the log on start; the tailer notices and restarts at 0.

The engine flushes EE.log lazily (a reward line can land 10-26 s late). When the
game runs with `PROTON_LOG=1 %command%`, the same lines also arrive at once
through Proton's debug log (see protonlog.py). Both streams are merged here, on
one thread, the way WFHelper's eeLogMonitor does it: real-time payloads pass
DebugLineGate and are delivered immediately; the file copy of such a line that
shows up later is an echo and is dropped for ordinary subscribers. Lines the
gate ignores still reach subscribers through the file, as before.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Callable

from . import log, protonlog, rpc
from .session import WARFRAME_STEAM_APPID, deck_user

_log = log.get("eelog")

LineHandler = Callable[[str], None]

FILE = "file"
REALTIME = "realtime"
MERGED = "merged"

_PREFIX_REL = os.path.join(
    "steamapps", "compatdata", WARFRAME_STEAM_APPID, "pfx", "drive_c", "users", "steamuser",
    "AppData", "Local", "Warframe", "EE.log",
)

# The file re-delivers real-time lines up to ~26 s later; keep above the flush lag
# (WFHelper missionRewardsIpc ECHO_WINDOW_MS, eeLogMonitor REWARD_FILE_ECHO_WINDOW_MS).
ECHO_WINDOW_S = 30.0
_UPTIME_STAMP = re.compile(r"^\s*(\d+\.\d+)\s+")
PROTON_POLL_S = 0.25
LINUX_REPROBE_S = 60.0


# ---- discovery (services/eeLogPath.ts) ------------------------------------

def steam_roots(home: str) -> list[str]:
    roots: list[str] = []
    for steam in (
        os.path.join(home, ".local", "share", "Steam"),
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".steam", "root"),
        os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam"),
        os.path.join(home, "snap", "steam", "common", ".local", "share", "Steam"),
    ):
        if os.path.isdir(steam) and os.path.realpath(steam) not in map(os.path.realpath, roots):
            roots.append(steam)
    return roots


def parse_steam_library_paths(vdf_text: str) -> list[str]:
    return [m.group(1).replace("\\\\", "\\") for m in re.finditer(r'"path"\s+"([^"]*)"', vdf_text)]


def steam_libraries(home: str) -> list[str]:
    roots = steam_roots(home)
    libs = list(roots)
    for steam in roots:
        vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                for p in parse_steam_library_paths(f.read()):
                    if os.path.isdir(p) and os.path.realpath(p) not in map(os.path.realpath, libs):
                        libs.append(p)
        except OSError:
            pass
    return libs


def _listdir(path: str) -> list[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _ee_logs_in_prefix(prefix: str) -> list[str]:
    """EE.log under any Windows user of a prefix; a Proton prefix nests it in pfx."""
    found = []
    for root in (prefix, os.path.join(prefix, "pfx")):
        users = os.path.join(root, "drive_c", "users")
        for user in _listdir(users):
            cand = os.path.join(users, user, "AppData", "Local", "Warframe", "EE.log")
            if os.path.exists(cand):
                found.append(cand)
    return found


def _launcher_prefixes(home: str) -> list[str]:
    """Heroic (Warframe is on Epic too), Lutris, Bottles and plain Wine prefixes."""
    def flatpak(app_id: str, *rest: str) -> str:
        return os.path.join(home, ".var", "app", app_id, *rest)

    def subdirs(d: str) -> list[str]:
        return [os.path.join(d, n) for n in _listdir(d)]

    def read(p: str) -> str:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    prefixes = [os.path.join(home, ".wine")]
    for d in (os.path.join(home, ".config", "heroic", "GamesConfig"),
              flatpak("com.heroicgameslauncher.hgl", "config", "heroic", "GamesConfig")):
        for f in subdirs(d):
            if not f.endswith(".json"):
                continue
            try:
                parsed = json.loads(read(f))
            except ValueError:
                continue
            if isinstance(parsed, dict):
                for entry in parsed.values():
                    if isinstance(entry, dict) and isinstance(entry.get("winePrefix"), str) and entry["winePrefix"]:
                        prefixes.append(entry["winePrefix"])
    prefixes += subdirs(os.path.join(home, "Games", "Heroic", "Prefixes", "default"))
    for d in (os.path.join(home, ".config", "lutris", "games"),
              os.path.join(home, ".local", "share", "lutris", "games"),
              flatpak("net.lutris.Lutris", "config", "lutris", "games"),
              flatpak("net.lutris.Lutris", "data", "lutris", "games")):
        for f in subdirs(d):
            if not re.search(r"\.ya?ml$", f):
                continue
            m = re.search(r"^[ \t]*prefix:[ \t]*(\S.*?)[ \t]*$", read(f), re.M)
            if m:
                prefixes.append(re.sub(r"^([\"'])(.*)\1$", r"\2", m.group(1)))
    prefixes += subdirs(os.path.join(home, ".local", "share", "bottles", "bottles"))
    prefixes += subdirs(flatpak("com.usebottles.bottles", "data", "bottles", "bottles"))
    out: list[str] = []
    for p in prefixes:
        if p == "~" or p.startswith("~/"):
            p = os.path.join(home, p[2:])  # root's ~ is not the player's
        if os.path.isabs(p) and p not in out:
            out.append(p)
    return out


def discover_ee_log(home: str) -> tuple[str | None, bool]:
    """(path, verified). Every compatdata id is checked, since Warframe added as a
    non-Steam game gets a random one; the newest log wins across launchers."""
    libs = steam_libraries(home)
    found: list[str] = []
    for lib in libs:
        compat = os.path.join(lib, "steamapps", "compatdata")
        for appid in _listdir(compat):
            found += _ee_logs_in_prefix(os.path.join(compat, appid))
    for prefix in _launcher_prefixes(home):
        found += _ee_logs_in_prefix(prefix)
    if found:
        return max(found, key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0), True
    # The expected path in a fresh prefix, so a later lookup attaches once written.
    for lib in libs:
        if os.path.exists(os.path.join(lib, "steamapps", "compatdata", WARFRAME_STEAM_APPID)):
            return os.path.join(lib, _PREFIX_REL), False
    return None, False


_discovery: tuple[float, str | None, bool] | None = None


def find_ee_log(override: str = "") -> str | None:
    """Existing EE.log (override first, then WFHELPER_EE_LOG, then discovery).

    Discovery walks every prefix, so an existing hit is pinned and a miss is
    re-probed at most once a minute.
    """
    global _discovery
    for cand in (override, os.environ.get("WFHELPER_EE_LOG", "").strip()):
        if cand and os.path.exists(cand):
            return cand
    now = time.monotonic()
    d = _discovery
    pinned = d is not None and d[2] and d[1] is not None and os.path.exists(d[1])
    if not pinned and (d is None or now - d[0] >= LINUX_REPROBE_S):
        path, verified = discover_ee_log(deck_user().pw_dir)
        _discovery = (now, path, verified)
    path = _discovery[1]
    return path if path and os.path.exists(path) else None


def _reset_discovery() -> None:
    global _discovery
    _discovery = None


# ---- echo pairing (missionRewardsIpc.ts isEcho, generalised) -----------------

def line_text(line: str) -> tuple[str | None, str]:
    """(uptime stamp, text without it) of an EE.log line or a debugstr payload."""
    m = _UPTIME_STAMP.match(line)
    stamp = m.group(1) if m else None
    return stamp, (line[m.end():] if m else line).strip()


class EchoFilter:
    """A file line pairs with the oldest unpaired real-time copy of the same text
    seen within the window; an equal uptime stamp marks a repeated file copy."""

    def __init__(self, window_s: float = ECHO_WINDOW_S):
        self.window = window_s
        self._seen: dict[str, list[dict]] = {}

    def _prune(self, now: float) -> None:
        for text in list(self._seen):
            recent = [s for s in self._seen[text] if now - s["at"] < self.window]
            if recent:
                self._seen[text] = recent
            else:
                del self._seen[text]

    def note_realtime(self, line: str, now: float) -> None:
        self._prune(now)
        stamp, text = line_text(line)
        if text:
            self._seen.setdefault(text, []).append({"source": REALTIME, "stamp": stamp, "at": now, "paired": False})

    def is_file_echo(self, line: str, now: float) -> bool:
        if not self._seen:
            return False
        self._prune(now)
        stamp, text = line_text(line)
        if not text:
            return False
        seen = self._seen.get(text)
        if seen is None and len(text) >= 12:
            # A payload logged without its channel prefix still pairs by suffix.
            for key, rows in self._seen.items():
                if text.endswith(key) and any(r["source"] == REALTIME and not r["paired"] for r in rows):
                    seen = rows
                    break
        if seen is None:
            return False
        original = None
        if stamp is not None:
            original = next((s for s in seen if s["stamp"] == stamp), None)
        if original is None:
            original = next((s for s in seen if s["source"] == REALTIME and not s["paired"]), None)
        if original is not None:
            original["paired"] = True
            seen.append({"source": FILE, "stamp": stamp, "at": now, "paired": True})
        return original is not None


# ---- the tailer --------------------------------------------------------------

class EELogTailer:
    def __init__(self, path_override: Callable[[], str] = lambda: "", clock: Callable[[], float] = time.monotonic,
                 proton_path: Callable[[], str] | None = None):
        self._handlers: list[tuple[LineHandler, str]] = []
        self._path_override = path_override
        self._clock = clock
        self._proton_path = proton_path or self._default_proton_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.path: str | None = None
        self.lines_seen = 0
        # Source of the line being dispatched right now ("file" or "realtime").
        self.source: str = FILE
        self._gate = protonlog.DebugLineGate()
        self._echoes = EchoFilter()
        self._proton: protonlog.ProtonLogTail | None = None
        self._proton_checked = 0.0
        self._proton_polled = -1e9
        self.realtime_lines = 0
        self.echoes_dropped = 0
        # file tail state
        self._f = None
        self._pos = 0
        self._ino: int | None = None
        self._buf = b""
        self._last_lookup = -1e9

    # -- subscription -----------------------------------------------------

    def subscribe(self, fn: LineHandler, source: str = MERGED) -> None:
        """fn(line) for every line. source="merged" (default) gets real-time lines
        at once and file lines minus their echoes; "file" gets every EE.log line
        exactly once in file order (run trackers); "realtime" only Proton lines."""
        if source not in (MERGED, FILE, REALTIME):
            raise ValueError(f"unknown source {source}")
        self._handlers.append((fn, source))

    def start(self) -> None:
        if self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="eelog", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # -- dispatch -----------------------------------------------------------

    def _dispatch(self, line: str, source: str, kinds: tuple[str, ...]) -> None:
        self.source = source
        for fn, kind in list(self._handlers):
            if kind not in kinds:
                continue
            try:
                fn(line)
            except Exception:  # noqa: BLE001
                _log.exception("EE.log handler %s failed", getattr(fn, "__qualname__", fn))
        self.source = FILE

    def feed(self, line: str, source: str = FILE) -> None:
        """Dispatch one line to every subscriber (also used by tests).

        source="realtime" treats line as an OutputDebugString payload.
        """
        if source == REALTIME:
            self.feed_realtime(line)
            return
        self.lines_seen += 1
        if self._echoes.is_file_echo(line, self._clock()):
            self.echoes_dropped += 1
            self._dispatch(line, FILE, (FILE,))
            return
        self._dispatch(line, FILE, (FILE, MERGED))

    def feed_realtime(self, payload: str) -> None:
        """One OutputDebugString payload (may hold several lines)."""
        now = self._clock()
        for part in payload.split("\n"):
            part = part.rstrip("\r")
            if not part.strip():
                continue
            verdict = self._gate.classify(part, now)
            if verdict == protonlog.IGNORE:
                continue
            # A cooldown-suppressed repeat is still noted, so its late file copy
            # cannot reopen what the first copy already triggered.
            self._echoes.note_realtime(part, now)
            if verdict == protonlog.FORWARD:
                self.realtime_lines += 1
                self._dispatch(part, REALTIME, (REALTIME, MERGED))

    def feed_proton_raw(self, raw: str) -> None:
        """One raw line of Proton's log (tests and the poll loop)."""
        parsed = protonlog.parse_wine_debugstr(raw)
        # Wine cuts long messages (trade dialog blobs); the file delivers those whole.
        if parsed is None or parsed.truncated:
            return
        self.feed_realtime(parsed.text)

    # -- status -------------------------------------------------------------

    def realtime_active(self) -> bool:
        """True while Proton's log grew in the last 30 s (isProtonDebugstrActive)."""
        return self._proton is not None and self._proton.is_fresh(self._clock())

    @staticmethod
    def _default_proton_path() -> str:
        return protonlog.resolve_proton_log_path(env=protonlog.game_env())

    def status(self) -> dict:
        env = protonlog.game_env()
        opts = protonlog.steam_launch_options()
        proton_path = self._proton.path if self._proton else protonlog.resolve_proton_log_path(env=env)
        return {
            "ee_log": self.path,
            "ee_log_found": bool(self.path and os.path.exists(self.path)),
            "lines_seen": self.lines_seen,
            "proton_log": proton_path,
            "proton_log_exists": os.path.exists(proton_path),
            # The running game's env has PROTON_LOG (None while it is not running).
            "proton_log_env": protonlog.proton_log_env_active(env),
            "launch_options": opts,
            "launch_option_set": protonlog.launch_option_has_proton_log(opts),
            "realtime_active": self.realtime_active(),
            "realtime_lines": self.realtime_lines,
            "echoes_dropped": self.echoes_dropped,
            "launch_option_hint": protonlog.LAUNCH_OPTION,
        }

    # -- polling --------------------------------------------------------------

    def _poll_proton(self, now: float) -> bool:
        if now - self._proton_checked >= 10 or self._proton is None:
            self._proton_checked = now
            path = self._proton_path()
            if self._proton is None or self._proton.path != path:
                if self._proton:
                    self._proton.close()
                self._proton = protonlog.ProtonLogTail(path)
                _log.info("watching %s for real-time lines%s", path,
                          "" if os.path.exists(path) else f" (absent; add {protonlog.LAUNCH_OPTION})")
        raws = self._proton.poll(now)
        for raw in raws:
            self.feed_proton_raw(raw)
        return bool(raws)

    def _close_file(self) -> None:
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
        self._f = None

    def _poll_file(self, now: float) -> bool:
        if self._f is None:
            if now - self._last_lookup < 5:
                return False
            self._last_lookup = now
            path = find_ee_log(self._path_override())
            if not path:
                return False
            try:
                self._f = open(path, "rb")
                st = os.fstat(self._f.fileno())
                self._ino = st.st_ino
                # Start at the end: only new events matter for overlays.
                self._pos = st.st_size
                self._f.seek(self._pos)
                self._buf = b""
                self.path = path
                _log.info("tailing %s from %d", path, self._pos)
            except OSError:
                self._close_file()
                return False
        try:
            st = os.stat(self.path)
        except OSError:
            self._close_file()
            return False
        if st.st_ino != self._ino or st.st_size < self._pos:
            # Game restarted: new or truncated log. Read the new one from the top.
            self._close_file()
            try:
                self._f = open(self.path, "rb")
            except OSError:
                return False
            self._ino = os.fstat(self._f.fileno()).st_ino
            self._pos = 0
            self._buf = b""
            _log.info("EE.log rotated, reading from start")
        chunk = self._f.read()
        if not chunk:
            return False
        self._pos += len(chunk)
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        for raw in lines:
            self.feed(raw.decode("utf-8", errors="replace").rstrip("\r"))
        return True

    def poll_once(self) -> bool:
        """One iteration of the loop (tests drive the tailer with this)."""
        now = self._clock()
        busy = False
        if now - self._proton_polled >= PROTON_POLL_S:
            self._proton_polled = now
            try:
                busy |= self._poll_proton(now)
            except Exception:  # noqa: BLE001 - never let the optional feed stop EE.log
                _log.exception("proton log poll failed")
        busy |= self._poll_file(now)
        return busy

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.poll_once():
                time.sleep(0.05)
        self._close_file()
        if self._proton:
            self._proton.close()


@rpc.method("eelog.status")
async def _rpc_status() -> dict:
    """EE.log + Proton real-time feed status (incl. whether PROTON_LOG is on)."""
    from .app import get_app
    from . import eecfg

    app = get_app()
    out = await app.run_blocking(app.eelog.status)
    out["ui_scale"] = await app.run_blocking(eecfg.ui_scale_info)
    return out
