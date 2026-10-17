"""World page view-models (docs/port/menus.md §10) on top of ``world.state``.

``world.state`` (data/worldstate.py) is WFHelper's WorldState with ISO dates. The
pages need the pure WorldView helpers from ``src/lib/world/useWorldView.ts``
(fissure rows/modes/tier order, cycle rows with urgency, the timer strip) plus a
dailies tracker, so those live here and every date the frontend counts down to is
also given as epoch ms (``...Ms`` keys) so the countdown is plain arithmetic.

RPCs
  world.view(force)              full World page model (state + rows + counts + timers)
  world.qam()                    compact QAM model (resets, cycles, fissure counts + top rows in the
                                 saved mode/tiers, limited to world_ui.qam_limit; next arbitration,
                                 Baro, Circuit)
  world.ui_set(fissure_mode?, fissure_tiers?, qam_limit?)   persist the fissure filters and the QAM
                                 fissure row count (settings world_ui.*)
  world.dailies()                dailies tracker rows (manual + inventory auto-detected)
  world.dailies_set(id, count)   manual progress for the current period
  world.dailies_hide(id, hidden) hide/show a task
Settings: ``world_ui.fissure_mode`` (all|normal|steel|railjack), ``world_ui.fissure_tiers``,
``world_ui.qam_limit``.

Deviations from WFHelper's buildCycleRows: Zariman is an extra cycle row (WFHelper has no Zariman
row), and Duviri is never urgent (WFHelper calls isUrgent(duviri.expiry, null) with no window), so
its row carries ``activationMs: None``.
Not ported yet (follow-ups): world-state notifications (§10.5 cycle / Baro / Varzia / fissure
alerts), bounties, Nightwave acts in the dailies tracker, arbitration schedule node search / presets.
"""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timezone
from typing import Any

from .. import log, rpc
from ..app import App, get_app
from ..data import worldstate as ws

_log = log.get("menus.world")

TIERS = ("Lith", "Meso", "Neo", "Axi", "Requiem", "Omnia")
TIER_ORDER = {t.lower(): i for i, t in enumerate(TIERS)}
FISSURE_MODES = ("all", "normal", "steel", "railjack")
FISSURE_EXPIRY_GUARD_MS = 1_500
URGENCY_RATIO = 0.2
DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
QAM_LIMIT_DEFAULT = 5


# ---- small helpers ------------------------------------------------------------------------

def ms(value: Any) -> int | None:
    return ws.parse_iso(value)


def _now() -> int:
    return ws.now_ms()


def _title(s: Any) -> str:
    s = str(s or "")
    return s[:1].upper() + s[1:]


def urgent(activation_ms: int | None, expiry_ms: int | None, now: int, fallback_total: int | None = None) -> bool:
    """remaining / total < URGENCY_RATIO (useWorldView.ts buildCycleRows)."""
    if not expiry_ms:
        return False
    total = (expiry_ms - activation_ms) if activation_ms and expiry_ms > activation_ms else fallback_total
    if not total or total <= 0:
        return False
    return (expiry_ms - now) / total < URGENCY_RATIO


# ---- fissures (buildFissureRows) ------------------------------------------------------------

def fissure_source_mode(f: dict) -> str:
    if f.get("isStorm"):
        return "railjack"
    return "steel" if f.get("isHard") else "normal"


def fissure_rows(fissures: list | None, now: int) -> list[dict]:
    """Every live fissure (normal, Steel Path, Void Storm), sorted tier then expiry asc.

    Drops expired ones and those expiring within FISSURE_EXPIRY_GUARD_MS. Mode filtering
    is ``filter_fissures`` so the page can switch modes without a round trip.
    """
    out = []
    for f in fissures or []:
        if not isinstance(f, dict) or f.get("expired"):
            continue
        exp = ms(f.get("expiry"))
        if exp is None or exp - now <= FISSURE_EXPIRY_GUARD_MS:
            continue
        tier = str(f.get("tier") or "")
        out.append({
            "id": f.get("id") or f"{tier}|{f.get('nodeId')}|{exp}",
            "tier": tier, "tierCls": tier.lower(),
            "missionType": f.get("missionType") or "Unknown",
            "node": f.get("node") or f.get("nodeId") or "Unknown",
            "nodeId": f.get("nodeId") or "", "planet": f.get("planet") or "",
            "isHard": bool(f.get("isHard")), "isStorm": bool(f.get("isStorm")),
            "sourceMode": fissure_source_mode(f),
            "expiryMs": exp, "activationMs": ms(f.get("activation")),
        })
    out.sort(key=lambda r: (TIER_ORDER.get(r["tierCls"], 99), r["expiryMs"]))
    return out


