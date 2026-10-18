"""Relic rewards overlay: EE.log trigger -> capture -> scan -> enrich -> price -> overlay.

Port of WFHelper's reward path (docs/port/relic-rewards.md). Pieces:

- triggers (§1.6, §1.7): "Pause countdown done" / "Got rewards" open the screen,
  "ProjectionRewardChoice.lua: Missing icon data!" marks the cards rendering (logged),
  "Relic reward screen shut down" closes it; relic-picker lines suppress triggers
  for 3 s and prefetch prime-part prices; activeMissionTag tracks omnia fissures;
- orchestration (§2): first capture at trigger + 650 ms (trigger backdated by the
  EE.log flush lag), up to 10 attempts 450 ms apart within 5 s, auto-hide at
  trigger + 14.5 s (10 s in omnia), never under 5 s, or 1.5 s after the close line;
- the scan itself lives in reward_scan / reward_geometry, enrichment and prices in
  reward_enrich, drawing in reward_overlay.

EE.log handlers run on the tailer thread: they only update a few fields and hand
the work to the pool (app.submit).
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Callable

from PIL import Image

from .. import capture, log, rpc, session
from ..app import App, get_app
from ..eelog import FILE, UptimeTracker
from . import reward_geometry as geo
from . import reward_overlay as ov
from .reward_enrich import (MarketPrices, PriceSource, RewardEntry, apply_prices, best_ducats, best_mastery, best_plat,
                            enrich, pending_slugs)
from .reward_scan import RewardScanner, ScanResult

_log = log.get("rewards")

# ---- patterns (eeLogMonitor.ts, verbatim) -------------------------------------------

REWARD_PATTERNS = (re.compile(r"\bPause countdown done\b", re.I), re.compile(r"\bGot rewards\b", re.I))
REWARD_UI_READY = re.compile(r"ProjectionRewardChoice\.lua:\s*Missing icon data!", re.I)
REWARD_CLOSED = re.compile(r"ProjectionRewardChoice\.lua:\s*Relic reward screen shut down", re.I)
PICKER_OPEN = (
    re.compile(r"\bThemedProjectionManager\.lua:\s*LoadingCompleteEnd\b", re.I),
    re.compile(r"\bProjection[A-Za-z_]*\.lua:\s*LoadingCompleteEnd\b", re.I),
    re.compile(r"\bThemedProjectionManager\.lua:\s*PopulateInventoryGrid\b", re.I),
    re.compile(r"\bProjectionManager\.lua:\s*PopulateInventoryGrid\b", re.I),
    re.compile(r"\bProjection[A-Za-z_]*\.lua:\s*PopulateInventoryGrid\b", re.I),
)
PICKER_CLOSE = re.compile(r"\bInitMapping for all devices with bindings\b", re.I)
MISSION_TAG = re.compile(r"(?:^|[\"\s])activeMissionTag[\"\s]*[=:]\s*\"?([A-Za-z0-9_]+)")
MISSION_END = re.compile(r"Sys \[Info\]: EOM missionLocationUnlocked=|TopMenu\.lua: Abort:")
TIER_BY_TAG = {"VOIDT1": "lith", "VOIDT2": "meso", "VOIDT3": "neo", "VOIDT4": "axi", "VOIDT5": "requiem",
               "VOIDT6": "omnia"}

# ---- timing (seconds; eeLogMonitor.ts / scan.ts) ------------------------------------

TRIGGER_DELAY = 0.25
REWARD_TRIGGER_COOLDOWN = 2.5
REWARD_FILE_ECHO_WINDOW = 30.0
REWARD_AFTER_PICKER_SUPPRESS = 3.0
REWARD_STALE_SUPPRESS = 20.0
EELOG_REWARD_SCAN_DELAY = 0.65
SCAN_RETRY_WINDOW = 5.0
SCAN_RETRY_INTERVAL = 0.45
SCAN_MAX_ATTEMPTS = 10
NO_LAYOUT_MAX_ATTEMPTS = 3
PARTIAL_LAYOUT_BONUS_ATTEMPTS = 1
MAX_REWARD_ITEMS = 4
REWARD_VOTE_WINDOW = 14.5
OMNIA_REWARD_VOTE_WINDOW = 10.0
REWARD_MIN_VISIBLE = 5.0
REWARD_CLOSE_GRACE = 1.5
REWARD_CLOSE_STALE_MAX = 5.0
AUTO_HIDE_FAILURE = 3.5
AUTO_HIDE_UNAVAILABLE = 12.0
AUTO_HIDE_DETECTING_MAX = 20.0
BEST_PICK_SETTLE_CAP = 4.0
RENDER_SIGNAL_LOG_WINDOW = 15.0
PREFETCH_INTERVAL = 10 * 60.0
MAX_SAVED_CAPTURES = 10


def _result_json(source: str, entries: list[RewardEntry], scan: ScanResult | None, failure: str | None,
                 attempts: int = 0, elapsed_ms: int = 0, capture_size=None, overlay_px: list | None = None) -> dict:
    return {
        "at": time.time(),
        "source": source,
        "items": [e.to_json() for e in sorted(entries, key=lambda e: e.slot)],
        "best_plat": best_plat(entries),
        "best_ducats": best_ducats(entries),
        "best_mastery": None,
        "slot_count": scan.slot_count if scan else 0,
        "card_count": scan.card_count if scan else 0,
        "meta": scan.meta() if scan else None,
        "reads": scan.reads if scan else [],
        "failure": failure,
        "attempts": attempts,
        "elapsed_ms": elapsed_ms,
        "capture_size": list(capture_size) if capture_size else None,
        "cards": overlay_px or [],
    }


def _item_prices_pending(entries: list[RewardEntry]) -> bool:
    return any(e.slug and e.price_state == "pending" for e in entries)


class RewardController:
    def __init__(self, app: App, scanner: RewardScanner | None = None, prices: PriceSource | None = None,
                 grab: Callable[[], Image.Image] | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, game_running: Callable[[], bool] | None = None,
                 ui_scale: Callable[[], float] | None = None, inventory: Callable[[], Any] | None = None,
                 items_db: Callable[[], Any] | None = None):
        self.app = app
        self.scanner = scanner or RewardScanner(app.models_dir)
        self.prices = prices or MarketPrices()
        self._grab = grab or (lambda: capture.grab(timeout=2.0))
        self.clock = clock
        self.sleep = sleep
        self._game_running = game_running or session.warframe_running
        self._ui_scale = ui_scale or self._default_ui_scale
        self._inventory = inventory or self._default_inventory
        self._items_db = items_db or self._default_items_db
        self._lock = threading.RLock()
        self._uptime = UptimeTracker()
        # trigger state
        self.last_reward_at = -1e9
        self.last_picker_at = -1e9
        self._trigger_pending = False
        self.in_flight = False
        self.trigger_at = 0.0
        self.closed_at = 0.0
        self._ui_signal_for = 0.0
        self.fissure_tier: str | None = None
        self._last_prefetch = -1e9
        # presentation state
        self.round = 0
        self.hide_at = 0.0
        self._image: tuple[Image.Image, int, int] | None = None
        self._entries: list[RewardEntry] = []
        self.last: dict | None = None
        self.triggers = 0
        self._started = 0.0
        self._last_frame: Image.Image | None = None

    # -- settings / deps ----------------------------------------------------------

    def setting(self, key: str, default: Any = None) -> Any:
        return self.app.settings.get(f"relic_rewards.{key}", default)

    def enabled(self) -> bool:
        return bool(self.setting("enabled", True))

    def _default_ui_scale(self) -> float:
        if self.setting("ui_scale_auto", True):
            try:
                from .. import eecfg

                v = eecfg.ui_scale()
                if v:
                    return v
            except Exception:  # noqa: BLE001
                _log.exception("EE.cfg read failed")
        try:
            return float(self.setting("ui_scale", geo.REFERENCE_UI_SCALE)) or geo.REFERENCE_UI_SCALE
        except (TypeError, ValueError):
            return geo.REFERENCE_UI_SCALE

    @staticmethod
    def _default_inventory():
        try:
            from .. import inventory

            return inventory.inventory()
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _default_items_db():
        try:
            from ..data.items import items

            return items()
        except Exception:  # noqa: BLE001
            return None

    def _schedule(self, delay: float, fn: Callable, *args) -> None:
        t = threading.Timer(delay, fn, args)
        t.daemon = True
        t.start()

    # -- EE.log (tailer thread: keep it short) ----------------------------------------

    def on_line(self, line: str) -> None:
        src = getattr(self.app.eelog, "source", FILE)
        now = self.clock()
        staleness = self._uptime.observe(line, now) if src == FILE else 0.0
        realtime = self._realtime_active()
        if "activeMissionTag" in line:
            m = MISSION_TAG.search(line)
            if m:
                self.fissure_tier = TIER_BY_TAG.get(m.group(1).upper())
        if MISSION_END.search(line):
            self.fissure_tier = None
        if any(p.search(line) for p in REWARD_PATTERNS):
            if src == FILE and realtime and now - self.last_reward_at < REWARD_FILE_ECHO_WINDOW:
                return  # flush echo of a real-time trigger
            self.schedule_trigger(staleness)
            return
        if REWARD_UI_READY.search(line):
            self.notify_ui_ready()
            return
        if REWARD_CLOSED.search(line):
            self.notify_closed(staleness)
            return
        if any(p.search(line) for p in PICKER_OPEN):
            if src == FILE and realtime:
                return
            self.last_picker_at = now
            self.on_picker_open()
            return

    def _realtime_active(self) -> bool:
        try:
            return bool(self.app.eelog.realtime_active())
        except Exception:  # noqa: BLE001
            return False

    def schedule_trigger(self, staleness: float = 0.0) -> None:
        now = self.clock()
        with self._lock:
            if now - self.last_reward_at < REWARD_TRIGGER_COOLDOWN or self._trigger_pending:
                return
            self._trigger_pending = True
        self._schedule(TRIGGER_DELAY, self._fire_trigger, staleness)

    def _fire_trigger(self, staleness: float) -> None:
        now = self.clock()
        with self._lock:
            self._trigger_pending = False
            if now - self.last_picker_at < REWARD_AFTER_PICKER_SUPPRESS:
                _log.info("reward trigger suppressed - relic picker")
                return
            if staleness > REWARD_STALE_SUPPRESS:
                _log.info("reward trigger dropped - line %.1fs stale", staleness)
                return
            self.last_reward_at = now
        self.trigger("eelog", staleness)

    def notify_ui_ready(self) -> None:
        since = self.clock() - self.trigger_at
        if self.trigger_at <= 0 or since > RENDER_SIGNAL_LOG_WINDOW or self._ui_signal_for == self.trigger_at:
            return
        self._ui_signal_for = self.trigger_at
        _log.info("reward UI render signal %.0fms after the trigger", since * 1000)

    def notify_closed(self, staleness: float = 0.0) -> None:
        with self._lock:
            if not self.trigger_at or staleness > REWARD_CLOSE_STALE_MAX or self.closed_at:
                return
            now = self.clock()
            self.closed_at = now
            if self.in_flight:
                return  # the success path shortens its own delay
            delay = max(REWARD_CLOSE_GRACE, self.trigger_at + REWARD_MIN_VISIBLE - now)
        _log.info("reward screen closed -> overlay hides in %.1fs", delay)
        self._set_hide(now + delay, shorten_only=True)

    def on_picker_open(self) -> None:
        """Relic picker up: the crack is coming, warm the prime-part prices."""
        if not self.setting("prefetch_prices", True):
            return
        now = self.clock()
        if now - self._last_prefetch < PREFETCH_INTERVAL:
            return
        self._last_prefetch = now
        self.app.submit(self.prefetch_prices)

    def prefetch_prices(self) -> int:
        db = self._items_db()
        if db is None:
            return 0
        pool = db.prime_parts()
        first: list[str] = []
        try:  # parts of the relics the player owns go first
            from .. import inventory
            from ..data.relics import relics

            rdb = relics()
            wanted = {n for (n, _ref) in inventory.owned_relics()}
            for name in wanted:
                rel = rdb.get(name)
                for rw in (rel.rewards if rel else []):
                    it = db.by_name(rw.item_name)
                    if it and it.wfm_slug:
                        first.append(it.wfm_slug)
        except Exception:  # noqa: BLE001 - inventory/relic data optional
            pass
        slugs = list(dict.fromkeys(first + [i.wfm_slug for i in pool if i.wfm_slug]))
        n = self.prices.prefetch(slugs)
        _log.info("prefetching %d reward prices (%d from owned relics)", n, len(set(first)))
        return n

    # -- scan orchestration ---------------------------------------------------------------

    def trigger(self, source: str = "manual", staleness: float = 0.0) -> bool:
        if not self.enabled():
            return False
        if source == "eelog" and not self.setting("auto_trigger", True):
            return False
        self.triggers += 1
        self.app.submit(self.dispatch, source, staleness)
        return True

    def dispatch(self, source: str, staleness: float = 0.0) -> dict | None:
        with self._lock:
            if self.in_flight:
                _log.info("scan already running, ignored duplicate trigger (%s)", source)
                return None
            self.in_flight = True
            self.round += 1
            rnd = self.round
            self._started = self.clock()
            if source == "eelog":
                self.trigger_at = self.clock() - staleness
                self.closed_at = 0.0
        try:
            # Hide last round's cards so they are neither shown nor captured.
            self.app.overlays.clear(ov.LAYER)
            self._image = None
            self._last_frame = None
            if source == "eelog" and not self._game_running():
                _log.info("skipped reward scan: Warframe is not running")
                return None
            if source != "eelog":
                self._show_message(rnd, "Reading reward screen…", "", AUTO_HIDE_DETECTING_MAX)
            if not self.scanner.ocr_available():
                return self._present_failure(rnd, source, "ocr-unavailable", None, 0, 0)
            if source == "eelog":
                wait = self.trigger_at + EELOG_REWARD_SCAN_DELAY - self.clock()
                if wait > 0:
                    self.sleep(wait)
            outcome = self.run_with_retries()
            return self._present(rnd, source, outcome)
        except Exception:  # noqa: BLE001
            _log.exception("reward scan pipeline error")
            return self._present_failure(rnd, source, "error", None, 0, 0)
        finally:
            with self._lock:
                self.in_flight = False

    def _grab_frame(self) -> Image.Image:
        img = self._grab()
        self._last_frame = img  # kept for debug_save_captures if this round fails
        return img

    def _save_capture(self, img: Image.Image, rnd: int, reason: str) -> None:
        """debug_save_captures: keep the last frame behind a failed/partial scan."""
        d = os.path.join(self.app.runtime_dir, "captures")
        os.makedirs(d, exist_ok=True)
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
        img.save(os.path.join(d, f"rewards-{stamp}-r{rnd}-{reason}.png"))
        files = sorted(f for f in os.listdir(d) if f.startswith("rewards-"))
        for f in files[:-MAX_SAVED_CAPTURES]:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass

    def run_with_retries(self) -> dict:
        """runRewardScanWithRetries: best result, attempts, and why it gave up."""
        started = self.clock()
        attempts = no_layout = partial_attempts = capture_errors = 0
        counted = 0
        best: ScanResult | None = None
        not_reward = False
        out = {"result": None, "attempts": 0, "partial": None, "not_reward_screen": False, "capture_failed": False}

        def better(cur: ScanResult | None, cand: ScanResult | None) -> ScanResult | None:
            if cand is None:
                return cur
            if cur is None:
                return cand
            if counted > 0:
                c_ok, n_ok = len(cur.items) == counted, len(cand.items) == counted
                if c_ok != n_ok:
                    return cand if n_ok else cur
            if len(cand.items) != len(cur.items):
                return cand if len(cand.items) > len(cur.items) else cur
            return cand if cand.score > cur.score else cur

        while attempts < SCAN_MAX_ATTEMPTS and self.clock() - started < SCAN_RETRY_WINDOW:
            attempts += 1
            res: ScanResult | None = None
            scan_error = False
            try:
                frame = self._grab_frame()
                res = self.scanner.scan(frame, self._ui_scale())
                capture_errors = 0
            except capture.CaptureError as e:
                capture_errors += 1
                _log.warning("capture failed (attempt %d): %s", attempts, e)
                if capture_errors >= 3 and best is None:
                    out["capture_failed"] = True
                    break
            except Exception:  # noqa: BLE001
                _log.exception("scan attempt %d failed", attempts)
                scan_error = True  # WFHelper: a throwing attempt counts as layoutCount 0
            counted = (res.card_count if res else 0) or counted
            best = better(best, res)
            n = len(res.items) if res else 0
            if n > 0:
                geometry_complete = res.slot_count == 3 and n >= 3
                layout_known = max(res.slot_count, res.layout_count) > 0
                partial = (n != counted) if counted > 0 else (layout_known and n < MAX_REWARD_ITEMS
                                                               and not geometry_complete)
                if not partial or partial_attempts >= PARTIAL_LAYOUT_BONUS_ATTEMPTS:
                    out["attempts"] = attempts
                    if counted > 0 and len(best.items) != counted:
                        out["partial"] = {"items": len(best.items), "cards": counted}
                        out["result"] = best
                        return out
                    out["result"] = best
                    return out
                partial_attempts += 1
                _log.info("partial layout (%d/%d slots) - one more attempt", n, counted or res.slot_count)
            if res is not None or scan_error:
                no_layout = 0 if res is not None and res.layout_count > 0 else no_layout + 1
                if no_layout >= NO_LAYOUT_MAX_ATTEMPTS:
                    not_reward = True
                    _log.info("no reward layout in %d attempt(s) - not the reward screen", attempts)
                    break
            remaining = SCAN_RETRY_WINDOW - (self.clock() - started)
            if remaining <= 0 or attempts >= SCAN_MAX_ATTEMPTS:
                break
            self.sleep(min(SCAN_RETRY_INTERVAL, remaining))
        out["attempts"] = attempts
        out["result"] = best
        read = len(best.items) if best else 0
        if read > 0 and counted > 0 and read != counted:
            out["partial"] = {"items": read, "cards": counted}
        out["not_reward_screen"] = not_reward and read == 0
        return out

    # -- presentation ------------------------------------------------------------------------

    def build_entries(self, scan: ScanResult) -> list[RewardEntry]:
        entries = enrich(scan.items[:MAX_REWARD_ITEMS], self._items_db(), self._inventory())
        apply_prices(entries, self.prices.cached)
        return entries

    def _vote_hide_at(self, source: str) -> float:
        now = self.clock()
        if source != "eelog" or not self.trigger_at:
            return now + float(self.setting("duration_s", 12) or 12)
        if self.closed_at:
            return now + REWARD_MIN_VISIBLE
        window = OMNIA_REWARD_VOTE_WINDOW if self.fissure_tier == "omnia" else REWARD_VOTE_WINDOW
        return now + max(REWARD_MIN_VISIBLE, self.trigger_at + window - now)

    def _present(self, rnd: int, source: str, outcome: dict) -> dict:
        scan: ScanResult | None = outcome["result"]
        since = self.trigger_at if source == "eelog" and self.trigger_at else self._started
        elapsed = round((self.clock() - since) * 1000)
        if outcome["capture_failed"]:
            return self._present_failure(rnd, source, "capture-unavailable", scan, outcome["attempts"], elapsed)
        if outcome["partial"]:
            _log.warning("gave up: %d/%d counted cards after %d attempt(s)", outcome["partial"]["items"],
                         outcome["partial"]["cards"], outcome["attempts"])
            return self._present_failure(rnd, source, "partial", scan, outcome["attempts"], elapsed)
        if scan is None or not scan.items:
            reason = "not-reward-screen" if outcome["not_reward_screen"] else "no-items"
            return self._present_failure(rnd, source, reason, scan, outcome["attempts"], elapsed,
                                         reveal=not outcome["not_reward_screen"])
        entries = self.build_entries(scan)
        # overlay.js:345-378 / 717-720: no best-plat crown (nor toast) until every item price
        # is in or the 4 s settle cap has passed; set prices do not hold it back.
        settled = not _item_prices_pending(entries)
        result = self._publish(rnd, source, entries, scan, None, outcome["attempts"], elapsed,
                               hide_at=self._vote_hide_at(source), settled=settled, toast=settled)
        if pending_slugs(entries):
            self.app.submit(self._price_pass, rnd, source, scan, outcome["attempts"], elapsed, not settled)
        return result

    def _price_pass(self, rnd: int, source: str, scan: ScanResult, attempts: int, elapsed: int,
                    toast_pending: bool = False) -> None:
        """Fetch the missing prices, settle the best pick at the 4 s cap, then fill in the rest."""
        entries = self._entries
        for i, deadline in enumerate((BEST_PICK_SETTLE_CAP, None)):
            if rnd != self.round:
                return
            slugs = pending_slugs(entries)
            if not slugs and i > 0:
                return
            if slugs:
                got = self.prices.fetch(slugs, deadline)
                if rnd != self.round:
                    return
                known = {s: v for s, v in got.items() if v is not None}
                if deadline is None:
                    known = got  # second pass: whatever is still unknown is missing
                apply_prices(entries, lambda s: (s in known, known.get(s)))
            # first step = settle point: crown the best pick and send the deferred toast
            self._publish(rnd, source, entries, scan, None, attempts, elapsed, hide_at=None, settled=True,
                          toast=(i == 0 and toast_pending))

    def _publish(self, rnd: int | None, source: str, entries: list[RewardEntry], scan: ScanResult | None,
                 failure: str | None, attempts: int, elapsed: int, hide_at: float | None,
                 settled: bool = True, toast: bool = False) -> dict:
        """rnd None: a debug file scan that only reports (no round, no overlay)."""
        overlay_px = []
        if scan is not None and scan.layout is not None:
            overlay_px = [(slot, (r.x, r.y, r.w, r.h)) for slot, r in scan.card_pixels()]
        result = _result_json(source, entries, scan, failure, attempts, elapsed,
                              scan.capture_size if scan else None,
                              [{"slot": s, "x": x, "y": y, "w": w, "h": h} for s, (x, y, w, h) in overlay_px])
        if self.setting("prioritize_mastery", True):
            result["best_mastery"] = best_mastery(entries)
        if not settled and _item_prices_pending(entries):
            result["best_plat"] = None
        with self._lock:
            if rnd is None:
                self.last = result
            elif rnd != self.round:
                return result
            else:
                self._entries = entries
                self.last = result
        self.app.emit("rewards.result", result)
        if rnd is None:
            return result
        if hide_at is not None:
            self.hide_at = hide_at
        bp, bd, bm = result["best_plat"], result["best_ducats"], result["best_mastery"]
        drawn = False
        if self._overlay_allowed() and overlay_px and scan is not None:
            placed = ov.layout_blocks(entries, overlay_px, scan.capture_size, self.app.overlays.size,
                                      str(self.setting("position", "below")), bp, bd,
                                      bool(self.setting("show_ducats", True)), bool(self.setting("show_owned", True)),
                                      best_mastery=bm)
            comp = ov.compose(placed)
            if comp:
                self._image = comp
                self._show_image(rnd)
                drawn = True
        if (toast and self.enabled() and (not drawn or not self._overlay_connected())
                and self.setting("toast_fallback", True)):
            title, body = ov.toast_text(entries, bp, bd, bm)
            self.app.toast(title, body)
        return result

    def _present_failure(self, rnd: int | None, source: str, reason: str, scan: ScanResult | None, attempts: int,
                         elapsed: int, reveal: bool = True) -> dict:
        result = _result_json(source, [], scan, reason, attempts, elapsed, scan.capture_size if scan else None)
        with self._lock:
            if rnd is None or rnd == self.round:
                self.last = result
            frame, self._last_frame = self._last_frame, None
        self.app.emit("rewards.result", result)
        if rnd is None:
            return result  # debug file scan: leave the live layer alone
        if frame is not None and self.app.settings.get("debug_save_captures", False):
            self.app.submit(self._save_capture, frame, rnd, reason)
        if source == "file" or not reveal:
            if rnd == self.round:
                self.app.overlays.clear(ov.LAYER)
            return result
        texts = {
            "ocr-unavailable": ("OCR model missing", "Reinstall the plugin to restore models/paddle."),
            "capture-unavailable": ("No screen capture", "gamescope did not return a screenshot."),
            "partial": ("Could not read every card", "Not showing a partial set."),
            "no-items": ("No reward items read", ""),
            "error": ("Reward scan failed", "See the plugin log."),
        }
        title, sub = texts.get(reason, ("No reward items read", ""))
        ttl = AUTO_HIDE_UNAVAILABLE if reason in ("ocr-unavailable", "capture-unavailable") else AUTO_HIDE_FAILURE
        self._show_message(rnd, title, sub, ttl)
        if reason in ("ocr-unavailable", "capture-unavailable") and (
                not self._overlay_allowed() or not self._overlay_connected()):
            self.app.toast("Relic rewards: " + title, sub)
        return result

    def _overlay_allowed(self) -> bool:
        return self.enabled() and bool(self.app.settings.get("overlays_enabled", True))

    def _overlay_connected(self) -> bool:
        try:
            st = self.app.overlays.status()
        except Exception:  # noqa: BLE001
            return False
        return bool(st.get("connected"))

    def _show_message(self, rnd: int, title: str, sub: str, ttl: float) -> None:
        if not self._overlay_allowed():
            return
        img = ov.render_message(title, sub)
        w, h = self.app.overlays.size
        self._image = (img, (w - img.width) // 2, round(h * 0.56))
        self.hide_at = self.clock() + ttl
        self._show_image(rnd)

    def _show_image(self, rnd: int) -> None:
        with self._lock:
            if rnd != self.round or self._image is None:
                return
            if not self._overlay_allowed():
                return
            img, x, y = self._image
            ttl = self.hide_at - self.clock()
        if ttl <= 0:
            self.app.overlays.clear(ov.LAYER)
            return
        self.app.overlays.show(ov.LAYER, img, x, y, ttl=ttl, z=10)

    def _set_hide(self, at: float, shorten_only: bool = False) -> None:
        with self._lock:
            if shorten_only and self.hide_at and at >= self.hide_at:
                return
            self.hide_at = at
            rnd = self.round
        self._show_image(rnd)

    def clear(self) -> None:
        with self._lock:
            self.round += 1
            self._image = None
        self.app.overlays.clear(ov.LAYER)

    # -- debug / RPC helpers ----------------------------------------------------------------

    def scan_file(self, path: str, ui_scale: float | None = None, show: bool = False,
                  fetch_prices: bool = True) -> dict:
        img = Image.open(path)
        img.load()
        img = img.convert("RGB")
        scan = self.scanner.scan(img, float(ui_scale) if ui_scale else self._ui_scale(), budget_s=None, dedup=False)
        rnd: int | None = None
        if show:  # only a shown file scan takes the live layer, and never from a running reward scan
            with self._lock:
                if self.in_flight:
                    raise RuntimeError("a reward scan is running; try again in a few seconds")
                self.round += 1
                rnd = self.round
        if not scan.items:
            return self._present_failure(rnd, "file", "no-items" if scan.layout_count else "not-reward-screen",
                                         scan, 1, scan.elapsed_ms)
        entries = self.build_entries(scan)
        if fetch_prices and pending_slugs(entries):
            got = self.prices.fetch(pending_slugs(entries), BEST_PICK_SETTLE_CAP)
            known = {s: v for s, v in got.items() if v is not None}
            apply_prices(entries, lambda s: (s in known, known.get(s)))
        hide = self.clock() + float(self.setting("duration_s", 12) or 12) if show else None
        return self._publish(rnd, "file", entries, scan, None, 1, scan.elapsed_ms, hide_at=hide)

    def status(self) -> dict:
        return {
            "enabled": self.enabled(),
            "in_flight": self.in_flight,
            "fissure_tier": self.fissure_tier,
            "ocr_available": self.scanner.ocr_available(),
            "overlay": self.app.overlays.status(),
            "realtime_active": self._realtime_active(),
            "triggers": self.triggers,
            "ui_scale": self._ui_scale(),
        }


_ctl: RewardController | None = None


def controller() -> RewardController:
    if _ctl is None:
        raise RuntimeError("relic rewards not initialised")
    return _ctl


def setup(app: App) -> None:
    global _ctl
    _ctl = RewardController(app)
    app.services["relic_rewards"] = _ctl
    app.eelog.subscribe(_ctl.on_line)
    if _ctl.enabled():
        try:
            from .. import ocr

            ocr.warmup(app.models_dir)
        except Exception:  # noqa: BLE001
            _log.exception("ocr warmup failed")

    def on_settings(data: dict) -> None:
        sec = data.get("relic_rewards") or {}
        if not sec.get("enabled", True):
            _ctl.clear()  # bumps the round so a pending price step does not redraw
        elif not data.get("overlays_enabled", True):
            app.overlays.clear(ov.LAYER)

    app.settings.on_change(on_settings)


# ---- RPC -----------------------------------------------------------------------------------

@rpc.method("rewards.last")
def _rpc_last() -> dict | None:
    """The latest reward result (items, prices, best picks, failure) for the QAM."""
    return controller().last


@rpc.method("rewards.status")
async def _rpc_status() -> dict:
    # off the event loop: the EE.cfg UI scale read may run EE.log path discovery
    return await get_app().run_blocking(controller().status)


@rpc.method("rewards.scan_file")
async def _rpc_scan_file(path: str, ui_scale: float | None = None, show: bool = False) -> dict:
    """Debug: run the whole pipeline on a saved PNG (any size; 1280x800 for Deck captures)."""
    if not path or not os.path.isfile(path):
        raise ValueError(f"no such file: {path}")
    return await get_app().run_blocking(controller().scan_file, path, ui_scale, show)


@rpc.method("rewards.scan_now")
def _rpc_scan_now() -> dict:
    """Manual trigger: capture and scan the current screen now."""
    started = controller().trigger("manual", 0.0)
    return {"started": started}


@rpc.method("rewards.clear")
def _rpc_clear() -> None:
    controller().clear()
