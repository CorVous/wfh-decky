"""Dashboard view-model (docs/port/menus.md §2): one-glance summary for the QAM and the page.

Widgets (WFHelper widget ids in brackets): cycles [widget.cycles], fissures [widget.fissures],
Baro [widget.baro], foundry ready [widget.foundryReady] from menus/foundry.qam_view (claimable,
building and ready-to-build incl. chain-buildable; raw PendingRecipes only as a fallback),
last mission [widget.lastMission] from menus/missions' tracker, last relic reward (Deck extra:
the relic-reward screen scan), last arbitration run [widget.recentRuns, newest by startedAt],
next arbitration, resets and dailies progress.
Every part degrades to None when its source is missing (no inventory, no EE.log yet).
The page reloads on events inventory.updated, missions.updated, rewards.result, arbi.run_saved.

RPCs
  dashboard.summary()                               the whole model
  dashboard.configure(fissure_limit?, foundry_limit?, hidden?)   settings dashboard.*
"""

from __future__ import annotations

from typing import Any

from .. import log, rpc
from ..app import App, get_app
from ..data import worldstate as ws
from . import world as wv

_log = log.get("menus.dashboard")

WIDGETS = ("cycles", "fissures", "baro", "foundry", "lastMission", "lastReward", "lastArbi", "arbitration",
           "resets")
LIMIT_MIN, LIMIT_MAX, LIMIT_DEFAULT = 1, 20, 5


def prefs(settings) -> dict:
    hidden = settings.get("dashboard.hidden", []) or []
    return {"fissureLimit": wv._clamp_int(settings.get("dashboard.fissure_limit", LIMIT_DEFAULT), LIMIT_MIN,
                                          LIMIT_MAX, LIMIT_DEFAULT),
            "foundryLimit": wv._clamp_int(settings.get("dashboard.foundry_limit", LIMIT_DEFAULT), LIMIT_MIN,
                                          LIMIT_MAX, LIMIT_DEFAULT),
            "hidden": [w for w in WIDGETS if isinstance(hidden, list) and w in hidden]}


# ---- foundry -------------------------------------------------------------------------------

def _product_name(item_type: str) -> str:
    """What a pending recipe builds: "Forma Blueprint" -> "Forma", "Mesa Prime Helmet Blueprint" -> "... Helmet"."""
    name = ws.resolve_item_name(item_type) if item_type else "Unknown"
    return name[: -len(" Blueprint")] if name.endswith(" Blueprint") else name


def foundry_from_qam(q: dict | None, limit: int = LIMIT_DEFAULT) -> dict | None:
    """menus/foundry.qam_view -> the widget model (foundryReadyRows: claimable first, then building,
    then ready to build); None without an inventory."""
    if not isinstance(q, dict) or not q.get("hasInventory"):
        return None
    c = q.get("counts") or {}
    rows = [{"name": r.get("name") or "Unknown", "status": r.get("status"), "endMs": r.get("endMs"),
             "count": r.get("count"), "chainOnly": bool(r.get("chainOnly"))}
            for r in (q.get("rows") or []) if r.get("status") in ("claimable", "in-progress", "ready-to-build")]
    return {"claimable": int(c.get("claimable") or 0), "building": int(c.get("building") or 0),
            "ready": int(c.get("ready") or 0), "rows": rows[:limit], "nextReadyMs": q.get("nextReadyMs"),
            "source": "foundry"}


def foundry_summary(inv: dict | None, now: int, limit: int = LIMIT_DEFAULT) -> dict | None:
    """Fallback when the foundry snapshot is unavailable: claimable and running builds from the raw
    PendingRecipes (ready-to-build is unknown here, so ``ready`` is None). Same shape as foundry_from_qam."""
    if not isinstance(inv, dict):
        return None
    claim, building = [], []
    for e in ws.as_list(inv.get("PendingRecipes")):
        if not isinstance(e, dict) or not isinstance(e.get("ItemType"), str):
            continue
        done = ws.de_ms(e.get("CompletionDate"))
        row = {"name": _product_name(e["ItemType"]), "status": "claimable" if done and done <= now else "in-progress",
               "endMs": done or None, "count": None, "chainOnly": False}
        (claim if row["status"] == "claimable" else building).append(row)
    claim.sort(key=lambda r: r["endMs"] or 0)
    building.sort(key=lambda r: r["endMs"] or 0)
    return {"claimable": len(claim), "building": len(building), "ready": None, "rows": (claim + building)[:limit],
            "nextReadyMs": building[0]["endMs"] if building else None, "source": "pending"}


