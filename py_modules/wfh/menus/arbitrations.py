"""Arbitrations page + QAM section RPCs (run history, analysis, summary overlay).

Run data comes from features/arbitration.py (tracker in features/arbi_tracker.py);
the schedule is data/arbi_schedule.py, whose ``world.arbi_*`` RPCs the page calls
directly. Mapping from WFHelper IPC (docs/port/arbitration.md §6):

  arbi:get-runs     -> arbi.runs            {runs[slim], diskUsageBytes, status}
  arbi:refresh-runs -> arbi.refresh         waits <= 5 s for pending saves
  (detail)          -> arbi.run(id)         {run (no heavy arrays), analysis}
  arbi:set-vitus    -> arbi.set_vitus(id, vitus)    record or None
  arbi:set-tags     -> arbi.set_tags(id, tags)
  arbi:set-notes    -> arbi.set_notes(id, notes)
  arbi:delete-run   -> arbi.delete_run(id)  {ok}
  arbi:delete-log   -> arbi.delete_log(id)
  arbi:export-log   -> arbi.export_log(id)  {path} in the player's ~/Downloads
  arbi:import-log   -> arbi.import_log(path?)  {imported, skipped}; no path = current EE.log
  (QAM)             -> arbi.qam, arbi.status, arbi.summary_show(id?), arbi.summary_dismiss
Events: arbi.run_saved (slim record), arbi.run_skipped, arbi.summary (payload).
"""

from __future__ import annotations

import os
import re
import tempfile

from .. import log, rpc, session
from ..app import App, get_app
from ..features import arbi_analysis as an
from ..features.arbi_tracker import clean_vitus, slim

_log = log.get("menus.arbitrations")

MAX_LIST = 2000


def setup(app: App) -> None:  # noqa: ARG001 - RPCs register at import
    pass


def _ctl():
    ctl = get_app().services.get("arbitration")
    if ctl is None:
        raise RuntimeError("arbitration tracker is not loaded")
    return ctl


def _run_id(v) -> str | None:
    return v if isinstance(v, str) and 1 <= len(v) <= 64 else None


def _runs_payload() -> dict:
    ctl = _ctl()
    t = ctl.tracker
    with t._lock:  # noqa: SLF001 - consistent snapshot while the tailer may prepend
        runs = [slim(r) for r in t.runs[:MAX_LIST]]
    return {"runs": runs, "total": len(t.runs), "diskUsageBytes": t.disk_usage_bytes(), "status": ctl.status()}


@rpc.method("arbi.runs")
async def _rpc_runs() -> dict:
    return await get_app().run_blocking(_runs_payload)


@rpc.method("arbi.refresh")
async def _rpc_refresh() -> dict:
    def run() -> dict:
        _ctl().tracker.await_pending(5.0)
        return _runs_payload()
    return await get_app().run_blocking(run)


@rpc.method("arbi.status")
async def _rpc_status() -> dict:
    # status() takes the tracker lock, which init()'s crash salvage and record inserts hold for a while.
    return await get_app().run_blocking(_ctl().status)


@rpc.method("arbi.run")
async def _rpc_run(id: str) -> dict | None:  # noqa: A002 - RPC param name
    rid = _run_id(id)
    if rid is None:
        return None

    def run() -> dict | None:
        t = _ctl().tracker
        with t._lock:  # noqa: SLF001
            rec = t.get(rid)
            if rec is None:
                return None
            ids = [r["id"] for r in t.runs]
            analysis = an.analyze(rec, list(t.runs))
            out = an.public_run(rec)
        i = ids.index(rid)
        return {"run": out, "analysis": analysis, "prevId": ids[i + 1] if i + 1 < len(ids) else None,
                "nextId": ids[i - 1] if i > 0 else None, "hasLog": t.run_log_path(rid) is not None}
    return await get_app().run_blocking(run)


def _mutate(fn, rid) -> dict | None:
    rid = _run_id(rid)
    if rid is None:
        return None
    rec = fn(rid)
    return slim(rec) if rec else None


@rpc.method("arbi.set_vitus")
async def _rpc_set_vitus(id: str, vitus=None) -> dict | None:  # noqa: A002
    v = clean_vitus(vitus)
    if v is False:
        return None
    return await get_app().run_blocking(_mutate, lambda rid: _ctl().tracker.set_vitus(rid, v), id)


@rpc.method("arbi.set_tags")
async def _rpc_set_tags(id: str, tags: list | None = None) -> dict | None:  # noqa: A002
    return await get_app().run_blocking(_mutate, lambda rid: _ctl().tracker.set_tags(rid, tags or []), id)


@rpc.method("arbi.set_notes")
async def _rpc_set_notes(id: str, notes: str = "") -> dict | None:  # noqa: A002
    return await get_app().run_blocking(_mutate, lambda rid: _ctl().tracker.set_notes(rid, notes), id)


