"""Arbitration EE.log run parser and run math (pure, no I/O).

Port of WFHelper ``services/arbiRunParser.ts`` and ``config/shared/arbiMath.ts``
(docs/port/arbitration.md §2). Every regex is kept verbatim and the rules run in
the same order as ``feedLine``. JS ``line.match(re)`` searches anywhere, so the
Python side uses ``re.search``. Output dicts use WFHelper's camelCase keys so a
record written here has the same shape as an ``arbi-runs.json`` entry.
"""

from __future__ import annotations

import math
import re
from typing import Any

# ---- regex table (arbiRunParser.ts:16-78) -----------------------------------

EE_LOG_LINE_TS = re.compile(r"^[^\d]*(\d+\.\d+)")
MISSION_NAME = re.compile(r"Script \[Info\]: ThemedSquadOverlay\.lua: Mission name: (.*)")
SPAM_SKIP = re.compile(r"Game \[Warning\]:|DamagePct")
AGENT_CREATED = re.compile(r"OnAgentCreated")
AGENT_FULL = re.compile(r"OnAgentCreated.*?/Npc/(.+?)(\d+)\s+.*?MonitoredTicking\s+(\d+)")
AGENT_NPC_NAME = re.compile(r"/Npc/([A-Za-z0-9_]+)")
AGENT_EXCLUDE = re.compile(r"(Replicant|RJCrew|petavatar|VoidClone|Turret|Dropship|CatbrowPetAgent|AllyAgent)", re.I)
DRONE = re.compile(r"OnAgentCreated.*?CorpusEliteShieldDroneAgent")
DEFENSE_REWARD = re.compile(r"Sys \[Info\]: Created /Lotus/Interface/DefenseReward\.swf")
SURVIVAL_REWARD = re.compile(r"Sys \[Info\]: Created /Lotus/Interface/SurvivalReward\.swf")
MONITORED_TICKING = re.compile(r"AI \[Info\]: .*?MonitoredTicking (\d+)")
WAVE_START_UNPAUSE = re.compile(r"WaveDefend\.lua: Starting wave (\d+)")
DEFENSE_WAVE = re.compile(r"WaveDefend\.lua: Defense wave: (\d+)")
SLEEP_BETWEEN = re.compile(r"WaveDefend\.lua: _SleepBetweenWaves")
SLEEP_BETWEEN_3 = re.compile(r"WaveDefend\.lua: _SleepBetweenWaves\(3\)")
SPAWN_POINT = re.compile(
    r"WaveDefend\.lua: Spawned a .+? spawn point: (\S+) @ Vector\((-?[\d.]+), (-?[\d.]+), (-?[\d.]+)\)")
SPAWN_POINT_HINT = "spawn point:"
WAVE_COUNTDOWN = re.compile(r"/Lotus/Interface/ProjectionsCountdown\.swf")
TERRITORY = re.compile(r"Script \[Info\]: TerritoryMission\.lua")
TERRITORY_START = re.compile(r"TerritoryMission\.lua: .*(control|captured)", re.I)
PENDING_SECTOR_PLAIN = re.compile(r"(?:ThemedSquadOverlay\.lua: Pending mission:|MapRedux\.lua: Confirm sector) (\S+)")
PENDING_SECTOR_JSON = re.compile(r'Set squad mission.*?"name":"([^"]+)"')
ELITE_SECTOR = re.compile(r"^(SolNode\d+)_EliteAlert$")
CLIENT_MISSION_JOIN = re.compile(r'Client (?:joining mission in-progress|loaded)[^{]*\{"name":"([^"]+)"\}')
CACHED_MISSION_NAME = re.compile(r"ThemedSquadOverlay\.lua: Cached mission name=(.+) \((SolNode\d+)\)")
SYNC_CONSUMABLES = re.compile(r"SyncAutoPopulatedConsumables for mission (MT_[A-Z_]+) with location (\S+)")
STATE_STARTED = re.compile(r"Game \[Info\]: OnStateStarted, mission type=(MT_[A-Z_]+)")
LOADOUT_LOADED = re.compile(r"Game \[Info\]: (.+?) loadout loader finished")
ABORT_CONFIRMED = re.compile(r"TopMenu\.lua: Abort:")
EOM_COMMIT = re.compile(r"Sys \[Info\]: EOM missionLocationUnlocked=")
# JS "$" has no multiline/newline slack; lines arrive without \r\n, \Z matches that.
DISRUPTION_ROUND_START = re.compile(r"SentientArtifactMission\.lua: Disruption: State change: ARTIFACT_ROUND\Z")
DISRUPTION_ROUND_DONE = re.compile(r"SentientArtifactMission\.lua: Disruption: State change: ARTIFACT_ROUND_DONE")

