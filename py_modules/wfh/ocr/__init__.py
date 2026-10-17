"""OCR foundation: PaddleOCR line recognizer, YOLO stat-line detector, strip reader.

Scanners call these from worker threads (app.run_blocking / app.submit): every
call here is CPU bound and blocks for tens to hundreds of milliseconds.
"""

import os
import re
import threading
from dataclasses import dataclass

from .. import log
from . import image
from .paddle import PaddleRecognizer, RecResult, trimmed_line_confidence
from .yolo import Box, StatLineDetector, extract_line_crops

_log = log.get("ocr")

__all__ = [
    "Box", "PaddleRecognizer", "available", "RecResult", "StatLineDetector", "StripRead", "StripRow",
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


def available(models_dir: str) -> bool:
    """True when the recognizer model and its dict are on disk.

    Lets a scanner report "ocr-unavailable" instead of "unreadable", since
    read_strip returns None for both.
    """
    return PaddleRecognizer.get(models_dir).available()


def read_strip(img, models_dir: str, recognizer: PaddleRecognizer | None = None) -> StripRead | None:
    """Read a reward title strip (recognizeRewardStripOnnx): rows on Otsu mono, text on RGB.

    Returns None when the model is unavailable, the strip is tiny, has no ink
    rows, no row reaches 0.55 confidence, or anything fails (logged as
    "strip read failed", rewardOcrOnnx.ts:185-188). Never raises, so a job on
    app.submit falls back to its failure path instead of dying.
    """
    rec = recognizer or PaddleRecognizer.get(models_dir)
    if not rec.available():
        return None
    try:
        return _read_strip(img, rec)
    except Exception as e:  # noqa: BLE001 - match WFHelper: any failure is "no read"
        _log.warning("strip read failed: %s", e)
        return None


def _read_strip(img, rec: PaddleRecognizer) -> StripRead | None:
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


# One warm-up per (models_dir, detector) for the process: rewards, planner and
# riven all call warmup() from setup(), and each extra inference would compete
# with the game for CPU at plugin load (relic-rewards.md §6.1, pitfall 7).
_warmed: set[tuple[str, bool]] = set()
_warmed_lock = threading.Lock()


def warmup(models_dir: str, detector: bool = False) -> None:
    """Load the recognizer (and optionally the detector) off the scan path, in a thread, once."""
    key = (os.path.abspath(models_dir), detector)
    with _warmed_lock:
        if key in _warmed:
            return
        _warmed.add(key)
    if not PaddleRecognizer.get(models_dir).available():
        return

    def run():
        try:
            PaddleRecognizer.get(models_dir).warmup()
            if detector:
                StatLineDetector.get(models_dir).warmup()
        except Exception as e:  # noqa: BLE001 - warm-up is best effort
            _log.warning("ocr warmup failed: %s", e)

    threading.Thread(target=run, name="wfh-ocr-warmup", daemon=True).start()
