"""Relic planner overlay, relic value math and the Relics page RPCs."""

import asyncio
import base64
import io
import itertools
import os
import time
import zlib

import numpy as np
import pytest
from PIL import Image, ImageDraw

from wfh import relic_value as rv
from wfh import rpc
from wfh.data import items as items_mod
from wfh.features import relic_planner as rp
from wfh.settings import Settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
from conftest import PREVIEWS_DIR as PREVIEWS
HAS_DATA = os.path.exists(os.path.join(DATA, "manifest.json"))
needs_data = pytest.mark.skipif(not HAS_DATA, reason="bundled data not built (scripts/fetch-data.sh)")


# ---- fixtures -----------------------------------------------------------------------

@pytest.fixture
def bundle():
    items_mod.set_data_dirs(DATA, None)
    yield
    items_mod._dirs_override = None
    items_mod.reset()


def sample_price(slug: str | None) -> float | None:
    """Deterministic, plausible prices: rares 25-64p, others 2-13p, Forma unpriced."""
    if not slug or "forma" in slug:
        return None
    h = zlib.crc32(slug.encode())
    return float(25 + h % 40) if h % 5 == 0 else float(2 + h % 12)


def sample_valuer(owned: dict | None = None) -> rv.Valuer:
    db = items_mod.items()

    def ducats(name):
        it = db.by_name(name)
        return it.ducats if it else None

    return rv.Valuer(sample_price, ducats, (lambda u: (owned or {}).get(u, 0)) if owned is not None else None)


class Reward:
    def __init__(self, name, chances, slug=None, rarity="Common", unique=""):
        self.item_name, self.chance_by_refinement, self.wfm_slug, self.rarity, self.item_unique = \
            name, chances, slug, rarity, unique

    def chance(self, q="intact"):
        return self.chance_by_refinement.get(q, 0.0)


class Relic:
    def __init__(self, name, rewards, vaulted=False):
        self.name, self.rewards, self.vaulted = name, rewards, vaulted
        self.tier, self.code = name.split(" ")

STD = [("c1", "Common", 25.33, 16.67), ("c2", "Common", 25.33, 16.67), ("c3", "Common", 25.33, 16.67),
       ("u1", "Uncommon", 11, 20), ("u2", "Uncommon", 11, 20), ("r1", "Rare", 2, 10)]


def std_relic(name="Axi Z1", prices=None, vaulted=False):
    rewards = [Reward(n, {"intact": i, "exceptional": i, "flawless": i, "radiant": r}, n, rar) for n, rar, i, r in STD]
    return Relic(name, rewards, vaulted), rv.Valuer(lambda s: (prices or {}).get(s), lambda n: None)


# ---- EV ---------------------------------------------------------------------------

def test_squad_ev_worked_example():
    # relic-planner.md §7.2: commons 2/3/5, uncommons 10/15, rare 50 -> 6.283 solo
    ch = [25.33, 25.33, 25.33, 11, 11, 2]
    assert rv.squad_ev(ch, [2, 3, 5, 10, 15, 50], 1) == pytest.approx(6.2833, abs=1e-3)
    # None counts as 0
    assert rv.squad_ev(ch, [None, None, None, 10, 15, 50], 1) == pytest.approx(.11 * 25 + 1, abs=1e-6)


def test_squad_ev_matches_brute_force_best_of_n():
    ch = [25.33, 25.33, 25.33, 11, 11, 2]
    vals = [2, 3, 3, 10, 15, 50]  # equal values merge
    p = [c / 100 for c in ch]
    p = [x / sum(p) for x in p]  # brute force needs a full distribution
    for n in (2, 3, 4):
        brute = sum(np.prod([p[i] for i in combo]) * max(vals[i] for i in combo)
                    for combo in itertools.product(range(6), repeat=n))
        assert rv.squad_ev([x * 100 for x in p], vals, n) == pytest.approx(brute, rel=1e-9)
    assert rv.squad_ev(ch, vals, 4) > rv.squad_ev(ch, vals, 1)


def test_best_owned_quality_and_modes():
    relic, val = std_relic(prices={"r1": 60, "u1": 5, "c1": 1})
    # radiant beats intact when the rare is the value
    assert rv.pick_best_owned_quality(relic, {"intact": 3, "radiant": 1}, 1, val) == "radiant"
    # nothing owned -> None; only intact owned -> intact
    assert rv.pick_best_owned_quality(relic, {}, 1, val) is None
    assert rv.pick_best_owned_quality(relic, {"intact": 1}, 4, val) == "intact"
    # value in commons: intact has the higher common chance
    relic2, val2 = std_relic(prices={"c1": 30, "c2": 30, "c3": 30})
    assert rv.pick_best_owned_quality(relic2, {"intact": 1, "radiant": 1}, 1, val2) == "intact"
    assert rv.quality_for_mode("flawless", {"intact": 1}) == "flawless"
    assert rv.quality_for_mode("owned", {"intact": 1, "exceptional": 2}) == "exceptional"
    assert rv.quality_for_mode("owned", {"intact": 1, "radiant": 2}, pinned="intact") == "intact"
    assert rv.quality_for_mode("owned", {"radiant": 2}, pinned="intact") == "radiant"
    # no prices -> plat None; refinement table reports the gain of refining
    relic3, val3 = std_relic(prices={})
    assert rv.relic_ev(relic3, "intact", 1, val3) == (None, None)
    table = rv.refinement_table(relic, 1, val, {"intact": 2})
    rad = next(t for t in table if t["quality"] == "radiant")
    assert rad["gainOverIntact"] == pytest.approx(0.10 * 60 + 0.20 * 5 + 0.1667 * 1 - (0.02 * 60 + 0.11 * 5 + 0.2533), abs=0.01)
    assert table[0]["owned"] == 2 and rad["traces"] == 100


