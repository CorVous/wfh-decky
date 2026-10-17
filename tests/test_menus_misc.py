"""Stats, Wiki and Settings view-models (menus/stats.py, menus/wiki.py, menus/settings_view.py)."""

import asyncio
import json
import os
import time

import pytest

from wfh import rpc
from wfh.menus import settings_view as sv
from wfh.menus import stats as st
from wfh.menus import wiki as wk
from wfh.settings import Settings

MISC = "/Lotus/Types/Items/MiscItems/"


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def local_ts(y, m, d, hh=12):
    return time.mktime((y, m, d, hh, 0, 0, 0, 0, -1))


def inv(plat=100, credits=1_000_000, endo=5000, ducats=None, kuva=None, relics=10):
    misc = []
    if ducats is not None:
        misc.append({"ItemType": MISC + "PrimeBucks", "ItemCount": ducats})
    if kuva is not None:
        misc.append({"ItemType": MISC + "Kuva", "ItemCount": kuva})
    return {"PremiumCredits": plat, "RegularCredits": credits, "FusionPoints": endo, "MiscItems": misc,
            "LevelKeys": [{"ItemType": "/Lotus/Types/Game/Projections/T1VoidProjectionGBronze", "ItemCount": relics}]}


# ---- stats -------------------------------------------------------------------------------------

def test_read_amounts_misc_absent_is_zero_but_missing_array_reports_nothing():
    a = st.read_amounts(inv(ducats=40))
    assert a["plat"] == 100 and a["ducats"] == 40 and a["kuva"] == 0 and a["credits"] == 1_000_000
    b = st.read_amounts({"PremiumCredits": 5})
    assert b == {"plat": 5}


def test_daily_deltas_relics_and_restart(tmp_path):
    clock = Clock(local_ts(2026, 10, 17, 10))
    t = st.StatsTracker(str(tmp_path), clock)
    t.on_inventory(inv(plat=100, credits=1_000_000, endo=5000, ducats=10, relics=10))
    clock.t += 3600
    t.on_inventory(inv(plat=145, credits=1_250_000, endo=5600, ducats=85, relics=7))
    clock.t += 60
    t.on_inventory(inv(plat=130, credits=1_300_000, endo=5600, ducats=85, relics=8))  # bought a relic
    today = t.today()
    assert today["date"] == "2026-10-17"
    assert today["resources"]["plat"] == {"delta": 30, "current": 130}
    assert today["resources"]["credits"]["delta"] == 300_000
    assert today["resources"]["ducats"]["delta"] == 75
    assert today["relicsOpened"] == 3

    # restart: today's deltas resume, the next payload re-baselines on top
    t2 = st.StatsTracker(str(tmp_path), clock)
    assert t2.today()["resources"]["plat"]["delta"] == 30
    t2.on_inventory(inv(plat=130, credits=1_300_000, endo=5600, ducats=85, relics=8))
    t2.on_inventory(inv(plat=150, credits=1_300_000, endo=5600, ducats=85, relics=8))
    assert t2.today()["resources"]["plat"]["delta"] == 50
    assert t2.today()["relicsOpened"] == 3

    # a snapshot persisted yesterday never becomes today's baseline
    clock.t = local_ts(2026, 10, 18, 9)
    t2.on_inventory(inv(plat=150), synced_at=local_ts(2026, 10, 17, 23))
    assert t2.today()["resources"]["plat"]["delta"] == 0
    t2.on_inventory(inv(plat=170))
    t2.on_inventory(inv(plat=200))
    assert t2.today()["resources"]["plat"]["delta"] == 30

    s = t2.series(["plat", "credits"], 3)
    assert s["dates"] == ["2026-10-16", "2026-10-17", "2026-10-18"]
    assert s["values"]["plat"] == [0, 50, 30]
    assert s["played"] == [False, True, True]
    with open(tmp_path / st.HISTORY_FILE) as f:
        saved = json.load(f)
    assert saved["schemaVersion"] == 2 and [e["date"] for e in saved["entries"]] == ["2026-10-17", "2026-10-18"]
    assert saved["entries"][0]["platDelta"] == 50


GLYPH = ""
DIALOG_HEAD = ("2416.657 Script [Info]: Dialog.lua: Dialog::CreateOkCancel(description="
               "Are you sure you want to accept this trade? You are offering:")
