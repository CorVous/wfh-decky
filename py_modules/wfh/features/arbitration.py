"""Arbitration run tracking + end-of-mission summary overlay (layer ``arbi``).

EE.log ("file" source, every line once, in order) feeds ``ArbiRunTracker``
(features/arbi_tracker.py, parser in arbi_parser.py). When a run is saved the
UI gets ``arbi.run_saved`` and, for an eligible live run (>= 2 rotations, ended
by EOM or abort, full stats), the summary panel is drawn top-center for
``arbitration.duration_s`` seconds, or a toast when the overlay window is not
connected. Runs the tracker refuses (client-side, < 2 rotations) emit
``arbi.run_skipped`` so the page can say why nothing was recorded.

The schedule and favourite-node alerts are data/arbi_schedule.py (RPCs
``world.arbi_*``, set up by data/worldstate.py); page RPCs are in menus/arbitrations.py.

Settings (``arbitration.*``): ``tracking`` (record runs), ``enabled`` (summary
overlay), ``duration_s``, ``summary_position`` (top-center | top-right |
top-left | left-middle), ``summary_scale`` (0.75-1.25).
"""

from __future__ import annotations

import atexit
import os
import threading
import time

from .. import log
from ..app import App
from . import arbi_overlay as ov
from .arbi_tracker import INACTIVITY_CHECK_S, ArbiRunTracker, slim

_log = log.get("arbitration")

LAYER = "arbi"


class ArbiController:
    def __init__(self, app: App, tracker: ArbiRunTracker):
        self.app = app
        self.tracker = tracker
        self.last_summary: dict | None = None
        self.summary_visible_until = 0.0
        tracker.on_run_saved = self._on_saved
        tracker.on_run_skipped = self._on_skipped

    def setting(self, key: str, default=None):
        return self.app.settings.get(f"arbitration.{key}", default)

    # -- tracker callbacks (worker pool / tailer thread) --------------------------
    def _on_saved(self, record: dict) -> None:
        self.app.emit("arbi.run_saved", slim(record))
        self.maybe_show_summary(record)

    def _on_skipped(self, info: dict) -> None:
        self.app.emit("arbi.run_skipped", info)

    # -- summary ------------------------------------------------------------------
    def maybe_show_summary(self, record: dict) -> bool:
        payload = ov.build_summary_payload(record)
        if payload is None:
            return False
        self.last_summary = payload
        self.app.emit("arbi.summary", payload)
        if not self.setting("enabled", True):
            return False
        return self.show(payload)

    def show(self, payload: dict, ttl: float | None = None) -> bool:
        """Draw the panel (or toast when no overlay window). True when drawn."""
        ttl = float(ttl if ttl is not None else (self.setting("duration_s", 25) or 25))
        if not self.app.settings.get("overlays_enabled", True):
            return False
        ovm = self.app.overlays
        st = ovm.status()
        if not st.get("connected") and not st.get("headless"):
            self.app.toast(*ov.summary_toast(payload))
            return False
        scale = ov.overlay_scale(self.setting("summary_scale", 1.0), tuple(ovm.size))
        img = ov.render_summary(payload, scale)
        pos = self.setting("summary_position", ov.DEFAULT_POSITION)
        x, y = ov.place(pos if pos in ov.POSITIONS else ov.DEFAULT_POSITION, tuple(ovm.size), img)
        ovm.show(LAYER, img, x, y, ttl=ttl, z=5)
        self.summary_visible_until = time.monotonic() + ttl
        return True

    def show_run(self, run_id: str) -> dict | None:
        """QAM 'show summary again' for a stored run (any source, still needs stats)."""
        run = self.tracker.get(run_id)
        if not run:
            return None
        payload = ov.build_summary_payload({**run, "source": "live",
                                            "endReason": run.get("endReason") if run.get("endReason") in
                                            ("mission-end", "aborted") else "mission-end"})
        if payload is None:
            return None
        self.last_summary = payload
        self.show(payload)
        return payload

    def dismiss(self) -> None:
        self.app.overlays.clear(LAYER)
        self.summary_visible_until = 0.0

    def summary_visible(self) -> bool:
        return LAYER in self.app.overlays.active() and time.monotonic() < self.summary_visible_until

    # -- status -------------------------------------------------------------------
    def status(self) -> dict:
        t = self.tracker
        return {
            "tracking": t.tracking_enabled,
            "initialized": t.initialized,
            "active": t.active(),
            "lastSkip": t.last_skip,
            "runCount": len(t.runs),
            "eeLog": self.app.eelog.path,
            "summaryVisible": self.summary_visible(),
            "lastSummaryId": (self.last_summary or {}).get("id"),
        }


_ctl: ArbiController | None = None


def controller() -> ArbiController | None:
    return _ctl


def setup(app: App) -> None:
    global _ctl
    tracker = ArbiRunTracker(app.runtime_dir, submit=app.submit)
    tracker.tracking_enabled = bool(app.settings.get("arbitration.tracking", True))
    _ctl = ArbiController(app, tracker)
    app.services["arbitration"] = _ctl

    app.settings.on_change(lambda data: tracker.set_tracking_enabled(
        bool(((data or {}).get("arbitration") or {}).get("tracking", True))))
    app.eelog.subscribe(tracker.process_line, source="file")

    # The schedule + alerts (data/arbi_schedule.py) are set up by data/worldstate.py.
    if os.environ.get("WFH_HEADLESS"):
        tracker.init()
        return
    # Index load + crash salvage (gzip) off the startup path; lines are ignored until ready.
    app.submit(tracker.init)
    atexit.register(tracker.shutdown)

    def inactivity_loop() -> None:
        while True:
            time.sleep(INACTIVITY_CHECK_S)
            try:
                tracker.check_inactivity()
            except Exception:  # noqa: BLE001
                _log.exception("inactivity check failed")

    threading.Thread(target=inactivity_loop, name="wfh-arbi-inactivity", daemon=True).start()