# ---- constants ----------------------------------------------------------------

MAX_SPAWN_POINTS = 300
FORCED_VALID_AGENTS = frozenset({"CorpusEliteShieldDroneAgent"})
REWARD_DEBOUNCE_SEC = 30
MIRROR_DEFENSE_NODES = ("munio", "tyana")
DISRUPTION_CONDUITS_PER_ROUND = 4
SATURATION_BUCKET_WIDTH = 3
SATURATION_MAX_COUNT = 30
SATURATION_MAX_SEGMENT_SEC = 29
ARBI_EARLY_WAVE_CAP = 15
ARBI_SPAWN_DATA_VERSION = 2
ARBI_SATURATION_THRESHOLD = 15
FULL_STATS_TYPES = ("defense", "interception", "disruption")

# ---- math (config/shared/arbiMath.ts) ------------------------------------------

VITUS_DROP_CHANCE = 0.15
VITUS_RETRIEVER_CHANCE = 0.18
ROTATION_VITUS_CHANCE = 0.1

SCENARIOS = (
    (-2.326, "99%", "worstCase"),
    (-1.282, "90%", "unlucky"),
    (-0.674, "75%", "belowAvg"),
    (0.0, "50%", "average"),
    (0.674, "25%", "aboveAvg"),
    (1.282, "10%", "highRoll"),
    (2.326, "1%", "godRoll"),
)
SCENARIO_LABELS = {
    "worstCase": "Worst Case", "unlucky": "Unlucky", "belowAvg": "Below Avg", "average": "Average",
    "aboveAvg": "Above Avg", "highRoll": "High Roll", "godRoll": "God Roll",
}
LUCK_COLORS = {
    "godRoll": "#ffd700", "highRoll": "#00e676", "aboveAvg": "#b2ff59", "average": "#cccccc",
    "belowAvg": "#ffcc80", "unlucky": "#ff9100", "worstCase": "#ff5252",
}
TYPE_LABELS = {"defense": "Defense", "interception": "Interception", "disruption": "Disruption", "other": "Other"}
RAW_TYPE_LABELS = {"MT_SURVIVAL": "Survival", "MT_EXCAVATE": "Excavation", "MT_EVACUATION": "Defection",
                   "MT_PURIFY": "Infested Salvage", "MT_ALCHEMY": "Alchemy"}


def js_round(v: float) -> int:
    """Math.round: half rounds up (Python's round() is half-to-even)."""
    return int(math.floor(v + 0.5))


def round1(v: float) -> float:
    return math.floor(v * 10 + 0.5) / 10


_JS_FLOAT = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _parse_float_js(s: str) -> float | None:
    """JS parseFloat(...) then round1: '1.2.3' -> 1.2; unparseable ('.') -> None (NaN, JSON null in WFHelper)."""
    m = _JS_FLOAT.match(s.strip())
    if not m:
        return None
    v = float(m.group(0))
    return round1(v) if math.isfinite(v) else None