DIALOG_TAIL = ", title= leftItem=/Menu/Confirm_Item_Ok, rightItem=/Menu/Confirm_Item_Cancel)"
NEXT_ENTRY = "2416.702 Sys [Info]: Fade out complete"
SUCCESS = ("2420.118 Script [Info]: Dialog.lua: Dialog::CreateOk(description=The trade was successful!"
           ", title= leftItem=/Menu/Confirm_Item_Ok)")


def test_trade_dialog_parse_cases():
    p = st.parse_trade_dialog(["Are you sure you want to accept this trade?", "You are offering:",
                               "Arcane Pistoleer x 3", "Vitus Essence", "Vitus Essence x 2",
                               "and will receive from Kestrelî\u0080\u0080 the following:",
                               "Platinum x 45, leftItem=/Menu/Confirm_Item_Ok"])
    assert p["type"] == "sale" and p["platChange"] == 45 and p["partner"] == "Kestrel"
    assert {i["name"]: i["count"] for i in p["items"]} == {"Arcane Pistoleer": 3, "Vitus Essence": 3}
    buy = st.parse_trade_dialog(["Are you sure you want to accept this trade?", "You are offering:",
                                 "Platinum x 20", "and will receive from Seller the following:",
                                 "Lith A1 Relic [Radiant]", "leftItem=/Menu/Confirm_Item_Ok"])
    assert buy["type"] == "purchase" and buy["platChange"] == 20
    assert buy["items"] == [{"name": "Lith A1 Relic [Radiant]", "count": 1, "direction": "received"}]
    assert st.parse_trade_dialog(["no dialog"]) is None


def test_trade_watcher_live_lines_and_ledger(tmp_path):
    clock = Clock(local_ts(2026, 10, 17, 12))
    t = st.StatsTracker(str(tmp_path), clock)
    got = []
    w = st.TradeDialogWatcher(got.append, clock=lambda: 0.0)
    # The reported Vicious Bond sale: bare CRs, next entry glued behind the platinum line.
    for line in [DIALOG_HEAD, "\rVicious Bond (RARE RANK 0)", "",
                 "and will receive from RendiW" + GLYPH + " the following:",
                 "\rPlatinum x 12" + DIALOG_TAIL + "\r" + NEXT_ENTRY,
                 "2417.000 Sys [Info]: unrelated", SUCCESS]:
        w.feed(line)
    assert len(got) == 1
    trade = got[0]
    assert trade["type"] == "sale" and trade["platChange"] == 12 and trade["partner"] == "RendiW"
    assert trade["items"] == [{"name": "Vicious Bond (RARE RANK 0)", "count": 1, "direction": "given"}]
    assert trade["stamp"] == "2416.657"
    # success without a dialog does nothing
    w.feed(SUCCESS)
    assert len(got) == 1

    assert t.add_trade(trade) is not None
    assert t.add_trade(dict(trade)) is None  # same trade within 30 s
    clock.t += 120
    t.add_trade({"type": "purchase", "platChange": 5, "partner": "X", "items": []})
    flow = st.trade_flow(t.trades_since("2026-10-17"))
    assert flow == {"count": 2, "sales": 1, "purchases": 1, "swaps": 0, "platIn": 12, "platOut": 5, "net": 7}
    assert t.today()["dailyTrades"] == 2
    page = t.trade_page(0, 1)
    assert page["total"] == 2 and page["rows"][0]["partner"] == "X"
    assert t.delete_trade(page["rows"][0]["id"])
    t3 = st.StatsTracker(str(tmp_path), clock)
    assert len(t3.trades) == 1 and t3.today()["dailyTrades"] == 2


def test_build_view_and_qam(tmp_path):
    clock = Clock(local_ts(2026, 10, 17, 10))
    t = st.StatsTracker(str(tmp_path), clock)
    t.on_inventory(inv(plat=10, credits=2_000_000, kuva=1000))
    t.on_inventory(inv(plat=10, credits=2_450_000, kuva=4000))
    v = st.build_view(t, ["credits", "kuva", "bogus"], 7)
    assert v["chart"] == ["credits", "kuva"]
    assert len(v["series"]["dates"]) == 7 and v["series"]["values"]["credits"][-1] == 450_000
    strip = {s["id"]: s for s in v["strip"]}
    assert strip["credits"]["currentText"] == "2.5M" and strip["kuva"]["deltaText"] == "3k"
    assert v["totals"]["kuva"] == 3000
    q = st.qam_view(t)
    assert [r["id"] for r in q["rows"]] == st.QAM_IDS and q["hasData"]
    json.dumps(v), json.dumps(q)


