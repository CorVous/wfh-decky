"""Finds the gamescope session, the Steam user and the Warframe process.

The worker runs as root (game memory and /proc access), so the X display,
Wayland socket and home directory of the logged-in Steam user are read from
the environment of that user's own processes.
"""

import os
import pwd
import time
from dataclasses import dataclass

from . import log

_log = log.get("session")

WARFRAME_STEAM_APPID = "230410"
WARFRAME_EXE = "Warframe.x64.exe"


@dataclass
class SessionEnv:
    uid: int
    gid: int
    user: str
    home: str
    display: str
    xauthority: str | None
    xdg_runtime_dir: str
    wayland_display: str | None

    def x_env(self) -> dict[str, str]:
        env = {"DISPLAY": self.display, "HOME": self.home, "XDG_RUNTIME_DIR": self.xdg_runtime_dir}
        if self.xauthority:
            env["XAUTHORITY"] = self.xauthority
        if self.wayland_display:
            env["WAYLAND_DISPLAY"] = self.wayland_display
        return env


def deck_user() -> pwd.struct_passwd:
    name = os.environ.get("DECKY_USER") or os.environ.get("SUDO_USER") or "deck"
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return pwd.getpwuid(os.getuid())


def _read_environ(pid: str) -> dict[str, str]:
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


def _comm(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def _pids() -> list[str]:
    return [p for p in os.listdir("/proc") if p.isdigit()]


_SESSION_COMMS = ("steam", "steamwebhelper", "gamescope-wl", "gamescope", "reaper")


def find_session_env() -> SessionEnv | None:
    """Environment of the gamescope session (DISPLAY :0 in SteamOS game mode)."""
    user = deck_user()
    best: dict[str, str] | None = None
    for pid in _pids():
        if _comm(pid) not in _SESSION_COMMS:
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != user.pw_uid:
                continue
        except OSError:
            continue
        env = _read_environ(pid)
        if "DISPLAY" not in env:
            continue
        if best is None or ("GAMESCOPE_WAYLAND_DISPLAY" in env and "GAMESCOPE_WAYLAND_DISPLAY" not in best):
            best = env
    if best is None:
        if os.environ.get("DISPLAY"):
            best = dict(os.environ)
        else:
            return None
    return SessionEnv(
        uid=user.pw_uid,
        gid=user.pw_gid,
        user=user.pw_name,
        home=user.pw_dir,
        display=best.get("DISPLAY", ":0"),
        xauthority=best.get("XAUTHORITY"),
        xdg_runtime_dir=best.get("XDG_RUNTIME_DIR", f"/run/user/{user.pw_uid}"),
        wayland_display=best.get("GAMESCOPE_WAYLAND_DISPLAY"),
    )


def find_warframe_pid() -> int | None:
    """PID of the running Warframe.x64.exe (under Proton/Wine)."""
    for pid in _pids():
        comm = _comm(pid)
        if not comm.startswith("Warframe"):
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if WARFRAME_EXE.lower() in cmd.lower():
            return int(pid)
    return None


_wf_cache: tuple[float, int | None] = (0.0, None)


def warframe_running() -> bool:
    """Cached for 5s; cheap enough to call from UI polls."""
    global _wf_cache
    now = time.monotonic()
    if now - _wf_cache[0] > 5:
        _wf_cache = (now, find_warframe_pid())
    return _wf_cache[1] is not None