def mode_matches(row: dict, mode: str) -> bool:
    if mode == "railjack":
        return row["isStorm"]
    if row["isStorm"]:
        return False
    if mode == "steel":
        return row["isHard"]
    if mode == "normal":
        return not row["isHard"]
    return True  # all = normal + steel


def filter_fissures(rows: list[dict], mode: str = "normal", tiers: list[str] | None = None,
                    limit: int | None = None) -> list[dict]:
    allowed = {t.lower() for t in tiers} if tiers is not None else None
    out = [r for r in rows if mode_matches(r, mode) and (allowed is None or r["tierCls"] in allowed)]
    return out[:limit] if limit else out


def fissure_counts(rows: list[dict]) -> dict:
    """{mode: {"total": n, "Lith": n, ...}} for all four modes (tiers with 0 included)."""
    out = {}
    for mode in FISSURE_MODES:
        c = {t: 0 for t in TIERS}
        total = 0
        for r in rows:
            if mode_matches(r, mode):
                c[r["tier"]] = c.get(r["tier"], 0) + 1
                total += 1
        c["total"] = total
        out[mode] = c
    return out


def group_by_tier(rows: list[dict]) -> list[dict]:
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["tier"], []).append(r)
    return [{"tier": t, "rows": groups[t]} for t in sorted(groups, key=lambda t: TIER_ORDER.get(t.lower(), 99))]


# ---- cycles (buildCycleRows) ---------------------------------------------------------------

_CYCLES = (
    # key, state key, label, (state -> next state)
    ("earth", "earthCycle", "Earth", {"day": "night", "night": "day"}),
    ("cetus", "cetusCycle", "Cetus", {"day": "night", "night": "day"}),
    ("vallis", "vallisCycle", "Orb Vallis", {"warm": "cold", "cold": "warm"}),
    ("cambion", "cambionCycle", "Cambion Drift", {"fass": "vome", "vome": "fass"}),
    ("duviri", "duviriCycle", "Duviri", None),
    ("zariman", "zarimanCycle", "Zariman", {"corpus": "grineer", "grineer": "corpus"}),
)


def cycle_rows(state: dict, now: int) -> list[dict]:
    """One row per cycle with an expiry still ahead; ``urgent`` = < 20 % of the window left."""
    out = []
    for key, skey, label, nxt in _CYCLES:
        c = state.get(skey)
        if not isinstance(c, dict):
            continue
        exp = ms(c.get("expiry"))
        st = c.get("state")
        if not exp or exp <= now or not st:
            continue
        # Duviri: no urgency (WFHelper passes no window for it)
        act = None if key == "duviri" else ms(c.get("activation"))
        next_state = c.get("nextState") if nxt is None else nxt.get(str(st).lower())
        out.append({"key": key, "label": label, "state": _title(st), "next": _title(next_state) if next_state else None,
                    "expiryMs": exp, "activationMs": act, "urgent": urgent(act, exp, now)})
    return out


# ---- timers (buildWorldTimes) ------------------------------------------------------------

def active_window(obj: dict | None, now: int) -> bool:
    if not isinstance(obj, dict):
        return False
    a, e = ms(obj.get("activation")), ms(obj.get("expiry"))
    return bool(a is not None and e and a <= now < e)


def baro_view(state: dict, now: int) -> dict | None:
    b = state.get("voidTrader")
    if not isinstance(b, dict):
        return None
    act, exp = ms(b.get("activation")), ms(b.get("expiry"))
    active = active_window(b, now)
    return {"active": active, "location": b.get("location") or "Unknown", "activationMs": act, "expiryMs": exp,
            "targetMs": exp if active else act, "inventory": (b.get("inventory") or []) if active else [],
            "itemCount": len(b.get("inventory") or []) if active else 0}


