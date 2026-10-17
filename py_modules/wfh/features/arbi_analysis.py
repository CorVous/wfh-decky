"""Per-run analysis for the Arbitrations page (pure).

Port of the WFHelper renderer helpers the run detail view uses
(``src/lib/arbi/arbiChartData.ts``, ``arbiDroneCadence.ts``,
``src/lib/runPersonalBest.ts``, ``arbiSpawnMap.ts`` top list), done in the
worker so the page only receives small arrays instead of thousands of drone
timestamps.
"""

from __future__ import annotations

import re

from .arbi_parser import ARBI_SATURATION_THRESHOLD, luck, merge_intervals, scenario_table, type_label

DRONE_WAIT_EDGES = (1, 2, 3, 5, 8, 12)
ARBI_DRY_WAIT_SEC = 12
ARBI_PEAK_WINDOW_SEC = 10


def drones_per_rotation(stats: dict) -> list[int]:
    out, i = [], 0
    drones = stats.get("droneTimestamps") or []
    for end in stats.get("rewardTimestamps") or []:
        n = 0
        while i < len(drones) and drones[i] <= end:
            n += 1
            i += 1
        out.append(n)
    return out


def rotation_windows(stats: dict) -> list[dict]:
    rewards = stats.get("rewardTimestamps") or []
    if not rewards:
        return []
    drones = stats.get("droneTimestamps") or []
    start = stats.get("preciseStartSec")
    if start is None:
        start = drones[0] if drones else stats.get("lastActivitySec", 0) - len(rewards) * 300
    if start >= rewards[0]:
        start = drones[0] if drones else rewards[0] - 300
    out = []
    for end in rewards:
        out.append({"start": start, "end": end})
        start = end
    return out


def dpm_series(stats: dict) -> list[float]:
    wins = rotation_windows(stats)
    return [n / (max(wins[i]["end"] - wins[i]["start"], 10) / 60) for i, n in enumerate(drones_per_rotation(stats))]


def overlap_seconds(intervals: list[dict], start: float, end: float) -> float:
    total = 0.0
    for iv in intervals:
        lo, hi = max(iv["start"], start), min(iv["end"], end)
        if hi > lo:
            total += hi - lo
    return total


def rotation_clear_cells(stats: dict) -> list[dict]:
    pauses = stats.get("pauseIntervals") or []
    sat = stats.get("rotationSaturationPct")
    return [{"index": i + 1,
             "durationSec": max(0.0, w["end"] - w["start"] - overlap_seconds(pauses, w["start"], w["end"])),
             "saturationPct": sat[i] if sat and i < len(sat) else None}
            for i, w in enumerate(rotation_windows(stats))]


def saturation_above_pct(buckets: list[dict], threshold: int = ARBI_SATURATION_THRESHOLD) -> float:
    total = sum(b.get("seconds", 0) for b in buckets)
    above = sum(b.get("seconds", 0) for b in buckets if b.get("minCount", 0) >= threshold)
    return above / total * 100 if total > 0 else 0.0


def densest_window(values: list[float], window: float) -> dict | None:
    if not values:
        return None
    best = {"start": values[0], "count": 0}
    right = 0
    for left in range(len(values)):
        while right < len(values) and values[right] - values[left] < window:
            right += 1
        if right - left > best["count"]:
            best = {"start": values[left], "count": right - left}
    return best


def drone_cadence(stats: dict) -> dict | None:
    drones_all = stats.get("droneTimestamps") or []
    start = stats.get("preciseStartSec")
    if start is None:
        start = drones_all[0] if drones_all else None
    end = stats.get("lastActivitySec")
    if start is None or end is None or not end > start:
        return None
    drones = sorted(t for t in drones_all if start <= t <= end)
    if len(drones) < 2:
        return None
    buckets = []
    lower = 0
    for edge in DRONE_WAIT_EDGES:
        buckets.append({"label": f"{lower}-{edge}s", "minSec": lower, "maxSec": edge, "seconds": 0.0, "pct": 0.0})
        lower = edge
    buckets.append({"label": f"{lower}s+", "minSec": lower, "maxSec": None, "seconds": 0.0, "pct": 0.0})
    idle = merge_intervals((stats.get("pauseIntervals") or []) + (stats.get("idleIntervals") or []))
    total = dry = 0.0
    for a, b in zip(drones, drones[1:]):
        wait = max(0.0, b - a - overlap_seconds(idle, a, b))
        if wait <= 0:
            continue
        bucket = next((x for x in buckets if x["maxSec"] is None or wait < x["maxSec"]), buckets[0])
        bucket["seconds"] += wait
        total += wait
        if wait >= ARBI_DRY_WAIT_SEC:
            dry += wait
    for bk in buckets:
        bk["pct"] = bk["seconds"] / total * 100 if total > 0 else 0.0
    peak = densest_window(drones, ARBI_PEAK_WINDOW_SEC)
    return {"buckets": buckets, "totalWaitSec": total, "dryPct": dry / total * 100 if total > 0 else 0.0,
            "peak": {"drones": peak["count"], "atSec": peak["start"] - start} if peak else None}


def percent_delta(value: float, ref: float | None) -> float | None:
    if ref is None or ref <= 0:
        return None
    return (value - ref) / ref * 100