def test_era_helpers():
    assert [rv.tag_era(t) for t in ("VoidT1", "voidt4", "VoidT5", "VoidT6", "SolNode1")] == \
        ["lith", "axi", "requiem", "omnia", None]
    assert rv.normalize_era("Requiem IV") == "requiem" and rv.normalize_era("x") is None
    assert rv.era_matches("Lith", "lith") and not rv.era_matches("Meso", "lith")
    assert rv.era_matches("Axi", "omnia") and not rv.era_matches("Requiem", "omnia")
    # omnia keeps everything but Requiem, Vanguard included (buildRecommendations: eraFilter=null)
    assert rv.era_matches("Vanguard", None) and rv.era_matches("Vanguard", "omnia")
    assert not rv.era_matches("Vanguard", "axi")


def test_normalize_filters():
    f = rv.normalize_filters({"squadSize": 9, "sortMode": "ev", "sortDirection": "desc", "ownedAbove": 3,
                              "tierFilter": "Axi", "pinnedQualities": {"Axi A1": "radiant", "Neo N1": "gold"}})
    assert f["squadSize"] == 4  # invalid -> first-push default squad 4
    assert f["sortMode"] == "ev" and f["ownedAbove"] == 0 and f["tierFilter"] == "axi"
    assert f["pinnedQualities"] == {"Axi A1": "radiant"}
    g = rv.normalize_filters({"squadSize": 2, "tierFilter": "all", "search": "x" * 300}, f)
    assert g["squadSize"] == 2 and g["tierFilter"] is None and len(g["search"]) == 200 and g["sortMode"] == "ev"


def test_sort_rows_nulls_last_and_filters():
    rows = [
        {"key": "Neo A1", "name": "Neo A1", "tier": "Neo", "plat": None, "ducat": 10, "ratio": None, "vaulted": False, "ownedTotal": 1, "owned": {}},
        {"key": "Lith B2", "name": "Lith B2", "tier": "Lith", "plat": 5.0, "ducat": 50, "ratio": 10, "vaulted": True, "ownedTotal": 5, "owned": {}},
        {"key": "Axi C3", "name": "Axi C3", "tier": "Axi", "plat": 9.0, "ducat": 20, "ratio": 2.2, "vaulted": False, "ownedTotal": 12, "owned": {}},
        {"key": "Lith B10", "name": "Lith B10", "tier": "Lith", "plat": 5.0, "ducat": 1, "ratio": 0.2, "vaulted": False, "ownedTotal": 3, "owned": {}},
    ]
    for r in rows:
        r["label"] = r["name"]
    names = lambda rs: [r["name"] for r in rs]  # noqa: E731
    assert names(rv.sort_rows(rows, "ev", "desc")) == ["Axi C3", "Lith B2", "Lith B10", "Neo A1"]
    assert names(rv.sort_rows(rows, "ev", "asc")) == ["Lith B2", "Lith B10", "Axi C3", "Neo A1"]
    assert names(rv.sort_rows(rows, "tier")) == ["Lith B2", "Lith B10", "Neo A1", "Axi C3"]
    assert names(rv.select_rows(rows, {"vaultedMode": "unvaulted", "ownedAbove": 2, "sortMode": "ducat",
                                       "sortDirection": "desc"})) == ["Axi C3", "Lith B10"]
    assert names(rv.rank_by_ev(rows))[:2] == ["Axi C3", "Lith B10"] or names(rv.rank_by_ev(rows))[:2] == ["Axi C3", "Lith B2"]


@needs_data
def test_search_and_rows_on_real_data(bundle):
    from wfh.data.relics import relics

    db = relics()
    relic = db.get("Vanguard E1")
    assert rv.matches_search(relic, "ash prime", None)
    assert rv.matches_search(relic, "ash bp", None) is False
    assert rv.matches_search(relic, "vanguard e1 radiant", {"radiant": 1})
    assert not rv.matches_search(relic, "vanguard e1 radiant", {"intact": 1})
    some = [r for r in db.all() if r.tier == "Axi"][:6]
    owned = {(r.name, "intact"): 3 for r in some}
    owned[(some[0].name, "radiant")] = 2
    rows = rv.build_rows(db, owned, squad=4, era="axi", best_quality=True, valuer=sample_valuer({}))
    assert {r["name"] for r in rows} == {r.name for r in some}
    assert all(r["ownedTotal"] >= 3 and len(r["rewards"]) <= 6 for r in rows)
    first = next(r for r in rows if r["name"] == some[0].name)
    assert first["label"].startswith(("3x", "2x")) and first["rewards"][0]["rarity"] == "Rare"
    assert rv.build_rows(db, owned, era="lith", valuer=sample_valuer()) == []
    assert len(rv.build_rows(db, owned, ownership="all", valuer=sample_valuer())) == len(db)