def varzia_view(state: dict, now: int) -> dict | None:
    v = state.get("vaultTrader")
    if not isinstance(v, dict):
        return None
    return {"active": active_window(v, now), "activationMs": ms(v.get("activation")), "expiryMs": ms(v.get("expiry")),
            "inventory": v.get("inventory") or []}


def timers(state: dict, now: int) -> list[dict]:
    resets = state.get("resets") or ws.compute_resets(now)
    daily, weekly = ms(resets.get("daily")), ms(resets.get("weekly"))
    out = [{"key": "daily", "label": "Daily reset", "expiryMs": daily, "activationMs": daily - DAY_MS if daily else None},
           {"key": "weekly", "label": "Weekly reset", "expiryMs": weekly,
            "activationMs": weekly - WEEK_MS if weekly else None}]

    def add(key, label, obj, fallback, fallback_span):
        exp = ms((obj or {}).get("expiry")) or fallback
        act = ms((obj or {}).get("activation")) or (exp - fallback_span if exp else None)
        out.append({"key": key, "label": label, "expiryMs": exp, "activationMs": act})

    add("sortie", "Sortie", state.get("sortie"), daily, DAY_MS)
    add("archon", "Archon Hunt", state.get("archonHunt"), weekly, WEEK_MS)
    add("steelPath", "Steel Path honors", state.get("steelPath"), weekly, WEEK_MS)
    b = baro_view(state, now)
    if b and b["targetMs"]:
        out.append({"key": "baro", "label": "Baro leaves" if b["active"] else "Baro arrives",
                    "expiryMs": b["targetMs"], "activationMs": b["activationMs"] if b["active"] else None})
    v = varzia_view(state, now)
    if v and v["expiryMs"] and v["expiryMs"] > now:
        out.append({"key": "varzia", "label": "Varzia rotation", "expiryMs": v["expiryMs"],
                    "activationMs": v["activationMs"]})
    for t in out:
        t["urgent"] = urgent(t.get("activationMs"), t.get("expiryMs"), now)
    return out


# ---- other sections ----------------------------------------------------------------------

def _with_ms(obj: dict | None) -> dict | None:
    if not isinstance(obj, dict):
        return None
    return {**obj, "activationMs": ms(obj.get("activation")), "expiryMs": ms(obj.get("expiry"))}


def steel_path_view(state: dict, now: int) -> dict | None:
    sp = state.get("steelPath")
    if not isinstance(sp, dict):
        return None
    upcoming = []
    for u in sp.get("upcoming") or []:
        a = ms(u.get("activation"))
        upcoming.append({**u, "activationMs": a, "inDays": max(1, math.ceil((a - now) / DAY_MS)) if a else None})
    return {"current": sp.get("currentReward"), "expiryMs": ms(sp.get("expiry")), "upcoming": upcoming,
            "evergreens": sp.get("evergreens") or []}


def circuit_view(state: dict) -> dict:
    c = state.get("circuit") or {}
    out = {}
    for cat in ("normal", "hard"):
        e = c.get(cat) or {}
        weeks = e.get("weeks") or []
        out[cat] = {"choices": e.get("choices") or [], "next": weeks[1:4], "known": bool(weeks)}
    return out


def nightwave_view(state: dict, now: int) -> dict | None:
    nw = state.get("nightwave")
    if not isinstance(nw, dict):
        return None
    ch = []
    for c in nw.get("challenges") or []:
        exp = ms(c.get("expiry"))
        if exp and exp <= now:
            continue
        kind = "daily" if c.get("isDaily") else ("elite" if c.get("isElite") else "weekly")
        ch.append({"id": c.get("id"), "title": c.get("title") or "", "description": c.get("description") or "",
                   "standing": c.get("standing"), "kind": kind, "expiryMs": exp})
    order = {"daily": 0, "weekly": 1, "elite": 2}
    ch.sort(key=lambda c: (order[c["kind"]], c["expiryMs"] or 0, c["title"]))
    return {"season": nw.get("season"), "phase": nw.get("phase"), "expiryMs": ms(nw.get("expiry")), "challenges": ch}