def compute_vitus_model(rotations: float, waves_per_rotation: float, drones: float) -> dict:
    """``{mean, std}`` of the vitus essence a run should have produced (svesk.github.io/arbi)."""
    mean_val = 4 * VITUS_RETRIEVER_CHANCE + 2 * (1 - VITUS_RETRIEVER_CHANCE)
    expect_val_sq = 16 * VITUS_RETRIEVER_CHANCE + 4 * (1 - VITUS_RETRIEVER_CHANCE)
    var_val = expect_val_sq - mean_val * mean_val
    rot_mean = rotations + rotations * ROTATION_VITUS_CHANCE * waves_per_rotation
    rot_var = rotations * ROTATION_VITUS_CHANCE * (1 - ROTATION_VITUS_CHANCE) * waves_per_rotation ** 2
    mean_drops = drones * VITUS_DROP_CHANCE
    var_drops = drones * VITUS_DROP_CHANCE * (1 - VITUS_DROP_CHANCE)
    mean = rot_mean + mean_drops * mean_val
    variance = rot_var + mean_drops * var_val + mean_val * mean_val * var_drops
    return {"mean": mean, "std": math.sqrt(max(0.0, variance))}


def scenario_table(mean: float, std: float) -> list[dict]:
    """Totals reached with 99% / 90% / ... / 1% probability."""
    return [{"prob": prob, "key": key, "label": SCENARIO_LABELS[key], "total": max(0, js_round(mean + z * std))}
            for z, prob, key in SCENARIOS]


def norm_cdf(x: float, mean: float, std: float) -> float:
    """Abramowitz-Stegun normal CDF (same constants as WFHelper)."""
    if std <= 0:
        return 1.0 if x >= mean else 0.0
    z = (x - mean) / std
    t = 1 / (1 + 0.2316419 * abs(z))
    d = 0.3989423 * math.exp(-z * z / 2)
    p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
    return 1 - p if z > 0 else p


def luck(actual: float | None, mean: float, std: float) -> dict | None:
    """Luck label for an entered vitus count (ArbiVitusPanel.svelte)."""
    if actual is None or std <= 0:
        return None
    p = norm_cdf(actual, mean, std)
    if p >= 0.99:
        key = "godRoll"
    elif p >= 0.9:
        key = "highRoll"
    elif p >= 0.75:
        key = "aboveAvg"
    elif p > 0.25:
        key = "average"
    elif p > 0.1:
        key = "belowAvg"
    elif p > 0.01:
        key = "unlucky"
    else:
        key = "worstCase"
    level = SCENARIO_LABELS[key]
    text = f"{level} (Top {100 - p * 100:.1f}%)" if p > 0.5 else f"{level} (Bottom {p * 100:.1f}%)"
    return {"key": key, "label": level, "percentile": p, "text": text, "color": LUCK_COLORS[key]}


def merge_intervals(items: list[dict], bounds: dict | None = None) -> list[dict]:
    """Clamp to bounds, drop empty windows, sort, merge overlapping and touching ones."""
    lo = bounds["start"] if bounds else -math.inf
    hi = bounds["end"] if bounds else math.inf
    clamped = sorted(({"start": max(iv["start"], lo), "end": min(iv["end"], hi)} for iv in items),
                     key=lambda iv: iv["start"])
    out: list[dict] = []
    for iv in clamped:
        if iv["end"] <= iv["start"]:
            continue
        if out and iv["start"] <= out[-1]["end"]:
            out[-1]["end"] = max(out[-1]["end"], iv["end"])
        else:
            out.append(dict(iv))
    return out


def count_from_wave(point: dict, min_wave: int) -> int:
    """Spawns a point produced from ``min_wave`` on (arbiTypes.ts countFromWave)."""
    early = point.get("early")
    if not early or min_wave <= 1:
        return point["count"]
    last = min(min_wave - 1, ARBI_EARLY_WAVE_CAP)
    cut = sum(early[w - 1] if w - 1 < len(early) else 0 for w in range(1, last + 1))
    return max(0, point["count"] - cut)


