"""Reward-screen geometry and slot/player-count detection.

Port of WFHelper services/rewardScannerImage.ts (relic-rewards.md §4-§5):

- fixed 16:9 card layouts measured at 99 % interface scale, corrected first for
  the EE.cfg UI scale and then for the frame's aspect (16:10 on the Deck squeezes
  y by 0.9 about the centre),
- the card-bar counter (preferred), with the interface-scale search,
- activity-scored fixed layouts (fallback; what fires on 16:10 frames),
- column projection (last resort),
- the layout gate and the frame sample used for dedup.

Frames are numpy HxWx3 uint8 RGB. WFHelper works on BGRA bitmaps; every formula
here is symmetric in the channels (Euclidean distance) or names R/G/B
explicitly, so the channel order is handled where luma is computed.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

import numpy as np

REFERENCE_UI_SCALE = 0.99
REFERENCE_ASPECT = 16 / 9
MIN_LAYOUT_UI_SCALE = 0.25
MAX_REWARD_SLOTS = 4


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    def to_json(self) -> dict:
        return {"x": round(self.x, 5), "y": round(self.y, 5), "w": round(self.w, 5), "h": round(self.h, 5)}


_CARD_W, _CARD_H, _CARD_Y = 0.122, 0.225, 0.225
FIXED_LAYOUTS: dict[int, tuple[Rect, ...]] = {
    1: (Rect(0.439, _CARD_Y, _CARD_W, _CARD_H),),
    2: (Rect(0.3755, _CARD_Y, _CARD_W, _CARD_H), Rect(0.5025, _CARD_Y, _CARD_W, _CARD_H)),
    3: (Rect(0.312, _CARD_Y, _CARD_W, _CARD_H), Rect(0.439, _CARD_Y, _CARD_W, _CARD_H),
        Rect(0.566, _CARD_Y, _CARD_W, _CARD_H)),
    4: (Rect(0.245, _CARD_Y, _CARD_W, _CARD_H), Rect(0.372, _CARD_Y, _CARD_W, _CARD_H),
        Rect(0.499, _CARD_Y, _CARD_W, _CARD_H), Rect(0.626, _CARD_Y, _CARD_W, _CARD_H)),
}
SLOT_LAYOUT_REGION = Rect(0.03, 0.37, 0.94, 0.34)

# Card-bar counter (countRewardCardsInBitmap).
CARD_COUNTER_BAND = (0.431, 0.027)  # top, height
CARD_COUNTER_WINDOW_WIDTH = 0.064
CARD_COUNTER_WINDOWS = ((4, (0.01, 0.91)), (3, (0.15, 0.78)), (2, (0.28, 0.665)), (1, (0.4, 0.535)))
CARD_ROW_LEFT = FIXED_LAYOUTS[4][0].x
CARD_ROW_WIDTH = FIXED_LAYOUTS[4][3].x + FIXED_LAYOUTS[4][3].w - CARD_ROW_LEFT
LEGACY_UI_SCALE_TIMES_HEIGHT = 885
LEGACY_PROBE_STEPS = 3
CARD_SEARCH_MIN_UI_SCALE = 0.5
CARD_SEARCH_MAX_UI_SCALE = 1.0

# Layout gate (hasConfidentSlotLayout; SCANNER_TUNING.slot.minLayoutConfidence).
MIN_LAYOUT_CONFIDENCE = 0.38
SINGLE_LAYOUT_CONFIDENCE = 0.55
MAX_LAYOUTS = 6

BAND_SAMPLE_STEP_X = 8
BAND_SAMPLE_STEP_Y = 2


def js_round(x: float) -> int:
    return math.floor(x + 0.5)


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def clamp01(v: float) -> float:
    return clamp(v, 0.0, 1.0)


# ---- corrections (§4.2, §4.3) ------------------------------------------------

def aspect_scale(width: int, height: int) -> tuple[float, float]:
    """(scaleX, scaleY): menus sit on a centred 16:9 canvas. 1280x800 -> (1, 0.9)."""
    if not width > 0 or not height > 0:
        return 1.0, 1.0
    ref_w = height * REFERENCE_ASPECT
    ref_h = width / REFERENCE_ASPECT
    return (ref_w / width if ref_w < width else 1.0, ref_h / height if ref_h < height else 1.0)


def ui_correct(rects, ui_scale: float) -> list[Rect]:
    try:
        ui = float(ui_scale)
    except (TypeError, ValueError):
        ui = REFERENCE_UI_SCALE
    if not math.isfinite(ui):
        ui = REFERENCE_UI_SCALE
    s = clamp(ui, MIN_LAYOUT_UI_SCALE, 1.0) / REFERENCE_UI_SCALE
    return [Rect(0.5 + (r.x - 0.5) * s, 0.5 + (r.y - 0.5) * s, r.w * s, r.h * s) for r in rects]


def aspect_correct(rects, scale: tuple[float, float]) -> list[Rect]:
    sx, sy = scale
    if sx >= 1 and sy >= 1:
        return list(rects)
    return [Rect(0.5 + (r.x - 0.5) * sx, 0.5 + (r.y - 0.5) * sy, r.w * sx, r.h * sy) for r in rects]


def card_rects(count: int, ui_scale: float, size: tuple[int, int]) -> list[Rect]:
    """The corrected (UI scale first, then aspect) card rects of an N-card row."""
    return aspect_correct(ui_correct(FIXED_LAYOUTS[count], ui_scale), aspect_scale(*size))


def title_rect(card: Rect) -> Rect:
    """buildFixedSlots: the title strip is 18 % of the card, 70 % down."""
    return Rect(card.x, card.y + card.h * 0.70, card.w, card.h * 0.18)


def crop_box(size: tuple[int, int], rect: Rect) -> tuple[int, int, int, int]:
    """cropRect rounding: (x, y, w, h) px, clamped to the frame."""
    width, height = size
    xr = clamp(rect.x, 0.0, 0.98)
    yr = clamp(rect.y, 0.0, 0.98)
    wr = clamp(rect.w, 0.02, max(0.02, 1 - xr))
    hr = clamp(rect.h, 0.02, max(0.02, 1 - yr))
    x = math.floor(width * xr)
    y = math.floor(height * yr)
    w = max(24, math.floor(width * wr))
    h = max(24, math.floor(height * hr))
    return x, y, max(1, min(w, width - x)), max(1, min(h, height - y))


def crop(px: np.ndarray, rect: Rect) -> np.ndarray:
    h, w = px.shape[:2]
    x, y, cw, ch = crop_box((w, h), rect)
    return px[y:y + ch, x:x + cw]


@dataclass
class SlotRect:
    index: int
    card: Rect
    title: Rect

    def to_json(self) -> dict:
        return {"index": self.index, "card": self.card.to_json(), "title": self.title.to_json()}


@dataclass
class Layout:
    count: int
    confidence: float
    slots: list[SlotRect]
    counted: bool = False
    ui_scale: float = REFERENCE_UI_SCALE
    method: str = "activity"  # counter | activity | projection

    def to_json(self) -> dict:
        return {"count": self.count, "confidence": self.confidence, "counted": self.counted,
                "ui_scale": self.ui_scale, "method": self.method}


def fixed_slots(cards: list[Rect]) -> list[SlotRect]:
    return [SlotRect(i, c, title_rect(c)) for i, c in enumerate(cards)]


# ---- pixel helpers -------------------------------------------------------------

def _as_int(px: np.ndarray) -> np.ndarray:
    return px.astype(np.int32, copy=False)


def _dist(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    d = a - b
    return np.sqrt((d * d).sum(axis=-1))


def luma(px: np.ndarray) -> np.ndarray:
    """luminanceFromBgr: (77R + 150G + 29B) >> 8."""
    p = _as_int(px)
    return (77 * p[..., 0] + 150 * p[..., 1] + 29 * p[..., 2]) >> 8


# ---- card-bar counter (§5.1) -------------------------------------------------------

def window_has_card_bar(px: np.ndarray, left: int, top: int, width: int, height: int, thin: bool = False) -> bool:
    """One thin horizontal bar of even colour fills the window. px is int32 HxWx3."""
    if width < 12 or height < 6:
        return False
    H, W = px.shape[:2]
    if left < 0 or top < 0 or left + width > W or top + height > H:
        return False
    mid = top + height // 2
    frm = left + width // 6
    to = left + (5 * width) // 6
    row = px[mid, frm:to]
    if len(row) >= 2:
        d = _dist(row[1:], row[:-1])
        breaks = np.flatnonzero(d >= 45)
        run = int(breaks[0]) if breaks.size else len(d)
    else:
        run = 0
    if run / (width * 0.6) < 0.9:
        return False
    block = px[top:top + height, left:left + width]
    ok = _dist(block[1:], block[:-1]) <= 32  # ok[r]: rows r and r+1 alike
    m = mid - top
    up = ok[m - 1::-1] if m >= 1 else ok[:0]
    down = ok[m:]
    tall = np.cumprod(up, axis=0).sum(axis=0) + np.cumprod(down, axis=0).sum(axis=0)
    heights = tall.astype(np.float64)
    avg = float(heights.mean())
    trimmed = np.sort(heights)[::-1][3:]
    var = float(((trimmed - avg) ** 2).sum() / max(1, len(trimmed)))
    norm_avg = avg / height
    norm_std = 5 * math.sqrt(var) / height
    if int((heights < (1 if thin else 2)).sum()) > len(heights) / 3:
        return False
    return (avg >= 1 if thin else norm_avg >= 0.05) and norm_avg <= 0.27 and norm_std <= 0.36


@dataclass
class CardBarBand:
    row_left: int
    row_width: int
    top: int
    band_height: int
    window_width: int


def card_bar_band(width: int, height: int, ui_scale: float, scale: tuple[float, float]) -> CardBarBand:
    top_r, h_r = CARD_COUNTER_BAND
    (row,) = aspect_correct(ui_correct([Rect(CARD_ROW_LEFT, top_r, CARD_ROW_WIDTH, h_r)], ui_scale), scale)
    row_left = js_round(row.x * width)
    row_width = js_round(row.w * width)
    top = max(0, js_round(row.y * height))
    band_h = min(height - top, js_round(row.h * height))
    return CardBarBand(row_left, row_width, top, band_h, js_round(row_width * CARD_COUNTER_WINDOW_WIDTH))


def count_cards(px: np.ndarray, ui_scale: float, scale: tuple[float, float] | None = None) -> int:
    """countRewardCardsInBitmap at one interface scale; 0 when no bar is found."""
    H, W = px.shape[:2]
    scale = scale or aspect_scale(W, H)
    p = _as_int(px)
    b = card_bar_band(W, H, ui_scale, scale)
    for count, starts in CARD_COUNTER_WINDOWS:
        for s in starts:
            left = b.row_left + js_round(b.row_width * s)
            if left < 0 or left + b.window_width > W:
                continue
            if window_has_card_bar(p, left, b.top, b.window_width, b.band_height):
                return count
    return 0


def thin_bar_share(px: np.ndarray, row: int, reach: int, frm: int, to: int, step: int = 1) -> float:
    H, W = px.shape[:2]
    if to <= frm:
        return 0.0
    xs = np.arange(frm, to, step)
    if row - reach < 0 or row + reach >= H:
        return 0.0
    xs = xs[(xs >= 0) & (xs < W)]
    sampled = len(range(frm, to, step))
    here = px[row, xs]
    cross = (_dist(here, px[row - reach, xs]) > 32) & (_dist(here, px[row + reach, xs]) > 32)
    return float(cross.sum()) / max(1, sampled)


def _bar_ends_at(px: np.ndarray, mid: int, reach: int, edge: int, outward: int, card_w: float) -> bool:
    W = px.shape[1]
    near = max(1, js_round(card_w * 0.03))
    far = max(near + 2, js_round(card_w * 0.1))
    inside = (edge + near, edge + far) if outward < 0 else (edge - far, edge - near)
    outside = (edge - far, edge - near) if outward < 0 else (edge + near, edge + far)
    if outside[0] < 0 or outside[1] > W:
        return False
    return (thin_bar_share(px, mid, reach, *inside) >= 2 / 3
            and thin_bar_share(px, mid, reach, *outside) <= 1 / 3)


def _bar_row_near(px: np.ndarray, band: CardBarBand) -> tuple[int, int] | None:
    H, W = px.shape[:2]
    middle = band.top + band.band_height // 2
    reach = max(2, math.ceil(band.band_height / 4))
    slack = max(2, js_round(band.band_height / 8))
    frm = max(0, band.row_left)
    to = min(W, band.row_left + band.row_width)
    step = max(1, band.row_width // 256)
    best, best_share = None, -1.0
    for row in range(middle - slack, middle + slack + 1):
        if row - band.band_height < 0 or row + band.band_height >= H:
            continue
        share = thin_bar_share(px, row, reach, frm, to, step)
        if share > best_share:
            best_share, best = share, (row, reach)
    return best


def _card_row_verified(px: np.ndarray, count: int, ui_scale: float, scale: tuple[float, float],
                       mid: int, reach: int, band_height: int, window_width: int) -> bool:
    W = px.shape[1]
    top = mid - band_height // 2

    def has_bar(left: int) -> bool:
        return left >= 0 and left + window_width <= W and window_has_card_bar(
            px, left, top, window_width, band_height, thin=True)

    cards = [(js_round(c.x * W), c.w * W) for c in aspect_correct(ui_correct(FIXED_LAYOUTS[count], ui_scale), scale)]
    inset = lambda cw: js_round(cw * 0.08)  # noqa: E731
    for left, cw in cards:
        if not has_bar(left + inset(cw)):
            return False
        if not has_bar(js_round(left + cw) - inset(cw) - window_width):
            return False
    first_left, first_w = cards[0]
    last_left, last_w = cards[-1]
    left_edge = first_left
    right_edge = js_round(last_left + last_w)
    if not _bar_ends_at(px, mid, reach, left_edge, -1, first_w):
        return False
    if not _bar_ends_at(px, mid, reach, right_edge, 1, last_w):
        return False
    step = max(1, window_width >> 1)
    offset = inset(first_w)
    while offset + window_width <= first_w * 1.1:
        if has_bar(left_edge - offset - window_width):
            return False
        if has_bar(right_edge + offset):
            return False
        offset += step
    return True


def find_cards(px: np.ndarray, ui_scale: float) -> tuple[int, float]:
    """findRewardCardsInBitmap: (count, ui scale it was read at); count 0 = not found.

    The configured scale first; then the Legacy-size probe (never in range on a
    Deck: 885/720 = 1.23) and a sweep over 50..100 % whose hits must agree.
    """
    H, W = px.shape[:2]
    scale = aspect_scale(W, H)
    p = _as_int(px)
    configured = count_cards(p, ui_scale, scale)
    if configured > 0:
        return configured, ui_scale
    rows: dict[int, int] = {}

    def row_at(step: int) -> int:
        if step in rows:
            return rows[step]
        cand = step / 100
        band = card_bar_band(W, H, cand, scale)
        bar = _bar_row_near(p, band)
        found = 0
        if bar:
            mid, reach = bar
            for n in (4, 3, 2, 1):
                if _card_row_verified(p, n, cand, scale, mid, reach, band.band_height, band.window_width):
                    found = n
                    break
        rows[step] = found
        return found

    def in_range(step: int) -> bool:
        return MIN_LAYOUT_UI_SCALE * 100 <= step <= CARD_SEARCH_MAX_UI_SCALE * 100

    legacy = LEGACY_UI_SCALE_TIMES_HEIGHT / (H * scale[1])
    legacy_step = js_round(legacy * 100)
    probe = [legacy_step + ((i + 1) // 2 if i % 2 else -(i // 2)) for i in range(LEGACY_PROBE_STEPS * 2 + 1)]
    nearest = next((s for s in probe if in_range(s) and row_at(s) > 0), None)
    if nearest is not None:
        count = row_at(nearest)
        lo = hi = nearest
        while in_range(lo - 1) and row_at(lo - 1) == count:
            lo -= 1
        while in_range(hi + 1) and row_at(hi + 1) == count:
            hi += 1
        if all(row_at(s) == 0 for s in (lo - 1, hi + 1) if in_range(s)):
            step = legacy_step if (legacy_step - lo >= 2 and hi - legacy_step >= 2) else lo + ((hi - lo + 1) >> 1)
            return count, step / 100
    floor = js_round(min(CARD_SEARCH_MIN_UI_SCALE, legacy * 0.9) * 100)
    ceiling = js_round(CARD_SEARCH_MAX_UI_SCALE * 100)
    hits = [(row_at(s), s / 100) for s in range(floor, ceiling + 1) if in_range(s)]
    hits = [h for h in hits if h[0] > 0]
    if hits and all(h[0] == hits[0][0] for h in hits):
        return hits[len(hits) // 2]
    return 0, ui_scale


# ---- activity-scored fixed layouts (§5.2) ---------------------------------------------

def slot_activity(px: np.ndarray, rect: Rect) -> float:
    region = crop(px, rect)
    h, w = region.shape[:2]
    if w < 30 or h < 30:
        return 0.0
    sx = max(1, w // 80)
    sy = max(1, h // 80)
    lum = luma(region[sy::sy])
    xs = np.arange(sx, w, sx)
    if lum.size == 0 or xs.size == 0:
        return 0.0
    cur = lum[:, xs]
    prev = lum[:, xs - sx]
    total = cur.size
    bright = int((cur >= 86).sum())
    texture = int(np.abs(cur - prev).sum())
    score = clamp01(bright / total / 0.24) * 0.45 + clamp01(texture / total / 42) * 0.55
    return round(score, 3)


def _sort_layouts(layouts: list[Layout]) -> list[Layout]:
    import functools

    def cmp(a: Layout, b: Layout) -> int:
        diff = b.confidence - a.confidence
        if abs(diff) < 0.08:
            return b.count - a.count
        return (1 if diff > 0 else -1) if diff else b.count - a.count

    return sorted(layouts, key=functools.cmp_to_key(cmp))


def activity_layouts(px: np.ndarray, ui_scale: float = REFERENCE_UI_SCALE) -> list[Layout]:
    H, W = px.shape[:2]
    out = []
    for count in (1, 2, 3, 4):
        cards = card_rects(count, ui_scale, (W, H))
        acts = [slot_activity(px, c) for c in cards]
        active = sum(1 for a in acts if a >= 0.22)
        avg = sum(acts) / max(1, len(acts))
        conf = round(clamp01(active / count) * 0.58 + clamp01(avg / 0.7) * 0.42, 3)
        if active >= min(2, count) or avg >= 0.28:
            out.append(Layout(count, conf, fixed_slots(cards), False, ui_scale, "activity"))
    return _sort_layouts(out)


# ---- column projection (§5.3) ------------------------------------------------------------

def projection_layout(px: np.ndarray) -> Layout:
    H, W = px.shape[:2]
    region_rect = aspect_correct([SLOT_LAYOUT_REGION], aspect_scale(W, H))[0]
    rx, ry, rw, rh = crop_box((W, H), region_rect)
    region = px[ry:ry + rh, rx:rx + rw]
    h, w = region.shape[:2]
    if w < 120 or h < 60:
        return Layout(0, 0.0, [], method="projection")
    rows = _as_int(region[0::2])
    lum = luma(rows)
    mx = rows.max(axis=-1)
    mn = rows.min(axis=-1)
    sat = np.where(mx == 0, 0.0, (mx - mn) / np.maximum(mx, 1))
    col = ((lum >= 108) | ((lum >= 86) & (sat <= 0.38))).sum(axis=0).astype(np.float64)
    if len(col) > 2:
        prev = np.concatenate([col[:1], col[:-1]])
        nxt = np.concatenate([col[1:], col[-1:]])
        smooth = (prev + col + nxt) / 3
    else:
        smooth = col
    mean = float(smooth.mean())
    std = float(smooth.std())
    threshold = max(3.0, mean + std * 0.38)
    min_run = max(18, w * 5 // 100)
    runs = []
    start = -1
    for i, v in enumerate(smooth):
        if v >= threshold:
            if start < 0:
                start = i
            continue
        if start >= 0 and i - start >= min_run:
            runs.append((start, i - 1))
        start = -1
    if start >= 0 and len(smooth) - start >= min_run:
        runs.append((start, len(smooth) - 1))
    pad = max(8, w // 100)
    runs = [(max(0, s - pad), min(w - 1, e + pad)) for s, e in runs][:4]
    if not runs:
        return Layout(0, 0.0, [], method="projection")
    slots = []
    for i, (s, e) in enumerate(runs):
        run_w = max(32, e - s + 1)
        xr = region_rect.x + (s / w) * region_rect.w
        wr = (run_w / w) * region_rect.w
        card = Rect(xr, region_rect.y, wr, region_rect.h)
        slots.append(SlotRect(i, card, Rect(xr, region_rect.y + region_rect.h * 0.56, wr, region_rect.h * 0.16)))
    coverage = sum(e - s + 1 for s, e in runs) / max(1, w)
    conf = round(clamp01(len(runs) / 4) * 0.45 + clamp01(coverage / 0.72) * 0.30 + clamp01(std / 36) * 0.25, 3)
    return Layout(len(runs), conf, slots, method="projection")


# ---- candidates + gate (§5, §5.4) --------------------------------------------------------

def layout_candidates(px: np.ndarray, ui_scale: float = REFERENCE_UI_SCALE) -> list[Layout]:
    """detectRewardSlotLayoutCandidates: counter, else activity, else projection."""
    H, W = px.shape[:2]
    count, found_scale = find_cards(px, ui_scale)
    if count > 0:
        cards = card_rects(count, found_scale, (W, H))
        return [Layout(count, 1.0, fixed_slots(cards), True, found_scale, "counter")]
    fixed = activity_layouts(px, ui_scale)
    primary = None if fixed else projection_layout(px)
    by_key: dict[str, Layout] = {}
    for lay in fixed + ([primary] if primary else []):
        if lay.count <= 0:
            continue
        key = f"{lay.count}:" + ",".join(f"{s.card.x:.3f}" for s in lay.slots)
        if key not in by_key or lay.confidence > by_key[key].confidence:
            by_key[key] = lay
    return _sort_layouts(list(by_key.values()))


def is_confident(layout: Layout) -> bool:
    if layout.counted:
        return True
    if layout.count == 1:
        return layout.confidence >= SINGLE_LAYOUT_CONFIDENCE
    return layout.count >= 2 and layout.confidence >= MIN_LAYOUT_CONFIDENCE


def confident_layouts(px: np.ndarray, ui_scale: float = REFERENCE_UI_SCALE) -> list[Layout]:
    return [lay for lay in layout_candidates(px, ui_scale) if is_confident(lay)][:MAX_LAYOUTS]


# ---- frame dedup sample (§7.1) -------------------------------------------------------------

def frame_key(px: np.ndarray, ui_scale: float) -> str | None:
    """sha1 of the green channel over the corrected four-card rect, plus the UI scale."""
    H, W = px.shape[:2]
    cards = card_rects(4, ui_scale, (W, H))
    left = max(0, math.floor(min(c.x for c in cards) * W))
    right = min(W, math.ceil(max(c.x + c.w for c in cards) * W))
    top = max(0, math.floor(min(c.y for c in cards) * H))
    bottom = min(H, math.ceil(max(c.y + c.h for c in cards) * H))
    cols = (right - left) // BAND_SAMPLE_STEP_X
    rows = (bottom - top) // BAND_SAMPLE_STEP_Y
    if cols < 1 or rows < 1:
        return None
    sample = px[top:top + rows * BAND_SAMPLE_STEP_Y:BAND_SAMPLE_STEP_Y,
                left:left + cols * BAND_SAMPLE_STEP_X:BAND_SAMPLE_STEP_X, 1]
    return hashlib.sha1(np.ascontiguousarray(sample).tobytes()).hexdigest() + f":{ui_scale}"


@dataclass
class PixelRect:
    x: int
    y: int
    w: int
    h: int

    def to_json(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


def to_pixels(rect: Rect, size: tuple[int, int]) -> PixelRect:
    return PixelRect(*crop_box(size, rect))


__all__ = [
    "Layout", "PixelRect", "Rect", "SlotRect", "FIXED_LAYOUTS", "MAX_REWARD_SLOTS", "REFERENCE_UI_SCALE",
    "activity_layouts", "aspect_correct", "aspect_scale", "card_rects", "confident_layouts", "count_cards",
    "crop", "crop_box", "find_cards", "frame_key", "is_confident", "layout_candidates",
    "projection_layout", "slot_activity", "title_rect", "to_pixels", "ui_correct", "window_has_card_bar",
]
