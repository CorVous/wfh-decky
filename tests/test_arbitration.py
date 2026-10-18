"""Arbitration tracker: parser golden tests (WFHelper arbiRunParser/arbiLogImporter
fixtures), math, run tracker, summary payload + overlay preview, RPCs."""

import asyncio
import datetime as dt
import gzip
import json
import math
import os
import time

import pytest
from PIL import Image, ImageDraw

from wfh.features import arbi_analysis as an
from wfh.features import arbi_overlay as ov
from wfh.features import arbi_parser as ap
from wfh.features import arbi_tracker as at

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "arbi")
from conftest import PREVIEWS_DIR as PREVIEWS


# ---- line builders (arbiRunParser.test.ts) --------------------------------------------

def mission(ts, name):
    return f"{ts:.3f} Script [Info]: ThemedSquadOverlay.lua: Mission name: {name}"


def drone(ts):
    return f"{ts:.3f} Sys [Info]: OnAgentCreated /Npc/CorpusEliteShieldDroneAgent7"


def enemy(ts, name, tick):
    return f"{ts:.3f} AI [Info]: OnAgentCreated /Npc/{name}3 pos MonitoredTicking {tick}"


def reward(ts):
    return f"{ts:.3f} Sys [Info]: Created /Lotus/Interface/DefenseReward.swf"


def tick(ts, n):
    return f"{ts:.3f} AI [Info]: NpcManager status MonitoredTicking {n}"


def wave(ts, n):
    return f"{ts:.3f} Script [Info]: WaveDefend.lua: Defense wave: {n}"


def wave_start(ts, n):
    return f"{ts:.3f} Script [Info]: WaveDefend.lua: Starting wave {n} (32 simultaneous)"


def sleep3(ts):
    return f"{ts:.3f} Script [Info]: WaveDefend.lua: _SleepBetweenWaves(3)"


def countdown(ts):
    return f"{ts:.3f} Sys [Info]: Created /Lotus/Interface/ProjectionsCountdown.swf"


def state(ts, s):
    return f"{ts:.3f} Script [Info]: SentientArtifactMission.lua: Disruption: State change: {s}"


def spawn_pt(ts, point, x, y, z):
    return (f"{ts:.3f} Script [Info]: WaveDefend.lua: Spawned a /Npc/ShieldLancer10 @ Vector(1, 2, 3), "
            f"spawn point: {point} @ Vector({x}, {y}, {z}), total spawned in current wave: 1")


def early(*counts):
    return [counts[i] if i < len(counts) else 0 for i in range(ap.ARBI_EARLY_WAVE_CAP)]


def run(lines):
    p = ap.ArbiParser()
    for line in lines:
        p.feed_line(line)
    return p.finalize()


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return ap.parse_lines(f.read().splitlines())


REAL = {
    "pendingElite": "158.074 Script [Info]: ThemedSquadOverlay.lua: Pending mission: SolNode167_EliteAlert",
    "missionNameSuffix": "178.428 Script [Info]: ThemedSquadOverlay.lua: Mission name: Oestrus (Eris) - Arbitration",
    "syncConsumables": "181.561 Sys [Info]: SyncAutoPopulatedConsumables for mission MT_PURIFY with location SolNode167",
    "stateStarted": "184.403 Game [Info]: OnStateStarted, mission type=MT_PURIFY",
    "abortDialog": "233.756 Script [Info]: Dialog.lua: Dialog::CreateOkCancel(description=/Lotus/Language/Menu/AbortMissionConfirm, title= leftItem=/Menu/Confirm_Item_Yes, rightItem=/Menu/Confirm_Item_No)",
    "abortConfirmed": "234.503 Script [Info]: TopMenu.lua: Abort: host/no session",
    "eomCommit": "234.503 Sys [Info]: EOM missionLocationUnlocked=1",
    "captureName": "255.746 Script [Info]: ThemedSquadOverlay.lua: Mission name: Isos (Eris)",
    "inMissionEndOfMatch": "432.123 Script [Info]: EndOfMatch.lua: Initialize",
    "inMissionSucceeded": "432.123 Script [Info]: EndOfMatch.lua: Mission Succeeded",
    "survivalReward": "735.449 Sys [Info]: Created /Lotus/Interface/SurvivalReward.swf",
}


# ---- run detection ---------------------------------------------------------------------

def test_starts_on_arbitration_mission_name():
    p = ap.ArbiParser()
    ev = p.feed_line(mission(100, "Arbitration: Casta Defense (Ceres)"))
    assert ev == {"type": "run-start", "missionName": "Arbitration: Casta Defense (Ceres)",
                  "node": "Casta Defense (Ceres)", "missionType": "defense", "gameTimeSec": 100}
    assert p.is_run_active()


def test_ignores_non_arbitration_when_idle():
    p = ap.ArbiParser()
    assert p.feed_line(mission(100, "Casta (Ceres)")) is None
    assert not p.is_run_active()


def test_classifies_interception_and_mirror_defense():
    p = ap.ArbiParser()
    assert p.feed_line(mission(1, "Arbitration: Berehynia Interception (Sedna)"))["missionType"] == "interception"
    p.reset()
    assert p.feed_line(mission(1, "Arbitration: Tyana Pass (Mars)"))["missionType"] == "defense"


def test_collects_squad_names_only_while_active():
    lo = lambda ts, n: f"{ts:.3f} Game [Info]: {n} loadout loader finished."  # noqa: E731
    r = run([lo(15, "OrbiterBoot"), mission(100, "Arbitration: Casta Defense (Ceres)"), lo(105, "HostPlayer"),
             lo(112, "ClientOne"), lo(300, "HostPlayer"), reward(400), reward(700)])
    assert r["players"] == ["HostPlayer", "ClientOne"]


def test_unknown_modes_other_with_null_stats():
    r = run([mission(100, "Arbitration: Olympus (Mars)"), reward(400), reward(700)])
    assert r["missionType"] == "other" and r["stats"] is None and r["rotations"] == 2


def test_wave_lines_upgrade_other_to_defense():
    r = run([mission(100, "Arbitration: Hydron (Sedna)"), wave(110, 1), drone(150), drone(200)])
    assert r["missionType"] == "defense" and r["stats"] is not None


def test_new_mission_ends_run_and_host_migration_guard():
    p = ap.ArbiParser()
    p.feed_line(mission(100, "Arbitration: Casta Defense (Ceres)"))
    assert p.feed_line(mission(900, "Cetus (Earth)")) == {"type": "run-end", "reason": "new-mission"}
    p = ap.ArbiParser()
    p.feed_line(mission(100, "Arbitration: Casta Defense (Ceres)"))
    p.feed_line(drone(500))
    assert p.feed_line(mission(300, "Arbitration: Casta Defense (Ceres)")) is None
    assert p.is_run_active()
    assert p.feed_line(mission(600, "Arbitration: Casta Defense (Ceres)")) == {"type": "run-end", "reason": "new-mission"}