def test_stats_rpcs_registered(app):
    for name in ("stats.view", "stats.qam", "stats.trades", "stats.configure", "stats.status",
                 "wiki.search", "wiki.relic", "wiki.relics", "settings.view", "settings.set",
                 "settings.diagnostics", "settings.proton_plan", "settings.check_path", "settings.reset"):
        assert name in rpc.methods(), name
    assert "wfh.menus" not in app.services["failed_modules"]
    q = run(rpc.call_local("stats.qam"))
    assert "rows" in q
    v = run(rpc.call_local("stats.view", {"days": 14}))
    assert v["days"] == 14 and len(v["series"]["dates"]) == 14
    cfg = run(rpc.call_local("stats.configure", {"chart": ["plat", "nope"], "days": 999}))
    assert cfg["chart"] == ["plat"] and cfg["days"] == 365
    app.settings.update({"stats": {"chart": st.DEFAULT_CHART, "days": 30}})


# ---- wiki --------------------------------------------------------------------------------------

def test_wiki_search_item_and_relic(app):
    out = run(rpc.call_local("wiki.search", {"query": "ash prime systems", "limit": 50}))
    assert out["total"] >= 1
    assert all("Ash Prime Systems" in r["item"] for r in out["rows"])
    # unvaulted relics first, then by intact chance
    flags = [r["vaulted"] for r in out["rows"]]
    assert flags == sorted(flags)
    r0 = out["rows"][0]
    assert set(r0["chances"]) == {"intact", "exceptional", "flawless", "radiant"}
    assert r0["rarity"] in ("Common", "Uncommon", "Rare")
    summ = out["items"][0]
    assert summ["item"] == "Ash Prime Systems Blueprint" and summ["wikiUrl"].endswith("/Ash_Prime")
    assert summ["ducats"] == 65 and summ["relics"] == out["total"]

    rel = run(rpc.call_local("wiki.search", {"query": "Lith A1 relic", "mode": "relic"}))
    assert rel["rows"][0]["relic"] == "Lith A1"
    assert {r["relic"] for r in rel["rows"][:6]} == {"Lith A1"}

    unv = run(rpc.call_local("wiki.search", {"query": "prime", "unvaulted_only": True, "limit": 300}))
    assert unv["rows"] and not any(r["vaulted"] for r in unv["rows"])

    d = run(rpc.call_local("wiki.relic", {"name": "lith a1"}))
    assert d["name"] == "Lith A1" and len(d["rewards"]) == 6
    assert [r["rarity"] for r in d["rewards"]] == ["Common"] * 3 + ["Uncommon"] * 2 + ["Rare"]
    assert d["wikiUrl"] == "https://wiki.warframe.com/w/Lith_A1_Relic"
    assert run(rpc.call_local("wiki.relic", {"name": "nope"})) is None

    lst = run(rpc.call_local("wiki.relics", {"tier": "Axi", "unvaulted_only": False}))
    names = [r["name"] for r in lst]
    assert names and all(n.startswith("Axi ") for n in names)


def test_wiki_scoring_and_recent(app):
    rows = [
        {"item": "Forma Blueprint", "relic": "Lith A2", "tier": "Lith", "vaulted": False, "rarity": "Common",
         "chances": {"intact": 25.33}, "_item": "forma blueprint", "_relic": "lith a2"},
        {"item": "Braton Prime Blueprint", "relic": "Lith A10", "tier": "Lith", "vaulted": True, "rarity": "Rare",
         "chances": {"intact": 2}, "_item": "braton prime blueprint", "_relic": "lith a10"},
        {"item": "Ash Prime Blueprint", "relic": "Lith B1", "tier": "Lith", "vaulted": False, "rarity": "Rare",
         "chances": {"intact": 2}, "_item": "ash prime blueprint", "_relic": "lith b1"},
    ]
    got, total = wk.search_rows(rows, "blueprint")
    assert total == 3 and got[0]["item"] == "Forma Blueprint"  # unvaulted, higher chance first
    got, _ = wk.search_rows(rows, "prime")
    assert [r["item"] for r in got] == ["Ash Prime Blueprint", "Braton Prime Blueprint"]
    got, _ = wk.search_rows(rows, "lith a", mode="relic")
    assert [r["relic"] for r in got] == ["Lith A2", "Lith A10"]
    assert wk.wiki_page_for("Forma Blueprint") == "https://wiki.warframe.com/w/Forma"

    app.settings.update({"wiki": {"recent": []}})
    run(rpc.call_local("wiki.search", {"query": "forma", "record": True}))
    run(rpc.call_local("wiki.search", {"query": "Lith A1", "mode": "relic", "record": True}))
    run(rpc.call_local("wiki.search", {"query": "Forma", "record": True}))
    assert run(rpc.call_local("wiki.recent")) == [{"q": "Forma", "mode": "item"}, {"q": "Lith A1", "mode": "relic"}]
    run(rpc.call_local("wiki.search", {"query": "zzzzqqq", "record": True}))  # no hits: not recorded
    assert len(run(rpc.call_local("wiki.recent"))) == 2
    assert run(rpc.call_local("wiki.recent_clear")) == []


