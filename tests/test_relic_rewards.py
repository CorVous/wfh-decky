"""Relic rewards: geometry, slot counting, the full scan pipeline on WFHelper's
fixture screenshots, enrichment/pricing, trigger orchestration and the overlay.

The pipeline tests run the real PaddleOCR model (skipped when models/ is absent)
with a stub price source; nothing touches the network.
"""

import asyncio
import glob
import json
import os
import re
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from wfh.data import items as items_mod
from wfh.features import reward_geometry as geo
from wfh.features import reward_overlay as ov
from wfh.features import relic_rewards as rr
from wfh.features.reward_enrich import (PriceSource, RewardEntry, apply_prices, best_ducats, best_plat, enrich,
                                        pending_slugs)
from wfh.features.reward_scan import RewardScanner, ScanItem, clean_reward_ocr_text
from wfh.inventory.model import Inventory
from wfh.overlay.manager import OverlayManager
from wfh.settings import Settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "models")
FIX = os.path.join(ROOT, "tests", "fixtures")
PUBLIC = os.path.join(FIX, "reward-scan", "public")
PREVIEWS = os.path.join(ROOT, "docs", "previews")
EXPECTED = json.load(open(os.path.join(FIX, "ocr", "expected.json"), encoding="utf-8"))
DECK = (1280, 800)

needs_models = pytest.mark.skipif(
    not os.path.isfile(os.path.join(MODELS, "paddle", "ch_PP-OCRv3_rec_infer.onnx")),
    reason="models not fetched (scripts/fetch-models.sh)",
)
needs_data = pytest.mark.skipif(not os.path.isfile(os.path.join(ROOT, "data", "items.json.gz")),
                                reason="bundled data not built (scripts/fetch-data.sh)")


@pytest.fixture(scope="module")
def db():
    items_mod.set_data_dirs(os.path.join(ROOT, "data"))
    return items_mod.items()


@pytest.fixture(scope="module")
def scanner(db):
    return RewardScanner(MODELS, pool_fn=db.prime_parts)


def public(name: str) -> Image.Image:
    return Image.open(os.path.join(PUBLIC, name)).convert("RGB")


def build_slot_frame(cards: int) -> Image.Image:
    """real-{n}p.png as WFHelper's build-screens.cjs makes it from the slot-title crops."""
    frame = Image.new("RGBA", (1920, 1080), (24, 16, 16, 255))
    scaffold = Image.new("RGBA", (234, 413 - 243 - 3), (146, 138, 138, 255))
    for x in EXPECTED["slot_frames"][str(cards)]["cards_1080"]:
        frame.alpha_composite(scaffold, (x, 243))
    for f in sorted(glob.glob(os.path.join(FIX, "reward-scan", "slot-crops", f"{cards}_players_*.png"))):
        x, y = map(int, re.search(r"_title_(\d+)x(\d+)_", f).group(1, 2))
        frame.alpha_composite(Image.open(f).convert("RGBA"), (x, y))
    return frame.convert("RGB")


def sim_deck(frame: Image.Image) -> Image.Image:
    """spec §12.2 port tip: a 1080p frame padded to 16:10, then resized to 1280x800."""
    padded = Image.new("RGB", (1920, 1200), (24, 16, 16))
    padded.paste(frame, (0, 60))
    return padded.resize(DECK, Image.LANCZOS)


def same_item(got: str, want: str) -> bool:
    """run-check accepts "Forma Blueprint" or "2X Forma Blueprint" for a counted Forma card."""
    strip = lambda s: re.sub(r"^\d+\s*x\s*", "", s, flags=re.I)  # noqa: E731
    if "forma blueprint" in want.lower():
        return strip(got) == "Forma Blueprint"
    return got == want


# ---------------------------------------------------------------- geometry (§4)

def test_aspect_scale():
    assert geo.aspect_scale(1920, 1080) == (1.0, 1.0)
    assert geo.aspect_scale(1280, 800) == (1.0, pytest.approx(0.9))
    sx, sy = geo.aspect_scale(2560, 1080)
    assert sx == pytest.approx(1920 / 2560) and sy == 1.0


def test_deck_card_and_title_rects_match_spec_table():
    want = {1: [561], 2: [480, 643], 3: [399, 561, 724], 4: [313, 476, 638, 801]}
    for n, xs in want.items():
        cards = geo.card_rects(n, 0.99, DECK)
        assert [geo.crop_box(DECK, c) for c in cards] == [(x, 201, 156, 162) for x in xs]
        assert [geo.crop_box(DECK, geo.title_rect(c)) for c in cards] == [(x, 315, 156, 29) for x in xs]
    band = geo.card_bar_band(1280, 800, 0.99, geo.aspect_scale(*DECK))
    assert (band.row_left, band.row_width, band.top, band.band_height, band.window_width) == (314, 644, 350, 19, 41)