def type_label(mission_type: str | None, raw: str | None = None) -> str:
    """'Defense' etc.; 'other' shows the engine type (arbiChartData.ts)."""
    if mission_type and mission_type != "other":
        return TYPE_LABELS.get(mission_type, mission_type.title())
    if raw:
        return RAW_TYPE_LABELS.get(raw, raw[3:] if raw.startswith("MT_") else raw)
    return "Other"


# ---- parser ---------------------------------------------------------------------

def classify_mission(name: str) -> tuple[str, str, int]:
    node = name.replace("Arbitration:", "", 1)        # JS String.replace: first occurrence only
    node = re.sub(r"\s+-\s+Arbitration$", "", node).strip()
    lower = node.lower()
    mirror = any(m in lower for m in MIRROR_DEFENSE_NODES)
    if "defense" in lower or mirror:
        mtype = "defense"
    elif "interception" in lower:
        mtype = "interception"
    else:
        mtype = "other"
    return node, mtype, 2 if mirror else 3


def mission_type_from_raw(mt: str) -> str:
    return {"MT_DEFENSE": "defense", "MT_TERRITORY": "interception", "MT_ARTIFACT": "disruption"}.get(mt, "other")


class _Run:
    __slots__ = ("mission_name", "node", "mission_type", "mission_type_raw", "sol_node", "waves_per_rotation",
                 "run_start_sec", "mission_start_sec", "run_end_sec", "last_activity_sec", "event_count",
                 "host_telemetry", "players", "rotations", "last_reward_sec", "reward_ts", "drone_ts",
                 "spawn_events", "tick_samples", "pause_intervals", "current_pause_start", "spawn_points",
                 "current_wave", "wave_starts", "wave_ends", "wave_countdowns", "round_starts", "round_ends",
                 "precise_start_sec")

    def __init__(self, name: str, node: str, mtype: str, wpr: int, sol_node: str | None, ts: float):
        self.mission_name, self.node, self.mission_type = name, node, mtype
        self.mission_type_raw: str | None = None
        self.sol_node = sol_node
        self.waves_per_rotation = wpr
        self.run_start_sec = ts
        self.mission_start_sec: float | None = None
        self.run_end_sec: float | None = None
        self.last_activity_sec = ts
        self.event_count = 0
        self.host_telemetry = False
        self.players: list[str] = []
        self.rotations = 0
        self.last_reward_sec = 0.0
        self.reward_ts: list[float] = []
        self.drone_ts: list[float] = []
        self.spawn_events: list[tuple[str | None, int | None]] = []
        self.tick_samples: list[tuple[float, int]] = []
        self.pause_intervals: list[dict] = []
        self.current_pause_start: float | None = None
        self.spawn_points: dict[str, dict] = {}
        self.current_wave = 1
        self.wave_starts: dict[int, float] = {}
        self.wave_ends: list[float] = []
        self.wave_countdowns: list[float] = []
        self.round_starts: list[float] = []
        self.round_ends: list[float] = []
        self.precise_start_sec: float | None = None

    def apply_raw(self, mt: str) -> None:
        if self.mission_type_raw is not None:
            return
        self.mission_type_raw = mt
        self.mission_type = mission_type_from_raw(mt)
        if self.mission_type == "disruption":
            self.waves_per_rotation = DISRUPTION_CONDUITS_PER_ROUND


def _bump(r: _Run, ts: float) -> None:
    r.last_activity_sec = max(r.last_activity_sec, ts)


