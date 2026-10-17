"""Tails Warframe's EE.log and fans lines out to subscribers.

Under Proton the log lives in the game's prefix:
  <library>/steamapps/compatdata/230410/pfx/drive_c/users/steamuser/AppData/Local/Warframe/EE.log
Steam libraries come from libraryfolders.vdf, so SD-card installs are found too.
The game truncates the log on start; the tailer notices and restarts at 0.
"""

import os
import re
import threading
import time
from typing import Callable

from . import log
from .session import WARFRAME_STEAM_APPID, deck_user

_log = log.get("eelog")

LineHandler = Callable[[str], None]

_PREFIX_REL = os.path.join(
    "steamapps", "compatdata", WARFRAME_STEAM_APPID, "pfx", "drive_c", "users", "steamuser",
    "AppData", "Local", "Warframe", "EE.log",
)


def steam_libraries(home: str) -> list[str]:
    roots = []
    for steam in (
        os.path.join(home, ".local", "share", "Steam"),
        os.path.join(home, ".steam", "steam"),
        os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam"),
    ):
        if os.path.isdir(steam) and os.path.realpath(steam) not in map(os.path.realpath, roots):
            roots.append(steam)
    libs = list(roots)
    for steam in roots:
        vdf = os.path.join(steam, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    p = m.group(1).replace("\\\\", "\\")
                    if os.path.isdir(p) and os.path.realpath(p) not in map(os.path.realpath, libs):
                        libs.append(p)
        except OSError:
            pass
    return libs


def find_ee_log(override: str = "") -> str | None:
    if override and os.path.exists(override):
        return override
    home = deck_user().pw_dir
    candidates = [os.path.join(lib, _PREFIX_REL) for lib in steam_libraries(home)]
    existing = [p for p in candidates if os.path.exists(p)]
    if not existing:
        return None
    return max(existing, key=os.path.getmtime)


class EELogTailer:
    def __init__(self, path_override: Callable[[], str] = lambda: ""):
        self._handlers: list[LineHandler] = []
        self._path_override = path_override
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.path: str | None = None
        self.lines_seen = 0

    def subscribe(self, fn: LineHandler) -> None:
        self._handlers.append(fn)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="eelog", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def feed(self, line: str) -> None:
        """Dispatch one line to every subscriber (also used by tests)."""
        self.lines_seen += 1
        for fn in self._handlers:
            try:
                fn(line)
            except Exception:  # noqa: BLE001
                _log.exception("EE.log handler %s failed", getattr(fn, "__qualname__", fn))

    def _run(self) -> None:
        f = None
        pos = 0
        ino = None
        buf = b""
        last_lookup = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if f is None:
                if now - last_lookup < 5:
                    time.sleep(0.5)
                    continue
                last_lookup = now
                path = find_ee_log(self._path_override())
                if not path:
                    continue
                try:
                    f = open(path, "rb")
                    st = os.fstat(f.fileno())
                    ino = st.st_ino
                    # Start at the end: only new events matter for overlays.
                    pos = st.st_size
                    f.seek(pos)
                    self.path = path
                    _log.info("tailing %s from %d", path, pos)
                except OSError:
                    f = None
                    continue
            try:
                st = os.stat(self.path)
            except OSError:
                f.close()
                f = None
                continue
            if st.st_ino != ino or st.st_size < pos:
                # Game restarted: new or truncated log. Read the new one from the top.
                f.close()
                f = open(self.path, "rb")
                ino = os.fstat(f.fileno()).st_ino
                pos = 0
                buf = b""
                _log.info("EE.log rotated, reading from start")
            chunk = f.read()
            if not chunk:
                time.sleep(0.05)
                continue
            pos += len(chunk)
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                self.feed(raw.decode("utf-8", errors="replace").rstrip("\r"))