# ---- counting ----------------------------------------------------------------------------

def test_drones_separate_from_enemies_and_excluded_agents():
    r = run([mission(100, "Arbitration: Casta Defense (Ceres)"), drone(110), drone(120),
             enemy(130, "GrineerLancerAgent", 5), enemy(131, "GrineerLancerAgent", 6),
             "132.000 AI [Info]: OnAgentCreated /Npc/TurretAgent3 MonitoredTicking 7"])
    assert r["drones"] == 2 and r["totalEnemies"] == 4


def test_non_ticking_agents_excluded():
    r = run([mission(100, "Arbitration: Casta Defense (Ceres)"), enemy(110, "DecorativeAgent", 5),
             enemy(111, "DecorativeAgent", 5), enemy(112, "DecorativeAgent", 5), enemy(113, "RealAgent", 5)])
    assert r["totalEnemies"] == 1


def test_spam_lines_skipped():
    r = run([mission(100, "Arbitration: Casta Defense (Ceres)"),
             "110.000 Game [Warning]: OnAgentCreated /Npc/GrineerLancerAgent3 MonitoredTicking 5",
             "111.000 AI [Info]: OnAgentCreated /Npc/LancerAgent3 DamagePct MonitoredTicking 6"])
    assert r["totalEnemies"] == 0


def test_reward_debounce():
    assert run([mission(100, "Arbitration: Casta Defense (Ceres)"), reward(400), reward(420)])["rotations"] == 1
    assert run([mission(100, "Arbitration: Casta Defense (Ceres)"), reward(400), reward(440)])["rotations"] == 2


# ---- stats -------------------------------------------------------------------------------

def test_duration_from_precise_start():
    r = run([mission(80, "Arbitration: Casta Defense (Ceres)"), wave(90, 1), drone(100), drone(130)])
    assert r["durationSec"] == 40
    assert r["stats"]["preciseStartSec"] == 90 and r["stats"]["avgDroneIntervalSec"] == 30


def test_duration_falls_back_to_first_drone():
    r = run([mission(80, "Arbitration: Berehynia Interception (Sedna)"), drone(100), drone(150)])
    assert r["durationSec"] == 50


def test_saturation_histogram():
    r = run([mission(80, "Arbitration: Casta Defense (Ceres)"), wave(90, 1), tick(100, 2), tick(110, 16),
             tick(120, 5), drone(130)])
    b = r["stats"]["saturationBuckets"]
    assert len(b) == 10 and b[0]["label"] == "0-2" and b[5]["label"] == "15-17" and b[9]["label"] == "27+"
    assert b[0]["pct"] == pytest.approx(50) and b[5]["pct"] == pytest.approx(50)


def test_paused_segments_excluded_from_saturation():
    r = run([mission(80, "Arbitration: Casta Defense (Ceres)"), wave(90, 1), tick(100, 2),
             "105.000 Script [Info]: WaveDefend.lua: _SleepBetweenWaves(10)", tick(110, 16), wave_start(115, 2),
             tick(120, 5), drone(130)])
    b = r["stats"]["saturationBuckets"]
    assert b[0]["pct"] == pytest.approx(100) and b[5]["pct"] == 0


def test_wave_durations_with_countdown_path():
    r = run([mission(80, "Arbitration: Casta Defense (Ceres)"), wave(100, 1), sleep3(130), wave(140, 2), sleep3(165),
             wave(170, 3), countdown(205), drone(210)])
    assert r["stats"]["waves"] == [{"index": 1, "durationSec": 30, "saturationPct": 0},
                                   {"index": 2, "durationSec": 25, "saturationPct": 0},
                                   {"index": 3, "durationSec": 30, "saturationPct": 0}]


def test_vitus_model_and_null_finalize():
    r = run([mission(80, "Arbitration: Casta Defense (Ceres)"), wave(90, 1), drone(100), reward(400)])
    assert r["stats"]["expectedVitusMean"] > 0 and r["stats"]["wavesPerRotation"] == 3
    assert ap.ArbiParser().finalize() is None


# ---- real EE.log lines -------------------------------------------------------------------

def test_suffix_name_format():
    assert ap.ArbiParser().feed_line(REAL["missionNameSuffix"]) == {
        "type": "run-start", "missionName": "Oestrus (Eris) - Arbitration", "node": "Oestrus (Eris)",
        "missionType": "other", "gameTimeSec": 178.428}


def test_elite_alert_sector_detection_and_consumption():
    p = ap.ArbiParser()
    p.feed_line(REAL["pendingElite"])
    assert p.feed_line(mission(178.428, "Oestrus (Eris)"))["type"] == "run-start"
    p.feed_line(REAL["abortConfirmed"])
    assert p.finalize()["solNode"] == "SolNode167"
    assert p.feed_line(REAL["captureName"]) is None and not p.is_run_active()


def test_abort_and_eom_end_markers():
    p = ap.ArbiParser()
    p.feed_line(REAL["missionNameSuffix"])
    assert p.feed_line(REAL["abortDialog"]) is None and p.is_run_active()
    assert p.feed_line(REAL["abortConfirmed"]) == {"type": "run-end", "reason": "aborted"}
    p = ap.ArbiParser()
    p.feed_line(REAL["missionNameSuffix"])
    assert p.feed_line(REAL["inMissionEndOfMatch"]) is None
    assert p.feed_line(REAL["inMissionSucceeded"]) is None
    assert p.feed_line(REAL["eomCommit"]) == {"type": "run-end", "reason": "mission-end"}


def test_survival_rewards_and_stray_survival_ui():
    r = run([mission(100, "Arbitration: Mot (Void)"),
             "110.000 Sys [Info]: SyncAutoPopulatedConsumables for mission MT_SURVIVAL with location SolNode409",
             REAL["survivalReward"], "1035.500 Sys [Info]: Created /Lotus/Interface/SurvivalReward.swf"])
    assert r["rotations"] == 2
    r = run([mission(99.488, "Rhea (Saturn) - Arbitration"), "103.240 Game [Info]: OnStateStarted, mission type=MT_TERRITORY",
             drone(104.75), "133.194 Script [Info]: TerritoryMission.lua: Alpha has is now under Enemy control",
             "331.811 Sys [Info]: Created /Lotus/Interface/SurvivalReward.swf", drone(337.236),
             "356.940 Sys [Info]: Created /Lotus/Interface/DefenseReward.swf"])
    assert r["rotations"] == 1 and r["stats"]["rewardTimestamps"] == [356.94]
    assert r["durationSec"] == pytest.approx(356.94 - 133.194, abs=1e-3)