def _foundry(app: Any, inv: dict | None, now: int, limit: int) -> dict | None:
    try:
        from . import foundry as fm
        from . import inventory as inv_menu

        snap = inv_menu.current_snapshot()
        if snap is not None:
            return foundry_from_qam(fm.qam_view(snap, now, fm.pinned_list(app.settings), limit), limit)
    except Exception as e:  # noqa: BLE001 - item data missing / foundry module not loaded
        _log.warning("foundry snapshot unavailable, using PendingRecipes: %s", e)
    return foundry_summary(inv, now, limit)


# ---- last mission ---------------------------------------------------------------------------

def mission_summary(v: dict | None, rows: int = 4) -> dict | None:
    """missions.summary_view(...) -> the widget model (node, time, +plat/+ducats/+credits, top rows)."""
    if not isinstance(v, dict):
        return None
    out = {k: v.get(k) for k in ("id", "endedAt", "nodeLabel", "missionTypeLabel", "node", "missionCount",
                                 "platinum", "ducats", "credits", "endo", "itemCount", "unpriced")}
    out["rows"] = [{"name": r.get("name"), "count": r.get("count"), "platinum": r.get("platinum"),
                    "ducats": r.get("ducats")} for r in (v.get("rows") or [])[:rows]]
    return out


def _last_mission() -> dict | None:
    try:
        from . import missions

        t = missions.tracker()
        hist = list(t.history) if t is not None else []
        if not hist:
            return None
        latest = max(hist, key=lambda s: s.get("endedAt") or 0)
        return mission_summary(missions.summary_view(latest, missions.default_valuer()))
    except Exception as e:  # noqa: BLE001 - missions module not loaded
        _log.debug("last mission unavailable: %s", e)
        return None


# ---- last reward / last arbitration ---------------------------------------------------------

def reward_summary(result: dict | None) -> dict | None:
    if not isinstance(result, dict) or not result.get("items"):
        return None
    items = result["items"]
    bp = result.get("best_plat")
    best = next((i for i in items if i.get("slot") == bp), None) if bp is not None else None
    return {"at": result.get("at"), "best": (best or {}).get("name"),
            "bestPlat": (best or {}).get("plat"),
            "items": [{"name": i.get("name"), "plat": i.get("plat"), "ducats": i.get("ducats"),
                       "owned": i.get("owned")} for i in items]}


def _last_reward() -> dict | None:
    try:
        from ..features import relic_rewards

        return reward_summary(relic_rewards.controller().last)
    except Exception:  # noqa: BLE001 - feature not loaded
        return None


def arbi_run_summary(run: dict | None) -> dict | None:
    if not isinstance(run, dict):
        return None
    return {k: run.get(k) for k in ("id", "startedAt", "endedAt", "node", "missionName", "missionType",
                                     "durationSec", "rotations", "drones", "vitusActual",
                                     "expectedVitusMean")}


def _last_arbi() -> tuple[dict | None, int]:
    try:
        from ..features import arbitration
        from ..features.arbi_tracker import slim

        ctl = arbitration.controller()
        if ctl is None:
            return None, 0
        t = ctl.tracker
        with t._lock:  # noqa: SLF001 - same access as menus/arbitrations.arbi.qam
            runs = [r for r in t.runs if not r.get("duplicateOf")]
            # newest by startedAt (widget.recentRuns): imported / reloaded runs are not sorted
            last = slim(max(runs, key=lambda r: r.get("startedAt") or 0)) if runs else None
        return arbi_run_summary(last), len(runs)
    except Exception:  # noqa: BLE001
        return None, 0