def test_ui_scale_shrinks_about_centre():
    (c,) = geo.ui_correct([geo.Rect(0.439, 0.225, 0.122, 0.225)], 0.5)
    s = 0.5 / 0.99
    assert c.x == pytest.approx(0.5 + (0.439 - 0.5) * s) and c.w == pytest.approx(0.122 * s)
    # out-of-range scales clamp to [0.25, 1]
    assert geo.ui_correct([geo.Rect(0.4, 0.4, 0.1, 0.1)], 5)[0] == geo.ui_correct([geo.Rect(0.4, 0.4, 0.1, 0.1)], 1)[0]


def synthetic_bars(n: int, size=(1920, 1080), ui=0.99) -> np.ndarray:
    """Reward card bars as WFHelper's rewardCardCounter test draws them (§12.3)."""
    w, h = size
    px = np.zeros((h, w, 3), np.uint8)
    px[:] = (18, 20, 30)
    sy = geo.aspect_scale(w, h)[1]
    s = ui / 0.99
    bar_y = round(h * (0.5 + ((0.5 + (0.443 - 0.5) * s) - 0.5) * sy))
    bar_h = max(2, round(0.004 * h))
    for c in geo.card_rects(n, ui, size):
        x0, x1 = round(c.x * w), round((c.x + c.w) * w)
        px[bar_y:bar_y + bar_h, x0:x1] = (196, 168, 96)
    return px


@pytest.mark.parametrize("size", [(1920, 1080), DECK])
@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_card_counter_counts_synthetic_bars(n, size):
    px = synthetic_bars(n, size)
    assert geo.count_cards(px, 0.99) == n
    lay = geo.layout_candidates(px, 0.99)
    assert len(lay) == 1 and lay[0].counted and lay[0].count == n and lay[0].method == "counter"


def test_card_counter_finds_other_ui_scale_by_sweep():
    px = synthetic_bars(3, (1920, 1080), ui=0.8)
    assert geo.count_cards(px, 0.99) == 0
    count, scale = geo.find_cards(px, 0.99)
    assert count == 3 and abs(scale - 0.8) <= 0.03


def test_blank_frame_has_no_layout():
    px = np.zeros((800, 1280, 3), np.uint8)
    assert geo.confident_layouts(px) == []


def test_activity_layouts_rank_lit_cards():
    """Flat lit cards where the 2-card row sits: the 2-card layout must be a candidate."""
    px = np.zeros((800, 1280, 3), np.uint8)
    for c in geo.card_rects(2, 0.99, DECK):
        x, y, w, h = geo.crop_box(DECK, c)
        px[y:y + h, x:x + w] = (150, 140, 120)
        px[y + 20:y + 60:4, x + 10:x + w - 10] = (250, 240, 200)  # some texture
    lays = geo.confident_layouts(px)
    assert any(l.count == 2 and l.method == "activity" for l in lays)
    act = geo.activity_layouts(px)
    two = next(l for l in act if l.count == 2)
    four = next((l for l in act if l.count == 4), None)
    assert four is None or two.confidence > four.confidence


def test_projection_fallback_finds_bright_columns():
    px = np.zeros((800, 1280, 3), np.uint8)
    for x in (200, 500, 800):
        px[300:560, x:x + 120] = (230, 230, 230)
    lay = geo.projection_layout(px)
    assert lay.count == 3 and lay.method == "projection" and lay.confidence > 0.38


def test_frame_key_is_stable_and_scale_specific():
    px = synthetic_bars(4, DECK)
    assert geo.frame_key(px, 0.99) == geo.frame_key(px.copy(), 0.99)
    assert geo.frame_key(px, 0.99) != geo.frame_key(px, 0.8)


def test_clean_reward_ocr_text():
    assert clean_reward_ocr_text("Khora Prime Systems Blueprint") == "Khora Prime Systems Blueprint"
    assert clean_reward_ocr_text("a Fang Prime Handle -") == "Fang Prime Handle"
    assert clean_reward_ocr_text("Cobra & Crane Prime") == "Cobra & Crane Prime"
    # The Deck-size glued quantity marker is re-split so "2X Forma Blueprint" can match.
    assert clean_reward_ocr_text("2 XForma Blueprint") == "2X Forma Blueprint"
    assert clean_reward_ocr_text("2 XFoima Blueprint") == "2X Foima Blueprint"
    assert clean_reward_ocr_text("2 X Forma Blueprint") == "2X Forma Blueprint"
    assert clean_reward_ocr_text("Xaku Prime Chassis") == "Xaku Prime Chassis"


# ---------------------------------------------------------------- full pipeline on fixtures (§12)