def test_engine_type_and_end_marker_duration():
    r = run([REAL["missionNameSuffix"], REAL["syncConsumables"], REAL["stateStarted"]])
    assert (r["missionTypeRaw"], r["solNode"], r["missionType"], r["stats"]) == ("MT_PURIFY", "SolNode167", "other", None)
    p = ap.ArbiParser()
    for line in (REAL["missionNameSuffix"], REAL["stateStarted"], REAL["abortConfirmed"]):
        p.feed_line(line)
    r = p.finalize()
    assert r["durationSec"] == pytest.approx(50.1, abs=1e-3) and r["runEndSec"] == 234.503
    p = ap.ArbiParser()
    for line in (REAL["missionNameSuffix"], REAL["stateStarted"], drone(200.001), drone(200.002), drone(200.003),
                 REAL["abortConfirmed"]):
        p.feed_line(line)
    assert p.finalize()["durationSec"] == pytest.approx(34.502, abs=1e-3)


def test_engine_type_outranks_wave_heuristic_and_maps_types():
    assert run([mission(100, "Arbitration: Hydron (Sedna)"), REAL["syncConsumables"], wave(110, 1)])["missionType"] == "other"
    d = run([mission(100, "Arbitration: Olympus (Mars)"),
             "110.000 Sys [Info]: SyncAutoPopulatedConsumables for mission MT_DEFENSE with location SolNode30"])
    assert d["missionType"] == "defense" and d["stats"] is not None
    i = run([mission(100, "Arbitration: Olympus (Mars)"), "110.000 Game [Info]: OnStateStarted, mission type=MT_TERRITORY"])
    assert i["missionType"] == "interception"


def test_disruption_rounds():
    r = run([mission(100, "Apollo (Lua) - Arbitration"), "101.000 Game [Info]: OnStateStarted, mission type=MT_ARTIFACT",
             state(110, "ARTIFACT_ROUND"), drone(150), enemy(160, "ButcherAgent", 4),
             "200.000 Sys [Info]: Created /Lotus/Interface/SurvivalReward.swf",
             "260.000 Sys [Info]: Created /Lotus/Interface/SurvivalReward.swf",
             state(400, "ARTIFACT_ROUND_DONE"), state(401, "REWARDS (host)"), state(420, "INTERVAL"),
             state(440, "ARTIFACT_ROUND"), drone(500), state(700, "ARTIFACT_ROUND_DONE")])
    assert (r["missionType"], r["missionTypeRaw"], r["rotations"], r["hostTelemetry"]) == ("disruption", "MT_ARTIFACT", 2, True)
    assert r["stats"]["wavesPerRotation"] == 4
    assert r["stats"]["waves"] == [{"index": 1, "durationSec": 290, "saturationPct": 0},
                                   {"index": 2, "durationSec": 260, "saturationPct": 0}]
    assert r["stats"]["preciseStartSec"] == 110


def test_mid_mission_join():
    p = ap.ArbiParser()
    p.feed_line("2040.984 Script [Info]: ThemedSquadOverlay.lua: Cached mission name=Apollo (Lua) - Arbitration (SolNode308)")
    ev = p.feed_line('2047.451 Script [Info]: ThemedSquadOverlay.lua: LoadLevelMsg received. Client joining mission in-progress: {"name":"SolNode308_EliteAlert"}')
    assert ev == {"type": "run-start", "missionName": "Apollo (Lua) - Arbitration", "node": "Apollo (Lua)",
                  "missionType": "other", "gameTimeSec": 2047.451}
    assert p.feed_line('2051.361 Script [Info]: Client loaded {"name":"SolNode308_EliteAlert"} with MissionInfo:') is None
    p.feed_line(drone(2100))
    assert p.finalize()["solNode"] == "SolNode308"
    p = ap.ArbiParser()
    assert p.feed_line('100.000 Script [Info]: ThemedSquadOverlay.lua: Client loaded {"name":"SolNode308"} with MissionInfo:') is None
    assert not p.is_run_active()


def test_cadence_intervals():
    sl = lambda ts: f"{ts:.3f} Script [Info]: WaveDefend.lua: _SleepBetweenWaves"  # noqa: E731
    r = run([mission(100, "Arbitration: Casta Defense (Ceres)"), wave(110, 1), drone(120), drone(130), sl(140),
             wave_start(160, 2), drone(170), tick(200, 5), tick(210, 6), tick(300, 7), reward(400),
             wave_start(430, 4), drone(440), reward(700)])
    assert r["stats"]["pauseIntervals"] == [{"start": 140, "end": 160}, {"start": 400, "end": 430}]
    assert r["stats"]["idleIntervals"] == [{"start": 210, "end": 300}]
    r = run([mission(100, "Arbitration: Casta Defense (Ceres)"), wave(110, 1), drone(120), reward(400),
             wave_start(430, 4), drone(440), reward(700), drone(750)])
    assert r["stats"]["pauseIntervals"] == [{"start": 400, "end": 430}, {"start": 700, "end": 750}]


def test_spawn_points_rounding_and_order():
    r = run([mission(80, "Arbitration: Casta Defense (Ceres)"), wave(90, 1),
             spawn_pt(91, "/Layer1/Layer1/NpcSpawnPoint37", 50.25, 98.0789, 53),
             spawn_pt(92, "/Layer1/Layer1/NpcSpawnPoint37", 50.25, 98.0789, 53),
             spawn_pt(93, "/Layer1/Layer1/NpcSpawnPoint177", 117, 106.75, 29), drone(100)])
    assert r["stats"]["spawnPoints"] == [
        {"id": "/Layer1/Layer1/NpcSpawnPoint37", "x": 50.3, "y": 98.1, "z": 53, "count": 2, "early": early(2)},
        {"id": "/Layer1/Layer1/NpcSpawnPoint177", "x": 117, "y": 106.8, "z": 29, "count": 1, "early": early(1)}]
    assert r["stats"]["spawnDataVersion"] == 2


def test_spawn_points_parsefloat_fidelity():
    # JS parseFloat semantics: '1.2.3' -> 1.2, '.' -> NaN (null); the point is still created and counted.
    r = run([mission(80, "Arbitration: Casta Defense (Ceres)"), wave(90, 1),
             spawn_pt(91, "/Layer1/NpcSpawnPoint9", "1.2.3", ".", 5),
             spawn_pt(92, "/Layer1/NpcSpawnPoint9", "1.2.3", ".", 5), drone(100)])
    assert r["stats"]["spawnPoints"] == [
        {"id": "/Layer1/NpcSpawnPoint9", "x": 1.2, "y": None, "z": 5, "count": 2, "early": early(2)}]