# ---- settings ----------------------------------------------------------------------------------

def test_settings_schema_keys_exist_in_defaults():
    for key, f in sv.FIELDS.items():
        for k in [key, *f["also"]]:
            assert sv.default_of(k) is not None, k
        d = sv.default_of(key)
        if f["kind"] == "dropdown":
            assert d in [o["value"] for o in f["options"]], key
        if f["kind"] == "slider":
            assert f["min"] <= d <= f["max"], key


def test_settings_set_validates_and_writes_also(tmp_path):
    s = Settings(str(tmp_path / "settings.json"))
    assert sv.set_value(s, "relic_rewards.duration_s", 99)["value"] == 30
    assert sv.set_value(s, "overlay_opacity", 0.1)["value"] == 0.3
    assert sv.set_value(s, "overlay_opacity", 0.83)["value"] == 0.85
    assert sv.set_value(s, "relic_planner.squad_size", 2.4)["value"] == 2
    sv.set_value(s, "relic_rewards.ui_scale", 0.875)
    assert s.get("riven_scanner.ui_scale") == s.get("relic_rewards.ui_scale") == 0.88
    sv.set_value(s, "relic_rewards.ui_scale_auto", False)
    assert s.get("riven_scanner.ui_scale_auto") is False
    with pytest.raises(ValueError):
        sv.set_value(s, "wfm_platform", "amiga")
    with pytest.raises(ValueError):
        sv.set_value(s, "overlays_enabled", "yes")
    with pytest.raises(ValueError):
        sv.set_value(s, "not.a.setting", 1)
    sv.set_value(s, "ee_log_path", "  /tmp/EE.log ")
    assert s.get("ee_log_path") == "/tmp/EE.log"
    # untouched siblings survive
    assert s.get("relic_rewards.show_ducats") is True
    view = sv.reset_section(s, "ui_scale")
    assert s.get("riven_scanner.ui_scale_auto") is True and s.get("relic_rewards.ui_scale") == 0.99
    field = next(f for sec in view["sections"] for f in sec["fields"] if f["key"] == "overlay_opacity")
    assert field["value"] == 0.85 and field["default"] == 1.0


def test_compose_launch_options():
    c = sv.compose_launch_options
    assert c("")["proposed"] == "PROTON_LOG=1 %command%"
    assert c(None)["proposed"] == "PROTON_LOG=1 %command%"
    assert c("gamemoderun %command% -dx11")["proposed"] == "PROTON_LOG=1 gamemoderun %command% -dx11"
    assert c("-dx11 -fullscreen")["proposed"] == "PROTON_LOG=1 %command% -dx11 -fullscreen"
    same = c("PROTON_LOG=1 DXVK_HUD=fps %command%")
    assert same["alreadySet"] and same["proposed"] == "PROTON_LOG=1 DXVK_HUD=fps %command%"
    assert c("PROTON_LOG=0 %command%")["proposed"] == "PROTON_LOG=1 %command%"
    assert not c("PROTON_LOG=0 %command%")["alreadySet"]


def test_check_path_and_diagnostics(app, tmp_path):
    f = tmp_path / "EE.log"
    f.write_text("x" * 10)
    out = sv.check_path(str(f), "/home/deck")
    assert out["exists"] and out["isFile"] and out["size"] == 10
    assert sv.check_path("~/nope.json", str(tmp_path))["path"] == str(tmp_path / "nope.json")
    assert not sv.check_path("", "/home/deck")["exists"]
    r = run(rpc.call_local("settings.check_path", {"kind": "inventory_file", "path": str(f)}))
    assert r["exists"]
    d = run(rpc.call_local("settings.diagnostics"))
    assert set(d) >= {"core", "eelog", "inventory", "data"}
    assert "version" in d["core"] and "items" in d["data"]
    plan = run(rpc.call_local("settings.proton_plan", {"current": "-dx11"}))
    assert plan["proposed"] == "PROTON_LOG=1 %command% -dx11" and plan["appId"] == 230410 and plan["valid"]
    view = run(rpc.call_local("settings.view"))
    assert [s["id"] for s in view["sections"]][0] == "overlays"


