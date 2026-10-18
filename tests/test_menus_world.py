"""World + Dashboard view-models (menus/world.py, menus/dashboard.py) over the recorded world state.

World state comes from tests/fixtures/world (DE worldState + oracle + warframestat), merged by
data/worldstate.py exactly as the RPC does, pinned to NOW = 2026-10-17T12:00:00Z.
"""

import asyncio
import json
import os
from datetime import datetime, timezone

import pytest

from wfh import rpc
from wfh.data import worldstate as ws
from wfh.menus import dashboard as dash
from wfh.menus import world as wv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORLD = os.path.join(ROOT, "tests", "fixtures", "world")
NOW = int(datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
H, M, D = 3_600_000, 60_000, 86_400_000


def load(name):
    with open(os.path.join(WORLD, name), encoding="utf-8") as f:
        return json.load(f)


class FakeNet:
    def __init__(self, routes):
        self.routes = routes

    def __call__(self, url, timeout):
        v = self.routes.get(url)
        if v is None:
            raise RuntimeError(f"HTTP 404 for {url}")
        return json.loads(json.dumps(v))


ROUTES = {ws.FETCH_URLS[0]: load("worldState.json"), ws.ORACLE_BOUNTY_CYCLE_URL: load("oracle_bounty_cycle.json"),
          ws.WARFRAMESTAT_URL: load("warframestat_pc.json")}


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    monkeypatch.setattr(ws, "item_name_lookup", lambda u: None)
    monkeypatch.setattr(ws, "fetch_json", FakeNet(ROUTES))
    monkeypatch.setattr(ws, "now_ms", lambda: NOW)
    ws.reset_cache()
    yield
    ws.reset_cache()


@pytest.fixture
def state():
    return ws.fetch_and_parse(True, now=lambda: NOW)


def dl(ms):
    return {"$date": {"$numberLong": str(ms)}}


# ---- fissures --------------------------------------------------------------------------------

def test_fissure_rows_sorted_and_tagged(state):
    rows = wv.fissure_rows(state["fissures"], NOW)
    assert [(r["tier"], r["sourceMode"]) for r in rows] == [
        ("Lith", "normal"), ("Meso", "railjack"), ("Meso", "normal"), ("Neo", "railjack"), ("Axi", "steel"),
        ("Requiem", "steel")]
    assert rows[0]["expiryMs"] == ws.parse_iso("2026-10-17T12:41:00.000Z") and rows[0]["node"] == "Mantle, Earth"
    # Meso storm (13:00) before the normal Meso (13:35): tier, then expiry
    assert rows[1]["isStorm"] and rows[1]["expiryMs"] < rows[2]["expiryMs"]


def test_fissure_modes_and_tiers(state):
    rows = wv.fissure_rows(state["fissures"], NOW)
    tiers = lambda xs: [r["tier"] for r in xs]  # noqa: E731
    assert tiers(wv.filter_fissures(rows, "normal")) == ["Lith", "Meso"]
    assert tiers(wv.filter_fissures(rows, "steel")) == ["Axi", "Requiem"]
    assert tiers(wv.filter_fissures(rows, "railjack")) == ["Meso", "Neo"]
    assert tiers(wv.filter_fissures(rows, "all")) == ["Lith", "Meso", "Axi", "Requiem"]
    assert tiers(wv.filter_fissures(rows, "all", ["Axi", "Lith"])) == ["Lith", "Axi"]
    assert tiers(wv.filter_fissures(rows, "all", limit=2)) == ["Lith", "Meso"]
    assert [g["tier"] for g in wv.group_by_tier(rows)] == ["Lith", "Meso", "Neo", "Axi", "Requiem"]
    c = wv.fissure_counts(rows)
    assert c["normal"] == {"Lith": 1, "Meso": 1, "Neo": 0, "Axi": 0, "Requiem": 0, "Omnia": 0, "total": 2}
    assert c["steel"]["total"] == 2 and c["railjack"]["total"] == 2 and c["all"]["total"] == 4


def test_fissure_expiry_guard():
    f = [{"tier": "Lith", "expiry": ws.iso(NOW + 1000), "nodeId": "a"},
         {"tier": "Omnia", "expiry": ws.iso(NOW + 5000), "nodeId": "b"},
         {"tier": "Weird", "expiry": ws.iso(NOW + 4000), "nodeId": "c"},
         {"tier": "Neo", "expiry": None}, "junk"]
    rows = wv.fissure_rows(f, NOW)
    assert [r["tier"] for r in rows] == ["Omnia", "Weird"]  # unknown tiers sort last (99)


# ---- cycles / timers --------------------------------------------------------------------------

def test_cycle_rows(state):
    rows = {r["key"]: r for r in wv.cycle_rows(state, NOW)}
    assert list(rows) == ["earth", "cetus", "vallis", "cambion", "duviri", "zariman"]
    assert (rows["cetus"]["state"], rows["cetus"]["next"]) == ("Night", "Day")
    # Cetus night: 50 min window, 37 min left -> not urgent; Vallis cold: 20 min window, 13m48s left -> not urgent
    assert rows["cetus"]["urgent"] is False
    assert (rows["vallis"]["state"], rows["vallis"]["next"]) == ("Cold", "Warm")
    assert (rows["cambion"]["state"], rows["cambion"]["next"]) == ("Vome", "Fass")
    assert rows["duviri"]["state"] == "Joy" and rows["duviri"]["next"] == "Anger"
    # WFHelper never marks Duviri urgent (no window passed to isUrgent)
    assert rows["duviri"]["activationMs"] is None
    assert not any(r["urgent"] for r in wv.cycle_rows(state, rows["duviri"]["expiryMs"] - M) if r["key"] == "duviri")
    assert rows["zariman"]["next"] == "Corpus"
    # 5 min before Cetus night ends: 5/50 < 0.2 -> urgent
    late = wv.cycle_rows(state, rows["cetus"]["expiryMs"] - 5 * M)
    assert next(r for r in late if r["key"] == "cetus")["urgent"] is True
    # expired cycles drop out
    assert all(r["key"] != "cetus" for r in wv.cycle_rows(state, rows["cetus"]["expiryMs"] + 1))


def test_timers(state):
    t = {x["key"]: x for x in wv.timers(state, NOW)}
    assert t["daily"]["expiryMs"] == ws.parse_iso("2026-10-18T00:00:00.000Z")
    assert t["weekly"]["expiryMs"] == ws.parse_iso("2026-10-19T00:00:00.000Z")
    assert t["sortie"]["expiryMs"] == ws.parse_iso("2026-10-18T08:00:00.000Z")
    assert t["baro"]["label"] == "Baro leaves" and t["baro"]["expiryMs"] == ws.parse_iso("2026-10-18T16:00:00.000Z")
    assert t["varzia"]["expiryMs"] == ws.parse_iso("2026-10-19T12:00:00.000Z")
    # no sortie in the payload: falls back to the daily reset
    t2 = {x["key"]: x for x in wv.timers({**state, "sortie": None, "voidTrader": None}, NOW)}
    assert t2["sortie"]["expiryMs"] == t2["daily"]["expiryMs"] and "baro" not in t2


def test_baro_away_and_here(state):
    here = wv.baro_view(state, NOW)
    assert here["active"] and here["location"] == "Orcus Relay (Pluto)" and here["itemCount"] == 5
    away = wv.baro_view(state, ws.parse_iso("2026-10-16T10:00:00.000Z"))
    assert not away["active"] and away["inventory"] == [] and away["targetMs"] == away["activationMs"]


# ---- whole view ---------------------------------------------------------------------------------

def test_world_view_shape(state):
    v = wv.world_view(state, NOW)
    json.dumps(v)
    assert v["sortie"]["boss"] == "Lephantis" and v["sortie"]["expiryMs"]
    assert v["archon"]["boss"] == "Nira"
    inv = v["invasions"]
    assert inv[0]["attacker"] == "Grineer" and inv[0]["attackerReward"] == "Dera Vandal Barrel"
    assert inv[1]["vsInfestation"] and inv[1]["attackerReward"] == ""
    assert v["steelPath"]["current"]["name"] == "Zaw Riven Mod"
    assert [u["inDays"] for u in v["steelPath"]["upcoming"]] == [2, 9]
    assert v["circuit"]["normal"]["choices"] == ["Saryn", "Vauban", "Nova"]
    assert v["circuit"]["hard"]["next"][0] == ["Torid", "Dual Toxocyst", "Dual Ichor", "Miter", "Atomos"]
    nw = v["nightwave"]
    assert nw["season"] == 18 and nw["challenges"]
    kinds = [c["kind"] for c in nw["challenges"]]
    assert kinds == sorted(kinds, key=["daily", "weekly", "elite"].index)
    assert v["darvo"][0]["salePrice"] == 75


def test_empty_state_survives():
    v = wv.world_view(ws.empty_world_state(NOW), NOW)
    json.dumps(v)
    assert v["fissures"] == [] and v["baro"] is None and v["sortie"] is None
    assert [c["key"] for c in v["cycles"]] == ["vallis"]  # computed from the clock alone
    assert v["circuit"]["normal"] == {"choices": [], "next": [], "known": False}


def test_qam_view(state):
    prefs = {"fissureMode": "all", "fissureTiers": ["Lith", "Axi", "Requiem"], "qamLimit": 2}
    sched = {"now": NOW, "entries": [{"epochMs": NOW + H, "nodeId": "SolNode149", "node": "Casta (Ceres)"},
                                     {"epochMs": NOW + 3 * H, "nodeId": "SolNode167", "node": "Oestrus (Eris)"}],
             "alerts": {"favoriteNodes": ["SolNode167"]}, "current": None,
             "next": {"epochMs": NOW + H, "nodeId": "SolNode149"}}
    q = wv.qam_view(state, NOW, prefs, sched)
    assert [f["tier"] for f in q["fissures"]] == ["Lith", "Axi"]
    assert [r["key"] for r in q["resets"]] == ["daily", "weekly", "sortie"]
    assert q["arbitration"]["nextFavorite"]["nodeId"] == "SolNode167"
    assert q["baro"]["active"]
    assert wv.qam_view(state, NOW, prefs, None)["arbitration"]["next"] is None


# ---- dailies ---------------------------------------------------------------------------------------

def test_period_keys(state):
    exp = wv.tracker_expiries(state)
    assert wv.period_key("daily", NOW, exp) == "daily:2026-10-18T00:00:00.000Z"
    assert wv.period_key("weekly", NOW, exp) == "weekly:2026-10-19T00:00:00.000Z"
    assert wv.period_key("sortie", NOW, exp) == "sortie:2026-10-18T08:00:00.000Z"
    assert wv.period_key("baro", NOW, exp) == "baro:2026-10-16T16:00:00.000Z"
    assert wv.period_key("descendia", NOW, wv.tracker_expiries({})) is None
    nxt = wv.next_vendor_rotation_ms(NOW, "tenet")
    assert 0 < nxt - NOW <= 4 * D and (nxt - wv.VENDOR_ROTATION_ANCHORS["tenet"]) % (4 * D) == 0


def test_auto_tracker(state):
    day_start = ws.parse_iso("2026-10-17T00:00:00.000Z")
    inv = {
        "LastSortieReward": [{"SortieId": {"$oid": "old"}}, {"SortieId": {"$oid": state["sortie"]["id"]}}],
        "LastLiteSortieReward": [{"SortieId": {"$oid": "last-week"}}],
        "EntratiVaultCountLastPeriod": 3, "EntratiVaultCountResetDate": dl(NOW + 2 * D),
        "EntratiLabConquestCacheScoreMission": 12,
        "DailyAffiliation": 0, "DailyFocus": 125000,
        "LibraryActiveDailyTaskInfo": {"Scans": 1, "ScansRequired": 3},
        "PeriodicMissionCompletions": [
            {"tag": "GetClemWeekly", "date": dl(NOW - D)},
            {"tag": "TreasureHuntB", "date": dl(NOW - 30 * D)},  # last month: not this week
            {"tag": "HardDaily1", "date": dl(day_start + H)}, {"tag": "HardDaily2", "date": dl(day_start + 2 * H)},
            {"tag": "HardDaily3", "date": dl(day_start - H)}],
        "Affiliations": [{"Tag": "KahlSyndicate", "WeeklyMissions": [
            {"WeekCount": (NOW - wv.KAHL_EPOCH_MS) // (7 * D), "CompletedMission": True}]}],
    }
    a = wv.auto_tracker(inv, state, NOW, NOW - H)
    assert a["sortie"] == {"count": 1} and "archonHunt" not in a
    assert a["netracells"] == {"count": 3}
    assert a["deepArchimedea"]["count"] == 0 and a["deepArchimedea"]["detail"] == "Score 12"
    assert a["syndicateStanding"] == {"count": 1}
    assert a["dailyFocus"] == {"count": 0, "detail": "125,000 focus left"}
    assert a["simaris"] == {"count": 0, "detail": "1/3 scans"}
    assert a["clem"] == {"count": 1} and "ayatanHunt" not in a
    assert a["spIncursions"] == {"count": 2}
    assert a["kahl"] == {"count": 1}
    # inventory synced yesterday: the point-in-time pools say nothing about today
    b = wv.auto_tracker(inv, state, NOW, day_start - 1)
    assert "syndicateStanding" not in b and "simaris" not in b
    # netracell count from a past reset window is zero
    inv["EntratiVaultCountResetDate"] = dl(NOW - 1)
    assert wv.auto_tracker(inv, state, NOW, None)["netracells"] == {"count": 0}
    assert wv.auto_tracker(None, state, NOW, None) == {}


def test_dailies_store_and_view(state, tmp_path):
    s = wv.DailiesStore(str(tmp_path))
    exp = wv.tracker_expiries(state)
    s.set("spIncursions", wv.period_key("daily", NOW, exp), 3)
    s.set("baro", wv.period_key("baro", NOW, exp), 1)
    s.set_hidden("yonta", True)
    v = wv.dailies_view(s, state, {"LastSortieReward": [{"SortieId": {"$oid": state["sortie"]["id"]}}]}, NOW, NOW)
    by = {r["id"]: r for r in v["tasks"]}
    assert by["spIncursions"]["count"] == 3 and not by["spIncursions"]["done"]
    assert by["baro"]["done"] and by["sortie"]["done"] and by["sortie"]["auto"]
    assert by["yonta"]["hidden"] and v["visibleCount"] == len(wv.DAILY_TASKS) - 1
    assert by["spIncursions"]["resetMs"] == ws.parse_iso("2026-10-18T00:00:00.000Z")
    # persisted and revived; next day the daily count resets, Baro (same visit) stays
    s2 = wv.DailiesStore(str(tmp_path))
    v2 = wv.dailies_view(s2, state, None, None, NOW + D)
    by2 = {r["id"]: r for r in v2["tasks"]}
    assert by2["spIncursions"]["count"] == 0 and by2["baro"]["done"] and by2["yonta"]["hidden"]
    # corrupt file -> empty store, no crash
    with open(s.path, "w") as f:
        f.write("{nope")
    assert wv.DailiesStore(str(tmp_path)).data == {"progress": {}, "hidden": []}


# ---- dashboard ---------------------------------------------------------------------------------

def test_foundry_summary_fallback(monkeypatch):
    inv = {"PendingRecipes": [
        {"ItemType": "/Lotus/Types/Recipes/WarframeRecipes/MesaPrimeHelmetBlueprint", "CompletionDate": dl(NOW - H)},
        {"ItemType": "/Lotus/Types/Recipes/Components/FormaBlueprint", "CompletionDate": dl(NOW - 2 * H)},
        {"ItemType": "/Lotus/Types/Recipes/Weapons/BratonBlueprint", "CompletionDate": dl(NOW + 3 * H)},
        {"nope": 1}]}
    names = {"/Lotus/Types/Recipes/Components/FormaBlueprint": "Forma Blueprint"}
    monkeypatch.setattr(ws, "item_name_lookup", names.get)
    f = dash.foundry_summary(inv, NOW)
    assert (f["claimable"], f["building"], f["ready"]) == (2, 1, None)
    assert [(r["name"], r["status"]) for r in f["rows"]] == [
        ("Forma", "claimable"), ("Mesa Prime Helmet", "claimable"), ("Braton", "in-progress")]
    assert f["nextReadyMs"] == NOW + 3 * H
    assert dash.foundry_summary(None, NOW) is None
    assert dash.foundry_summary({}, NOW)["rows"] == []


def test_foundry_from_qam():
    # menus/foundry.qam_view output: claimable, in progress, then ready to build (incl. chain-buildable)
    q = {"hasInventory": True, "now": NOW, "counts": {"claimable": 1, "building": 1, "ready": 3, "missing": 4},
         "rows": [{"key": "a", "name": "Forma", "status": "claimable", "endMs": NOW - 1, "count": None},
                  {"key": "b", "name": "Mesa Prime Helmet", "status": "in-progress", "endMs": NOW + H, "count": None},
                  {"key": "c", "name": "Mesa Prime", "status": "ready-to-build", "endMs": None, "count": 1,
                   "chainOnly": True},
                  {"key": "d", "name": "Forma", "status": "ready-to-build", "endMs": None, "count": 3}],
         "nextReadyMs": NOW + H, "pinnedTotals": {}}
    f = dash.foundry_from_qam(q, 3)
    assert (f["claimable"], f["building"], f["ready"]) == (1, 1, 3)
    assert [r["status"] for r in f["rows"]] == ["claimable", "in-progress", "ready-to-build"]
    assert f["rows"][2]["chainOnly"] is True and f["nextReadyMs"] == NOW + H
    assert dash.foundry_from_qam({"hasInventory": False}) is None and dash.foundry_from_qam(None) is None


def test_last_mission(monkeypatch):
    from wfh.menus import missions

    class T:
        history = [{"id": "old", "endedAt": NOW - 2 * H, "items": [], "credits": 10},
                   {"id": "new", "endedAt": NOW - H, "items": [], "credits": 12_000, "node": "SolNode1",
                    "missionType": "MT_CAPTURE", "missionCount": 1}]

    monkeypatch.setattr(missions, "tracker", lambda: T())
    m = dash._last_mission()
    assert m["id"] == "new" and m["endedAt"] == NOW - H and m["credits"] == 12_000 and m["rows"] == []
    assert m["missionTypeLabel"]
    monkeypatch.setattr(missions, "tracker", lambda: None)
    assert dash._last_mission() is None
    v = {"id": "x", "endedAt": 1, "platinum": 38, "ducats": 45, "itemCount": 6,
         "rows": [{"name": f"R{i}", "count": 1, "platinum": 1, "ducats": None, "slug": "s"} for i in range(6)]}
    s = dash.mission_summary(v)
    assert len(s["rows"]) == 4 and "slug" not in s["rows"][0] and s["platinum"] == 38


def test_last_arbi_newest_by_start(monkeypatch):
    import threading
    import types

    from wfh.features import arbitration

    t = types.SimpleNamespace(_lock=threading.Lock(), runs=[
        {"id": "live", "startedAt": NOW - H}, {"id": "imported-newer", "startedAt": NOW},
        {"id": "dup", "startedAt": NOW + H, "duplicateOf": "live"}, {"id": "imported-old", "startedAt": NOW - D}])
    monkeypatch.setattr(arbitration, "controller", lambda: types.SimpleNamespace(tracker=t))
    last, n = dash._last_arbi()
    assert last["id"] == "imported-newer" and n == 3


def test_reward_and_arbi_summaries():
    r = dash.reward_summary({"at": 1.0, "best_plat": 1, "items": [
        {"slot": 0, "name": "Forma Blueprint", "plat": None, "ducats": None},
        {"slot": 1, "name": "Ash Prime Systems", "plat": 25, "ducats": 45, "owned": 0}]})
    assert r["best"] == "Ash Prime Systems" and r["bestPlat"] == 25 and len(r["items"]) == 2
    assert dash.reward_summary({"items": [], "failure": "no cards"}) is None
    a = dash.arbi_run_summary({"id": "x", "node": "Casta (Ceres)", "rotations": 8, "logFile": "big"})
    assert a["rotations"] == 8 and "logFile" not in a


def test_build_summary(state, tmp_path):
    ui = {"fissureMode": "normal", "fissureTiers": list(wv.TIERS), "qamLimit": 5}
    p = {"fissureLimit": 1, "foundryLimit": 5, "hidden": []}
    dailies = wv.dailies_view(wv.DailiesStore(str(tmp_path)), state, None, None, NOW)
    out = dash.build_summary(state, NOW, ui, p, None, None, None, None, None, 0, dailies)
    json.dumps(out)
    assert out["lastMission"] is None
    lm = {"id": "m", "endedAt": NOW, "rows": []}
    assert dash.build_summary(state, NOW, ui, p, None, None, None, None, None, 0, dailies,
                              last_mission=lm)["lastMission"] is lm
    assert [f["tier"] for f in out["fissures"]] == ["Lith"]
    assert out["foundry"] is None and out["inventory"]["available"] is False
    assert out["baro"]["active"] and "inventory" not in out["baro"] and len(out["baroItems"]) == 4
    assert out["dailies"]["sortieDone"] is False and out["sortie"]["boss"] == "Lephantis"
    assert [r["key"] for r in out["resets"]][:3] == ["daily", "weekly", "sortie"]


def test_rpcs(app, tmp_path):
    for m in ("world.view", "world.qam", "world.ui_set", "world.dailies", "world.dailies_set", "world.dailies_hide",
              "dashboard.summary", "dashboard.configure"):
        assert m in rpc.methods(), m
    v = asyncio.run(rpc.call_local("world.view", {}))
    assert v["sortie"]["boss"] == "Lephantis" and v["prefs"]["fissureMode"] in wv.FISSURE_MODES
    p = asyncio.run(rpc.call_local("world.ui_set", {"fissure_mode": "steel", "fissure_tiers": ["Axi", "Bogus"],
                                                    "qam_limit": 99}))
    assert p == {"fissureMode": "steel", "fissureTiers": ["Axi"], "qamLimit": 20}
    q = asyncio.run(rpc.call_local("world.qam", {}))
    assert [f["tier"] for f in q["fissures"]] == ["Axi"]
    asyncio.run(rpc.call_local("world.ui_set", {"fissure_mode": "bogus"}))
    assert app.settings.get("world_ui.fissure_mode") == "steel"
    asyncio.run(rpc.call_local("world.ui_set", {"fissure_mode": "normal", "fissure_tiers": list(wv.TIERS),
                                                "qam_limit": 5}))

    d = asyncio.run(rpc.call_local("world.dailies_set", {"id": "clem", "count": 1}))
    assert next(t for t in d["tasks"] if t["id"] == "clem")["done"]
    d = asyncio.run(rpc.call_local("world.dailies_hide", {"id": "clem", "hidden": True}))
    assert next(t for t in d["tasks"] if t["id"] == "clem")["hidden"]
    with pytest.raises(ValueError):
        asyncio.run(rpc.call_local("world.dailies_set", {"id": "nope"}))
    asyncio.run(rpc.call_local("world.dailies_hide", {"id": "clem", "hidden": False}))

    c = asyncio.run(rpc.call_local("dashboard.configure", {"fissure_limit": 0, "hidden": ["baro", "x"]}))
    assert c["fissureLimit"] == 1 and c["hidden"] == ["baro"]
    s = asyncio.run(rpc.call_local("dashboard.summary", {}))
    json.dumps(s)
    assert len(s["fissures"]) == 1 and s["cycles"] and s["prefs"]["hidden"] == ["baro"]
    asyncio.run(rpc.call_local("dashboard.configure", {"fissure_limit": 5, "hidden": []}))


def test_foundry_wiring_and_fallback(monkeypatch):
    import types

    from wfh.menus import foundry as fm
    from wfh.menus import inventory as inv_menu

    app = types.SimpleNamespace(settings={"foundry.pinned": ["x"]})
    seen = {}

    def qam_view(snap, now, pinned, limit):
        seen.update(snap=snap, pinned=pinned, limit=limit)
        return {"hasInventory": True, "counts": {"claimable": 0, "building": 0, "ready": 1},
                "rows": [{"name": "Forma", "status": "ready-to-build", "endMs": None, "count": 2}], "nextReadyMs": None}

    monkeypatch.setattr(fm, "pinned_list", lambda s: ["x"])
    monkeypatch.setattr(fm, "qam_view", qam_view)
    monkeypatch.setattr(inv_menu, "current_snapshot", lambda: "SNAP")
    f = dash._foundry(app, {"PendingRecipes": []}, NOW, 4)
    assert f["source"] == "foundry" and f["ready"] == 1 and seen == {"snap": "SNAP", "pinned": ["x"], "limit": 4}

    def boom():
        raise RuntimeError("no item data")

    monkeypatch.setattr(inv_menu, "current_snapshot", boom)
    f = dash._foundry(app, {"PendingRecipes": []}, NOW, 4)
    assert f["source"] == "pending" and f["ready"] is None