def test_spawn_points_waves_and_caps():
    r = run([spawn_pt(10, "/Layer1/NpcSpawnPoint1", 1, 2, 3), mission(80, "Arbitration: Casta Defense (Ceres)"),
             wave(90, 1), spawn_pt(91, "/Layer1/NpcSpawnPoint2", 4, 5, 6), drone(100)])
    assert r["stats"]["spawnPoints"] == [{"id": "/Layer1/NpcSpawnPoint2", "x": 4, "y": 5, "z": 6, "count": 1, "early": early(1)}]
    r = run([mission(80, "Arbitration: Casta Defense (Ceres)"), spawn_pt(85, "/Layer1/NpcSpawnPoint1", 1, 2, 3),
             wave(90, 1), spawn_pt(91, "/Layer1/NpcSpawnPoint1", 1, 2, 3), wave(200, 7),
             spawn_pt(201, "/Layer1/NpcSpawnPoint1", 1, 2, 3), spawn_pt(202, "/Layer1/NpcSpawnPoint1", 1, 2, 3), drone(210)])
    pt = r["stats"]["spawnPoints"][0]
    assert pt == {"id": "/Layer1/NpcSpawnPoint1", "x": 1, "y": 2, "z": 3, "count": 4, "early": early(2, 0, 0, 0, 0, 0, 2)}
    assert [ap.count_from_wave(pt, w) for w in (1, 7, 8, ap.ARBI_EARLY_WAVE_CAP + 2)] == [4, 2, 0, 0]
    r = run([mission(80, "Arbitration: Casta Defense (Ceres)"), wave(90, 16), spawn_pt(91, "/Layer1/NpcSpawnPoint1", 1, 2, 3), drone(100)])
    pt = r["stats"]["spawnPoints"][0]
    assert pt == {"id": "/Layer1/NpcSpawnPoint1", "x": 1, "y": 2, "z": 3, "count": 1} and ap.count_from_wave(pt, 7) == 1
    lines = [mission(80, "Arbitration: Casta Defense (Ceres)"), wave(90, 1)]
    lines += [spawn_pt(100 + i, f"/Layer1/NpcSpawnPoint{i}", i, 0, i) for i in range(305)]
    lines += [spawn_pt(500, "/Layer1/NpcSpawnPoint0", 0, 0, 0), drone(510)]
    pts = run(lines)["stats"]["spawnPoints"]
    assert len(pts) == 300 and pts[0] == {"id": "/Layer1/NpcSpawnPoint0", "x": 0, "y": 0, "z": 0, "count": 2, "early": early(2)}
    assert not any(p["id"] == "/Layer1/NpcSpawnPoint300" for p in pts)


def test_window_saturation():
    r = run([mission(80, "Arbitration: Casta Defense (Ceres)"), wave(100, 1), tick(100, 20), tick(110, 2), tick(120, 2),
             sleep3(130), wave(140, 2), tick(140, 20), tick(150, 20), tick(160, 20), sleep3(165), drone(170)])
    w = r["stats"]["waves"]
    assert [x["durationSec"] for x in w] == [30, 25]
    assert w[0]["saturationPct"] == pytest.approx(100 / 3) and w[1]["saturationPct"] == pytest.approx(100)
    r = run([mission(80, "Arbitration: Casta Defense (Ceres)"), wave(100, 1), tick(100, 20), tick(110, 20), tick(120, 2),
             reward(130), wave_start(140, 2), tick(140, 20), tick(150, 2), tick(160, 2), reward(200), drone(210)])
    assert r["stats"]["rewardTimestamps"] == [130, 200]
    pct = r["stats"]["rotationSaturationPct"]
    assert pct[0] == pytest.approx(200 / 3) and pct[1] == pytest.approx(50)


# ---- recorded EE.log fixtures (arbiLogImporter.test.ts) -----------------------------------

def test_fixture_oestrus_abort():
    r = fixture("oestrus-abort-ee.txt")
    assert (r["node"], r["missionType"], r["missionTypeRaw"], r["solNode"]) == ("Oestrus (Eris)", "other", "MT_PURIFY", "SolNode167")
    assert (r["rotations"], r["stats"], r["drones"], r["totalEnemies"]) == (0, None, 0, 0)


def test_fixture_mot_survival():
    r = fixture("mot-survival-ee.txt")
    assert (r["node"], r["missionType"], r["missionTypeRaw"], r["solNode"]) == ("Mot (Void)", "other", "MT_SURVIVAL", "SolNode409")
    assert r["rotations"] == 1 and r["drones"] == 7
    assert r["durationSec"] == pytest.approx(301.35, abs=0.01)


def test_fixture_rhea_interception():
    r = fixture("rhea-interception-ee.txt")
    assert (r["node"], r["missionType"], r["missionTypeRaw"], r["solNode"]) == ("Rhea (Saturn)", "interception", "MT_TERRITORY", "SolNode18")
    assert r["rotations"] == 1 and r["drones"] == 5
    assert r["stats"]["rewardTimestamps"] == [356.94] and r["stats"]["preciseStartSec"] == 133.194
    assert r["durationSec"] == pytest.approx(223.746, abs=1e-3)


def test_fixture_stoefler_defense():
    r = fixture("stoefler-defense-ee.txt")
    assert (r["node"], r["missionType"], r["missionTypeRaw"], r["solNode"]) == ("Stöfler (Lua)", "defense", "MT_DEFENSE", "SolNode305")
    assert (r["rotations"], r["drones"], r["totalEnemies"]) == (2, 3, 8)
    s = r["stats"]
    assert s["wavesPerRotation"] == 3 and s["rewardTimestamps"] == [16855.934, 16936.816]
    assert s["preciseStartSec"] == 16786.896
    assert r["durationSec"] == pytest.approx(16936.816 - 16786.896, abs=1e-3)
    w = s["waves"]
    assert [x["index"] for x in w] == [1, 2, 3, 4, 5, 6]
    assert w[0]["durationSec"] == pytest.approx(16.151, abs=1e-3)
    assert w[1]["durationSec"] == pytest.approx(22.873, abs=1e-3)
    assert w[2]["durationSec"] == pytest.approx(16855.932 - 5 - 16831.973, abs=1e-3)
    assert w[3]["durationSec"] == pytest.approx(19.101, abs=1e-3)   # "!"-prefixed sleep line
    assert w[5]["durationSec"] == pytest.approx(16936.807 - 5 - 16910.236, abs=1e-3)


# ---- math ------------------------------------------------------------------------------------

def test_vitus_model_and_scenarios():
    m = ap.compute_vitus_model(10, 3, 100)
    assert m["mean"] == pytest.approx(48.4)
    assert m["std"] == pytest.approx(math.sqrt(8.1 + 15 * 0.5904 + 2.36 * 2.36 * 12.75))
    assert ap.compute_vitus_model(10, 2, 0)["mean"] == pytest.approx(12)
    assert ap.compute_vitus_model(0, 3, 0) == {"mean": 0, "std": 0}
    table = ap.scenario_table(m["mean"], m["std"])
    assert [r["prob"] for r in table] == ["99%", "90%", "75%", "50%", "25%", "10%", "1%"]
    assert table[3]["total"] == 48 and table[0]["total"] < table[6]["total"]
    assert ap.js_round(2.5) == 3 and ap.round1(0.25) == 0.3


