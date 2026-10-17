"""OCR foundation: pure helpers on synthetic data, then the real models on WFHelper fixtures."""

import difflib
import glob
import json
import math
import os
import re

import numpy as np
import pytest
from PIL import Image

from wfh import ocr
from wfh.ocr import image, paddle, yolo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS = os.path.join(ROOT, "models")
FIX = os.path.join(ROOT, "tests", "fixtures")
EXPECTED = json.load(open(os.path.join(FIX, "ocr", "expected.json"), encoding="utf-8"))

needs_models = pytest.mark.skipif(
    not (os.path.isfile(os.path.join(MODELS, paddle.MODEL_FILE)) and os.path.isfile(os.path.join(MODELS, yolo.MODEL_FILE))),
    reason="models not fetched (scripts/fetch-models.sh)",
)


# ---------------------------------------------------------------- image ops

def test_js_round_is_half_up():
    assert [image.js_round(v) for v in (0.5, 1.5, 2.5, -0.5, 2.4999)] == [1, 2, 3, 0, 2]


def test_strip_target_width():
    assert image.strip_target_width(156) == 468  # Deck title strip
    assert image.strip_target_width(234) == 702  # 1080p
    assert image.strip_target_width(326) == 720  # capped
    assert image.strip_target_width(2) == 8
    assert image.strip_target_width(0) == 0


