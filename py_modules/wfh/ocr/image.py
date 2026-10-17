"""Shared image ops for the OCR scanners (ports of WFHelper's sharp pipelines).

WFHelper resizes with sharp/libvips; Pillow stands in: sharp "lanczos3" maps to
Image.LANCZOS and sharp "linear" to Image.BILINEAR. Arrays are numpy HxWx3
uint8 RGB (alpha dropped, never composited, like sharp's removeAlpha) and HxW
uint8 for gray/mono images.
"""

import math
from typing import NamedTuple

import numpy as np
from PIL import Image

# Reward title strips are upscaled x3 (capped at 720 px) before OCR
# (ocrStripTargetWidth, rewardScannerImage.ts:140-146).
STRIP_UPSCALE = 3
STRIP_TARGET_WIDTH = 720

# splitStripRows tuning (rewardOcrOnnx.ts:12-15). One game text line is
# ~17-22 px at 1080p, ~51-66 px at the calibrated width.
MAX_LINE_HEIGHT = 72
MIN_LINE_HEIGHT = 10
ROW_GAP_TOLERANCE = 2

_KERNELS = {"lanczos": Image.LANCZOS, "linear": Image.BILINEAR, "nearest": Image.NEAREST}


def js_round(x: float) -> int:
    """JavaScript Math.round (half up); Python's round() is banker's rounding."""
    return math.floor(x + 0.5)


def to_rgb(img: "Image.Image | np.ndarray") -> np.ndarray:
    """HxWx3 uint8 RGB from a PIL image or an array (gray, RGB or RGBA)."""
    if isinstance(img, Image.Image):
        if img.mode != "RGB":
            img = img.convert("RGB")
        return np.asarray(img, dtype=np.uint8)
    arr = np.asarray(img)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    if arr.ndim == 2:
        return np.repeat(arr[:, :, None], 3, axis=2)
    if arr.shape[2] == 4:
        return np.ascontiguousarray(arr[:, :, :3])
    return arr


def resize(rgb: np.ndarray, width: int, height: int, kernel: str = "linear") -> np.ndarray:
    """Resize an RGB (or gray) array to exactly width x height."""
    h, w = rgb.shape[:2]
    if (w, h) == (width, height):
        return rgb
    return np.asarray(Image.fromarray(rgb).resize((width, height), _KERNELS[kernel]))


def resize_to_width(rgb: np.ndarray, width: int, kernel: str = "lanczos") -> np.ndarray:
    """Aspect-keeping resize to a width (sharp `resize({width})` rounds the height)."""
    h, w = rgb.shape[:2]
    height = max(1, js_round(h * width / w))
    return resize(rgb, width, height, kernel)


def upscale(rgb: np.ndarray, factor: int, kernel: str = "lanczos") -> np.ndarray:
    """Integer upscale, e.g. the riven stat band x2 with Lanczos3."""
    if factor <= 1:
        return rgb
    h, w = rgb.shape[:2]
    return resize(rgb, w * factor, h * factor, kernel)


def grayscale(rgb: np.ndarray) -> np.ndarray:
    """Luma (ITU-R 601, Pillow's "L"); libvips differs by a level or two, Otsu absorbs it."""
    return np.asarray(Image.fromarray(rgb).convert("L"))