def test_merge_intervals_and_luck():
    assert ap.merge_intervals([{"start": 30, "end": 40}, {"start": 0, "end": 10}, {"start": 10, "end": 20},
                               {"start": 5, "end": 5}, {"start": 35, "end": 60}]) == [
        {"start": 0, "end": 20}, {"start": 30, "end": 60}]
    assert ap.merge_intervals([{"start": -50, "end": 10}, {"start": 90, "end": 400}, {"start": 500, "end": 600}],
                              {"start": 0, "end": 300}) == [{"start": 0, "end": 10}, {"start": 90, "end": 300}]
    assert ap.norm_cdf(5, 5, 2) == pytest.approx(0.5, abs=1e-6)
    assert ap.luck(48.4, 48.4, 6)["key"] == "average"
    hi = ap.luck(70, 48.4, 6)
    assert hi["key"] == "godRoll" and hi["text"].startswith("God Roll (Top ")
    assert ap.luck(30, 48.4, 6)["text"].startswith("Worst Case (Bottom ")
    assert ap.luck(None, 1, 1) is None and ap.luck(1, 1, 0) is None


def test_annotations():
    assert at.normalize_tags([" boar  run ", "Boar Run", 3, "", "x" * 40] + [f"t{i}" for i in range(20)]) == \
        ["boar run", "x" * 32] + [f"t{i}" for i in range(10)]
    assert at.normalize_notes("a\r\nb\x00c\x7f\t ") == "a\nbc"
    assert len(at.normalize_notes("y" * 3000)) == 2000
    assert at.clean_vitus(None) is None and at.clean_vitus(12.6) == 13
    assert at.clean_vitus(-1) is False and at.clean_vitus("x") is False and at.clean_vitus(True) is False


# ---- tracker ---------------------------------------------------------------------------------

class Clock:
    def __init__(self, ms=1_783_193_415_000):
        self.ms = ms

    def __call__(self):
        return self.ms


def defense_run(t0=100.0, rotations=2, drones=4, end="eom", name="Arbitration: Casta Defense (Ceres)"):
    lines = [mission(t0, name), f"{t0 + 2:.3f} Sys [Info]: SyncAutoPopulatedConsumables for mission MT_DEFENSE with location SolNode149",
             wave(t0 + 10, 1)]
    t = t0 + 10
    for r in range(rotations):
        for _ in range(drones):
            t += 7
            lines.append(drone(t))
            lines.append(tick(t + 1, 12 + r * 4))
        t += 5
        lines.append(reward(t))
        t += 31
        lines.append(wave_start(t, 3 * (r + 1) + 1))
    if end == "eom":
        lines.append(f"{t + 5:.3f} Sys [Info]: EOM missionLocationUnlocked=1")
    elif end == "abort":
        lines.append(f"{t + 5:.3f} Script [Info]: TopMenu.lua: Abort: host/no session")
    return lines


@pytest.fixture
def tracker(tmp_path):
    t = at.ArbiRunTracker(str(tmp_path), now_ms=Clock())
    t.saved, t.skipped = [], []
    t.on_run_saved = t.saved.append
    t.on_run_skipped = t.skipped.append
    t.init()
    return t


def feed(t, lines):
    for line in lines:
        t.process_line(line)


def test_tracker_records_run_and_gz(tracker, tmp_path):
    feed(tracker, defense_run())
    assert len(tracker.runs) == 1 and tracker.saved
    rec = tracker.runs[0]
    assert rec["endReason"] == "mission-end" and rec["source"] == "live" and rec["rotations"] == 2
    assert rec["drones"] == 8 and rec["solNode"] == "SolNode149" and rec["vitusActual"] is None
    assert at.RUN_ID_RE.match(rec["id"]) and rec["logFile"] == f"{rec['id']}.log.gz" and rec["logSizeBytes"] > 0
    with gzip.open(tracker.run_log_path(rec["id"]), "rt") as f:
        captured = f.read().splitlines()
    assert captured[0].endswith("Arbitration: Casta Defense (Ceres)") and "EOM" not in captured[-1]
    idx = json.load(open(tmp_path / "arbi-runs.json"))
    assert idx["schemaVersion"] == 1 and idx["runs"][0]["id"] == rec["id"]
    assert not [f for f in os.listdir(tmp_path / "arbi-logs") if f.endswith(".partial.log")]
    # parsed start anchors the wall clock: capture started at the mission line
    assert rec["endedAt"] > rec["startedAt"]


def test_tracker_skips_client_and_short_runs(tracker):
    feed(tracker, [mission(100, "Arbitration: Casta Defense (Ceres)"), reward(400), reward(500), reward(600),
                   "700.000 Sys [Info]: EOM missionLocationUnlocked=1"])
    feed(tracker, defense_run(t0=1000, rotations=1))
    assert tracker.runs == []
    assert [s["reason"] for s in tracker.skipped] == ["client-side run, no AI data in the log", "1 rotation(s)"]
    assert tracker.last_skip["node"] == "Casta Defense (Ceres)"


def test_tracker_back_to_back_and_abort(tracker):
    lines = defense_run(end=None) + defense_run(t0=2000, end="abort", name="Arbitration: Hydron Defense (Sedna)")
    feed(tracker, lines)
    assert [r["endReason"] for r in tracker.runs] == ["aborted", "new-mission"]
    assert tracker.runs[0]["node"] == "Hydron Defense (Sedna)" and tracker.runs[0]["id"] != tracker.runs[1]["id"]


def test_tracker_log_reset_and_shutdown(tracker):
    feed(tracker, defense_run(end=None))
    tracker.process_line("0.127 Sys [Diag]: Current time: Tue Jul  7 15:40:49 2026 [UTC: Tue Jul  7 21:40:49 2026]")
    assert tracker.runs[0]["endReason"] == "log-truncated"
    feed(tracker, defense_run(t0=3000, end=None))
    assert tracker.active() is not None
    tracker.shutdown()
    assert tracker.runs[0]["endReason"] == "app-quit"


def test_tracker_continuation_lines_do_not_reset(tracker):
    # Multi-line EE.log entries (JSON/Lua dumps) carry bare numbers; they must not look like a restart.
    lines = defense_run(end=None)
    mid = len(lines) // 2
    lines = lines[:mid] + ['    "scale": 0.5,', "12.25", "  pos 3.141 0 0", "0.250 trailing"] + lines[mid:]
    feed(tracker, lines + ["2000.000 Sys [Info]: EOM missionLocationUnlocked=1"])
    assert [r["endReason"] for r in tracker.runs] == ["mission-end"]
    assert tracker.runs[0]["rotations"] == 2


