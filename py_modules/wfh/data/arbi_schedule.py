"""Arbitration schedule from browse.wf/arbys.txt, plus favourite/bell alerts.

Port of WFHelper ``services/arbiSchedule.ts`` (docs/port/arbitration.md §5).
The file is static future data (one ``epochSeconds,NodeId`` line per hour), so
it is fetched at most hourly, cached on disk as ``arbi-sched-cache.json`` in the
runtime dir and reparsed on start: a Deck that is offline for a week still
shows the right arbitrations. Labels come from the bundled solnodes table.

Alerts: one-shot bells per occurrence (``"<epochMs>:<nodeId>"``) and favourite
nodes that alert on every occurrence, ``minutesBefore`` ahead. They live in
``arbi-sched-alerts.json`` (same file names as WFHelper so a user can copy them
over); favourites are mirrored into ``settings.arbitration.favorite_nodes`` so
the arbitration overlay can read them without importing this module.
Delivery is ``app.toast`` (works with the QAM closed) plus an
``arbi.schedule_alert`` event; ``arbitration.notify_favorites`` gates both.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .. import log, net, rpc
from . import solnodes

_log = log.get("arbi_schedule")

ARBYS_URL = "https://browse.wf/arbys.txt"
FETCH_TIMEOUT = 15
REFRESH_INTERVAL_MS = 60 * 60_000
STALE_AFTER_MS = 55 * 60_000
FIRST_FETCH_DELAY = 5.0
ALERT_CHECK = 30.0
RETRY_AFTER_FAILURE_MS = 10 * 60_000   # Deck addition: do not hammer while offline
PAST_WINDOW_MS = 60 * 60_000
YEAR_MS = 365 * 24 * 60 * 60_000
FIRED_RETENTION_MS = 2 * 60 * 60_000
MIN_LEAD, MAX_LEAD, DEFAULT_LEAD = 1, 120, 5
DAYS_OPTIONS = (7, 14, 30, 60)
DEFAULT_DAYS = 30
STORE_SCHEMA = 1
CACHE_FILE = "arbi-sched-cache.json"
STORE_FILE = "arbi-sched-alerts.json"

NODE_ID_RE = re.compile(r"^[A-Za-z0-9_./:-]{2,120}$")
OCCURRENCE_KEY_RE = re.compile(r"^\d{10,17}:[A-Za-z0-9_./:-]{2,120}$")


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class Entry:
    epoch_ms: int
    node_id: str
    node: str
    mission: str
    faction: str

    @property
    def key(self) -> str:
        return f"{self.epoch_ms}:{self.node_id}"

    def to_json(self) -> dict:
        return {"epochMs": self.epoch_ms, "nodeId": self.node_id, "node": self.node,
                "mission": self.mission, "faction": self.faction, "key": self.key}


# ---- pure functions (tested directly) ------------------------------------

def parse_arbys_text(text: str) -> list[tuple[float, str]]:
    """``epochSeconds,NodeId`` lines -> [(epoch, nodeId)]; malformed lines are skipped."""
    rows: list[tuple[float, str]] = []
    for raw in str(text or "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        try:
            epoch = float(parts[0])
        except ValueError:
            continue
        node_id = parts[1].strip()
        if not math.isfinite(epoch) or epoch <= 0 or not NODE_ID_RE.match(node_id):
            continue
        rows.append((epoch, node_id))
    return rows


def build_entries(rows: list[tuple[float, str]]) -> list[Entry]:
    return [Entry(int(round(epoch * 1000)), nid, solnodes.node_label(nid), solnodes.node_mission(nid),
                  solnodes.node_faction(nid)) for epoch, nid in rows]


def filter_window(entries: list[Entry], now_ms: int, days: float | None = None) -> list[Entry]:
    """The still-active hour plus up to a year (or ``days``) ahead, ascending."""
    ahead = YEAR_MS if days is None else min(YEAR_MS, int(days * 86_400_000))
    return sorted((e for e in entries if now_ms - PAST_WINDOW_MS <= e.epoch_ms <= now_ms + ahead),
                  key=lambda e: e.epoch_ms)


def compute_due_alerts(entries: list[Entry], occurrences: list[str], favorites: list[str],
                       minutes_before: int, fired: set[str], now_ms: int) -> list[Entry]:
    """Entries inside their lead window that are belled or on a favourite node and not yet fired."""
    lead = max(0, minutes_before) * 60_000
    occ, fav = set(occurrences), set(favorites)
    due = []
    for e in entries:
        if now_ms < e.epoch_ms - lead or now_ms >= e.epoch_ms:
            continue
        if e.key in fired:
            continue
        if e.key not in occ and e.node_id not in fav:
            continue
        due.append(e)
    return due


def prune_keys(keys: list[str], now_ms: int) -> list[str]:
    out = []
    for k in keys:
        try:
            if int(k.split(":")[0]) >= now_ms - FIRED_RETENTION_MS:
                out.append(k)
        except ValueError:
            continue
    return out


def alert_text(entry: Entry, now_ms: int) -> tuple[str, str]:
    minutes = max(0, round((entry.epoch_ms - now_ms) / 60_000))
    timing = f"starts in ~{minutes} min" if minutes > 0 else "starting now"
    return "Arbitration Alert", f"{entry.mission} on {entry.node} vs {entry.faction} - {timing}."


def faction_badge(faction: str) -> str:
    """Badge key for the UI (ArbiSchedule.svelte)."""
    f = (faction or "").lower()
    if f in ("grineer", "corpus"):
        return f
    if f.startswith("infest"):
        return "infested"
    if f.startswith("corrupt") or f == "orokin":
        return "corrupted"
    return "other"


def _sanitize_store(raw) -> dict:
    c = raw if isinstance(raw, dict) else {}

    def strings(v, pattern: re.Pattern) -> list[str]:
        if not isinstance(v, list):
            return []
        return list(dict.fromkeys(s for s in v if isinstance(s, str) and pattern.match(s)))

    try:
        minutes = float(c.get("minutesBefore"))
    except (TypeError, ValueError):
        minutes = float("nan")
    return {
        "schemaVersion": STORE_SCHEMA,
        "occurrences": strings(c.get("occurrences"), OCCURRENCE_KEY_RE),
        "favoriteNodes": strings(c.get("favoriteNodes"), NODE_ID_RE),
        "minutesBefore": int(minutes) if math.isfinite(minutes) and MIN_LEAD <= minutes <= MAX_LEAD else DEFAULT_LEAD,
        "firedKeys": strings(c.get("firedKeys"), OCCURRENCE_KEY_RE),
    }


def _write_atomic(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---- stateful schedule ----------------------------------------------------

class ArbiSchedule:
    """Cached schedule + alert store. One instance per worker (``get()``)."""

    def __init__(self, store_dir: str, fetch: Callable[[], str] | None = None,
                 now_ms: Callable[[], int] = _now_ms):
        self.store_dir = store_dir
        self._fetch_text = fetch or (lambda: net.fetch(ARBYS_URL, timeout=FETCH_TIMEOUT).decode("utf-8", "replace"))
        self._now = now_ms
        self._lock = threading.RLock()
        self._fetch_lock = threading.Lock()
        self.entries: list[Entry] = []
        self.fetched_at: int | None = None
        self.last_error: str | None = None
        self._last_attempt = 0
        self.store = _sanitize_store({})
        # Hooks set by setup(): notifications toggle, favourites mirror, delivery.
        self.notifications_enabled: Callable[[], bool] = lambda: True
        self.extra_favorites: Callable[[], list[str]] = lambda: []
        self.on_favorites_changed: Callable[[list[str]], None] = lambda favs: None
        self.notify: Callable[[str, str, Entry], None] = lambda title, body, entry: None
        self._load_store()
        self._load_cache()

    # disk ----------------------------------------------------------------
    @property
    def cache_path(self) -> str:
        return os.path.join(self.store_dir, CACHE_FILE)

    @property
    def store_path(self) -> str:
        return os.path.join(self.store_dir, STORE_FILE)

    def _load_cache(self) -> None:
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                raw = json.load(f)
            fetched = raw.get("fetchedAt")
            if not isinstance(raw.get("raw"), str) or not isinstance(fetched, (int, float)):
                return
            self.entries = build_entries(parse_arbys_text(raw["raw"]))
            self.fetched_at = int(fetched)
            _log.info("loaded %d cached arbitration entries", len(self.entries))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as e:
            _log.warning("arbitration schedule cache unreadable: %s", e)

    def _load_store(self) -> None:
        try:
            with open(self.store_path, encoding="utf-8") as f:
                self.store = _sanitize_store(json.load(f))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            _log.warning("arbitration alert store unreadable: %s", e)

    def _save_store(self) -> None:
        try:
            _write_atomic(self.store_path, json.dumps(self.store, indent=2))
        except OSError as e:
            _log.warning("failed to save arbitration alert store: %s", e)

    # fetch -----------------------------------------------------------------
    def is_stale(self) -> bool:
        return self.fetched_at is None or self._now() - self.fetched_at > STALE_AFTER_MS

    def refresh(self) -> bool:
        """Fetch arbys.txt; on any failure keep the previous (or disk-cached) schedule."""
        if not self._fetch_lock.acquire(blocking=False):
            # Another thread is fetching: wait for it and share its result.
            with self._fetch_lock:
                return self.last_error is None
        try:
            self._last_attempt = self._now()
            try:
                text = self._fetch_text()
                rows = parse_arbys_text(text)
                if not rows:
                    raise ValueError("no valid arbitration entries")
            except Exception as e:  # noqa: BLE001 - network, HTTP or parse: all keep old data
                self.last_error = f"{type(e).__name__}: {e}"
                _log.warning("arbitration schedule fetch failed: %s", self.last_error)
                return False
            entries = build_entries(rows)
            with self._lock:
                self.entries = entries
                self.fetched_at = self._now()
                self.last_error = None
            try:
                _write_atomic(self.cache_path, json.dumps({"fetchedAt": self.fetched_at, "raw": text}))
            except OSError as e:
                _log.warning("failed to save arbitration schedule cache: %s", e)
            _log.info("fetched %d arbitration entries", len(entries))
            return True
        finally:
            self._fetch_lock.release()

    def ensure_fresh(self) -> None:
        """Refetch when older than 55 min; after a failure wait 10 min before trying again."""
        if not self.is_stale():
            return
        if self.last_error and self._now() - self._last_attempt < RETRY_AFTER_FAILURE_MS:
            return
        self.refresh()

    # read ------------------------------------------------------------------
    def favorites(self) -> list[str]:
        extra = [n for n in (self.extra_favorites() or []) if isinstance(n, str) and NODE_ID_RE.match(n)]
        return list(dict.fromkeys(self.store["favoriteNodes"] + extra))

    def alerts(self) -> dict:
        return {"occurrences": list(self.store["occurrences"]), "favoriteNodes": self.favorites(),
                "minutesBefore": self.store["minutesBefore"]}

    def upcoming(self, days: float | None = None) -> list[Entry]:
        with self._lock:
            return filter_window(self.entries, self._now(), days)

    def payload(self, days: float | None = DEFAULT_DAYS, fetch: bool = True) -> dict:
        if fetch:
            self.ensure_fresh()
        now = self._now()
        entries = self.upcoming(days)
        nxt = next((e for e in entries if e.epoch_ms + 3_600_000 > now), None)
        return {
            "entries": [e.to_json() for e in entries],
            "fetchedAt": self.fetched_at,
            "stale": self.is_stale(),
            "error": self.last_error,
            "alerts": self.alerts(),
            "current": nxt.to_json() if nxt and nxt.epoch_ms <= now else None,
            "next": next((e.to_json() for e in entries if e.epoch_ms > now), None),
            "now": now,
        }

    # alert prefs -------------------------------------------------------------
    def set_occurrence(self, key: str, enabled: bool) -> dict | None:
        if not isinstance(key, str) or not OCCURRENCE_KEY_RE.match(key):
            return None
        occ = self.store["occurrences"]
        if enabled and key not in occ:
            occ.append(key)
        elif not enabled and key in occ:
            occ.remove(key)
        self._save_store()
        return self.alerts()

    def set_favorite(self, node_id: str, enabled: bool) -> dict | None:
        if not isinstance(node_id, str) or not NODE_ID_RE.match(node_id):
            return None
        favs = self.favorites()
        if enabled and node_id not in favs:
            favs.append(node_id)
        elif not enabled:
            favs = [n for n in favs if n != node_id]
        self.store["favoriteNodes"] = favs
        self._save_store()
        try:
            self.on_favorites_changed(list(favs))
        except Exception:  # noqa: BLE001
            _log.exception("favourite mirror failed")
        return self.alerts()

    def set_lead(self, minutes) -> dict | None:
        try:
            m = float(minutes)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(m) or m < MIN_LEAD or m > MAX_LEAD:
            return None
        self.store["minutesBefore"] = int(m)
        self._save_store()
        return self.alerts()

    # alert sweep -------------------------------------------------------------
    def check_alerts(self) -> list[Entry]:
        """One 30 s sweep: prune old keys, fire due bells/favourites once."""
        with self._lock:
            if not self.entries:
                return []
            now = self._now()
            occ = prune_keys(self.store["occurrences"], now)
            fired = prune_keys(self.store["firedKeys"], now)
            changed = occ != self.store["occurrences"] or fired != self.store["firedKeys"]
            self.store["occurrences"], self.store["firedKeys"] = occ, fired
            # With notifications off bells stay unconsumed; they fire later or get pruned.
            if not self.notifications_enabled():
                if changed:
                    self._save_store()
                return []
            due = compute_due_alerts(self.entries, occ, self.favorites(), self.store["minutesBefore"],
                                     set(fired), now)
            for e in due:
                self.store["firedKeys"].append(e.key)
                if e.key in self.store["occurrences"]:
                    self.store["occurrences"].remove(e.key)
            if due or changed:
                self._save_store()
        for e in due:
            title, body = alert_text(e, now)
            try:
                self.notify(title, body, e)
            except Exception:  # noqa: BLE001
                _log.exception("arbitration alert delivery failed")
        return due


_instance: ArbiSchedule | None = None
_instance_lock = threading.Lock()


def get() -> ArbiSchedule:
    global _instance
    with _instance_lock:
        if _instance is None:
            from ..app import get_app

            _instance = ArbiSchedule(get_app().runtime_dir)
        return _instance


def set_instance(inst: ArbiSchedule | None) -> None:
    """Tests: swap in an instance with a fake fetcher and clock."""
    global _instance
    with _instance_lock:
        _instance = inst


def schedule(days: float | None = None) -> list[dict]:
    """Upcoming entries (still-active hour first), fetching when older than 55 min."""
    s = get()
    s.ensure_fresh()
    return [e.to_json() for e in s.upcoming(days)]


def setup(app) -> None:
    """Wire settings and delivery; start the hourly refresh + 30 s alert loop."""
    s = get()
    s.notifications_enabled = lambda: bool(app.settings.get("arbitration.notify_favorites", True))
    s.extra_favorites = lambda: list(app.settings.get("arbitration.favorite_nodes", []) or [])
    s.on_favorites_changed = lambda favs: app.settings.update({"arbitration": {"favorite_nodes": favs}})

    def notify(title: str, body: str, entry: Entry) -> None:
        app.toast(title, body)
        app.emit("arbi.schedule_alert", {"title": title, "body": body, "entry": entry.to_json()})

    s.notify = notify
    if os.environ.get("WFH_HEADLESS"):
        return  # tests and CLI runs never touch the network on their own

    def loop() -> None:
        time.sleep(FIRST_FETCH_DELAY)
        while True:
            try:
                if s.fetched_at is None or s._now() - s.fetched_at > REFRESH_INTERVAL_MS:
                    s.ensure_fresh()
                s.check_alerts()
            except Exception:  # noqa: BLE001
                _log.exception("arbitration schedule loop failed")
            time.sleep(ALERT_CHECK)

    threading.Thread(target=loop, name="wfh-arbi-sched", daemon=True).start()


# ---- RPC -------------------------------------------------------------------

def _days(days) -> float:
    try:
        d = float(days)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return d if math.isfinite(d) and d > 0 else DEFAULT_DAYS


@rpc.method("world.arbi_schedule")
async def _rpc_schedule(days: float = DEFAULT_DAYS) -> dict:
    """``{entries[{epochMs,nodeId,node,mission,faction,key}], fetchedAt, stale, error, alerts, current, next, now}``."""
    from ..app import get_app

    return await get_app().run_blocking(lambda: get().payload(_days(days)))


@rpc.method("world.arbi_refresh")
async def _rpc_refresh(days: float = DEFAULT_DAYS) -> dict:
    from ..app import get_app

    def run() -> dict:
        get().refresh()
        return get().payload(_days(days), fetch=False)

    return await get_app().run_blocking(run)


@rpc.method("world.arbi_set_occurrence")
def _rpc_set_occurrence(key: str, enabled: bool = True) -> dict | None:
    return get().set_occurrence(key, bool(enabled))


@rpc.method("world.arbi_set_favorite")
def _rpc_set_favorite(node_id: str, enabled: bool = True) -> dict | None:
    return get().set_favorite(node_id, bool(enabled))


@rpc.method("world.arbi_set_lead")
def _rpc_set_lead(minutes: float) -> dict | None:
    return get().set_lead(minutes)
