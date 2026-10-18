"""PaddleOCR CH PP-OCRv3 text recognizer on onnxruntime (port of rivenOcrOnnx.ts).

Shared by the reward title strips, the riven stat lines and the FITS IN label.
Input crops are single text lines; there is no detector here.
"""

import math
import os
import re
import threading

import numpy as np

from .. import log
from . import image

_log = log.get("ocr.paddle")

MODEL_FILE = os.path.join("paddle", "ch_PP-OCRv3_rec_infer.onnx")
DICT_FILE = os.path.join("paddle", "ch_dict.txt")

REC_IMG_HEIGHT = 48
REC_MAX_CHUNK = 6
# PP-OCRv3 pads a batch out to its widest crop; an 80:1 panel rule beside nine
# text lines once cost 667 MB, so batches group by width under an output budget.
REC_OUTPUT_BUDGET = 48 * 1024 * 1024
REC_MAX_PAD_STRETCH = 2

# The Deck's APU also renders the game: 2 ORT threads keep inference from stuttering it.
INTRA_OP_THREADS = 2
INTER_OP_THREADS = 1


def session_options():
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.intra_op_num_threads = INTRA_OP_THREADS
    opts.inter_op_num_threads = INTER_OP_THREADS
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    opts.log_severity_level = 3
    return opts


class RecResult(tuple):
    """(text, confidence), plus `.char_confidences` (one per character of text).

    A tuple so callers can unpack `text, conf` as the module contract says.
    """

    def __new__(cls, text: str, confidence: float, char_confidences: list[float] | None = None):
        obj = super().__new__(cls, (text, float(confidence)))
        obj.char_confidences = list(char_confidences or [])
        return obj

    @property
    def text(self) -> str:
        return self[0]

    @property
    def confidence(self) -> float:
        return self[1]


def load_dict(path: str) -> list[str]:
    """["blank", *lines]: index 0 is the CTC blank, the last line " " is the space class.

    CR is stripped explicitly; a stray \\r on every character corrupts decoding.
    """
    with open(path, "rb") as f:
        lines = re.split(r"\r?\n", f.read().decode("utf-8"))
    if lines and lines[-1] == "":
        lines.pop()
    return ["blank", *lines]


def ctc_greedy_decode(probs: np.ndarray, charset: list[str]) -> RecResult:
    """Greedy CTC over a [T, C] softmax matrix.

    argmax ties go to the lowest index; a class is emitted when it is not blank,
    differs from the previous step (blanks reset that) and is in the dictionary.
    Confidence is the mean probability of the emitted classes, 0 when none.
    """
    if probs.size == 0:
        return RecResult("", 0.0)
    best = probs.argmax(axis=1)
    best_p = probs[np.arange(len(best)), best]
    prev = np.concatenate(([0], best[:-1]))
    emit = (best != 0) & (best != prev) & (best < len(charset))
    idx = best[emit]
    confs = [float(p) for p in best_p[emit]]
    text = "".join(charset[i] for i in idx)
    conf = sum(confs) / len(confs) if confs else 0.0
    return RecResult(text, conf, confs)


# The element icon after a whole value decodes as one non-ASCII glyph.
ICON_GLYPH_AFTER_VALUE = re.compile(
    r"^([+\-–x×]?\s*[0-9](?:[0-9.,]*[0-9])?(?:%\s*|\s+))([\u0080-￿])(?=\s*[A-Za-z])"
)


def _js_trim_start(s: str) -> str:
    return s.lstrip()


def trimmed_line_confidence(result: RecResult) -> float:
    """Mean confidence without edge spaces and one icon glyph after the value.

    Crop padding decodes as edge spaces at 0.5-0.7 that pulled correct lines
    under the 0.8 gate (rivenOcrOnnx.ts:388-398).
    """
    text, conf = result[0], result[1]
    confs = getattr(result, "char_confidences", None)
    if not confs or len(confs) != len(text):
        return conf
    start = len(text) - len(_js_trim_start(text))
    end = len(text.rstrip())
    if end <= start:
        return conf
    kept = list(confs[start:end])
    m = ICON_GLYPH_AFTER_VALUE.match(text[start:end])
    if m:
        del kept[len(m.group(1)):len(m.group(1)) + len(m.group(2))]
    return sum(kept) / len(kept)


def width_at_48(crop: np.ndarray) -> int:
    h, w = crop.shape[:2]
    return math.ceil(REC_IMG_HEIGHT * (w / h))


def _output_bytes(count: int, img_w: int, classes: int) -> int:
    return count * math.ceil(img_w / 8) * max(1, classes) * 4


