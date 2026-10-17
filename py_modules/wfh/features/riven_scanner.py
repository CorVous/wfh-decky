"""Riven scanner overlay: EE.log -> capture -> YOLO+Paddle -> parse -> grade -> two panels.

Port of WFHelper's riven flow (docs/port/riven-scanner.md):
- riven_state:   EE.log state machine (cycle / roll / choice / close / chat-link);
- riven_scan:    16:9 canvas crops, readiness gate, recognition loop, FITS IN plate;
- riven_text:    OCR post-processing and the stat parser;
- riven_grading: roll floats, letter grades, good-roll sheet, inventory fingerprints;
- riven_market:  warframe.market similar listings;
- riven_overlay: the Pillow panels ("riven" left / "riven_right" right).

This module is the orchestrator (ipc/rivenOverlayIpc.ts): it owns the session
state, schedules the scans on timers (initial +200 ms, roll +2850 ms, choice
+1200 ms, stale rescans +1100 ms x2) and redraws the panels from that state on
every change, so a restarted overlay window simply gets the current picture.
Gamescope overlays get no input, so close / rescan / the auctions toggle and
listing details live in the QAM (RPCs below, event "riven.state").

EE.log handlers run on the tailer thread and only flip state and start timers;
captures and OCR run in the worker pool.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .. import log, rpc
from ..app import App, get_app
from . import riven_data as rd
from . import riven_grading as rg
from . import riven_market as rm
from . import riven_overlay as ov
from . import riven_scan as rs
from . import riven_text as rt
from .riven_state import Callbacks, RivenLogStateMachine, TimerFactory

_log = log.get("riven")

INITIAL_SCAN_DELAY = 0.2
ROLL_SCAN_DELAY = 2.85
CHOICE_RESCAN_DELAY = 1.2
ROLL_STALE_RESCAN_DELAY = 1.1
MAX_ROLL_STALE_RESCANS = 2
CHOSEN_HIGHLIGHT = 2.0
REFERENCE_UI_SCALE = 0.99
ARROW_EPSILON = 0.02
EVENT = "riven.state"
S = ov.STRINGS


@dataclass
class Session:
    visible: str = "none"  # none | both | left
    layout: str = "reroll"  # reroll | chat
    token: int = 0
    roll_gen: int = 0
    has_roll_result: bool = False
    initial_stats: list[dict] = field(default_factory=list)
    new_roll_stats: list[dict] = field(default_factory=list)
    weapon: str = ""
    weapon_source: str = ""  # "" | dialog | ocr | diorama | label
    weapon_label_exact: bool = False
    roll_count: int = 0
    kuva_per_roll: int = 0
    total_kuva: int = 0
    left_status: str | None = None
    right_status: str | None = None
    left_error: str | None = None
    right_error: str | None = None
    weapon_missing: bool = False
    initial_grade: dict | None = None
    roll_grade: dict | None = None
    best: dict | None = None
    listings: list[dict] | None = None
    listings_slug: str = ""
    chosen: str | None = None
    chosen_until: float = 0.0
    started_at: float = 0.0
    last_scan: dict | None = None


def _ui_scale(app: App) -> float:
    if app.settings.get("riven_scanner.ui_scale_auto", True):
        try:
            from .. import eecfg

            v = eecfg.ui_scale()
            if v:
                return float(v)
        except Exception:  # noqa: BLE001
            pass
    try:
        return float(app.settings.get("riven_scanner.ui_scale", REFERENCE_UI_SCALE) or REFERENCE_UI_SCALE)
    except (TypeError, ValueError):
        return REFERENCE_UI_SCALE


class _SerialWorker:
    """One daemon thread running submitted calls in order (EE.log callbacks)."""

    def __init__(self) -> None:
        import queue

        self._q: Any = queue.Queue()
        self._t: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        with self._lock:
            if self._t is None or not self._t.is_alive():
                self._t = threading.Thread(target=self._run, name="riven-events", daemon=True)
                self._t.start()
        self._q.put((fn, args))

    def _run(self) -> None:
        while True:
            fn, args = self._q.get()
            try:
                fn(*args)
            except Exception:  # noqa: BLE001
                _log.exception("riven event handler %s failed", getattr(fn, "__name__", fn))


class RivenController:
    def __init__(self, app: App, clock: Callable[[], float] = time.monotonic,
                 timer: Callable[[float, Callable[[], Any]], Any] | None = None,
                 submit: Callable[..., None] | None = None, sleep: Callable[[float], None] = time.sleep,
                 scanner: rs.RivenScanner | None = None, label_reader: rs.FitsInReader | None = None,
                 search: rm.SimilarSearch | None = None, fetch_goodrolls: bool = True,
                 dispatch: Callable[..., None] | None = None):
        self.app = app
        self.clock = clock
        self.timer = timer or TimerFactory()
        self.submit = submit or app.submit
        self.sleep = sleep
        self._lock = threading.RLock()
        self.s = Session()
        self._timers: dict[str, Any] = {}
        self.scanner = scanner or rs.RivenScanner(app.models_dir, self._capture, clock=clock, sleep=sleep,
                                                  canvas=self._canvas, dump_dir=self._dump_dir)
        self.labels = label_reader or rs.FitsInReader(app.models_dir, dump_dir=None)
        self._search = search
        self.fetch_goodrolls = fetch_goodrolls
        # State machine callbacks fire on the tailer thread; they are handed, in order,
        # to one serial worker so the tailer never waits on rendering.
        self._dispatch = dispatch or _SerialWorker().submit
        d = self._via_dispatch
        self.sm = RivenLogStateMachine(Callbacks(
            on_session_open=d(self.on_session_open), on_session_close=d(self.on_session_close),
            on_chat_view=d(self.on_chat_view), on_roll_pending=d(self.on_roll_pending),
            on_roll_confirmed=d(self.on_roll_confirmed), on_choice_confirmed=d(self.on_choice_confirmed),
            on_weapon_path=d(self.on_weapon_path)), clock=clock, timer=self.timer)
        self.events = 0
        self.last_result: dict | None = None

    def _via_dispatch(self, fn: Callable[..., Any]) -> Callable[..., None]:
        return lambda *a: self._dispatch(fn, *a)

    # -- settings / environment ------------------------------------------------------------

    def setting(self, key: str, default: Any = None) -> Any:
        return self.app.settings.get(f"riven_scanner.{key}", default)

    def enabled(self) -> bool:
        return bool(self.setting("enabled", True))

    def _canvas(self) -> str:
        v = str(self.setting("canvas", "16:9") or "16:9")
        return v if v in ("16:9", "full") else "16:9"

    def _dump_dir(self) -> str | None:
        if not self.setting("debug_dumps", True):
            return None
        return os.path.join(self.app.log_dir, "riven-scan-debug")

    def _capture(self) -> rs.Capture | None:
        from .. import capture

        img = capture.grab(timeout=2.0)
        # Gamescope gives the bare game plane: no desktop around it, and bar detection
        # misreads dark menus, so treat it like a client-area (window) capture.
        return rs.Capture(np.asarray(img.convert("RGB")), "window", self.clock())

    def search(self) -> rm.SimilarSearch:
        return self._search or rm.search()

    # -- EE.log ----------------------------------------------------------------------------------

    def on_line(self, line: str) -> None:
        """Tailer thread: merged feed (real-time lines first, file lines minus their echoes)."""
        # Cheap pre-filter; it must cover every riven_state pattern (open/diorama weapon "/Lotus/",
        # dialogs, HudVis "...PurchaseDialog", "OmegaRerollSelection.lua: Diorama setup", close markers).
        if ("Lotus" not in line and "Dialog" not in line and "OmegaReroll" not in line
                and "NpcManager" not in line and "ytes of recycled" not in line):
            return
        eel = self.app.eelog
        self.sm.process(line, eel.source, eel.realtime_active())

    # -- timers ----------------------------------------------------------------------------------

    def _schedule(self, name: str, delay: float, fn: Callable[[], Any]) -> None:
        self._cancel(name)

        def fire():
            with self._lock:
                if self._timers.get(name) is not handle_box[0]:
                    return
                self._timers.pop(name, None)
            self.submit(fn)

        handle_box = [None]
        handle_box[0] = self.timer(delay, fire)
        self._timers[name] = handle_box[0]

    def _cancel(self, name: str) -> None:
        t = self._timers.pop(name, None)
        if t is not None:
            try:
                t.cancel()
            except Exception:  # noqa: BLE001
                pass

    def _cancel_all(self) -> None:
        for n in list(self._timers):
            self._cancel(n)

    # -- handlers (state machine callbacks) ----------------------------------------------------

    def _snapshot_result(self) -> None:
        """Keep the finished session's cards (weapon, both sides, grades) for the QAM/page after close."""
        s = self.s
        if not s.initial_stats and not s.new_roll_stats:
            return

        def side(stats: list[dict], grade: dict | None) -> dict:
            return {"stats": list(stats), "grade": grade, "status": None, "error": None}

        self.last_result = {
            "weapon": s.weapon if s.weapon not in ("", "Riven") else None, "layout": s.layout,
            "rollCount": s.roll_count, "totalKuvaSpent": s.total_kuva,
            "left": side(s.initial_stats, s.initial_grade),
            "right": side(s.new_roll_stats, s.roll_grade) if s.new_roll_stats else None,
            "at": int(time.time() * 1000),
        }

    def _reset_session(self, visible: str, layout: str) -> None:
        self._snapshot_result()
        tok = self.s.token + 1
        gen = self.s.roll_gen + 1
        self.s = Session(visible=visible, layout=layout, token=tok, roll_gen=gen, started_at=self.clock())

    def on_session_open(self) -> None:
        if not self.enabled():
            return
        with self._lock:
            self._cancel_all()
            self.scanner.abort()
            self._reset_session("both", "reroll")
            self.s.left_status = S["scanningCurrent"]
            self.s.right_status = S["waitingForRoll"]
        _log.info("riven session open")
        self.render()
        self.trigger_initial_scan("reroll")

    def on_chat_view(self) -> None:
        if not self.enabled():
            return
        with self._lock:
            if self.s.has_roll_result:
                return
            self._cancel_all()
            self.scanner.abort()
            self._reset_session("left", "chat")
            self.s.left_status = S["scanningCurrent"]
        self.render()
        self.trigger_initial_scan("chat")

    def trigger_initial_scan(self, layout: str) -> None:
        with self._lock:
            token = self.s.token
        self._schedule("initial", INITIAL_SCAN_DELAY, lambda: self._run_initial(token, layout))

    def _run_initial(self, token: int, layout: str) -> None:
        self.scanner.reset_abort()
        res = self.scanner.scan("chat" if layout == "chat" else "initial")
        with self._lock:
            if token != self.s.token:
                return
            self.s.last_scan = self._scan_info("initial", res)
            self.s.initial_stats = list(res.stats)
            self.s.left_status = None
            if not res.stats:
                self.s.left_error = S["textTooSmall"] if res.low_confidence else S["readFailed"]
            else:
                self.s.left_error = None
        if res.stats:
            self._maybe_detect_weapon_from_text(res.text)
        self._regrade()
        self.render(toast="initial")
        if res.capture is not None:
            self._detect_fits_in(res.capture, token)

    def on_roll_pending(self, weapon: str, kuva: int) -> None:
        if not self.enabled():
            return
        # The dialog names the riven ("Burston Sati-critades") or the family; resolve to a weapon.
        name = self._weapon_from_dialog(weapon) if weapon else None
        db = rd.db()
        with self._lock:
            self.s.has_roll_result = False
            if kuva:
                self.s.kuva_per_roll = int(kuva)
            cur, src = self.s.weapon, self.s.weapon_source
            first = cur in ("", "Riven")
            same_family = bool(name) and db.family_slug(name) == db.family_slug(cur)
            # WFHelper keeps only a label variant; the diorama path is an exact uniqueName
            # too ("Kuva Sobek" on a "Sobek ..." dialog), so it is kept the same way.
            keep = not first and ((src == "label" and (self.s.weapon_label_exact or same_family))
                                  or (src == "diorama" and same_family) or name == cur)
        if name and not keep:
            self.apply_weapon(name, "dialog", announce=first)

    def _weapon_from_dialog(self, text: str) -> str | None:
        db = rd.db()
        if db.weapon(text):
            return db.weapon(text).name
        # An unresolvable dialog string must never replace a known (e.g. diorama) weapon.
        return db.find_weapon_in_text(text) or None

    def on_roll_confirmed(self) -> None:
        if not self.enabled():
            return
        with self._lock:
            if self.s.visible == "none":
                # A cycle dialog can start a session without the open line.
                self._reset_session("both", "reroll")
            self.s.roll_count += 1
            self.s.total_kuva += self.s.kuva_per_roll
            self.s.left_status = None if self.s.initial_stats else self.s.left_status
            self.s.right_status = S["scanningRoll"]
            self.s.right_error = None
            self.s.chosen = None
        self.render()
        self.trigger_roll_scan(ROLL_SCAN_DELAY)

    def trigger_roll_scan(self, delay: float = ROLL_SCAN_DELAY) -> None:
        with self._lock:
            self.s.roll_gen += 1
            serial = self.s.roll_gen
        self._schedule("roll", delay, lambda: self._run_roll(serial))

    def _run_roll(self, serial: int) -> None:
        with self._lock:
            if serial != self.s.roll_gen:
                return
            known = [list(self.s.initial_stats), list(self.s.new_roll_stats)]
        self.scanner.reset_abort()
        res = self.scanner.scan("roll")
        reason = None
        for i in range(MAX_ROLL_STALE_RESCANS):
            if serial != self.s.roll_gen:
                return
            reason = rt.roll_rescan_reason(res.stats, known)
            if not reason:
                break
            _log.warning("roll result %s (rescan %d/%d)", reason, i + 1, MAX_ROLL_STALE_RESCANS)
            self.sleep(ROLL_STALE_RESCAN_DELAY)
            if serial != self.s.roll_gen:
                return
            res = self.scanner.scan("roll")
        else:
            reason = rt.roll_rescan_reason(res.stats, known)
        with self._lock:
            if serial != self.s.roll_gen:
                return
            self.s.last_scan = self._scan_info("roll", res)
            self.s.right_status = None
            if reason or not res.stats:
                self.s.new_roll_stats = []
                self.s.right_error = S["rollReadFailed"]
                self.s.roll_grade = None
            else:
                self.s.new_roll_stats = list(res.stats)
                self.s.has_roll_result = True
                self.s.right_error = None
        if not reason and res.stats:
            self._maybe_detect_weapon_from_text(res.text)
        self._regrade()
        self.render(toast="roll")

    def on_choice_confirmed(self) -> None:
        with self._lock:
            if self.s.visible == "none":
                return
            self.s.roll_gen += 1
            self.scanner.abort()
            self._cancel("roll")
            self._cancel("initial")
            pre, new = list(self.s.initial_stats), list(self.s.new_roll_stats)
            self.s.new_roll_stats = []
            self.s.roll_grade = None
            self.s.has_roll_result = False
            self.s.chosen = "unknown"
            self.s.left_status = S["scanning"]
            self.s.right_status = S["waitingForRoll"]
            self.s.right_error = None
            token = self.s.token
        self.render()
        self._schedule("choice", CHOICE_RESCAN_DELAY, lambda: self._run_choice(token, pre, new))

    def _run_choice(self, token: int, pre: list[dict], new: list[dict]) -> None:
        self.scanner.reset_abort()
        res = self.scanner.scan("choice")
        stats = res.stats
        side = "unknown"
        if stats and pre and new:
            L = rt.score_stat_similarity(stats, pre)
            R = rt.score_stat_similarity(stats, new)
            if max(L, R) >= 12 and abs(L - R) >= 6:
                side = "right" if R > L else "left"
            _log.info("choice similarity left=%.1f right=%.1f -> %s", L, R, side)
        with self._lock:
            if token != self.s.token:
                return
            self.s.last_scan = self._scan_info("choice", res)
            if side == "right":
                self.s.initial_stats = new
            elif side == "left":
                self.s.initial_stats = pre
            elif stats:
                self.s.initial_stats = list(stats)
            self.s.left_status = None
            self.s.chosen = side
            self.s.chosen_until = self.clock() + CHOSEN_HIGHLIGHT
        self._regrade()
        self.render()
        self._schedule("chosen", CHOSEN_HIGHLIGHT, self._clear_chosen)

    def _clear_chosen(self) -> None:
        with self._lock:
            self.s.chosen = None
        self.render()

    def on_session_close(self) -> None:
        # The state machine already ended its own session when it raised this (on the tailer
        # thread). Calling sm.force_end() here, later on the serial worker, would kill a session
        # the tailer reopened in the same EE.log batch, so it is reserved for settings closes.
        self.close(force_end=False)

    def close(self, force_end: bool = False) -> None:
        with self._lock:
            self._cancel_all()
            self.scanner.abort()
            self._snapshot_result()
            tok, gen = self.s.token + 1, self.s.roll_gen + 1
            was = self.s.visible
            self.s = Session(token=tok, roll_gen=gen)
        if force_end:
            self.sm.force_end()
        self.app.overlays.clear(ov.LAYER_LEFT)
        self.app.overlays.clear(ov.LAYER_RIGHT)
        if was != "none":
            _log.info("riven session closed")
        self._emit()

    def rescan(self) -> dict:
        """Manual rescan (QAM button; WFHelper's F6)."""
        if not self.enabled():
            return {"started": None, "reason": "disabled"}
        self.sm.resume()
        with self._lock:
            visible = self.s.visible != "none"
            if visible:
                self.s.roll_gen += 1
                self.s.token += 1
                self.scanner.abort()
                self._cancel_all()
                self.s.has_roll_result = False
                self.s.new_roll_stats = []
                self.s.roll_grade = None
                self.s.left_status = S["rescanning"]
                self.s.left_error = None
                self.s.right_status = S["waitingForRoll"] if self.s.visible == "both" else None
                self.s.right_error = None
                layout = self.s.layout
        if not visible:
            self.on_session_open()
            return {"started": "session"}
        self.render()
        self.trigger_initial_scan(layout)
        return {"started": "rescan"}

    # -- weapon -------------------------------------------------------------------------------------

    def on_weapon_path(self, path: str) -> None:
        name = rd.db().name_by_unique(path)
        if not name:
            return
        with self._lock:
            cur, src = self.s.weapon, self.s.weapon_source
            if cur and cur != "Riven":
                if cur == name:
                    return
                db = rd.db()
                if db.family_slug(cur) == db.family_slug(name):
                    if src == "label":
                        return
                else:
                    _log.warning("diorama weapon %s differs from %s (%s); keeping the first", name, cur, src)
                    return
        self.apply_weapon(name, "diorama")

    def _maybe_detect_weapon_from_text(self, text: str) -> None:
        with self._lock:
            if self.s.weapon not in ("", "Riven") or not text:
                return
        hit = rd.db().find_weapon_in_text(text)
        if hit:
            self.apply_weapon(hit, "ocr")

    def apply_weapon(self, name: str, source: str, exact: bool = False, announce: bool = True) -> None:
        with self._lock:
            changed = name != self.s.weapon
            self.s.weapon = name
            self.s.weapon_source = source
            self.s.weapon_label_exact = exact
            self.s.weapon_missing = False
            token = self.s.token
        _log.info("riven weapon %s (%s%s)", name, source, ", exact" if exact else "")
        self._regrade()
        self.render()
        if changed and name != "Riven":
            self.submit(self._enrich, token, name)

    def _detect_fits_in(self, cap: rs.Capture, token: int) -> None:
        ui = _ui_scale(self.app)
        canvas = self._canvas()
        match = None
        try:
            if ui < 0.98:
                for attempt in range(3):
                    if attempt:
                        self.sleep(0.6)
                    if token != self.s.token:
                        return
                    fresh = self.scanner.capture() if attempt else cap
                    if fresh is None:
                        continue
                    match = self.labels.read_small_ui(fresh.rgb, ui, fresh.source_type, canvas)
                    if match:
                        break
            else:
                match = self.labels.read(cap.rgb, cap.source_type, canvas)
        except Exception as e:  # noqa: BLE001
            _log.warning("fits-in label read failed: %s", e)
        with self._lock:
            if token != self.s.token:
                return
            cur, src = self.s.weapon, self.s.weapon_source
        if match:
            db = rd.db()
            same = bool(cur) and db.family_slug(match.name) == db.family_slug(cur)
            if rs.should_apply_label_weapon(match, cur, src, same):
                self.apply_weapon(match.name, "label", match.exact)
            return
        if cur in ("", "Riven"):
            with self._lock:
                self.s.weapon_missing = True
            self.render()

    def set_weapon(self, name: str) -> dict:
        """QAM: pick the weapon by hand (exact, treated as a label read)."""
        w = rd.db().weapon(name)
        if not w:
            raise ValueError(f"unknown weapon: {name}")
        self.apply_weapon(w.name, "label", True)
        return self.state()

    # -- grading and enrichment ------------------------------------------------------------------

    def _grade(self, stats: list[dict], weapon: str) -> dict | None:
        try:
            return rg.grade_scanned(weapon, stats)
        except Exception:  # noqa: BLE001
            _log.exception("grading failed")
            return None

    def _regrade(self) -> None:
        with self._lock:
            weapon = self.s.weapon
            init, new = list(self.s.initial_stats), list(self.s.new_roll_stats)
        gi = self._grade(init, weapon) if init else None
        gr = self._grade(new, weapon) if new else None
        with self._lock:
            if init == self.s.initial_stats:
                self.s.initial_grade = gi
            if new == self.s.new_roll_stats:
                self.s.roll_grade = gr

    def _enrich(self, token: int, weapon: str) -> None:
        """sendWeaponEnrichment: good-roll sheet best attributes + WFM similar listings."""
        db = rd.db()
        try:
            gr = rg.goodrolls()
            if self.fetch_goodrolls:
                gr.ensure_loaded(wait=True)
            best = gr.best_attributes(weapon, db.is_melee(weapon), db)
        except Exception as e:  # noqa: BLE001
            _log.warning("good-roll sheet unavailable: %s", e)
            best = None
        with self._lock:
            if token != self.s.token or weapon != self.s.weapon:
                return
            self.s.best = best
        self._regrade()  # the attribute grade needs the sheet
        self.render()
        slug = db.family_slug(weapon)
        if not slug:
            return
        listings = self.search().search(slug)
        pool = rm.similar_pool(listings, rm.SIMILAR_LISTING_COUNT)
        with self._lock:
            if token != self.s.token or weapon != self.s.weapon:
                return
            self.s.listings = pool
            self.s.listings_slug = slug
        self.render()

    # -- views --------------------------------------------------------------------------------------

    @staticmethod
    def _scan_info(kind: str, res: rs.ScanResult) -> dict:
        return {"kind": kind, "stats": len(res.stats), "outcome": res.outcome, "elapsed_ms": res.elapsed_ms,
                "low_confidence": res.low_confidence, "ready": res.ready, "at": int(time.time() * 1000)}

    def _ranked(self, stats: list[dict]) -> list[dict] | None:
        if self.s.listings is None or not stats:
            return None
        return rm.rank_similar(stats, self.s.listings, bool(self.setting("auctions_shown", True)))

    def panel_views(self) -> tuple[dict | None, dict | None]:
        with self._lock:
            s = self.s
            if s.visible == "none":
                return None, None
            weapon = s.weapon if s.weapon not in ("", "Riven") else "Riven"
            gi, gr = s.initial_grade, s.roll_grade
            left_stats = gi["stats"] if gi else list(s.initial_stats)
            right_stats = gr["stats"] if gr else list(s.new_roll_stats)
            ls, rsc = rg.overall_score(gi), rg.overall_score(gr)
            both = ls is not None and rsc is not None and s.visible == "both"
            arrows: dict[str, int] = {}
            if both and self.setting("compare_arrows", True):
                old = {str(x["name"]).lower(): x for x in gi["stats"] if x.get("grade") not in (None, "?")}
                for x in gr["stats"]:
                    o = old.get(str(x["name"]).lower())
                    if not o or x.get("grade") in (None, "?") or o.get("positive") != x.get("positive"):
                        continue
                    fo = o["rollFloat"] if o["positive"] else 1 - o["rollFloat"]
                    fn = x["rollFloat"] if x["positive"] else 1 - x["rollFloat"]
                    if abs(fn - fo) > ARROW_EPSILON:
                        arrows[str(x["name"]).lower()] = 1 if fn > fo else -1
            chosen = s.chosen if s.chosen and self.clock() < s.chosen_until + 0.05 else None
            left = {
                "side": "left", "label": S["current"], "weapon": weapon, "roll_count": 0,
                "status": s.left_status, "error": s.left_error,
                "warning": S["weaponMissing"] if s.weapon_missing and weapon == "Riven" else None,
                "stats": left_stats, "overall_grade": gi["overallGrade"] if gi else None,
                "attribute_grade": gi.get("attributeGrade") if gi else None,
                "best": s.best, "listings": self._ranked(s.initial_stats),
                "better": both and ls > rsc + 1e-9, "chosen": chosen == "left",
            }
            if s.visible != "both":
                return left, None
            right = {
                "side": "right", "label": S["newRoll"], "weapon": weapon, "roll_count": s.roll_count,
                "status": s.right_status, "error": s.right_error, "stats": right_stats,
                "overall_grade": gr["overallGrade"] if gr else None,
                "attribute_grade": gr.get("attributeGrade") if gr else None,
                "best": s.best if right_stats else None, "listings": self._ranked(s.new_roll_stats),
                "better": both and rsc > ls + 1e-9, "chosen": chosen == "right", "arrows": arrows,
            }
            return left, right

    def _overlay_allowed(self) -> bool:
        return self.enabled() and bool(self.app.settings.get("overlays_enabled", True))

    def render(self, toast: str | None = None) -> None:
        """Redraw both panels from the session state and publish riven.state."""
        left, right = self.panel_views()
        self._emit()
        ovm = self.app.overlays
        if not self._overlay_allowed() or left is None:
            ovm.clear(ov.LAYER_LEFT)
            ovm.clear(ov.LAYER_RIGHT)
            return
        st = ovm.status()
        if not st.get("connected") and not st.get("headless"):
            if toast:
                self._toast(toast)
            return
        try:
            img = ov.render_panel(left, ov.PANEL_W, ov.LEFT_MAX_H)
            img, x, y = ov.scale_to_overlay(img, ov.LEFT_X, ov.PANEL_TOP, ovm.size)
            ovm.show(ov.LAYER_LEFT, img, x, y, ttl=None, z=8)
            if right is not None:
                img, x, y = ov.scale_to_overlay(ov.render_panel(right, ov.PANEL_W, ov.RIGHT_MAX_H),
                                                ov.RIGHT_X, ov.PANEL_TOP, ovm.size)
                ovm.show(ov.LAYER_RIGHT, img, x, y, ttl=None, z=8)
            else:
                ovm.clear(ov.LAYER_RIGHT)
        except Exception:  # noqa: BLE001
            _log.exception("riven panel render failed")

    def _toast(self, kind: str) -> None:
        with self._lock:
            s = self.s
            g = s.initial_grade if kind == "initial" else s.roll_grade
            stats = (g or {}).get("stats") or (s.initial_stats if kind == "initial" else s.new_roll_stats)
            weapon = s.weapon or "Riven"
            err = s.left_error if kind == "initial" else s.right_error
        title = f"{weapon} riven" if kind == "initial" else f"{weapon} roll {self.s.roll_count}"
        if not stats:
            self.app.toast(title, err or S["readFailed"])
            return
        body = ", ".join(f"{ov.format_value(x)} {x['name']}{' ' + x['grade'] if x.get('grade') else ''}"
                         for x in stats)
        if g and g.get("overallGrade"):
            body = f"{g['overallGrade']}: {body}"
        self.app.toast(title, body)

    def state(self) -> dict:
        with self._lock:
            s = self.s
            out = {
                "active": s.visible != "none", "visible": s.visible, "layout": s.layout,
                "weapon": s.weapon or None, "weaponSource": s.weapon_source or None,
                "weaponMissing": s.weapon_missing, "rollCount": s.roll_count, "kuvaPerRoll": s.kuva_per_roll,
                "totalKuvaSpent": s.total_kuva,
                "left": {"stats": s.initial_stats, "grade": s.initial_grade, "status": s.left_status,
                         "error": s.left_error},
                "right": {"stats": s.new_roll_stats, "grade": s.roll_grade, "status": s.right_status,
                          "error": s.right_error},
                "best": s.best, "chosen": s.chosen, "lastScan": s.last_scan,
                "lastResult": self.last_result,
                "listingsCount": None if s.listings is None else len(s.listings),
                "slug": s.listings_slug or None,
                "session": self.sm.snapshot(),
                "updatedAt": int(time.time() * 1000),
            }
        return out

    def _emit(self) -> None:
        self.events += 1
        st = self.state()
        self.last_state = st
        self.app.emit(EVENT, st)

    def similar(self, side: str = "left", auctions: bool | None = None) -> dict:
        with self._lock:
            stats = list(self.s.initial_stats if side != "right" else self.s.new_roll_stats)
            pool = self.s.listings
            weapon = self.s.weapon
        shown = bool(self.setting("auctions_shown", True)) if auctions is None else bool(auctions)
        rows = rm.rank_similar(stats, pool or [], shown) if pool is not None else []
        for r in rows:
            r["url"] = rm.auction_url(r["id"])
        return {"weapon": weapon or None, "loaded": pool is not None, "auctionsShown": shown, "rows": rows,
                "error": self.search().last_error}

    def status(self) -> dict:
        return {
            "enabled": self.enabled(),
            "ocr_available": self.scanner.ocr_available(),
            "data": rd.status(),
            "goodrolls": rg.goodrolls().status(),
            "overlay": self.app.overlays.status(),
            "realtime_active": self.app.eelog.realtime_active(),
            "ui_scale": _ui_scale(self.app),
            "canvas": self._canvas(),
            "last_capture_ms": self.scanner.last_capture_ms,
        }

    def scan_file(self, path: str, profile: str = "initial", source_type: str = "window") -> dict:
        """Debug: run a saved screenshot through one scan profile (and the FITS IN reader)."""
        from PIL import Image

        img = np.asarray(Image.open(path).convert("RGB"))
        res = self.scanner.scan_image(img, profile, source_type)
        label = None
        try:
            m = self.labels.read(img, source_type, self._canvas())
            label = {"name": m.name, "exact": m.exact} if m else None
        except Exception as e:  # noqa: BLE001
            _log.warning("fits-in read failed: %s", e)
        return {"stats": res.stats, "text": res.text, "lowConfidence": res.low_confidence, "outcome": res.outcome,
                "elapsedMs": res.elapsed_ms, "label": label,
                "formatted": [rt.format_stat(x) for x in res.stats]}