PUBLIC_CASES = [
    ("real-full-2p.png", None, 2, 2, ["Khora Prime Systems Blueprint", "Fang Prime Handle"]),
    ("real-full-4p-fps.png", None, 4, 4,
     ["Xaku Prime Chassis Blueprint", "Bronco Prime Receiver", "Paris Prime Lower Limb", "Vadarya Prime Barrel"]),
    ("real-full-4p-1080x607.png", None, 4, 4,
     ["Okina Prime Handle", "Velox Prime Barrel", "Caliban Prime Blueprint", "Grendel Prime Blueprint"]),
    # The only real 16:10 frame: the card bars are not found, activity scoring sets the count (§12.4).
    ("real-full-4p-16x10.png", None, 4, 0, ["2X Forma Blueprint"] * 4),
    ("real-full-4p-16x10.png", DECK, 4, 0, ["2X Forma Blueprint"] * 4),
]


@needs_models
@needs_data
@pytest.mark.parametrize("name,resize,slots,cards,expected", PUBLIC_CASES,
                         ids=[f"{c[0]}{'@deck' if c[1] else ''}" for c in PUBLIC_CASES])
def test_public_frames_full_pipeline(scanner, name, resize, slots, cards, expected):
    img = public(name)
    if resize:
        img = img.resize(resize, Image.LANCZOS)
    res = scanner.scan(img, 0.99, budget_s=None, dedup=False)
    assert res.slot_count == slots
    assert res.card_count == cards
    assert [i.slot for i in res.items] == list(range(len(expected)))
    for got, want in zip([i.name for i in res.items], expected):
        assert same_item(got, want), (got, want)
    assert res.strategy.startswith("slot-")


@needs_models
@needs_data
@pytest.mark.parametrize("cards", [2, 3, 4])
@pytest.mark.parametrize("deck", [False, True], ids=["1080p", "sim-deck"])
def test_reconstructed_slot_frames(scanner, cards, deck):
    frame = build_slot_frame(cards)
    if deck:
        frame = sim_deck(frame)
    res = scanner.scan(frame, 0.99, budget_s=None, dedup=False)
    expected = EXPECTED["slot_frames"][str(cards)]["expected"]
    assert res.slot_count == cards
    names = [i.name for i in res.items]
    assert len(names) == cards, names
    for got, want in zip(names, expected):
        assert same_item(got, want), (got, want)


@needs_models
@needs_data
def test_frame_dedup_returns_cached_result(db):
    sc = RewardScanner(MODELS, pool_fn=db.prime_parts)
    img = public("real-full-2p.png")
    a = sc.scan(img, 0.99, budget_s=None)
    b = sc.scan(img, 0.99, budget_s=None)
    assert not a.cached and b.cached and [i.name for i in b.items] == [i.name for i in a.items]
    sc.reset_dedup()
    assert not sc.scan(img, 0.99, budget_s=None).cached


def test_not_a_reward_screen_reads_nothing():
    calls = []
    sc = RewardScanner(MODELS, pool_fn=lambda: [], reader=lambda s: calls.append(s) or "")
    res = sc.scan(Image.new("RGB", DECK, (10, 10, 10)), 0.99)
    assert res.items == [] and res.layout_count == 0 and calls == []


# ---------------------------------------------------------------- enrichment + prices (§9, §10)

class StubPrices(PriceSource):
    """Recorded-price stand-in for warframe.market."""

    def __init__(self, table: dict[str, int | None], cached: set[str] | None = None):
        self.table = table
        self._cached = set(table) if cached is None else set(cached)
        self.fetched: list[list[str]] = []
        self.prefetched: list[str] = []

    def cached(self, slug):
        return (slug in self._cached, self.table.get(slug)) if slug in self._cached else (False, None)

    def fetch(self, slugs, deadline):
        self.fetched.append(list(slugs))
        self._cached |= set(slugs)
        return {s: self.table.get(s) for s in slugs}

    def prefetch(self, slugs):
        self.prefetched += list(slugs)
        return len(slugs)


PRICES = {
    "xaku_prime_chassis_blueprint": 18, "bronco_prime_receiver": 4, "paris_prime_lower_limb": 6,
    "vadarya_prime_barrel": 9, "xaku_prime_set": 140, "bronco_prime_set": 25, "paris_prime_set": 40,
    "vadarya_prime_set": 52, "khora_prime_systems_blueprint": 7, "fang_prime_handle": 3,
}


