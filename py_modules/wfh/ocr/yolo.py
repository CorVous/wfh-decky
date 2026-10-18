"""YOLO riven stat-line detector on onnxruntime (port of rivenOcrOnnx.ts yoloDetectStatLines).

One class, input `images` [1,3,640,640] letterboxed with 114 grey, output
`output0` [1,5,8400] rows cx,cy,w,h,conf in letterbox pixels, no NMS in the
graph. Boxes cover the title, stat lines, wrapped fragments and the MR badge.
"""

import math
import os
import threading
from dataclasses import dataclass

import numpy as np

from .. import log
from . import image
from .paddle import session_options

_log = log.get("ocr.yolo")

MODEL_FILE = os.path.join("yolo", "stat_line_detector.onnx")
INPUT_SIZE = 640
CONF_THRESHOLD = 0.25
IOU_THRESHOLD = 0.5

# extractAndUpscaleCrops (rivenOcrOnnx.ts:255-318)
CROP_PAD = 8
MAX_STAT_CROP_HEIGHT = 80
MIN_OCR_WIDTH = 1200
MAX_CROP_DIM = 6000


@dataclass(frozen=True)
class Box:
    """Pixel box in the source image (x2/y2 exclusive-style edges, as WFHelper rounds them)."""

    x1: int
    y1: int
    x2: int
    y2: int
    confidence: float

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1


@dataclass(frozen=True)
class Letterbox:
    blob: np.ndarray  # float32 [1, 3, size, size]
    scale: float
    pad_left: int
    pad_top: int


def letterbox(rgb: np.ndarray, size: int = INPUT_SIZE) -> Letterbox:
    """Fit into size x size (bilinear), centre on 114/255 grey, RGB CHW in 0..1."""
    h, w = rgb.shape[:2]
    scale = min(size / h, size / w)
    new_w = image.js_round(w * scale)
    new_h = image.js_round(h * scale)
    pad_left = (size - new_w) // 2
    pad_top = (size - new_h) // 2
    resized = image.resize(rgb, new_w, new_h, "linear")
    blob = np.full((1, 3, size, size), 114 / 255, dtype=np.float32)
    blob[0, :, pad_top:pad_top + new_h, pad_left:pad_left + new_w] = resized.transpose(2, 0, 1).astype(np.float32) / 255
    return Letterbox(blob, scale, pad_left, pad_top)


def decode(preds: np.ndarray, lb: Letterbox, width: int, height: int, conf_threshold: float = CONF_THRESHOLD) -> list[Box]:
    """[1,5,N] (or [5,N]) predictions to source-pixel boxes, before NMS.

    Clamped to the image; boxes under 10 px wide or 3 px tall are dropped, then
    the coordinates are rounded half up (JS Math.round).
    """
    p = np.asarray(preds, dtype=np.float32)
    if p.ndim == 3:
        p = p[0]
    if p.ndim != 2 or p.shape[0] < 5:
        return []
    keep = np.flatnonzero(p[4] >= conf_threshold)
    boxes: list[Box] = []
    for i in keep:
        cx, cy, bw, bh, conf = (float(v) for v in p[:5, i])
        x1 = min(max((cx - bw / 2 - lb.pad_left) / lb.scale, 0.0), width)
        y1 = min(max((cy - bh / 2 - lb.pad_top) / lb.scale, 0.0), height)
        x2 = min(max((cx + bw / 2 - lb.pad_left) / lb.scale, 0.0), width)
        y2 = min(max((cy + bh / 2 - lb.pad_top) / lb.scale, 0.0), height)
        if x2 - x1 < 10 or y2 - y1 < 3:
            continue
        boxes.append(Box(image.js_round(x1), image.js_round(y1), image.js_round(x2), image.js_round(y2), conf))
    return boxes