# ---- era OCR -----------------------------------------------------------------------

def test_era_text_matchers():
    assert rp.era_from_label("All") == ("omnia", 1.0)
    assert rp.era_from_label("AII") == ("omnia", 0.95)
    assert rp.era_from_label("Axi") == ("axi", 1.0)
    assert rp.era_from_label("LITK") == ("lith", 0.76)  # one edit
    assert rp.era_from_label("REQ") == ("requiem", 0.82)  # prefix
    assert rp.era_from_text("REQUIEN")[0] == "requiem"
    assert rp.era_from_tile("Lith A1 Relic") == ("lith", 1.0)
    assert rp.era_from_tile("Lith A1 Meso B2") == (None, 0.0)
    assert rp.era_from_band("Neo Relics") == ("neo", 1.0)
    assert rp.era_from_band("Lith Meso Neo Axi") == (None, 0.0)
    assert rp.era_from_text("x y")[0] is None
    assert rp.levenshtein("LITK", "LITH") == 1


def test_crop_geometry_deck_16_10():
    w, h = 1280, 800
    labels = rp.label_boxes(w, h)
    assert labels[0] == ("filter-label", (25, 68, 243, 46))
    assert labels[1] == ("filter-label-canvas", (25, 101, 243, 41))
    assert "filter-label-canvas" not in rp.TRUSTED_LABEL_IDS
    tiles = [rp.crop_rect(w, h, r) for r in rp.TILE_RECTS]
    # literal 0.20 like rewardScannerSupport.ts (0.02 + 0.18 would floor to 255)
    assert [t[0] for t in tiles] == [25, 256, 486, 716, 947] and tiles[0][1:] == (400, 230, 336)
    assert [rp.crop_band(w, h, *b) for b in rp.BANDS] == [(0, 126, 1280, 86), (0, 155, 1280, 93), (0, 184, 1280, 100)]
    # 16:9 has one label box
    assert len(rp.label_boxes(1920, 1080)) == 1


def test_detect_era_order_budget_and_self_read():
    w, h = 1280, 800
    img = np.zeros((h, w, 3), np.uint8)
    seen = []

    def reader(texts):
        def read(crop):
            seen.append(crop.shape[:2])
            return texts.pop(0) if texts else ""
        return read

    # label reads "Meso" at once: one attempt
    r = rp.detect_era(img, reader(["Meso"]))
    assert (r.era, r.confidence, r.candidate, r.attempts) == ("meso", 1.0, "filter-label", 1)
    # label unreadable -> tiles; a tile at 1.0 confidence stops the scan
    seen.clear()
    r = rp.detect_era(img, reader(["", "", "", "", "Lith A1 Relic"]))
    assert (r.era, r.candidate) == ("lith", "tile-slot-1") and len(seen) == 5
    # label_only never reads tiles
    seen.clear()
    r = rp.detect_era(img, reader([]), label_only=True)
    assert r.era is None and len(seen) == 4
    # our own overlay text is ignored
    r = rp.detect_era(img, reader(["3x Axi A16 Radiant"]), self_signatures={rp.row_signature("3x Axi A16 Radiant")})
    assert r.era is None or r.candidate != "filter-label"
    # budget: a slow reader stops after the first attempt once < 0.9 s remain
    t = [0.0]

    def slow(crop):
        t[0] += 1.0
        return ""
    r = rp.detect_era(img, slow, timeout_s=2.4, clock=lambda: t[0])
    assert r.attempts == 2


def test_enhanced_variant():
    crop = np.full((10, 20, 3), 214, np.uint8)
    out = rp.enhanced(crop)
    assert out.shape == (20, 40, 3) and out.max() == 255
    assert rp.enhanced(np.full((4, 4, 3), 60, np.uint8)).max() == 0


@pytest.mark.skipif(not os.path.exists(os.path.join(ROOT, "models", "paddle", "ch_PP-OCRv3_rec_infer.onnx")),
                    reason="OCR model missing")
def test_detect_era_real_ocr_on_synthetic_picker():
    from wfh import ocr
    from wfh.overlay import draw

    img = Image.new("RGB", (1280, 800), (18, 22, 30))
    d = ImageDraw.Draw(img)
    d.text((40, 76), "AXI", font=draw.font(26, True), fill=(235, 235, 235))
    r = rp.detect_era(img, lambda crop: (ocr.read_strip(crop, os.path.join(ROOT, "models")) or ocr.StripRead("", [])).text,
                      timeout_s=10, label_only=True)
    assert r.era == "axi" and r.candidate == "filter-label"


# ---- controller --------------------------------------------------------------------

