"""Screen capture of the game plane through gamescope's screenshot support.

Preferred: `gamescopectl screenshot <path> 1` (1 = base plane only, i.e. the
game without Steam's or our overlay). Fallback: set the
GAMESCOPECTRL_REQUEST_SCREENSHOT property on the root window of the gamescope
X display, which writes /tmp/gamescope.png and deletes the property when done.
Both paths give a PNG at the game's output resolution (1280x800 on a Deck).
"""

import os
import shutil
import subprocess
import time

from PIL import Image

from . import log
from .session import SessionEnv, find_session_env

_log = log.get("capture")

BASE_PLANE_ONLY = 1
_OUT_DIR = "/tmp/wfh-decky"


class CaptureError(RuntimeError):
    pass


def _png_complete(path: str) -> bool:
    try:
        size = os.path.getsize(path)
        if size < 64:
            return False
        with open(path, "rb") as f:
            f.seek(size - 12)
            return f.read(12)[4:8] == b"IEND"
    except OSError:
        return False


def _wait_for(path: str, since: float, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if os.path.getmtime(path) >= since and _png_complete(path):
                return True
        except OSError:
            pass
        time.sleep(0.02)
    return False


def _gamescopectl(env: SessionEnv, path: str, timeout: float) -> bool:
    exe = shutil.which("gamescopectl")
    if not exe or not env.wayland_display:
        return False
    since = time.time() - 0.01
    run_env = {**os.environ, **env.x_env(), "WAYLAND_DISPLAY": env.wayland_display}
    try:
        subprocess.run(
            [exe, "screenshot", path, str(BASE_PLANE_ONLY)],
            env=run_env, timeout=timeout, capture_output=True, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _log.warning("gamescopectl failed: %s", e)
        return False
    return _wait_for(path, since, timeout)


def _x_property(env: SessionEnv, timeout: float) -> str | None:
    from Xlib import X, Xatom, display as xdisplay

    path = "/tmp/gamescope.png"
    since = time.time() - 0.01
    os.environ.update(env.x_env())
    d = xdisplay.Display(env.display)
    try:
        root = d.screen().root
        atom = d.intern_atom("GAMESCOPECTRL_REQUEST_SCREENSHOT")
        root.change_property(atom, Xatom.CARDINAL, 32, [BASE_PLANE_ONLY], X.PropModeReplace)
        d.flush()
    finally:
        d.close()
    return path if _wait_for(path, since, timeout) else None


def grab(timeout: float = 2.0) -> Image.Image:
    """Full-screen RGB image of the game. Raises CaptureError when unavailable."""
    env = find_session_env()
    if env is None:
        raise CaptureError("no gamescope session found")
    os.makedirs(_OUT_DIR, exist_ok=True)
    os.chmod(_OUT_DIR, 0o777)
    path = os.path.join(_OUT_DIR, "capture.png")
    t0 = time.monotonic()
    src = path if _gamescopectl(env, path, timeout) else None
    if src is None:
        src = _x_property(env, timeout)
    if src is None:
        raise CaptureError("gamescope did not write a screenshot")
    img = Image.open(src)
    img.load()
    _log.debug("captured %sx%s in %.0fms", img.width, img.height, (time.monotonic() - t0) * 1000)
    return img.convert("RGB")
