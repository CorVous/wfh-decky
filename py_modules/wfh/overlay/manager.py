"""Owns the overlay window and composites named layers onto it.

Features call `overlays.show(name, image, x, y, ttl=...)` and
`overlays.clear(name)`. Coordinates are in screen pixels of the game output;
`overlays.size` gives that size. All X traffic happens on one background
thread. Without a gamescope display (development, tests) the manager runs
headless and only keeps the composite in memory for `snapshot()`.
"""

import io
import base64
import queue
import threading
import time
from dataclasses import dataclass

from PIL import Image

from .. import log
from ..session import find_session_env

_log = log.get("overlay")

DEFAULT_SIZE = (1280, 800)


@dataclass
class Layer:
    image: Image.Image
    x: int
    y: int
    expires: float | None
    z: int = 0

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.image.width, self.y + self.image.height)


def _union(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


class OverlayManager:
    def __init__(self, headless: bool = False):
        self._layers: dict[str, Layer] = {}
        self._lock = threading.Lock()
        self._dirty = None
        self._q: queue.Queue = queue.Queue()
        self._win = None
        self.headless = headless
        self.enabled = True
        self.opacity = 1.0
        self.size = DEFAULT_SIZE
        self._canvas = Image.new("RGBA", self.size, (0, 0, 0, 0))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="overlay", daemon=True)
        self._thread.start()

    # Public API ---------------------------------------------------------

    def show(self, name: str, image: Image.Image, x: int, y: int, ttl: float | None = None, z: int = 0) -> None:
        if image.mode != "RGBA":
            image = image.convert("RGBA")
        with self._lock:
            old = self._layers.get(name)
            layer = Layer(image, int(x), int(y), time.monotonic() + ttl if ttl else None, z)
            self._layers[name] = layer
            self._dirty = _union(self._dirty, _union(old.box if old else None, layer.box))
        self._q.put("redraw")

    def clear(self, name: str) -> None:
        with self._lock:
            old = self._layers.pop(name, None)
            if old is None:
                return
            self._dirty = _union(self._dirty, old.box)
        self._q.put("redraw")

    def clear_all(self) -> None:
        with self._lock:
            names = list(self._layers)
        for n in names:
            self.clear(n)

    def active(self) -> list[str]:
        with self._lock:
            return sorted(self._layers)

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        with self._lock:
            self._dirty = (0, 0, *self.size)
        self._q.put("redraw")

    def snapshot_png(self) -> str:
        """Current composite as a data: URL, for the QAM preview."""
        with self._lock:
            img = self._canvas.copy()
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    def status(self) -> dict:
        return {
            "connected": self._win is not None,
            "headless": self.headless,
            "enabled": self.enabled,
            "size": list(self.size),
            "layers": self.active(),
        }

    def stop(self) -> None:
        self._stop.set()
        self._q.put("stop")

    # Internals ----------------------------------------------------------

    def _connect(self) -> None:
        if self.headless or self._win is not None:
            return
        env = find_session_env()
        if env is None:
            return
        try:
            from .window import OverlayWindow

            self._win = OverlayWindow(env.display, env.x_env())
            self.size = (self._win.width, self._win.height)
            with self._lock:
                self._canvas = Image.new("RGBA", self.size, (0, 0, 0, 0))
                self._dirty = (0, 0, *self.size)
        except Exception as e:  # noqa: BLE001
            _log.warning("overlay window unavailable: %s", e)
            self._win = None

    def _expire(self) -> None:
        now = time.monotonic()
        with self._lock:
            gone = [n for n, l in self._layers.items() if l.expires and l.expires <= now]
        for n in gone:
            self.clear(n)

    def _compose(self):
        with self._lock:
            box = self._dirty
            self._dirty = None
            if box is None:
                return None
            w, h = self.size
            box = (max(0, box[0]), max(0, box[1]), min(w, box[2]), min(h, box[3]))
            if box[2] <= box[0] or box[3] <= box[1]:
                return None
            region = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
            if self.enabled:
                for layer in sorted(self._layers.values(), key=lambda l: l.z):
                    lb = layer.box
                    if lb[2] <= box[0] or lb[0] >= box[2] or lb[3] <= box[1] or lb[1] >= box[3]:
                        continue
                    img = layer.image
                    if self.opacity < 1.0:
                        img = img.copy()
                        img.putalpha(img.getchannel("A").point(lambda v: int(v * self.opacity)))
                    region.alpha_composite(img, (lb[0] - box[0], lb[1] - box[1]))
            self._canvas.paste(region, box[:2])
            any_layers = bool(self._layers) and self.enabled
        return region, box, any_layers

    def _run(self) -> None:
        last_connect = 0.0
        while not self._stop.is_set():
            try:
                self._q.get(timeout=0.25)
            except queue.Empty:
                pass
            now = time.monotonic()
            if self._win is None and not self.headless and now - last_connect > 5:
                last_connect = now
                self._connect()
            self._expire()
            out = self._compose()
            if out is None or self._win is None:
                continue
            region, box, any_layers = out
            try:
                if not any_layers:
                    self._win.hide()
                elif not self._win.mapped:
                    # An unmapped window keeps no contents: push the whole canvas.
                    self._win.show()
                    with self._lock:
                        full = self._canvas.copy()
                    self._win.put(full, 0, 0)
                else:
                    self._win.put(region, box[0], box[1])
                self._win.pump()
            except Exception as e:  # noqa: BLE001 - X server went away; reconnect later
                _log.warning("overlay draw failed, reconnecting: %s", e)
                try:
                    self._win.close()
                finally:
                    self._win = None
        if self._win is not None:
            self._win.close()