class FakeApp:
    def __init__(self, tmp_path):
        from wfh.eelog import EELogTailer
        from wfh.overlay.manager import OverlayManager

        self.settings = Settings(str(tmp_path / "settings.json"))
        self.overlays = OverlayManager(headless=True)
        self.eelog = EELogTailer()
        self.events, self.toasts = [], []
        self.models_dir = os.path.join(ROOT, "models")
        self.runtime_dir = str(tmp_path)
        self.services = {}

    def emit(self, name, data=None):
        self.events.append((name, data))

    def toast(self, title, body=""):
        self.toasts.append((title, body))

    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture
def planner_env(tmp_path, monkeypatch, bundle):
    from wfh import inventory as inv_mod
    from wfh.data.relics import relics

    db = relics()
    axi = [r for r in db.all() if r.tier == "Axi"][:5]
    lith = [r for r in db.all() if r.tier == "Lith"][:3]
    owned = {(r.name, "intact"): 2 for r in axi + lith}
    owned[(axi[1].name, "radiant")] = 1
    monkeypatch.setattr(inv_mod, "inventory", lambda: object())
    monkeypatch.setattr(inv_mod, "owned_relics", lambda: owned)
    monkeypatch.setattr(rv, "default_valuer", lambda with_owned=True: sample_valuer({}))
    app = FakeApp(tmp_path)
    clock = [1000.0]
    grabs = []

    def grab():
        grabs.append(1)
        return Image.new("RGB", (1280, 800))

    reads: list[str] = []
    p = rp.RelicPlanner(app, clock=lambda: clock[0], grab=grab, read_text=lambda c: reads.pop(0) if reads else "",
                        schedule=lambda delay, fn: fn(), sleep=lambda s: None)
    app.eelog.subscribe(p.on_line)
    yield p, app, clock, grabs, reads, axi, lith
    app.overlays.stop()


def last_rows(app):
    return [d for n, d in app.events if n == "relics.planner" and d.get("state") == "rows"][-1]


@needs_data
def test_eelog_tag_then_picker_shows_era_rows(planner_env):
    p, app, clock, grabs, reads, axi, lith = planner_env
    app.eelog.feed('    "activeMissionTag" : "VoidT4",')
    assert p.s.log_tier == "axi"
    reads[:] = ["Axi"]  # label cross-check agrees
    app.eelog.feed("12.3 Script [Info]: ThemedProjectionManager.lua: PopulateInventoryGrid")
    payload = last_rows(app)
    assert payload["era"] == "axi" and payload["eraSource"] == "eelog"
    assert {r["name"] for r in payload["rows"]} == {r.name for r in axi}
    plats = [r["plat"] if r["plat"] is not None else -1 for r in payload["rows"]]
    assert plats == sorted(plats, reverse=True)
    assert payload["squadSize"] == 4 and "planner" in app.overlays.active()
    # second open line within the 3 s cooldown is ignored
    n = len(app.events)
    clock[0] += 1
    app.eelog.feed("ThemedProjectionManager.lua: LoadingCompleteEnd")
    assert len(app.events) == n
    # InitMapping inside the 800 ms entry window is not a close; later it is
    clock[0] = p.s.last_open_at + 0.2
    app.eelog.feed("InitMapping for all devices with bindings")
    assert p.s.picker_open is True and "planner" in app.overlays.active()
    clock[0] += 2
    app.eelog.feed("InitMapping for all devices with bindings")
    assert p.s.picker_open is False and "planner" not in app.overlays.active()
    # mission end clears the tier
    app.eelog.feed("234.503 Sys [Info]: EOM missionLocationUnlocked=1")
    assert p.s.log_tier is None


@needs_data
def test_label_overrides_stale_tag_but_not_with_requiem(planner_env):
    p, app, clock, grabs, reads, axi, lith = planner_env
    p.set_mission_tag("VoidT4")
    reads[:] = ["Lith"]
    p.trigger("eelog")
    payload = last_rows(app)
    assert payload["era"] == "lith" and payload["eraSource"] == "ocr"
    assert {r["name"] for r in payload["rows"]} == {r.name for r in lith}
    clock[0] += 5
    p.close()
    reads[:] = ["Requiem"]
    p.trigger("eelog")
    assert last_rows(app)["era"] == "axi"


@needs_data
def test_ocr_fallback_cache_and_unknown_era(planner_env):
    p, app, clock, grabs, reads, axi, lith = planner_env
    # the uncalibrated 16:9-canvas label box gives an era but never the 25-min cache
    reads[:] = ["", "", "Axi"]  # full label box raw/enhanced miss, canvas box hits
    p.trigger("eelog")
    payload = last_rows(app)
    assert payload["era"] == "axi" and payload["eraSource"] == "ocr"
    assert payload["detection"]["candidateId"] == "filter-label-canvas" and p.cached_era() is None
    clock[0] += 5
    reads[:] = ["Axi"]  # WFHelper's label rect: cached
    p.trigger("eelog")
    assert last_rows(app)["era"] == "axi" and p.cached_era() == "axi"
    # cached era: no OCR on the next trigger
    clock[0] += 5
    n = len(grabs)
    p.trigger("eelog")
    assert len(grabs) == n and last_rows(app)["eraSource"] == "cache"
    # close forgets the OCR era; nothing readable -> message, short ttl
    p.close()
    clock[0] += 5
    p.trigger("eelog")
    payload = last_rows(app)
    # unknown era -> every owned relic (WFHelper's effectiveEra = hint = null)
    assert payload["era"] is None and payload["eraSource"] is None and payload["count"] == len(axi) + len(lith)
    # pushed tier hint is used without OCR
    app.settings.update({"relic_planner": {"filters": rv.normalize_filters({"tierFilter": "Lith", "squadSize": 2,
                                                                             "sortMode": "ev", "sortDirection": "desc"})}})
    clock[0] += 5
    n = len(grabs)
    p.trigger("eelog")
    payload = last_rows(app)
    assert len(grabs) == n and payload["era"] == "lith" and payload["squadSize"] == 2


