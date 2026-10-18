"""EE.log riven state machine (port of WFHelper services/rivenLogStateMachine.ts).

Turns EE.log lines into riven-screen callbacks: session open/close, chat-link
view, roll pending (cycle dialog), roll confirmed / choice confirmed
(Dialog::SendResult(4)), diorama weapon path. docs/port/riven-scanner.md §2.

process(line, source, realtime_active) runs on the EE.log tailer thread and
only flips a few fields and fires callbacks; the callbacks themselves must
return fast (the orchestrator schedules its scans on timers / the pool).

Timestamps use an injected monotonic clock in seconds; "never" is None, so a
clock that starts at 0 (tests) behaves like JS Date.now() starting at epoch.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .. import log

_log = log.get("riven.state")

# ---- regexes (rivenLogStateMachine.ts:33-54, verbatim) --------------------------------

SESSION_OPEN = re.compile(r"Sys \[Info\]: Created /Lotus/Interface/OmegaRerollSelection\.swf")
SESSION_CLOSE = re.compile(r"NpcManager::ClearAgents\(\) ReadyToCreateAgents = false")
HUD_VIS = re.compile(r"ThemedDetailedPurchaseDialog\.lua: DBG: HudVis (\d+)")
POPULATE_RIVEN = re.compile(r"ThemedDetailedPurchaseDialog\.lua: PopulateInfo->/Lotus/StoreItems/Upgrades/Mods/Randomized/")
CYCLE_CONFIRM_EN = re.compile(r"Dialog::CreateOkCancel\(description=Are you sure you want to cycle (.+?) for ([\d,. ]+)\?")
CHOICE_CONFIRM_EN = re.compile(r"Dialog::CreateOkCancel\(description=Cycle Riven into current selection\?")
GENERIC_DIALOG = re.compile(r"Dialog::CreateOkCancel\(")
GENERIC_DIALOG_NON_INTERACTIVE = re.compile(r"leftItem=nil")
SEND_RESULT = re.compile(r"Dialog\.lua:\s*Dialog::SendResult\((\d+)\)")
DIORAMA_SETUP = re.compile(r"OmegaRerollSelection\.lua.*Diorama setup", re.I)
DIORAMA_WEAPON_LOAD = re.compile(r"(?:ResourceLoader|Resloader|Resource load completed) 0x[0-9A-Fa-f]+ \((/Lotus/Weapons/[^)]+)\)")
RECYCLED_EFFECTS = re.compile(r"ytes of recycled effects")

# ---- constants (seconds; :87-135) ---------------------------------------------------------

SESSION_IDLE_TIMEOUT = 600.0
SESSION_MAX = 30 * 60.0
SEND_RESULT_COOLDOWN = 0.4
GENERIC_DIALOG_COOLDOWN = 0.6
CHOICE_DIALOG_COOLDOWN = 2.0
SESSION_OPEN_COOLDOWN = 2.0
CLOSE_MARKER_SPACING = 2.0
DIORAMA_DEDUP = 2.0
WEAPON_PATH_WINDOW = 15.0
FORCE_END_COOLDOWN = 5.0
CHAT_POPULATE_WINDOW = 2.0

SOURCE_FILE = "file"
SOURCE_REALTIME = "realtime"


@dataclass
class Callbacks:
    on_session_open: Callable[[], Any] | None = None
    on_session_close: Callable[[], Any] | None = None
    on_chat_view: Callable[[], Any] | None = None
    on_roll_pending: Callable[[str, int], Any] | None = None
    on_roll_confirmed: Callable[[], Any] | None = None
    on_choice_confirmed: Callable[[], Any] | None = None
    on_diorama_setup: Callable[[], Any] | None = None
    on_weapon_path: Callable[[str], Any] | None = None


def _since(now: float, at: float | None) -> float:
    """now - at, with "never" infinitely long ago (JS timestamps start at 0 = epoch)."""
    return float("inf") if at is None else now - at


def parse_cost(text: str) -> int:
    """'4,000' / '3.500' / '4 000' -> 4000 (parseInt after stripping separators)."""
    m = re.match(r"\d+", re.sub(r"[,. ]", "", text or ""))
    return int(m.group(0)) if m else 0


class TimerFactory:
    """threading.Timer wrapper; tests inject a fake with the same call shape."""

    def __call__(self, delay: float, fn: Callable[[], Any]):
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()
        return t


class RivenLogStateMachine:
    def __init__(self, callbacks: Callbacks | None = None, clock: Callable[[], float] = time.monotonic,
                 timer: Callable[[float, Callable[[], Any]], Any] | None = None):
        self.cb = callbacks or Callbacks()
        self.clock = clock
        self._timer_factory = timer or TimerFactory()
        self._lock = threading.RLock()
        self.reset()

    # -- state ---------------------------------------------------------------------------

    def reset(self) -> None:
        """resetRivenState (callbacks are kept)."""
        self.pending_dialog: str | None = None  # None | "roll_confirm" | "choice"
        self.next_dialog = "cycle"
        self.active = False
        self.started_at: float | None = None
        self.diorama_ready = False
        self.weapon_path_sent = False
        self.chat_view_active = False
        self.chat_hud_vis_level = 0
        self.chat_weapon_path: str | None = None
        self.last_send_result_at: float | None = None
        self.last_generic_dialog_at: float | None = None
        self.last_choice_dialog_at: float | None = None
        self.last_open_at: float | None = None
        self.last_suppressed_open_log_at: float | None = None
        self.last_diorama_at: float | None = None
        self.last_close_marker_at: float | None = None
        self.last_close_dispatch_at: float | None = None
        self.force_ended_at: float | None = None
        self.last_hud_vis = 0
        self.last_hud_vis_increase_at: float | None = None
        self._cancel_idle()

    def snapshot(self) -> dict:
        return {"active": self.active, "chat_view": self.chat_view_active, "pending_dialog": self.pending_dialog,
                "next_dialog": self.next_dialog, "diorama_ready": self.diorama_ready}

    def _fire(self, name: str, *args) -> None:
        fn = getattr(self.cb, name, None)
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - a broken handler must not stop the state machine
            _log.exception("riven callback %s failed", name)

    # -- idle backstop -------------------------------------------------------------------

    _idle_timer: Any = None

    def _cancel_idle(self) -> None:
        t = getattr(self, "_idle_timer", None)
        self._idle_timer = None
        if t is not None:
            try:
                t.cancel()
            except Exception:  # noqa: BLE001
                pass

    def _reset_idle(self) -> None:
        self._cancel_idle()
        token = object()
        self._idle_token = token

        def fire() -> None:
            with self._lock:
                if getattr(self, "_idle_token", None) is not token:
                    return
                self._idle_timer = None
                self._end_session_state()
            _log.info("riven session idle timeout -> dispatching overlay close")
            self._fire("on_session_close")

        self._idle_timer = self._timer_factory(SESSION_IDLE_TIMEOUT, fire)

    def _end_session_state(self) -> None:
        self.active = False
        self.started_at = None
        self.diorama_ready = False
        self.pending_dialog = None
        self.next_dialog = "cycle"
        self.weapon_path_sent = False
        # A closed session must not leak its cooldowns into the next open.
        self.last_open_at = None
        self.last_diorama_at = None
        self._idle_token = None
        self._cancel_idle()

    def _force_end_if_expired(self) -> bool:
        if not self.active or self.started_at is None:
            return False
        now = self.clock()
        if now - self.started_at < SESSION_MAX:
            return False
        _log.info("riven session exceeded %d min cap - force closing", SESSION_MAX // 60)
        self._end_session_state()
        self.force_ended_at = now
        self._fire("on_session_close")
        return True

    # -- the line processor ----------------------------------------------------------------

    def process(self, line: str, source: str = SOURCE_FILE, realtime_active: bool = False) -> bool:
        """processRivenPatterns. Returns whether a SendResult was consumed by the riven flow."""
        with self._lock:
            return self._process(line, source, realtime_active)

    def _process(self, line: str, source: str, realtime_active: bool) -> bool:
        skip = realtime_active and source == SOURCE_FILE
        self._force_end_if_expired()

        if not skip and SESSION_OPEN.search(line):
            now = self.clock()
            if not self.active and _since(now, self.last_open_at) >= SESSION_OPEN_COOLDOWN:
                self.last_open_at = now
                self.active = True
                self.started_at = now
                self.chat_view_active = False
                self.chat_hud_vis_level = 0
                self.chat_weapon_path = None
                self.next_dialog = "cycle"
                self.pending_dialog = None
                self.weapon_path_sent = False
                self._reset_idle()
                _log.info("riven rolling screen opened -> session open")
                self._fire("on_session_open")
            elif _since(now, self.last_suppressed_open_log_at) >= SESSION_OPEN_COOLDOWN:
                self.last_suppressed_open_log_at = now
                _log.debug("riven session open suppressed (cooldown)")

        weapon_match = DIORAMA_WEAPON_LOAD.search(line)
        if (self.active and not self.weapon_path_sent and self.started_at is not None
                and self.clock() - self.started_at < WEAPON_PATH_WINDOW and weapon_match):
            self.weapon_path_sent = True
            _log.info("riven diorama weapon load: %s", weapon_match.group(1))
            self._fire("on_weapon_path", weapon_match.group(1))

        # Readiness from either source so real-time/file ordering cannot block roll OCR.
        if self.active and DIORAMA_SETUP.search(line):
            now = self.clock()
            if _since(now, self.last_diorama_at) >= DIORAMA_DEDUP:
                self.last_diorama_at = now
                self.diorama_ready = True
                self._reset_idle()
                self._fire("on_diorama_setup")

        # Close lines also fire during screen loading, so they need the diorama.
        if (not skip and self.active and self.diorama_ready
                and (SESSION_CLOSE.search(line) or RECYCLED_EFFECTS.search(line))):
            now = self.clock()
            trailing = (realtime_active and _since(now, self.last_close_marker_at) < CLOSE_MARKER_SPACING
                        and _since(now, self.last_close_dispatch_at) < 2 * CLOSE_MARKER_SPACING)
            self.last_close_marker_at = now
            if trailing:
                _log.debug("riven close marker ignored - trailing the previous close")
            else:
                _log.info("riven session close detected")
                self.last_close_dispatch_at = now
                self._end_session_state()
                self._fire("on_session_close")

        hud = HUD_VIS.search(line)
        if not skip and hud and not self.active:
            v = int(hud.group(1))
            if self.chat_view_active and v < self.chat_hud_vis_level:
                self.chat_view_active = False
                self.chat_hud_vis_level = 0
                self.chat_weapon_path = None
                self.last_hud_vis_increase_at = None
                _log.info("riven chat-link view closed below its HudVis level")
                self._fire("on_session_close")
            elif v > self.last_hud_vis:
                if not self.chat_view_active:
                    self.chat_weapon_path = None
                self.last_hud_vis_increase_at = self.clock()
            elif not self.chat_view_active and v < self.last_hud_vis:
                self.chat_weapon_path = None
                self.last_hud_vis_increase_at = None
            self.last_hud_vis = v

        in_chat_window = (self.last_hud_vis_increase_at is not None
                          and self.clock() - self.last_hud_vis_increase_at < CHAT_POPULATE_WINDOW)
        if not skip and not self.active and not self.chat_view_active and in_chat_window and weapon_match:
            self.chat_weapon_path = weapon_match.group(1)

        if (not skip and not self.active and not self.chat_view_active and in_chat_window
                and POPULATE_RIVEN.search(line)):
            path = self.chat_weapon_path
            self.chat_weapon_path = None
            self.chat_view_active = True
            self.chat_hud_vis_level = self.last_hud_vis
            _log.info("riven chat-link view confirmed")
            self._fire("on_chat_view")
            if path:
                self._fire("on_weapon_path", path)

        handled = skip
        force_guard = (not self.active and _since(self.clock(), self.force_ended_at) < FORCE_END_COOLDOWN)
        cycle = CYCLE_CONFIRM_EN.search(line) if not skip else None
        if cycle and not force_guard:
            handled = True
            self.active = True
            self._reset_idle()
            self.pending_dialog = "roll_confirm"
            weapon = cycle.group(1).strip()
            cost = parse_cost(cycle.group(2))
            _log.info("riven roll pending: weapon=%s cost=%d", weapon, cost)
            self._fire("on_roll_pending", weapon, cost)

        if not handled and not skip and CHOICE_CONFIRM_EN.search(line) and not force_guard:
            handled = True
            self.active = True
            self._reset_idle()
            self.pending_dialog = "choice"
            now = self.clock()
            if _since(now, self.last_choice_dialog_at) >= CHOICE_DIALOG_COOLDOWN:
                self.last_choice_dialog_at = now
                _log.info("riven choice dialog detected (English)")

        if (not handled and self.active and self.pending_dialog is None
                and _since(self.clock(), self.last_send_result_at) >= GENERIC_DIALOG_COOLDOWN
                and GENERIC_DIALOG.search(line) and not GENERIC_DIALOG_NON_INTERACTIVE.search(line)):
            self._reset_idle()
            self.last_generic_dialog_at = self.clock()
            if self.next_dialog == "cycle":
                self.pending_dialog = "roll_confirm"
                _log.info("riven roll pending (generic dialog)")
                self._fire("on_roll_pending", "", 0)
            else:
                self.pending_dialog = "choice"
                _log.info("riven choice dialog detected (generic)")

        consumed = False
        sr = SEND_RESULT.search(line)
        if sr and self.active and skip:
            consumed = True
        if sr and not skip and (self.pending_dialog is not None or self.active):
            consumed = True
            if self.active:
                self._reset_idle()
            code = sr.group(1)
            if self.pending_dialog is not None:
                if code == "4":
                    now = self.clock()
                    if _since(now, self.last_send_result_at) >= SEND_RESULT_COOLDOWN:
                        self.last_send_result_at = now
                        if self.pending_dialog == "roll_confirm":
                            self.next_dialog = "choice"
                            _log.info("riven roll confirmed -> roll scan")
                            self._fire("on_roll_confirmed")
                        elif self.pending_dialog == "choice":
                            self.next_dialog = "cycle"
                            _log.info("riven choice confirmed -> choice scan")
                            self._fire("on_choice_confirmed")
                else:
                    _log.info("riven dialog cancelled (SendResult %s)", code)
                self.pending_dialog = None
        return consumed

    # -- external entry points -------------------------------------------------------------

    def resume(self) -> None:
        """resumeRivenSession: re-arm over a rolling screen whose open marker is gone."""
        with self._lock:
            was_active = self.active
            self.active = True
            self.started_at = self.clock()
            self.diorama_ready = True
            self.force_ended_at = None
            if not was_active:
                self.pending_dialog = None
                self.next_dialog = "cycle"
                self.chat_view_active = False
            self._reset_idle()

    def force_end(self) -> None:
        """forceEndRivenSession: the overlay was dismissed; resets HudVis tracking too."""
        with self._lock:
            if not self.active and not self.pending_dialog and not self.chat_view_active:
                return
            self.active = False
            self.started_at = None
            self.diorama_ready = False
            self.chat_view_active = False
            self.chat_hud_vis_level = 0
            self.chat_weapon_path = None
            self.pending_dialog = None
            self.next_dialog = "cycle"
            self.force_ended_at = self.clock()
            self.last_hud_vis = 0
            self.last_hud_vis_increase_at = None
            self._idle_token = None
            self._cancel_idle()
            _log.info("riven session force-ended")