def personal_best(value: float, others: list[float], higher_is_better: bool = True) -> dict:
    others = [o for o in others if isinstance(o, (int, float))]
    best_other = (max(others) if higher_is_better else min(others)) if others else None
    rank = sum(1 for o in others if (o > value if higher_is_better else o < value)) + 1
    return {"value": value, "rank": rank, "poolSize": len(others) + 1, "isPb": rank == 1,
            "vsBestPct": percent_delta(value, best_other),
            "vsSecondPct": percent_delta(value, best_other) if rank == 1 else None}


def usable(run: dict) -> bool:
    return run.get("endReason") != "log-truncated" and not run.get("duplicateOf")


def per_min(value: float, duration: float) -> float | None:
    return value / duration * 60 if duration and duration > 0 else None


def spawn_top(stats: dict, limit: int = 10) -> dict:
    pts = stats.get("spawnPoints") or []
    top = []
    for p in pts[:limit]:
        m = re.search(r"(\d+)$", p.get("id", ""))
        top.append({"id": p.get("id"), "label": m.group(1) if m else p.get("id"), "count": p.get("count", 0),
                    "x": p.get("x"), "y": p.get("y"), "z": p.get("z")})
    return {"total": len(pts), "cold": sum(1 for p in pts if p.get("count", 0) <= 2), "top": top}


def spawn_map(stats: dict, size: int = 100, padding: int = 8) -> list[dict]:
    """x/z projection into a size x size box (arbiSpawnMap.ts) for an SVG dot map."""
    pts = [p for p in (stats.get("spawnPoints") or [])
           if isinstance(p.get("x"), (int, float)) and isinstance(p.get("z"), (int, float))]
    if not pts:
        return []
    xs = [p["x"] for p in pts]
    zs = [p["z"] for p in pts]
    span = max(max(xs) - min(xs), max(zs) - min(zs)) or 1
    sc = (size - padding * 2) / span
    cmax = max(p["count"] for p in pts) or 1
    cmin = min(p["count"] for p in pts)
    out = []
    for p in pts:
        hue = 120.0 if cmax == cmin else (p["count"] - cmin) / (cmax - cmin) * 120
        out.append({"cx": round(padding + (p["x"] - min(xs)) * sc, 2), "cy": round(padding + (p["z"] - min(zs)) * sc, 2),
                    "r": round(max(1.4, 4.5 * (p["count"] / cmax) ** 0.5), 2), "hue": round(hue, 1),
                    "count": p["count"]})
    return out


def analyze(run: dict, all_runs: list[dict]) -> dict:
    """Everything the run detail view shows, from one full record."""
    stats = run.get("stats") or None
    duration = run.get("durationSec") or 0
    out: dict = {
        "typeLabel": type_label(run.get("missionType"), run.get("missionTypeRaw")),
        "dronesPerMin": per_min(run.get("drones") or 0, duration),
        "enemiesPerMin": per_min(run.get("totalEnemies") or 0, duration),
        "dronesPerRotation": (run.get("drones") or 0) / run["rotations"] if run.get("rotations") else None,
        "hasStats": bool(stats),
    }
    if not stats:
        return out
    mean, std = stats.get("expectedVitusMean") or 0, stats.get("expectedVitusStd") or 0
    actual = run.get("vitusActual")
    out.update({
        "scenarios": scenario_table(mean, std),
        "luck": luck(actual, mean, std) if actual is not None else None,
        "vitusPerMinActual": per_min(actual, duration) if actual is not None else None,
        "expectedVitusPerMin": per_min(mean, duration),
        "saturationAbovePct": saturation_above_pct(stats.get("saturationBuckets") or []),
        "saturationBuckets": stats.get("saturationBuckets") or [],
        "rotations": [
            {**cell, "drones": d, "dpm": dpm}
            for cell, d, dpm in zip(rotation_clear_cells(stats), drones_per_rotation(stats), dpm_series(stats))
        ],
        "waves": stats.get("waves"),
        "cadence": drone_cadence(stats),
        "spawns": spawn_top(stats),
        "spawnMap": spawn_map(stats),
        "pauseSec": sum(iv["end"] - iv["start"] for iv in stats.get("pauseIntervals") or []),
        "idleSec": sum(iv["end"] - iv["start"] for iv in stats.get("idleIntervals") or []),
    })
    peers = [r for r in all_runs if r.get("id") != run.get("id") and usable(r) and r.get("stats")
             and r.get("node") == run.get("node") and r.get("missionType") == run.get("missionType")]

    def metric(r: dict, key: str) -> float | None:
        d = r.get("durationSec") or 0
        if key == "dronesPerMin":
            return per_min(r.get("drones") or 0, d)
        return per_min((r.get("stats") or {}).get("expectedVitusMean") or 0, d)

    pb = {}
    for key in ("dronesPerMin", "expectedVitusPerMin"):
        v = metric(run, key)
        if v is not None:
            pb[key] = personal_best(v, [m for m in (metric(r, key) for r in peers) if m is not None])
    out["personalBest"] = pb
    return out


def public_run(run: dict) -> dict:
    """The record without its heavy arrays (they are summarised by ``analyze``)."""
    out = {k: v for k, v in run.items() if k != "stats"}
    stats = run.get("stats")
    if stats:
        out["stats"] = {k: v for k, v in stats.items()
                        if k not in ("droneTimestamps", "spawnPoints", "saturationBuckets")}
        out["stats"]["droneCount"] = len(stats.get("droneTimestamps") or [])
    else:
        out["stats"] = None
    return out