@needs_data
def test_dismiss_suppresses_reopen_and_disabled(planner_env):
    p, app, clock, grabs, reads, axi, lith = planner_env
    p.set_mission_tag("VoidT1")
    p.dismiss()
    assert p.trigger("eelog") is None
    assert p.trigger("manual") is not None
    clock[0] += 4
    app.settings.update({"relic_planner": {"enabled": False}})
    assert p.trigger("eelog") is None
    app.settings.update({"relic_planner": {"enabled": True, "auto_trigger": False}})
    assert p.trigger("eelog") is None and p.trigger("manual", "all") is not None
    assert last_rows(app)["era"] is None and last_rows(app)["count"] == len(axi) + len(lith)


@needs_data
def test_toast_fallback_without_overlay_window(planner_env):
    p, app, clock, grabs, reads, axi, lith = planner_env
    app.overlays.headless = False  # not headless and never connected -> toast
    p.set_mission_tag("VoidT4")
    p.trigger("manual")
    assert app.toasts and app.toasts[-1][0] == "Best Axi relics"
    app.settings.update({"overlays_enabled": False})
    n = len(app.toasts)
    p.trigger("manual")
    assert len(app.toasts) == n


# ---- preview -----------------------------------------------------------------------

@needs_data
def test_planner_preview_png(bundle):
    """Renders a realistic Axi fissure panel with the headless manager -> docs/previews/planner.png."""
    from wfh.data.relics import relics
    from wfh.overlay.manager import OverlayManager

    db = relics()
    names = ["Axi A16", "Axi S9", "Axi V10", "Axi N11", "Axi L4", "Axi G9", "Axi C8", "Axi R5", "Axi T11",
             "Axi E6", "Axi K7", "Axi P9", "Axi B5", "Axi W4", "Axi O6", "Axi H6", "Axi M3", "Axi D4"]
    chosen = [db.get(n) for n in names if db.get(n)]
    owned = {}
    for i, r in enumerate(chosen):
        owned[(r.name, "intact")] = 1 + (i * 7) % 9
        if i % 3 == 0:
            owned[(r.name, "radiant")] = 1 + i % 3
    val = sample_valuer({chosen[0].rewards[0].item_unique: 1} if chosen else {})
    rows = [rv.public(r) for r in rv.rank_by_ev(rv.build_rows(db, owned, squad=4, era="axi", best_quality=True, valuer=val))]
    # Show the mastery marks: pretend the second relic holds a part never crafted.
    rows[1]["masteryParts"] = [rows[1]["rewards"][1]["name"]] if rows[1].get("rewards") else ["x"]
    rows = [rows[1]] + rows[:1] + rows[2:]
    payload = {"state": "rows", "era": "axi", "eraSource": "eelog", "rows": rows, "squadSize": 4,
               "totalOwnedCount": sum(r["ownedTotal"] for r in rows)}
    panel = rp.render_panel(payload, show_rewards=False)
    assert panel.width == rp.PANEL_W and panel.height <= rp.PANEL_MAX_H
    with_rewards = rp.render_panel({**payload, "rows": rows[:4]}, show_rewards=True)
    scanning = rp.render_panel({"state": "scanning", "rows": [], "squadSize": 4})
    empty = rp.render_panel({"state": "rows", "era": "requiem", "rows": [], "message": "No owned Requiem relics to recommend."})
    assert scanning.height < 200 and empty.height < 200

    om = OverlayManager(headless=True)
    try:
        img, x, y = rp.scale_to_overlay(panel, om.size)
        assert (x, y) == (1280 - 380 - 8, 40)
        om.show("planner", img, x, y, ttl=30)
        deadline = time.time() + 5
        comp = None
        while time.time() < deadline:
            comp = Image.open(io.BytesIO(base64.b64decode(om.snapshot_png().split(",", 1)[1])))
            if comp.getbbox():
                break
            time.sleep(0.05)
        assert comp is not None and comp.getbbox() is not None
    finally:
        om.stop()
    # a dim stand-in for the relic picker behind the panel
    bg = Image.new("RGBA", (1280, 800), (20, 24, 34, 255))
    d = ImageDraw.Draw(bg)
    for i in range(5):
        d.rounded_rectangle((25 + i * 172, 400, 25 + i * 172 + 160, 600), 8, outline=(60, 70, 95, 255), fill=(30, 35, 50, 255))
    d.text((32, 76), "AXI", fill=(200, 200, 210, 255))
    bg.alpha_composite(comp)
    bg.alpha_composite(with_rewards, (440, 40))
    os.makedirs(PREVIEWS, exist_ok=True)
    bg.convert("RGB").save(os.path.join(PREVIEWS, "planner.png"))
    # 720 px Deck-scale fits >= 12 cards
    assert len(rows) < 12 or panel.height > 600


