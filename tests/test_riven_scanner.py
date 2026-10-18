"""Riven scanner: data, grading, good rolls, parser, replay, geometry, OCR fixtures,
EE.log state machine, orchestrator, market, inventory decode, Rivens menu, preview.

Offline only: warframe.market and docs.google.com are faked; the OCR tests need
models/ and skip without them; data tests need data/riven (scripts/fetch-data.sh).
"""

from __future__ import annotations

import base64
import io
import json
import os
import time

import numpy as np
import pytest
from PIL import Image

from wfh.features import riven_data as rd
from wfh.features import riven_grading as rg
from wfh.features import riven_market as rm
from wfh.features import riven_overlay as rov
from wfh.features import riven_scan as rs
from wfh.features import riven_text as rt
from wfh.features.riven_state import Callbacks, RivenLogStateMachine, parse_cost

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "riven")
MODELS = os.path.join(ROOT, "models")
from conftest import PREVIEWS_DIR

PREVIEW = os.path.join(PREVIEWS_DIR, "riven.png")
DEMO = os.path.join(FIX, "cycle-screen-demo-1280x800.jpg")

needs_data = pytest.mark.skipif(not os.path.exists(os.path.join(ROOT, "data", "riven", "riven.json.gz")),
                                reason="data/riven not built (scripts/fetch-data.sh)")


def _models_ok() -> bool:
    try:
        from wfh.ocr import PaddleRecognizer, StatLineDetector
        return PaddleRecognizer.get(MODELS).available() and StatLineDetector.get(MODELS).available()
    except Exception:  # noqa: BLE001
        return False


needs_models = pytest.mark.skipif(not _models_ok(), reason="OCR models missing")

LEX_SHEET = {
    "lex": {"goodAttrs": [{"mandatory": ["WeaponCritDamageMod"],
                           "optional": ["WeaponFireIterationsMod", "WeaponCritChanceMod"]},
                          {"mandatory": ["WeaponFireIterationsMod", "WeaponDamageAmountMod"], "optional": []}],
            "acceptedBadAttrs": ["WeaponZoomFovMod", "WeaponRecoilReductionMod"]},
    "sobek": {"goodAttrs": [{"mandatory": ["WeaponFireIterationsMod"],
                             "optional": ["WeaponCritChanceMod", "WeaponCritDamageMod", "WeaponDamageAmountMod",
                                          "WeaponFireRateMod"]}],
              "acceptedBadAttrs": ["WeaponZoomFovMod", "WeaponProcTimeMod", "WeaponImpactDamageMod"]},
    "boltor": {"goodAttrs": [{"mandatory": ["WeaponCritDamageMod"], "optional": []}], "acceptedBadAttrs": []},
}


def _offline(url: str) -> bytes:
    raise RuntimeError("offline")


@pytest.fixture(autouse=True)
def _riven_env():
    rd.set_data_dirs(os.path.join(ROOT, "data"), None)
    g = rg.GoodRolls(None, fetch=_offline)
    g.set_data(json.loads(json.dumps(LEX_SHEET)), time.time())
    rg.set_goodrolls(g)
    yield
    rg.set_goodrolls(None)


def S(name, value, positive=True, **kw):
    return {"name": name, "value": value, "positive": positive, **kw}


def grades(weapon, stats, **kw):
    r = rg.grade_riven(weapon, stats, **kw)
    return None if r is None else [s["grade"] for s in r["stats"]]


# ---- data ----------------------------------------------------------------------------------

@needs_data
def test_riven_data_lookups():
    db = rd.db()
    assert db.disposition("Edun") == 1.15 and db.disposition("Syam") == 0.75
    assert db.disposition("Nonexistent Weapon") is None
    assert "RifleRandomModRare" in db.resolve_riven_type("Rubico Prime")
    assert "ShotgunRandomModRare" in db.resolve_riven_type("Kuva Drakgoon")
    assert "ModularMeleeRandomModRare" in db.resolve_riven_type("Sepfahn")
    assert db.resolve_riven_type("Artemis Bow") is None
    assert db.is_melee("Deconstructor") and not db.is_melee("Verglas")
    assert db.family_slug("Boar Prime") == "boar" and db.family_slug("Prisma Obex") == "obex"
    assert db.family_slug("Kuva Bramma") == "kuva_bramma" and db.family_slug("Dex Nikana") == "dex_nikana"
    assert db.name_by_unique("/Lotus/Weapons/Grineer/KuvaLich/LongGuns/Sobek/KuvaSobek") == "Kuva Sobek"
    assert db.find_weapon_in_text("Sobek Croni-zetican\n+72.7% Fire Rate") == "Sobek"
    assert db.find_weapon_by_label_line(["SHOW", "Kuva Sobek"]) == ("Kuva Sobek", True)
    shotgun = db.resolve_riven_type("Boar")
    assert db.riven_suffix(shotgun, [("WeaponFireRateMod", 5e8), ("WeaponFireIterationsMod", 9e8)]) == "Satidra"
    assert db.riven_suffix(shotgun, [("WeaponFireIterationsMod", 9e8), ("WeaponCritChanceMod", 6e8),
                                     ("WeaponFireRateMod", 3e8)]) == "Sati-critadra"


@needs_data
def test_wfm_family_slugs_match_fixture():
    rows = json.load(open(os.path.join(FIX, "wfm-riven-weapons.json")))
    db = rd.db()
    misses = [r for r in rows if db.name_by_unique(r["gameRef"])
              and db.family_slug(db.name_by_unique(r["gameRef"])) != r["slug"]]
    assert len(misses) <= len(rows) // 50, misses[:5]


# ---- grading -------------------------------------------------------------------------------

def test_grade_constants():
    assert [rg.float_to_grade(f, False) for f in (1.0, 0.975, 0.974, 0.875, 0.874, 0.5, 0.025, 0.024)] == \
        ["S", "S", "A+", "A+", "A", "B", "C-", "F"]
    assert rg.float_to_grade(0.0, True) == "S" and rg.float_to_grade(1.0, True) == "F"
    assert rg.unparse_buff(157.5, 0.016666, 0.7, 1, 0, "WeaponCritChanceMod") == pytest.approx(0.5, abs=0.05)
    assert rg.unparse_buff(173.2, 0.016666, 0.7, 1, 0, "WeaponCritChanceMod") == pytest.approx(1.0, abs=0.05)
    assert rg.unparse_buff(107.3, 0.016666, 0.7, 3, 1, "WeaponCritChanceMod") == pytest.approx(0.95, abs=0.05)
    assert rg.unparse_curse(49.1, -0.01, 0.7, 3, 1, "WeaponRecoilReductionMod") == pytest.approx(0.696, abs=0.05)
    assert rg.unparse_buff(50, 0, 1.0, 1, 0) == 0.5


