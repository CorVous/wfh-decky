"""World state parser, cycle math and arbitration schedule against recorded payloads (no network).

Fixtures in tests/fixtures/world are DE-format payloads written for these tests
(DE's worldState.php, oracle.browse.wf/bounty-cycle, warframestat /pc, browse.wf
arbys.txt), all pinned to NOW = 2026-10-17T12:00:00Z. Expected labels mirror
WFHelper's tests/main/worldStateParser.test.ts and arbiSchedule.test.ts.
"""

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timezone

import pytest

from wfh import rpc
from wfh.data import arbi_schedule as arbi
from wfh.data import solnodes
from wfh.data import worldstate as ws

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORLD = os.path.join(ROOT, "tests", "fixtures", "world")
NOW = int(datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
H, M = 3_600_000, 60_000

NAMES = {
    "/Lotus/Types/Items/MiscItems/WaterFightBucks": "Nakak Pearls",
    "/Lotus/Upgrades/Mods/Warframe/AvatarShieldMaxMod": "Redirection",
    "/Lotus/Weapons/Tenno/Rifle/Rifle": "Braton",
    "/Lotus/Weapons/Tenno/Melee/Glaives/Boomerang/BoomerangWeapon": "Kestrel",
    "/Lotus/Types/Game/Projections/T1VoidProjectionGaussPrimeDBronze": "Lith G9 Intact",
    "/Lotus/Types/Gameplay/NarmerSorties/ArchonCrystalBoreal": "<Shard_blue_simple> Azure Archon Shard",
    "/Lotus/Types/Recipes/Weapons/WeaponParts/DeraVandalBarrel": "Dera Vandal Barrel",
    "/Lotus/Types/Items/Research/EnergyComponent": "Fieldron",
    "/Lotus/Types/Items/MiscItems/Forma": "Forma",
}


def load(name):
    with open(os.path.join(WORLD, name), encoding="utf-8") as f:
        return f.read() if name.endswith(".txt") else json.load(f)


@pytest.fixture(autouse=True)
def fake_items(monkeypatch):
    """Item names from a small table so tests do not depend on the bundled item DB."""
    monkeypatch.setattr(ws, "item_name_lookup", NAMES.get)
    ws.reset_cache()
    yield
    ws.reset_cache()


@pytest.fixture
def parsed():
    return ws.parse_raw(load("worldState.json"), NOW)


def dl(ms):
    return {"$date": {"$numberLong": str(ms)}}


# ---- solnodes ---------------------------------------------------------------------------

def test_solnodes_labels():
    assert solnodes.node_label("SolNode149") == "Casta (Ceres)"
    assert solnodes.node_label_comma("SolNode149") == "Casta, Ceres"
    assert solnodes.node_mission("SolNode149") == "Defense"
    assert solnodes.node_faction("SolNode149") == "Grineer"
    # Oestrus (Eris): DE's node mission name, not the MT_ enum.
    assert solnodes.node_mission("SolNode167") == "Infested Salvage"
    assert solnodes.node_faction("SolNode167") == "Infested"
    assert solnodes.node_label("SolNode999") == "SolNode999"
    assert solnodes.node_label("") == "Unknown"
    assert solnodes.node_mission("SolNode999") == "Unknown"
    assert solnodes.faction_label("FC_OROKIN") == "Corrupted"
    assert solnodes.faction_label("FC_MADE_UP") == "Made Up"
    assert solnodes.faction_label(None) == "Unknown"
    assert solnodes.title_case("INFESTED SALVAGE") == "Infested Salvage"
    assert solnodes.title_case("kala-azar x") == "Kala-Azar X"
    assert len(solnodes.NODES) > 300 and len(solnodes.MISSION_TYPES) > 30


@pytest.mark.parametrize("mt, label", sorted(solnodes.MISSION_TYPE_FALLBACK.items()))
def test_fallback_mission_labels_match_de_export(mt, label):
    # keeps the fallback mission labels in step with DE's export (WFHelper test of the same name)
    assert solnodes.mission_type_label(mt, "UnmappedNode") == label


# ---- parse_raw ------------------------------------------------------------------------------

def test_fissures(parsed):
    f = parsed["fissures"]
    normal = [x for x in f if not x.get("isStorm")]
    storms = [x for x in f if x.get("isStorm")]
    # expired Interception and the non-void modifier are dropped
    assert [(x["tier"], x["missionType"], x["node"], x["isHard"]) for x in normal] == [
        ("Lith", "Capture", "Mantle, Earth", False),
        ("Axi", "Defense", "Casta, Ceres", True),
        ("Meso", "Void Flood", "Everview Arc, Zariman", False),
        ("Requiem", "Defense", "Hyf, Deimos", True),
    ]
    assert normal[0]["nodeId"] == "SolNode63" and normal[0]["planet"] == "Earth"
    assert normal[0]["expiry"] == "2026-10-17T12:41:00.000Z"
    # void storms resolve the node's railjack mission; unknown tier dropped
    assert [(x["tier"], x["missionType"], x["isHard"]) for x in storms] == [
        ("Neo", "Survival", False), ("Meso", "Volatile", True)]


def test_fissure_mission_type_labels():
    def fissure(mt, node="UnmappedNode"):
        raw = {"ActiveMissions": [{"Modifier": "VoidT1", "MissionType": mt, "Node": node, "Expiry": dl(NOW + 1000)}]}
        return ws.parse_raw(raw, NOW)["fissures"][0]["missionType"]

    assert fissure("MT_TERRITORY") == "Interception"
    assert fissure("MT_PURIFY") == "Infested Salvage"
    assert fissure("MT_ARTIFACT") == "Disruption"
    assert fissure("MT_RAILJACK", "CrewBattleNode524") == "Volatile"


def test_baro_and_varzia(parsed):
    baro = parsed["voidTrader"]
    assert baro["location"] == "Orcus Relay (Pluto)"
    assert baro["activation"] == "2026-10-16T16:00:00.000Z"
    inv = baro["inventory"]
    assert [i["uniqueName"] for i in inv] == [
        "/Lotus/Upgrades/Mods/Warframe/AvatarShieldMaxMod",
        "/Lotus/Types/Items/ShipDecos/Plushies/PlushyNecraLoid",
        "/Lotus/Types/StoreItems/AvatarImages/AvatarImageBaroKiTeel",
        "/Lotus/Types/Game/Projections/T1VoidProjectionGaussPrimeDBronze",
        "/Lotus/Weapons/Tenno/Rifle/Rifle",
    ]
    assert [i["item"] for i in inv] == ["Redirection", "Plushy Necra Loid", "Glyph Baro Ki Teel", "Lith G9 Relic",
                                       "Braton"]
    assert inv[0]["ducats"] == 350 and inv[0]["credits"] == 150000
    assert inv[3] == {**inv[3], "ducats": 25} and "credits" not in inv[3]
    assert "ducats" not in inv[4] and inv[4]["credits"] == 25000
    varzia = parsed["vaultTrader"]
    assert varzia["location"] == "TennoConHUB2" and varzia["inventory"][0]["item"] == "Forma"


def test_baro_costs_preserved_like_wfhelper():
    raw = {"VoidTraders": {"Activation": dl(NOW), "Expiry": dl(NOW + H), "Node": "EarthHUB", "Manifest": [
        {"ItemType": "/Lotus/StoreItems/Weapons/MissingCosts"},
        {"ItemType": "/Lotus/StoreItems/Weapons/Free", "PrimePrice": 0, "RegularPrice": 0},
        {"ItemType": "/Lotus/StoreItems/Weapons/InvalidCosts", "PrimePrice": -1, "RegularPrice": float("nan")}]}}
    items = ws.parse_raw(raw, NOW)["voidTrader"]["inventory"]
    assert "ducats" not in items[0] and "credits" not in items[0]
    assert items[1]["ducats"] == 0 and items[1]["credits"] == 0
    assert "ducats" not in items[2] and "credits" not in items[2]
    assert ws.parse_raw(raw, NOW)["voidTrader"]["location"] == "Larunda Relay (Earth)"


def test_sortie_archon(parsed):
    s = parsed["sortie"]
    assert s["id"] == "6710b0000000000000000001" and s["boss"] == "Lephantis"
    assert s["missions"] == [
        {"node": "Pacific (Earth)", "mission": "Survival", "modifier": "Enhanced Enemy Armor"},
        {"node": "Tharsis (Mars)", "mission": "Exterminate", "modifier": "Melee Only"},
        {"node": "Mantle (Earth)", "mission": "Defense", "modifier": "Made Up"},
    ]
    a = parsed["archonHunt"]
    assert a["boss"] == "Nira"
    assert a["missions"] == [{"node": "Callisto (Jupiter)", "mission": "Rescue"},
                             {"node": "Pacific (Earth)", "mission": "Survival"},
                             {"node": "Themisto (Jupiter)", "mission": "Assassination"}]
    assert a["expiry"] == "2026-10-19T00:00:00.000Z"


def test_nightwave(parsed):
    nw = parsed["nightwave"]
    assert nw["season"] == 18 and nw["affiliationTag"] == "RadioLegionIntermission16Syndicate"
    acts = {c["id"]: c for c in nw["challenges"]}
    assert list(acts) == ["nw-daily1", "nw-elite1", "nw-rad", "nw-unknown", "nw-undated"]
    assert acts["nw-daily1"] == {**acts["nw-daily1"], "name": "SeasonDailyAimGlide", "title": "Glider",
                                 "description": "Kill 15 Enemies while Aim Gliding", "standing": 1000,
                                 "requiredCount": 15, "isDaily": True, "isElite": False}
    assert acts["nw-elite1"]["title"] == "Rise of the Machine"
    assert acts["nw-elite1"]["standing"] == 7000 and acts["nw-elite1"]["isElite"]
    assert acts["nw-rad"]["description"] == "Kill 150 Enemies with Radiation Damage."
    assert acts["nw-unknown"] == {**acts["nw-unknown"], "title": "Season Daily Made Up Act", "description": "",
                                  "standing": None, "requiredCount": 0}


def test_alerts(parsed):
    assert len(parsed["alerts"]) == 1
    a = parsed["alerts"][0]
    assert a == {**a, "id": "6710aa01e1b2c3d4e5f60001", "node": "Callisto (Jupiter)", "mission": "Interception",
                 "faction": "Corpus", "minLevel": 1, "maxLevel": 2, "credits": 50000}
    assert a["items"] == [{"name": "Nakak Pearls", "count": 175}, {"name": "Made Up Thing", "count": 1}]


def test_invasions(parsed):
    inv = parsed["invasions"]
    assert len(inv) == 2  # completed one dropped
    a, b = inv
    assert a["node"] == "Casta, Ceres" and a["attacker"]["faction"] == "Grineer"
    assert a["completion"] == 14.0 and not a["vsInfestation"]
    assert a["attacker"]["reward"]["countedItems"] == [{"count": 1, "type": "Dera Vandal Barrel"}]
    assert a["defender"]["reward"]["countedItems"] == [{"count": 3, "type": "Fieldron"}]
    assert b["vsInfestation"] and b["attacker"]["faction"] == "Infested" and b["completion"] == 90.0
    assert b["attacker"]["reward"] == {"items": [], "countedItems": [], "credits": 0}


def test_raw_bounties(parsed):
    b = {x["syndicateKey"]: x for x in parsed["bounties"]}
    assert set(b) == {"CetusSyndicate", "SolarisSyndicate"}  # expired Entrati dropped
    cetus = b["CetusSyndicate"]
    assert cetus["syndicate"] == "Ostrons" and len(cetus["jobs"]) == 2
    assert cetus["jobs"][0] == {"type": "Attrition Bounty", "enemyLevels": [5, 15], "standingStages": [430, 430, 430],
                                "minMR": 0}
    assert cetus["jobs"][1]["type"] == "Reclamation Bounty"


def test_deals_calendar_descents_circuit(parsed):
    assert len(parsed["dailyDeals"]) == 1
    deal = parsed["dailyDeals"][0]
    assert deal["item"] == "Kestrel" and deal["salePrice"] == 75 and deal["sold"] == 97
    cal = parsed["calendarSeason"]
    assert cal["season"] == "Autumn"
    assert [d["day"] for d in cal["days"]] == [13, 24]
    ev = cal["days"][0]["events"]
    assert ev[0] == {"kind": "reward", "label": "Mod Drop Chance Booster 3 Day",
                     "uniqueName": "/Lotus/Types/Boosters/ModDropChanceBooster3DayStoreItem"}
    assert ev[1] == {"kind": "challenge", "label": "Even the Odds", "description": "Kill 250 Enemies"}
    assert [e["label"] for e in cal["days"][1]["events"]] == ["Armor", "Ability Strength"]
    assert parsed["descents"]["expiry"] == "2026-10-19T00:00:00.000Z"
    assert parsed["duviriCycle"]["choices"] == [
        {"category": "normal", "choices": ["Saryn", "Vauban", "Nova"]},
        {"category": "hard", "choices": ["Lex", "Magistar", "Boltor", "Bronco", "Ceramic Dagger"]}]


def test_calendar_reward_strips_tags():
    raw = {"KnownCalendarSeasons": [{"Activation": dl(NOW - H), "Expiry": dl(NOW + H), "Season": "CST_WINTER",
                                     "Days": [{"day": 21, "events": [{"type": "CET_REWARD",
                                               "reward": "/Lotus/StoreItems/Types/Gameplay/NarmerSorties/ArchonCrystalBoreal"}]}]}]}
    ev = ws.parse_raw(raw, NOW)["calendarSeason"]["days"][0]["events"][0]
    assert ev == {"kind": "reward", "label": "Azure Archon Shard",
                  "uniqueName": "/Lotus/Types/Gameplay/NarmerSorties/ArchonCrystalBoreal"}


def test_malformed_payload_survives():
    window = {"Activation": dl(NOW - M), "Expiry": dl(NOW + H)}
    parsed = ws.parse_raw({"ActiveMissions": "nope", "Sorties": [{**window, "Boss": "SORTIE_BOSS_LEPHANTIS", "Variants": {}}],
                           "LiteSorties": [{**window, "Boss": "SORTIE_BOSS_NIRA", "Missions": "nope"}],
                           "SeasonInfo": {**window, "Season": 18, "Phase": 0, "ActiveChallenges": {}},
                           "Alerts": {}, "Invasions": [None, 3], "VoidTraders": [],
                           "Descents": {}, "DailyDeals": "x"}, NOW)
    assert parsed["sortie"]["boss"] == "Lephantis" and parsed["sortie"]["missions"] == []
    assert parsed["archonHunt"]["missions"] == []
    assert parsed["nightwave"]["challenges"] == []
    assert parsed["alerts"] == [] and parsed["fissures"] == [] and parsed["invasions"] == []
    assert parsed["voidTrader"] is None and parsed["descents"] is None
    empty = ws.parse_raw({"ActiveMissions": []}, NOW)
    assert empty["sortie"] is None and empty["archonHunt"] is None and empty["nightwave"] is None
    assert ws.parse_raw(None, NOW) is None


def test_parse_circuit_choices_windows():
    start, end = 1_784_505_600_000, 1_785_110_400_000
    sched = {"EndlessXpSchedule": [{"Activation": dl(start), "Expiry": dl(end), "CategoryChoices": [
        {"Category": "EXC_NORMAL", "Choices": ["Saryn", "Vauban", "Nova"]},
        {"Category": "EXC_HARD", "Choices": ["Lex", "Magistar", "CeramicDagger"]}]}]}
    assert ws.parse_circuit_choices(sched, start + 1000)[1]["choices"] == ["Lex", "Magistar", "Ceramic Dagger"]
    assert ws.parse_circuit_choices(sched, start - 1000) == []
    assert ws.parse_circuit_choices(sched, end + 1000) == []
    assert ws.parse_circuit_choices({"EndlessXpChoices": [{"Category": "EXC_NORMAL", "Choices": ["Excalibur"]}]},
                                    start) == [{"category": "normal", "choices": ["Excalibur"]}]
    assert ws.parse_circuit_choices(None, start) == []


def test_circuit_rotation():
    c = ws.build_circuit([{"category": "normal", "choices": ["Saryn", "Vauban", "Nova"]},
                          {"category": "hard", "choices": ["Ack and Brunt", "Soma", "Vasto"]}])
    assert c["normal"]["weekIndex"] == 3 and c["normal"]["weeks"][1] == ["Nekros", "Valkyr", "Oberon"]
    assert len(c["normal"]["weeks"]) == 11
    assert c["hard"]["weekIndex"] == 5  # "Ack & Brunt" matches "Ack and Brunt"
    assert ws.circuit_rotation_index(ws.CIRCUIT_HARD_ROTATION, ["Braton", "Soma"]) == -1
    assert ws.build_circuit([])["hard"] == {"choices": [], "weekIndex": -1, "weeks": []}


def test_bounty_cycle_seed_bounties():
    nodes = lambda n: [{"node": f"FakeNode{i}"} for i in range(n)]  # noqa: E731
    [z] = ws.parse_bounty_cycle_bounties({"bounties": {"ZarimanSyndicate": nodes(6)}})
    assert [j["enemyLevels"] for j in z["jobs"]][:5] == [[50, 55], [60, 65], [70, 75], [90, 95], [110, 115]]
    assert z["jobs"][5]["enemyLevels"] == [0, 0]
    [hex_] = ws.parse_bounty_cycle_bounties({"bounties": {"HexSyndicate": nodes(7)}})
    assert hex_["syndicate"] == "The Hex"
    assert [j["tierIndex"] for j in hex_["jobs"]] == list(range(7))
    assert [j["standingStages"] for j in hex_["jobs"]] == [[1000], [2000], [3000], [4000], [5000], [6000], [7500]]
    assert ws.parse_bounty_cycle_bounties({"bounties": {"MadeUpSyndicate": nodes(1)}}) == []
    oracle = ws.parse_bounty_cycle_bounties(load("oracle_bounty_cycle.json"))
    zar = next(b for b in oracle if b["syndicateKey"] == "ZarimanSyndicate")
    assert zar["jobs"][0]["type"] == "Void Flood" and zar["expiry"] == "2026-10-17T12:37:00.000Z"


# ---- cycle math -------------------------------------------------------------------------------

def test_vallis():
    epoch = ws.VALLIS_EPOCH_MS
    assert epoch == int(datetime(2018, 11, 10, 8, 13, 48, tzinfo=timezone.utc).timestamp() * 1000)
    warm = ws.compute_vallis(epoch + 100_000)
    assert warm["isWarm"] and warm["expiry"] == ws.iso(epoch + 400_000)
    cold = ws.compute_vallis(epoch + 1_600_000 * 7 + 500_000)
    assert not cold["isWarm"] and cold["expiry"] == ws.iso(epoch + 1_600_000 * 8)
    assert cold["activation"] == ws.iso(epoch + 1_600_000 * 7 + 400_000)


def test_cetus_cambion():
    e = NOW + 37 * M
    cetus, cambion = ws.compute_cetus_cambion(e, NOW)
    assert not cetus["isDay"] and cetus["expiry"] == ws.iso(e) and cambion["active"] == "vome"
    cetus, cambion = ws.compute_cetus_cambion(e, NOW - 60 * M)
    assert cetus["isDay"] and cetus["expiry"] == ws.iso(e - 3_000_000) and cambion["active"] == "fass"
    assert cetus["activation"] == ws.iso(e - 9_000_000)


def test_duviri_mood():
    i = NOW // 7_200_000
    d = ws.compute_duviri_mood(NOW)
    assert d["state"] == ws.DUVIRI_MOODS[i % 5] and d["nextState"] == ws.DUVIRI_MOODS[(i + 1) % 5]
    assert d["expiry"] == ws.iso((i + 1) * 7_200_000)
    assert ws.compute_duviri_mood(0)["state"] == "Sorrow"


def test_steel_path_and_resets():
    sp = ws.compute_steel_path(NOW)
    ind = int(((NOW - ws.STEEL_PATH_EPOCH_MS) / 1000 % 4_838_400) // 604_800)
    assert sp["currentReward"] == ws.STEEL_PATH_ROTATION[ind]
    assert sp["activation"] == "2026-10-12T00:00:00.000Z" and sp["expiry"] == "2026-10-18T23:59:59.000Z"
    assert [u["weekOffset"] for u in sp["upcoming"]] == [1, 2]
    assert sp["upcoming"][0]["name"] == ws.STEEL_PATH_ROTATION[(ind + 1) % 8]["name"]
    assert sp["upcoming"][0]["activation"] == "2026-10-19T00:00:00.000Z"
    # epoch week itself is index 0 (Umbra Forma)
    assert ws.compute_steel_path(ws.STEEL_PATH_EPOCH_MS + 1000)["currentReward"]["name"] == "Umbra Forma Blueprint"
    r = ws.compute_resets(NOW)
    assert r["daily"] == "2026-10-18T00:00:00.000Z" and r["weekly"] == "2026-10-19T00:00:00.000Z"
    # On Monday 00:00 exactly the weekly reset is a full week away.
    monday = int(datetime(2026, 10, 19, tzinfo=timezone.utc).timestamp() * 1000)
    assert ws.compute_resets(monday)["weekly"] == "2026-10-26T00:00:00.000Z"


def test_iso_and_de_dates():
    assert ws.iso(0) == "1970-01-01T00:00:00.000Z" and ws.iso(1234) == "1970-01-01T00:00:01.234Z"
    assert ws.de_ms({"$date": {"$numberLong": "1792238400000"}}) == NOW
    assert ws.de_ms({"$date": NOW}) == NOW and ws.de_ms(None) == 0 and ws.de_ms({"$date": {"$numberLong": "x"}}) == 0
    assert ws.parse_iso("2026-10-17T12:00:00.000Z") == NOW


# ---- fetch chain and merge ---------------------------------------------------------------------

class FakeNet:
    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, url, timeout):
        with self.lock:
            self.calls.append(url)
        v = self.routes.get(url)
        if v is None or isinstance(v, Exception):
            raise v or RuntimeError(f"HTTP 404 for {url}")
        return json.loads(json.dumps(v))

    def ws_calls(self):
        return [u for u in self.calls if u in (*ws.FETCH_URLS, ws.ORACLE_WORLDSTATE_URL)]


DE, LEGACY = ws.FETCH_URLS


def full_routes():
    return {DE: load("worldState.json"), ws.ORACLE_BOUNTY_CYCLE_URL: load("oracle_bounty_cycle.json"),
            ws.WARFRAMESTAT_URL: load("warframestat_pc.json")}


def run(net, enrich=True):
    return ws.fetch_and_parse(enrich, now=lambda: NOW)


def test_de_first(monkeypatch):
    net = FakeNet({DE: {"ActiveMissions": []}})
    monkeypatch.setattr(ws, "fetch_json", net)
    out = run(net)
    assert net.ws_calls() == [DE] and out["source"] == "de"


def test_falls_through_to_legacy_then_oracle(monkeypatch):
    net = FakeNet({ws.ORACLE_WORLDSTATE_URL: {"ActiveMissions": []}})
    monkeypatch.setattr(ws, "fetch_json", net)
    assert run(net, False)["source"] == "oracle"
    assert net.ws_calls() == [DE, LEGACY, ws.ORACLE_WORLDSTATE_URL]


def test_rejects_empty_payload(monkeypatch):
    net = FakeNet({DE: {}, LEGACY: {"ActiveMissions": []}})
    monkeypatch.setattr(ws, "fetch_json", net)
    assert run(net, False)["source"] == "de-legacy"
    assert net.ws_calls() == [DE, LEGACY]


def test_every_source_failed(monkeypatch):
    net = FakeNet({DE: {}, LEGACY: {}})
    monkeypatch.setattr(ws, "fetch_json", net)
    with pytest.raises(RuntimeError, match="every world-state source failed"):
        run(net, False)


def test_full_merge(monkeypatch):
    net = FakeNet(full_routes())
    monkeypatch.setattr(ws, "fetch_json", net)
    out = run(net)
    # Cetus from the oracle bounty cycle: night (night started 13 min ago), 37 min left
    assert out["cetusCycle"] == {**out["cetusCycle"], "isDay": False, "state": "night",
                                 "expiry": "2026-10-17T12:37:00.000Z"}
    assert out["cambionCycle"]["active"] == "vome"
    # Earth from warframestat; Zariman too (warframestat wins over the oracle faction)
    assert out["earthCycle"] == {**out["earthCycle"], "isDay": True, "expiry": "2026-10-17T13:35:00.000Z"}
    assert out["zarimanCycle"]["state"] == "grineer"
    assert out["vallisCycle"]["expiry"] and out["duviriCycle"]["state"] in ws.DUVIRI_MOODS
    assert out["duviriCycle"]["expiry"] == ws.iso((NOW // 7_200_000 + 1) * 7_200_000)
    assert out["duviriCycle"]["choices"][0]["choices"] == ["Saryn", "Vauban", "Nova"]
    assert out["circuit"]["hard"]["weekIndex"] == 3
    assert out["bountyRotation"] == "B"
    keys = [b["syndicateKey"] for b in out["bounties"]]
    # raw Cetus/Solaris; warframestat "Ostrons" skipped (raw covers it), "Entrati" fills the gap; seeds after
    assert keys == ["CetusSyndicate", "SolarisSyndicate", "Entrati", "ZarimanSyndicate", "EntratiLabSyndicate",
                    "HexSyndicate"]
    assert len(out["invasions"]) == 2 and out["invasions"][0]["nodeId"] == "SolNode149"  # raw wins
    assert out["resets"]["daily"] == "2026-10-18T00:00:00.000Z"
    json.dumps(out)  # JSON-able for the RPC


def test_enrichment_fallbacks(monkeypatch):
    routes = full_routes()
    routes[ws.ORACLE_BOUNTY_CYCLE_URL] = RuntimeError("blocked")
    routes[ws.WARFRAMESTAT_URL] = RuntimeError("blocked")
    raw = routes[DE]
    raw["Invasions"] = []
    net = FakeNet(routes)
    monkeypatch.setattr(ws, "fetch_json", net)
    out = run(net)
    # Cetus still resolves from DE's own CetusSyndicate expiry; earth follows Cetus.
    assert out["cetusCycle"]["expiry"] == "2026-10-17T12:37:00.000Z" and not out["cetusCycle"]["isDay"]
    assert out["earthCycle"]["isDay"] is False
    assert out["zarimanCycle"] is None and out["invasions"] == []
    assert [b["syndicateKey"] for b in out["bounties"]] == ["CetusSyndicate", "SolarisSyndicate"]
    # failed hosts are backed off: the next refresh does not ask them again
    n = len(net.calls)
    run(net)
    assert net.calls[n:] == [DE]


def test_warframestat_cetus_last_resort(monkeypatch):
    routes = full_routes()
    routes[ws.ORACLE_BOUNTY_CYCLE_URL] = RuntimeError("blocked")
    routes[DE] = {"ActiveMissions": [], "Invasions": []}
    net = FakeNet(routes)
    monkeypatch.setattr(ws, "fetch_json", net)
    out = run(net)
    assert out["cetusCycle"]["expiry"] == "2026-10-17T12:37:00.000Z" and out["cetusCycle"]["state"] == "night"
    assert out["invasions"][0]["id"] == "wfs-inv-1" and len(out["invasions"]) == 1
    assert out["duviriCycle"]["choices"][1]["choices"][-1] == "Ceramic Dagger"  # from warframestat
    assert [b["syndicate"] for b in out["bounties"]][:2] == ["Ostrons", "Entrati"]


# ---- state() cache --------------------------------------------------------------------------------

def test_state_cache_and_stale_on_error(monkeypatch):
    monkeypatch.setattr(ws, "now_ms", lambda: NOW)
    net = FakeNet(full_routes())
    monkeypatch.setattr(ws, "fetch_json", net)
    a = ws.state(60)
    assert not a["stale"] and a["error"] is None and a["fetchedAt"] == NOW and len(a["fissures"]) == 6
    n = len(net.calls)
    assert ws.state(60)["fetchedAt"] == NOW and len(net.calls) == n  # cached
    net.routes = {}
    b = ws.state(0)
    assert b["stale"] and "every world-state source failed" in b["error"]
    assert len(b["fissures"]) == 6 and b["fetchedAt"] == NOW


def test_state_empty_when_nothing_cached(monkeypatch):
    monkeypatch.setattr(ws, "fetch_json", FakeNet({}))
    s = ws.state()
    assert s["stale"] and s["error"] and s["fissures"] == [] and s["steelPath"]["currentReward"]
    assert s["vallisCycle"]["expiry"] and s["resets"]["daily"]


def test_state_single_flight(monkeypatch):
    monkeypatch.setattr(ws, "now_ms", lambda: NOW)
    gate = threading.Event()
    net = FakeNet({DE: {"ActiveMissions": []}})

    def slow(url, timeout):
        gate.wait(2)
        return net(url, timeout)

    monkeypatch.setattr(ws, "fetch_json", slow)
    results = []
    threads = [threading.Thread(target=lambda: results.append(ws.state(60))) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    gate.set()
    for t in threads:
        t.join(5)
    assert len(results) == 4 and net.ws_calls() == [DE]


def test_rpc_world_state(app, monkeypatch):
    monkeypatch.setattr(ws, "now_ms", lambda: NOW)
    monkeypatch.setattr(ws, "fetch_json", FakeNet(full_routes()))
    assert "world.state" in rpc.methods() and "world.arbi_schedule" in rpc.methods()
    out = asyncio.run(rpc.call_local("world.state", {}))
    assert out["sortie"]["boss"] == "Lephantis" and out["source"] == "de"
    assert app.services["worldstate"] is ws.state


# ---- arbitration schedule ----------------------------------------------------------------------------

def test_parse_arbys_text():
    text = "\n".join(["1751980800,SolNode149", "1751984400,SolNode167", "", "garbage", "abc,SolNode1", "-5,SolNode1",
                      "1751988000,bad node!", " 1751991600 , SolNode42 "])
    assert arbi.parse_arbys_text(text) == [(1751980800, "SolNode149"), (1751984400, "SolNode167"),
                                           (1751991600, "SolNode42")]
    rows = arbi.parse_arbys_text(load("arbys.txt"))
    assert len(rows) == 72


def test_build_entries_labels():
    casta, oestrus, unknown = arbi.build_entries([(1751980800, "SolNode149"), (1751984400, "SolNode167"),
                                                  (2, "SolNode999")])
    assert casta.to_json() == {"epochMs": 1751980800000, "nodeId": "SolNode149", "node": "Casta (Ceres)",
                               "mission": "Defense", "faction": "Grineer", "key": "1751980800000:SolNode149"}
    assert (oestrus.mission, oestrus.faction) == ("Infested Salvage", "Infested")
    assert (unknown.node, unknown.mission, unknown.faction) == ("SolNode999", "Unknown", "Unknown")
    assert arbi.faction_badge("Infested") == "infested" and arbi.faction_badge("Corrupted") == "corrupted"


def test_filter_window():
    now = 10_000_000_000
    es = [arbi.Entry(t, "SolNode149", "", "", "") for t in (now + H, now - H // 2, now - 2 * H, now + 400 * 24 * H,
                                                            now + 3 * 24 * H)]
    assert [e.epoch_ms for e in arbi.filter_window(es, now)] == [now - H // 2, now + H, now + 3 * 24 * H]
    assert [e.epoch_ms for e in arbi.filter_window(es, now, days=1)] == [now - H // 2, now + H]


def test_due_alerts_and_prune():
    now = 20_000_000_000
    e = lambda ms, n="SolNode149": arbi.Entry(ms, n, "Casta (Ceres)", "Defense", "Grineer")  # noqa: E731
    in_lead, outside, started = e(now + 3 * M), e(now + 30 * M, "SolNode167"), e(now - 1)
    assert [x.key for x in arbi.compute_due_alerts([in_lead, outside], [in_lead.key, outside.key], [], 5, set(), now)] \
        == [in_lead.key]
    assert len(arbi.compute_due_alerts([in_lead], [], ["SolNode149"], 5, set(), now)) == 1
    assert arbi.compute_due_alerts([in_lead], [], ["SolNode149"], 5, {in_lead.key}, now) == []
    assert arbi.compute_due_alerts([started, in_lead], [], [], 5, set(), now) == []
    now = 30_000_000_000
    fresh, stale = f"{now - M}:SolNode1", f"{now - 3 * H}:SolNode1"
    assert arbi.prune_keys([fresh, stale, "junk"], now) == [fresh]
    assert arbi.alert_text(in_lead, 20_000_000_000)[1] == "Defense on Casta (Ceres) vs Grineer - starts in ~3 min."


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_sched(tmp_path, text=None, clock=None, fail=False):
    calls = []

    def fetch():
        calls.append(1)
        if fail:
            raise RuntimeError("blocked")
        return text if text is not None else load("arbys.txt")

    s = arbi.ArbiSchedule(str(tmp_path), fetch=fetch, now_ms=clock or Clock(NOW))
    s.calls = calls
    return s


def test_schedule_fetch_cache_and_payload(tmp_path):
    clock = Clock(NOW)
    s = make_sched(tmp_path, clock=clock)
    p = s.payload(days=1)
    assert len(s.calls) == 1 and p["fetchedAt"] == NOW and not p["stale"] and p["error"] is None
    # the still-active hour (started at 12:00) plus 24 h ahead
    assert p["entries"][0]["epochMs"] == NOW - H  # the window keeps one hour back
    assert p["current"]["epochMs"] == NOW and p["next"]["epochMs"] == NOW + H
    assert all(NOW - H <= e["epochMs"] <= NOW + 24 * H for e in p["entries"])
    s.payload()
    assert len(s.calls) == 1  # fresh for 55 min
    # a new instance reads the disk cache without fetching
    s2 = make_sched(tmp_path, clock=clock, fail=True)
    assert len(s2.entries) == 72 and s2.fetched_at == NOW
    clock.t = NOW + 56 * M
    p2 = s2.payload()
    assert len(s2.calls) == 1 and p2["error"] and len(p2["entries"]) > 0  # stale-on-error
    s2.payload()
    assert len(s2.calls) == 1  # backs off 10 min after a failure
    clock.t += 11 * M
    s2.payload()
    assert len(s2.calls) == 2


def test_schedule_rejects_empty_text(tmp_path):
    s = make_sched(tmp_path, text="garbage\n")
    assert not s.refresh() and s.entries == [] and "no valid" in s.last_error
    assert not os.path.exists(s.cache_path)


def test_alert_prefs_persist_and_sanitize(tmp_path):
    s = make_sched(tmp_path)
    assert s.set_occurrence("1751980800000:SolNode149", True)["occurrences"] == ["1751980800000:SolNode149"]
    assert s.set_favorite("SolNode167", True)["favoriteNodes"] == ["SolNode167"]
    assert s.set_lead(10)["minutesBefore"] == 10
    assert s.set_lead(0) is None and s.set_lead(500) is None and s.set_lead("x") is None
    assert s.set_occurrence("garbage", True) is None and s.set_favorite("bad node!", True) is None
    raw = json.loads(open(os.path.join(tmp_path, "arbi-sched-alerts.json")).read())
    assert raw == {"schemaVersion": 1, "occurrences": ["1751980800000:SolNode149"], "favoriteNodes": ["SolNode167"],
                   "minutesBefore": 10, "firedKeys": []}
    assert s.set_favorite("SolNode167", False)["favoriteNodes"] == []
    # sanitize on load
    with open(os.path.join(tmp_path, "arbi-sched-alerts.json"), "w") as f:
        json.dump({"occurrences": ["x", "1751980800000:SolNode149", "1751980800000:SolNode149"],
                   "favoriteNodes": [3, "SolNode42"], "minutesBefore": 999}, f)
    s2 = make_sched(tmp_path)
    assert s2.alerts() == {"occurrences": ["1751980800000:SolNode149"], "favoriteNodes": ["SolNode42"],
                           "minutesBefore": 5}


def test_check_alerts_fires_once(tmp_path):
    clock = Clock(NOW + H - 2 * M)  # 2 min before the 13:00 entry
    s = make_sched(tmp_path, clock=clock)
    s.refresh()
    sent = []
    s.notify = lambda title, body, e: sent.append((title, body, e.key))
    entry = next(e for e in s.entries if e.epoch_ms == NOW + H)
    s.set_occurrence(entry.key, True)
    assert [e.key for e in s.check_alerts()] == [entry.key]
    assert sent[0][0] == "Arbitration Alert" and entry.node in sent[0][1] and "starts in ~2 min" in sent[0][1]
    assert s.check_alerts() == [] and len(sent) == 1
    raw = json.loads(open(s.store_path).read())
    assert raw["occurrences"] == [] and raw["firedKeys"] == [entry.key]
    # favourites stay armed for the next occurrence of that node
    s.set_favorite(entry.node_id, True)
    nxt = next(e for e in s.entries if e.node_id == entry.node_id and e.epoch_ms > entry.epoch_ms)
    clock.t = nxt.epoch_ms - 60_000
    assert [e.key for e in s.check_alerts()] == [nxt.key]


def test_check_alerts_respects_toggle(tmp_path):
    s = make_sched(tmp_path, clock=Clock(NOW + H - 2 * M))
    s.refresh()
    key = f"{NOW + H}:{next(e for e in s.entries if e.epoch_ms == NOW + H).node_id}"
    s.set_occurrence(key, True)
    s.notifications_enabled = lambda: False
    assert s.check_alerts() == []
    assert s.alerts()["occurrences"] == [key]  # bell not consumed


def test_rpc_arbi_schedule(app, tmp_path):
    s = make_sched(tmp_path)
    arbi.set_instance(s)
    try:
        # wire settings like setup() does, and check the favourite mirror
        arbi.setup(app)
        out = asyncio.run(rpc.call_local("world.arbi_schedule", {"days": 7}))
        assert out["entries"] and out["entries"][-1]["epochMs"] <= NOW + 7 * 24 * H
        assert out["alerts"]["minutesBefore"] == 5
        fav = asyncio.run(rpc.call_local("world.arbi_set_favorite", {"node_id": "SolNode149", "enabled": True}))
        assert "SolNode149" in fav["favoriteNodes"]
        assert "SolNode149" in app.settings.get("arbitration.favorite_nodes")
        assert ws.arbi_schedule(1)[0]["nodeId"]
        asyncio.run(rpc.call_local("world.arbi_set_favorite", {"node_id": "SolNode149", "enabled": False}))
        assert "SolNode149" not in app.settings.get("arbitration.favorite_nodes")
    finally:
        arbi.set_instance(None)


# ---- review fixes: robustness, fallbacks, backoff ----------------------------------------------------

def test_malformed_numbers_do_not_sink_the_parse():
    window = {"Activation": dl(NOW - M), "Expiry": dl(NOW + H)}
    parsed = ws.parse_raw({
        "ActiveMissions": [],
        "Alerts": [{**window, "MissionInfo": {"location": "SolNode149", "missionType": "MT_DEFENSE",
                                              "minEnemyLevel": "10.5", "maxEnemyLevel": "x",
                                              "missionReward": {"credits": "lots", "countedItems": [
                                                  {"ItemType": "/Lotus/StoreItems/Types/Items/MiscItems/Forma",
                                                   "ItemCount": "a"}]}}}],
        "Invasions": [{**window, "Node": "SolNode149", "Faction": "FC_GRINEER", "DefenderFaction": "FC_CORPUS",
                       "Goal": 100, "Count": 10,
                       "AttackerReward": {"countedItems": [{"ItemType": "/Lotus/Types/Items/Research/EnergyComponent",
                                                            "ItemCount": "3"}]}}],
        "SeasonInfo": {**window, "Season": "x", "Phase": "2", "ActiveChallenges": []},
        "KnownCalendarSeasons": [{**window, "Season": "CST_WINTER", "Days": [
            {"day": "a", "events": [{"type": "CET_UPGRADE", "upgrade": "/Lotus/Upgrades/Calendar/SomeBuff"}]}]}],
    }, NOW)
    alert = parsed["alerts"][0]
    assert (alert["minLevel"], alert["maxLevel"], alert["credits"]) == (10, 0, 0)
    assert alert["items"] == [{"name": "Forma", "count": 1}]
    assert parsed["invasions"][0]["attacker"]["reward"]["countedItems"][0]["count"] == 3
    assert (parsed["nightwave"]["season"], parsed["nightwave"]["phase"]) == (0, 2)
    assert [d["day"] for d in parsed["calendarSeason"]["days"]] == [0]
    assert ws._num("nan") == 0 and ws._num("inf", 7) == 7 and ws._num(None, 1) == 1 and ws._num(" 4 ") == 4


def test_circuit_active_week_without_choices_falls_back():
    start, end = 1_784_505_600_000, 1_785_110_400_000
    raw = {"EndlessXpSchedule": [{"Activation": dl(start), "Expiry": dl(end)}],
           "EndlessXpChoices": [{"Category": "EXC_NORMAL", "Choices": ["Excalibur"]}]}
    assert ws.parse_circuit_choices(raw, start + 1000) == [{"category": "normal", "choices": ["Excalibur"]}]


def test_stale_oracle_expiry_falls_back_to_de(monkeypatch):
    routes = full_routes()
    oracle = routes[ws.ORACLE_BOUNTY_CYCLE_URL]
    oracle["expiry"] = NOW - 2 * H          # an old answer from a cache somewhere
    routes[ws.WARFRAMESTAT_URL] = RuntimeError("blocked")
    net = FakeNet(routes)
    monkeypatch.setattr(ws, "fetch_json", net)
    out = run(net)
    assert out["cetusCycle"]["expiry"] == "2026-10-17T12:37:00.000Z"
    assert out["cambionCycle"] and out["earthCycle"]["isDay"] is False
    assert out["zarimanCycle"] is None      # a stale oracle's faction is not trusted either
    # a numeric-string oracle expiry is accepted like JS Number()
    assert ws.parse_bounty_cycle_bounties({"expiry": str(NOW + H), "bounties": {"ZarimanSyndicate": [
        {"node": "SolNode230"}]}})[0]["expiry"] == ws.iso(NOW + H)


def test_failed_refresh_backs_off(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(ws, "now_ms", lambda: clock[0])
    net = FakeNet({})
    monkeypatch.setattr(ws, "fetch_json", net)
    first = ws.state(60)
    n = len(net.calls)
    assert first["stale"] and first["error"] and n == 3
    second = ws.state(60)
    assert len(net.calls) == n and second["stale"] and second["error"] == first["error"]
    ws.state(0)                             # an explicit refresh still tries
    assert len(net.calls) == 2 * n


def test_busy_refresh_does_not_block(monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    net = FakeNet({DE: {"ActiveMissions": []}})

    def slow(url, timeout):
        entered.set()
        gate.wait(5)
        return net(url, timeout)

    monkeypatch.setattr(ws, "fetch_json", slow)
    t = threading.Thread(target=ws.state, args=(60,))
    t.start()
    assert entered.wait(2)
    start = time.monotonic()
    busy = ws.state(60)
    assert time.monotonic() - start < 0.5 and busy["refreshing"] and busy["fissures"] == []
    gate.set()
    t.join(5)
    assert "refreshing" not in ws.state(60)


def test_stale_view_rolls_cetus_forward(monkeypatch):
    clock = [NOW]
    monkeypatch.setattr(ws, "now_ms", lambda: clock[0])
    routes = full_routes()
    routes[ws.WARFRAMESTAT_URL] = RuntimeError("blocked")   # earth then derives from Cetus
    net = FakeNet(routes)
    monkeypatch.setattr(ws, "fetch_json", net)
    a = ws.state(60)
    assert a["cetusCycle"]["expiry"] == "2026-10-17T12:37:00.000Z" and "_bountyExpiry" not in a
    net.routes = {}
    clock[0] = NOW + 5 * H                  # two full 150-min cycles later: 17:00, bounty ends 17:37
    b = ws.state(0)
    assert b["stale"]
    assert b["cetusCycle"]["expiry"] == "2026-10-17T17:37:00.000Z" and b["cetusCycle"]["state"] == "night"
    assert b["cambionCycle"]["expiry"] == b["cetusCycle"]["expiry"]
    assert b["earthCycle"]["expiry"] == b["cetusCycle"]["expiry"]
    clock[0] = NOW + 5 * H + 40 * M         # 17:40: next day runs to 19:17
    c = ws.state(0)
    assert c["cetusCycle"]["isDay"] and c["cetusCycle"]["expiry"] == "2026-10-17T19:17:00.000Z"


def test_http_json_single_attempt_and_budget(monkeypatch):
    seen = []

    def fake_fetch(url, headers=None, timeout=20, retry_network=True, **kw):
        seen.append((timeout, retry_network))
        return b"{}"

    monkeypatch.setattr(ws.net, "fetch", fake_fetch)
    assert ws._http_json(DE, ws.FETCH_TIMEOUT) == {} and seen == [(ws.FETCH_TIMEOUT, False)]
    clock = [0.0]
    monkeypatch.setattr(ws.time, "monotonic", lambda: clock[0])
    timeouts = []

    def slow_fail(url, timeout):
        timeouts.append(timeout)
        clock[0] += timeout
        raise TimeoutError("timed out")

    monkeypatch.setattr(ws, "fetch_json", slow_fail)
    with pytest.raises(RuntimeError):
        ws.fetch_raw()
    assert sum(timeouts) <= ws.FETCH_BUDGET and timeouts == [20.0, 20.0, 5.0]


def test_alert_text_rounds_half_up():
    e = arbi.Entry(NOW + 150_000, "SolNode149", "Casta (Ceres)", "Defense", "Grineer")
    assert "starts in ~3 min" in arbi.alert_text(e, NOW)[1]      # 2.5 min, Math.round
    assert "starts in ~4 min" in arbi.alert_text(e, NOW - 90_000)[1]  # 4.0 min


def test_arbys_fetch_is_single_attempt(monkeypatch, tmp_path):
    seen = []

    def fake_fetch(url, headers=None, timeout=20, retry_network=True, **kw):
        seen.append((url, timeout, retry_network))
        return b"1751980800,SolNode149\n"

    monkeypatch.setattr(arbi.net, "fetch", fake_fetch)
    s = arbi.ArbiSchedule(str(tmp_path), now_ms=Clock(NOW))
    assert s.refresh() and seen == [(arbi.ARBYS_URL, arbi.FETCH_TIMEOUT, False)]


def test_setters_wait_for_the_alert_sweep_lock(tmp_path):
    s = make_sched(tmp_path)
    s.refresh()
    key = f"{NOW + H}:SolNode149"
    done = threading.Event()
    with s._lock:   # what check_alerts holds while it prunes and reassigns the lists
        t = threading.Thread(target=lambda: (s.set_occurrence(key, True), done.set()))
        t.start()
        assert not done.wait(0.2)
    t.join(2)
    assert done.is_set() and s.alerts()["occurrences"] == [key]


def test_store_writes_are_atomic_under_concurrency(tmp_path):
    s = make_sched(tmp_path)
    s.refresh()
    keys = [f"{NOW + i * H}:SolNode{i}" for i in range(1, 41)]
    threads = [threading.Thread(target=s.set_occurrence, args=(k, True)) for k in keys]
    threads += [threading.Thread(target=s.check_alerts) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    raw = json.loads(open(s.store_path).read())
    assert sorted(raw["occurrences"]) == sorted(keys)
    assert [f for f in os.listdir(tmp_path) if f.endswith(".tmp")] == []


def test_favourites_follow_settings(tmp_path):
    settings = {"favs": ["SolNode149"]}
    s = make_sched(tmp_path, clock=Clock(NOW))
    s.refresh()
    s.load_favorites = lambda: list(settings["favs"])
    s.on_favorites_changed = lambda favs: settings.update(favs=favs)
    assert s.alerts()["favoriteNodes"] == ["SolNode149"]
    s.set_favorite("SolNode167", True)
    assert settings["favs"] == ["SolNode149", "SolNode167"]
    assert json.loads(open(s.store_path).read())["favoriteNodes"] == ["SolNode149", "SolNode167"]
    # removed on a settings page: gone from alerts and from the store copy, no longer due
    settings["favs"] = ["SolNode167"]
    s.sync_favorites(settings["favs"])
    assert s.alerts()["favoriteNodes"] == ["SolNode167"]
    assert json.loads(open(s.store_path).read())["favoriteNodes"] == ["SolNode167"]
    casta = next(e for e in s.entries if e.node_id == "SolNode149" and e.epoch_ms > NOW)
    s._now = Clock(casta.epoch_ms - M)
    assert all(e.node_id != "SolNode149" for e in s.check_alerts())


def test_setup_seeds_settings_from_imported_store(app, tmp_path):
    with open(tmp_path / arbi.STORE_FILE, "w") as f:
        json.dump({"schemaVersion": 1, "favoriteNodes": ["SolNode42"], "occurrences": [], "minutesBefore": 5,
                   "firedKeys": []}, f)
    app.settings.update({"arbitration": {"favorite_nodes": []}})
    s = make_sched(tmp_path)
    arbi.set_instance(s)
    try:
        arbi.setup(app)
        assert app.settings.get("arbitration.favorite_nodes") == ["SolNode42"]
        app.settings.update({"arbitration": {"favorite_nodes": ["SolNode149"]}})
        assert s.alerts()["favoriteNodes"] == ["SolNode149"]
        assert json.loads(open(s.store_path).read())["favoriteNodes"] == ["SolNode149"]
    finally:
        arbi.set_instance(None)
        app.settings.update({"arbitration": {"favorite_nodes": []}})
