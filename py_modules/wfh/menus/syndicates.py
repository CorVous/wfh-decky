"""Syndicates page / QAM view-models (docs/port/menus.md §11).

Port of WFHelper src/lib/syndicates/rankup.ts: rank and standing per syndicate from
the inventory's Affiliations, the daily standing cap (16000 + 500 x MR) with today's
remaining pool per bin, and rank-up goals (credits, sacrifice items, standing and an
estimate in days). The rank table is WFHelper's src/data/syndicateRanks.json (MIT,
generated from DE's ExportSyndicates), shipped next to this file.

Pure: load_table, syndicate_status, daily_cap, plan_steps, aggregate_needs,
alignment_conflicts, view_model, qam_model.

RPCs
  syndicates.view()                         groups normal/openWorld/other, pools, goals plan
  syndicates.qam(limit)                     pools + ranked syndicates (compact)
  syndicates.set_goal(tag, level)           level None/<= current clears the goal
  syndicates.clear_goals()
Settings: syndicates.goals = [[tag, level], ...]; syndicates.qam_limit.
Event: syndicates.updated after a new inventory snapshot.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Callable

from .. import log, rpc

_log = log.get("menus.syndicates")

TABLE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "syndicate_ranks.json")
DAILY_CAP_BASE = 16000
DAILY_CAP_PER_MR = 500
KINDS = (("normal", "Factions"), ("openWorld", "Open world"), ("other", "Other"))
BIN_LABEL = {"NORMAL": "Factions", "CAVIA": "Cavia", "ENTRATI": "Entrati", "NECRALOID": "Necraloid",
             "CETUS": "Ostron", "SOLARIS": "Solaris", "HEX": "Hex", "ZARIMAN": "Holdfasts", "QUILLS": "Quills",
             "VENTKIDS": "Ventkids", "VOX": "Vox", "PVP": "Conclave"}

_table: list[dict] | None = None


def load_table(path: str = TABLE_FILE) -> list[dict]:
    global _table
    if _table is None or path != TABLE_FILE:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f).get("syndicates") or []
        if path != TABLE_FILE:
            return rows
        _table = rows
    return _table


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return v


def _affiliation(inv: dict | None, tag: str) -> dict | None:
    for e in (inv or {}).get("Affiliations") or []:
        if isinstance(e, dict) and e.get("Tag") == tag:
            return e
    return None


def daily_cap(inv: dict | None) -> int:
    mr = _num((inv or {}).get("PlayerLevel"))
    if mr is None or mr < 0:
        return DAILY_CAP_BASE
    return DAILY_CAP_BASE + DAILY_CAP_PER_MR * int(mr)


def _title(meta: dict, level: int) -> dict | None:
    return next((t for t in meta.get("titles") or [] if t.get("level") == level), None)


def rank_threshold(meta: dict, level: int) -> int | None:
    own = _title(meta, level)
    if own:
        return own["minStanding"]
    below = _title(meta, level - 1)
    if below:
        return below["maxStanding"]
    return 0 if level == 0 else None


def max_level(meta: dict) -> int:
    return max([t["level"] for t in meta.get("titles") or []] + [0])


def min_level(meta: dict) -> int:
    return min([t["level"] for t in meta.get("titles") or []] + [0])


def syndicate_status(inv: dict | None, meta: dict) -> dict:
    e = _affiliation(inv, meta["tag"])
    level = int(_num((e or {}).get("Title")) or 0)
    standing = int(_num((e or {}).get("Standing")) or 0)
    nxt = level + 1 if level < max_level(meta) else None
    nxt_thr = rank_threshold(meta, nxt) if nxt is not None else None
    start = rank_threshold(meta, level) or 0
    t = _title(meta, level)
    end = nxt_thr if nxt_thr is not None else (t["maxStanding"] if t else start)
    remaining = _num((inv or {}).get(meta["dailyField"]))
    span = end - start
    return {"level": level, "title": t["name"] if t else "", "standing": standing, "nextLevel": nxt,
            "nextTitle": (_title(meta, nxt) or {}).get("name", "") if nxt is not None else "",
            "standingToNext": 0 if nxt_thr is None else max(0, nxt_thr - standing), "tierStart": start,
            "tierEnd": end, "progress": max(0.0, min(1.0, (standing - start) / span)) if span > 0 else 1.0,
            "initiated": (e or {}).get("Initiated") is True, "joined": e is not None,
            "dailyRemaining": int(remaining) if remaining is not None else None}


def _merge_items(into: dict, items: list) -> None:
    for it in items or []:
        cur = into.get(it["itemType"])
        if cur:
            cur["count"] += it["count"]
        else:
            into[it["itemType"]] = dict(it)


def plan_steps(inv: dict | None, meta: dict, target: int) -> list[dict]:
    st = syndicate_status(inv, meta)
    top = min(target, max_level(meta))
    steps, prev = [], None
    for level in range(st["level"] + 1, top + 1):
        thr = rank_threshold(meta, level)
        if thr is None:
            continue
        items: dict = {}
        credits = 0
        initiation = level == 1 and not st["initiated"] and bool(meta.get("initiation"))
        if initiation:
            credits += meta["initiation"]["credits"]
            _merge_items(items, meta["initiation"]["items"])
        sac = (_title(meta, level - 1) if level <= 0 else _title(meta, level)) or {}
        if sac.get("sacrifice"):
            credits += sac["sacrifice"]["credits"]
            _merge_items(items, sac["sacrifice"]["items"])
        steps.append({"level": level, "title": (_title(meta, level) or {}).get("name", ""),
                      "standingNeeded": max(0, thr - (prev if prev is not None else st["standing"])),
                      "credits": credits, "items": list(items.values()), "initiation": initiation})
        prev = thr
    return steps


def aggregate_needs(plans: list[dict], inv: dict | None, owned: Callable[[str], float]) -> dict:
    cap = daily_cap(inv)
    items: dict[str, dict] = {}
    pools: dict[str, dict] = {}
    credits = 0
    for plan in plans:
        meta = plan["meta"]
        standing = 0
        for step in plan["steps"]:
            credits += step["credits"]
            standing += step["standingNeeded"]
            for it in step["items"]:
                cur = items.get(it["itemType"])
                if cur:
                    cur["needed"] += it["count"]
                else:
                    items[it["itemType"]] = {"itemType": it["itemType"], "name": it.get("name") or it["itemType"],
                                             "needed": it["count"], "owned": 0, "missing": 0}
        pool = pools.get(meta["dailyBin"])
        if pool:
            pool["needed"] += standing
            if meta["tag"] not in pool["tags"]:
                pool["tags"].append(meta["tag"])
        else:
            rem = _num((inv or {}).get(meta["dailyField"]))
            pools[meta["dailyBin"]] = {"bin": meta["dailyBin"], "label": BIN_LABEL.get(meta["dailyBin"], meta["dailyBin"]),
                                       "dailyField": meta["dailyField"], "tags": [meta["tag"]], "needed": standing,
                                       "remainingToday": int(rem) if rem is not None else None, "dailyCap": cap,
                                       "daysEstimate": 0}
    for it in items.values():
        it["owned"] = int(owned(it["itemType"]))
        it["missing"] = max(0, it["needed"] - it["owned"])
    for p in pools.values():
        after = max(0, p["needed"] - (p["remainingToday"] or 0))
        p["daysEstimate"] = 0 if after == 0 else math.ceil(after / p["dailyCap"])
    have = max(0, int(_num((inv or {}).get("RegularCredits")) or 0))
    return {"credits": {"needed": credits, "owned": have, "missing": max(0, credits - have)},
            "items": sorted(items.values(), key=lambda i: (-i["missing"], i["name"].lower())),
            "standing": list(pools.values())}


def alignment_conflicts(metas: list[dict]) -> list[dict]:
    selected = {m["tag"] for m in metas}
    names = {m["tag"]: m["name"] for m in metas}
    seen, out = set(), []
    for m in metas:
        for other, factor in (m.get("alignments") or {}).items():
            if factor >= 0 or other not in selected:
                continue
            key = "|".join(sorted((m["tag"], other)))
            if key in seen:
                continue
            seen.add(key)
            out.append({"a": m["tag"], "b": other, "aName": names[m["tag"]], "bName": names[other], "factor": factor})
    return out


def normalize_goals(raw: Any, table: list[dict]) -> dict[str, int]:
    tags = {m["tag"] for m in table}
    out: dict[str, int] = {}
    if isinstance(raw, dict):
        raw = list(raw.items())
    for pair in raw if isinstance(raw, list) else []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] in tags and isinstance(pair[1], int) \
                and not isinstance(pair[1], bool):
            out[pair[0]] = pair[1]
    return out


def daily_pools(inv: dict | None, table: list[dict]) -> list[dict]:
    """Today's remaining standing per bin (the six factions share one)."""
    cap = daily_cap(inv)
    seen: dict[str, dict] = {}
    for m in table:
        if m["dailyBin"] in seen:
            seen[m["dailyBin"]]["tags"].append(m["tag"])
            continue
        rem = _num((inv or {}).get(m["dailyField"]))
        seen[m["dailyBin"]] = {"bin": m["dailyBin"], "label": BIN_LABEL.get(m["dailyBin"], m["name"]),
                               "kind": m["kind"], "remaining": int(rem) if rem is not None else None, "cap": cap,
                               "tags": [m["tag"]]}
    return list(seen.values())