@needs_data
def test_grade_riven_vectors():
    assert grades("Edun", [S("Melee Damage", 177.6), S("Critical Damage", 101.3), S("Attack Speed", 56.8),
                           S("Impact", 101.2, False)]) == ["B", "A-", "C+", "B+"]
    assert grades("Boar Prime", [S("Critical Damage", 109.9), S("Damage", 202.5), S("Critical Chance", 124),
                                 S("Impact", 121, False)]) == ["B-", "B-", "S", "B"]
    assert grades("Tombfinger", [S("Critical Damage", 66.4), S("Multishot", 86.4), S("Critical Chance", 115.6),
                                 S("Zoom", 51.1, False)]) == ["C", "C-", "B-", "B"]
    boar = grades("Boar", [S("Multishot", 199.3), S("Critical Chance", 163.6), S("Slash", 75.9, False)])
    assert boar[0] == "B" and boar[1] in ("S", "A+") and boar[2] == "A-"
    drak = rg.grade_riven("Kuva Drakgoon", [S("Reload Speed", 78.1), S("Multishot", 181.6),
                                            S("Status Duration", 52.3, False)])
    assert all(0 < s["rollFloat"] < 1 for s in drak["stats"])
    assert rg.grade_riven("Kuva Nukor", [S("Status Chance", 42.6), S("Heat", 41.2), S("Reload Speed", 23.0),
                                         S("Zoom", 29.8)]) is None
    assert rg.grade_riven("Made Up Weapon", [S("Critical Chance", 100)]) is None
    assert grades("Rubico Prime", [S("Critical Chance", 9999)]) == ["S"]
    assert grades("Rubico Prime", [S("Critical Chance", None)]) == ["?"]
    unk = rg.grade_riven("Rubico Prime", [S("Critical Chance", 90), S("Some Unknown Stat", 42)])["stats"][1]
    assert unk["grade"] == "B" and unk["rollFloat"] == 0.5


@needs_data
def test_correct_scanned_stats():
    nami = lambda mid: [S("Additional Combo Count Chance", 69.3), S(mid, 190.2), S("Heat", 95.8)]  # noqa: E731
    for mid in ("Damage", "Critical Damage", "Melee Damage"):
        assert rg.correct_scanned_stats("Nami Solo", nami(mid))[1]["name"] == "Melee Damage"
    obex = [S("Range", 2.3), S("Critical Damage", 104.6), S("Finisher Damage", 141.8)]
    assert [s["name"] for s in rg.correct_scanned_stats("Prisma Obex", obex)] == \
        ["Range", "Critical Damage", "Finisher Damage"]
    assert [s["name"] for s in rg.correct_scanned_stats("Soma", [S("Critical Chance", 150), S("Damage", 165)])] == \
        ["Critical Chance", "Damage"]


# ---- good-roll sheet -----------------------------------------------------------------------

@needs_data
def test_attribute_grades():
    g = rg.goodrolls()
    stats = lambda pos, neg=None: [S(n, 1) for n in pos] + ([S(neg, 1, False)] if neg else [])  # noqa: E731
    assert g.attribute_grade(stats(["Critical Damage", "Multishot"], "Zoom"), "Lex") == "Great"
    assert g.attribute_grade(stats(["Critical Damage", "Multishot"], "Critical Chance"), "Lex") == "OK"
    assert g.attribute_grade(stats(["Multishot"], "Zoom"), "Lex") == "OK"
    assert g.attribute_grade(stats(["Reload Speed"], "Zoom"), "Lex") == "Bad"
    assert g.attribute_grade(stats(["Multishot", "Damage"], "Recoil"), "Lex") == "Great"
    assert g.attribute_grade(stats(["Critical Damage"], "Zoom"), "Made Up Weapon") == "?"
    assert g.lookup_name("Telos Boltor") == "boltor" and g.lookup_name("Boltor Prime") == "boltor"
    assert g.best_attributes("Kuva Sobek")["positives"][0] == "Multishot"


def test_good_roll_csv_and_cache(tmp_path):
    csv = ('﻿Weapon,Positive,Negative,Notes\n'
           'Lex,CD MS/CC or MS DMG,ZOOM/REC,"a ""quoted"" note"\r\n'
           'Galatine,CD AS RANGE,EFF\n')
    rows = dict(rg.parse_good_roll_csv(csv, "primary"))
    assert rows["lex"]["goodAttrs"][0] == {"mandatory": ["WeaponCritDamageMod"],
                                           "optional": ["WeaponFireIterationsMod", "WeaponCritChanceMod"]}
    assert rows["lex"]["goodAttrs"][1]["mandatory"] == ["WeaponFireIterationsMod", "WeaponDamageAmountMod"]
    assert rows["lex"]["acceptedBadAttrs"] == ["WeaponZoomFovMod", "WeaponRecoilReductionMod"]
    melee = dict(rg.parse_good_roll_csv("weapon,positive,negative\nSkana,DMG CD,IMP\n", "melee"))
    assert melee["skana"]["goodAttrs"][0]["mandatory"][0] == "WeaponMeleeDamageMod"

    calls = []

    def fetch(url):
        calls.append(url)
        return csv.encode() if "gid=0" in url else b"weapon,positive,negative\n"

    now = [1_800_000_000.0]
    g = rg.GoodRolls(str(tmp_path), fetch=fetch, clock=lambda: now[0])
    g.ensure_loaded(wait=True)
    assert g.lookup_name("Lex") == "lex" and len(calls) == 5
    assert os.path.exists(os.path.join(tmp_path, rg.CACHE_FILE))
    # a fresh process reads the cache and does not refetch inside 7 days
    g2 = rg.GoodRolls(str(tmp_path), fetch=_offline, clock=lambda: now[0] + 3600)
    g2.ensure_loaded(wait=True)
    assert g2.lookup_name("Galatine") == "galatine" and not g2.stale()
    # stale after 7 days; a failed refresh keeps the stale cache
    g3 = rg.GoodRolls(str(tmp_path), fetch=_offline, clock=lambda: now[0] + 8 * 86400)
    g3.ensure_loaded(wait=True)
    assert g3.stale() and g3.lookup_name("Lex") == "lex" and g3.status()["error"]


# ---- parser and replay ---------------------------------------------------------------------

def test_parse_and_postprocess():
    text = "Sobek Croni-zetican\n+72,7% Fire Rate (x2 for\nBows)\n-66,2% Weapon Recoil\n+85,7% Multishot\n-65,1% Status Duration"
    stats = rt.parse_riven_stats(text)
    assert [rt.format_stat(s) for s in stats] == \
        ["+72.7 Fire Rate", "-66.2 Weapon Recoil", "+85.7 Multishot", "-65.1 Status Duration"]
    recoil = stats[1]
    assert recoil["positive"] is True and recoil["displayPositive"] is False
    mult = rt.parse_riven_stats("x0.84 Damage to Corpus\n+120% Multishot")
    assert mult[0]["multiplier"] and mult[0]["positive"] is False and mult[0]["value"] == 0.84
    merged = rt.merge_split_lines([("+167.2% Status", 0.9), ("Chance", 0.85), ("+1.2 Punch Through", 0.95)])
    assert len(merged) == 2 and merged[0][1] == 0.85
    assert rt.has_low_confidence_line([("+1.0% Zoom", 0.79)]) and not rt.has_low_confidence_line([("Title", 0.1)])
    assert rs.is_incomplete_read([S("A", 1), S("B", 1), S("C", 1, False)], False, False)
    assert not rs.is_incomplete_read([S("A", 1), S("B", 1), S("C", 1), S("D", 1, False)], True, False)