class ArbiParser:
    """Feeds EE.log lines; emits run-start / run-end events; ``finalize()`` builds the parsed run."""

    def __init__(self) -> None:
        self._run: _Run | None = None
        self.pending_sector: str | None = None
        self.cached_mission: tuple[str, str] | None = None

    # -- interface ------------------------------------------------------------
    def is_run_active(self) -> bool:
        return self._run is not None

    def last_activity_sec(self) -> float:
        return self._run.last_activity_sec if self._run else 0

    def activity_count(self) -> int:
        return self._run.event_count if self._run else 0

    def reset(self) -> None:
        self._run = None
        self.pending_sector = None
        self.cached_mission = None

    def current(self) -> dict | None:
        """Live view of the active run (QAM 'in progress' line)."""
        r = self._run
        if not r:
            return None
        return {"missionName": r.mission_name, "node": r.node, "missionType": r.mission_type,
                "missionTypeRaw": r.mission_type_raw, "solNode": r.sol_node, "rotations": r.rotations,
                "drones": len(r.drone_ts), "hostTelemetry": r.host_telemetry,
                "runStartSec": r.run_start_sec, "lastActivitySec": r.last_activity_sec}

    # -- feeding ----------------------------------------------------------------
    def _start_run(self, name: str, ts: float) -> dict:
        node, mtype, wpr = classify_mission(name)
        sector = ELITE_SECTOR.search(self.pending_sector) if self.pending_sector is not None else None
        self.pending_sector = None   # consumed: a stale _EliteAlert must not mark a later mission
        self._run = _Run(name, node, mtype, wpr, sector.group(1) if sector else None, ts)
        return {"type": "run-start", "missionName": name, "node": node, "missionType": mtype, "gameTimeSec": ts}

    def feed_line(self, line: str) -> dict | None:
        if not line or SPAM_SKIP.search(line):
            return None
        m = EE_LOG_LINE_TS.search(line)
        ts = float(m.group(1)) if m else 0.0

        sector = PENDING_SECTOR_PLAIN.search(line) or PENDING_SECTOR_JSON.search(line)
        if sector:
            self.pending_sector = sector.group(1)
        cached = CACHED_MISSION_NAME.search(line)
        if cached:
            self.cached_mission = (cached.group(1).strip(), cached.group(2))

        r = self._run
        if r is None:
            join = CLIENT_MISSION_JOIN.search(line)
            if join:
                joined = ELITE_SECTOR.search(join.group(1))
                if joined:
                    self.pending_sector = join.group(1)
                    cm = self.cached_mission
                    name = cm[0] if cm and cm[1] == joined.group(1) else joined.group(1)
                    return self._start_run(name, ts)

        mission = MISSION_NAME.search(line)
        if mission:
            name = mission.group(1).strip()
            is_arbi = "Arbitration" in name or (
                self.pending_sector is not None and ELITE_SECTOR.search(self.pending_sector) is not None)
            if r is None:
                return self._start_run(name, ts) if is_arbi else None
            # Host-migration replay guard: an older repeat of the mission name is not a new run.
            if is_arbi and ts > 0 and r.last_activity_sec > 0 and ts < r.last_activity_sec:
                return None
            if ts > 0:
                r.run_end_sec = ts
            return {"type": "run-end", "reason": "new-mission"}

        if r is None:
            return None

        if ABORT_CONFIRMED.search(line):
            if ts > 0:
                r.run_end_sec = ts
            return {"type": "run-end", "reason": "aborted"}
        if EOM_COMMIT.search(line):
            if ts > 0:
                r.run_end_sec = ts
            return {"type": "run-end", "reason": "mission-end"}

        lo = LOADOUT_LOADED.search(line)
        if lo:
            player = lo.group(1).strip()
            if player and player not in r.players:
                r.players.append(player)

        sync = SYNC_CONSUMABLES.search(line)
        if sync:
            r.apply_raw(sync.group(1))
            if r.sol_node is None:
                r.sol_node = sync.group(2)
        st = STATE_STARTED.search(line)
        if st:
            r.apply_raw(st.group(1))
            if r.mission_start_sec is None and ts > 0:
                r.mission_start_sec = ts

        is_defense_reward = DEFENSE_REWARD.search(line) is not None
        if SLEEP_BETWEEN.search(line) or is_defense_reward:
            r.event_count += 1
            if r.current_pause_start is None and ts > 0:
                r.current_pause_start = ts
        unpause = False
        if WAVE_START_UNPAUSE.search(line):
            r.event_count += 1
            if r.mission_type == "defense":
                unpause = True
        if TERRITORY.search(line):
            r.event_count += 1
            if r.mission_type_raw is None and r.mission_type == "other":
                r.mission_type = "interception"
                r.waves_per_rotation = 3
            unpause = True
        if unpause and r.current_pause_start is not None and ts > 0:
            r.pause_intervals.append({"start": r.current_pause_start, "end": ts})
            r.current_pause_start = None

        if SPAWN_POINT_HINT in line:
            sp = SPAWN_POINT.search(line)
            if sp:
                self._record_spawn_point(r, sp)

        dw = DEFENSE_WAVE.search(line)
        if dw:
            r.event_count += 1
            if r.mission_type_raw is None:
                r.mission_type = "defense"
            wave = int(dw.group(1))
            if wave > 0:
                r.current_wave = wave
            if ts > 0:
                r.wave_starts[wave] = ts
                _bump(r, ts)
                if r.precise_start_sec is None and wave == 1:
                    r.precise_start_sec = ts
        elif r.precise_start_sec is None and ts > 0 and TERRITORY_START.search(line):
            r.precise_start_sec = ts

        if r.mission_type == "disruption" and ts > 0:
            if DISRUPTION_ROUND_DONE.search(line):
                r.event_count += 1
                r.rotations += 1
                r.round_ends.append(ts)
                r.reward_ts.append(ts)
                _bump(r, ts)
                if r.current_pause_start is None:
                    r.current_pause_start = ts
            elif DISRUPTION_ROUND_START.search(line):
                r.event_count += 1
                r.round_starts.append(ts)
                _bump(r, ts)
                if r.precise_start_sec is None:
                    r.precise_start_sec = ts
                if r.current_pause_start is not None:
                    r.pause_intervals.append({"start": r.current_pause_start, "end": ts})
                    r.current_pause_start = None

        survival = r.mission_type_raw == "MT_SURVIVAL" and SURVIVAL_REWARD.search(line) is not None
        if survival:
            r.event_count += 1
        if survival or is_defense_reward:
            if ts - r.last_reward_sec > REWARD_DEBOUNCE_SEC:
                r.rotations += 1
                r.last_reward_sec = ts
                r.reward_ts.append(ts)
                _bump(r, ts)
                if r.current_pause_start is None:
                    r.current_pause_start = ts

        if r.mission_type in FULL_STATS_TYPES and ts > 0:
            mt = MONITORED_TICKING.search(line)
            if mt:
                r.event_count += 1
                r.tick_samples.append((ts, int(mt.group(1))))

        if DRONE.search(line):
            r.event_count += 1
            r.host_telemetry = True
            if ts > 0:
                r.drone_ts.append(ts)
                _bump(r, ts)
        elif AGENT_CREATED.search(line):
            r.event_count += 1
            r.host_telemetry = True
            if not AGENT_EXCLUDE.search(line):
                full = AGENT_FULL.search(line)
                if full:
                    r.spawn_events.append((full.group(1), int(full.group(3))))
                else:
                    npc = AGENT_NPC_NAME.search(line)
                    r.spawn_events.append((npc.group(1) if npc else None, None))

        if ts > 0 and SLEEP_BETWEEN_3.search(line):
            r.wave_ends.append(ts)
        if ts > 0 and WAVE_COUNTDOWN.search(line):
            r.event_count += 1
            r.wave_countdowns.append(ts)
        return None

    @staticmethod
    def _record_spawn_point(r: _Run, m: re.Match) -> None:
        pid = m.group(1)
        tally = r.spawn_points.get(pid)
        if tally is None:
            if len(r.spawn_points) >= MAX_SPAWN_POINTS:
                return
            x, y, z = (_parse_float_js(m.group(i)) for i in (2, 3, 4))
            tally = {"x": x, "y": y, "z": z, "count": 0, "early": [0] * ARBI_EARLY_WAVE_CAP}
            r.spawn_points[pid] = tally
        tally["count"] += 1
        if 1 <= r.current_wave <= ARBI_EARLY_WAVE_CAP:
            tally["early"][r.current_wave - 1] += 1

    # -- finalize ---------------------------------------------------------------
    def finalize(self) -> dict | None:
        r = self._run
        if r is None:
            return None
        self._run = None
        drones = len(r.drone_ts)
        total_enemies = _count_valid_enemies(r.spawn_events) + drones
        start = r.precise_start_sec
        if start is None:
            start = r.drone_ts[0] if r.drone_ts else (
                r.mission_start_sec if r.mission_start_sec is not None else r.run_start_sec)
        duration = max(0.0, r.last_activity_sec - start)
        if duration < 1 and r.run_end_sec is not None:
            duration = max(0.0, r.run_end_sec - start)

        stats = None
        if r.mission_type in FULL_STATS_TYPES:
            avg_iv = None
            if drones > 1:
                avg_iv = sum(r.drone_ts[i] - r.drone_ts[i - 1] for i in range(1, drones)) / (drones - 1)
            model = compute_vitus_model(r.rotations, r.waves_per_rotation, drones)
            pauses = list(r.pause_intervals)
            if r.current_pause_start is not None:
                pauses.append({"start": r.current_pause_start, "end": r.last_activity_sec})
            waves = _build_waves(r) if r.mission_type == "defense" else (
                _build_rounds(r) if r.mission_type == "disruption" else None)
            stats = {
                "killsPerDrone": total_enemies / drones if drones > 0 else 0,
                "avgDroneIntervalSec": avg_iv,
                "expectedVitusMean": model["mean"],
                "expectedVitusStd": model["std"],
                "vitusPerMin": model["mean"] / (duration / 60) if duration > 0 else 0,
                "wavesPerRotation": r.waves_per_rotation,
                "droneTimestamps": list(r.drone_ts),
                "rewardTimestamps": list(r.reward_ts),
                "preciseStartSec": r.precise_start_sec,
                "lastActivitySec": r.last_activity_sec,
                "saturationBuckets": _build_saturation(r, start, r.last_activity_sec),
                "waves": waves,
                "pauseIntervals": merge_intervals(pauses, {"start": start, "end": r.last_activity_sec}),
                "idleIntervals": _build_idle(r, start, r.last_activity_sec),
                "rotationSaturationPct": _build_rotation_saturation(r, start),
                "spawnPoints": _build_spawn_points(r),
                "spawnDataVersion": ARBI_SPAWN_DATA_VERSION,
            }
        return {
            "missionName": r.mission_name, "node": r.node, "missionType": r.mission_type,
            "missionTypeRaw": r.mission_type_raw, "solNode": r.sol_node, "runStartSec": r.run_start_sec,
            "runEndSec": r.run_end_sec, "lastActivitySec": r.last_activity_sec, "durationSec": duration,
            "rotations": r.rotations, "drones": drones, "totalEnemies": total_enemies,
            "hostTelemetry": r.host_telemetry, "players": list(r.players), "stats": stats,
        }