def _reward_text(reward: dict | None) -> str:
    reward = reward or {}
    parts = [f"{c['count']}x {c['type']}" if (c.get("count") or 1) > 1 else str(c.get("type"))
             for c in reward.get("countedItems") or [] if isinstance(c, dict)]
    parts += [str(i) for i in reward.get("items") or []]
    if reward.get("credits"):
        parts.append(f"{reward['credits']:,} cr")
    return ", ".join(parts)


def invasion_rows(state: dict) -> list[dict]:
    out = []
    for inv in state.get("invasions") or []:
        if not isinstance(inv, dict) or inv.get("completed"):
            continue
        atk, dfn = inv.get("attacker") or {}, inv.get("defender") or {}
        out.append({"id": inv.get("id"), "node": inv.get("node") or "", "planet": inv.get("planet") or "",
                    "attacker": atk.get("faction") or "", "defender": dfn.get("faction") or "",
                    "attackerReward": "" if inv.get("vsInfestation") else _reward_text(atk.get("reward")),
                    "defenderReward": _reward_text(dfn.get("reward")),
                    "vsInfestation": bool(inv.get("vsInfestation")),
                    "completion": inv.get("completion") or 0})
    return out


def world_view(state: dict, now: int) -> dict:
    rows = fissure_rows(state.get("fissures"), now)
    return {
        "now": now,
        "fetchedAt": state.get("fetchedAt"), "stale": bool(state.get("stale")), "error": state.get("error"),
        "refreshing": bool(state.get("refreshing")), "source": state.get("source"),
        "timers": timers(state, now),
        "cycles": cycle_rows(state, now),
        "fissures": rows,
        "fissureCounts": fissure_counts(rows),
        "sortie": _with_ms(state.get("sortie")),
        "archon": _with_ms(state.get("archonHunt")),
        "invasions": invasion_rows(state),
        "alerts": [_with_ms(a) for a in state.get("alerts") or [] if isinstance(a, dict)],
        "nightwave": nightwave_view(state, now),
        "baro": baro_view(state, now),
        "varzia": varzia_view(state, now),
        "darvo": [_with_ms(d) for d in state.get("dailyDeals") or [] if isinstance(d, dict)],
        "steelPath": steel_path_view(state, now),
        "circuit": circuit_view(state),
        "duviri": {"state": (state.get("duviriCycle") or {}).get("state")},
    }


# ---- QAM ---------------------------------------------------------------------------------

def ui_prefs(settings) -> dict:
    mode = settings.get("world_ui.fissure_mode", "normal")
    tiers = settings.get("world_ui.fissure_tiers", list(TIERS))
    limit = settings.get("world_ui.qam_limit", QAM_LIMIT_DEFAULT)
    return {"fissureMode": mode if mode in FISSURE_MODES else "normal",
            "fissureTiers": [t for t in TIERS if isinstance(tiers, list) and t in tiers],
            "qamLimit": _clamp_int(limit, 1, 20, QAM_LIMIT_DEFAULT)}


def _clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = round(float(v))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def next_arbitration(sched: dict | None) -> dict:
    sched = sched or {}
    favs = set((sched.get("alerts") or {}).get("favoriteNodes") or [])
    now = sched.get("now") or _now()
    nxt_fav = next((e for e in sched.get("entries") or [] if e.get("nodeId") in favs and e.get("epochMs", 0) > now),
                   None)
    return {"current": sched.get("current"), "next": sched.get("next"), "nextFavorite": nxt_fav,
            "error": sched.get("error")}


def arbi_payload(days: float = 1, fetch: bool = False) -> dict | None:
    """Schedule payload without blocking on the network (the schedule's own loop refreshes it)."""
    try:
        from ..data import arbi_schedule

        return arbi_schedule.get().payload(days, fetch=fetch)
    except Exception as e:  # noqa: BLE001
        _log.debug("arbitration schedule unavailable: %s", e)
        return None


