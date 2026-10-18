"""Missions page / QAM: rewards of each mission from inventory diffs (docs/port/menus.md §7).

Port of WFHelper services/missionRewards.ts + missionRewardsDiff.ts + src/lib/missionRewardRows.ts.
The inventory service (wfh.inventory) already reads the game's resident inventory 3 s
after each EE.log mission end and publishes it (on_update); this module only keeps the
pre-mission baseline, notes the mission end / type / node from EE.log (cheap, on the
tailer thread) and, when a snapshot synced after the end arrives, records the gains:
stacked counts per ItemType, new ItemId entries (gear), credits and endo. Spending is
ignored. PendingRecipes / PendingTrades / PersonalTechProjects never count. Ends that
no fresh read covered are carried into the next one (missionCount > 1).

History: <runtime>/mission-rewards.json (newest MAX_HISTORY summaries).
Rows: plat from the cached market price (rank 0, else rankless; never network here,
missing prices are prefetched in the background), ducats from the item db.

RPCs
  missions.page(period, since_ms?, mission_type?, offset, limit)   list + totals
  missions.last()                      latest summary with rows
  missions.qam()                       latest summary + today's count/plat
  missions.status()
  missions.set_enabled(enabled)        setting missions.enabled
  missions.delete(id) / missions.clear()
Event: missions.updated {id} after a summary is recorded.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Callable

from .. import log, rpc
from ..eelog import FILE, REALTIME, UptimeTracker

_log = log.get("menus.missions")

HISTORY_FILE = "mission-rewards.json"
MAX_HISTORY = 2000
NON_REWARD_ARRAYS = frozenset({"PendingRecipes", "PendingTrades", "PersonalTechProjects"})
MISSION_END_JOIN_S = 15.0
SYNC_TOLERANCE_S = 30.0
LATE_END_S = 60.0
PERIODS = ("today", "7d", "30d", "all")
DAY_MS = 86_400_000

MISSION_END_LINE = re.compile(r"Sys \[Info\]: EOM missionLocationUnlocked=|TopMenu\.lua: Abort:")
HUB_ABORT = re.compile(r"TopMenu\.lua: Abort: in hub")
HUB_NODE = re.compile(r"hub", re.I)
SYNC_CONSUMABLES = re.compile(r"SyncAutoPopulatedConsumables for mission (MT_[A-Z_]+) with location (\S+)")
STATE_STARTED = re.compile(r"Game \[Info\]: OnStateStarted, mission type=(MT_[A-Z_]+)")


# ---- diff (missionRewardsDiff.ts) -------------------------------------------------------------

def _item_id(entry: dict) -> str | None:
    i = entry.get("ItemId")
    if isinstance(i, str):
        return i or None
    if isinstance(i, dict) and isinstance(i.get("$oid"), str) and i["$oid"]:
        return i["$oid"]
    return None


def _finite(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) == float("inf"):
        return 0
    return v


def snapshot(inv: Any) -> dict | None:
    """{stacks: {uniqueName: count}, ids: {ItemId: uniqueName}, credits, endo}."""
    if not isinstance(inv, dict):
        return None
    stacks: dict[str, float] = {}
    ids: dict[str, str] = {}
    for name, value in inv.items():
        if not isinstance(value, list):
            continue
        counts_ids = name not in NON_REWARD_ARRAYS
        for e in value:
            if not isinstance(e, dict):
                continue
            u = e.get("ItemType")
            if not isinstance(u, str) or not u:
                continue
            c = e.get("ItemCount")
            if isinstance(c, (int, float)) and not isinstance(c, bool):
                if c == c and abs(c) != float("inf"):
                    stacks[u] = stacks.get(u, 0) + c
                continue
            if not counts_ids:
                continue
            i = _item_id(e)
            if i:
                ids[i] = u
    return {"stacks": stacks, "ids": ids, "credits": _finite(inv.get("RegularCredits")),
            "endo": _finite(inv.get("FusionPoints"))}


def diff(before: dict, after: dict) -> dict:
    gained: dict[str, float] = {}
    for u, c in after["stacks"].items():
        d = c - before["stacks"].get(u, 0)
        if d > 0:
            gained[u] = d
    for i, u in after["ids"].items():
        if i not in before["ids"]:
            gained[u] = gained.get(u, 0) + 1
    items = [{"uniqueName": u, "count": int(c) if float(c).is_integer() else c} for u, c in sorted(gained.items())]
    return {"items": items, "credits": int(max(0, after["credits"] - before["credits"])),
            "endo": int(max(0, after["endo"] - before["endo"]))}


def sync_time_ms(inv: Any) -> int | None:
    sync = inv.get("LastInventorySync") if isinstance(inv, dict) else None
    oid = sync.get("$oid") if isinstance(sync, dict) else None
    if isinstance(oid, str) and re.fullmatch(r"[0-9a-f]{24}", oid):
        return int(oid[:8], 16) * 1000
    return None


# ---- tracker (missionRewards.ts) ----------------------------------------------------------------

class MissionTracker:
    def __init__(self, runtime_dir: str, enabled: Callable[[], bool] = lambda: True,
                 clock: Callable[[], float] = time.time, line_source: Callable[[], str] = lambda: FILE,
                 on_record: Callable[[dict], None] | None = None, can_read: Callable[[], bool] = lambda: True):
        self.path = os.path.join(runtime_dir, HISTORY_FILE)
        self._enabled = enabled
        self._can_read = can_read      # False: no inventory source, so mission ends are not queued
        self._clock = clock
        self._line_source = line_source
        self._on_record = on_record or (lambda s: None)
        self._lock = threading.RLock()
        self._uptime = UptimeTracker()
        self._info: dict | None = None
        self._last_end: float | None = None
        self.pending = {"count": 0, "endedAt": 0.0, "info": None}
        self.baseline: dict | None = None      # {snap, sync (ms|None), taken}
        self._replaced: list[dict] = []
        self.history: list[dict] = []
        self.last_note: str | None = None
        self._load()

    # persistence
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                rows = json.load(f).get("summaries") or []
            self.history = [r for r in rows if isinstance(r, dict) and isinstance(r.get("endedAt"), (int, float))]
        except FileNotFoundError:
            self.history = []
        except (OSError, ValueError) as e:
            _log.warning("mission history unreadable: %s", e)
            self.history = []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"summaries": self.history[-MAX_HISTORY:]}, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError as e:
            _log.warning("could not save mission history: %s", e)

    # EE.log (tailer thread: bookkeeping only)
    def observe_file_line(self, line: str) -> None:
        self._uptime.observe(line, self._clock())

    def _wall_time(self, line: str) -> float:
        now = self._clock()
        if self._line_source() == REALTIME:
            return now
        return now - self._uptime.observe(line, now)

    def on_line(self, line: str) -> None:
        if not self._enabled():
            return
        if "SyncAutoPopulated" in line or "OnStateStarted" in line:
            m = SYNC_CONSUMABLES.search(line)
            info = {"missionType": m.group(1), "node": m.group(2)} if m else None
            if info is None:
                m = STATE_STARTED.search(line)
                info = {"missionType": m.group(1), "node": None} if m else None
            if info is None:
                return
            cur = self._info or {}
            keep = info["missionType"] == cur.get("missionType") and not HUB_NODE.search(cur.get("node") or "")
            if info["node"] or not keep:
                self._info = info
            return
        if ("EOM" in line or "Abort:" in line) and MISSION_END_LINE.search(line):
            self.on_mission_end(line)

    def on_mission_end(self, line: str = "", ended_at: float | None = None) -> None:
        with self._lock:
            ended = ended_at if ended_at is not None else self._wall_time(line)
            if HUB_ABORT.search(line):
                return
            if self._last_end is not None and abs(ended - self._last_end) < MISSION_END_JOIN_S:
                self._last_end = ended
                return
            info, self._info = self._info, None
            if info and info.get("node") and HUB_NODE.search(info["node"]):
                return
            self._last_end = ended
            if not self._can_read():
                _log.info("mission end ignored: no inventory source to read rewards from")
                return
            self._rewind(ended)
            self.pending = {"count": self.pending["count"] + 1, "endedAt": ended, "info": info}
            _log.info("mission end (%s); waiting for a fresh inventory", (info or {}).get("missionType") or "type unknown")

    # baselines
    def _rewind(self, ended: float) -> None:
        """Steps back over loads that arrived after the end yet were synced for it."""
        b = self.baseline
        while (b is not None and b["sync"] is not None and b["taken"] > ended
               and b["sync"] >= (ended - SYNC_TOLERANCE_S) * 1000):
            if not self._replaced:
                break
            b = self.baseline = self._replaced.pop()
        self._replaced = []

    def _adopt(self, data: dict, sync: int | None) -> None:
        if self.baseline and self.baseline["sync"] is not None and sync is not None and sync < self.baseline["sync"]:
            return
        snap = snapshot(data)
        if snap is None:
            return
        now = self._clock()
        if self.baseline:
            replaced = self._replaced + [self.baseline]
            self._replaced = [b for i, b in enumerate(replaced)
                              if (replaced[i + 1]["taken"] if i + 1 < len(replaced) else now) > now - LATE_END_S]
        self.baseline = {"snap": snap, "sync": sync, "taken": now}

    def reset(self, data: dict | None = None) -> None:
        with self._lock:
            self.pending = {"count": 0, "endedAt": 0.0, "info": None}
            self._replaced = []
            self._last_end = None
            self._info = None
            self.baseline = None
            if data is not None:
                snap = snapshot(data)
                self.baseline = {"snap": snap, "sync": sync_time_ms(data), "taken": self._clock()} if snap else None

    def on_inventory(self, data: dict) -> dict | None:
        """A new snapshot: records the pending missions when it is fresh for them, else
        refreshes the baseline. Returns the recorded summary."""
        if not self._enabled() or not isinstance(data, dict):
            return None
        with self._lock:
            sync = sync_time_ms(data)
            if self.pending["count"] > 0:
                if sync is None:
                    return None
                if self.baseline and self.baseline["sync"] is not None and sync <= self.baseline["sync"]:
                    return None
                if sync < (self.pending["endedAt"] - SYNC_TOLERANCE_S) * 1000:
                    return None
                return self._attribute(data, sync)
            if sync is None:
                if self.baseline is None:
                    self._adopt(data, None)
            else:
                self._adopt(data, sync)
            return None

    def _attribute(self, data: dict, sync: int) -> dict | None:
        after = snapshot(data)
        if after is None:
            return None
        before, batch = self.baseline, self.pending
        now = self._clock()
        self.baseline = {"snap": after, "sync": sync, "taken": now}
        self.pending = {"count": 0, "endedAt": 0.0, "info": None}
        if before is None:
            self.last_note = "no earlier inventory to compare; baseline taken"
            return None
        delta = diff(before["snap"], after)
        info = batch["info"] or {}
        summary = {"id": f"{int(batch['endedAt'] * 1000)}-{int(now * 1000)}", "endedAt": int(batch["endedAt"] * 1000),
                   "readAt": int(now * 1000), "missionCount": batch["count"], **delta}
        if info.get("missionType"):
            summary["missionType"] = info["missionType"]
        if info.get("node"):
            summary["node"] = info["node"]
        if before["sync"] is not None:
            summary["baselineAt"] = before["sync"]
        self.history.append(summary)
        self.history = self.history[-MAX_HISTORY:]
        self._save()
        _log.info("%d mission(s): %d item types, credits +%d, endo +%d", batch["count"], len(delta["items"]),
                  delta["credits"], delta["endo"])
        try:
            self._on_record(summary)
        except Exception:  # noqa: BLE001
            _log.exception("mission record listener failed")
        return summary

    def delete(self, sid: str) -> bool:
        with self._lock:
            n = len(self.history)
            self.history = [s for s in self.history if s.get("id") != sid]
            if len(self.history) != n:
                self._save()
                return True
            return False

    def clear(self) -> None:
        with self._lock:
            self.history = []
            self._save()

    def status(self) -> dict:
        return {"enabled": bool(self._enabled()), "phase": "waiting" if self.pending["count"] else "idle",
                "pendingMissions": self.pending["count"], "baseline": self.baseline is not None,
                "baselineAt": self.baseline["sync"] if self.baseline else None, "recorded": len(self.history),
                "note": self.last_note}


# ---- rows (missionRewardRows.ts) ------------------------------------------------------------------

class Valuer:
    """Name / plat each / ducats each for a uniqueName (item db + cached market prices)."""

    def __init__(self, item_of: Callable[[str], Any], price_of: Callable[[str], float | None],
                 fallback_name: Callable[[str], str] | None = None):
        self.item_of = item_of
        self.price_of = price_of
        self.fallback_name = fallback_name or _fallback_name

    def row(self, u: str, count: float) -> dict:
        it = self.item_of(u)
        name = it.name if it is not None and it.name else self.fallback_name(u)
        slug = it.wfm_slug if it is not None and it.tradable and it.wfm_slug else None
        each = self.price_of(slug) if slug else None
        ducats = it.ducats if it is not None and isinstance(it.ducats, int) and it.ducats > 0 else None
        return {"uniqueName": u, "name": name, "count": count, "slug": slug, "vaulted": bool(it and it.vaulted),
                "platinum": round(each * count, 1) if each is not None else None, "each": each,
                "ducats": ducats * count if ducats else None, "unpriced": bool(slug) and each is None,
                "tradable": bool(slug)}


def _fallback_name(u: str) -> str:
    last = u.rsplit("/", 1)[-1] or "Unknown"
    last = re.sub(r"([a-z])([A-Z])", r"\1 \2", last)
    return re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", last)


def reward_rows(items: list[dict], valuer: Valuer) -> tuple[list[dict], dict]:
    rows = [valuer.row(i["uniqueName"], i["count"]) for i in items]
    rows.sort(key=lambda r: (-(r["platinum"] or 0), -(r["ducats"] or 0), r["name"].lower()))
    totals = {"platinum": round(sum(r["platinum"] or 0 for r in rows), 1),
              "ducats": sum(r["ducats"] or 0 for r in rows), "unpriced": sum(1 for r in rows if r["unpriced"])}
    return rows, totals


def mission_label(s: dict) -> dict:
    from ..data import solnodes

    mt = s.get("missionType") or ""
    node = s.get("node") or ""
    try:
        type_label = solnodes.mission_type_label(mt, node) if mt else ""
        node_label = solnodes.node_label(node) if node else ""
    except Exception:  # noqa: BLE001
        type_label, node_label = mt, node
    if mt and not type_label:
        type_label = mt[3:].replace("_", " ").title() if mt.startswith("MT_") else mt
    return {"missionTypeLabel": type_label or None, "nodeLabel": node_label or None}


def summary_view(s: dict, valuer: Valuer, with_rows: bool = True) -> dict:
    rows, totals = reward_rows(s.get("items") or [], valuer)
    out = {k: s.get(k) for k in ("id", "endedAt", "readAt", "missionCount", "missionType", "node", "credits", "endo",
                                 "baselineAt")}
    out.update(mission_label(s))
    out.update(totals)
    out["itemCount"] = len(rows)
    if with_rows:
        out["rows"] = rows
    return out


def period_start(period: str, now_ms: int, since_ms: int | None = None) -> int | None:
    if period == "all":
        return None
    if period == "today":
        if since_ms:
            return int(since_ms)
        t = time.localtime(now_ms / 1000)
        return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)
    return now_ms - (7 if period == "7d" else 30) * DAY_MS


def page(history: list[dict], valuer: Valuer, period: str = "7d", now_ms: int | None = None,
         since_ms: int | None = None, mission_type: str | None = None, offset: int = 0, limit: int = 50) -> dict:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    period = period if period in PERIODS else "7d"
    start = period_start(period, now_ms, since_ms)
    newest = sorted(history, key=lambda s: s.get("endedAt") or 0, reverse=True)
    types = sorted({s["missionType"] for s in newest if s.get("missionType")})
    matched = [s for s in newest if (start is None or s["endedAt"] >= start)
               and (not mission_type or s.get("missionType") == mission_type)]
    offset = max(0, int(offset or 0))
    limit = max(1, min(200, int(limit or 50)))
    gained: dict[str, float] = {}
    credits = endo = missions = 0
    for s in matched:
        missions += int(s.get("missionCount") or 1)
        credits += int(s.get("credits") or 0)
        endo += int(s.get("endo") or 0)
        for i in s.get("items") or []:
            gained[i["uniqueName"]] = gained.get(i["uniqueName"], 0) + i["count"]
    total_rows, total_vals = reward_rows([{"uniqueName": u, "count": c} for u, c in gained.items()], valuer)
    today_start = period_start("today", now_ms, since_ms if period == "today" else None)
    today = sum(int(s.get("missionCount") or 1) for s in newest if s["endedAt"] >= today_start)
    return {"period": period, "since": start, "matched": len(matched), "offset": offset, "limit": limit,
            "summaries": [summary_view(s, valuer) for s in matched[offset: offset + limit]],
            "totals": {"missions": missions, "credits": credits, "endo": endo, **total_vals,
                       "items": total_rows[:30], "itemTypes": len(total_rows)},
            "missionTypes": [{"id": t, "label": mission_label({"missionType": t})["missionTypeLabel"] or t}
                             for t in types],
            "recorded": len(history), "today": today,
            "latest": summary_view(newest[0], valuer) if newest else None}


def unpriced_slugs(history: list[dict], valuer: Valuer, limit: int = 60) -> list[str]:
    out: list[str] = []
    for s in sorted(history, key=lambda s: s.get("endedAt") or 0, reverse=True)[:20]:
        for i in s.get("items") or []:
            r = valuer.row(i["uniqueName"], 1)
            if r["unpriced"] and r["slug"] not in out:
                out.append(r["slug"])
                if len(out) >= limit:
                    return out
    return out


# ---- wiring -----------------------------------------------------------------------------------------

_tracker: MissionTracker | None = None


def tracker() -> MissionTracker | None:
    return _tracker


def default_valuer() -> Valuer:
    try:
        from ..data import items

        db = items.items()
        item_of = db.by_unique
    except Exception:  # noqa: BLE001
        item_of = lambda _u: None  # noqa: E731
    from .mastery import _default_price

    def price_of(slug: str) -> float | None:
        try:
            from ..data import market

            m = market.get()
            for rank in (0, None):
                hit, p = m.cached(slug, rank, max_age=float("inf"))
                if hit and p is not None and (p.median or p.low_sell):
                    return p.median if p.median else float(p.low_sell)
        except Exception:  # noqa: BLE001
            return _default_price(slug)
        return None
    return Valuer(item_of, price_of)


def _prefetch(slugs: list[str]) -> None:
    if not slugs:
        return
    try:
        from ..data import market

        # notify: each landed price emits market.price; Missions.tsx reloads at most every 2 s.
        market.get().prefetch(slugs, notify=True)
    except Exception:  # noqa: BLE001
        _log.exception("mission reward price prefetch failed")


def setup(app) -> None:
    global _tracker
    from .. import inventory

    def enabled() -> bool:
        return bool(app.settings.get("missions.enabled", True))

    def recorded(summary: dict) -> None:
        app.emit("missions.updated", {"id": summary["id"]})
        app.submit(_after_record, app, summary)

    def can_read() -> bool:
        try:
            return inventory.status().get("source") != "none"
        except Exception:  # noqa: BLE001
            return True

    _tracker = MissionTracker(app.runtime_dir, enabled, line_source=lambda: app.eelog.source, on_record=recorded,
                              can_read=can_read)
    inv = inventory.inventory()
    if inv is not None:
        _tracker.reset(inv.data)
    last_enabled = [enabled()]

    def on_settings(d: dict) -> None:
        # WFHelper setTrackingEnabled: any switch (this page, the Settings page, an import)
        # drops the pending batch and re-baselines, so gains made while off are not rewards.
        now = bool(((d or {}).get("missions") or {}).get("enabled", True))
        if now == last_enabled[0]:
            return
        last_enabled[0] = now
        snap = inventory.inventory() if now else None
        _tracker.reset(snap.data if snap is not None else None)
        app.emit("missions.updated", None)

    app.settings.on_change(on_settings)
    inventory.on_update(lambda snap: _tracker.on_inventory(snap.data))
    app.eelog.subscribe(_tracker.observe_file_line, source=FILE)
    app.eelog.subscribe(_tracker.on_line)


def _after_record(app, summary: dict) -> None:
    val = default_valuer()
    slugs = [r["slug"] for r in reward_rows(summary.get("items") or [], val)[0] if r["unpriced"]]
    _prefetch(slugs)
    if app.settings.get("missions.toast", False):
        v = summary_view(summary, val, with_rows=False)
        parts = [f"+{v['platinum']:g}p" if v["platinum"] else "", f"+{v['ducats']}d" if v["ducats"] else "",
                 f"+{v['credits']:,} cr" if v["credits"] else "", f"+{v['endo']:,} endo" if v["endo"] else ""]
        app.toast("Mission rewards", " ".join(p for p in parts if p) or f"{v['itemCount']} items")


def _app():
    from ..app import get_app

    return get_app()


def _status() -> dict:
    t = _tracker
    st = t.status() if t else {"enabled": False, "phase": "idle", "pendingMissions": 0, "recorded": 0}
    try:
        from .. import inventory

        inv = inventory.status()
        st["inventorySource"] = inv.get("source")
        st["missionScan"] = bool(_app().settings.get("inventory_mission_scan", True))
        if st["enabled"] and inv.get("source") == "none":
            st["blocked"] = "source-none"
        elif st["enabled"] and inv.get("source") != "memory":
            st["blocked"] = "source-not-memory"
        elif st["enabled"] and not st["missionScan"]:
            st["blocked"] = "mission-scan-off"
        if not st["enabled"]:
            st["blocked"] = "tracking-off"
        if st.get("pendingMissions") and not inv.get("mission_read_pending") and inv.get("last_reason"):
            st["lastFailure"] = inv.get("last_reason")
    except Exception:  # noqa: BLE001
        pass
    return st


def _page(period: str, since_ms: int | None, mission_type: str | None, offset: int, limit: int) -> dict:
    val = default_valuer()
    hist = list(_tracker.history) if _tracker else []
    out = page(hist, val, period, None, since_ms, mission_type, offset, limit)
    out["status"] = _status()
    _prefetch(unpriced_slugs(hist, val))
    return out


@rpc.method("missions.page")
async def _rpc_page(period: str = "7d", since_ms: int | None = None, mission_type: str | None = None,
                    offset: int = 0, limit: int = 50) -> dict:
    return await _app().run_blocking(_page, period, since_ms, mission_type, offset, limit)


def _last() -> dict:
    val = default_valuer()
    hist = list(_tracker.history) if _tracker else []
    latest = max(hist, key=lambda s: s.get("endedAt") or 0) if hist else None
    return {"latest": summary_view(latest, val) if latest else None, "status": _status()}


@rpc.method("missions.last")
async def _rpc_last() -> dict:
    return await _app().run_blocking(_last)


def _qam(since_ms: int | None, rows: int) -> dict:
    val = default_valuer()
    hist = list(_tracker.history) if _tracker else []
    pg = page(hist, val, "today", None, since_ms, None, 0, 1)
    latest = pg["latest"]
    if latest:
        latest["rows"] = latest["rows"][:max(1, min(10, rows))]
    return {"latest": latest, "today": {"missions": pg["totals"]["missions"], "platinum": pg["totals"]["platinum"],
                                        "ducats": pg["totals"]["ducats"]},
            "status": _status()}


@rpc.method("missions.qam")
async def _rpc_qam(since_ms: int | None = None, rows: int = 4) -> dict:
    return await _app().run_blocking(_qam, since_ms, rows)


@rpc.method("missions.status")
async def _rpc_status() -> dict:
    return await _app().run_blocking(_status)


@rpc.method("missions.set_enabled")
async def _rpc_set_enabled(enabled: bool) -> dict:
    app = _app()
    app.settings.update({"missions": {"enabled": bool(enabled)}})   # setup's listener resets the tracker
    return await app.run_blocking(_status)


@rpc.method("missions.set_toast")
async def _rpc_set_toast(enabled: bool) -> dict:
    _app().settings.update({"missions": {"toast": bool(enabled)}})
    return {"toast": bool(enabled)}


@rpc.method("missions.delete")
async def _rpc_delete(id: str) -> dict:  # noqa: A002 - RPC param name
    ok = _tracker.delete(id) if _tracker else False
    return {"deleted": ok}


@rpc.method("missions.clear")
async def _rpc_clear() -> dict:
    if _tracker:
        _tracker.clear()
    return {"recorded": 0}