def _count_valid_enemies(spawns: list[tuple[str | None, int | None]]) -> int:
    named = [s for s in spawns if s[0] is not None]
    confirmed: set[str] = set()
    suspected: set[str] = set()
    for prev, cur in zip(named, named[1:]):
        if prev[1] is not None and cur[1] is not None and prev[0]:
            (confirmed if cur[1] > prev[1] else suspected).add(prev[0])
    non_ticking = suspected - confirmed - FORCED_VALID_AGENTS
    return sum(1 for name, _ in spawns if not name or name not in non_ticking)


def _segments(r: _Run, start: float, end: float):
    samples = r.tick_samples
    for (t0, val), (t1, _) in zip(samples, samples[1:]):
        seg_start = max(t0, start)
        seg_end = min(t1, end)
        dur = seg_end - seg_start
        if dur <= 0 or dur > SATURATION_MAX_SEGMENT_SEC:
            continue
        if any((seg_start < p["start"] and seg_end > p["end"]) or (p["start"] <= seg_start < p["end"])
               for p in r.pause_intervals):
            continue
        yield val, dur


def _saturation_pct_in(r: _Run, start: float, end: float) -> float:
    total = above = 0.0
    for val, dur in _segments(r, start, end):
        total += dur
        if val >= ARBI_SATURATION_THRESHOLD:
            above += dur
    return above / total * 100 if total > 0 else 0