def _row(inv: dict | None, meta: dict, goal: int | None) -> dict:
    st = syndicate_status(inv, meta)
    levels = [{"level": lv, "title": (_title(meta, lv) or {}).get("name", "") or ("Neutral" if lv == 0 else "")}
              for lv in range(max(st["level"] + 1, min_level(meta)), max_level(meta) + 1)]
    return {"tag": meta["tag"], "name": meta["name"], "kind": meta["kind"], "bin": meta["dailyBin"],
            **st, "maxLevel": max_level(meta), "goal": goal if goal is not None and goal > st["level"] else None,
            "goalOptions": levels}


def view_model(inv: dict | None, table: list[dict], goals: dict[str, int],
               owned: Callable[[str], float] = lambda _u: 0) -> dict:
    rows = [_row(inv, m, goals.get(m["tag"])) for m in table]
    groups = [{"kind": k, "label": label, "rows": [r for r in rows if r["kind"] == k]} for k, label in KINDS]
    by_tag = {m["tag"]: m for m in table}
    plans = []
    for tag, target in goals.items():
        meta = by_tag.get(tag)
        if meta is None:
            continue
        steps = plan_steps(inv, meta, target)
        if steps:
            plans.append({"meta": meta, "targetLevel": target, "steps": steps})
    needs = aggregate_needs(plans, inv, owned) if plans else None
    return {"inventory": inv is not None, "mr": int(_num((inv or {}).get("PlayerLevel")) or 0) if inv else None,
            "dailyCap": daily_cap(inv), "pools": daily_pools(inv, table), "groups": groups,
            "plans": [{"tag": p["meta"]["tag"], "name": p["meta"]["name"], "targetLevel": p["targetLevel"],
                       "steps": p["steps"]} for p in plans],
            "needs": needs, "conflicts": alignment_conflicts([by_tag[p["meta"]["tag"]] for p in plans])}