# ---- the whole model -------------------------------------------------------------------------

def build_summary(state: dict, now: int, ui: dict, p: dict, inv: dict | None, inv_time: int | None,
                  sched: dict | None, reward: dict | None, last_arbi: dict | None, arbi_count: int,
                  dailies: dict | None, foundry: dict | None = None, last_mission: dict | None = None) -> dict:
    rows = wv.fissure_rows(state.get("fissures"), now)
    t = {x["key"]: x for x in wv.timers(state, now)}
    baro = wv.baro_view(state, now)
    sortie_done = archon_done = None
    if dailies:
        by = {r["id"]: r for r in dailies["tasks"]}
        sortie_done, archon_done = by["sortie"]["done"], by["archonHunt"]["done"]
    return {
        "now": now, "stale": bool(state.get("stale")), "error": state.get("error"),
        "prefs": p, "worldPrefs": ui,
        "cycles": wv.cycle_rows(state, now),
        "resets": [t[k] for k in ("daily", "weekly", "sortie", "archon", "steelPath") if k in t],
        "fissures": wv.filter_fissures(rows, ui["fissureMode"], ui["fissureTiers"], p["fissureLimit"]),
        "fissureCounts": wv.fissure_counts(rows),
        "baro": {k: v for k, v in baro.items() if k != "inventory"} if baro else None,
        "baroItems": [i.get("item") for i in (baro or {}).get("inventory", [])[:4]],
        "foundry": foundry if foundry is not None else foundry_summary(inv, now, p["foundryLimit"]),
        "inventory": {"available": inv is not None, "syncMs": inv_time},
        "lastMission": last_mission,
        "lastReward": reward,
        "lastArbi": last_arbi, "arbiRunCount": arbi_count,
        "arbitration": wv.next_arbitration(sched),
        "dailies": {"done": dailies["doneCount"], "total": dailies["visibleCount"], "sortieDone": sortie_done,
                    "archonDone": archon_done} if dailies else None,
        "sortie": {"boss": (state.get("sortie") or {}).get("boss")} if state.get("sortie") else None,
        "archon": {"boss": (state.get("archonHunt") or {}).get("boss")} if state.get("archonHunt") else None,
        "steelPath": ((state.get("steelPath") or {}).get("currentReward")),
    }


def summary(app: Any) -> dict:
    now = wv._now()
    state = ws.state(ws.RPC_MAX_AGE)
    inv, inv_time = wv._inventory()
    try:
        dailies = wv.dailies_view(wv.store(), state, inv, inv_time, now)
    except Exception as e:  # noqa: BLE001
        _log.warning("dailies unavailable: %s", e)
        dailies = None
    last_arbi, n = _last_arbi()
    p = prefs(app.settings)
    return build_summary(state, now, wv.ui_prefs(app.settings), p, inv, inv_time,
                         wv.arbi_payload(1), _last_reward(), last_arbi, n, dailies,
                         foundry=_foundry(app, inv, now, p["foundryLimit"]) if inv is not None else None,
                         last_mission=_last_mission())


def setup(app: App) -> None:  # noqa: ARG001 - RPCs register at import
    pass


@rpc.method("dashboard.summary")
async def _rpc_summary() -> dict:
    app = get_app()
    return await app.run_blocking(summary, app)


@rpc.method("dashboard.configure")
def _rpc_configure(fissure_limit: int | None = None, foundry_limit: int | None = None,
                   hidden: list | None = None) -> dict:
    app = get_app()
    patch: dict = {}
    if fissure_limit is not None:
        patch["fissure_limit"] = wv._clamp_int(fissure_limit, LIMIT_MIN, LIMIT_MAX, LIMIT_DEFAULT)
    if foundry_limit is not None:
        patch["foundry_limit"] = wv._clamp_int(foundry_limit, LIMIT_MIN, LIMIT_MAX, LIMIT_DEFAULT)
    if isinstance(hidden, list):
        patch["hidden"] = [w for w in WIDGETS if w in hidden]
    if patch:
        app.settings.update({"dashboard": patch})
    return prefs(app.settings)