def test_tracker_inactivity(tracker):
    clock = tracker._now
    feed(tracker, defense_run(end=None))
    clock.ms += 5 * 60_000
    assert not tracker.check_inactivity()
    feed(tracker, ["900.000 Sys [Info]: orbiter chatter"] * 3)   # non-combat lines do not keep it alive
    clock.ms += 6 * 60_000
    assert tracker.check_inactivity()
    assert tracker.runs[0]["endReason"] == "inactivity"


def test_tracker_disable_discards_capture(tracker, tmp_path):
    lines = defense_run(end=None)
    feed(tracker, lines + [tick(999, 3)] * 250)       # > FLUSH_EVERY_LINES: partial exists
    assert [f for f in os.listdir(tmp_path / "arbi-logs") if f.endswith(".partial.log")]
    tracker.set_tracking_enabled(False)
    assert tracker.active() is None
    assert not [f for f in os.listdir(tmp_path / "arbi-logs") if f.endswith(".partial.log")]
    feed(tracker, defense_run(t0=5000))
    assert tracker.runs == []
    tracker.set_tracking_enabled(True)
    feed(tracker, defense_run(t0=6000))
    assert len(tracker.runs) == 1


def test_tracker_salvage_partial(tmp_path):
    logs = tmp_path / "arbi-logs"
    logs.mkdir()
    (logs / "2026-07-04_21-30-15.partial.log").write_text("\n".join(defense_run(end=None)) + "\n")
    (logs / "2026-07-04_22-00-00.partial.log").write_text(mission(1, "Arbitration: Casta Defense (Ceres)") + "\n")
    t = at.ArbiRunTracker(str(tmp_path))
    t.init()
    assert len(t.runs) == 1 and t.runs[0]["endReason"] == "log-truncated"
    assert sorted(os.listdir(logs)) == [t.runs[0]["logFile"]]


def test_tracker_mutations_and_reload(tracker, tmp_path):
    feed(tracker, defense_run())
    rid = tracker.runs[0]["id"]
    assert tracker.set_vitus(rid, 42)["vitusActual"] == 42
    assert tracker.set_tags(rid, ["Boar", "boar", " solo "])["tags"] == ["Boar", "solo"]
    assert tracker.set_notes(rid, "nice\x01 run")["notes"] == "nice run"
    reloaded = at.ArbiRunTracker(str(tmp_path))
    reloaded.init()
    assert reloaded.runs[0]["tags"] == ["Boar", "solo"] and reloaded.runs[0]["vitusActual"] == 42
    assert "tags" not in tracker.set_tags(rid, [])
    rec = tracker.delete_log(rid)
    assert rec["logFile"] is None and rec["logSizeBytes"] == 0 and tracker.disk_usage_bytes() == 0
    assert tracker.delete_run(rid) and tracker.runs == [] and not tracker.delete_run(rid)


def test_tracker_rejects_traversal_log_paths(tmp_path):
    (tmp_path / "secret.log.gz").write_bytes(b"x")
    rec = {"id": "2026-07-04_21-30-15", "logFile": "../secret.log.gz", "logSizeBytes": 1, "node": "x",
           "startedAt": 0, "durationSec": 0, "source": "live"}
    (tmp_path / "arbi-runs.json").write_text(json.dumps({"schemaVersion": 1, "runs": [rec, {**rec, "id": "../bad"}]}))
    t = at.ArbiRunTracker(str(tmp_path))
    t.init()
    assert len(t.runs) == 1 and t.runs[0]["logFile"] is None and t.runs[0]["logSizeBytes"] == 0


def test_corrupt_index_quarantined(tmp_path):
    (tmp_path / "arbi-runs.json").write_text("{nope")
    t = at.ArbiRunTracker(str(tmp_path))
    t.init()
    assert t.runs == [] and any(f.startswith("arbi-runs.json.corrupt-") for f in os.listdir(tmp_path))


def test_duplicates_live_vs_imported(tracker):
    base = {"node": "Casta (Ceres)", "durationSec": 1800, "logSizeBytes": 10, "endReason": "mission-end"}
    live = {**base, "id": "2026-07-04_21-30-15", "startedAt": 1_000_000, "source": "live", "stats": None,
            "vitusActual": 40, "notes": "keep"}
    imp = {**base, "id": "2026-07-04_21-30-45", "startedAt": 1_030_000, "source": "imported", "stats": {"x": 1},
           "vitusActual": None}
    tracker.runs = [live, imp]
    assert tracker.mark_duplicates()
    assert live["duplicateOf"] == imp["id"] and "duplicateOf" not in imp
    assert imp["vitusActual"] == 40 and imp["notes"] == "keep"
    tracker.delete_run(imp["id"])
    assert "duplicateOf" not in live


# ---- importer ----------------------------------------------------------------------------------

HEADER = "0.500 Sys [Diag]: Current time: Sun Mar 01 12:00:00 2026 [UTC: Sun Mar 01 11:00:00 2026]"


def test_import_multi_run_log(tracker, tmp_path):
    p = tmp_path / "EE.log"
    p.write_text("\n".join([
        HEADER, "5.000 Sys [Info]: Some unrelated line", mission(100, "Arbitration: Casta Defense (Ceres)"),
        drone(150), drone(200), reward(400), reward(500), mission(900, "Cetus (Earth)"), "950.000 Sys [Info]: idle",
        mission(1000, "Arbitration: Berehynia Interception (Sedna)"), drone(1100), drone(1160), reward(1400), reward(1500),
    ]))
    res = tracker.import_ee_log(str(p))
    assert res["skipped"] == 0 and len(res["imported"]) == 2
    first, second = res["imported"]
    anchored = int(dt.datetime(2026, 3, 1, 11, 0, 0, tzinfo=dt.timezone.utc).timestamp() * 1000) - 500 + 100_000
    assert (first["node"], first["missionType"], first["drones"], first["source"], first["endReason"]) == \
        ("Casta Defense (Ceres)", "defense", 2, "imported", "new-mission")
    assert first["startedAt"] == anchored and first["id"] == at.format_run_id(anchored)
    assert (second["missionType"], second["rotations"], second["endReason"]) == ("interception", 2, "imported")
    assert len([f for f in os.listdir(tmp_path / "arbi-logs") if f.endswith(".log.gz")]) == 2
    again = tracker.import_ee_log(str(p))
    assert again["imported"] == [] and again["skipped"] == 2


