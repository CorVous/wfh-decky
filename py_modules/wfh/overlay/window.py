"""A full-screen, transparent X11 window that gamescope composites above the game.

gamescope treats any X window carrying the GAMESCOPE_EXTERNAL_OVERLAY
property as an external overlay (the same mechanism MangoHud's mangoapp
uses): it is drawn over the game plane and never takes input focus. Pixels
are pushed with PutImage on a 32-bit ARGB visual, premultiplied alpha.
"""

import os
import select
import time

from PIL import Image

from .. import log

_log = log.get("overlay.window")


class OverlayWindow:
    def __init__(self, display_name: str, env: dict[str, str] | None = None):
        from Xlib import X, Xatom, display as xdisplay

        if env:
            os.environ.update(env)
        self._X = X
        self.d = xdisplay.Display(display_name)
        screen = self.d.screen()
        self.width = screen.width_in_pixels
        self.height = screen.height_in_pixels
        visual = self._argb_visual(screen)
        if visual is None:
            raise RuntimeError("no 32-bit TrueColor visual on this display")
        cmap = screen.root.create_colormap(visual, X.AllocNone)
        self.win = screen.root.create_window(
            0, 0, self.width, self.height, 0, 32, X.InputOutput, visual,
            colormap=cmap, border_pixel=0, background_pixel=0,
            event_mask=X.StructureNotifyMask | X.ExposureMask,
        )
        self.win.set_wm_name("WFHelper overlay")
        self.win.set_wm_class("wfh-overlay", "WFHelper")
        self._overlay_atom = self.d.intern_atom("GAMESCOPE_EXTERNAL_OVERLAY")
        self._cardinal = Xatom.CARDINAL
        # Starts unmapped and not claiming the overlay slot; show() claims it.
        self._set_external_overlay(0)
        self._make_click_through()
        self.gc = self.win.create_gc()
        self.mapped = False
        # PutImage requests must stay under the server's max request size.
        max_units = self.d.display.info.max_request_length
        self._max_bytes = max(64 * 1024, max_units * 4 - 64)
        self.d.flush()
        _log.info("overlay window %sx%s on %s", self.width, self.height, display_name)

    @staticmethod
    def _argb_visual(screen):
        from Xlib import X

        for depth in screen.allowed_depths:
            if depth.depth != 32:
                continue
            for v in depth.visuals:
                if v.visual_class == X.TrueColor:
                    return v.visual_id
        return None

    def _make_click_through(self) -> None:
        try:
            from Xlib.ext import shape

            if self.d.has_extension("SHAPE"):
                self.win.shape_rectangles(shape.SO.Set, shape.SK.Input, 0, 0, 0, [])
        except Exception:  # noqa: BLE001 - gamescope routes no input to overlays anyway
            _log.debug("input shape not set", exc_info=True)

    def _set_external_overlay(self, value: int) -> None:
        self.win.change_property(self._overlay_atom, self._cardinal, 32, [value])

    # gamescope draws a single external overlay per focus and picks it from
    # every window carrying isExternalOverlay, mapped or not (the flag is only
    # re-read on map or on a property change). An unmapped window that kept
    # GAMESCOPE_EXTERNAL_OVERLAY=1 would keep winning that slot and starve
    # mangoapp (the Steam performance overlay). So, like mangoapp, the flag is
    # cleared while still mapped before unmapping, and set again before map
    # (map_win reads it). While one of our layers is up, the performance
    # overlay is hidden.
    #
    # gamescope redirects substructure on the root, so map() only sends a
    # MapRequest; gamescope maps the window later from its own loop. Until then
    # the window is unviewable and has no composite backing pixmap, so any
    # PutImage is discarded. show() therefore waits (bounded) for MapNotify,
    # and pump() reports Expose/MapNotify so the caller re-pushes everything.
    MAP_TIMEOUT = 0.2

    def show(self) -> bool:
        """Map the window; True once gamescope has actually mapped it."""
        if self.mapped:
            return True
        self._set_external_overlay(1)
        self.win.map()
        self.mapped = True
        self.d.flush()
        return self._wait_mapped(self.MAP_TIMEOUT)

    def _wait_mapped(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            while self.d.pending_events():
                ev = self.d.next_event()
                if ev.type == self._X.MapNotify and self._is_ours(ev):
                    return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            select.select([self.d.fileno()], [], [], left)

    def _is_ours(self, ev) -> bool:
        w = getattr(ev, "window", None)
        return w is not None and w.id == self.win.id

    def hide(self) -> None:
        if self.mapped:
            self._set_external_overlay(0)
            self.win.unmap()
            self.mapped = False
            self.d.flush()

    def put(self, img: Image.Image, x: int, y: int) -> None:
        """Upload an RGBA region at (x, y)."""
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        data = premultiplied_bgra(img)
        w, h = img.size
        row_bytes = w * 4
        rows_per_req = max(1, self._max_bytes // row_bytes)
        for top in range(0, h, rows_per_req):
            rows = min(rows_per_req, h - top)
            chunk = data[top * row_bytes:(top + rows) * row_bytes]
            self.win.put_image(self.gc, x, y + top, w, rows, self._X.ZPixmap, 32, 0, chunk)
        self.d.flush()

    def pump(self) -> bool:
        """Drain events; True if the window was mapped or exposed and its
        contents must be pushed again in full."""
        X = self._X
        repaint = False
        while self.d.pending_events():
            ev = self.d.next_event()
            if ev.type in (X.Expose, X.MapNotify) and self._is_ours(ev):
                repaint = True
        return repaint and self.mapped

    def close(self) -> None:
        try:
            self.win.destroy()
            self.d.close()
        except Exception:  # noqa: BLE001
            pass


def premultiplied_bgra(img: Image.Image) -> bytes:
    """RGBA → premultiplied BGRA bytes (the little-endian ARGB32 X layout)."""
    import numpy as np

    a = np.asarray(img, dtype=np.uint16)
    alpha = a[..., 3:4]
    rgb = (a[..., :3] * alpha + 127) // 255
    out = np.empty(a.shape, dtype=np.uint8)
    out[..., 0] = rgb[..., 2]
    out[..., 1] = rgb[..., 1]
    out[..., 2] = rgb[..., 0]
    out[..., 3] = alpha[..., 0]
    return out.tobytes()
