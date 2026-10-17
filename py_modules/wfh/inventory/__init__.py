"""The player's inventory: acquisition, persistence and the counts features use.

Sources (setting `inventory_source`):
  memory  (default) WFHelper's Linux "helper" path (apiHelperRunner.ts runOnceLinux):
          read the `?accountId=..&nonce=..` query from Warframe's memory as root,
          then GET inventory.php over HTTPS. The game's own resident inventory
          JSON (gameMemoryInventory.ts) is read in the same memory walk; it is
          used right after a mission ends (when the game holds the fresh
          post-mission copy) and as a fallback when the API is unreachable.
  file    a JSON file the user exported (setting `inventory_file`, any envelope
          inventoryPayload.ts accepts); never replaced behind the user's back.
  none    no inventory.

Schedule (inventorySync.ts + apiHelperRunner.ts polling): an API fetch at most
every `inventory_refresh_min` minutes while the game runs (90 s retry after a
failure that never reached DE), right after `MainMenu::LoginDone`, and on
demand; a resident read 3 s after each mission end (dated by the line's
uptime stamp, since EE.log can arrive 10-26 s late). The newest snapshot is kept
in <runtime_dir>/inventory.json so a restart does not need the game.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable

from .. import log, rpc, session
from ..eelog import FILE, REALTIME, UptimeTracker
from .api import InventoryFetchError, fetch_inventory
from .memory import GameInventoryRead, read_game
from .model import Inventory
from .payload import InventoryFormatError, parse_inventory_bytes

_log = log.get("inventory")

INVENTORY_FILE = "inventory.json"
STATE_FILE = "inventory-state.json"
DEFAULT_INTERVAL_MIN = 10
# Failures before reaching DE (game closed, not logged in) retry much sooner.
LOCAL_FAILURE_RETRY_S = 90.0
LOCAL_REASONS = ("game-not-running", "access-denied", "not-logged-in")
MAX_FILE_BYTES = 50 * 1024 * 1024
FILE_POLL_S = 10.0

LOGIN_LINE = re.compile(r"\bMainMenu::LoginDone result=true\b")
LOGIN_DEDUP_S = 30.0
LOGIN_DELAY_S = 1.0
MISSION_END_LINE = re.compile(r"Sys \[Info\]: EOM missionLocationUnlocked=|TopMenu\.lua: Abort:")
HUB_ABORT = re.compile(r"TopMenu\.lua: Abort: in hub")
# Leaving Cetus/Fortuna/Necralisk logs an end with the town's hub node (missionRewards.ts).
HUB_NODE = re.compile(r"hub", re.IGNORECASE)
SYNC_CONSUMABLES = re.compile(r"SyncAutoPopulatedConsumables for mission (MT_[A-Z_]+) with location (\S+)")
STATE_STARTED = re.compile(r"Game \[Info\]: OnStateStarted, mission type=(MT_[A-Z_]+)")
# missionRewards.ts timings.
MISSION_READ_DELAY_S = 3.0
MISSION_READ_RETRY_S = 1.0
MISSION_READ_ATTEMPTS = 2
MISSION_END_JOIN_S = 15.0
SYNC_TOLERANCE_S = 30.0
EE_LOG_LOGIN_MARKER = b"Logging in as "

SOURCES = ("memory", "file", "none")


def normalize_source(raw: Any) -> str:
    """WFHelper's names map onto ours: helper -> memory, manual/aleca -> file."""
    raw = str(raw or "").lower()
    if raw in SOURCES:
        return raw
    return {"helper": "memory", "manual": "file", "aleca": "file", "json": "memory"}.get(raw, "memory")


def should_retry_after_login(running: bool, last_ok: bool | None, reason: str | None) -> bool:
    if running:
        return True
    if last_ok is True:
        return False
    # "api-failed" is WFHelper's "error" for a fetch that never got an inventory.
    return reason in (None, "game-not-running", "not-logged-in", "token-not-found", "error", "api-failed")


def file_contains(path: str, marker: bytes, chunk: int = 4 * 1024 * 1024) -> bool:
    """Chunked substring scan so a large EE.log never sits in memory at once."""
    try:
        with open(path, "rb") as f:
            carry = b""
            while True:
                buf = f.read(chunk)
                if not buf:
                    return False
                if marker in carry + buf:
                    return True
                carry = buf[-len(marker):]
    except OSError:
        return False