def recognition_chunks(crops: list[np.ndarray], classes: int) -> list[list[int]]:
    """Crop indices grouped by width so one wide crop only pads its own kind.

    A crop that busts the budget on its own is a detector artifact and is skipped.
    """
    order = sorted(range(len(crops)), key=lambda i: width_at_48(crops[i]))
    chunks: list[list[int]] = []
    current: list[int] = []
    current_w = 0
    for i in order:
        w = width_at_48(crops[i])
        if _output_bytes(1, w, classes) > REC_OUTPUT_BUDGET:
            _log.warning("skipped %dx%d crop over the decode budget", crops[i].shape[1], crops[i].shape[0])
            continue
        merged = max(current_w, w)
        narrowest = width_at_48(crops[current[0]]) if current else w
        fits = (len(current) < REC_MAX_CHUNK and merged <= narrowest * REC_MAX_PAD_STRETCH
                and _output_bytes(len(current) + 1, merged, classes) <= REC_OUTPUT_BUDGET)
        if current and not fits:
            chunks.append(current)
            current, current_w = [], 0
        current.append(i)
        current_w = max(current_w, w)
    if current:
        chunks.append(current)
    return chunks


def build_batch(crops: list[np.ndarray]) -> np.ndarray:
    """float32 [N, 3, 48, imgW]: bilinear to height 48, RGB CHW, v/127.5-1, zero right pad."""
    img_h = REC_IMG_HEIGHT
    img_w = math.ceil(img_h * max(c.shape[1] / c.shape[0] for c in crops))
    batch = np.zeros((len(crops), 3, img_h, img_w), dtype=np.float32)
    for n, crop in enumerate(crops):
        h, w = crop.shape[:2]
        rw = min(img_w, math.ceil(img_h * (w / h)))
        resized = image.resize(crop, rw, img_h, "linear").astype(np.float32)
        batch[n, :, :, :rw] = resized.transpose(2, 0, 1) / 127.5 - 1.0
    return batch


class PaddleRecognizer:
    """Lazy-loaded singleton per models dir; recognize() is thread-safe."""

    _instances: dict[str, "PaddleRecognizer"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, models_dir: str):
        self.model_path = os.path.join(models_dir, MODEL_FILE)
        self.dict_path = os.path.join(models_dir, DICT_FILE)
        self._session = None
        self._input = ""
        self._charset: list[str] = []
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._load_error: Exception | None = None

    @classmethod
    def get(cls, models_dir: str) -> "PaddleRecognizer":
        key = os.path.abspath(models_dir)
        with cls._instances_lock:
            inst = cls._instances.get(key)
            if inst is None:
                inst = cls._instances[key] = cls(key)
            return inst

    def available(self) -> bool:
        return os.path.isfile(self.model_path) and os.path.isfile(self.dict_path)

    @property
    def charset(self) -> list[str]:
        self._ensure()
        return self._charset

    def _ensure(self):
        if self._session is not None:
            return self._session
        with self._load_lock:
            if self._session is not None:
                return self._session
            if self._load_error is not None:  # a missing file won't fix itself
                raise self._load_error
            if not self.available():
                self._load_error = FileNotFoundError(f"PaddleOCR model or dict missing in {os.path.dirname(self.model_path)}")
                raise self._load_error
            import onnxruntime as ort

            self._charset = load_dict(self.dict_path)
            sess = ort.InferenceSession(self.model_path, sess_options=session_options(), providers=["CPUExecutionProvider"])
            self._input = sess.get_inputs()[0].name
            self._session = sess
            _log.info("PaddleOCR CH v3 loaded, %d classes incl. blank", len(self._charset))
            return sess

    def warmup(self) -> bool:
        """Load the session and run one 160x48 white crop (the first run costs ~1.4 s)."""
        try:
            self.recognize([np.full((48, 160, 3), 255, dtype=np.uint8)])
            return True
        except Exception as e:  # noqa: BLE001 - warmup is best effort
            _log.warning("warmup failed: %s", e)
            return False

    def recognize(self, crops: list) -> list[RecResult]:
        """One (text, confidence) per crop (HxWx3 uint8 RGB, or a PIL image).

        Skipped or empty crops give ("", 0.0). Text keeps the edge spaces the
        model emits; callers trim (see trimmed_line_confidence).
        """
        arrs = [image.to_rgb(c) for c in crops]
        results = [RecResult("", 0.0) for _ in arrs]
        valid = [i for i, a in enumerate(arrs) if a.ndim == 3 and a.shape[0] > 0 and a.shape[1] > 0]
        if not valid:
            return results
        sess = self._ensure()
        sub = [arrs[i] for i in valid]
        for chunk in recognition_chunks(sub, len(self._charset)):
            batch = build_batch([sub[i] for i in chunk])
            with self._run_lock:
                probs = sess.run(None, {self._input: batch})[0]
            for slot, i in enumerate(chunk):
                results[valid[i]] = ctc_greedy_decode(probs[slot], self._charset)
        return results