def test_scale_to_overlay_other_sizes():
    img = Image.new("RGBA", (380, 400))
    out, x, y = rp.scale_to_overlay(img, (1920, 1200))
    assert out.width == 570 and x == 1920 - 570 - 12 and y == 60


# ---- page RPCs ---------------------------------------------------------------------

@needs_data
def test_relics_view_detail_top_and_push(app, bundle, monkeypatch):
    from wfh import inventory as inv_mod
    from wfh.data.relics import relics

    db = relics()
    axi = [r for r in db.all() if r.tier == "Axi"][:4]
    neo = [r for r in db.all() if r.tier == "Neo"][:3]
    owned = {(r.name, "intact"): 3 for r in axi + neo}
    owned[(axi[0].name, "radiant")] = 2

    class Inv:
        origin, sync_time, fetched_at = "file", 1, 1.0

        def owned_count(self, u):
            return 1 if u == axi[0].rewards[0].item_unique else 0

    monkeypatch.setattr(inv_mod, "inventory", lambda: Inv())
    monkeypatch.setattr(inv_mod, "owned_relics", lambda: owned)
    monkeypatch.setattr(rv, "default_valuer", lambda with_owned=True: sample_valuer({axi[0].rewards[0].item_unique: 1}))

    call = lambda m, **kw: asyncio.run(rpc.call_local(m, kw))  # noqa: E731
    v = call("relics.view", tier="all", sort="ev", direction="desc", squad=2, limit=5)
    assert v["hasInventory"] and v["total"] == 7 and len(v["groups"]) == 5 and v["tierCounts"]["Axi"] == 4
    g0 = v["groups"][0]
    assert set(g0) >= {"key", "name", "tier", "vaulted", "owned", "quality", "plat", "ducat", "ratio", "state", "rewards"}
    plats = [g["plat"] for g in v["groups"] if g["plat"] is not None]
    assert plats == sorted(plats, reverse=True)
    assert call("relics.view", tier="Neo")["total"] == 3
    assert call("relics.view", ownership="all", quality="radiant", limit=1)["total"] == len(db)
    hits = call("relics.view", search=axi[0].name)
    assert hits["total"] >= 1 and axi[0].name in [g["name"] for g in hits["groups"]]
    page2 = call("relics.view", offset=5, limit=5)
    assert page2["offset"] == 5 and len(page2["groups"]) == 2

    det = call("relics.detail", name=axi[0].name + " Relic", squad=1)
    assert det["name"] == axi[0].name and det["owned"]["radiant"] == 2 and det["bestOwned"] in ("intact", "radiant")
    assert set(det["rewards"]) == set(rv.REFINEMENTS) and det["rewards"]["radiant"][0]["rarity"] == "Rare"
    assert det["evBySquad"]["4"]["radiant"]["plat"] >= det["evBySquad"]["1"]["radiant"]["plat"]
    assert any(r["owned"] == 1 for r in det["rewards"]["intact"])
    assert call("relics.detail", name="Lith Nope") is None

    top = call("relics.top", limit=3, squad=4, era="axi")
    assert top["squad"] == 4 and len(top["rows"]) == 3 and all(r["tier"] == "Axi" for r in top["rows"])

    f = call("relics.push_filters", push={"squadSize": 3, "sortMode": "ev", "pinnedQualities": {axi[0].name: "intact"}})
    assert f["squadSize"] == 3 and app.settings.get("relic_planner.filters")["pinnedQualities"] == {axi[0].name: "intact"}
    f = call("relics.push_filters", push={"pinnedQualities": {}})
    assert app.settings.get("relic_planner.filters")["pinnedQualities"] == {} and f["squadSize"] == 3
    assert call("relics.planner_state")["filtersPushed"] is True
    call("relics.clear_filters")
    assert app.settings.get("relic_planner.filters") is None
    assert call("relics.planner_hide") == {"ok": True}


def test_view_without_inventory(app, monkeypatch):
    from wfh import inventory as inv_mod

    monkeypatch.setattr(inv_mod, "inventory", lambda: None)
    top = asyncio.run(rpc.call_local("relics.top", {"limit": 3}))
    assert top == {"hasInventory": False, "rows": [], "squad": 1, "total": 0}


@needs_data
def test_no_inventory_message(planner_env, monkeypatch):
    from wfh import inventory as inv_mod

    p, app, clock, grabs, reads, axi, lith = planner_env
    monkeypatch.setattr(inv_mod, "inventory", lambda: None)
    p.set_mission_tag("VoidT2")
    p.trigger("manual")
    payload = last_rows(app)
    assert payload["rows"] == [] and payload["hasInventory"] is False and "inventory" in payload["message"].lower()


# ---- review regressions ------------------------------------------------------------