def qam_model(inv: dict | None, table: list[dict], goals: dict[str, int], limit: int = 6) -> dict:
    """Pools with standing left, then joined syndicates: goals first, then highest rank."""
    rows = [_row(inv, m, goals.get(m["tag"])) for m in table]
    joined = [r for r in rows if r["joined"] and (r["level"] != 0 or r["standing"] != 0)]
    joined.sort(key=lambda r: (r["goal"] is None, -r["level"], -r["standing"]))
    pools = [p for p in daily_pools(inv, table) if p["remaining"] is not None]
    keep = {r["bin"] for r in joined} | {"NORMAL"}
    return {"inventory": inv is not None, "dailyCap": daily_cap(inv),
            "pools": [p for p in pools if p["bin"] in keep],
            "rows": [{k: r[k] for k in ("tag", "name", "level", "title", "standing", "standingToNext", "progress",
                                        "nextLevel", "goal", "dailyRemaining", "maxLevel")} for r in joined[:limit]],
            "more": max(0, len(joined) - limit)}


# ---- wiring -------------------------------------------------------------------------------------

def _inv_raw() -> tuple[Any, dict | None]:
    from .. import inventory

    inv = inventory.inventory()
    return inv, (inv.data if inv is not None else None)


def _goals(settings) -> dict[str, int]:
    return normalize_goals(settings.get("syndicates.goals", []), load_table())


def _app():
    from ..app import get_app

    return get_app()


def setup(app) -> None:
    from .. import inventory

    try:
        load_table()
    except (OSError, ValueError):
        _log.exception("syndicate rank table unreadable")
    inventory.on_update(lambda _inv: app.emit("syndicates.updated", None))


def _view() -> dict:
    inv, raw = _inv_raw()
    owned = inv.owned_count if inv is not None else (lambda _u: 0)
    return view_model(raw, load_table(), _goals(_app().settings), owned)


@rpc.method("syndicates.view")
async def _rpc_view() -> dict:
    return await _app().run_blocking(_view)


@rpc.method("syndicates.qam")
async def _rpc_qam(limit: int | None = None) -> dict:
    app = _app()
    lim = limit if isinstance(limit, int) else app.settings.get("syndicates.qam_limit", 6)
    _, raw = _inv_raw()
    return qam_model(raw, load_table(), _goals(app.settings), max(1, min(17, int(lim or 6))))


@rpc.method("syndicates.set_goal")
async def _rpc_set_goal(tag: str, level: int | None = None) -> dict:
    app = _app()
    goals = _goals(app.settings)
    goals.pop(tag, None)
    if isinstance(level, int) and not isinstance(level, bool) and any(m["tag"] == tag for m in load_table()):
        goals[tag] = level
    app.settings.update({"syndicates": {"goals": [[t, lv] for t, lv in goals.items()]}})
    return await app.run_blocking(_view)


@rpc.method("syndicates.clear_goals")
async def _rpc_clear_goals() -> dict:
    app = _app()
    app.settings.update({"syndicates": {"goals": []}})
    return await app.run_blocking(_view)