@rpc.method("arbi.delete_run")
async def _rpc_delete_run(id: str) -> dict:  # noqa: A002
    rid = _run_id(id)
    if rid is None:
        return {"ok": False}
    return {"ok": await get_app().run_blocking(_ctl().tracker.delete_run, rid)}


@rpc.method("arbi.delete_log")
async def _rpc_delete_log(id: str) -> dict | None:  # noqa: A002
    return await get_app().run_blocking(_mutate, lambda rid: _ctl().tracker.delete_log(rid), id)


def export_name(node: str, run_id: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9\-_ ]", "", node or "").replace(" ", "_")
    return f"{safe}_{run_id}.log"


def export_log(run_id: str, dest_dir: str | None = None) -> dict:
    t = _ctl().tracker
    rec = t.get(run_id)
    text = t.read_log(run_id) if rec else None
    if text is None:
        return {"ok": False, "error": "No stored log for this run."}
    user = session.deck_user()
    as_root = os.geteuid() == 0
    dest_dir = dest_dir or os.path.join(user.pw_dir, "Downloads")
    if not os.path.isdir(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        if as_root:   # a root-owned ~/Downloads would break the user's own downloads
            try:
                os.chown(dest_dir, user.pw_uid, user.pw_gid)
            except OSError:
                pass
    path = os.path.join(dest_dir, export_name(rec.get("node", ""), run_id))
    # Root writing into a user-writable dir: write a private temp file, chown it, then rename over the
    # target. os.replace swaps the directory entry, so a planted symlink is replaced, never followed.
    fd, tmp = tempfile.mkstemp(prefix=".wfh-export-", suffix=".tmp", dir=dest_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        if as_root:
            try:
                os.chown(tmp, user.pw_uid, user.pw_gid)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"ok": True, "path": path}


@rpc.method("arbi.export_log")
async def _rpc_export_log(id: str) -> dict:  # noqa: A002
    rid = _run_id(id)
    if rid is None:
        return {"ok": False, "error": "Bad run id."}
    return await get_app().run_blocking(export_log, rid)


@rpc.method("arbi.import_log")
async def _rpc_import_log(path: str | None = None) -> dict:
    """Split an EE.log into runs. No path: the current EE.log (whole file, not just the tail)."""
    app = get_app()
    if not path:
        from ..eelog import find_ee_log
        path = app.eelog.path or find_ee_log(app.settings.get("ee_log_path", ""))
    if not path or not os.path.isabs(path) or not os.path.isfile(path):
        return {"imported": [], "skipped": 0, "error": "EE.log not found."}
    res = await app.run_blocking(_ctl().tracker.import_ee_log, path)
    return {**res, "path": path}


@rpc.method("arbi.summary_show")
async def _rpc_summary_show(id: str | None = None) -> dict | None:  # noqa: A002
    ctl = _ctl()
    if id:
        rid = _run_id(id)
        return await get_app().run_blocking(ctl.show_run, rid) if rid else None
    if ctl.last_summary is None:
        return None
    await get_app().run_blocking(ctl.show, ctl.last_summary)
    return ctl.last_summary


@rpc.method("arbi.summary_dismiss")
def _rpc_summary_dismiss() -> None:
    _ctl().dismiss()


@rpc.method("arbi.qam")
async def _rpc_qam() -> dict:
    """Compact QAM view: current/next arbitration, live capture, last run, last skip."""
    def run() -> dict:
        from ..data import arbi_schedule
        upcoming: list[dict] = []
        try:
            store = arbi_schedule.get()
            sched = store.payload(1)
            # Favourites can be days away: search the whole fetched schedule, not just the 24 h window.
            upcoming = [e.to_json() for e in store.upcoming(30)]
        except Exception as e:  # noqa: BLE001
            _log.warning("schedule unavailable: %s", e)
            sched = {"current": None, "next": None, "alerts": {"favoriteNodes": []}, "error": str(e)}
        ctl = _ctl()
        t = ctl.tracker
        with t._lock:  # noqa: SLF001
            last = next((r for r in t.runs if not r.get("duplicateOf")), None)
        favs = set((sched.get("alerts") or {}).get("favoriteNodes") or [])
        nxt_fav = next((e for e in upcoming if e["nodeId"] in favs
                        and e["epochMs"] > sched.get("now", 0)), None)
        return {"current": sched.get("current"), "next": sched.get("next"), "nextFavorite": nxt_fav,
                "favorites": sorted(favs), "scheduleError": sched.get("error"),
                "lastRun": slim(last) if last else None, "status": ctl.status(), "lastSummary": ctl.last_summary}
    return await get_app().run_blocking(run)