def _write_atomic(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


class InventoryService:
    def __init__(self, runtime_dir: str, settings, emit: Callable[[str, Any], None] | None = None,
                 clock: Callable[[], float] = time.time,
                 scan: Callable[[bool], tuple] | None = None,
                 fetcher: Callable[[str], tuple[bytes, str]] = fetch_inventory,
                 game_running: Callable[[], bool] = session.warframe_running,
                 ee_log_path: Callable[[], str | None] = lambda: None,
                 home: Callable[[], str] = lambda: session.deck_user().pw_dir,
                 line_source: Callable[[], str] = lambda: FILE):
        self.runtime_dir = runtime_dir
        self.settings = settings
        self._emit = emit or (lambda name, data: None)
        self._clock = clock
        # scan(eager): eager parses the resident copy in the walk (mission reads);
        # otherwise it comes back as a callable used only on the API fallback.
        self._scan = scan or (lambda eager: read_game(want_inventory=True, defer_inventory=not eager))
        self._fetch = fetcher
        self._game_running = game_running
        self._ee_log_path = ee_log_path
        self._home = home
        self._line_source = line_source
        self._uptime = UptimeTracker()
        self._mission_info: dict | None = None
        self._reapply = False
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._listeners: list[Callable[[Inventory], None]] = []
        self.current: Inventory | None = None
        self.running = False
        self.last_run_at: float | None = None
        self.last_run_ok: bool | None = None
        self.last_reason: str | None = None
        self.last_trigger: str | None = None
        self.last_api_at: float | None = None
        self.last_error: dict | None = None
        self.last_resident: dict | None = None
        self.last_scan: dict | None = None
        self._pending_mission: dict | None = None
        self._last_end_at: float | None = None
        self._pending_login: float | None = None
        self._last_login_at = -1e18
        self._file_checked = 0.0
        self._file_mtime: float | None = None
        self._applied: tuple[str, str] | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- settings --------------------------------------------------------

    def source(self) -> str:
        return normalize_source(self.settings.get("inventory_source", "memory"))

    def file_setting(self) -> str:
        return self._expand(str(self.settings.get("inventory_file", "") or ""))

    def interval_s(self) -> float:
        try:
            minutes = float(self.settings.get("inventory_refresh_min", DEFAULT_INTERVAL_MIN))
        except (TypeError, ValueError):
            minutes = DEFAULT_INTERVAL_MIN
        return max(1.0, minutes) * 60

    def auto_enabled(self) -> bool:
        return bool(self.settings.get("inventory_auto_sync", True))

    def mission_scan_enabled(self) -> bool:
        return bool(self.settings.get("inventory_mission_scan", True))

    def _expand(self, path: str) -> str:
        path = path.strip()
        if path == "~" or path.startswith("~/"):
            path = os.path.join(self._home(), path[2:])  # root's ~ is not the player's
        return path

    @property
    def inventory_path(self) -> str:
        return os.path.join(self.runtime_dir, INVENTORY_FILE)

    @property
    def state_path(self) -> str:
        return os.path.join(self.runtime_dir, STATE_FILE)

    # ---- listeners -----------------------------------------------------------

    def on_update(self, fn: Callable[[Inventory], None]) -> None:
        self._listeners.append(fn)

    def _publish(self, inv: Inventory | None) -> None:
        self._emit("inventory.updated", inv.summary() if inv else None)
        if inv is None:
            return
        for fn in list(self._listeners):
            try:
                fn(inv)
            except Exception:  # noqa: BLE001
                _log.exception("inventory listener failed")

    # ---- loading ---------------------------------------------------------------

    def apply_settings(self, force: bool = False) -> None:
        """(Re)load from the configured source when it changed."""
        key = (self.source(), self.file_setting())
        if not force and key == self._applied:
            return
        self._applied = key
        src, path = key
        if src == "file":
            # A failed load must not leave the previous api/memory snapshot showing as "file".
            if not path or not self._load_file(path):
                self._set_current(None)
        elif src == "memory":
            self._load_persisted()
        else:
            self._set_current(None)

    def settings_changed(self) -> None:
        """Settings listener (runs on the event loop): only compare the key; the
        reload itself (up to a 50 MB file parse) happens on the inventory thread."""
        if (self.source(), self.file_setting()) != self._applied:
            self._reapply = True
            self._wake.set()

    def _set_current(self, inv: Inventory | None, publish: bool = True) -> None:
        with self._lock:
            changed = inv is not self.current
            self.current = inv
        if changed and publish:
            self._publish(inv)

    def _load_persisted(self) -> None:
        path = self.inventory_path
        try:
            with open(path, "rb") as f:
                raw = f.read()
            data = parse_inventory_bytes(raw)
        except FileNotFoundError:
            self._set_current(None)
            return
        except (OSError, InventoryFormatError) as e:
            _log.warning("saved inventory unreadable: %s", e)
            self._set_current(None)
            return
        state: dict = {}
        try:
            with open(self.state_path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            pass
        self.last_api_at = state.get("last_api_at")
        inv = Inventory(data, str(state.get("origin") or "api"),
                        float(state.get("fetched_at") or os.path.getmtime(path)), path)
        self._set_current(inv)

    def _load_file(self, path: str, quiet: bool = False) -> bool:
        try:
            st = os.stat(path)
            # Remembered even if the parse fails, so a broken file is not re-read every poll.
            self._file_mtime = st.st_mtime
            if not os.path.isfile(path):
                raise InventoryFormatError("Inventory path is not a file")
            if st.st_size > MAX_FILE_BYTES:
                raise InventoryFormatError(f"Inventory file exceeds {MAX_FILE_BYTES} byte limit")
            with open(path, "rb") as f:
                data = parse_inventory_bytes(f.read())
        except (OSError, InventoryFormatError) as e:
            kind = "read" if isinstance(e, OSError) else "parse"
            self.last_error = {"kind": kind, "message": str(e), "path": path, "at": self._clock()}
            if not quiet:
                _log.warning("inventory file %s: %s", path, e)
            return False
        self.last_error = None
        self._set_current(Inventory(data, "file", st.st_mtime, path))
        return True

    def import_file(self, path: str) -> dict:
        """Use a JSON export as the inventory (source "file")."""
        path = self._expand(path)
        if not path or not os.path.isabs(path):
            raise ValueError("give an absolute path (or ~/...)")
        if not self._load_file(path):
            raise ValueError(self.last_error["message"] if self.last_error else "unreadable inventory file")
        self._applied = ("file", path)
        self.settings.update({"inventory_source": "file", "inventory_file": path})
        return self.status()

    # ---- acquisition (memory source) ------------------------------------------

    def _settle(self, ok: bool, reason: str | None, trigger: str) -> bool:
        self.last_run_at = self._clock()
        self.last_run_ok = ok
        self.last_reason = None if ok else (reason or "error")
        self.last_trigger = trigger
        self._emit("inventory.status", self.status())
        return ok

    def _is_newer(self, sync_time: int | None) -> bool:
        cur = self.current.sync_time if self.current else None
        return cur is None or (sync_time is not None and sync_time > cur)

    def _adopt(self, data: dict, origin: str, raw: bytes | None = None) -> Inventory:
        now = self._clock()
        body = raw if raw is not None else json.dumps(data, separators=(",", ":")).encode()
        try:
            _write_atomic(self.inventory_path, body)
            if origin == "api":
                self.last_api_at = now
            _write_atomic(self.state_path, json.dumps(
                {"origin": origin, "fetched_at": now, "last_api_at": self.last_api_at}).encode())
        except OSError as e:
            _log.warning("could not save inventory: %s", e)
        inv = Inventory(data, origin, now, self.inventory_path)
        self._set_current(inv)
        return inv

    def _classify_not_logged_in(self) -> str:
        """No auth string: logged in (EE.log says so) means the scan missed it."""
        path = self._ee_log_path()
        if path and file_contains(path, EE_LOG_LOGIN_MARKER):
            return "token-not-found"
        return "not-logged-in"

    def _adopt_resident(self, resident: GameInventoryRead | Callable[[], GameInventoryRead] | None,
                        min_sync_ms: float | None = None) -> bool:
        if callable(resident):  # deferred extraction (memory.read_game defer_inventory)
            try:
                resident = resident()
            except Exception:  # noqa: BLE001
                _log.exception("deferred resident inventory read failed")
                return False
        if resident is None:
            return False
        self.last_resident = resident.summary()
        copy = resident.newest
        if copy is None or not self._is_newer(copy.sync_time):
            return False
        if min_sync_ms is not None and copy.sync_time < min_sync_ms:
            return False
        self._adopt(copy.inventory, "memory")
        _log.info("resident inventory adopted (sync %s)", time.strftime("%H:%M:%S", time.localtime(copy.sync_time / 1000)))
        return True

    def _scan_game(self, eager: bool) -> tuple[tuple[str | None, str], Any]:
        (authz, reason), resident, scan = self._scan(eager)
        self.last_scan = {"ms": scan.ms, "mb": scan.bytes // (1024 * 1024), "regions": scan.regions,
                          "reason": reason, "at": self._clock()}
        return (authz, reason), resident

    def _api_from(self, authz: str | None, reason: str, resident: GameInventoryRead | None, trigger: str) -> bool:
        if not authz:
            if reason == "process-not-found":
                return self._settle(False, "game-not-running", trigger)
            if reason.startswith("mem-open-"):
                _log.error("cannot read game memory (%s)", reason)
                self._adopt_resident(resident)
                return self._settle(False, "access-denied", trigger)
            got = self._adopt_resident(resident)
            return self._settle(got, None if got else self._classify_not_logged_in(), trigger)
        try:
            body, host = self._fetch(authz)
            data = parse_inventory_bytes(body)
        except (InventoryFetchError, InventoryFormatError, OSError) as e:
            _log.error("inventory fetch failed: %s", e)
            self.last_error = {"kind": "api", "message": str(e), "path": "", "at": self._clock()}
            # DE unreachable: the game's own copy is better than nothing (reported in last_resident).
            self._adopt_resident(resident)
            return self._settle(False, "api-failed", trigger)
        self.last_error = None
        self._adopt(data, "api", body)
        return self._settle(True, None, trigger)

    def run_memory(self, trigger: str = "manual") -> bool:
        """Auth scan + inventory.php (resident copy as fallback). False when busy."""
        if not self._run_lock.acquire(blocking=False):
            return False
        try:
            self.running = True
            if not self._game_running():
                return self._settle(False, "game-not-running", trigger)
            _log.info("reading inventory auth from game memory (%s)", trigger)
            (authz, reason), resident = self._scan_game(False)
            return self._api_from(authz, reason, resident, trigger)
        except Exception as e:  # noqa: BLE001
            _log.exception("inventory run failed")
            self.last_error = {"kind": "error", "message": str(e), "path": "", "at": self._clock()}
            return self._settle(False, "error", trigger)
        finally:
            self.running = False
            self._run_lock.release()

    def mission_read(self) -> str:
        """Resident read after a mission end; returns adopted | retry | api | stale | busy."""
        pending = self._pending_mission
        if pending is None:
            return "stale"
        if not self._run_lock.acquire(blocking=False):
            pending["due"] = self._clock() + MISSION_READ_RETRY_S
            return "busy"
        try:
            self.running = True
            pending["attempts"] += 1
            (authz, reason), resident = self._scan_game(True)
            if self._adopt_resident(resident, (pending["ended_at"] - SYNC_TOLERANCE_S) * 1000):
                self._pending_mission = None
                self._settle(True, None, "mission-end")
                return "adopted"
            if pending["attempts"] < MISSION_READ_ATTEMPTS:
                pending["due"] = self._clock() + MISSION_READ_RETRY_S
                return "retry"
            self._pending_mission = None
            # No fresh copy in memory: ask DE, but no more often than the interval.
            if authz and (self.last_api_at is None or self._clock() - self.last_api_at >= self.interval_s()):
                self._api_from(authz, reason, None, "mission-end")
                return "api"
            return "stale"
        except Exception:  # noqa: BLE001
            _log.exception("mission-end read failed")
            self._pending_mission = None
            return "stale"
        finally:
            self.running = False
            self._run_lock.release()

    # ---- EE.log hooks (tailer thread: only bookkeeping here) -------------------

    def observe_file_line(self, line: str) -> None:
        """Every EE.log file line (echoes included), in order: feeds the lag estimate."""
        self._uptime.observe(line, self._clock())

    def _line_wall_time(self, line: str) -> float:
        """When the game logged line: now for a real-time line, else now minus the
        file's flush lag from its uptime stamp (missionRewards.ts lineWallTime)."""
        now = self._clock()
        if self._line_source() == REALTIME:
            return now
        return now - self._uptime.observe(line, now)

    def _observe_mission_info(self, line: str) -> None:
        """missionRewards.ts observeLine: the last mission type/node before an end."""
        m = SYNC_CONSUMABLES.search(line)
        info = {"type": m.group(1), "node": m.group(2)} if m else None
        if info is None:
            m = STATE_STARTED.search(line)
            info = {"type": m.group(1), "node": None} if m else None
        if info is None:
            return
        cur = self._mission_info or {}
        # OnStateStarted repeats the type its SyncAutoPopulatedConsumables named with a
        # node; a hub node is never carried into the next mission.
        keep_node = info["type"] == cur.get("type") and not HUB_NODE.search(cur.get("node") or "")
        if info["node"] or not keep_node:
            self._mission_info = info

    def on_line(self, line: str) -> None:
        if ("SyncAutoPopulated" in line or "OnStateStarted" in line):
            self._observe_mission_info(line)
            return
        if "LoginDone" in line and LOGIN_LINE.search(line):
            now = self._clock()
            if now - self._last_login_at >= LOGIN_DEDUP_S:
                self._last_login_at = now
                self._pending_login = now + LOGIN_DELAY_S
                self._wake.set()
            return
        if ("EOM" in line or "Abort:" in line) and MISSION_END_LINE.search(line):
            self.on_mission_end(line)

    def on_mission_end(self, line: str = "") -> None:
        if self.source() != "memory" or not self.mission_scan_enabled():
            return
        ended_at = self._line_wall_time(line)
        if HUB_ABORT.search(line):
            return
        if self._last_end_at is not None and abs(ended_at - self._last_end_at) < MISSION_END_JOIN_S:
            # The same mission's later line restarts the wait.
            self._last_end_at = ended_at
            if self._pending_mission is not None:
                self._pending_mission["due"] = ended_at + MISSION_READ_DELAY_S
                self._wake.set()
            return
        info, self._mission_info = self._mission_info, None
        if info and info.get("node") and HUB_NODE.search(info["node"]):
            _log.info("mission end in a hub ignored")
            return
        self._last_end_at = ended_at
        # A late file line may already be past the delay: then the read is immediate.
        self._pending_mission = {"ended_at": ended_at, "due": ended_at + MISSION_READ_DELAY_S, "attempts": 0}
        self._wake.set()

    # ---- scheduling ---------------------------------------------------------------

    def next_auto_at(self) -> float:
        interval = self.interval_s()
        if self.last_run_at is None:
            # Keep the cooldown across restarts: the saved snapshot counts as the last run.
            try:
                return os.path.getmtime(self.inventory_path) + interval
            except OSError:
                return 0.0
        if self.last_run_ok is not True and self.last_reason in LOCAL_REASONS:
            return self.last_run_at + min(LOCAL_FAILURE_RETRY_S, interval)
        return self.last_run_at + interval

    def tick(self) -> None:
        if self._reapply:
            self._reapply = False
            self.apply_settings()
        now = self._clock()
        src = self.source()
        if src == "file":
            if now - self._file_checked >= FILE_POLL_S:
                self._file_checked = now
                path = self.file_setting()
                try:
                    mtime = os.path.getmtime(path) if path else None
                except OSError:
                    mtime = None
                if mtime is not None and mtime != self._file_mtime:
                    _log.info("inventory file changed, reloading")
                    if not self._load_file(path) and self.current is not None and self.current.origin != "file":
                        self._set_current(None)
            return
        if src != "memory":
            return
        if self._pending_mission is not None and now >= self._pending_mission["due"]:
            self.mission_read()
            return
        if self._pending_login is not None and now >= self._pending_login:
            if self.running:
                self._pending_login = now + 0.25
                return
            self._pending_login = None
            if should_retry_after_login(self.running, self.last_run_ok, self.last_reason):
                _log.info("Warframe login completed - refreshing inventory")
                self.run_memory("login")
            return
        if self.auto_enabled() and now >= self.next_auto_at():
            if not self._game_running():
                # Settles like a run that found no game, without touching memory.
                self._settle(False, "game-not-running", "auto")
            else:
                self.run_memory("auto")

    def refresh(self) -> dict:
        """On demand: re-read the file source, or scan + fetch for the memory source."""
        src = self.source()
        if src == "file":
            path = self.file_setting()
            if path:
                self._load_file(path)
        elif src == "memory":
            if not self.run_memory("manual"):
                if self.running:
                    _log.info("inventory refresh already running")
        return self.status()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="inventory", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                _log.exception("inventory tick failed")

    # ---- status -----------------------------------------------------------------

    def status(self) -> dict:
        inv = self.current
        src = self.source()
        return {
            "source": src,
            "file": self.file_setting() if src == "file" else None,
            "found": inv is not None,
            "inventory": inv.summary() if inv else None,
            "running": self.running,
            "last_run_at": self.last_run_at,
            "last_run_ok": self.last_run_ok,
            "last_reason": self.last_reason,
            "last_trigger": self.last_trigger,
            "last_api_at": self.last_api_at,
            "next_auto_at": self.next_auto_at() if src == "memory" and self.auto_enabled() else None,
            "mission_read_pending": self._pending_mission is not None,
            "last_error": self.last_error,
            "last_scan": self.last_scan,
            "last_resident": self.last_resident,
            "warframe_running": bool(self._game_running()),
            "interval_min": self.interval_s() / 60,
        }


# ---- module API (docs/ARCHITECTURE.md) ------------------------------------------

_svc: InventoryService | None = None
# (snapshot, counts): keyed on the object itself, since id() is reused after GC.
_relic_cache: tuple[Inventory, dict] | None = None
# on_update() calls made before setup() are handed to the service there.
_pending_listeners: list[Callable[[Inventory], None]] = []


def service() -> InventoryService | None:
    return _svc


def inventory() -> Inventory | None:
    """Latest snapshot, or None."""
    return _svc.current if _svc else None


def refresh() -> dict:
    """Blocking refresh from the configured source; returns status()."""
    if _svc is None:
        raise RuntimeError("inventory not initialised")
    return _svc.refresh()


def owned_count(unique_name: str) -> int:
    inv = inventory()
    return inv.owned_count(unique_name) if inv else 0


def _relic_name_of() -> Callable[[str], str | None] | None:
    """Projection uniqueName -> "Lith V1 Radiant": the relic db's byUniqueName
    (as WFHelper's parseOwnedRelics), else, per relic, the item db's relic rows.
    relics() may return an empty db instead of raising, so the fallback is per name."""
    rdb = idb = None
    try:
        from ..data.relics import relics

        rdb = relics()
    except Exception:  # noqa: BLE001 - relic data not built
        pass
    try:
        from ..data.items import items

        idb = items()
    except Exception:  # noqa: BLE001 - no item data yet
        pass
    if rdb is None and idb is None:
        return None

    def name_of(unique: str) -> str | None:
        if rdb is not None:
            hit = rdb.by_unique(unique)
            if hit:
                return f"{hit[0].name} {hit[1].capitalize()}"
        if idb is not None:
            it = idb.by_unique(unique)
            if it:
                return it.name
        return None
    return name_of


def owned_relics() -> dict[tuple[str, str], int]:
    """{("Lith V1", "radiant"): count} for the latest snapshot."""
    global _relic_cache
    inv = inventory()
    if inv is None:
        return {}
    if _relic_cache is not None and _relic_cache[0] is inv:
        return _relic_cache[1]
    result = inv.owned_relics(_relic_name_of())
    # An empty answer for an inventory with relics means the data is not loaded yet.
    if result or not inv.relic_counts():
        _relic_cache = (inv, result)
    return result


def status() -> dict:
    return _svc.status() if _svc else {"source": None, "found": False}


def on_update(fn: Callable[[Inventory], None]) -> None:
    """fn(inventory) after every new snapshot (runs on the inventory thread).
    Safe to call before setup(): the listener is attached when the service starts."""
    if _svc is not None:
        _svc.on_update(fn)
    else:
        _pending_listeners.append(fn)


def import_file(path: str) -> dict:
    if _svc is None:
        raise RuntimeError("inventory not initialised")
    return _svc.import_file(path)


def setup(app) -> None:
    global _svc
    _svc = InventoryService(
        app.runtime_dir, app.settings, emit=app.emit,
        ee_log_path=lambda: app.eelog.path,
        line_source=lambda: app.eelog.source,
    )
    for fn in _pending_listeners:
        _svc.on_update(fn)
    _pending_listeners.clear()
    _svc.apply_settings(force=True)
    app.settings.on_change(lambda _data: _svc.settings_changed())
    # File lines (echoes too) date mission ends; merged lines drive the events.
    app.eelog.subscribe(_svc.observe_file_line, source=FILE)
    app.eelog.subscribe(_svc.on_line)
    app.services["inventory"] = _svc
    app.services.setdefault("on_start", []).append(_svc.start)


@rpc.method("inventory.status")
async def _rpc_status() -> dict:
    # Polled by the frontend; status() may walk /proc, so keep it off the loop.
    from ..app import get_app

    return await get_app().run_blocking(status)


@rpc.method("inventory.refresh")
async def _rpc_refresh() -> dict:
    from ..app import get_app

    return await get_app().run_blocking(refresh)


@rpc.method("inventory.import_file")
async def _rpc_import_file(path: str) -> dict:
    from ..app import get_app

    return await get_app().run_blocking(import_file, path)