def sample_inventory() -> Inventory:
    xaku = "/Lotus/Types/Recipes/WarframeRecipes/XakuPrime"
    return Inventory({
        "Recipes": [{"ItemType": f"{xaku}ChassisBlueprint", "ItemCount": 1},
                    {"ItemType": f"{xaku}Blueprint", "ItemCount": 1},
                    {"ItemType": "/Lotus/Types/Recipes/Weapons/WeaponParts/BroncoPrimeReceiver", "ItemCount": 2}],
        "MiscItems": [{"ItemType": f"{xaku}SystemsComponent", "ItemCount": 1}],
        "PendingRecipes": [{"ItemType": f"{xaku}HelmetBlueprint"}],
        "XPInfo": [{"ItemType": "/Lotus/Weapons/Tenno/Pistol/BroncoPrime", "XP": 450000}],
    }, origin="file")


def _scan_items(db, names_):
    return [ScanItem(i, db.by_name(n), 1.0, 1000, "exact", n) for i, n in enumerate(names_)]


@needs_data
def test_enrich_owned_set_and_vaulted(db):
    names_ = ["Xaku Prime Chassis Blueprint", "Bronco Prime Receiver", "2X Forma Blueprint", "Fang Prime Handle"]
    entries = enrich(_scan_items(db, names_), db, sample_inventory())
    xaku, bronco, forma, fang = entries
    assert xaku.ducats == 45 and xaku.rarity == "Uncommon" and xaku.slug == "xaku_prime_chassis_blueprint"
    assert xaku.owned == 1 and xaku.required == 1
    assert xaku.set_name == "Xaku Prime Set" and xaku.set_slug == "xaku_prime_set"
    assert (xaku.set_owned, xaku.set_required) == (3, 4)  # BP + chassis + systems; neuroptics is building
    assert any(p.is_reward for p in xaku.set_parts) and any(p.building for p in xaku.set_parts)
    assert xaku.mastered is False
    assert bronco.owned == 2 and bronco.mastered is True
    assert forma.vaulted is None and forma.set_name is None  # never for Forma (scan.ts:231-266)
    assert fang.required == 2  # Fang Prime needs two handles
    assert isinstance(fang.vaulted, bool)


@needs_data
def test_enrich_without_inventory_has_no_counts(db):
    (e,) = enrich(_scan_items(db, ["Fang Prime Handle"]), db, None)
    assert e.owned is None and e.set_owned is None and e.mastered is None and e.ducats == 25


def test_prices_and_best_picks():
    entries = [RewardEntry(0, "A", slug="a", ducats=45), RewardEntry(1, "B", slug="b", ducats=100),
               RewardEntry(2, "C", slug="c", ducats=15), RewardEntry(3, "D", slug=None, price_state="missing")]
    pending = apply_prices(entries, lambda s: (s in ("a", "b"), {"a": 12.4, "b": -12}.get(s)))
    assert [e.plat for e in entries] == [12, 12, None, None]
    assert [e.price_state for e in entries] == ["ok", "ok", "pending", "missing"]
    assert pending == 1 and pending_slugs(entries) == ["c"]
    assert best_plat(entries) == 0  # tie goes to the lower slot
    assert best_ducats(entries) == 1
    apply_prices(entries, lambda s: (True, 0))
    assert entries[2].price_state == "missing" and entries[2].plat is None


# ---------------------------------------------------------------- trigger orchestration (§1.7, §2)

class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


class FakeEELog:
    def __init__(self):
        self.source = "file"
        self.realtime = False
        self.subs = []

    def realtime_active(self):
        return self.realtime

    def subscribe(self, fn, source="merged"):
        self.subs.append(fn)


def fake_app(tmp_path, overlays=None):
    events, toasts = [], []
    app = SimpleNamespace(
        settings=Settings(str(tmp_path / "settings.json")),
        overlays=overlays or OverlayManager(headless=True),
        eelog=FakeEELog(),
        runtime_dir=str(tmp_path / "runtime"),
        models_dir=MODELS,
        services={},
        events=events,
        toasts=toasts,
    )
    app.emit = lambda name, data=None: events.append((name, data))
    app.toast = lambda title, body="": toasts.append((title, body))
    app.submit = lambda fn, *a: fn(*a)
    return app