REPLAY = json.load(open(os.path.join(FIX, "roll-scan-replay.json")))


def _fmt(stat):
    t = rt.format_stat(stat)
    return t[:-len("Recoil")] + "Weapon Recoil" if t.endswith(" Recoil") and "Weapon Recoil" not in t else t


def _replay(scan):
    blank = np.full((48, 64, 3), (10, 10, 18), np.uint8)
    for cap in scan["captures"]:
        queue = [rs.ocr_read_from_lines(a["lines"], a["yoloBoxes"]) for a in cap["attempts"]]
        r = rs.recognize_card_stats(blank, blank, lambda _img: queue.pop(0) if queue else rs.OcrRead(),
                                    roll_reveal=True)
        if len(r.stats) >= rs.MIN_ACCEPTABLE_STATS:
            return r.stats
    return []


def _parse_expected(text):
    import re
    m = re.match(r"^([+-]|x)([\d.]+) (.+)$", text)
    mult = m.group(1) == "x"
    value = float(m.group(2))
    shown = value >= 1 if mult else m.group(1) == "+"
    positive = (not shown) if m.group(3).endswith("Recoil") else shown
    out = {"name": m.group(3), "positive": positive, "value": value}
    if positive != shown:
        out["displayPositive"] = shown
    if mult:
        out["multiplier"] = True
    return out


def test_roll_scan_replay():
    outcomes = {s["scan"]: _replay(s) for s in REPLAY}
    read = []
    for s in REPLAY:
        if not s.get("expected"):
            continue
        got = sorted(_fmt(x) for x in outcomes[s["scan"]])
        if got:
            assert (s["scan"], got) == (s["scan"], sorted(s["expected"]))  # never short or wrong
            read.append(s["scan"])
    assert len(read) == 36 and 7 not in read
    for s in REPLAY:
        if s.get("recapture"):
            assert outcomes[s["scan"]] == [], s["scan"]
    kept = {19: 11, 43: 20}
    for s in REPLAY:
        if s.get("stale"):
            got = outcomes[s["scan"]]
            card = [_parse_expected(t) for t in REPLAY[kept[s["scan"]]]["expected"]]
            assert not got or rt.looks_like_stale_card_read(got, [card])


def test_choice_similarity_and_stale():
    old = [S("Multishot", 159), S("Damage", 276.2), S("Projectile Speed", 90.9, False)]
    new = [S("Critical Chance", 150.1), S("Multishot", 120), S("Zoom", 40, False)]
    assert rt.score_stat_similarity(old, old) > rt.score_stat_similarity(old, new) + 6
    assert rt.looks_like_stale_card_read([S("Multishot", 159.04), S("Damage", 276.2)], [old])
    assert rt.roll_rescan_reason([], [old]) == "read nothing"
    assert rt.roll_rescan_reason(new, [old]) is None


# ---- geometry --------------------------------------------------------------------------------

def test_deck_geometry():
    frame = np.full((800, 1280, 3), 60, np.uint8)
    content = rs.content_rect(frame, "window")
    assert content == rs.PxRect(0, 40, 1280, 720)
    assert rs.content_rect(frame, "window", "full") == rs.PxRect(0, 0, 1280, 800)
    assert rs.crop_box(rs.CROPS["singleCard"], content) == rs.PxRect(281, 349, 716, 324)
    assert rs.crop_box(rs.CROPS["chatCard"], content) == rs.PxRect(281, 256, 716, 324)
    assert rs.crop_box(rs.CROPS["rollCard"], content) == rs.PxRect(460, 339, 358, 312)
    assert rs.crop_box(rs.FITS_IN_NAME_CROP, content) == rs.PxRect(998, 558, 268, 158)
    card, band = rs.crop_stat_image(frame, rs.CROPS["singleCard"], "window")
    assert card.shape[:2] == (324, 716) and band.shape[:2] == (162, 224)
    _, roll_band = rs.crop_stat_image(frame, rs.CROPS["rollCard"], "window")
    assert roll_band.shape[:2] == (156, 216)
    assert [rs.stat_crop_upscale_factor(h) for h in (162, 228, 400, 156, 0)] == [2, 2, 1, 3, 1]
    # letterboxed 16:9 output inside 1280x800 lands on the same canvas
    boxed = np.zeros((800, 1280, 3), np.uint8)
    boxed[40:760] = 90
    assert rs.content_rect(boxed, "screen") == content