def _build_rotation_saturation(r: _Run, start: float) -> list[float]:
    out = []
    win = start
    for end in r.reward_ts:
        out.append(_saturation_pct_in(r, win, end))
        win = end
    return out


def _build_saturation(r: _Run, start: float, end: float) -> list[dict]:
    n = math.ceil(SATURATION_MAX_COUNT / SATURATION_BUCKET_WIDTH)
    seconds = [0.0] * n
    total = 0.0
    for val, dur in _segments(r, start, end):
        idx = min(val // SATURATION_BUCKET_WIDTH, n - 1)
        seconds[idx] += dur
        total += dur
    out = []
    for i, sec in enumerate(seconds):
        lo = i * SATURATION_BUCKET_WIDTH
        out.append({"minCount": lo, "label": f"{lo}+" if i == n - 1 else f"{lo}-{lo + SATURATION_BUCKET_WIDTH - 1}",
                    "seconds": sec, "pct": sec / total * 100 if total > 0 else 0})
    return out


def _build_idle(r: _Run, start: float, end: float) -> list[dict]:
    raw = [{"start": a[0], "end": b[0]} for a, b in zip(r.tick_samples, r.tick_samples[1:])
           if b[0] - a[0] > SATURATION_MAX_SEGMENT_SEC]
    return merge_intervals(raw, {"start": start, "end": end})


def _build_waves(r: _Run) -> list[dict]:
    out = []
    end_i = cd_i = 0
    for wave in sorted(r.wave_starts):
        start = r.wave_starts[wave]
        while end_i < len(r.wave_ends) and r.wave_ends[end_i] <= start:
            end_i += 1
        if wave % 3 == 0:
            # Every 3rd wave has no sleep line; the reward countdown fires ~5 s after the clear.
            while cd_i < len(r.wave_countdowns) and r.wave_countdowns[cd_i] <= start:
                cd_i += 1
            if cd_i >= len(r.wave_countdowns):
                continue
            dur = r.wave_countdowns[cd_i] - 5 - start
            cd_i += 1
        else:
            if end_i >= len(r.wave_ends):
                continue
            dur = r.wave_ends[end_i] - start
            end_i += 1
        out.append({"index": wave, "durationSec": dur, "saturationPct": _saturation_pct_in(r, start, start + dur)})
    return out


def _build_rounds(r: _Run) -> list[dict]:
    out = []
    end_i = 0
    for i, start in enumerate(r.round_starts):
        while end_i < len(r.round_ends) and r.round_ends[end_i] <= start:
            end_i += 1
        if end_i >= len(r.round_ends):
            break
        end = r.round_ends[end_i]
        out.append({"index": i + 1, "durationSec": end - start, "saturationPct": _saturation_pct_in(r, start, end)})
        end_i += 1
    return out


def _build_spawn_points(r: _Run) -> list[dict]:
    pts = []
    for pid, p in r.spawn_points.items():
        d: dict[str, Any] = {"id": pid, "x": p["x"], "y": p["y"], "z": p["z"], "count": p["count"]}
        if any(p["early"]):
            d["early"] = list(p["early"])
        pts.append(d)
    pts.sort(key=lambda d: (-d["count"], d["id"]))
    return pts


def parse_lines(lines) -> dict | None:
    """Feed a whole capture and finalize (salvage, backfill, tests)."""
    p = ArbiParser()
    for line in lines:
        p.feed_line(line.rstrip("\r\n"))
    return p.finalize()
