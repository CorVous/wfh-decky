"""OCR foundation: PaddleOCR line recognizer, YOLO stat-line detector, strip reader.

Scanners call these from worker threads (app.run_blocking / app.submit): every
call here is CPU bound and blocks for tens to hundreds of milliseconds.
"""

import re
import threading
from dataclasses import dataclass

from .. import log
from . import image
from .paddle import PaddleRecognizer, RecResult, trimmed_line_confidence
from .yolo import Box, StatLineDetector, extract_line_crops

_log = log.get("ocr")

__all__ = [
    "Box", "PaddleRecognizer", "RecResult", "StatLineDetector", "StripRead", "StripRow",
    "clean_row_text", "extract_line_crops", "image", "read_strip", "trimmed_line_confidence", "warmup",
]

# Reward strip rows under this confidence are noise (rewardOcrOnnx.ts:16).
MIN_ROW_CONFIDENCE = 0.55


def clean_row_text(text: str) -> str:
    """Restore spaces the CH model drops at case boundaries ("XakuPrime" -> "Xaku Prime").

    Then anything outside [A-Za-z0-9 &'-] becomes a space, runs collapse, and
    the ends are trimmed (cleanOnnxRowText, rewardOcrOnnx.ts:98-104).
    """
    t = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
    t = re.sub(r"[^A-Za-z0-9 &'-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class StripRow:
    text: str
    confidence: float
    box: tuple[float, float, float, float]  # x, y, w, h as fractions of the strip


@dataclass
class StripRead:
    text: str
    rows: list[StripRow]


def read_strip(img, models_dir: str, recognizer: PaddleRecognizer | None = None) -> StripRead | None:
    """Read a reward title strip (recognizeRewardStripOnnx): rows on Otsu mono, text on RGB.

    Returns None when the strip is tiny, has no ink rows, or no row reaches
    0.55 confidence. Raises when the model is missing.
    """
    prep = image.prepare_strip(img)
    if prep is None:
        return None
    segments = image.split_strip_rows(prep.mono)
    if not segments:
        return None
    h, w = prep.mono.shape
    crops, boxes = [], []
    for seg in segments:
        cut = image.segment_crop(prep.mono, prep.rgb, seg)
        if cut is None:
            continue
        crop, (x1, y1, x2, y2) = cut
        crops.append(crop)
        boxes.append((x1 / w, y1 / h, (x2 - x1 + 1) / w, (y2 - y1 + 1) / h))
    if not crops:
        return None
    rec = recognizer or PaddleRecognizer.get(models_dir)
    rows = []
    for (text, conf), box in zip(rec.recognize(crops), boxes):
        if conf < MIN_ROW_CONFIDENCE:
            continue
        cleaned = clean_row_text(text)
        if cleaned:
            rows.append(StripRow(cleaned, conf, box))
    if not rows:
        return None
    return StripRead(re.sub(r"\s+", " ", " ".join(r.text for r in rows)).strip(), rows)


def warmup(models_dir: str, detector: bool = False) -> None:
    """Load the recognizer (and optionally the detector) off the scan path, in a thread."""

    def run():
        PaddleRecognizer.get(models_dir).warmup()
        if detector:
            StatLineDetector.get(models_dir).warmup()

    threading.Thread(target=run, name="wfh-ocr-warmup", daemon=True).start()