def qam_view(state: dict, now: int, prefs: dict, sched: dict | None) -> dict:
    rows = fissure_rows(state.get("fissures"), now)
    shown = filter_fissures(rows, prefs["fissureMode"], prefs["fissureTiers"], prefs["qamLimit"])
    t = {x["key"]: x for x in timers(state, now)}
    return {
        "now": now, "stale": bool(state.get("stale")), "error": state.get("error"),
        "resets": [t[k] for k in ("daily", "weekly", "sortie") if k in t],
        "cycles": cycle_rows(state, now),
        "fissureCounts": fissure_counts(rows),
        "fissures": shown,
        "prefs": prefs,
        "arbitration": next_arbitration(sched),
        "baro": baro_view(state, now),
        "circuit": circuit_view(state),
    }


# ---- dailies tracker (src/lib/world/dailies.ts, dailiesAuto.ts) -----------------------------

# id, label, period, target, group
DAILY_TASKS = (
    ("sortie", "Sortie", "sortie", 1, "daily"),
    ("spIncursions", "Steel Path Incursions", "daily", 5, "daily"),
    ("simaris", "Simaris synthesis target", "daily", 1, "daily"),
    ("syndicateStanding", "Syndicate standing cap", "daily", 1, "daily"),
    ("dailyFocus", "Daily Focus cap", "daily", 1, "daily"),
    ("archonHunt", "Archon Hunt", "archon", 1, "weekly"),
    ("circuitNormal", "The Circuit (Normal)", "weekly", 1, "weekly"),
    ("circuitSteelPath", "The Circuit (Steel Path)", "weekly", 1, "weekly"),
    ("netracells", "Netracells", "weekly", 5, "weekly"),
    ("deepArchimedea", "Deep Archimedea", "weekly", 1, "weekly"),
    ("temporalArchimedea", "Temporal Archimedea", "weekly", 1, "weekly"),
    ("kahl", "Break Narmer (Kahl)", "weekly", 1, "weekly"),
    ("clem", "Help Clem", "weekly", 1, "weekly"),
    ("ayatanHunt", "Ayatan Treasure Hunt", "weekly", 1, "weekly"),
    ("descendiaNormal", "The Descendia (Normal)", "descendia", 1, "weekly"),
    ("descendiaSteelPath", "The Descendia (Steel Path)", "descendia", 1, "weekly"),
    ("calendar1999", "1999 Calendar tasks", "calendar1999", 1, "weekly"),
    ("steelPathHonors", "Steel Path Honors (Teshin)", "steelPath", 1, "weekly"),
    ("palladino", "Palladino (Iron Wake)", "weekly", 1, "vendors"),
    ("acrithis", "Acrithis weekly stock", "weekly", 1, "vendors"),
    ("bird3", "Bird 3 Archon Shard", "weekly", 1, "vendors"),
    ("yonta", "Yonta weekly Kuva trade", "weekly", 1, "vendors"),
    ("tenetMelee", "Tenet melee rotation (Ergo Glast)", "tenet", 1, "vendors"),
    ("codaWeapons", "Coda weapon rotation (Eleanor)", "coda", 1, "vendors"),
    ("baro", "Visit Baro Ki'Teer", "baro", 1, "vendors"),
    ("varzia", "Visit Varzia", "varzia", 1, "vendors"),
    ("darvo", "Buy Darvo's Deal", "darvo", 1, "vendors"),
)
TASK_IDS = {t[0] for t in DAILY_TASKS}
VENDOR_ROTATION_MS = 4 * DAY_MS
VENDOR_ROTATION_ANCHORS = {"tenet": int(datetime(2015, 12, 3, tzinfo=timezone.utc).timestamp() * 1000),
                           "coda": int(datetime(2025, 3, 18, tzinfo=timezone.utc).timestamp() * 1000)}
KAHL_EPOCH_MS = int(datetime(2014, 2, 10, tzinfo=timezone.utc).timestamp() * 1000)
MAX_TARGET = 99