class StubScanner:
    """Returns canned ScanResults per attempt (frames are ignored)."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def ocr_available(self):
        return True

    def scan(self, frame, ui_scale):
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


def make_controller(app, scanner, clock, prices=None, db=None, inv=None, running=True):
    ctl = rr.RewardController(app, scanner=scanner, prices=prices or StubPrices({}),
                              grab=lambda: Image.new("RGB", DECK), clock=clock, sleep=clock.sleep,
                              game_running=lambda: running, ui_scale=lambda: 0.99,
                              inventory=lambda: inv, items_db=lambda: db)
    ctl.scheduled = []
    ctl._schedule = lambda delay, fn, *a: ctl.scheduled.append((delay, fn, a))
    return ctl


def run_scheduled(ctl, clock):
    while ctl.scheduled:
        delay, fn, a = ctl.scheduled.pop(0)
        clock.t += delay
        fn(*a)


def _result(db, names_, slot_count=4, card_count=4):
    from wfh.features.reward_scan import ScanResult

    lay = geo.Layout(slot_count, 1.0, geo.fixed_slots(geo.card_rects(slot_count, 0.99, DECK)), card_count > 0)
    items = _scan_items(db, names_) if names_ else []
    return ScanResult(items=items, layout=lay, strategy="slot-primary", score=500, exact_count=len(items),
                      layout_count=1, slot_count=slot_count, card_count=card_count, capture_size=DECK)


@needs_data
def test_eelog_trigger_scans_after_delay_and_hides_after_vote(tmp_path, db):
    clock = FakeClock()
    app = fake_app(tmp_path)
    names_ = ["Xaku Prime Chassis Blueprint", "Bronco Prime Receiver", "Paris Prime Lower Limb", "Vadarya Prime Barrel"]
    sc = StubScanner([_result(db, names_)])
    ctl = make_controller(app, sc, clock, StubPrices(PRICES), db, sample_inventory())
    ctl.on_line("1289.269 Sys [Info]: Pause countdown done")
    ctl.on_line("1289.300 Sys [Info]: Got rewards")  # second pattern inside the debounce: one trigger
    assert len(ctl.scheduled) == 1 and ctl.scheduled[0][0] == pytest.approx(0.25)
    run_scheduled(ctl, clock)
    assert sc.calls == 1
    assert ctl.last and ctl.last["failure"] is None
    assert [i["name"] for i in ctl.last["items"]] == names_
    assert ctl.last["best_plat"] == 0 and ctl.last["best_ducats"] == 0  # Xaku: 18p / 45d
    # first capture at trigger + 650 ms; hide at trigger + 14.5 s
    assert ctl.hide_at == pytest.approx(ctl.trigger_at + 14.5)
    assert clock.t >= ctl.trigger_at + 0.65
    assert ov.LAYER in app.overlays.active()
    assert app.events and app.events[-1][0] == "rewards.result"
    assert app.toasts and app.toasts[0][0].startswith("Best: Xaku")  # headless: no window -> toast too
    # close line: hide 1.5 s later at the earliest (trigger + 5 s floor)
    clock.t = ctl.trigger_at + 9
    ctl.on_line("Script [Info]: ProjectionRewardChoice.lua: Relic reward screen shut down")
    assert ctl.hide_at == pytest.approx(clock.t + 1.5)


@needs_data
def test_omnia_shortens_vote_window_and_mission_end_clears(tmp_path, db):
    clock = FakeClock()
    app = fake_app(tmp_path)
    ctl = make_controller(app, StubScanner([_result(db, ["Fang Prime Handle", "Bronco Prime Receiver"], 2, 2)]),
                          clock, StubPrices(PRICES), db)
    ctl.on_line('12.0 Sys [Info]: Mission vote: activeMissionTag="VOIDT6"')
    assert ctl.fissure_tier == "omnia"
    ctl.on_line("13.0 Sys [Info]: Pause countdown done")
    run_scheduled(ctl, clock)
    assert ctl.hide_at == pytest.approx(ctl.trigger_at + 10.0)
    ctl.on_line("50.0 Sys [Info]: EOM missionLocationUnlocked=1")
    assert ctl.fissure_tier is None


@needs_data
def test_trigger_suppression_rules(tmp_path, db):
    clock = FakeClock()
    app = fake_app(tmp_path)
    sc = StubScanner([_result(db, ["Fang Prime Handle"], 1, 1)])
    ctl = make_controller(app, sc, clock, StubPrices(PRICES), db)
    # relic picker within 3 s suppresses (and lastRewardAt is not updated)
    ctl.on_line("100.000 Script [Info]: ThemedProjectionManager.lua: LoadingCompleteEnd")
    ctl.on_line("100.500 Sys [Info]: Pause countdown done")
    run_scheduled(ctl, clock)
    assert sc.calls == 0
    clock.t += 5
    ctl.on_line("106.000 Sys [Info]: Pause countdown done")
    run_scheduled(ctl, clock)
    assert sc.calls == 1
    # cooldown 2.5 s
    ctl.on_line("106.500 Sys [Info]: Got rewards")
    assert ctl.scheduled == []
    # stale line (> 20 s behind the freshest flush) is dropped
    clock.t += 60
    ctl.on_line("140.000 Sys [Info]: Pause countdown done")  # offset grew by 26 s -> stale
    run_scheduled(ctl, clock)
    assert sc.calls == 1
    # a file echo of a real-time trigger is ignored while the real-time source is active
    app.eelog.realtime = True
    app.eelog.source = "realtime"
    clock.t += 10
    ctl.on_line("Sys [Info]: Pause countdown done")
    run_scheduled(ctl, clock)
    assert sc.calls == 2
    app.eelog.source = "file"
    clock.t += 3
    ctl.on_line("200.000 Sys [Info]: Pause countdown done")
    assert ctl.scheduled == []


@needs_data
def test_retries_until_counted_cards_read(tmp_path, db):
    clock = FakeClock()
    app = fake_app(tmp_path)
    partial = _result(db, ["Fang Prime Handle", "Bronco Prime Receiver"], 4, 4)
    full = _result(db, ["Fang Prime Handle", "Bronco Prime Receiver", "Paris Prime Lower Limb",
                        "Vadarya Prime Barrel"], 4, 4)
    sc = StubScanner([partial, full])
    ctl = make_controller(app, sc, clock, StubPrices(PRICES), db)
    out = ctl.run_with_retries()
    assert sc.calls == 2 and len(out["result"].items) == 4 and out["partial"] is None


@needs_data
def test_partial_read_against_counted_cards_shows_nothing(tmp_path, db):
    clock = FakeClock()
    app = fake_app(tmp_path)
    partial = _result(db, ["Fang Prime Handle", "Bronco Prime Receiver"], 4, 4)
    ctl = make_controller(app, StubScanner([partial]), clock, StubPrices(PRICES), db)
    res = ctl.dispatch("eelog")
    assert res["failure"] == "partial" and res["items"] == []


@needs_data
def test_not_reward_screen_after_three_empty_attempts(tmp_path, db):
    from wfh.features.reward_scan import ScanResult

    clock = FakeClock()
    app = fake_app(tmp_path)
    sc = StubScanner([ScanResult(capture_size=DECK)])
    ctl = make_controller(app, sc, clock, StubPrices({}), db)
    res = ctl.dispatch("eelog")
    assert sc.calls == 3 and res["failure"] == "not-reward-screen"
    assert ov.LAYER not in app.overlays.active()


def test_capture_failure_reports_unavailable(tmp_path):
    from wfh import capture

    clock = FakeClock()
    app = fake_app(tmp_path)
    ctl = make_controller(app, StubScanner([None]), clock)

    def boom():
        raise capture.CaptureError("no gamescope")

    ctl._grab = boom
    res = ctl.dispatch("manual")
    assert res["failure"] == "capture-unavailable"
    assert app.toasts and "capture" in app.toasts[-1][0].lower()


def test_game_not_running_skips_eelog_trigger(tmp_path):
    clock = FakeClock()
    app = fake_app(tmp_path)
    sc = StubScanner([None])
    ctl = make_controller(app, sc, clock, running=False)
    assert ctl.dispatch("eelog") is None and sc.calls == 0


def test_disabled_feature_does_not_trigger(tmp_path):
    clock = FakeClock()
    app = fake_app(tmp_path)
    app.settings.update({"relic_rewards": {"enabled": False}})
    sc = StubScanner([None])
    ctl = make_controller(app, sc, clock)
    assert ctl.trigger("manual") is False and sc.calls == 0
    app.settings.update({"relic_rewards": {"enabled": True, "auto_trigger": False}})
    assert ctl.trigger("eelog") is False and ctl.trigger("manual") is True


@needs_data
def test_picker_open_prefetches_reward_prices_once(tmp_path, db):
    clock = FakeClock()
    app = fake_app(tmp_path)
    prices = StubPrices({})
    ctl = make_controller(app, StubScanner([None]), clock, prices, db)
    ctl.on_line("Script [Info]: ThemedProjectionManager.lua: LoadingCompleteEnd")
    assert len(prices.prefetched) > 300 and "xaku_prime_chassis_blueprint" in prices.prefetched
    n = len(prices.prefetched)
    clock.t += 60
    ctl.on_line("Script [Info]: ThemedProjectionManager.lua: PopulateInventoryGrid")
    assert len(prices.prefetched) == n  # once per 10 min


@needs_data
def test_missing_prices_are_fetched_and_redrawn(tmp_path, db):
    clock = FakeClock()
    app = fake_app(tmp_path)
    prices = StubPrices(PRICES, cached=set())  # nothing cached: overlay first shows "…"
    names_ = ["Xaku Prime Chassis Blueprint", "Bronco Prime Receiver"]
    ctl = make_controller(app, StubScanner([_result(db, names_, 2, 2)]), clock, prices, db)
    res = ctl.dispatch("manual")
    assert prices.fetched and set(prices.fetched[0]) >= {"xaku_prime_chassis_blueprint", "xaku_prime_set"}
    first = [e for e in app.events if e[0] == "rewards.result"]
    assert len(first) >= 2  # initial + after prices
    assert ctl.last["items"][0]["plat"] == 18 and ctl.last["items"][0]["set_plat"] == 140
    assert res["items"][0]["price_state"] in ("pending", "ok")


@needs_data
def test_best_pick_waits_for_prices_with_partial_cache(tmp_path, db):
    """Only Bronco cached: no crown/toast until the settle step, then Xaku (18p) wins."""
    clock = FakeClock()
    app = fake_app(tmp_path)
    prices = StubPrices(PRICES, cached={"bronco_prime_receiver"})
    names_ = ["Xaku Prime Chassis Blueprint", "Bronco Prime Receiver"]
    ctl = make_controller(app, StubScanner([_result(db, names_, 2, 2)]), clock, prices, db)
    first = ctl.dispatch("manual")
    assert first["best_plat"] is None  # Xaku price still pending: no early crown
    results = [d for n, d in app.events if n == "rewards.result"]
    assert results[-1]["best_plat"] == 0 and ctl.last["best_plat"] == 0
    assert len(app.toasts) == 1 and app.toasts[0][0].startswith("Best: Xaku")
    assert "18p" in app.toasts[0][1]


@needs_data
def test_scan_exceptions_count_as_no_layout_attempts(tmp_path, db):
    clock = FakeClock()
    app = fake_app(tmp_path)

    class Broken(StubScanner):
        def scan(self, frame, ui_scale):
            self.calls += 1
            raise RuntimeError("model broke")

    sc = Broken([None])
    ctl = make_controller(app, sc, clock, StubPrices({}), db)
    res = ctl.dispatch("eelog")
    assert sc.calls == 3 and res["failure"] == "not-reward-screen"
    assert ov.LAYER not in app.overlays.active()


@needs_data
def test_disabling_mid_price_pass_does_not_redraw(tmp_path, db):
    clock = FakeClock()
    app = fake_app(tmp_path)
    names_ = ["Xaku Prime Chassis Blueprint", "Bronco Prime Receiver"]

    class DisablingPrices(StubPrices):
        def fetch(self, slugs, deadline):
            app.settings.update({"relic_rewards": {"enabled": False}})
            app.overlays.clear(ov.LAYER)  # what an on_settings layer clear does
            return super().fetch(slugs, deadline)

    ctl = make_controller(app, StubScanner([_result(db, names_, 2, 2)]), clock,
                          DisablingPrices(PRICES, cached=set()), db)
    ctl.dispatch("manual")
    assert ov.LAYER not in app.overlays.active()
    assert not app.toasts


@needs_data
def test_failed_scan_saves_capture_with_unique_name(tmp_path, db):
    clock = FakeClock()
    app = fake_app(tmp_path)
    app.settings.update({"debug_save_captures": True})
    ok = _result(db, ["Fang Prime Handle", "Bronco Prime Receiver"], 2, 2)
    ctl = make_controller(app, StubScanner([ok]), clock, StubPrices(PRICES), db)
    ctl.dispatch("manual")
    cap_dir = tmp_path / "runtime" / "captures"
    assert not cap_dir.exists()  # successful scans keep nothing
    partial = _result(db, ["Fang Prime Handle", "Bronco Prime Receiver"], 4, 4)
    ctl.scanner = StubScanner([partial])
    ctl.dispatch("eelog")
    ctl.dispatch("eelog")
    files = sorted(os.listdir(cap_dir))
    assert len(files) == 2 and all("partial" in f for f in files)


@needs_data
def test_file_scan_leaves_live_round_alone(tmp_path, db, monkeypatch):
    from wfh.features.reward_scan import ScanResult

    clock = FakeClock()
    app = fake_app(tmp_path)
    names_ = ["Xaku Prime Chassis Blueprint", "Bronco Prime Receiver"]
    ctl = make_controller(app, StubScanner([_result(db, names_, 2, 2)]), clock, StubPrices(PRICES), db)
    ctl.dispatch("manual")
    assert ov.LAYER in app.overlays.active()
    rnd = ctl.round
    path = str(tmp_path / "blank.png")
    Image.new("RGB", DECK).save(path)
    ctl.scanner = SimpleNamespace(scan=lambda *a, **k: ScanResult(capture_size=DECK), ocr_available=lambda: True)
    out = ctl.scan_file(path, 0.99)
    assert out["failure"] == "not-reward-screen" and ctl.round == rnd
    assert ov.LAYER in app.overlays.active()  # live result still shown
    ctl.in_flight = True
    with pytest.raises(RuntimeError):
        ctl.scan_file(path, 0.99, show=True)


# ---------------------------------------------------------------- overlay + preview

def test_overlay_blocks_sit_under_each_card():
    entries = [RewardEntry(i, n, ducats=d, plat=p, price_state="ok", rarity=r)
               for i, (n, d, p, r) in enumerate([("A Prime Blueprint", 45, 18, "Uncommon"),
                                                   ("B Prime Barrel", 15, 4, "Common")])]
    cards = [(i, geo.crop_box(DECK, c)) for i, c in enumerate(geo.card_rects(2, 0.99, DECK))]
    placed = ov.layout_blocks(entries, cards, DECK, DECK, "below", 0, 0)
    assert len(placed) == 2
    for p, (_, (x, y, w, h)) in zip(placed, cards):
        assert p.y == y + h + ov.GAP
        assert abs((p.x + p.image.width / 2) - (x + w / 2)) <= 2 and p.image.width == w
    above = ov.layout_blocks(entries, cards, DECK, DECK, "above", 0, 0)
    assert all(p.y + p.image.height + ov.GAP == 201 for p in above)
    # capture coordinates map onto a differently sized overlay
    scaled = ov.layout_blocks(entries, cards, DECK, (640, 400), "below")
    assert scaled[0].y == round((201 + 162) / 2) + ov.GAP


def test_name_lines_wrap_then_abbreviate():
    assert ov.name_lines("Fang Prime Handle", 140, 12) == ["Fang Prime Handle"]
    lines = ov.name_lines("Khora Prime Systems Blueprint", 140, 12)
    assert len(lines) == 2 and "…" not in "".join(lines) and lines[0].startswith("Khora Prime")
    long = ov.name_lines("Caliban Prime Neuroptics Blueprint", 100, 12)
    assert len(long) == 2 and all(ov.draw.text_width(l, 12, True) <= 100 for l in long)
    assert "BP" in long[-1] or "…" in long[-1]


def _wait_canvas(mgr: OverlayManager, box, timeout=3.0) -> Image.Image:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with mgr._lock:
            canvas = mgr._canvas.copy()
            dirty = mgr._dirty
        if dirty is None and canvas.crop(box).getbbox():
            return canvas
        time.sleep(0.02)
    return canvas


@needs_models
@needs_data
def test_render_preview_png(tmp_path, db):
    """Runs the pipeline on a Deck-size frame and renders the overlay with the headless manager."""
    frame = sim_deck(public("real-full-4p-fps.png"))
    mgr = OverlayManager(headless=True)
    try:
        app = fake_app(tmp_path, overlays=mgr)
        clock = FakeClock()
        sc = RewardScanner(MODELS, pool_fn=db.prime_parts)
        ctl = rr.RewardController(app, scanner=sc, prices=StubPrices(PRICES), grab=lambda: frame, clock=clock,
                                  sleep=clock.sleep, game_running=lambda: True, ui_scale=lambda: 0.99,
                                  inventory=sample_inventory, items_db=lambda: db)
        res = ctl.dispatch("manual")
        assert res["failure"] is None and res["slot_count"] == 4
        assert [i["name"] for i in res["items"]] == ["Xaku Prime Chassis Blueprint", "Bronco Prime Receiver",
                                                     "Paris Prime Lower Limb", "Vadarya Prime Barrel"]
        assert res["best_plat"] == 0
        layer = mgr._layers[ov.LAYER]
        assert layer.y >= 363 and layer.x <= 313  # under the card row, starting at the first card
        canvas = _wait_canvas(mgr, layer.box)
        out = frame.convert("RGBA")
        out.alpha_composite(canvas)
        os.makedirs(PREVIEWS, exist_ok=True)
        out.convert("RGB").save(os.path.join(PREVIEWS, "rewards.png"))
        assert canvas.crop(layer.box).getbbox() is not None
    finally:
        mgr.stop()


# ---------------------------------------------------------------- module wiring + RPCs

@needs_models
@needs_data
def test_module_loads_and_scan_file_rpc(app, tmp_path):
    from wfh import rpc

    assert "wfh.features.relic_rewards" not in app.services.get("failed_modules", [])
    for name in ("rewards.last", "rewards.scan_file", "rewards.scan_now", "rewards.status", "rewards.clear"):
        assert name in rpc.methods()
    ctl = rr.controller()
    ctl.prices = StubPrices(PRICES)
    path = str(tmp_path / "deck.png")
    public("real-full-4p-16x10.png").resize(DECK, Image.LANCZOS).save(path)
    out = asyncio.run(rpc.call_local("rewards.scan_file", {"path": path, "ui_scale": 0.99}))
    assert out["failure"] is None and out["slot_count"] == 4 and len(out["items"]) == 4
    assert all(same_item(i["name"], "Forma Blueprint") for i in out["items"])
    assert asyncio.run(rpc.call_local("rewards.last")) == out
    st = asyncio.run(rpc.call_local("rewards.status"))
    assert st["ocr_available"] is True
    with pytest.raises(ValueError):
        asyncio.run(rpc.call_local("rewards.scan_file", {"path": str(tmp_path / "missing.png")}))
