"""Riven card capture, crop geometry, readiness gate and YOLO+Paddle recognition.

Ports (docs/port/riven-scanner.md):
- §5.2 content rect / centred 16:9 menu canvas (rewardScannerImage.ts),
- §5.3-5.5 crops, aspect trim, upscale, fallback text-bounds crop (rivenScanImage.ts),
- §5.6 text metrics, readiness gate, frame hash,
- §6 scan profiles and the one-retry attempt (rivenScan.ts),
- §7.1 recognition loop (rivenScanOcr.ts) and §7.2 recognizeStatArea (rivenOcrOnnx.ts),
- §9.3 the FITS IN weapon plate reader (rivenWeaponLabel.ts).

Images are numpy RGB arrays (H, W, 3) uint8 throughout; WFHelper's BGRA order
does not matter for the metrics because they only use max/min/sum of channels.
Every function here is CPU bound and blocks: call from app.submit / run_blocking.

The Deck renders 1280x800 (16:10). WFHelper assumes Warframe draws menus on a
centred 16:9 canvas, which makes the crop base (0, 40, 1280, 720); setting
riven_scanner.canvas = "full" switches to the whole 1280x800 frame instead.
That assumption is the top porting risk and needs a real Deck screenshot.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np
from PIL import Image

from .. import log
from . import riven_data as rd
from . import riven_text as rt

_log = log.get("riven.scan")


def js_round(x: float) -> int:
    """Math.round: half up, never banker's."""
    return math.floor(x + 0.5)


# ---- geometry ----------------------------------------------------------------------------

@dataclass(frozen=True)
class NRect:
    """A crop normalised to the content canvas."""
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class PxRect:
    x: int
    y: int
    width: int
    height: int


CROPS = {
    "singleCard": NRect(0.22, 0.43, 0.56, 0.45),  # initial + choice rescan (centred card)
    "chatCard": NRect(0.22, 0.3, 0.56, 0.45),     # chat-link preview
    "rollCard": NRect(0.36, 0.416, 0.28, 0.434),  # post-roll new card
}
FITS_IN_NAME_CROP = NRect(0.78, 0.72, 0.21, 0.22)
FITS_IN_PANEL_CROP = NRect(0.7, 0.55, 0.3, 0.45)
FITS_IN_WIDE_CROP = NRect(0.58, 0.48, 0.42, 0.47)

BAR_LUMA_THRESHOLD = 12
BAR_SAMPLE_COUNT = 32
BAR_BLACK_RATIO = 0.85