def _otsu_reference(gray: np.ndarray) -> int:
    """Literal transcription of rewardScannerImage.ts:1142-1167."""
    hist = [0] * 256
    for v in gray.ravel():
        hist[int(v)] += 1
    total = gray.size
    s = sum(t * hist[t] for t in range(256))
    sum_b = w_b = 0
    best, thr = 0, 127
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b, m_f = sum_b / w_b, (s - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best:
            best, thr = between, t
    return thr


def test_otsu_matches_reference():
    rng = np.random.default_rng(7)
    bimodal = np.concatenate([rng.normal(40, 10, 3000), rng.normal(200, 15, 800)]).clip(0, 255).astype(np.uint8)
    assert 60 < image.otsu_threshold(bimodal) < 180
    for _ in range(5):
        g = rng.integers(0, 256, size=(20, 37), dtype=np.uint8)
        assert image.otsu_threshold(g) == _otsu_reference(g)
    assert image.otsu_threshold(np.full((4, 4), 9, np.uint8)) == 127  # flat image keeps the start value


def test_normalise_stretches_percentiles():
    g = np.tile(np.arange(50, 151, dtype=np.uint8), (10, 1))
    out = image.normalise(g)
    assert out.min() == 0 and out.max() == 255
    assert np.all(np.diff(out[0].astype(int)) >= 0)
    flat = np.full((5, 5), 77, np.uint8)
    assert np.array_equal(image.normalise(flat), flat)


def test_bright_pixels_are_ink():
    mono = image.bright_ink_mono(np.array([[10, 200], [128, 127]], np.uint8), 128)
    assert mono.tolist() == [[255, 0], [0, 255]]


def _mono_with_rows(height: int, width: int, rows: list[tuple[int, int]], ink_cols=(10, 90)) -> np.ndarray:
    m = np.full((height, width), 255, np.uint8)
    for y1, y2 in rows:
        m[y1:y2 + 1, ink_cols[0]:ink_cols[1]] = 0
    return m


def test_split_rows_basic_and_gap_tolerance():
    # Two lines; a 2-row gap inside the first one does not split it, a 3-row gap does.
    m = _mono_with_rows(80, 100, [(5, 12), (15, 20), (35, 50)])
    assert image.split_strip_rows(m) == [(5, 20), (35, 50)]
    m = _mono_with_rows(80, 100, [(5, 14), (18, 30)])
    assert image.split_strip_rows(m) == [(5, 14), (18, 30)]


def test_split_rows_drops_short_and_thin_ink():
    m = _mono_with_rows(60, 200, [(5, 10), (20, 40)])  # first segment is 6 rows < 10
    m[50, 0:2] = 0  # 2 px of ink < max(3, 3) -> not a row
    assert image.split_strip_rows(m) == [(20, 40)]


def test_split_rows_runs_to_bottom_edge():
    m = _mono_with_rows(30, 100, [(15, 29)])
    assert image.split_strip_rows(m) == [(15, 29)]


def test_split_rows_splits_tall_segment_at_valley():
    m = _mono_with_rows(120, 100, [(5, 104)])
    m[50:53, 10:85] = 255  # thin the ink in the middle half: 5 px left per row
    m[50:53, 10:15] = 0
    segs = image.split_strip_rows(m)
    assert segs == [(5, 49), (51, 104)]
    # A segment with no valley under 80 % of the peak stays whole.
    m = _mono_with_rows(120, 100, [(5, 104)])
    assert image.split_strip_rows(m) == [(5, 104)]


def test_segment_crop_pads_and_trims_to_ink():
    mono = _mono_with_rows(40, 120, [(10, 20)], ink_cols=(30, 60))
    rgb = np.zeros((40, 120, 3), np.uint8)
    rgb[..., 0] = np.arange(120)[None, :]
    crop, (x1, y1, x2, y2) = image.segment_crop(mono, rgb, image.RowSegment(10, 20))
    assert (x1, y1, x2, y2) == (22, 6, 67, 24)
    assert crop.shape == (19, 46, 3) and crop[0, 0, 0] == 22
    assert image.segment_crop(np.full((10, 10), 255, np.uint8), rgb[:10, :10], image.RowSegment(2, 5)) is None


def test_to_rgb_drops_alpha_and_expands_gray():
    rgba = np.zeros((2, 3, 4), np.uint8)
    rgba[..., 3] = 7
    assert image.to_rgb(rgba).shape == (2, 3, 3)
    assert image.to_rgb(np.zeros((2, 3), np.uint8)).shape == (2, 3, 3)
    assert image.to_rgb(Image.new("RGBA", (3, 2))).shape == (2, 3, 3)
    one = image.to_rgb(np.full((2, 3, 1), 9, np.uint8))
    assert one.shape == (2, 3, 3) and (one == 9).all()
    la = np.zeros((2, 3, 2), np.uint8)
    la[..., 0], la[..., 1] = 5, 200
    two = image.to_rgb(la)
    assert two.shape == (2, 3, 3) and (two == 5).all()


def test_to_rgb_rejects_float_and_odd_shapes():
    with pytest.raises(TypeError):
        image.to_rgb(np.ones((2, 3, 3), np.float32))
    with pytest.raises(TypeError):
        image.to_rgb(np.zeros((2, 3, 5), np.uint8))


def test_clean_row_text_restores_spaces():
    assert ocr.clean_row_text("XakuPrimeChassis Blueprint") == "Xaku Prime Chassis Blueprint"
    assert ocr.clean_row_text("2XForma") == "2 XForma"
    assert ocr.clean_row_text("  Fang·Prime  Handle ") == "Fang Prime Handle"
    assert ocr.clean_row_text("Akbronco & Lato's-Prime") == "Akbronco & Lato's-Prime"
    assert ocr.clean_row_text("") == ""


# ---------------------------------------------------------------- recognizer helpers

def test_dict_loading(tmp_path):
    p = tmp_path / "d.txt"
    p.write_bytes(b"a\r\nb\n \n")
    assert paddle.load_dict(str(p)) == ["blank", "a", "b", " "]


@needs_models
def test_real_dict_has_6625_classes_with_space_last():
    d = paddle.load_dict(os.path.join(MODELS, paddle.DICT_FILE))
    assert len(d) == 6625 and d[0] == "blank" and d[-1] == " "
    assert not any("\r" in c for c in d)


def _probs(seq: list[int], classes: int = 5, p: float = 0.9) -> np.ndarray:
    m = np.full((len(seq), classes), (1 - p) / (classes - 1), np.float32)
    for t, c in enumerate(seq):
        m[t, c] = p
    return m


def test_ctc_greedy_decode():
    charset = ["blank", "a", "b", " "]
    # Repeats collapse; a blank between equal classes keeps both; index 4 is outside the dict.
    r = paddle.ctc_greedy_decode(_probs([1, 1, 0, 1, 2, 2, 4, 3]), charset)
    assert r == ("aab ", pytest.approx(0.9))
    assert r.text == "aab " and len(r.char_confidences) == 4
    text, conf = paddle.ctc_greedy_decode(_probs([0, 0, 0]), charset)
    assert (text, conf) == ("", 0.0)
    # Ties go to the lowest index (blank here).
    tie = np.array([[0.5, 0.5, 0, 0, 0]], np.float32)
    assert paddle.ctc_greedy_decode(tie, charset)[0] == ""
    # Confidence is the mean over emitted characters.
    m = _probs([1, 2])
    m[1, 2] = 0.5
    assert paddle.ctc_greedy_decode(m, charset)[1] == pytest.approx(0.7)


def test_trimmed_line_confidence():
    r = paddle.RecResult(" +5 Heat ", 0.0, [0.5, 1, 1, 1, 1, 1, 1, 1, 0.5])
    assert paddle.trimmed_line_confidence(r) == pytest.approx(1.0)
    icon = paddle.RecResult("+86,5%制Heat", 0.0, [1, 1, 1, 1, 1, 1, 0.1, 1, 1, 1, 1])
    assert paddle.trimmed_line_confidence(icon) == pytest.approx(1.0)
    mismatch = paddle.RecResult("abc", 0.42, [1.0])
    assert paddle.trimmed_line_confidence(mismatch) == 0.42
    assert paddle.trimmed_line_confidence(("abc", 0.3)) == 0.3
    # JS \\d is ASCII only: a fullwidth digit is not a "value", so the glyph stays in.
    wide = paddle.RecResult("+\uff112%制Heat", 0.0, [1, 1, 1, 1, 0.1, 1, 1, 1, 1])
    assert paddle.trimmed_line_confidence(wide) == pytest.approx(8.1 / 9)


def test_recognition_chunks_group_by_width():
    def crop(w, h=48):
        return np.zeros((h, w, 3), np.uint8)

    crops = [crop(300), crop(100), crop(150), crop(190), crop(210)]
    # Sorted 100,150,190,210,300: 210 > 2*100 starts a new chunk.
    assert paddle.recognition_chunks(crops, 6625) == [[1, 2, 3], [4, 0]]
    many = [crop(100) for _ in range(8)]
    assert [len(c) for c in paddle.recognition_chunks(many, 6625)] == [6, 2]
    # A single crop over the 48 MiB output budget is skipped.
    assert paddle.recognition_chunks([crop(20000, 10)], 6625) == []


def test_build_batch_layout():
    a = np.full((24, 48, 3), 255, np.uint8)   # 2:1 -> 96 wide at height 48
    b = np.zeros((48, 192, 3), np.uint8)      # 4:1 -> 192 wide
    b[..., 0] = 255
    batch = paddle.build_batch([a, b])
    assert batch.shape == (2, 3, 48, 192) and batch.dtype == np.float32
    assert np.allclose(batch[0, :, :, :96], 1.0)
    assert np.all(batch[0, :, :, 96:] == 0.0)  # right padding stays 0.0
    assert np.allclose(batch[1, 0], 1.0) and np.allclose(batch[1, 1:], -1.0)  # RGB order


def test_session_options_limit_threads():
    o = paddle.session_options()
    assert o.intra_op_num_threads == 2 and o.inter_op_num_threads == 1


# ---------------------------------------------------------------- detector helpers

def test_letterbox_geometry():
    lb = yolo.letterbox(np.full((162, 224, 3), 255, np.uint8))
    assert lb.scale == pytest.approx(640 / 224)
    assert (lb.pad_left, lb.pad_top) == (0, (640 - image.js_round(162 * 640 / 224)) // 2)
    assert lb.blob.shape == (1, 3, 640, 640)
    assert lb.blob[0, 0, 0, 0] == pytest.approx(114 / 255)
    assert lb.blob[0, 0, 320, 320] == pytest.approx(1.0)


def test_decode_and_nms():
    lb = yolo.Letterbox(np.zeros(1), scale=2.0, pad_left=0, pad_top=100)
    cols = [
        # cx, cy, w, h, conf (letterbox pixels)
        (200, 200, 300, 40, 0.9),   # -> (25, 40, 175, 60)
        (205, 202, 300, 40, 0.8),   # overlaps the first -> suppressed
        (200, 300, 300, 40, 0.7),   # second line, lower on screen
        (200, 400, 300, 40, 0.2),   # under the confidence threshold
        (200, 500, 10, 40, 0.9),    # 5 px wide -> dropped
        (-50, 140, 200, 40, 0.6),   # clamped at x=0 -> (0, 10, 25, 30)
    ]
    preds = np.array(cols, np.float32).T[None]
    boxes = yolo.decode(preds, lb, 200, 200)
    assert len(boxes) == 4
    kept = yolo.nms(boxes)
    assert [(b.x1, b.y1, b.x2, b.y2) for b in kept] == [(0, 10, 25, 30), (25, 40, 175, 60), (25, 90, 175, 110)]
    assert kept[1].confidence == pytest.approx(0.9)


def test_extract_line_crops_skip_and_upscale():
    rgb = np.zeros((300, 400, 3), np.uint8)
    boxes = [
        yolo.Box(50, 20, 250, 40, 0.9),    # 216 x 36 with padding
        yolo.Box(0, 100, 3, 110, 0.9),     # too narrow after padding (11 px)
        yolo.Box(10, 150, 300, 240, 0.9),  # too tall (106 px)
    ]
    crops = yolo.extract_line_crops(rgb, boxes)
    f = math.ceil(1200 / 216)
    assert len(crops) == 1 and crops[0].shape == (36 * f, 216 * f, 3)
    assert yolo.extract_line_crops(rgb, []) == []


def test_missing_models_raise(tmp_path):
    with pytest.raises(FileNotFoundError):
        ocr.PaddleRecognizer(str(tmp_path)).recognize([np.zeros((10, 20, 3), np.uint8)])
    with pytest.raises(FileNotFoundError):
        ocr.StatLineDetector(str(tmp_path)).detect(np.zeros((10, 20, 3), np.uint8))
    assert ocr.PaddleRecognizer(str(tmp_path)).warmup() is False


def test_read_strip_never_raises(tmp_path, monkeypatch):
    strip = np.zeros((40, 200, 3), np.uint8)
    strip[12:28, 20:180] = 255
    assert ocr.available(str(tmp_path)) is False
    assert ocr.read_strip(strip, str(tmp_path)) is None  # model missing -> None, not FileNotFoundError

    class Boom:
        def available(self):
            return True

        def recognize(self, crops):
            raise RuntimeError("onnx exploded")

    assert ocr.read_strip(strip, str(tmp_path), recognizer=Boom()) is None
    assert ocr.read_strip(np.ones((40, 200, 3), np.float32), str(tmp_path), recognizer=Boom()) is None


def test_warmup_runs_once_per_dir(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(ocr.threading.Thread, "start", lambda self: started.append(self.name))
    monkeypatch.setattr(ocr.PaddleRecognizer, "available", lambda self: True)
    d = str(tmp_path)
    ocr.warmup(d)
    ocr.warmup(d + "/")
    ocr.warmup(d, detector=True)
    ocr.warmup(d, detector=True)
    assert len(started) == 2
    monkeypatch.setattr(ocr.PaddleRecognizer, "available", lambda self: False)
    ocr.warmup(str(tmp_path / "missing"))  # no model: no thread
    assert len(started) == 2


# ---------------------------------------------------------------- real models on fixtures

def _similar(a: str, b: str) -> float:
    fold = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    return difflib.SequenceMatcher(None, fold(a), fold(b)).ratio()


_CARD_X = {1: [0.439], 2: [0.3755, 0.5025], 3: [0.312, 0.439, 0.566], 4: [0.245, 0.372, 0.499, 0.626]}


def title_strips(img: Image.Image, cards: int) -> list[Image.Image]:
    """Title strips at UI scale 0.99 with the 16:9 canvas correction (relic-rewards.md §4)."""
    w, h = img.size
    sy = (w / (16 / 9)) / h if w / (16 / 9) < h else 1.0
    sx = (h * 16 / 9) / w if h * 16 / 9 < w else 1.0
    out = []
    for cx in _CARD_X[cards]:
        x, y, cw, ch = cx, 0.225 + 0.225 * 0.7, 0.122, 0.225 * 0.18
        x, y, cw, ch = 0.5 + (x - 0.5) * sx, 0.5 + (y - 0.5) * sy, cw * sx, ch * sy
        px, py = math.floor(w * x), math.floor(h * y)
        out.append(img.crop((px, py, px + max(24, math.floor(w * cw)), py + max(24, math.floor(h * ch)))))
    return out


def build_slot_frame(cards: int) -> Image.Image:
    """real-{n}p.png as build-screens.cjs makes it from the slot-title crops."""
    frame = Image.new("RGBA", (1920, 1080), (24, 16, 16, 255))
    scaffold = Image.new("RGBA", (234, 413 - 243 - 3), (146, 138, 138, 255))
    for x in EXPECTED["slot_frames"][str(cards)]["cards_1080"]:
        frame.alpha_composite(scaffold, (x, 243))
    for f in sorted(glob.glob(os.path.join(FIX, "reward-scan", "slot-crops", f"{cards}_players_*.png"))):
        x, y = map(int, re.search(r"_title_(\d+)x(\d+)_", f).group(1, 2))
        frame.alpha_composite(Image.open(f).convert("RGBA"), (x, y))
    return frame.convert("RGB")


def to_deck(frame: Image.Image) -> Image.Image:
    """Pad a 1080p frame to 16:10 and downscale to the Deck's 1280x800."""
    padded = Image.new("RGB", (1920, 1200), (24, 16, 16))
    padded.paste(frame, (0, 60))
    return padded.resize((1280, 800), Image.LANCZOS)


def read_all(strips):
    reads = [ocr.read_strip(s, MODELS) for s in strips]
    return [r.text if r else "" for r in reads]


@needs_models
def test_singleton_and_warmup():
    assert ocr.PaddleRecognizer.get(MODELS) is ocr.PaddleRecognizer.get(MODELS + "/")
    assert ocr.StatLineDetector.get(MODELS) is ocr.StatLineDetector.get(MODELS)
    assert ocr.PaddleRecognizer.get(MODELS).warmup() is True
    assert ocr.StatLineDetector.get(MODELS).warmup() is True


@needs_models
def test_recognize_contract_and_empty_crops():
    rec = ocr.PaddleRecognizer.get(MODELS)
    out = rec.recognize([np.full((48, 160, 3), 255, np.uint8), np.zeros((0, 5, 3), np.uint8)])
    assert len(out) == 2 and out[1] == ("", 0.0)
    text, conf = out[0]
    assert isinstance(text, str) and 0.0 <= conf <= 1.0
    assert rec.recognize([]) == []


@needs_models
@pytest.mark.parametrize("cards", [2, 4])
def test_slot_crop_frames_read_exactly(cards):
    expected = EXPECTED["slot_frames"][str(cards)]["expected"]
    assert read_all(title_strips(build_slot_frame(cards), cards)) == expected


@needs_models
def test_slot_crop_frame_3p():
    # Slot 0 reads "2 X Forma Blueprint" (the item count); run-check accepts it for "Forma Blueprint".
    got = read_all(title_strips(build_slot_frame(3), 3))
    for text, exp in zip(got, EXPECTED["slot_frames"]["3"]["expected"]):
        assert exp in text, (text, exp)


@needs_models
@pytest.mark.parametrize("cards", [2, 3, 4])
def test_slot_crop_frames_at_deck_resolution(cards):
    """156x29 px strips (relic-rewards.md §4.5): reads stay close enough for the fuzzy matcher."""
    got = read_all(title_strips(to_deck(build_slot_frame(cards)), cards))
    for text, exp in zip(got, EXPECTED["slot_frames"][str(cards)]["expected"]):
        assert exp.split()[0] in text and _similar(text, exp) >= 0.75, (text, exp)


@needs_models
@pytest.mark.parametrize("name", sorted(EXPECTED["public_frames"]))
def test_public_reward_frames(name):
    spec = EXPECTED["public_frames"][name]
    img = Image.open(os.path.join(FIX, "reward-scan", "public", name)).convert("RGB")
    got = read_all(title_strips(img, spec["cards"]))
    if spec["exact"]:
        assert got == spec["expected"]
    else:
        # 1080x607 ("Prme", "Bluerint") and 16x10 ("XFoima") need the fuzzy matcher (§6.4).
        for text, exp in zip(got, spec["expected"]):
            assert _similar(text, exp) >= 0.8, (text, exp)


@needs_models
def test_16x10_frame_downscaled_to_deck():
    img = Image.open(os.path.join(FIX, "reward-scan", "public", "real-full-4p-16x10.png")).convert("RGB")
    got = read_all(title_strips(img.resize((1280, 800), Image.LANCZOS), 4))
    assert all(_similar(t, "2 X Forma Blueprint") >= 0.8 for t in got), got


def riven_lines(name: str, up: int) -> list[tuple[str, float]]:
    rgb = image.upscale(image.to_rgb(Image.open(os.path.join(FIX, "riven", name))), up, "lanczos")
    boxes = ocr.StatLineDetector.get(MODELS).detect(rgb)
    results = ocr.PaddleRecognizer.get(MODELS).recognize(ocr.extract_line_crops(rgb, boxes))
    return [(r[0].strip(), ocr.trimmed_line_confidence(r)) for r in results if r[0].strip()]


# Lines the Python port reads with a trailing glyph that the TS port's parse
# (parseRivenStats) drops. The curse line on the 2x chat card decodes as
# "-90,9% Projectile Speed g" here; the riven owner's parser must tolerate it.
# Kept as a prefix check so any other change to this line still fails.
KNOWN_SUFFIX_DIFF = {("chat-card-angstrum-stats.png", "-90,9% Projectile Speed")}


def _is_stat(t: str) -> bool:
    return bool(re.match(r"^[+-]|^[x×]\s*\d", t, re.I))


@needs_models
@pytest.mark.parametrize("name", sorted(EXPECTED["riven"]))
def test_riven_stat_lines(name):
    spec = EXPECTED["riven"][name]
    lines = riven_lines(name, spec["upscale"])
    texts = [t.rstrip() for t, _ in lines]
    matched = []
    for exp in spec["lines"]:
        if (name, exp) in KNOWN_SUFFIX_DIFF:
            hit = [t for t in texts if t.startswith(exp) and len(t) <= len(exp) + 2]
        else:
            hit = [t for t in texts if t == exp]
        assert hit, (exp, texts)
        matched.append(hit[0])
    stat = [c for t, c in lines if _is_stat(t) and t.rstrip() in matched]
    assert stat and min(stat) >= 0.8  # hasLowConfidenceLine would be false


@needs_models
def test_chat_card_loses_curse_line_at_1x():
    """rivenCurseLineFixtures: the chat-linked card needs the 2x scale to keep its curse line."""
    texts = [t for t, _ in riven_lines("chat-card-angstrum-stats.png", 1)]
    assert not any(t.startswith("-90,9% Projectile Speed") for t in texts), texts
    assert any(t.startswith("-90,9% Projectile Speed") for t, _ in riven_lines("mod-card-angstrum-stats.png", 1))


@needs_models
def test_arum_heat_icon_line_confidence():
    """rivenCurseLineFixtures "element icon line confidence": every stat line passes at 2x.

    The fire icon decodes as a letter here ("+86,5% wHeat"), so the glyph trim
    does not fire; the line still has to clear 0.8 on its own.
    """
    lines = riven_lines("chat-card-arum-heat-icon-stats.png", 2)
    stat = [(t, c) for t, c in lines if _is_stat(t)]
    assert len(stat) == 3, lines
    assert all(c >= 0.8 for _, c in stat), stat
    assert stat[0][0].startswith("+86,5%") and stat[0][0].endswith("Heat"), stat
    assert stat[1][0].startswith("+86,3% Critical Damage"), stat
    assert stat[2][0].startswith("+140,9% Melee Damage"), stat


# TODO(riven owner): port the parse-level fixtures from rivenCurseLineFixtures.test.ts
# that need parseRivenStats/cropRivenStatImage: mod-card-soma-trait-locked-stats.png,
# choice-card-soma-trait-locked-stats.png (wrapped trait-locked line) and
# roll-card-corufell-trait-locked.png (roll card in a 1920x1080 frame).


@needs_models
def test_detector_boxes_are_sorted_lines():
    rgb = image.upscale(image.to_rgb(Image.open(os.path.join(FIX, "riven", "chat-card-angstrum-stats.png"))), 2)
    boxes = ocr.StatLineDetector.get(MODELS).detect(rgb)
    assert len(boxes) >= 6
    assert [b.y1 for b in boxes] == sorted(b.y1 for b in boxes)
    assert all(0 <= b.x1 < b.x2 <= rgb.shape[1] and 0 <= b.y1 < b.y2 <= rgb.shape[0] for b in boxes)