def next_vendor_rotation_ms(now: int, period: str) -> int:
    anchor = VENDOR_ROTATION_ANCHORS[period]
    return anchor + ((now - anchor) // VENDOR_ROTATION_MS + 1) * VENDOR_ROTATION_MS


def tracker_expiries(state: dict | None) -> dict:
    s = state or {}
    deals = s.get("dailyDeals") or []
    return {
        "sortie": (s.get("sortie") or {}).get("expiry"),
        "archon": (s.get("archonHunt") or {}).get("expiry"),
        "steelPath": (s.get("steelPath") or {}).get("expiry"),
        "descendia": (s.get("descents") or {}).get("expiry"),
        "calendar1999": (s.get("calendarSeason") or {}).get("expiry"),
        "baro": (s.get("voidTrader") or {}).get("activation"),  # one visit, stable away -> here
        "darvo": deals[0].get("expiry") if deals and isinstance(deals[0], dict) else None,
        "varzia": (s.get("vaultTrader") or {}).get("activation"),
    }


def period_key(period: str, now: int, expiries: dict) -> str | None:
    """Key naming the current window of ``period``; None while the world state lacks it."""
    resets = ws.compute_resets(now)
    if period == "daily":
        return f"daily:{resets['daily']}"
    if period == "weekly":
        return f"weekly:{resets['weekly']}"
    if period in VENDOR_ROTATION_ANCHORS:
        return f"{period}:{ws.iso(next_vendor_rotation_ms(now, period))}"
    v = expiries.get(period)
    return f"{period}:{v}" if v else None


def period_reset_ms(period: str, now: int, state: dict | None) -> int | None:
    resets = ws.compute_resets(now)
    if period == "daily":
        return ms(resets["daily"])
    if period == "weekly":
        return ms(resets["weekly"])
    if period in VENDOR_ROTATION_ANCHORS:
        return next_vendor_rotation_ms(now, period)
    s = state or {}
    src = {"sortie": "sortie", "archon": "archonHunt", "steelPath": "steelPath", "descendia": "descents",
           "calendar1999": "calendarSeason", "baro": "voidTrader", "varzia": "vaultTrader"}.get(period)
    if period == "darvo":
        deals = s.get("dailyDeals") or []
        return ms(deals[0].get("expiry")) if deals and isinstance(deals[0], dict) else None
    return ms((s.get(src) or {}).get("expiry")) if src else None


def _de_date_ms(v: Any) -> int | None:
    n = ws.de_ms(v) if v is not None else 0
    return n if n > 0 else None


def _oid(v: Any) -> str | None:
    return v.get("$oid") if isinstance(v, dict) and isinstance(v.get("$oid"), str) else None


def _num(v: Any) -> float | None:
    if isinstance(v, dict):
        for k in ("$numberInt", "$numberLong", "$numberDouble"):
            if k in v:
                v = v[k]
                break
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def auto_tracker(inv: dict | None, state: dict | None, now: int, inv_time_ms: int | None) -> dict:
    """Doneness signals from the raw DE inventory (dailiesAuto.ts). Rules only ever add doneness."""
    out: dict[str, dict] = {}
    if not isinstance(inv, dict):
        return out
    s = state or {}

    def reward_matches(rewards: Any, active_id: str | None) -> bool:
        return bool(active_id) and any(isinstance(e, dict) and _oid(e.get("SortieId")) == active_id
                                       for e in ws.as_list(rewards))

    if reward_matches(inv.get("LastSortieReward"), (s.get("sortie") or {}).get("id")):
        out["sortie"] = {"count": 1}
    if reward_matches(inv.get("LastLiteSortieReward"), (s.get("archonHunt") or {}).get("id")):
        out["archonHunt"] = {"count": 1}

    reset = _de_date_ms(inv.get("EntratiVaultCountResetDate"))
    cells = _num(inv.get("EntratiVaultCountLastPeriod"))
    if cells is not None and reset is not None:
        out["netracells"] = {"count": int(min(cells, MAX_TARGET)) if reset > now else 0}
    for field, tid in (("EntratiLabConquestCacheScoreMission", "deepArchimedea"),
                       ("EchoesHexConquestCacheScoreMission", "temporalArchimedea")):
        score = _num(inv.get(field))
        if score and score > 0 and reset and reset > now:
            out[tid] = {"count": 0, "detail": f"Score {int(score)}"}

    resets = ws.compute_resets(now)
    week_start = ms(resets["weekly"]) - WEEK_MS
    day_start = ms(resets["daily"]) - DAY_MS

    if inv_time_ms is not None and inv_time_ms >= day_start:
        for field, tid, unit in (("DailyAffiliation", "syndicateStanding", "standing"), ("DailyFocus", "dailyFocus", "focus")):
            rem = _num(inv.get(field))
            if rem is not None:
                out[tid] = {"count": 1 if rem == 0 else 0}
                if rem > 0:
                    out[tid]["detail"] = f"{int(rem):,} {unit} left"
        lsi = inv.get("LibraryActiveDailyTaskInfo")
        if isinstance(lsi, dict):
            # Informational only, as WFHelper: the scan count never ticks the box by itself.
            have, need = _num(lsi.get("Scans")), _num(lsi.get("ScansRequired"))
            if need and need > 0 and have is not None and have >= 0:
                out["simaris"] = {"count": 0, "detail": f"{int(min(have, need))}/{int(need)} scans"}

    def periodic_since(prefix: str, start: int) -> int:
        n = 0
        for e in ws.as_list(inv.get("PeriodicMissionCompletions")):
            if not isinstance(e, dict) or not str(e.get("tag") or "").startswith(prefix):
                continue
            d = _de_date_ms(e.get("date"))
            if d is not None and d >= start:
                n += 1
        return n

    if periodic_since("GetClem", week_start) > 0:
        out["clem"] = {"count": 1}
    if periodic_since("TreasureHunt", week_start) > 0:
        out["ayatanHunt"] = {"count": 1}
    inc = periodic_since("HardDaily", day_start)
    if inc > 0:
        out["spIncursions"] = {"count": min(inc, 5)}

    week = (now - KAHL_EPOCH_MS) // WEEK_MS
    for aff in ws.as_list(inv.get("Affiliations")):
        if isinstance(aff, dict) and aff.get("Tag") == "KahlSyndicate":
            if any(isinstance(m, dict) and m.get("WeekCount") == week and m.get("CompletedMission") is True
                   for m in ws.as_list(aff.get("WeeklyMissions"))):
                out["kahl"] = {"count": 1}
    return out


class DailiesStore:
    """``<runtime>/world-dailies.json``: {progress: {id: {key, count}}, hidden: [id]} (WFHelper key)."""

    def __init__(self, runtime_dir: str):
        self.path = os.path.join(runtime_dir, "world-dailies.json")
        self._lock = threading.Lock()
        self.data = {"progress": {}, "hidden": []}
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            self.data = self._revive(raw)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            _log.warning("dailies store unreadable: %s", e)

    @staticmethod
    def _revive(raw: Any) -> dict:
        raw = raw if isinstance(raw, dict) else {}
        prog = {}
        for k, v in (raw.get("progress") or {}).items():
            if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("key"), str) \
                    and isinstance(v.get("count"), (int, float)) and not isinstance(v.get("count"), bool):
                prog[k] = {"key": v["key"], "count": max(0, min(int(v["count"]), MAX_TARGET))}
        hidden = [h for h in raw.get("hidden") or [] if isinstance(h, str)]
        return {"progress": prog, "hidden": hidden}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)

    def count(self, tid: str, key: str | None, now: int) -> int:
        e = self.data["progress"].get(tid)
        if not e:
            return 0
        if key is not None:
            return e["count"] if e["key"] == key else 0
        # Period unknown (world state missing): retire entries whose own expiry has passed.
        if e["key"].startswith(("baro:", "varzia:")):
            return e["count"]
        exp = ms(e["key"].split(":", 1)[1]) if ":" in e["key"] else None
        return 0 if exp is not None and exp <= now else e["count"]

    def set(self, tid: str, key: str | None, count: int) -> None:
        with self._lock:
            prev = self.data["progress"].get(tid, {}).get("key", "")
            self.data["progress"][tid] = {"key": key if key is not None else prev,
                                          "count": max(0, min(int(count), MAX_TARGET))}
            self._save()

    def set_hidden(self, tid: str, hidden: bool) -> None:
        with self._lock:
            h = [x for x in self.data["hidden"] if x != tid]
            if hidden:
                h.append(tid)
            self.data["hidden"] = h
            self._save()