@needs_data
def test_pushed_quality_without_copies_falls_back_to_owned(planner_env):
    """A pushed qualityMode the player owns no copy of must not rank "0x" rows on the overlay."""
    from wfh.data.relics import relics

    p, app, clock, grabs, reads, axi, lith = planner_env
    db = relics()
    owned = {(axi[0].name, "intact"): 3}
    rows = rv.build_rows(db, owned, era="axi", quality_mode="radiant", valuer=sample_valuer(), fallback_unowned=True)
    assert [(r["label"], r["count"]) for r in rows] == [(f"3x {axi[0].name} Intact", 3)]
    # the page keeps the chosen refinement (desktop planner behaviour)
    page = rv.build_rows(db, owned, era="axi", quality_mode="radiant", valuer=sample_valuer())
    assert page[0]["quality"] == "radiant" and page[0]["count"] == 0
    # overlay path with pushed filters
    app.settings.update({"relic_planner": {"filters": rv.normalize_filters(
        {"qualityMode": "radiant", "sortMode": "ev", "sortDirection": "desc", "search": axi[1].name})}})
    p.trigger("manual", "axi")
    payload = last_rows(app)
    assert payload["rows"] and all(r["count"] > 0 for r in payload["rows"])
    # radiant stays where it is owned
    assert next(r for r in payload["rows"] if r["name"] == axi[1].name)["quality"] == "radiant"
    # totalOwnedCount counts the whole era before the search filter
    assert payload["count"] == 1 and payload["totalOwnedCount"] == 2 * len(axi) + 1


@needs_data
def test_file_picker_lines_ignored_while_realtime(planner_env):
    p, app, clock, grabs, reads, axi, lith = planner_env

    class FreshProton:
        path = "/fake/proton.log"

        def __init__(self, fresh):
            self.fresh = fresh

        def is_fresh(self, _now):
            return self.fresh

    app.eelog._proton = FreshProton(True)
    p.set_mission_tag("VoidT4")
    app.eelog.feed("ThemedProjectionManager.lua: PopulateInventoryGrid")
    assert p.s.picker_open is False and not [e for e in app.events if e[0] == "relics.planner"]
    app.eelog._proton = FreshProton(False)
    app.eelog.feed("ThemedProjectionManager.lua: PopulateInventoryGrid")
    assert p.s.picker_open is True and last_rows(app)["era"] == "axi"


@needs_data
def test_one_toast_and_one_rows_event_per_trigger(planner_env):
    p, app, clock, grabs, reads, axi, lith = planner_env
    app.overlays.headless = False  # overlay window not connected -> toast fallback
    p.set_mission_tag("VoidT4")
    reads[:] = ["Axi"]  # label cross-check agrees with the tag
    p.trigger("eelog")
    rows_events = [d for n, d in app.events if n == "relics.planner" and d.get("state") == "rows"]
    assert len(app.toasts) == 1 and len(rows_events) == 1
    # manual era: one ranking, no detection pass, no capture
    clock[0] += 5
    n_grabs = len(grabs)
    p.trigger("manual", "lith")
    assert len(app.toasts) == 2 and app.toasts[-1][0] == "Best Lith relics" and len(grabs) == n_grabs
    # the label overriding the tag still replaces the rows (one more event, one more toast)
    clock[0] += 5
    reads[:] = ["Lith"]
    n_ev = len([e for e in app.events if e[1].get("state") == "rows"])
    p.trigger("eelog")
    assert len([e for e in app.events if e[1].get("state") == "rows"]) == n_ev + 2
    assert app.toasts[-1][0] == "Best Lith relics" and len(app.toasts) == 4


@needs_data
def test_event_payload_is_compact(planner_env):
    p, app, clock, grabs, reads, axi, lith = planner_env
    app.settings.update({"relic_planner": {"show_rewards": True}})
    p.trigger("manual", "all")
    payload = last_rows(app)
    assert payload["count"] == len(axi) + len(lith) and len(payload["rows"]) <= rp.EVENT_ROWS
    assert all("rewards" not in r for r in payload["rows"])
    assert p.state()["last"]["count"] == payload["count"]


@needs_data
def test_panel_ttl_picker_vs_manual(planner_env):
    p, app, clock, grabs, reads, axi, lith = planner_env
    assert p.rows_ttl("eelog") >= rp.AUTO_HIDE_SUCCESS_S
    assert p.rows_ttl("manual") == app.settings.get("relic_planner.duration_s")


def test_era_ocr_budget_includes_capture(tmp_path):
    app = FakeApp(tmp_path)
    t = [0.0]
    reads = []

    def slow_grab():
        t[0] += 1.2  # capture + PNG decode
        return Image.new("RGB", (1280, 800))

    def read(crop):
        reads.append(1)
        t[0] += 0.2
        return ""

    p = rp.RelicPlanner(app, clock=lambda: t[0], grab=slow_grab, read_text=read)
    try:
        r = p._ocr(rp.ERA_LABEL_TIMEOUT_S, True, set())
        # 0.3 s left after the capture: one read, then no budget for another
        assert len(reads) == 1 and r.elapsed_ms >= 1400
        reads.clear()
        t[0] = 0.0

        def slower_grab():
            t[0] += 1.6
            return Image.new("RGB", (1280, 800))

        p.grab = slower_grab
        r = p._ocr(rp.ERA_LABEL_TIMEOUT_S, True, set())
        assert reads == [] and r.era is None
    finally:
        app.overlays.stop()