_ctl: RivenController | None = None


def controller() -> RivenController:
    if _ctl is None:
        raise RuntimeError("riven scanner not initialised")
    return _ctl


def setup(app: App) -> None:
    global _ctl
    _ctl = RivenController(app)
    app.services["riven_scanner"] = _ctl
    app.eelog.subscribe(_ctl.on_line)
    if _ctl.enabled() and not app.overlays.headless:
        try:
            from .. import ocr

            ocr.warmup(app.models_dir, detector=True)
        except Exception:  # noqa: BLE001
            _log.exception("ocr warmup failed")

    def on_settings(data: dict) -> None:
        sec = data.get("riven_scanner") or {}
        # Runs on the event loop (settings.update): rendering goes to the worker pool.
        if not sec.get("enabled", True):
            if _ctl.s.visible != "none" or _ctl.sm.active:
                app.submit(_ctl.close, True)
            return
        if _ctl.s.visible != "none":
            app.submit(_ctl.render)

    app.settings.on_change(on_settings)


# ---- RPC ------------------------------------------------------------------------------------------

@rpc.method("riven.state")
def _rpc_state() -> dict:
    """The current riven session (both cards, grades, weapon, roll count) for the QAM."""
    return controller().state()


@rpc.method("riven.status")
async def _rpc_status() -> dict:
    return await get_app().run_blocking(controller().status)