def test_import_fixtures(tracker):
    for name in ("oestrus-abort-ee.txt", "mot-survival-ee.txt", "rhea-interception-ee.txt"):
        res = tracker.import_ee_log(os.path.join(FIX, name))
        assert res == {"imported": [], "skipped": 1}, name
    res = tracker.import_ee_log(os.path.join(FIX, "stoefler-defense-ee.txt"))
    assert len(res["imported"]) == 1
    rec = res["imported"][0]
    assert rec["endReason"] == "mission-end" and rec["node"] == "Stöfler (Lua)"
    started = int(dt.datetime(2026, 7, 7, 21, 40, 49, tzinfo=dt.timezone.utc).timestamp() * 1000) - 127 + 16_596_750
    assert rec["startedAt"] == started and rec["id"] == at.format_run_id(started)


def test_import_without_header_uses_mtime(tracker, tmp_path):
    p = tmp_path / "EE.log"
    p.write_text("\n".join([mission(100, "Arbitration: Casta Defense (Ceres)"), drone(150), reward(120), reward(151)]))
    mtime = os.path.getmtime(p) * 1000
    res = tracker.import_ee_log(str(p))
    assert len(res["imported"]) == 1 and abs(res["imported"][0]["startedAt"] - (mtime - 51_000)) < 100


# ---- summary payload + analysis ----------------------------------------------------------------

def make_record(**over):
    stats = {"killsPerDrone": 6.4, "avgDroneIntervalSec": 51.2, "expectedVitusMean": 14.6, "expectedVitusStd": 3.2,
             "vitusPerMin": 0.42, "wavesPerRotation": 3, "droneTimestamps": [], "rewardTimestamps": [],
             "preciseStartSec": 100, "lastActivitySec": 2000,
             "saturationBuckets": [{"minCount": 0, "label": "0-2", "seconds": 60, "pct": 10},
                                   {"minCount": 12, "label": "12-14", "seconds": 120, "pct": 20},
                                   {"minCount": 15, "label": "15-17", "seconds": 180, "pct": 30.25},
                                   {"minCount": 27, "label": "27+", "seconds": 30, "pct": 5.5}], "waves": None}
    rec = {"id": "2026-07-08_18-00-00", "startedAt": 1_780_000_000_000, "endedAt": 1_780_000_600_000,
           "missionName": "Arbitration: Casta (Ceres)", "node": "Casta (Ceres)", "missionType": "defense",
           "missionTypeRaw": "MT_DEFENSE", "solNode": "SolNode149", "durationSec": 2948, "rotations": 4, "drones": 120,
           "totalEnemies": 800, "vitusActual": None, "logFile": None, "logSizeBytes": 0, "endReason": "mission-end",
           "source": "live", "stats": stats}
    rec.update(over)
    return rec


def test_summary_payload():
    p = ov.build_summary_payload(make_record())
    assert p["pctTimeAt15Plus"] == 35.8 and p["expectedVitusMean"] == 14.6 and p["rotations"] == 4
    assert p["killsPerDrone"] == pytest.approx(800 / 120)
    assert p["expectedVitusPerMin"] == pytest.approx(14.6 / 2948 * 60)
    p = ov.build_summary_payload(make_record(players=[" Alice ", "Bob", "Alice", "", "Cara", "Dan", "Unexpected"],
                                             vitusActual=17))
    assert p["players"] == ["Alice", "Bob", "Cara", "Dan"] and p["squadSize"] == 4 and p["vitusActual"] == 17
    assert ov.build_summary_payload(make_record(durationSec=0))["expectedVitusPerMin"] is None
    assert ov.build_summary_payload(make_record(endReason="aborted")) is not None
    for bad in ({"rotations": 1}, {"source": "imported"}, {"stats": None}, {"endReason": "inactivity"},
                {"endReason": "log-truncated"}):
        assert ov.build_summary_payload(make_record(**bad)) is None, bad


def test_analysis_rotations_cadence_pb():
    rec = make_record()
    rec["stats"].update({"preciseStartSec": 100, "droneTimestamps": [110, 120, 130, 200, 260, 270],
                         "rewardTimestamps": [150, 300], "lastActivitySec": 300,
                         "pauseIntervals": [{"start": 150, "end": 180}], "idleIntervals": [],
                         "rotationSaturationPct": [40, 60]})
    assert an.drones_per_rotation(rec["stats"]) == [3, 3]
    cells = an.rotation_clear_cells(rec["stats"])
    assert [c["durationSec"] for c in cells] == [50, 120] and cells[1]["saturationPct"] == 60
    assert an.dpm_series(rec["stats"]) == pytest.approx([3 / (50 / 60), 3 / (150 / 60)])
    cad = an.drone_cadence(rec["stats"])
    assert cad["totalWaitSec"] == pytest.approx(10 + 10 + 40 + 60 + 10)   # 130->200 minus the 30 s pause
    assert cad["peak"]["drones"] >= 1
    peer = make_record(id="2026-07-01_18-00-00", drones=60)
    a = an.analyze(rec, [rec, peer])
    assert a["personalBest"]["dronesPerMin"]["isPb"] and a["personalBest"]["dronesPerMin"]["poolSize"] == 2
    assert a["scenarios"][3]["total"] == 15 and a["typeLabel"] == "Defense"
    assert an.analyze(make_record(stats=None, missionType="other", missionTypeRaw="MT_SURVIVAL"), [])["typeLabel"] == "Survival"
    pub = an.public_run(rec)
    assert "droneTimestamps" not in pub["stats"] and pub["stats"]["droneCount"] == 6


# ---- overlay -------------------------------------------------------------------------------------

def test_summary_geometry():
    img = ov.render_summary(ov.build_summary_payload(make_record()), 1.0)
    assert img.size == (420, 252)
    assert ov.place("top-center", (1280, 800), img) == (430, 8)
    assert ov.place("top-right", (1280, 800), img) == (1280 - 420 - 16, 8)
    assert ov.place("left-middle", (1280, 800), img) == (16, 274)
    assert ov.overlay_scale(5, (1280, 800)) == 1.25 and ov.overlay_scale("x", (1920, 1200)) == 1.5
    assert ov.render_summary(ov.build_summary_payload(make_record()), 0.75).size == (315, 189)