def detect_content_rect(rgb: np.ndarray) -> PxRect:
    """detectGameContentRect: symmetric letterbox / pillarbox bars."""
    h, w = rgb.shape[:2]
    if w < 120 or h < 80:
        return PxRect(0, 0, w, h)
    px = rgb.astype(np.int32)

    def row_black(y: int) -> bool:
        step = max(1, w // BAR_SAMPLE_COUNT)
        xs = np.minimum(w - 1, np.arange(BAR_SAMPLE_COUNT) * step)
        lum = px[y, xs].sum(axis=1) / 3
        return (lum <= BAR_LUMA_THRESHOLD).sum() / BAR_SAMPLE_COUNT >= BAR_BLACK_RATIO

    def col_black(x: int) -> bool:
        step = max(1, h // BAR_SAMPLE_COUNT)
        ys = np.minimum(h - 1, np.arange(BAR_SAMPLE_COUNT) * step)
        lum = px[ys, x].sum(axis=1) / 3
        return (lum <= BAR_LUMA_THRESHOLD).sum() / BAR_SAMPLE_COUNT >= BAR_BLACK_RATIO

    top = 0
    for y in range(h // 4):
        if not row_black(y):
            top = y
            break
        top = y + 1
    bottom = h
    for y in range(h - 1, math.floor(h * 0.75) - 1, -1):
        if not row_black(y):
            bottom = y + 1
            break
        bottom = y
    left = 0
    for x in range(w // 4):
        if not col_black(x):
            left = x
            break
        left = x + 1
    right = w
    for x in range(w - 1, math.floor(w * 0.75) - 1, -1):
        if not col_black(x):
            right = x + 1
            break
        right = x
    pillar = min(left, w - right)
    letter = min(top, h - bottom)
    return PxRect(pillar, letter, max(24, w - 2 * pillar), max(24, h - 2 * letter))


def center_canvas(c: PxRect) -> PxRect:
    """centerGameCanvas: menus sit on a centred 16:9 canvas."""
    cw = min(c.width, js_round(c.height * 16 / 9))
    ch = min(c.height, js_round(c.width * 9 / 16))
    return PxRect(c.x + (c.width - cw) // 2, c.y + (c.height - ch) // 2, cw, ch)


def content_rect(rgb: np.ndarray, source_type: str = "screen", canvas: str = "16:9") -> PxRect:
    """rivenContentRect: "window" skips bar detection; canvas="full" skips the 16:9 clamp."""
    h, w = rgb.shape[:2]
    base = PxRect(0, 0, w, h) if source_type == "window" else detect_content_rect(rgb)
    return base if canvas == "full" else center_canvas(base)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def crop_box(rect: NRect, content: PxRect, size: tuple[int, int] | None = None) -> PxRect:
    """cropRectContent maths; clipped to the image when size is given."""
    x = _clamp(rect.x, 0.0, 0.98)
    y = _clamp(rect.y, 0.0, 0.98)
    w = _clamp(rect.width, 0.02, max(0.02, 1 - x))
    h = _clamp(rect.height, 0.02, max(0.02, 1 - y))
    X = max(0, content.x + math.floor(content.width * x))
    Y = max(0, content.y + math.floor(content.height * y))
    W = max(24, math.floor(content.width * w))
    H = max(24, math.floor(content.height * h))
    if size:
        iw, ih = size
        W = max(1, min(W, iw - X))
        H = max(1, min(H, ih - Y))
    return PxRect(X, Y, W, H)


def crop_rect_content(rgb: np.ndarray, rect: NRect, content: PxRect) -> np.ndarray:
    b = crop_box(rect, content, (rgb.shape[1], rgb.shape[0]))
    return np.ascontiguousarray(rgb[b.y:b.y + b.height, b.x:b.x + b.width])


# ---- text metrics and frame hash (§5.6) ---------------------------------------------------

@dataclass
class Bounds:
    left: int
    top: int
    width: int
    height: int


@dataclass
class TextMetrics:
    score: float = 0.0
    coverage: float = 0.0
    active_rows: int = 0
    active_cols: int = 0
    row_groups: int = 0
    bounds: Bounds | None = None


def _smooth(v: np.ndarray) -> np.ndarray:
    if len(v) <= 2:
        return v.astype(float).copy()
    prev = np.concatenate(([v[0]], v[:-1]))
    nxt = np.concatenate((v[1:], [v[-1]]))
    return (prev + v + nxt) / 3


def _find_bounds(v: np.ndarray, thr: float) -> tuple[int, int] | None:
    idx = np.nonzero(v >= thr)[0]
    if len(idx) == 0:
        return None
    return int(idx[0]), int(idx[-1])


def _count_groups(v: np.ndarray, thr: float, min_run: int) -> int:
    groups = run = 0
    for val in v:
        if val >= thr:
            run += 1
            continue
        if run >= min_run:
            groups += 1
        run = 0
    if run >= min_run:
        groups += 1
    return groups


def text_metrics(rgb: np.ndarray, min_bounds_width_fraction: float = 0.2) -> TextMetrics:
    """analyzeRivenTextMetrics: bright low-saturation (text-like) sample density."""
    if rgb is None or rgb.ndim < 2:
        return TextMetrics()
    h, w = rgb.shape[:2]
    if w < 24 or h < 24:
        return TextMetrics()
    cols = max(48, min(w, 320))
    rows = max(32, min(h, 160))
    step_x = w / cols
    step_y = h / rows
    ys = np.minimum(h - 1, np.floor(np.arange(rows) * step_y).astype(int))
    xs = np.minimum(w - 1, np.floor(np.arange(cols) * step_x).astype(int))
    s = rgb[ys][:, xs, :3].astype(np.int32)
    mx = s.max(axis=2)
    mn = s.min(axis=2)
    with np.errstate(divide="ignore", invalid="ignore"):
        sat = np.where(mx == 0, 0.0, (mx - mn) / np.maximum(mx, 1))
    text = (mx >= 146) & (sat <= 0.42)
    row_scores = text.sum(axis=1).astype(float)
    col_scores = text.sum(axis=0).astype(float)
    active = int(text.sum())
    sr = _smooth(row_scores)
    sc = _smooth(col_scores)
    row_thr = max(2.0, sr.mean() + sr.std() * 0.45)
    col_thr = max(2.0, sc.mean() + sc.std() * 0.45)
    rb = _find_bounds(sr, row_thr)
    cb = _find_bounds(sc, col_thr)
    active_rows = int((sr >= row_thr).sum())
    active_cols = int((sc >= col_thr).sum())
    groups = _count_groups(sr, row_thr, max(2, math.floor(rows * 0.03)))
    coverage = active / max(1, cols * rows)
    bounds = None
    if rb and cb:
        pad_x = max(2, math.floor(cols * 0.04))
        pad_y = max(2, math.floor(rows * 0.03))
        left = max(0, math.floor((cb[0] - pad_x) * step_x))
        top = max(0, math.floor((rb[0] - pad_y) * step_y))
        right = min(w - 1, math.floor((cb[1] + pad_x + 1) * step_x))
        bottom = min(h - 1, math.floor((rb[1] + pad_y + 1) * step_y))
        bw, bh = right - left + 1, bottom - top + 1
        if bw >= max(24, math.floor(w * min_bounds_width_fraction)) and bh >= max(24, math.floor(h * 0.12)):
            bounds = Bounds(left, top, bw, bh)

    def c01(v: float) -> float:
        return max(0.0, min(1.0, v))

    score = (c01(coverage / 0.08) * 0.28 + c01(active_rows / max(8, rows * 0.16)) * 0.24
             + c01(active_cols / max(24, cols * 0.2)) * 0.24 + c01(groups / 3) * 0.24)
    return TextMetrics(round(score, 3), round(coverage, 4), active_rows, active_cols, groups, bounds)


def frame_hash(rgb: np.ndarray) -> str:
    """computeRivenFrameHash: coarse brightness grid, used to skip identical retries."""
    if rgb is None or rgb.ndim < 2:
        return ""
    h, w = rgb.shape[:2]
    if w < 24 or h < 24:
        return ""
    cols = max(12, min(w, 24))
    rows = max(8, min(h, 16))
    sx = max(1, w // cols)
    sy = max(1, h // rows)
    ys = np.minimum(h - 1, np.arange(rows) * sy)
    xs = np.minimum(w - 1, np.arange(cols) * sx)
    s = rgb[ys][:, xs, :3].astype(np.int32).sum(axis=2)
    buckets = np.floor(s / 32 + 0.5).astype(int)
    return "".join(format(int(b), "x") for b in buckets.ravel())


# ---- stat band crops (§5.4, §5.5) ------------------------------------------------------------

CARD_ASPECT = 287 / 433
MAX_ASPECT_OVERFLOW = 1.1
TRIM_WIDTH_MARGIN = 1.25
MAX_CENTER_SHIFT = 0.2
STAT_TOP, STAT_BOTTOM, STAT_LEFT, STAT_RIGHT = 0.34, 0.84, 0.08, 0.92
MIN_CROP = 30
RELIABLE_STAT_CROP_HEIGHT = 320
FALLBACK_TARGET_HEIGHT = 400
FALLBACK_MAX_UPSCALE = 3
FALLBACK_PAD_FRACTION = 0.06


def crop_stat_area(rough: np.ndarray) -> np.ndarray:
    """cropRivenStatArea: trim to card aspect around the text column, then the stat band."""
    h, w = rough.shape[:2]
    if w < 50 or h < 50:
        return rough
    expected_w = math.floor(h * CARD_ASPECT)
    trimmed, tw = rough, w
    if w > expected_w * MAX_ASPECT_OVERFLOW:
        keep_w = min(w, math.floor(expected_w * TRIM_WIDTH_MARGIN))
        center_x = w / 2
        b = text_metrics(rough).bounds
        if b and b.width <= keep_w:
            bc = b.left + b.width / 2
            if abs(bc - w / 2) <= w * MAX_CENTER_SHIFT:
                center_x = bc
        x1 = min(max(0, js_round(center_x - keep_w / 2)), max(0, w - keep_w))
        trimmed = rough[:, x1:x1 + keep_w]
        tw = keep_w
    sy0, sy1 = math.floor(h * STAT_TOP), math.floor(h * STAT_BOTTOM)
    sx0, sx1 = math.floor(tw * STAT_LEFT), math.floor(tw * STAT_RIGHT)
    if sx1 - sx0 < MIN_CROP or sy1 - sy0 < MIN_CROP:
        return rough
    return np.ascontiguousarray(trimmed[sy0:sy1, sx0:sx1])


def stat_crop_upscale_factor(height: int) -> int:
    if height <= 0:
        return 1
    return min(3, max(1, math.ceil(RELIABLE_STAT_CROP_HEIGHT / height)))


def crop_stat_area_fallback(card: np.ndarray) -> tuple[np.ndarray, int] | None:
    """cropRivenStatAreaFallback: re-trim around the text bounds (small cards)."""
    h, w = card.shape[:2]
    if w < 50 or h < 50:
        return None
    b = text_metrics(card, 0.08).bounds
    if not b:
        return None
    pad_x = js_round(b.width * FALLBACK_PAD_FRACTION)
    pad_y = js_round(b.height * FALLBACK_PAD_FRACTION)
    x = max(0, b.left - pad_x)
    y = max(0, b.top - pad_y)
    cw = min(w - x, b.width + pad_x * 2)
    ch = min(h - y, b.height + pad_y * 2)
    if cw < MIN_CROP or ch < MIN_CROP:
        return None
    up = min(FALLBACK_MAX_UPSCALE, max(1, js_round(FALLBACK_TARGET_HEIGHT / ch)))
    return np.ascontiguousarray(card[y:y + ch, x:x + cw]), up


def crop_stat_image(rgb: np.ndarray, rect: NRect, source_type: str = "screen",
                    canvas: str = "16:9") -> tuple[np.ndarray, np.ndarray]:
    """cropRivenStatImage -> (card crop, stat band)."""
    card = crop_rect_content(rgb, rect, content_rect(rgb, source_type, canvas))
    return card, crop_stat_area(card)


def _resize(rgb: np.ndarray, w: int, h: int, kernel: str) -> np.ndarray:
    k = Image.LANCZOS if kernel == "lanczos" else Image.BILINEAR
    return np.asarray(Image.fromarray(rgb).resize((w, h), k))


# ---- recognizeStatArea (§7.2) -----------------------------------------------------------------

@dataclass
class OcrRead:
    lines: list[tuple[str, float]] = field(default_factory=list)
    text: str = ""
    min_confidence: float = -1.0
    yolo_box_count: int = 0


def ocr_read_from_lines(lines: list[tuple[str, float]], yolo_boxes: int | None = None) -> OcrRead:
    """An OcrRead from already post-processed lines (also the replay test's mock)."""
    lines = [(str(t), float(c)) for t, c in lines]
    return OcrRead(lines, "\n".join(t for t, _ in lines), rt.min_confidence(lines),
                   len(lines) if yolo_boxes is None else yolo_boxes)


def recognize_stat_area(rgb: np.ndarray, models_dir: str) -> OcrRead:
    from ..ocr import PaddleRecognizer, StatLineDetector, extract_line_crops, trimmed_line_confidence

    boxes = StatLineDetector.get(models_dir).detect(rgb)
    if not boxes:
        return OcrRead()
    crops = extract_line_crops(rgb, boxes)
    if not crops:
        return OcrRead(yolo_box_count=len(boxes))
    results = PaddleRecognizer.get(models_dir).recognize(crops)
    lines: list[tuple[str, float]] = []
    for r in results:
        t = (r[0] or "").strip()
        if not t:
            continue
        p = rt.postprocess_ocr_text(t).strip()
        lines.append((p, trimmed_line_confidence(r)))
    merged = rt.merge_split_lines(lines)
    return ocr_read_from_lines(merged, len(boxes))


# ---- recognition loop (§7.1) ----------------------------------------------------------------

MIN_ACCEPTABLE_STATS = 2
MIN_ACCEPTABLE_BUFFS = 2
MAX_RIVEN_STATS = 4
MAX_LOW_CONFIDENCE_RETRIES = 1


def is_incomplete_read(stats: list[dict], dropped_whole: bool = False, title_seen: bool = True) -> bool:
    if not stats:
        return False
    if len(stats) < MIN_ACCEPTABLE_STATS:
        return True
    if sum(1 for s in stats if s["positive"]) < MIN_ACCEPTABLE_BUFFS:
        return True
    if len(stats) > MAX_RIVEN_STATS:
        return True
    if len(stats) == MAX_RIVEN_STATS:
        return False
    return dropped_whole or not title_seen


@dataclass
class CardRead:
    text: str = ""
    stats: list[dict] = field(default_factory=list)
    low_confidence: bool = False
    outcome: str = "empty"
    lines: list[tuple[str, float]] = field(default_factory=list)


def _nulls(stats: list[dict]) -> int:
    return sum(1 for s in stats if s.get("value") is None)


def recognize_card_stats(card: np.ndarray, stat: np.ndarray, ocr: Callable[[np.ndarray], OcrRead], *,
                         roll_reveal: bool = False, is_stale: Callable[[], bool] = lambda: False,
                         label: str = "", dump: Callable[[str, str, np.ndarray, np.ndarray], Any] | None = None,
                         ) -> CardRead:
    """recognizeRivenCardStats: two reads (band, then text-bounds fallback), keep the best."""
    best: OcrRead | None = None
    best_stats: list[dict] = []
    best_text = ""
    dropped_any = False
    best_incomplete = best_blocking = best_dropped_whole = False

    for attempt in range(MAX_LOW_CONFIDENCE_RETRIES + 1):
        if is_stale():
            return CardRead()
        try:
            img = stat
            up = stat_crop_upscale_factor(stat.shape[0])
            if attempt > 0:
                up = 1
                fb = crop_stat_area_fallback(card)
                if fb:
                    img, up = fb
            if up > 1:
                img = _resize(img, img.shape[1] * up, img.shape[0] * up, "lanczos")
            read = ocr(img)
            diag: dict = {"droppedLines": []}
            stats = rt.parse_riven_stats(read.text, diag)
            for line in diag["droppedLines"]:
                _log.warning("%s: unparsed stat-like line %r", label or "riven", line)
            _log.info("%s attempt=%d: %d stats, %d boxes, minConf=%.3f (%dx%d x%d) %s", label or "riven", attempt,
                      len(stats), read.yolo_box_count, read.min_confidence, img.shape[1] // max(1, up),
                      img.shape[0] // max(1, up), up, ", ".join(rt.format_stat(s) for s in stats))
            dropped_whole = any(rt.looks_like_whole_stat_line(l) for l in diag["droppedLines"])
            title_seen = diag.get("titleSeen") is not False
            incomplete = is_incomplete_read(stats, dropped_whole, title_seen)
            blocking = is_incomplete_read(stats, dropped_whole, title_seen or not roll_reveal)
            nulls = _nulls(stats)
            better_tie = (best is not None and len(stats) > 0 and len(stats) == len(best_stats)
                          and (nulls < _nulls(best_stats)
                               or (nulls == _nulls(best_stats) and read.min_confidence > best.min_confidence)))
            completes = (best_incomplete and not incomplete
                         and (len(best_stats) > MAX_RIVEN_STATS
                              or (len(stats) > len(best_stats) if best_dropped_whole
                                  else len(stats) >= len(best_stats))))
            same_comp = incomplete == best_incomplete or len(best_stats) == 0
            if completes or (same_comp and (len(stats) > len(best_stats) or better_tie)):
                best, best_stats, best_text = read, stats, read.text
                dropped_any = len(diag["droppedLines"]) > 0
                best_incomplete, best_blocking = incomplete, blocking
                best_dropped_whole = incomplete and dropped_whole
            if len(stats) >= MIN_ACCEPTABLE_STATS:
                low = rt.has_low_confidence_line(read.lines)
                if not low and not any(s.get("value") is None for s in stats) and not incomplete:
                    break
        except Exception as e:  # noqa: BLE001 - one failed attempt must not kill the scan
            _log.warning("%s attempt=%d failed: %s", label or "riven", attempt, e)

    low_conf = best is not None and len(best_stats) >= MIN_ACCEPTABLE_STATS and rt.has_low_confidence_line(best.lines)
    if not best_stats:
        outcome = "empty"
    elif low_conf or best_blocking:
        outcome = "failed"
    elif dropped_any:
        outcome = "dropped"
    else:
        outcome = "ok"
    if dump:
        try:
            dump(label or "riven", outcome, card, stat)
        except Exception as e:  # noqa: BLE001
            _log.warning("scan crop dump failed: %s", e)
    lines = best.lines if best else []
    if low_conf:
        return CardRead("", [], True, outcome, lines)
    if best_blocking:
        return CardRead(best_text, [], True, outcome, lines)
    return CardRead(best_text, best_stats, False, outcome, lines)


# ---- debug dumps --------------------------------------------------------------------------------

DUMP_KEEP = {"empty": 10, "failed": 10, "dropped": 10, "ok": 4}
FITS_IN_DUMP_KEEP = 6


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3] + "Z"


def _rotate(dir_: str, bucket: str, keep: int) -> None:
    files = sorted(f for f in os.listdir(dir_) if f.endswith(".png") and bucket in f)
    for f in files[:max(0, len(files) - keep)]:
        try:
            os.unlink(os.path.join(dir_, f))
        except OSError:
            pass


def make_dumper(dir_: str) -> Callable[[str, str, np.ndarray, np.ndarray], None]:
    def dump(label: str, outcome: str, card: np.ndarray, stat: np.ndarray) -> None:
        os.makedirs(dir_, exist_ok=True)
        stamp = _stamp()
        Image.fromarray(card).save(os.path.join(dir_, f"{stamp}-{outcome}-{label}-card.png"))
        Image.fromarray(stat).save(os.path.join(dir_, f"{stamp}-{outcome}-{label}-stats.png"))
        _rotate(dir_, f"Z-{outcome}-", DUMP_KEEP.get(outcome, 4))
    return dump


def dump_fits_in(dir_: str, label: str, crop: np.ndarray) -> None:
    try:
        os.makedirs(dir_, exist_ok=True)
        Image.fromarray(crop).save(os.path.join(dir_, f"{_stamp()}-fits-in-{label}.png"))
        _rotate(dir_, "Z-fits-in-", FITS_IN_DUMP_KEEP)
    except Exception as e:  # noqa: BLE001
        _log.warning("fits-in dump failed: %s", e)


# ---- FITS IN plate (§9.3) ---------------------------------------------------------------------

REFERENCE_CONTENT_HEIGHT = 1080
CAPTION_TOP_OFFSET, CAPTION_HEIGHT, CAPTION_HALF_WIDTH, CAPTION_UPSCALE = 110, 60, 150, 4
CAPTION_FIXED_OFFSET_X, CAPTION_FIXED_OFFSET_Y = 738, 366
CAPTION_FIXED_HALF_WIDTH, CAPTION_FIXED_HALF_HEIGHT = 85, 18


def clahe(gray: np.ndarray, tile: int = 64, clip_slope: float = 3.0) -> np.ndarray:
    """Contrast-limited adaptive histogram equalisation (libvips hist_local-like, tiles of `tile` px)."""
    h, w = gray.shape
    ty, tx = max(1, math.ceil(h / tile)), max(1, math.ceil(w / tile))
    luts = np.zeros((ty, tx, 256), dtype=np.float64)
    for j in range(ty):
        for i in range(tx):
            block = gray[j * tile:(j + 1) * tile, i * tile:(i + 1) * tile]
            hist = np.bincount(block.ravel(), minlength=256).astype(np.float64)
            limit = clip_slope * block.size / 256
            excess = np.maximum(hist - limit, 0).sum()
            hist = np.minimum(hist, limit) + excess / 256
            cdf = np.cumsum(hist)
            luts[j, i] = (cdf - cdf[0]) / max(1e-9, cdf[-1] - cdf[0]) * 255
    # bilinear blend between the four nearest tile centres
    yc = (np.arange(h) + 0.5) / tile - 0.5
    xc = (np.arange(w) + 0.5) / tile - 0.5
    y0 = np.clip(np.floor(yc).astype(int), 0, ty - 1)
    x0 = np.clip(np.floor(xc).astype(int), 0, tx - 1)
    y1 = np.clip(y0 + 1, 0, ty - 1)
    x1 = np.clip(x0 + 1, 0, tx - 1)
    fy = np.clip(yc - y0, 0, 1)[:, None]
    fx = np.clip(xc - x0, 0, 1)[None, :]
    g = gray.astype(int)
    a = luts[y0[:, None], x0[None, :], g]
    b = luts[y0[:, None], x1[None, :], g]
    c = luts[y1[:, None], x0[None, :], g]
    d = luts[y1[:, None], x1[None, :], g]
    out = (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


@dataclass
class LabelMatch:
    name: str
    exact: bool


class FitsInReader:
    """Reads the exact weapon variant from the FITS IN plate (bottom right of the Cycle screen)."""

    def __init__(self, models_dir: str, db: Callable[[], rd.RivenDb] = rd.db,
                 strip_reader: Callable[[np.ndarray], Any] | None = None,
                 line_recognizer: Callable[[np.ndarray], str] | None = None,
                 dump_dir: str | None = None):
        self.models_dir = models_dir
        self.db = db
        self._strip = strip_reader
        self._line = line_recognizer
        self.dump_dir = dump_dir
        self.last_rows: list = []

    def _read_strip(self, rgb: np.ndarray):
        if self._strip:
            return self._strip(rgb)
        from .. import ocr
        return ocr.read_strip(Image.fromarray(rgb), self.models_dir)

    def _recognize_line(self, rgb: np.ndarray) -> str:
        if self._line:
            return self._line(rgb)
        from ..ocr import PaddleRecognizer
        rec = PaddleRecognizer.get(self.models_dir)
        if not rec.available():
            return ""
        res = rec.recognize([rgb])
        return (res[0][0] if res else "").strip()

    def _match(self, lines: list[str]) -> LabelMatch | None:
        m = self.db().find_weapon_by_label_line(lines)
        return LabelMatch(m[0], bool(m[1])) if m else None

    def read_panel(self, rgb: np.ndarray, content_h: int, *, invert: bool = False, upscale: bool = True,
                   ui_scale: float | None = None, use_clahe: bool = False) -> LabelMatch | None:
        """readWeaponLabelFromPanelPng: normalise to 1080p text size, strip-read, match rows."""
        ui_corr = 1 / min(1.0, max(0.5, ui_scale if ui_scale is not None else 1.0))
        scale = (REFERENCE_CONTENT_HEIGHT / max(1, content_h)) * ui_corr
        img = rgb
        if scale < 0.98 or ui_corr > 1.02 or (upscale and scale > 1.02):
            h = max(1, js_round(rgb.shape[0] * scale))
            w = max(1, js_round(rgb.shape[1] * h / rgb.shape[0]))
            img = _resize(rgb, w, h, "lanczos")
        if use_clahe:
            g = np.asarray(Image.fromarray(img).convert("L"))
            img = np.stack([clahe(g)] * 3, axis=2)
        if invert:
            img = 255 - img
        read = self._read_strip(np.ascontiguousarray(img))
        rows = list(getattr(read, "rows", None) or [])
        self.last_rows = rows
        if not rows:
            return None
        match = self._match([r.text for r in rows])
        _log.info("fits-in rows: %s -> %s", ", ".join(repr(r.text) for r in rows),
                  f"{match.name}{'' if match.exact else ' (fuzzy)'}" if match else "no weapon")
        return match

    def read(self, rgb: np.ndarray, source_type: str = "screen", canvas: str = "16:9") -> LabelMatch | None:
        """readFitsInWeapon: name crop, inverted name crop, then the upscaled panel crop."""
        content = content_rect(rgb, source_type, canvas)
        name = crop_rect_content(rgb, FITS_IN_NAME_CROP, content)
        if name.shape[1] < 48 or name.shape[0] < 48:
            return None
        m = self.read_panel(name, content.height, upscale=False)
        if m:
            return m
        m = self.read_panel(name, content.height, invert=True, upscale=False)
        if m:
            return m
        panel = crop_rect_content(rgb, FITS_IN_PANEL_CROP, content)
        m = self.read_panel(panel, content.height)
        if not m and self.dump_dir:
            dump_fits_in(self.dump_dir, "panel", panel)
        return m

    def _caption_band(self, wide: np.ndarray, x: float, y: float, w: float, h: float, label: str) -> LabelMatch | None:
        H, W = wide.shape[:2]
        X, Y = max(0, js_round(x)), max(0, js_round(y))
        cw, ch = min(W - X, js_round(w)), min(H - Y, js_round(h))
        if cw < 24 or ch < 10:
            return None
        band = wide[Y:Y + ch, X:X + cw]
        band = _resize(band, cw * CAPTION_UPSCALE, ch * CAPTION_UPSCALE, "lanczos")
        # sharp normalise(): stretch the 1st..99th percentile to the full range
        lo, hi = np.percentile(band, 1), np.percentile(band, 99)
        if hi > lo:
            band = np.clip((band.astype(float) - lo) * 255 / (hi - lo), 0, 255).astype(np.uint8)
        text = self._recognize_line(np.ascontiguousarray(band))
        m = self._match([text]) if text else None
        _log.info("fits-in caption %s (%dx%d at %d,%d): %r -> %s", label, cw, ch, X, Y, text, m.name if m else "none")
        return m

    def read_small_ui(self, rgb: np.ndarray, ui_scale: float, source_type: str = "screen",
                      canvas: str = "16:9") -> LabelMatch | None:
        """readFitsInWeaponSmallUi: wide crop, CLAHE, caption band under the heading, fixed band, inverted."""
        content = content_rect(rgb, source_type, canvas)
        wide = crop_rect_content(rgb, FITS_IN_WIDE_CROP, content)
        H, W = wide.shape[:2]
        if W < 48 or H < 48:
            return None
        self.last_rows = []
        m = self.read_panel(wide, content.height, ui_scale=ui_scale)
        if m:
            return m
        plain = self.last_rows
        m = self.read_panel(wide, content.height, ui_scale=ui_scale, use_clahe=True)
        if m:
            return m
        heading = _heading_row(self.last_rows) or _heading_row(plain)
        unit = (content.height / 1080) * min(1.0, max(0.5, ui_scale))
        if heading is not None:
            bx, by, bw, bh = heading.box
            cx = (bx + bw / 2) * W
            bottom = (by + bh) * H
            m = self._caption_band(wide, cx - CAPTION_HALF_WIDTH * unit, bottom + CAPTION_TOP_OFFSET * unit,
                                   CAPTION_HALF_WIDTH * 2 * unit, CAPTION_HEIGHT * unit, "band")
            if m:
                return m
        ox, oy = FITS_IN_WIDE_CROP.x * content.width, FITS_IN_WIDE_CROP.y * content.height
        cx = content.width / 2 + CAPTION_FIXED_OFFSET_X * unit - ox
        cy = content.height / 2 + CAPTION_FIXED_OFFSET_Y * unit - oy
        hw, hh = (CAPTION_FIXED_HALF_WIDTH + 20) * unit, (CAPTION_FIXED_HALF_HEIGHT + 8) * unit
        m = self._caption_band(wide, cx - hw, cy - hh, hw * 2, hh * 2, "fixed")
        if m:
            return m
        m = self.read_panel(wide, content.height, invert=True, ui_scale=ui_scale)
        if not m and self.dump_dir:
            dump_fits_in(self.dump_dir, "wide", wide)
        return m


def _heading_row(rows: list) -> Any:
    for r in rows or []:
        if not getattr(r, "box", None):
            continue
        compact = re.sub(r"[^a-z]", "", r.text.lower())
        if "tsin" in compact or "fitsi" in compact:
            return r
    return None


def should_apply_label_weapon(match: LabelMatch, current: str, current_source: str, same_family: bool) -> bool:
    if not current or current == "Riven":
        return True
    if match.name == current:
        return False
    if match.exact or same_family:
        return True
    return current_source == "ocr"


# ---- scan profiles (§6) -----------------------------------------------------------------------

@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    crop: str
    ready_gate: float | None  # seconds, None = no gate
    retry_delay: float
    accept_equal_retry: bool
    roll_reveal: bool


PROFILES = {
    "initial": Profile("initial", "initial-card", "singleCard", 1.8, 0.65, False, False),
    "chat": Profile("chat", "chat-card", "chatCard", 1.8, 0.65, False, False),
    "roll": Profile("roll", "roll-right", "rollCard", None, 0.8, True, True),
    "choice": Profile("choice", "choice-rescan", "singleCard", None, 0.5, False, False),
}

READY_POLL = 0.04
READY_REQUIRED_HITS = 3
READY_MAX_NULL_CAPTURES = 3
READY_SCORE_THRESHOLD = 0.2


@dataclass
class Capture:
    rgb: np.ndarray
    source_type: str = "screen"
    at: float = 0.0


@dataclass
class ScanResult:
    stats: list[dict] = field(default_factory=list)
    text: str = ""
    low_confidence: bool = False
    capture: Capture | None = None
    elapsed_ms: int = 0
    outcome: str = "empty"
    ready: bool | None = None


class RivenScanner:
    """Capture + crop + recognise, with the readiness gate, one retry and stale-generation checks."""

    def __init__(self, models_dir: str, capture: Callable[[], Capture | None],
                 ocr: Callable[[np.ndarray], OcrRead] | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 canvas: Callable[[], str] = lambda: "16:9", dump_dir: Callable[[], str | None] = lambda: None):
        self.models_dir = models_dir
        self._capture = capture
        self._ocr = ocr or (lambda rgb: recognize_stat_area(rgb, models_dir))
        self.clock = clock
        self.sleep = sleep
        self.canvas = canvas
        self.dump_dir = dump_dir
        self.generation = 0
        self.aborted = False
        self._lock = threading.Lock()
        self.last_capture_ms: int | None = None

    # -- generation / abort ---------------------------------------------------------------

    def abort(self) -> None:
        self.aborted = True

    def reset_abort(self) -> None:
        self.aborted = False

    def _begin(self) -> int:
        with self._lock:
            self.generation += 1
            return self.generation

    def is_stale(self, gen: int) -> bool:
        return gen != self.generation or self.aborted

    def ocr_available(self) -> bool:
        try:
            from ..ocr import PaddleRecognizer, StatLineDetector
            return PaddleRecognizer.get(self.models_dir).available() and StatLineDetector.get(self.models_dir).available()
        except Exception:  # noqa: BLE001
            return False

    # -- capture ------------------------------------------------------------------------------

    def capture(self) -> Capture | None:
        t0 = self.clock()
        try:
            cap = self._capture()
        except Exception as e:  # noqa: BLE001 - capture backends raise CaptureError and friends
            _log.debug("capture failed: %s", e)
            return None
        self.last_capture_ms = int((self.clock() - t0) * 1000)
        return cap

    def _rough(self, cap: Capture, rect: NRect) -> np.ndarray:
        return crop_rect_content(cap.rgb, rect, content_rect(cap.rgb, cap.source_type, self.canvas()))

    def frame_hash_for(self, cap: Capture, rect: NRect) -> str:
        return frame_hash(self._rough(cap, rect))

    def wait_ready(self, rect: NRect, budget: float) -> tuple[bool, Capture | None, str]:
        """waitForRivenUiReady: 3 consecutive stable text-metric frames within the budget."""
        start = self.clock()
        hits = nulls = 0
        best_score = 0.0
        best_cap: Capture | None = None
        best_hash = ""
        last_m: TextMetrics | None = None
        last_hash = ""
        last_cap: Capture | None = None
        while self.clock() - start < budget:
            if self.aborted:
                break
            cap = self.capture()
            if cap is None:
                hits = 0
                nulls += 1
                if nulls >= READY_MAX_NULL_CAPTURES:
                    break
                self.sleep(READY_POLL)
                continue
            nulls = 0
            last_cap = cap
            rough = self._rough(cap, rect)
            m = text_metrics(rough)
            hsh = frame_hash(rough)
            if m.score > best_score:
                best_score, best_cap, best_hash = m.score, cap, hsh
            stable = m.score >= READY_SCORE_THRESHOLD
            if stable and last_m and last_m.bounds and m.bounds:
                rh, rw = rough.shape[:2]
                stable = ((abs(m.coverage - last_m.coverage) <= 0.025 or m.coverage > 0.25)
                          and abs(m.bounds.left - last_m.bounds.left) / max(1, rw) <= 0.05
                          and abs(m.bounds.top - last_m.bounds.top) / max(1, rh) <= 0.05
                          and abs(m.bounds.width - last_m.bounds.width) / max(1, rw) <= 0.08
                          and abs(m.bounds.height - last_m.bounds.height) / max(1, rh) <= 0.08)
            hits = hits + 1 if stable else 0
            last_m, last_hash = m, hsh
            if hits >= READY_REQUIRED_HITS:
                return True, cap, hsh
            self.sleep(READY_POLL)
        return False, last_cap or best_cap, last_hash or best_hash

    # -- recognition -----------------------------------------------------------------------------

    def recognize(self, cap: Capture, profile: Profile, gen: int, label: str | None = None) -> CardRead:
        rect = CROPS[profile.crop]
        card, stat = crop_stat_image(cap.rgb, rect, cap.source_type, self.canvas())
        d = self.dump_dir()
        return recognize_card_stats(card, stat, self._ocr, roll_reveal=profile.roll_reveal,
                                    is_stale=lambda: self.is_stale(gen), label=label or profile.label,
                                    dump=make_dumper(d) if d else None)

    def scan(self, profile_name: str) -> ScanResult:
        """runRivenScanAttempt for one profile; a new public scan makes older ones stale."""
        profile = PROFILES[profile_name]
        gen = self._begin()
        t0 = self.clock()
        try:
            return self._attempt(profile, gen, t0)
        except Exception:  # noqa: BLE001
            _log.exception("%s scan failed", profile.label)
            return ScanResult(elapsed_ms=int((self.clock() - t0) * 1000))

    def scan_image(self, rgb: np.ndarray, profile_name: str = "initial", source_type: str = "screen") -> ScanResult:
        """Recognise one still image (debug RPC / tests): no gate, no retry capture."""
        profile = PROFILES[profile_name]
        gen = self._begin()
        t0 = self.clock()
        cap = Capture(rgb, source_type, t0)
        r = self.recognize(cap, profile, gen)
        return ScanResult(r.stats, r.text, r.low_confidence, cap, int((self.clock() - t0) * 1000), r.outcome)

    def _attempt(self, profile: Profile, gen: int, t0: float) -> ScanResult:
        rect = CROPS[profile.crop]
        ready = None
        hsh = ""
        if profile.ready_gate:
            ready, cap, hsh = self.wait_ready(rect, profile.ready_gate)
            _log.info("%s: ready=%s", profile.label, ready)
        else:
            cap = self.capture()
        if cap is None or self.is_stale(gen):
            return ScanResult(elapsed_ms=int((self.clock() - t0) * 1000), ready=ready)
        hsh = hsh or self.frame_hash_for(cap, rect)
        r = self.recognize(cap, profile, gen)

        def done(read: CardRead, c: Capture) -> ScanResult:
            return ScanResult(read.stats, read.text, read.low_confidence, c, int((self.clock() - t0) * 1000),
                              read.outcome, ready)

        if len(r.stats) >= MIN_ACCEPTABLE_STATS or self.is_stale(gen):
            return done(r, cap)
        _log.info("%s: sparse result (%d stats), retrying in %dms", profile.label, len(r.stats),
                  profile.retry_delay * 1000)
        self.sleep(profile.retry_delay)
        if self.is_stale(gen):
            return done(r, cap)
        cap2 = self.capture()
        if cap2 is None:
            return done(r, cap)
        h2 = self.frame_hash_for(cap2, rect)
        if h2 and h2 == hsh:
            _log.info("%s-retry skipped identical frame hash", profile.label)
            return done(r, cap)
        r2 = self.recognize(cap2, profile, gen, f"{profile.label}-retry")
        better = len(r2.stats) >= len(r.stats) if profile.accept_equal_retry else len(r2.stats) > len(r.stats)
        if better:
            return done(r2, cap2)
        return done(r, cap)