@rpc.method("riven.rescan")
async def _rpc_rescan() -> dict:
    return await get_app().run_blocking(controller().rescan)


@rpc.method("riven.close")
async def _rpc_close() -> None:
    await get_app().run_blocking(controller().close, False)


@rpc.method("riven.set_weapon")
async def _rpc_set_weapon(name: str) -> dict:
    return await get_app().run_blocking(controller().set_weapon, name)


@rpc.method("riven.weapons")
def _rpc_weapons(query: str = "", limit: int = 30) -> list[str]:
    """Riven-capable weapon names, filtered by a case-insensitive substring."""
    q = str(query or "").lower().strip()
    names = [n for n in rd.db().all_weapon_names() if rd.db().resolve_riven_type(n)]
    return sorted((n for n in names if q in n.lower()), key=lambda n: (not n.lower().startswith(q), n))[:limit]


@rpc.method("riven.similar")
async def _rpc_similar(side: str = "left", auctions: bool | None = None) -> dict:
    return controller().similar(side, auctions)


@rpc.method("riven.scan_file")
async def _rpc_scan_file(path: str, profile: str = "initial", source_type: str = "window") -> dict:
    if not path or not os.path.isfile(path):
        raise ValueError(f"no such file: {path}")
    if profile not in rs.PROFILES:
        raise ValueError(f"unknown profile: {profile}")
    return await get_app().run_blocking(controller().scan_file, path, profile, source_type)


@rpc.method("riven.data_status")
def _rpc_data_status() -> dict:
    return {"data": rd.status(), "goodrolls": rg.goodrolls().status()}


@rpc.method("riven.data_refresh")
async def _rpc_data_refresh(force: bool = False) -> dict:
    """Fetch the latest warframe-public-export-plus from npm into the runtime dir."""
    a = get_app()
    return await a.run_blocking(rd.refresh, os.path.join(a.runtime_dir, "data"), bool(force))


@rpc.method("riven.goodrolls_refresh")
async def _rpc_goodrolls_refresh() -> dict:
    g = rg.goodrolls()
    await get_app().run_blocking(lambda: g.ensure_loaded(force=True, wait=True))
    return g.status()
