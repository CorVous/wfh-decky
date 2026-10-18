"""Finds the gamescope session, the Steam user and the Warframe process.

The worker runs as root (game memory and /proc access), so the X display,
Wayland socket and home directory of the logged-in Steam user are read from
the environment of that user's own processes.
"""

import ctypes
import os
import platform
import pwd
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, TypeVar

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


T = TypeVar("T")

# Raw syscall numbers. Calling them through libc.syscall() changes the
# credentials of the calling thread only; os.setresuid() would go through
# glibc, which applies the change to every thread of the worker.
_SYS_SETRESUID = {"x86_64": 117, "aarch64": 147}
_SYS_SETRESGID = {"x86_64": 119, "aarch64": 149}


def _thread_setres(uid_nr: int, gid_nr: int, uid: int, gid: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    # Order matters: drop the gid while still root, restore the uid first.
    if uid != 0:
        if libc.syscall(gid_nr, -1, gid, -1) != 0 or libc.syscall(uid_nr, -1, uid, -1) != 0:
            raise OSError(ctypes.get_errno(), "setresuid/setresgid failed")
    else:
        if libc.syscall(uid_nr, -1, 0, -1) != 0 or libc.syscall(gid_nr, -1, gid, -1) != 0:
            raise OSError(ctypes.get_errno(), "restoring root credentials failed")


@contextmanager
def thread_as_user(uid: int, gid: int):
    """Run the body with this thread's effective uid/gid set to the user's.

    gamescope's Xwayland only accepts clients running as the session user
    (mangoapp gets in the same way), so the root worker opens its X
    connections as that user. The peer credentials the X server checks are
    taken at connect(), so the connection keeps working after root returns.
    """
    arch = platform.machine()
    if os.geteuid() != 0 or uid == 0 or arch not in _SYS_SETRESUID:
        yield
        return
    real_gid = os.getegid()
    _thread_setres(_SYS_SETRESUID[arch], _SYS_SETRESGID[arch], uid, gid)
    try:
        yield
    finally:
        _thread_setres(_SYS_SETRESUID[arch], _SYS_SETRESGID[arch], 0, real_gid)


def x_connect(env: "SessionEnv", connect: Callable[[], T]) -> T:
    """Open an X connection to the game-mode display.

    Tries as the worker (root) first, then as the session user, which is
    what gamescope's Xwayland allows.
    """
    os.environ.update(env.x_env())
    try:
        return connect()
    except Exception as root_error:  # noqa: BLE001
        if os.geteuid() != 0 or env.uid == 0:
            raise
        _log.info("X connect as root failed (%s); retrying as %s", root_error, env.user)
        with thread_as_user(env.uid, env.gid):
            result = connect()
        _log.info("X connect as %s worked", env.user)
        return result


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


def _xwayland_auth(display: str, uid: int) -> str | None:
    """The -auth cookie file of the user's Xwayland serving `display`, if any."""
    for pid in _pids():
        if _comm(pid) != "Xwayland":
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = [a.decode(errors="replace") for a in f.read().split(b"\0")]
        except OSError:
            continue
        if display not in args or "-auth" not in args:
            continue
        i = args.index("-auth")
        if i + 1 < len(args) and os.path.isfile(args[i + 1]):
            return args[i + 1]
    return None


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
    display = best.get("DISPLAY", ":0")
    return SessionEnv(
        uid=user.pw_uid,
        gid=user.pw_gid,
        user=user.pw_name,
        home=user.pw_dir,
        display=display,
        xauthority=best.get("XAUTHORITY") or _xwayland_auth(display, user.pw_uid),
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