def test_identical_trades_with_distinct_stamps_both_recorded(tmp_path):
    clock = Clock(local_ts(2026, 10, 17, 12))
    t = st.StatsTracker(str(tmp_path), clock)
    base = {"type": "sale", "platChange": 15, "partner": "Buyer",
            "items": [{"name": "Primed Flow", "count": 1, "direction": "given"}]}
    assert t.add_trade({**base, "stamp": "100.000"}) is not None
    clock.t += 5
    # A second, separate trade with the same buyer: different log stamp.
    assert t.add_trade({**base, "stamp": "140.500"}) is not None
    # Echo of the second one (same stamp), or one without a stamp: duplicates.
    assert t.add_trade({**base, "stamp": "140.500"}) is None
    assert t.add_trade({**base, "partner": "BUYER", "stamp": None}) is None
    # Item order does not change the signature.
    two = {**base, "items": [{"name": "A", "count": 1, "direction": "given"},
                             {"name": "B", "count": 2, "direction": "received"}], "stamp": "200.0"}
    assert t.add_trade(two) is not None
    assert t.add_trade({**two, "items": list(reversed(two["items"]))}) is None
    assert len(t.trades) == 3 and t.today()["dailyTrades"] == 3


def test_legacy_flat_history_rows_chart_and_resume(tmp_path):
    clock = Clock(local_ts(2026, 10, 17, 12))
    rows = [{"date": "2026-10-16", "platDelta": 40, "creditsDelta": 120000, "absPlat": 500},
            {"date": "2026-10-17", "platDelta": 7, "endoDelta": 300, "absEndo": 9000}]
    (tmp_path / st.HISTORY_FILE).write_text(json.dumps({"entries": rows}))
    t = st.StatsTracker(str(tmp_path), clock)
    s = t.series(["plat", "credits", "endo"], 2)
    assert s["values"] == {"plat": [40, 7], "credits": [120000, 0], "endo": [0, 300]}
    today = t.today()["resources"]
    assert today["plat"]["delta"] == 7 and today["endo"]["delta"] == 300 and today["endo"]["current"] == 9000
    assert st.read_day({"resources": {"plat": {"delta": 3}}, "platDelta": 9}, "plat") == {"delta": 3}
    assert st.read_day({"date": "x"}, "kuva") is None


def test_trade_page_clamps_past_the_end(tmp_path):
    clock = Clock(local_ts(2026, 10, 17, 12))
    t = st.StatsTracker(str(tmp_path), clock)
    for i in range(3):
        clock.t += 60
        t.add_trade({"type": "purchase", "platChange": i + 1, "partner": f"P{i}", "items": []})
    p = t.trade_page(2, 2)
    assert p["offset"] == 2 and len(p["rows"]) == 1
    t.delete_trade(p["rows"][0]["id"])
    p = t.trade_page(2, 2)
    assert p["offset"] == 0 and len(p["rows"]) == 2 and p["total"] == 2
    assert t.trade_page(0, 2)["offset"] == 0


def test_ee_log_override_expands_home(tmp_path):
    s = Settings(str(tmp_path / "settings.json"))
    out = sv.set_value(s, "ee_log_path", "~/logs/EE.log", home="/home/deck")
    assert out["value"] == s.get("ee_log_path") == "/home/deck/logs/EE.log"
    # the inventory module expands ~ itself, so that field keeps the raw text
    sv.set_value(s, "inventory_file", "~/inv.json", home="/home/deck")
    assert s.get("inventory_file") == "~/inv.json"
    assert sv.set_value(s, "ee_log_path", "", home="/home/deck")["value"] == ""


def test_settings_change_emits_event(app, monkeypatch):
    sent = []
    monkeypatch.setattr(rpc, "emit", lambda event, data=None: sent.append(event))
    sv.set_value(app.settings, "overlays_enabled", False)
    assert "settings.changed" in sent
