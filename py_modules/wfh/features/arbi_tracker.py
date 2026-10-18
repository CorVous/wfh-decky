"""Arbitration run tracker: capture, gzip, index, duplicates, salvage, import.

Port of WFHelper ``services/runTracker.ts`` + ``arbiRunTracker.ts`` +
``runAnnotations.ts`` + ``eeLogSegmentImporter.ts`` (docs/port/arbitration.md
§3-4), specialised for arbitrations (Profit-Taker is not ported).

Files (same names as WFHelper, so a user can copy an index over) live in the
plugin runtime dir: ``arbi-runs.json`` (``{"schemaVersion":1,"runs":[...]}``,
newest first) and ``arbi-logs/<id>.partial.log`` / ``<id>.log.gz``.

Threading: ``process_line`` runs on the EE.log tailer thread and only does regex
work plus a 200-line append; gzip + index writes go through ``submit`` (the
worker pool). Mutations from RPC handlers share one lock with the tailer.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import json
import os
import re
import shutil
import tempfile
import threading
import time
from typing import Callable

from .. import log
from .arbi_parser import EE_LOG_LINE_TS, ArbiParser

_log = log.get("arbi_tracker")

INDEX_SCHEMA_VERSION = 1
INDEX_FILE = "arbi-runs.json"
LOGS_DIR = "arbi-logs"
FLUSH_EVERY_LINES = 200
INACTIVITY_TIMEOUT_MS = 10 * 60_000
INACTIVITY_CHECK_S = 60
RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d+)?$")
DUPLICATE_START_TOLERANCE_MS = 120_000
DUPLICATE_DURATION_TOLERANCE_SEC = 5
MIN_SAVED_ROTATIONS = 2
MAX_SEGMENT_BYTES = 256 * 1024 * 1024
MAX_VITUS = 10_000_000
MAX_TAGS, MAX_TAG_LEN, MAX_NOTES_LEN = 12, 32, 2000
END_REASONS = ("mission-end", "aborted", "new-mission", "log-truncated", "app-quit", "inactivity", "imported")
# A genuine EE.log timestamp prefix ("12.345 Sys [Diag]: ..."), not a number inside a continuation line.
REAL_TS_PREFIX = re.compile(r"^\D{0,2}(\d+\.\d{3}) ")
CURRENT_TIME = re.compile(r"Sys \[Diag\]: Current time: (.+?)(?: \[UTC: (.+?)\])?\s*$")

# Light projection for list views (stats arrays can hold thousands of timestamps).
SLIM_KEYS = ("id", "startedAt", "endedAt", "missionName", "node", "missionType", "missionTypeRaw", "solNode",
             "durationSec", "rotations", "drones", "totalEnemies", "vitusActual", "logFile", "logSizeBytes",
             "endReason", "source", "players", "tags", "notes", "duplicateOf")


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---- annotations (runAnnotations.ts) ------------------------------------------

def normalize_tags(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            continue
        tag = re.sub(r"\s+", " ", entry.strip())[:MAX_TAG_LEN].strip()
        if not tag or tag.lower() in seen:
            continue
        seen.add(tag.lower())
        out.append(tag)
        if len(out) >= MAX_TAGS:
            break
    return out


def normalize_notes(raw) -> str:
    if not isinstance(raw, str):
        return ""
    out: list[str] = []
    length = 0
    for ch in raw.replace("\r\n", "\n").replace("\r", "\n"):
        code = ord(ch)
        if (code < 32 and code not in (9, 10)) or code == 127:
            continue
        n = 2 if code > 0xFFFF else 1           # JS string length counts UTF-16 units
        if length + n > MAX_NOTES_LEN:
            break
        out.append(ch)
        length += n
    return "".join(out).strip()


def clean_vitus(value) -> int | None | bool:
    """IPC guard (arbiIpc.ts): None clears; a number in 0..10M is rounded; anything else -> False."""
    if value is None:
        return None
    if isinstance(value, bool):
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    if not (0 <= v <= MAX_VITUS):
        return False
    return int(v + 0.5)


def format_run_id(ms: float) -> str:
    return _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d_%H-%M-%S")


def skip_reason(parsed: dict) -> str | None:
    if not parsed.get("hostTelemetry"):
        return "client-side run, no AI data in the log"
    if parsed.get("rotations", 0) < MIN_SAVED_ROTATIONS:
        return f"{parsed.get('rotations', 0)} rotation(s)"
    return None


def build_record(run_id: str, started_at: float, parsed: dict, end_reason: str, log_size: int) -> dict:
    end_sec = parsed["runEndSec"] if parsed.get("runEndSec") is not None else parsed["lastActivitySec"]
    elapsed = max(0.0, (end_sec - parsed["runStartSec"]) * 1000)
    started_at = int(round(started_at))
    return {
        "id": run_id, "startedAt": started_at, "endedAt": int(round(started_at + elapsed)),
        "missionName": parsed["missionName"], "node": parsed["node"], "missionType": parsed["missionType"],
        "missionTypeRaw": parsed["missionTypeRaw"], "solNode": parsed["solNode"],
        "durationSec": parsed["durationSec"], "rotations": parsed["rotations"], "drones": parsed["drones"],
        "totalEnemies": parsed["totalEnemies"], "vitusActual": None,
        "logFile": f"{run_id}.log.gz" if log_size > 0 else None, "logSizeBytes": log_size,
        "endReason": end_reason, "source": "live", "stats": parsed["stats"], "players": parsed["players"],
    }


def slim(record: dict) -> dict:
    out = {k: record[k] for k in SLIM_KEYS if k in record}
    stats = record.get("stats")
    if stats:
        out["expectedVitusMean"] = stats.get("expectedVitusMean")
        out["expectedVitusStd"] = stats.get("expectedVitusStd")
    out["hasStats"] = bool(stats)
    return out


def _write_atomic(path: str, data: bytes) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _gzip_file(src: str, dst: str) -> int:
    with open(src, "rb") as fi, gzip.open(dst, "wb") as fo:
        shutil.copyfileobj(fi, fo, 1 << 20)
    return os.path.getsize(dst)


class _Active:
    __slots__ = ("id", "started_at", "captured_at_sec", "partial_path", "pending", "last_combat_at",
                 "last_activity_count")

    def __init__(self, run_id: str, started_at: int, captured_at_sec: float, partial_path: str, first: str):
        self.id = run_id
        self.started_at = started_at
        self.captured_at_sec = captured_at_sec
        self.partial_path = partial_path
        self.pending = [first]
        self.last_combat_at = started_at
        self.last_activity_count = 0


class ArbiRunTracker:
    def __init__(self, base_dir: str, submit: Callable | None = None, now_ms: Callable[[], int] = _now_ms):
        self.base_dir = base_dir
        self._submit = submit or (lambda fn, *a: fn(*a))
        self._now = now_ms
        self._lock = threading.RLock()
        self._saves = threading.Condition(self._lock)
        self._pending_saves = 0
        self.parser = ArbiParser()
        self._active: _Active | None = None
        self.runs: list[dict] = []
        self._reserved: set[str] = set()
        self.on_run_saved: Callable[[dict], None] | None = None
        self.on_run_skipped: Callable[[dict], None] | None = None
        self.tracking_enabled = True
        self.initialized = False
        self.last_skip: dict | None = None
        self._last_ts = 0.0

    # ---- paths -----------------------------------------------------------------
    @property
    def index_path(self) -> str:
        return os.path.join(self.base_dir, INDEX_FILE)

    @property
    def logs_dir(self) -> str:
        return os.path.join(self.base_dir, LOGS_DIR)

    def _gz_path(self, run_id: str) -> str:
        return os.path.join(self.logs_dir, f"{run_id}.log.gz")

    def stored_log_path(self, run: dict) -> str | None:
        """Path-traversal guard: only ``<logs>/<id>.log.gz`` for a well-formed id."""
        rid = run.get("id")
        if not isinstance(rid, str) or not RUN_ID_RE.match(rid) or run.get("logFile") != f"{rid}.log.gz":
            return None
        logs = os.path.realpath(self.logs_dir)
        cand = os.path.realpath(os.path.join(logs, run["logFile"]))
        return cand if os.path.dirname(cand) == logs else None

    def run_log_path(self, run_id: str) -> str | None:
        run = self.get(run_id)
        p = self.stored_log_path(run) if run else None
        return p if p and os.path.exists(p) else None

    # ---- index -------------------------------------------------------------------
    def _unique_id(self, ms: float) -> str:
        base = format_run_id(ms)
        rid, n = base, 2
        while any(r["id"] == rid for r in self.runs) or rid in self._reserved:
            rid = f"{base}-{n}"
            n += 1
        self._reserved.add(rid)
        return rid

    def save_index(self) -> None:
        with self._lock:
            payload = json.dumps({"schemaVersion": INDEX_SCHEMA_VERSION, "runs": self.runs}, separators=(",", ":"))
        try:
            _write_atomic(self.index_path, payload.encode("utf-8"))
        except OSError as e:
            _log.warning("failed to save arbitration run index: %s", e)

    @staticmethod
    def _normalize_annotations(run: dict) -> dict:
        tags = normalize_tags(run.get("tags"))
        if tags:
            run["tags"] = tags
        else:
            run.pop("tags", None)
        notes = normalize_notes(run.get("notes"))
        if notes:
            run["notes"] = notes
        else:
            run.pop("notes", None)
        return run

    def _load_index(self) -> None:
        try:
            with open(self.index_path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            _log.warning("arbitration run index unreadable (%s); quarantined", e)
            try:
                os.replace(self.index_path, f"{self.index_path}.corrupt-{self._now()}")
            except OSError:
                pass
            self.runs = []
            return
        runs = raw.get("runs") if isinstance(raw, dict) else None
        if not isinstance(runs, list):
            return
        out = []
        for r in runs:
            if not isinstance(r, dict) or not isinstance(r.get("id"), str) or not RUN_ID_RE.match(r["id"]):
                continue
            if r.get("logFile") is not None and not self.stored_log_path(r):
                r = {**r, "logFile": None, "logSizeBytes": 0}
            elif r.get("logFile") and not os.path.exists(self.stored_log_path(r) or ""):
                r = {**r, "logFile": None, "logSizeBytes": 0}
            out.append(self._normalize_annotations(r))
        self.runs = out

    # ---- duplicates (runTracker.ts:250-305) --------------------------------------
    @staticmethod
    def _same_run(a: dict, b: dict) -> bool:
        return (abs(a.get("startedAt", 0) - b.get("startedAt", 0)) <= DUPLICATE_START_TOLERANCE_MS
                and abs(a.get("durationSec", 0) - b.get("durationSec", 0)) <= DUPLICATE_DURATION_TOLERANCE_SEC
                and a.get("node") == b.get("node"))

    @staticmethod
    def _richer_first(a: dict, b: dict) -> tuple[dict, dict]:
        rich = (1 if a.get("stats") is not None else 0) - (1 if b.get("stats") is not None else 0)
        if rich:
            return (a, b) if rich > 0 else (b, a)
        if a.get("logSizeBytes", 0) != b.get("logSizeBytes", 0):
            return (a, b) if a.get("logSizeBytes", 0) > b.get("logSizeBytes", 0) else (b, a)
        return (a, b) if a.get("source") == "live" else (b, a)

    @staticmethod
    def _adopt(winner: dict, loser: dict) -> None:
        if "notes" not in winner and "notes" in loser:
            winner["notes"] = loser["notes"]
        if "tags" not in winner and "tags" in loser:
            winner["tags"] = list(loser["tags"])
        if winner.get("vitusActual") is None and loser.get("vitusActual") is not None:
            winner["vitusActual"] = loser["vitusActual"]

    def mark_duplicates(self) -> bool:
        with self._lock:
            winners: dict[str, str] = {}
            runs = self.runs
            for i in range(len(runs)):
                for j in range(i + 1, len(runs)):
                    a, b = runs[i], runs[j]
                    if a.get("source") == b.get("source") or not self._same_run(a, b):
                        continue
                    w, lo = self._richer_first(a, b)
                    winners.setdefault(lo["id"], w["id"])

            def resolve(rid: str, depth: int = 0) -> str:
                nxt = winners.get(rid)
                return rid if nxt is None or depth >= 4 else resolve(nxt, depth + 1)

            changed = False
            for run in runs:
                raw = winners.get(run["id"])
                target = None if raw is None else resolve(raw)
                if target == run.get("duplicateOf"):
                    continue
                if target is None or target == run["id"]:
                    run.pop("duplicateOf", None)
                else:
                    run["duplicateOf"] = target
                    winner = next((r for r in runs if r["id"] == target), None)
                    if winner:
                        self._adopt(winner, run)
                changed = True
            return changed

    def _add_record(self, record: dict) -> None:
        with self._lock:
            self.runs.insert(0, record)
            self.mark_duplicates()
        self.save_index()
        _log.info("arbitration run saved: %s (%s), %d rotations, %d drones, end=%s", record["node"],
                  record["missionType"], record["rotations"], record["drones"], record["endReason"])
        if self.on_run_saved:
            try:
                self.on_run_saved(record)
            except Exception:  # noqa: BLE001
                _log.exception("run-saved callback failed")

    # ---- init / salvage -------------------------------------------------------------
    def init(self) -> None:
        with self._lock:
            self._load_index()
            if self.mark_duplicates():
                self.save_index()
            self._salvage()
            self.initialized = True
        _log.info("arbitration tracker ready: %d run(s)", len(self.runs))

    def _salvage(self) -> None:
        try:
            files = [f for f in os.listdir(self.logs_dir) if f.endswith(".partial.log")]
        except OSError:
            return
        for name in files:
            path = os.path.join(self.logs_dir, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
                p = ArbiParser()
                for line in re.split(r"\r?\n", content):
                    p.feed_line(line)
                parsed = p.finalize()
                if not parsed or skip_reason(parsed):
                    os.unlink(path)
                    continue
                mtime = os.path.getmtime(path) * 1000
                end_sec = parsed["runEndSec"] if parsed["runEndSec"] is not None else parsed["lastActivitySec"]
                started_at = mtime - max(0.0, end_sec - parsed["runStartSec"]) * 1000
                rid = self._unique_id(started_at)
                size = _gzip_file(path, self._gz_path(rid))
                os.unlink(path)
                self._add_record(build_record(rid, started_at, parsed, "log-truncated", size))
                self._reserved.discard(rid)
                _log.info("salvaged interrupted arbitration run from %s", name)
            except Exception as e:  # noqa: BLE001
                _log.warning("failed to salvage %s: %s", name, e)

    # ---- live capture -----------------------------------------------------------------
    def _flush(self, run: _Active) -> None:
        if not run.pending:
            return
        try:
            os.makedirs(self.logs_dir, exist_ok=True)
            with open(run.partial_path, "a", encoding="utf-8") as f:
                f.write("\n".join(run.pending) + "\n")
        except OSError as e:
            _log.warning("failed to append arbitration capture: %s", e)
        run.pending = []

    def _start_capture(self, game_ts: float, first_line: str) -> None:
        now = self._now()
        rid = self._unique_id(now)
        self._active = _Active(rid, now, game_ts, os.path.join(self.logs_dir, f"{rid}.partial.log"), first_line)
        _log.info("arbitration capture started: %s", rid)

    def _finalize(self, reason: str, sync: bool = False) -> None:
        with self._lock:
            run = self._active
            if run is None:
                return
            self._active = None
            parsed = self.parser.finalize()
            if not parsed:
                self._reserved.discard(run.id)
                return
            skip = skip_reason(parsed)
            if skip:
                self._reserved.discard(run.id)
                try:
                    os.unlink(run.partial_path)
                except OSError:
                    pass
                self.last_skip = {"node": parsed["node"], "reason": skip, "rotations": parsed["rotations"],
                                  "hostTelemetry": parsed["hostTelemetry"], "at": self._now(), "endReason": reason}
                _log.info("arbitration run not saved (%s)", skip)
                cb = self.on_run_skipped
                skipped = self.last_skip
            else:
                cb = skipped = None
                self._flush(run)
                started_at = run.started_at + (parsed["runStartSec"] - run.captured_at_sec) * 1000
                self._pending_saves += 1
        if skipped is not None:
            if cb:
                try:
                    cb(skipped)
                except Exception:  # noqa: BLE001
                    _log.exception("run-skipped callback failed")
            return

        def save() -> None:
            try:
                size = 0
                try:
                    size = _gzip_file(run.partial_path, self._gz_path(run.id))
                    try:
                        os.unlink(run.partial_path)
                    except OSError:
                        pass
                except OSError as e:
                    _log.warning("gzip of arbitration capture failed: %s", e)
                self._add_record(build_record(run.id, started_at, parsed, reason, size))
            finally:
                with self._lock:
                    self._reserved.discard(run.id)
                    self._pending_saves -= 1
                    self._saves.notify_all()

        if sync:
            save()
        else:
            self._submit(save)

    @staticmethod
    def _is_log_reset(line: str) -> bool:
        """Warframe restarted: a fresh EE.log starts with the 'Current time' header near ts 0.

        Only the header line counts. A bare "ts went backwards" test misfires on multi-line
        entries (JSON/Lua continuation lines such as '"scale": 0.5,'), and the tailer already
        re-reads a recreated EE.log from offset 0, so the header is always seen.
        """
        if "Sys [Diag]: Current time:" not in line:
            return False
        m = REAL_TS_PREFIX.match(line)
        return bool(m) and float(m.group(1)) < 60

    def process_line(self, line: str) -> None:
        if not self.tracking_enabled or not self.initialized:
            return
        if line and self._is_log_reset(line):
            self.notify_log_reset()
        m = REAL_TS_PREFIX.match(line) if line else None
        if m:
            self._last_ts = float(m.group(1))
        with self._lock:
            ev = self.parser.feed_line(line)
            if ev is None:
                run = self._active
                if run is not None:
                    run.pending.append(line)
                    count = self.parser.activity_count()
                    if count != run.last_activity_count:
                        run.last_activity_count = count
                        run.last_combat_at = self._now()
                    if len(run.pending) >= FLUSH_EVERY_LINES:
                        self._flush(run)
                return
            if ev["type"] == "run-start":
                self._start_capture(ev["gameTimeSec"], line)
                return
        # run-end: finalize, then re-feed the same line (back-to-back missions).
        self._finalize(ev["reason"])
        with self._lock:
            nxt = self.parser.feed_line(line)
            if nxt and nxt["type"] == "run-start":
                self._start_capture(nxt["gameTimeSec"], line)

    def check_inactivity(self) -> bool:
        """Called every minute: finalize a run with no combat events for 10 min."""
        with self._lock:
            run = self._active
            if run is None:
                return False
            if self._now() - run.last_combat_at <= INACTIVITY_TIMEOUT_MS:
                self._flush(run)   # keep the partial current for crash salvage
                return False
        _log.info("no arbitration activity for 10 min - finalizing")
        self._finalize("inactivity")
        return True

    def notify_log_reset(self) -> None:
        self._last_ts = 0.0
        if self._active is not None:
            _log.info("EE.log reset mid-run - finalizing")
            self._finalize("log-truncated")
        with self._lock:
            self.parser.reset()

    def set_tracking_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.tracking_enabled:
            return
        self.tracking_enabled = enabled
        if not enabled:
            with self._lock:
                run = self._active
                self._active = None
                self.parser = ArbiParser()
                if run:
                    self._reserved.discard(run.id)
                    try:
                        os.unlink(run.partial_path)
                    except OSError:
                        pass

    def shutdown(self) -> None:
        if self._active is not None:
            self._finalize("app-quit", sync=True)

    def await_pending(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._pending_saves > 0:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._saves.wait(left)

    # ---- status / queries ---------------------------------------------------------------
    def active(self) -> dict | None:
        with self._lock:
            run = self._active
            cur = self.parser.current() if run else None
            if cur is None:
                return None
            return {**cur, "id": run.id, "startedAt": run.started_at}

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            return next((r for r in self.runs if r["id"] == run_id), None)

    def disk_usage_bytes(self) -> int:
        total = 0
        try:
            for f in os.listdir(self.logs_dir):
                if f.endswith(".log.gz"):
                    try:
                        total += os.path.getsize(os.path.join(self.logs_dir, f))
                    except OSError:
                        pass
        except OSError:
            return 0
        return total

    # ---- mutations --------------------------------------------------------------------------
    def update(self, run_id: str, mutate: Callable[[dict], None]) -> dict | None:
        with self._lock:
            run = self.get(run_id)
            if run is None:
                return None
            mutate(run)
        self.save_index()
        return run

    def set_vitus(self, run_id: str, vitus: int | None) -> dict | None:
        return self.update(run_id, lambda r: r.__setitem__("vitusActual", vitus))

    def set_tags(self, run_id: str, tags) -> dict | None:
        def m(r: dict) -> None:
            clean = normalize_tags(tags)
            if clean:
                r["tags"] = clean
            else:
                r.pop("tags", None)
        return self.update(run_id, m)

    def set_notes(self, run_id: str, notes) -> dict | None:
        def m(r: dict) -> None:
            clean = normalize_notes(notes)
            if clean:
                r["notes"] = clean
            else:
                r.pop("notes", None)
        return self.update(run_id, m)

    def delete_log(self, run_id: str) -> dict | None:
        with self._lock:
            run = self.get(run_id)
            if run is None:
                return None
            p = self.stored_log_path(run)
            if not p:
                return run
            try:
                os.unlink(p)
            except OSError as e:
                _log.warning("failed to delete run log: %s", e)
            run["logFile"] = None
            run["logSizeBytes"] = 0
        self.save_index()
        return run

    def delete_run(self, run_id: str) -> bool:
        with self._lock:
            idx = next((i for i, r in enumerate(self.runs) if r["id"] == run_id), -1)
            if idx < 0:
                return False
            p = self.stored_log_path(self.runs[idx])
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            del self.runs[idx]
            self.mark_duplicates()
        self.save_index()
        return True

    def read_log(self, run_id: str) -> str | None:
        p = self.run_log_path(run_id)
        if not p:
            return None
        with gzip.open(p, "rt", encoding="utf-8", errors="replace") as f:
            return f.read()

    # ---- import (eeLogSegmentImporter.ts) ---------------------------------------------------
    def add_imported(self, parsed: dict, started_at: float, segment_path: str, reason: str = "imported") -> dict | None:
        def remove_segment() -> None:
            try:
                os.unlink(segment_path)
            except OSError:
                pass

        if skip_reason(parsed):
            remove_segment()
            return None
        rid = format_run_id(started_at)
        with self._lock:
            if any(r["id"] == rid for r in self.runs) or rid in self._reserved:
                remove_segment()
                return None
            self._reserved.add(rid)
        size = 0
        try:
            os.makedirs(self.logs_dir, exist_ok=True)
            size = _gzip_file(segment_path, self._gz_path(rid))
        except OSError as e:
            _log.warning("failed to write imported run gz: %s", e)
            try:
                os.unlink(self._gz_path(rid))
            except OSError:
                pass
        finally:
            remove_segment()
        record = build_record(rid, started_at, parsed, reason, size)
        record["source"] = "imported"
        try:
            self._add_record(record)
        finally:
            with self._lock:
                self._reserved.discard(rid)
        return record

    def import_ee_log(self, file_path: str, max_segment_bytes: int = MAX_SEGMENT_BYTES) -> dict:
        """Split an EE.log into arbitration runs; ``{imported: [slim records], skipped: n}``."""
        imported: list[dict] = []
        skipped = 0
        cap = min(int(max_segment_bytes), MAX_SEGMENT_BYTES) if max_segment_bytes and max_segment_bytes > 0 \
            else MAX_SEGMENT_BYTES
        tmp_dir = tempfile.mkdtemp(prefix="arbi-import-", dir=self.base_dir if os.path.isdir(self.base_dir) else None)
        try:
            mtime_ms = os.path.getmtime(file_path) * 1000
        except OSError:
            mtime_ms = float(self._now())
        parser = ArbiParser()
        zero_ms: float | None = None
        last_ts = 0.0
        seg = {"f": None, "path": None, "bytes": 0, "truncated": False}
        idx = 0

        def started_for(parsed: dict) -> float:
            if zero_ms is not None:
                return zero_ms + parsed["runStartSec"] * 1000
            return mtime_ms - max(0.0, last_ts - parsed["runStartSec"]) * 1000

        def close_seg() -> str | None:
            f, path = seg["f"], seg["path"]
            seg.update(f=None, path=None, bytes=0, truncated=False)
            if f is None:
                return None
            f.close()
            return path

        def discard_seg() -> None:
            path = close_seg()
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

        def capture(line: str) -> None:
            if seg["f"] is None or seg["truncated"]:
                return
            n = len(line.encode("utf-8")) + 1
            if seg["bytes"] + n > cap:
                seg["truncated"] = True
                _log.warning("imported arbitration segment exceeds cap - capture truncated")
                return
            seg["bytes"] += n
            seg["f"].write(line + "\n")

        def start_seg(line: str) -> None:
            nonlocal idx
            discard_seg()
            path = os.path.join(tmp_dir, f"segment-{idx}.log")
            idx += 1
            seg.update(f=open(path, "w", encoding="utf-8"), path=path, bytes=0, truncated=False)
            capture(line)

        def finish(reason: str) -> None:
            nonlocal skipped
            parsed = parser.finalize()
            path = close_seg()
            if not parsed or not path:
                return
            rec = self.add_imported(parsed, started_for(parsed), path, reason)
            if rec:
                imported.append(slim(rec))
            else:
                skipped += 1

        try:
            with open(file_path, encoding="utf-8", errors="replace", newline="") as f:
                for raw in f:
                    line = raw.rstrip("\r\n")
                    if zero_ms is None:
                        tm = CURRENT_TIME.search(line)
                        if tm:
                            wall = _parse_header_time(tm.group(2)) if tm.group(2) else None
                            if wall is None:
                                wall = _parse_header_time(tm.group(1), local=True)
                            tsm = EE_LOG_LINE_TS.search(line)
                            if wall is not None:
                                zero_ms = wall - (float(tsm.group(1)) if tsm else 0.0) * 1000
                    tsm = EE_LOG_LINE_TS.search(line)
                    if tsm:
                        last_ts = max(last_ts, float(tsm.group(1)))
                    ev = parser.feed_line(line)
                    if ev and ev["type"] == "run-start":
                        start_seg(line)
                        continue
                    if ev and ev["type"] == "run-end":
                        finish(ev["reason"])
                        nxt = parser.feed_line(line)
                        if nxt and nxt["type"] == "run-start":
                            start_seg(line)
                        continue
                    if parser.is_run_active():
                        capture(line)
            if parser.is_run_active():
                finish("imported")
        except Exception as e:  # noqa: BLE001
            _log.warning("arbitration import failed: %s", e)
            parser.reset()
        finally:
            discard_seg()
            shutil.rmtree(tmp_dir, ignore_errors=True)
        _log.info("arbitration import: %d imported, %d skipped", len(imported), skipped)
        return {"imported": imported, "skipped": skipped}


def _parse_header_time(s: str, local: bool = False) -> float | None:
    """'Tue Jul  7 21:40:49 2026' -> epoch ms (UTC unless ``local``)."""
    try:
        d = _dt.datetime.strptime(" ".join(s.split()), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    if not local:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.timestamp() * 1000