def normalise(gray: np.ndarray, lower: float = 1.0, upper: float = 99.0) -> np.ndarray:
    """sharp normalise(): stretch the lower..upper percentile to 0..255."""
    hist = np.bincount(gray.ravel(), minlength=256)
    cum = np.cumsum(hist)
    total = cum[-1]
    if total == 0:
        return gray
    lo = int(np.searchsorted(cum, total * lower / 100.0))
    hi = int(np.searchsorted(cum, total * upper / 100.0))
    if hi <= lo:
        return gray
    out = (gray.astype(np.float32) - lo) * (255.0 / (hi - lo))
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def otsu_threshold(gray: np.ndarray) -> int:
    """Otsu over a 256-bin histogram; starts at 127 and keeps the first strict maximum.

    Same loop as rewardScannerImage.ts:1142-1167 (it stops once the foreground is empty).
    """
    hist = np.bincount(np.asarray(gray, dtype=np.uint8).ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    w_b = np.cumsum(hist)
    sum_b = np.cumsum(hist * np.arange(256))
    total_sum = sum_b[-1]
    threshold = 127
    best = 0.0
    for t in range(256):
        wb = w_b[t]
        if wb == 0:
            continue
        wf = total - wb
        if wf == 0:
            break
        mb = sum_b[t] / wb
        mf = (total_sum - sum_b[t]) / wf
        between = wb * wf * (mb - mf) * (mb - mf)
        if between > best:
            best = between
            threshold = t
    return threshold


def bright_ink_mono(gray: np.ndarray, threshold: int) -> np.ndarray:
    """Mono with 0 = ink: game text is light on dark, so pixels >= threshold are ink."""
    return np.where(gray >= threshold, 0, 255).astype(np.uint8)


class RowSegment(NamedTuple):
    y1: int
    y2: int  # inclusive


def split_strip_rows(mono: np.ndarray) -> list[RowSegment]:
    """Text rows by horizontal ink projection (splitStripRows, rewardOcrOnnx.ts:39-95).

    Rows with ink >= max(3, 1.5 % of W) belong to a line; a line ends after more
    than 2 quiet rows. Segments over 72 px are merged wrapped lines and split at
    the weakest row of their middle half when it is under 80 % of the peak.
    """
    height, width = mono.shape[:2]
    ink = (mono == 0).sum(axis=1).astype(np.int64)
    min_ink = max(3, width * 15 // 1000)

    segments: list[RowSegment] = []
    start, gap = -1, 0
    for y in range(height):
        if ink[y] >= min_ink:
            if start < 0:
                start = y
            gap = 0
        elif start >= 0:
            gap += 1
            if gap > ROW_GAP_TOLERANCE:
                segments.append(RowSegment(start, y - gap))
                start, gap = -1, 0
    if start >= 0:
        segments.append(RowSegment(start, height - 1))

    out: list[RowSegment] = []
    queue = list(segments)
    while queue:
        seg = queue.pop(0)
        seg_h = seg.y2 - seg.y1 + 1
        if seg_h <= MAX_LINE_HEIGHT:
            out.append(seg)
            continue
        lo = seg.y1 + seg_h // 4
        hi = seg.y1 + (seg_h * 3) // 4
        window = ink[lo:hi + 1]
        valley_y = lo + int(np.argmin(window))  # first minimum, like the strict < loop
        valley_ink = int(ink[valley_y])
        peak_ink = int(ink[seg.y1:seg.y2 + 1].max())
        if valley_ink >= peak_ink * 0.8:
            out.append(seg)
            continue
        queue[0:0] = [RowSegment(seg.y1, valley_y - 1), RowSegment(valley_y + 1, seg.y2)]

    return sorted((s for s in out if s.y2 - s.y1 + 1 >= MIN_LINE_HEIGHT), key=lambda s: s.y1)


def segment_crop(mono: np.ndarray, rgb: np.ndarray, seg: RowSegment) -> tuple[np.ndarray, tuple[int, int, int, int]] | None:
    """Raw RGB crop of one row, trimmed to its ink columns (segmentToRgbCrop).

    Rows are found on mono but recognized on RGB: glare thins strokes that the
    threshold would drop. Returns (crop, (x1, y1, x2, y2) inclusive) or None.
    """
    height, width = mono.shape[:2]
    y1 = max(0, seg.y1 - 4)
    y2 = min(height - 1, seg.y2 + 4)
    cols = np.flatnonzero((mono[y1:y2 + 1] == 0).any(axis=0))
    if cols.size == 0:
        return None
    x1 = max(0, int(cols[0]) - 8)
    x2 = min(width - 1, int(cols[-1]) + 8)
    return np.ascontiguousarray(rgb[y1:y2 + 1, x1:x2 + 1]), (x1, y1, x2, y2)


def strip_target_width(source_width: int) -> int:
    """ocrStripTargetWidth: x3 up to 720 px (Deck 156 px strip -> 468 px)."""
    if not source_width > 0:
        return 0
    return max(8, js_round(min(source_width * STRIP_UPSCALE, STRIP_TARGET_WIDTH)))


class PreparedStrip(NamedTuple):
    rgb: np.ndarray   # Lanczos-resized RGB, not normalised (what the recognizer sees)
    gray: np.ndarray  # resized, grayscale, percentile-normalised
    mono: np.ndarray  # 0 = ink
    threshold: int


def prepare_strip(img: "Image.Image | np.ndarray") -> PreparedStrip | None:
    """Steps 1-5 of recognizeRewardStripOnnx: resize, gray, normalise, Otsu."""
    rgb = to_rgb(img)
    h, w = rgb.shape[:2]
    if w < 8 or h < 8:
        return None
    rgb_t = resize_to_width(rgb, strip_target_width(w), "lanczos")
    gray = normalise(grayscale(rgb_t))
    thr = otsu_threshold(gray)
    return PreparedStrip(rgb_t, gray, bright_ink_mono(gray, thr), thr)