def dailies_view(store: DailiesStore, state: dict | None, inv: dict | None, inv_time_ms: int | None,
                 now: int) -> dict:
    exp = tracker_expiries(state)
    auto = auto_tracker(inv, state, now, inv_time_ms)
    rows = []
    for tid, label, period, target, group in DAILY_TASKS:
        key = period_key(period, now, exp)
        manual = store.count(tid, key, now)
        a = auto.get(tid) or {}
        count = max(manual, int(a.get("count") or 0))
        rows.append({"id": tid, "label": label, "group": group, "period": period, "target": target,
                     "count": min(count, target), "manual": manual, "auto": bool(a.get("count")),
                     "detail": a.get("detail"), "done": count >= target,
                     "hidden": tid in store.data["hidden"], "resetMs": period_reset_ms(period, now, state)})
    return {"now": now, "tasks": rows, "inventory": inv is not None,
            "doneCount": sum(1 for r in rows if r["done"] and not r["hidden"]),
            "visibleCount": sum(1 for r in rows if not r["hidden"])}


# ---- wiring ------------------------------------------------------------------------------

_store: DailiesStore | None = None


def store() -> DailiesStore:
    global _store
    if _store is None:
        _store = DailiesStore(get_app().runtime_dir)
    return _store


def _inventory() -> tuple[dict | None, int | None]:
    try:
        from .. import inventory as inv_mod

        inv = inv_mod.inventory()
    except Exception:  # noqa: BLE001
        return None, None
    if inv is None:
        return None, None
    t = inv.sync_time or int(inv.fetched_at * 1000)
    return inv.data, t