def nms(boxes: list[Box], iou_threshold: float = IOU_THRESHOLD) -> list[Box]:
    """Greedy NMS by confidence (stable for ties), result sorted by y1."""
    ordered = sorted(boxes, key=lambda b: -b.confidence)
    kept: list[Box] = []
    for box in ordered:
        area_b = (box.x2 - box.x1) * (box.y2 - box.y1)
        suppressed = False
        for k in kept:
            inter = max(0, min(box.x2, k.x2) - max(box.x1, k.x1)) * max(0, min(box.y2, k.y2) - max(box.y1, k.y1))
            union = area_b + (k.x2 - k.x1) * (k.y2 - k.y1) - inter
            if union > 0 and inter / union > iou_threshold:
                suppressed = True
                break
        if not suppressed:
            kept.append(box)
    kept.sort(key=lambda b: b.y1)
    return kept


def extract_line_crops(rgb: np.ndarray, boxes: list[Box], pad: int = CROP_PAD) -> list[np.ndarray]:
    """Padded RGB crops, uniformly upscaled (bilinear) so the widest reaches 1200 px.

    Crops narrower than 20, shorter than 5 or taller than 80 px are skipped, so
    the result can be shorter than `boxes`.
    """
    h, w = rgb.shape[:2]
    crops: list[np.ndarray] = []
    for b in boxes:
        y1, y2 = max(0, b.y1 - pad), min(h, b.y2 + pad)
        x1, x2 = max(0, b.x1 - pad), min(w, b.x2 + pad)
        if x2 - x1 < 20 or y2 - y1 < 5 or y2 - y1 > MAX_STAT_CROP_HEIGHT:
            continue
        crops.append(np.ascontiguousarray(rgb[y1:y2, x1:x2]))
    if not crops:
        return []
    max_w = max(c.shape[1] for c in crops)
    if max_w >= MIN_OCR_WIDTH:
        return crops
    f = math.ceil(MIN_OCR_WIDTH / max_w)
    return [image.resize(c, min(MAX_CROP_DIM, c.shape[1] * f), min(MAX_CROP_DIM, c.shape[0] * f), "linear") for c in crops]


class StatLineDetector:
    """Lazy-loaded singleton per models dir; detect() is thread-safe."""

    _instances: dict[str, "StatLineDetector"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, models_dir: str):
        self.model_path = os.path.join(models_dir, MODEL_FILE)
        self._session = None
        self._input = ""
        self._size = INPUT_SIZE
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._load_error: Exception | None = None

    @classmethod
    def get(cls, models_dir: str) -> "StatLineDetector":
        key = os.path.abspath(models_dir)
        with cls._instances_lock:
            inst = cls._instances.get(key)
            if inst is None:
                inst = cls._instances[key] = cls(key)
            return inst

    def available(self) -> bool:
        return os.path.isfile(self.model_path)

    def _ensure(self):
        if self._session is not None:
            return self._session
        with self._load_lock:
            if self._session is not None:
                return self._session
            if self._load_error is not None:
                raise self._load_error
            if not self.available():
                self._load_error = FileNotFoundError(f"YOLO model not found at {self.model_path}")
                raise self._load_error
            import onnxruntime as ort

            sess = ort.InferenceSession(self.model_path, sess_options=session_options(), providers=["CPUExecutionProvider"])
            inp = sess.get_inputs()[0]
            self._input = inp.name
            if isinstance(inp.shape[-1], int):
                self._size = inp.shape[-1]
            self._session = sess
            _log.info("YOLO stat-line detector loaded, input %s %d", self._input, self._size)
            return sess

    def warmup(self) -> bool:
        try:
            self.detect(np.zeros((64, 64, 3), dtype=np.uint8))
            return True
        except Exception as e:  # noqa: BLE001 - warmup is best effort
            _log.warning("warmup failed: %s", e)
            return False

    def detect(self, img_rgb, conf_threshold: float = CONF_THRESHOLD, iou_threshold: float = IOU_THRESHOLD) -> list[Box]:
        """Stat-line boxes in source pixels, top to bottom."""
        rgb = image.to_rgb(img_rgb)
        h, w = rgb.shape[:2]
        if h == 0 or w == 0:
            return []
        sess = self._ensure()
        lb = letterbox(rgb, self._size)
        with self._run_lock:
            preds = sess.run(None, {self._input: lb.blob})[0]
        return nms(decode(preds, lb, w, h, conf_threshold), iou_threshold)