def test_summary_preview_png():
    """Realistic Casta defense run summary -> docs/previews/arbi.png (headless manager)."""
    from wfh.overlay.manager import OverlayManager

    rec = make_record(node="Casta (Ceres)", durationSec=1842.3, rotations=6, drones=184, totalEnemies=4238,
                      players=["Tenno", "Volt_Prime", "xXGaraXx", "NyxMain"])
    rec["stats"].update({"expectedVitusMean": 48.5, "expectedVitusStd": 6.2, "avgDroneIntervalSec": 9.8,
                         "saturationBuckets": [{"minCount": 0, "pct": 8.0}, {"minCount": 12, "pct": 30.8},
                                               {"minCount": 15, "pct": 41.2}, {"minCount": 27, "pct": 20.0}]})
    payload = ov.build_summary_payload(rec)
    panel = ov.render_summary(payload, ov.overlay_scale(1.0, (1280, 800)))
    om = OverlayManager(headless=True)
    try:
        x, y = ov.place("top-center", om.size, panel)
        om.show("arbi", panel, x, y, ttl=30)
        comp = None
        deadline = time.time() + 5
        while time.time() < deadline:
            import base64
            import io
            comp = Image.open(io.BytesIO(base64.b64decode(om.snapshot_png().split(",", 1)[1])))
            if comp.getbbox():
                break
            time.sleep(0.05)
        assert comp is not None and comp.getbbox() == (x, y, x + 420, y + 252)
    finally:
        om.stop()
    # A dim orbiter-ish backdrop so the preview reads like the game screen.
    bg = Image.new("RGBA", (1280, 800), (22, 26, 36, 255))
    d = ImageDraw.Draw(bg)
    d.rectangle((0, 640, 1280, 800), fill=(30, 34, 46, 255))
    d.ellipse((1120, 20, 1260, 160), outline=(70, 80, 100, 255), width=2)   # minimap stand-in (top-right)
    bg.alpha_composite(comp.convert("RGBA"))
    lucky = ov.render_summary(ov.build_summary_payload({**rec, "vitusActual": 61, "endReason": "aborted"}), 1.0)
    bg.alpha_composite(lucky, (16, 274))
    os.makedirs(PREVIEWS, exist_ok=True)
    bg.convert("RGB").save(os.path.join(PREVIEWS, "arbi.png"))


def test_controller_toast_fallback(tmp_path):
    from wfh.eelog import EELogTailer
    from wfh.features.arbitration import ArbiController
    from wfh.overlay.manager import OverlayManager
    from wfh.settings import Settings

    class FakeApp:
        def __init__(self):
            self.settings = Settings(str(tmp_path / "s.json"))
            self.overlays = OverlayManager(headless=True)
            self.eelog = EELogTailer()
            self.events, self.toasts = [], []

        def emit(self, n, d=None):
            self.events.append((n, d))

        def toast(self, t, b=""):
            self.toasts.append((t, b))

    app = FakeApp()
    tr = at.ArbiRunTracker(str(tmp_path), now_ms=Clock())
    ctl = ArbiController(app, tr)
    tr.init()
    try:
        feed(tr, defense_run())
        names = [e[0] for e in app.events]
        assert names[:2] == ["arbi.run_saved", "arbi.summary"]
        deadline = time.time() + 2
        while "arbi" not in app.overlays.active() and time.time() < deadline:
            time.sleep(0.02)
        assert "arbi" in app.overlays.active() and not app.toasts
        ctl.dismiss()
        assert "arbi" not in app.overlays.active()
        app.overlays.headless = False                 # not connected -> toast
        assert not ctl.show(ctl.last_summary)
        assert app.toasts and app.toasts[0][0] == "Arbitration complete"
        app.settings.update({"arbitration": {"enabled": False}})
        app.toasts.clear()
        feed(tr, defense_run(t0=4000))
        assert not app.toasts and app.events[-1][0] == "arbi.summary"
        feed(tr, [mission(9000, "Arbitration: Casta Defense (Ceres)"), "9100.000 Sys [Info]: EOM missionLocationUnlocked=1"])
        assert app.events[-1][0] == "arbi.run_skipped"
    finally:
        app.overlays.stop()


# ---- RPCs through the loaded app -------------------------------------------------------------------

def test_rpcs(app, tmp_path):
    from wfh import rpc
    from wfh.data import arbi_schedule

    assert "wfh.features.arbitration" not in app.services["failed_modules"]
    ctl = app.services["arbitration"]
    now = int(time.time() // 3600 * 3600)
    text = "\n".join(f"{now + h * 3600},SolNode{149 + h}" for h in range(-1, 30))
    sched = arbi_schedule.ArbiSchedule(str(tmp_path), fetch=lambda: text)
    sched.refresh()
    arbi_schedule.set_instance(sched)
    try:
        feed(ctl.tracker, defense_run(t0=20000))
        ctl.tracker.await_pending(5)
        res = asyncio.run(rpc.call_local("arbi.runs"))
        assert res["total"] >= 1 and "stats" not in res["runs"][0] and res["runs"][0]["hasStats"]
        rid = res["runs"][0]["id"]
        det = asyncio.run(rpc.call_local("arbi.run", {"id": rid}))
        assert det["run"]["id"] == rid and det["analysis"]["rotations"] and det["hasLog"]
        assert asyncio.run(rpc.call_local("arbi.set_vitus", {"id": rid, "vitus": 30}))["vitusActual"] == 30
        assert asyncio.run(rpc.call_local("arbi.set_vitus", {"id": rid, "vitus": -3})) is None
        assert asyncio.run(rpc.call_local("arbi.set_tags", {"id": rid, "tags": ["a", "A"]}))["tags"] == ["a"]
        assert asyncio.run(rpc.call_local("arbi.run", {"id": "x" * 65})) is None
        from wfh.menus import arbitrations as menu
        out = menu.export_log(rid, str(tmp_path / "dl"))
        assert out["ok"] and os.path.basename(out["path"]) == f"Casta_Defense_Ceres_{rid}.log"
        # A symlink planted at the export name is replaced, never followed.
        victim = tmp_path / "victim.txt"
        victim.write_text("keep")
        os.unlink(out["path"])
        os.symlink(victim, out["path"])
        out2 = menu.export_log(rid, str(tmp_path / "dl"))
        assert out2["ok"] and victim.read_text() == "keep" and not os.path.islink(out2["path"])
        assert [f for f in os.listdir(tmp_path / "dl") if f.startswith(".wfh-export-")] == []
        assert asyncio.run(rpc.call_local("arbi.status"))["tracking"] is True
        sched.set_favorite("SolNode176", True)   # 27 h out: beyond the 24 h current/next window
        q = asyncio.run(rpc.call_local("arbi.qam"))
        assert q["lastRun"]["id"] == rid and q["current"]["nodeId"] == "SolNode149" and q["next"]["nodeId"] == "SolNode150"
        assert q["nextFavorite"] and q["nextFavorite"]["nodeId"] == "SolNode176"
        assert asyncio.run(rpc.call_local("arbi.summary_show")) is not None
        asyncio.run(rpc.call_local("arbi.summary_dismiss"))
        imp = asyncio.run(rpc.call_local("arbi.import_log", {"path": os.path.join(FIX, "stoefler-defense-ee.txt")}))
        assert len(imp["imported"]) == 1
        assert asyncio.run(rpc.call_local("arbi.delete_run", {"id": rid})) == {"ok": True}
    finally:
        arbi_schedule.set_instance(None)