def test_text_metrics_and_hash():
    img = np.zeros((162, 224, 3), np.uint8)
    for y in (30, 60, 90, 120):
        img[y:y + 10, 20:200] = 230
    m = rs.text_metrics(img)
    assert m.score >= rs.READY_SCORE_THRESHOLD and m.bounds is not None and m.row_groups >= 3
    assert rs.text_metrics(np.zeros((162, 224, 3), np.uint8)).score < 0.2
    assert rs.frame_hash(img) == rs.frame_hash(img.copy()) and rs.frame_hash(img) != rs.frame_hash(img // 2)
    fb = rs.crop_stat_area_fallback(np.pad(img, ((40, 40), (40, 40), (0, 0))))
    assert fb is not None and fb[1] >= 1


def test_readiness_gate_and_retry():
    img = np.zeros((800, 1280, 3), np.uint8)
    for y in (470, 500, 530, 560):
        img[y:y + 12, 560:720] = 230
    t = [0.0]
    caps = []

    def capture():
        caps.append(1)
        return rs.Capture(img, "window", t[0])

    def sleep(dt):
        t[0] += dt

    reads = [rs.ocr_read_from_lines([("Lex Critacron", 0.95)]),
             rs.ocr_read_from_lines([("Lex Critacron", 0.95)]),
             rs.ocr_read_from_lines([("Lex Critacron", 0.95), ("+120.1% Critical Chance", 0.93),
                                     ("+90.5% Multishot", 0.92)])]
    sc = rs.RivenScanner("", capture, ocr=lambda _x: reads.pop(0) if reads else rs.OcrRead(), clock=lambda: t[0],
                         sleep=sleep)
    ok, cap, _h = sc.wait_ready(rs.CROPS["singleCard"], 1.8)
    assert ok and len(caps) == 3
    # identical frame on retry: skipped, so the sparse read stands
    res = sc.scan("initial")
    assert res.stats == [] and res.ready
    # a stale generation stops a scan
    sc.abort()
    assert sc.scan("roll").stats == []


# ---- OCR on fixtures (models needed) ------------------------------------------------------------

def _read(file, scale=None):
    a = np.asarray(Image.open(os.path.join(FIX, file)).convert("RGB"))
    up = rs.stat_crop_upscale_factor(a.shape[0]) if scale is None else scale
    if up > 1:
        a = np.asarray(Image.fromarray(a).resize((a.shape[1] * up, a.shape[0] * up), Image.LANCZOS))
    return rs.recognize_stat_area(a, MODELS)


@needs_models
def test_ocr_fixture_cards():
    want = ["+159 Multishot", "+276.2 Damage", "+93.2 Fire Rate", "-90.9 Projectile Speed"]
    for f in ("chat-card-angstrum-stats.png", "mod-card-angstrum-stats.png"):
        assert [rt.format_stat(s) for s in rt.parse_riven_stats(_read(f).text)] == want, f
    assert [rt.format_stat(s) for s in rt.parse_riven_stats(_read("mod-card-soma-trait-locked-stats.png").text)] == \
        ["x1.5 Damage to Grineer", "+167.2 Critical Chance"]
    assert [rt.format_stat(s) for s in rt.parse_riven_stats(
        _read("choice-card-soma-trait-locked-stats.png").text)] == ["+133.7 Puncture", "+167.2 Critical Chance"]
    arum = _read("chat-card-arum-heat-icon-stats.png")
    assert not rt.has_low_confidence_line(arum.lines)
    assert [f"{s['value']:g} {s['name']}" for s in rt.parse_riven_stats(arum.text)] == \
        ["86.5 Heat", "86.3 Critical Damage", "140.9 Melee Damage"]


@needs_models
def test_ocr_corufell_roll_crop():
    frame = np.zeros((1080, 1920, 3), np.uint8)
    p = np.asarray(Image.open(os.path.join(FIX, "roll-card-corufell-trait-locked.png")).convert("RGB"))
    x, y = int(1920 * 0.36), int(1080 * 0.416)
    frame[y:y + p.shape[0], x:x + p.shape[1]] = p[:1080 - y, :1920 - x]
    res = rs.RivenScanner(MODELS, capture=lambda: None).scan_image(frame, "roll", "window")
    assert [rt.format_stat(s) for s in res.stats] == \
        ["+110.1 Finisher Damage", "+88.1 Heat", "+166.4 Critical Chance", "-101.5 Puncture"]


@needs_models
@needs_data
def test_deck_frame_scan_and_fits_in():
    """Deck-sized Cycle screen (WFHelper's 1080p demo scaled into the 16:9 band of 1280x800)."""
    rgb = np.asarray(Image.open(DEMO).convert("RGB"))
    res = rs.RivenScanner(MODELS, capture=lambda: None).scan_image(rgb, "initial", "window")
    assert [rt.format_stat(s) for s in res.stats] == \
        ["+72.7 Fire Rate", "-66.2 Weapon Recoil", "+85.7 Multishot", "-65.1 Status Duration"]
    assert rd.db().find_weapon_in_text(res.text) == "Sobek"
    m = rs.FitsInReader(MODELS).read(rgb, "window")
    assert m is not None and (m.name, m.exact) == ("Kuva Sobek", True)


def test_label_priority():
    m = rs.LabelMatch("Kuva Sobek", True)
    assert rs.should_apply_label_weapon(m, "", "", False)
    assert not rs.should_apply_label_weapon(m, "Kuva Sobek", "dialog", True)
    assert rs.should_apply_label_weapon(m, "Sobek", "dialog", True)
    assert not rs.should_apply_label_weapon(rs.LabelMatch("Soma", False), "Sobek", "diorama", False)
    assert rs.should_apply_label_weapon(rs.LabelMatch("Soma", False), "Sobek", "ocr", False)


def test_clahe_keeps_shape():
    g = (np.arange(130 * 70) % 97).reshape(130, 70).astype(np.uint8)
    out = rs.clahe(g)
    assert out.shape == g.shape and out.dtype == np.uint8 and out.max() > g.max()


# ---- EE.log state machine ------------------------------------------------------------------------

OPEN = "Sys [Info]: Created /Lotus/Interface/OmegaRerollSelection.swf @ 0x12345678 of class OmegaRerollSelectionScreen"
DIORAMA = "Script [Info]: OmegaRerollSelection.lua: Diorama setup complete"
CYCLE = ("Script [Info]: Dialog.lua: Dialog::CreateOkCancel(description=Are you sure you want to cycle "
         "Burston Sati-critades for 3.500?, leftItem=/Menu/Confirm_Item_Yes, rightItem=/Menu/Confirm_Item_No)")
CYCLE_SOBEK = ("Script [Info]: Dialog.lua: Dialog::CreateOkCancel(description=Are you sure you want to cycle "
               "Sobek Croni-zetican for 4,000?, leftItem=/Menu/Confirm_Item_Yes, rightItem=/Menu/Confirm_Item_No)")
CHOICE = ("Dialog::CreateOkCancel(description=Cycle Riven into current selection?, "
          "leftItem=/Menu/Confirm_Item_Yes, rightItem=/Menu/Confirm_Item_No)")
NONINTERACTIVE = ("Script [Info]: Dialog.lua: Dialog::CreateOkCancel(description=/Lotus/Language/Menu/"
                  "NavBar_QuickMatchPleaseWait, leftItem=nil, rightItem=nil)")
GENERIC = "Script [Info]: Dialog.lua: Dialog::CreateOkCancel(description=/Lotus/Language/Menu/RerollConfirm, leftItem=/Menu/Confirm_Item_Yes)"
OK = "Script [Info]: Dialog.lua: Dialog::SendResult(4)"
CANCEL = "Script [Info]: Dialog.lua: Dialog::SendResult(5)"
CLOSE = "NpcManager::ClearAgents() ReadyToCreateAgents = false"
WEAPON = "48.096 Sys [Info]: Resource load completed 0x00000245D4699890 (/Lotus/Weapons/Grineer/KuvaLich/LongGuns/Sobek/KuvaSobek) in one pass"
HUD1 = "530.304 Script [Info]: ThemedDetailedPurchaseDialog.lua: DBG: HudVis 1"
HUD0 = "540.000 Script [Info]: ThemedDetailedPurchaseDialog.lua: DBG: HudVis 0"
CHAT_WEAPON = "530.357 Sys [Info]: ResourceLoader 0x000001ABBE845340 (/Lotus/Weapons/Tenno/Shotgun/PrimeBoar) Found 1,081 items"
POPULATE = "530.415 Script [Info]: ThemedDetailedPurchaseDialog.lua: PopulateInfo->/Lotus/StoreItems/Upgrades/Mods/Randomized/LotusShotgunRandomModRare"


class FakeTimer:
    def __init__(self):
        self.q = []

    def __call__(self, delay, fn):
        class H:
            cancelled = False

            def cancel(self):
                self.cancelled = True

        h = H()
        self.q.append((delay, fn, h))
        return h

    def run(self, max_delay=100.0):
        while True:
            due = [x for x in self.q if not x[2].cancelled and x[0] < max_delay]
            if not due:
                return
            self.q.remove(due[0])
            due[0][1]()

    def fire_idle(self):
        for d, fn, h in list(self.q):
            if d >= 100 and not h.cancelled:
                fn()


def _machine():
    t = [1000.0]
    ev = []
    cb = Callbacks(on_session_open=lambda: ev.append("open"), on_session_close=lambda: ev.append("close"),
                   on_chat_view=lambda: ev.append("chat"), on_roll_pending=lambda w, k: ev.append(("pending", w, k)),
                   on_roll_confirmed=lambda: ev.append("roll"), on_choice_confirmed=lambda: ev.append("choice"),
                   on_weapon_path=lambda p: ev.append(("path", p)))
    timer = FakeTimer()
    return RivenLogStateMachine(cb, clock=lambda: t[0], timer=timer), t, ev, timer


def test_state_machine_cycle_flow():
    sm, t, ev, timer = _machine()
    sm.process(OPEN)
    sm.process(OPEN)  # burst: absorbed by the active session
    sm.process(WEAPON)
    sm.process(DIORAMA)
    sm.process(CYCLE)
    t[0] += 1
    assert sm.process(OK) is True
    sm.process(CHOICE)
    t[0] += 1
    sm.process(OK)
    sm.process(GENERIC)  # within 600 ms of the accepted SendResult: ignored
    t[0] += 1
    sm.process(GENERIC)  # localized cycle dialog -> generic fallback
    sm.process(CANCEL)
    t[0] += 1
    sm.process(GENERIC)
    sm.process(OK)
    assert ev == ["open", ("path", "/Lotus/Weapons/Grineer/KuvaLich/LongGuns/Sobek/KuvaSobek"),
                  ("pending", "Burston Sati-critades", 3500), "roll", "choice", ("pending", "", 0),
                  ("pending", "", 0), "roll"]
    sm.process(NONINTERACTIVE)
    assert sm.pending_dialog is None
    sm.process(CLOSE)
    assert ev[-1] == "close" and not sm.active
    assert [parse_cost(c) for c in ("4,000", "3.500", "4 000", "")] == [4000, 3500, 4000, 0]


def test_state_machine_close_gating_and_cooldowns():
    sm, t, ev, timer = _machine()
    sm.process(OPEN)
    sm.process(CLOSE)  # loading-screen close line before the diorama: ignored
    assert ev == ["open"] and sm.active
    sm.process(DIORAMA)
    sm.process("123 ytes of recycled effects")
    assert ev == ["open", "close"]
    t[0] += 3
    sm.process(OPEN)
    sm.force_end()  # overlay dismissed while the screen is open
    sm.process(CYCLE)  # force-ended: a dialog cannot resurrect the session for 5 s
    assert ev == ["open", "close", "open"] and not sm.active
    t[0] += 6
    sm.process(CYCLE)
    assert ev[-1] == ("pending", "Burston Sati-critades", 3500)
    # duplicate SendResult(4) inside 400 ms
    sm.process(OK)
    sm.process(CHOICE)
    t[0] += 0.1
    sm.process(OK)
    assert ev.count("choice") == 0
    # idle backstop closes
    timer.fire_idle()
    assert ev[-1] == "close" and not sm.active


def test_state_machine_realtime_skips_file_echoes():
    sm, t, ev, _ = _machine()
    sm.process(OPEN, "file", realtime_active=True)
    assert ev == []
    sm.process(OPEN, "realtime", realtime_active=True)
    sm.process(DIORAMA, "file", realtime_active=True)  # readiness accepted from either source
    sm.process(CYCLE, "realtime", True)
    assert sm.process(OK, "file", True) is True and "roll" not in ev  # consumed, not acted on
    sm.process(OK, "realtime", True)
    assert ev[-1] == "roll"
    # trailing close markers on the real-time feed
    sm.process(CLOSE, "realtime", True)
    sm.process(OPEN, "realtime", True)
    t[0] += 0.5
    sm.process(DIORAMA, "realtime", True)
    sm.process(CLOSE, "realtime", True)
    assert ev.count("close") == 1


def test_state_machine_chat_link():
    sm, t, ev, _ = _machine()
    sm.process(HUD1)
    sm.process(CHAT_WEAPON)
    t[0] += 0.1
    sm.process(POPULATE)
    assert ev == ["chat", ("path", "/Lotus/Weapons/Tenno/Shotgun/PrimeBoar")]
    sm.process(HUD0)
    assert ev[-1] == "close" and not sm.chat_view_active
    # PopulateInfo outside the 2 s window is not a chat view
    sm.process(HUD1)
    t[0] += 3
    sm.process(POPULATE)
    assert ev.count("chat") == 1


# ---- orchestrator --------------------------------------------------------------------------------

CARD_OLD = [S("Fire Rate", 72.7), S("Weapon Recoil", 66.2, True, displayPositive=False), S("Multishot", 85.7),
            S("Status Duration", 65.1, False)]
CARD_NEW = [S("Multishot", 95.5), S("Critical Chance", 73.3), S("Critical Damage", 68.2), S("Impact", 69.1, False)]

WFM_PAYLOAD = {"payload": {"auctions": [
    {"id": "64f0aa01", "owner": {"ingame_name": "Tenno1", "status": "ingame"}, "buyout_price": 450,
     "starting_price": 300, "is_direct_sell": False,
     "item": {"name": "croni-zetican", "weapon_url_name": "sobek", "re_rolls": 12,
              "attributes": [{"url_name": "multishot", "value": 110.2, "positive": True},
                             {"url_name": "critical_chance", "value": 130.0, "positive": True},
                             {"url_name": "critical_damage", "value": 95.0, "positive": True},
                             {"url_name": "impact", "value": -70.0, "positive": False}]}},
    {"id": "64f0aa02", "owner": {"ingame_name": "Tenno2", "status": "online"}, "buyout_price": None,
     "starting_price": 180, "is_direct_sell": True,
     "item": {"name": "visi-satilis", "weapon_url_name": "sobek", "re_rolls": 3,
              "attributes": [{"url_name": "multishot", "value": 90.0, "positive": True},
                             {"url_name": "fire_rate", "value": 70.0, "positive": True},
                             {"url_name": "damage_vs_corpus", "value": 0.95, "positive": False}]}},
    {"id": "64f0aa03", "owner": {"ingame_name": "Tenno3", "status": "offline"}, "buyout_price": 90,
     "starting_price": 90, "is_direct_sell": True,
     "item": {"name": "toxi-pura", "weapon_url_name": "sobek", "re_rolls": 0,
              "attributes": [{"url_name": "toxin_damage", "value": 80.0, "positive": True},
                             {"url_name": "reload_speed", "value": 40.0, "positive": True}]}},
]}}


class FakeScanner:
    last_capture_ms = None

    def __init__(self, cards):
        self.cards = cards
        self.calls = []

    def abort(self):
        pass

    def reset_abort(self):
        pass

    def ocr_available(self):
        return True

    def capture(self):
        return None

    def scan(self, profile):
        self.calls.append(profile)
        stats, text = self.cards[profile].pop(0) if isinstance(self.cards[profile], list) and \
            self.cards[profile] and isinstance(self.cards[profile][0], tuple) else (self.cards[profile], "")
        return rs.ScanResult(stats=[dict(s) for s in stats], text=text, capture=None, outcome="ok")


def _controller(app, cards, toasts=None):
    from wfh.features import riven_scanner as R

    timer = FakeTimer()
    fetched = []

    def fetch(url):
        fetched.append(url)
        return WFM_PAYLOAD

    c = R.RivenController(app, timer=timer, submit=lambda fn, *a: fn(*a), sleep=lambda s: None,
                          scanner=FakeScanner(cards), search=rm.SimilarSearch(fetch=fetch), fetch_goodrolls=False,
                          dispatch=lambda fn, *a: fn(*a))
    return c, timer, fetched


@needs_data
def test_controller_roll_and_choice(app):
    app.settings.update({"overlays_enabled": True, "riven_scanner": {"enabled": True}})
    cards = {"initial": [(CARD_OLD, "Sobek Croni-zetican\n")], "roll": [(CARD_NEW, "Sobek Critatox\n")],
             "choice": [(CARD_NEW, "")], "chat": []}
    c, timer, fetched = _controller(app, cards)
    for line in (OPEN, WEAPON, DIORAMA):
        c.sm.process(line)
    timer.run()
    st = c.state()
    assert st["weapon"] == "Kuva Sobek" and st["weaponSource"] == "diorama"
    assert st["left"]["grade"]["overallGrade"] and len(st["left"]["stats"]) == 4
    assert fetched and "weapon_url_name=sobek" in fetched[0]
    c.sm.process(CYCLE_SOBEK)
    st = c.state()
    assert st["weapon"] == "Kuva Sobek"  # the dialog's family does not replace the diorama variant
    c.clock = lambda: time.monotonic() + 10
    c.sm.process(OK)
    timer.run()
    st = c.state()
    assert st["rollCount"] == 1 and st["kuvaPerRoll"] == 4000 and len(st["right"]["stats"]) == 4
    left, right = c.panel_views()
    assert right["roll_count"] == 1 and left["listings"] and right["listings"][0]["match"] == 100
    assert left["better"] != right["better"]
    assert set(app.overlays.active()) >= {"riven", "riven_right"}
    # the player keeps the new card: the choice rescan reads it back
    c.sm.process(CHOICE)
    c.sm.last_send_result_at = None
    c.sm.process(OK)
    timer.run()
    st = c.state()
    assert [s["name"] for s in st["left"]["stats"]] == [s["name"] for s in CARD_NEW]
    assert st["right"]["stats"] == []
    c.on_session_close()
    assert "riven" not in app.overlays.active() and not c.state()["active"]


@needs_data
def test_controller_stale_roll_and_failures(app):
    app.settings.update({"overlays_enabled": True, "riven_scanner": {"enabled": True}})
    cards = {"initial": [([], "")], "roll": [(CARD_OLD, ""), (CARD_OLD, ""), (CARD_OLD, "")], "choice": [],
             "chat": []}
    c, timer, _ = _controller(app, cards)
    c.on_session_open()
    timer.run()
    st = c.state()
    assert st["left"]["error"] == rov.STRINGS["readFailed"]
    c.s.initial_stats = [dict(s) for s in CARD_OLD]
    c.on_roll_confirmed()
    timer.run()
    st = c.state()
    assert c.scanner.calls.count("roll") == 3 and st["right"]["stats"] == []
    assert st["right"]["error"] == rov.STRINGS["rollReadFailed"]
    c.close()


@needs_data
def test_controller_toast_fallback(app):
    app.settings.update({"overlays_enabled": True, "riven_scanner": {"enabled": True}})
    toasts = []
    orig_toast, orig_headless = app.toast, app.overlays.headless
    app.toast = lambda title, body="": toasts.append((title, body))
    app.overlays.headless = False
    try:
        c, timer, _ = _controller(app, {"initial": [(CARD_OLD, "Sobek Croni-zetican\n")], "roll": [],
                                        "choice": [], "chat": []})
        c.on_session_open()
        timer.run()
        assert toasts and toasts[-1][0] == "Sobek riven" and "Fire Rate" in toasts[-1][1]
        assert "riven" not in app.overlays.active()
        c.close()
    finally:
        app.toast, app.overlays.headless = orig_toast, orig_headless


def test_controller_respects_disabled(app):
    app.settings.update({"riven_scanner": {"enabled": False}})
    try:
        c, timer, _ = _controller(app, {"initial": [], "roll": [], "choice": [], "chat": []})
        c.on_session_open()
        timer.run()
        assert not c.state()["active"] and "riven" not in app.overlays.active()
    finally:
        app.settings.update({"riven_scanner": {"enabled": True}})


def test_rpcs_registered(app):
    from wfh import rpc

    names = set(rpc.methods())
    for n in ("riven.state", "riven.status", "riven.rescan", "riven.close", "riven.set_weapon", "riven.weapons",
              "riven.similar", "riven.scan_file", "riven.data_status", "riven.data_refresh",
              "riven.goodrolls_refresh", "rivens.owned", "rivens.similar", "rivens.sheet"):
        assert n in names, n
    assert "wfh.features.riven_scanner" not in app.services.get("failed_modules", [])


# ---- market ---------------------------------------------------------------------------------------

def test_market_parse_pool_similarity():
    rows = rm.parse_auctions(WFM_PAYLOAD)
    assert [r["platinum"] for r in rows] == [450, 180, 90]
    assert rows[1]["stats"][2]["name"] == "Damage Vs Corpus" and rows[0]["sellerStatus"] == "ingame"
    pct, matched = rm.similarity(["multishot", "critical chance", "critical damage", "impact"], rows[0]["stats"])
    assert pct == 100 and len(matched) == 4
    assert rm.similarity(["multishot", "heat"], rows[2]["stats"])[0] == 0
    many = [dict(rows[0], id=str(i), isDirectSell=False) for i in range(30)] + \
        [dict(rows[2], id=f"d{i}", buyoutPrice=100 - i) for i in range(40)]
    pool = rm.similar_pool(many, 30)
    assert len(pool) == 60 and pool[30]["buyoutPrice"] == 61
    ranked = rm.rank_similar(CARD_NEW, rows, auctions_shown=False)
    assert all(r["isDirectSell"] for r in ranked)
    assert rm.format_listing_value("Damage Vs Corpus", 0.95) == "x0.95"
    assert rm.format_listing_value("Zoom", -40.4) == "−40%"
    assert rm.auction_url("64f/0a?a01") == "https://warframe.market/auction/64f0aa01"
    calls = []
    s = rm.SimilarSearch(fetch=lambda u: calls.append(u) or WFM_PAYLOAD)
    assert len(s.search("sobek")) == 3 and len(calls) == 2 and "price_desc" in calls[1]
    assert len(s.search("sobek")) == 3 and len(calls) == 2  # cached
    bad = rm.SimilarSearch(fetch=lambda u: (_ for _ in ()).throw(RuntimeError("cloudflare challenge")))
    assert bad.search("sobek") == [] and "cloudflare" in bad.last_error


# ---- inventory decode and the Rivens menu ---------------------------------------------------------

MAX_ROLL = 0x3FFFFFFF


def _inventory(lvl=None):
    fp = {"compat": "/Lotus/Weapons/Tenno/Rifle/Rifle", "lim": 0, "lvlReq": 9, "rerolls": 2, "pol": "AP_ATTACK",
          "buffs": [{"Tag": "WeaponFireDamageMod", "Value": round(MAX_ROLL * 0.72)}],
          "curses": [{"Tag": "WeaponFireRateMod", "Value": round(MAX_ROLL * 0.3)}]}
    if lvl is not None:
        fp["lvl"] = lvl
    ch = "/Lotus/Types/Challenges/Seasons/"
    return {"Upgrades": [
        {"ItemType": "/Lotus/Upgrades/Mods/Randomized/LotusRifleRandomModRare", "ItemId": {"$oid": "a1"},
         "UpgradeFingerprint": json.dumps(fp)},
        {"ItemType": "/Lotus/Upgrades/Mods/Randomized/LotusPistolRandomModRare", "ItemId": {"$oid": "b1"},
         "UpgradeFingerprint": json.dumps({"challenge": {"Type": ch + "RandomizedHeadshot", "Progress": 3,
                                                         "Required": 40}})},
        {"ItemType": "/Lotus/Upgrades/Mods/Randomized/LotusPistolRandomModRare", "ItemId": {"$oid": "b2"},
         "UpgradeFingerprint": json.dumps({"challenge": {
             "Type": ch + "DJRandomizedHeadshot", "Required": 15,
             "Complication": "/Lotus/Types/Challenges/Complications/ResetOnDamageTaken"}})}],
        "RawUpgrades": [{"ItemType": "/Lotus/Upgrades/Mods/Randomized/RawShotgunRandomMod", "ItemCount": 2}]}


@needs_data
def test_fingerprint_decode():
    omitted = rg.decode_all_rivens(_inventory())["unveiled"][0]
    maxed = rg.decode_all_rivens(_inventory(8))["unveiled"][0]
    assert omitted["currentRank"] == 0 and maxed["currentRank"] == 8 and omitted["masteryReq"] == 9
    assert [s["maxRankValue"] for s in omitted["stats"]] == [s["displayValue"] for s in maxed["stats"]]
    assert omitted["overallGrade"] == maxed["overallGrade"]
    for rank in range(9):
        at = rg.decode_all_rivens(_inventory(rank))["unveiled"][0]
        assert [s["rankValues"][rank] for s in maxed["stats"]] == [s["displayValue"] for s in at["stats"]]
    curse = next(s for s in omitted["stats"] if not s["positive"])
    assert curse["displayValue"] < 0 and curse["maxRankValue"] < 0
    assert omitted["weaponName"] == "Braton" and omitted["rivenName"].startswith("Braton ")
    d = rg.decode_all_rivens(_inventory())
    assert [v["challengeDesc"] for v in d["veiled"]] == ["Kill 40 Enemies with Headshots",
                                                        "Kill 15 Enemies with Headshots without taking damage"]
    assert {v["challengeGroup"] for v in d["veiled"]} == {"Kill Enemies with Headshots"}
    assert d["veiledUnseen"] and d["veiledUnseen"][0]["count"] == 2


@needs_data
def test_rivens_menu(app, monkeypatch):
    from wfh import inventory as inv_mod
    from wfh.menus import rivens as menu

    class Inv:
        data = _inventory(8)
        origin = "file"
        sync_time = None
        fetched_at = time.time()

    monkeypatch.setattr(inv_mod, "inventory", lambda: Inv())
    out = menu.build_owned("grade")
    assert out["total"] == 1 and out["rivens"][0]["itemId"] == "a1" and len(out["veiled"]) == 2
    assert menu.build_owned("weapon", "zzz")["rivens"] == []
    sim = menu.build_similar("a1", True, rm.SimilarSearch(fetch=lambda u: WFM_PAYLOAD))
    assert sim["slug"] == "braton" and sim["rows"][0]["url"].startswith("https://warframe.market/auction/")
    with pytest.raises(ValueError):
        menu.build_similar("nope")


# ---- overlay rendering and preview -------------------------------------------------------------

def test_panel_render_sizes():
    left = {"side": "left", "label": "Current", "weapon": "Kuva Sobek", "stats": [], "status": "Scanning..."}
    img = rov.render_panel(left)
    assert img.width == rov.PANEL_W and img.height < 120
    err = rov.render_panel({**left, "status": None, "error": rov.STRINGS["textTooSmall"]})
    assert err.height > img.height
    many = {"side": "right", "label": "New roll", "weapon": "X", "roll_count": 12,
            "stats": [dict(s, grade="A", rollFloat=0.8) for s in CARD_NEW],
            "listings": rm.rank_similar(CARD_NEW, rm.parse_auctions(WFM_PAYLOAD) * 4),
            "best": {"positives": ["Multishot", "Critical Chance", "Critical Damage", "Damage", "Fire Rate"],
                     "negatives": ["Zoom", "Impact", "Status Duration"]}}
    assert rov.render_panel(many, max_h=rov.RIGHT_MAX_H).height <= rov.RIGHT_MAX_H
    # layout stays clear of the card column and the FITS IN plate on the Deck
    assert rov.LEFT_X + rov.PANEL_W < 281 and rov.RIGHT_X > 997
    assert rov.PANEL_TOP + rov.RIGHT_MAX_H <= 558
    big, x, y = rov.scale_to_overlay(img, rov.RIGHT_X, rov.PANEL_TOP, (1920, 1200))
    assert (x, y) == (1512, 72) and big.width == 396
    assert rov.format_value(S("Weapon Recoil", 44.3, True, displayPositive=False)) == "−44.3%"
    assert rov.format_value(S("Damage to Corpus", 0.84, False, multiplier=True)) == "x0.84"


@needs_data
def test_riven_preview_png(app):
    """Realistic old-vs-new comparison on a Deck Cycle screen -> docs/previews/riven.png."""
    from wfh.features import riven_scanner as R
    from wfh.overlay.manager import OverlayManager

    app.settings.update({"overlays_enabled": True, "riven_scanner": {"enabled": True, "auctions_shown": True}})
    om = OverlayManager(headless=True)
    real = app.overlays
    app.overlays = om
    try:
        cards = {"initial": [(CARD_OLD, "Sobek Croni-zetican\n")], "roll": [(CARD_NEW, "Sobek Critatox\n")],
                 "choice": [], "chat": []}
        c, timer, _ = _controller(app, cards)
        for line in (OPEN, WEAPON, DIORAMA):
            c.sm.process(line)
        timer.run()
        c.clock = lambda: time.monotonic() + 10
        c.sm.process(CYCLE_SOBEK)
        c.sm.process(OK)
        timer.run()
        left, right = c.panel_views()
        assert right["arrows"] and right["best"]["positives"][0] == "Multishot"
        assert set(om.active()) == {R.ov.LAYER_LEFT, R.ov.LAYER_RIGHT}
        # the manager composes on its own thread: wait until two snapshots agree
        deadline = time.time() + 5
        comp, prev = None, None
        while time.time() < deadline:
            time.sleep(0.15)
            comp = om.snapshot_png()
            if comp == prev and Image.open(io.BytesIO(base64.b64decode(comp.split(",", 1)[1]))).getbbox():
                break
            prev = comp
        comp = Image.open(io.BytesIO(base64.b64decode(comp.split(",", 1)[1])))
        assert comp is not None and comp.getbbox() is not None
        bg = Image.open(DEMO).convert("RGBA") if os.path.exists(DEMO) else Image.new("RGBA", (1280, 800), (20, 24, 36, 255))
        bg.alpha_composite(comp)
        os.makedirs(os.path.dirname(PREVIEW), exist_ok=True)
        bg.convert("RGB").save(PREVIEW, optimize=True)
        assert os.path.getsize(PREVIEW) > 10_000
        c.close()
    finally:
        app.overlays = real
        om.stop()


def test_controller_on_line_uses_tailer_source(app):
    c, timer, _ = _controller(app, {"initial": [([], "")], "roll": [], "choice": [], "chat": []})
    app.eelog.source = "file"
    c.on_line("unrelated line")
    c.on_line(OPEN)
    assert c.sm.active and c.state()["active"]
    c.close(force_end=True)
    assert not c.sm.active


@needs_data
def test_controller_end_to_end_via_on_line(app):
    """The real entry point: EE.log lines through on_line (pre-filter included), serial dispatch."""
    app.settings.update({"overlays_enabled": True, "riven_scanner": {"enabled": True}})
    cards = {"initial": [(CARD_OLD, ""), (CARD_OLD, "")], "roll": [(CARD_NEW, "")], "choice": [], "chat": []}
    c, timer, _ = _controller(app, cards)
    queue = []
    c._dispatch = lambda fn, *a: queue.append((fn, a))  # serial worker, drained by hand

    def drain():
        while queue:
            fn, a = queue.pop(0)
            fn(*a)
        timer.run()

    app.eelog.source = "file"
    for line in (OPEN, WEAPON, DIORAMA):
        c.on_line(line)
    assert c.sm.diorama_ready  # the diorama line passes the pre-filter
    drain()
    st = c.state()
    assert st["active"] and st["weapon"] == "Kuva Sobek" and len(st["left"]["stats"]) == 4
    assert {"riven", "riven_right"} <= set(app.overlays.active())
    c.on_line(CYCLE_SOBEK)
    c.on_line(OK)
    drain()
    st = c.state()
    assert st["rollCount"] == 1 and len(st["right"]["stats"]) == 4 and st["lastResult"] is None

    # Close and an immediate reopen in the same EE.log flush, before the worker runs.
    c.on_line(CLOSE)
    c.on_line(OPEN)
    assert c.sm.active
    fn, a = queue.pop(0)
    fn(*a)  # the queued close
    assert "riven" not in app.overlays.active() and "riven_right" not in app.overlays.active()
    st = c.state()
    assert not st["active"] and st["lastResult"]["weapon"] == "Kuva Sobek"
    assert st["lastResult"]["rollCount"] == 1 and len(st["lastResult"]["right"]["stats"]) == 4
    assert c.sm.active and c.sm.force_ended_at is None  # the new session survived the queued close
    drain()
    st = c.state()
    assert st["active"] and "riven" in app.overlays.active()
    # ...and its cycle dialogs still count (no force-end cooldown)
    c.on_line(DIORAMA)
    c.on_line(CYCLE_SOBEK)
    assert c.sm.pending_dialog == "roll_confirm"
    c.on_line(CLOSE)
    drain()
    assert not c.state()["active"] and "riven" not in app.overlays.active()
    assert c.state()["lastResult"]["weapon"] == "Sobek"  # no diorama weapon line this time: the dialog's


@needs_data
def test_controller_unknown_dialog_keeps_diorama_weapon(app):
    app.settings.update({"overlays_enabled": True, "riven_scanner": {"enabled": True}})
    c, timer, _ = _controller(app, {"initial": [(CARD_OLD, "")], "roll": [], "choice": [], "chat": []})
    for line in (OPEN, WEAPON, DIORAMA):
        c.on_line(line)
    timer.run()
    c.on_roll_pending("Croni-zetican", 4000)  # a riven name with no weapon prefix
    st = c.state()
    assert st["weapon"] == "Kuva Sobek" and st["weaponSource"] == "diorama"
    c.close()


def test_controller_rescan_disabled(app):
    app.settings.update({"riven_scanner": {"enabled": False}})
    try:
        c, timer, _ = _controller(app, {"initial": [], "roll": [], "choice": [], "chat": []})
        assert c.rescan() == {"started": None, "reason": "disabled"}
        assert not c.sm.active
    finally:
        app.settings.update({"riven_scanner": {"enabled": True}})


@needs_data
def test_rivens_menu_loads_sheet_in_background(app, monkeypatch):
    from wfh.menus import rivens as menu

    gr = rg.goodrolls()
    events, submitted = [], []
    monkeypatch.setattr(gr, "has_sheet", lambda: False)
    monkeypatch.setattr(gr, "load_cache_if_needed", lambda: None)

    def ensure_loaded(force=False, wait=None):
        gr.updated_at = (gr.updated_at or 0) + 1

    monkeypatch.setattr(gr, "ensure_loaded", ensure_loaded)
    monkeypatch.setattr(gr, "updated_at", gr.updated_at)
    monkeypatch.setattr(app, "submit", lambda fn, *a: submitted.append((fn, a)))
    monkeypatch.setattr(app, "emit", lambda name, data=None: events.append(name))
    assert menu.refresh_sheet_in_background(app) is True
    assert menu.refresh_sheet_in_background(app) is False  # one load at a time
    fn, a = submitted.pop()
    fn(*a)
    assert events == ["rivens.updated"]