def setup(app: App) -> None:
    global _store
    _store = DailiesStore(app.runtime_dir)
    app.services["world_view"] = lambda: world_view(ws.state(ws.RPC_MAX_AGE), _now())


@rpc.method("world.view")
async def _rpc_view(force: bool = False) -> dict:
    app = get_app()

    def run() -> dict:
        st = ws.state(0 if force else ws.RPC_MAX_AGE)
        out = world_view(st, _now())
        out["prefs"] = ui_prefs(app.settings)
        return out

    return await app.run_blocking(run)


@rpc.method("world.qam")
async def _rpc_qam() -> dict:
    app = get_app()

    def run() -> dict:
        st = ws.state(ws.RPC_MAX_AGE)
        return qam_view(st, _now(), ui_prefs(app.settings), arbi_payload(1))

    return await app.run_blocking(run)


@rpc.method("world.ui_set")
def _rpc_ui_set(fissure_mode: str | None = None, fissure_tiers: list | None = None,
                qam_limit: int | None = None) -> dict:
    app = get_app()
    patch: dict = {}
    if fissure_mode in FISSURE_MODES:
        patch["fissure_mode"] = fissure_mode
    if isinstance(fissure_tiers, list):
        patch["fissure_tiers"] = [t for t in TIERS if t in fissure_tiers]
    if qam_limit is not None:
        patch["qam_limit"] = _clamp_int(qam_limit, 1, 20, QAM_LIMIT_DEFAULT)
    if patch:
        app.settings.update({"world_ui": patch})
    return ui_prefs(app.settings)


@rpc.method("world.dailies")
async def _rpc_dailies() -> dict:
    def run() -> dict:
        inv, t = _inventory()
        return dailies_view(store(), ws.state(ws.RPC_MAX_AGE), inv, t, _now())

    return await get_app().run_blocking(run)


@rpc.method("world.dailies_set")
async def _rpc_dailies_set(id: str, count: int = 1) -> dict:  # noqa: A002 - RPC param name
    def run() -> dict:
        if id not in TASK_IDS:
            raise ValueError(f"unknown task {id!r}")
        st = ws.state(ws.RPC_MAX_AGE)
        now = _now()
        period = next(t[2] for t in DAILY_TASKS if t[0] == id)
        store().set(id, period_key(period, now, tracker_expiries(st)), _clamp_int(count, 0, MAX_TARGET, 0))
        inv, t = _inventory()
        return dailies_view(store(), st, inv, t, now)

    return await get_app().run_blocking(run)


@rpc.method("world.dailies_hide")
async def _rpc_dailies_hide(id: str, hidden: bool = True) -> dict:  # noqa: A002
    def run() -> dict:
        if id not in TASK_IDS:
            raise ValueError(f"unknown task {id!r}")
        store().set_hidden(id, bool(hidden))
        inv, t = _inventory()
        return dailies_view(store(), ws.state(ws.RPC_MAX_AGE), inv, t, _now())

    return await get_app().run_blocking(run)