def test_price_snapshot_survives_restart(tmp_path, monkeypatch):
    """Market drops disk prices older than 30 min on load; the planner keeps last-known prices."""
    from wfh.data import market

    class P:
        def __init__(self, median):
            self.median, self.low_sell = median, None

    class FakeMarket:
        cache_dir = str(tmp_path)

        def __init__(self, prices):
            self.prices = prices

        def cached(self, slug, rank=None, max_age=0):
            return (slug in self.prices), (P(self.prices[slug]) if slug in self.prices else None)

    class R:
        wfm_slug, item_name = "axi_thing", "Axi Thing"

    monkeypatch.setattr(rv, "_snap", None)
    monkeypatch.setattr(market, "get", lambda: FakeMarket({"axi_thing": 42.0}))
    v = rv.default_valuer(with_owned=False)
    assert v.plat(R()) == 42.0
    rv.save_price_snapshot()
    assert os.path.exists(tmp_path / rv.SNAPSHOT_FILE)
    # "restart": empty market cache, fresh module state
    monkeypatch.setattr(rv, "_snap", None)
    monkeypatch.setattr(market, "get", lambda: FakeMarket({}))
    v = rv.default_valuer(with_owned=False)
    assert v.plat(R()) == 42.0 and not v.missing


def test_panel_prices_loading_line():
    rows = [{"key": f"Axi A{i}", "name": f"Axi A{i}", "qualityLabel": "Intact", "count": 1, "plat": None,
             "ducat": 30.0, "vaulted": False, "label": f"1x Axi A{i} Intact"} for i in range(3)]
    base = {"state": "rows", "era": "axi", "rows": rows, "squadSize": 4}
    plain = rp.render_panel(base)
    noted = rp.render_panel({**base, "pricesMissing": 40})
    assert noted.height == plain.height + 32


def test_mastery_first_moves_relics_with_needed_parts_up(monkeypatch):
    from types import SimpleNamespace

    from wfh.features import reward_enrich

    class Needs:
        def __init__(self, db, inv):
            pass

        def needed(self, name):
            return name == "Vadarya Prime Barrel"

    monkeypatch.setattr(reward_enrich, "MasteryNeeds", Needs)
    monkeypatch.setattr("wfh.data.items.items", lambda: None)
    relic = lambda *names: SimpleNamespace(rewards=[SimpleNamespace(item_name=n) for n in names])  # noqa: E731
    rows = [
        {"name": "A", "_relic": relic("Forma Blueprint", "Bronco Prime Receiver")},
        {"name": "B", "_relic": relic("Vadarya Prime Barrel")},
        {"name": "C", "_relic": relic("Paris Prime Lower Limb")},
        {"name": "D", "_relic": relic("Vadarya Prime Barrel", "Forma Blueprint")},
    ]
    out = rp.RelicPlanner._mastery_first(rows, inv=object())
    assert [r["name"] for r in out] == ["B", "D", "A", "C"]  # stable within each half
    assert out[0]["masteryParts"] == ["Vadarya Prime Barrel"] and out[2]["masteryParts"] == []


def test_mastery_needs_by_name():
    from types import SimpleNamespace

    from wfh.features.reward_enrich import MasteryNeeds

    comp = SimpleNamespace(unique_name="/Lotus/Types/Recipes/Weapons/WeaponParts/VadaryaPrimeBarrel", item_count=1)
    parent = SimpleNamespace(unique_name="/Lotus/Weapons/VadaryaPrime", category="Melee", components=[comp])
    part = SimpleNamespace(unique_name=comp.unique_name, parent=parent.unique_name)

    class Db:
        def by_name(self, n):
            return part if n == "Vadarya Prime Barrel" else None

        def by_unique(self, u):
            return parent if u == parent.unique_name else None

    class Inv:
        def __init__(self, owned=None, xp=0):
            self._owned = owned or {}
            self.data = {"XPInfo": [{"ItemType": parent.unique_name, "XP": xp}] if xp else []}

        def owned_count(self, u):
            return self._owned.get(u, 0)

        def pending_recipes(self):
            return {}

    assert MasteryNeeds(Db(), Inv()).needed("Vadarya Prime Barrel") is True
    assert MasteryNeeds(Db(), Inv({parent.unique_name: 1})).needed("Vadarya Prime Barrel") is False  # crafted
    assert MasteryNeeds(Db(), Inv({comp.unique_name: 1})).needed("Vadarya Prime Barrel") is False    # part held
    assert MasteryNeeds(Db(), Inv(xp=10**7)).needed("Vadarya Prime Barrel") is False                 # mastered
    assert MasteryNeeds(Db(), Inv()).needed("Forma Blueprint") is None
    assert MasteryNeeds(Db(), None).needed("Vadarya Prime Barrel") is None
