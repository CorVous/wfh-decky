"""One reward-screen scan: layouts -> title strips -> OCR -> names -> best layout.

Port of WFHelper services/rewardScannerSlotScan.ts + rewardScannerPipeline.ts
(relic-rewards.md §7). Pure CPU work on one frame; call it off the event loop.
The ONNX reader is the only reader (the Windows OCR and its text fallback are
Windows-only and skipped, §6, §13).
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import numpy as np
from PIL import Image

from .. import log, ocr
from ..data import names
from . import reward_geometry as geo

_log = log.get("rewards.scan")

FRAME_DEDUP_TTL_S = 5.0
NEAR_MISS_RESCUE_MARGIN = 0.06
# computeRewardScanBudgetMs with ocrPasses 2, ocrTimeoutMs 15000: max(1800, min(5000, 800+1000+2000)).
SCAN_BUDGET_S = 3.8

# reader(strip RGB array) -> OCR text or None
Reader = Callable[[np.ndarray], "str | None"]


# The CH model glues the card's quantity marker to the name at Deck strip sizes
# ("2 XForma Blueprint", "2 XFoima Blueprint"). WFHelper's 1-char token filter then
# drops the "2" and leaves "XFoima", which no gate accepts. Re-split it as "2X Foima"
# so the quantity-prefixed pool entry ("2X Forma Blueprint") can match. Deck-only
# deviation from cleanRewardOcrText; it only fires on a leading "<n> X<Capital>".
_GLUED_QUANTITY = re.compile(r"^\s*(\d+)\s*[xX]\s*(?=[A-Z][a-z])")


def clean_reward_ocr_text(text: str) -> str:
    """cleanRewardOcrText: drop 1-char tokens, keep "&" (after re-splitting a glued "2 XName")."""
    t = _GLUED_QUANTITY.sub(lambda m: f"{m.group(1)}X ", str(text or ""))
    return " ".join(w for w in t.split()
                    if w == "&" or len(re.sub(r"[^a-z0-9]", "", w, flags=re.I)) > 1).strip()


@dataclass
class SlotRead:
    index: int
    text: str
    candidates: list[names.Candidate]  # usable, best first
    near_miss: names.Candidate | None
    rect: geo.Rect


@dataclass
class LayoutRun:
    layout: geo.Layout
    rects: list[geo.Rect]
    reads: list[SlotRead | None]
    collected: list[tuple[int, names.Candidate]]
    slot_limit: int


@dataclass
class ScanItem:
    slot: int
    item: Any
    confidence: float
    score: float
    mode: str
    text: str = ""

    @property
    def name(self) -> str:
        return names.item_name(self.item)


@dataclass
class ScanResult:
    items: list[ScanItem] = field(default_factory=list)
    layout: geo.Layout | None = None
    strategy: str = "none"  # slot-primary | slot-merged | slot-rescued | none
    score: float = 0.0
    exact_count: int = 0
    layout_count: int = 0
    slot_count: int = 0
    card_count: int = 0
    capture_size: tuple[int, int] = (0, 0)
    elapsed_ms: int = 0
    reads: list[dict] = field(default_factory=list)
    cached: bool = False

    def meta(self) -> dict:
        return {
            "captureWidth": self.capture_size[0], "captureHeight": self.capture_size[1],
            "score": round(self.score, 2), "exactCount": self.exact_count, "strategy": self.strategy,
            "layoutCount": self.layout_count, "slotCount": self.slot_count, "cardCount": self.card_count,
            "elapsedMs": self.elapsed_ms, "hadOcrSuccess": bool(self.items),
            "uiScale": self.layout.ui_scale if self.layout else None,
            "method": self.layout.method if self.layout else None, "cached": self.cached,
        }

    def card_pixels(self) -> list[tuple[int, geo.PixelRect]]:
        """(slot, card rect in capture px) for every slot of the chosen layout."""
        if not self.layout:
            return []
        return [(s.index, geo.to_pixels(s.card, self.capture_size)) for s in self.layout.slots[:geo.MAX_REWARD_SLOTS]]


def _usable_after_margin(c: names.Candidate) -> bool:
    if c.mode == "substring" and c.confidence <= names.SUBSTRING_SCORE_FLOOR + 1e-6:
        return False
    bumped = names.Candidate(c.item, c.confidence + NEAR_MISS_RESCUE_MARGIN, c.score, c.mode)
    return names.is_usable_slot_candidate(bumped)


def _x_overlap(a: geo.Rect, b: geo.Rect) -> float:
    ov = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    return 0.0 if ov <= 0 else ov / min(a.w, b.w)


def _build(run: LayoutRun, collected: list[tuple[int, names.Candidate]], expected: int, strategy: str) -> dict:
    exact = sum(1 for _, c in collected if c.mode == "exact")
    n = len(collected)
    avg_conf = sum(c.confidence for _, c in collected) / max(1, n)
    avg_score = sum(c.score for _, c in collected) / max(1, n)
    empty = run.slot_limit - n
    fill = min(n, expected) / expected if expected > 0 else 0
    score = (avg_score + n * 44 + exact * 35 + avg_conf * 20 + run.layout.confidence * 12
             + fill * 18 - empty * 30)
    return {"collected": sorted(collected, key=lambda e: e[0]), "score": score, "exact": exact,
            "matched": n, "empty": empty, "strategy": strategy, "run": run}


class RewardScanner:
    """Holds the frame-dedup cache between scans; one per process is enough."""

    def __init__(self, models_dir: str, pool_fn: Callable[[], list] | None = None, reader: Reader | None = None):
        self.models_dir = models_dir
        self._pool_fn = pool_fn
        self._reader = reader
        self._lock = threading.Lock()
        self._last_key: str | None = None
        self._last_at = 0.0
        self._last_result: ScanResult | None = None

    def reset_dedup(self) -> None:
        with self._lock:
            self._last_key, self._last_result = None, None

    def pool(self) -> list:
        if self._pool_fn:
            return self._pool_fn()
        from ..data.items import items

        return items().prime_parts()

    def read_text(self, strip: np.ndarray) -> str | None:
        if self._reader:
            return self._reader(strip)
        r = ocr.read_strip(strip, self.models_dir)
        return r.text if r else None

    def ocr_available(self) -> bool:
        if self._reader:
            return True
        return ocr.available(self.models_dir)

    # -- the scan ----------------------------------------------------------------

    def scan(self, frame: "Image.Image | np.ndarray", ui_scale: float = geo.REFERENCE_UI_SCALE,
             budget_s: float | None = SCAN_BUDGET_S, dedup: bool = True) -> ScanResult:
        t0 = time.monotonic()
        px = np.asarray(frame.convert("RGB")) if isinstance(frame, Image.Image) else np.asarray(frame)[..., :3]
        h, w = px.shape[:2]
        key = geo.frame_key(px, ui_scale) if dedup else None
        if key:
            with self._lock:
                if key == self._last_key and self._last_result and time.monotonic() - self._last_at < FRAME_DEDUP_TTL_S:
                    _log.info("frame unchanged - returning cached result")
                    cached = self._last_result
                    return ScanResult(**{**cached.__dict__, "cached": True})
        layouts = geo.confident_layouts(px, ui_scale)
        result = ScanResult(capture_size=(w, h), layout_count=len(layouts),
                            card_count=layouts[0].count if layouts and layouts[0].counted else 0)
        if layouts:
            self._scan_slots(px, layouts, result, t0, budget_s)
        result.elapsed_ms = round((time.monotonic() - t0) * 1000)
        _log.info("scan %dx%d ui=%s layouts=%d cards=%d -> %s (%s) in %dms", w, h, ui_scale, len(layouts),
                  result.card_count, [i.name for i in result.items], result.strategy, result.elapsed_ms)
        if key and result.items:
            with self._lock:
                self._last_key, self._last_at, self._last_result = key, time.monotonic(), result
        return result

    def _scan_slots(self, px: np.ndarray, layouts: list[geo.Layout], result: ScanResult, t0: float,
                    budget_s: float | None) -> None:
        pool = self.pool()
        cache: dict[str, SlotRead | None] = {}

        def read_slot(rect: geo.Rect, index: int) -> SlotRead | None:
            k = ":".join(f"{v:.4f}" for v in (rect.x, rect.y, rect.w, rect.h))
            if k in cache:
                hit = cache[k]
                return SlotRead(index, hit.text, hit.candidates, hit.near_miss, rect) if hit else None
            if budget_s is not None and time.monotonic() - t0 > budget_s:
                return None
            strip = np.ascontiguousarray(geo.crop(px, rect))
            text = clean_reward_ocr_text(self.read_text(strip) or "")
            usable: list[names.Candidate] = []
            near: names.Candidate | None = None
            if text:
                for c in names.rank_candidates(text, pool, 4):
                    if c.item is None:
                        continue
                    if names.is_usable_slot_candidate(c):
                        usable.append(c)
                    elif near is None or c.confidence > near.confidence:
                        near = c
            usable.sort(key=lambda c: (-c.score, -c.confidence, -len(c.name or "") if c.mode == "exact" else 0))
            read = SlotRead(index, text, usable, near, rect)
            cache[k] = read
            return read

        best: dict | None = None
        runs: list[LayoutRun] = []
        for lay in layouts:
            limit = min(lay.count, geo.MAX_REWARD_SLOTS)
            rects = [s.title for s in lay.slots[:limit]]
            reads = [read_slot(r, i) for i, r in enumerate(rects)]
            result.reads.append({"count": lay.count, "method": lay.method, "confidence": lay.confidence,
                                 "texts": [r.text if r else None for r in reads]})
            collected = [(i, r.candidates[0]) for i, r in enumerate(reads) if r and r.candidates]
            if not collected:
                continue
            run = LayoutRun(lay, rects, reads, collected, limit)
            runs.append(run)
            res = _build(run, collected, geo.MAX_REWARD_SLOTS, "slot-primary")
            if (best is None or res["matched"] > best["matched"]
                    or (res["matched"] == best["matched"]
                        and (res["score"] > best["score"]
                             or (abs(res["score"] - best["score"]) < 12 and res["empty"] < best["empty"])))):
                best = res
            if res["matched"] >= 2 and res["matched"] == limit and res["exact"] == res["matched"] and res["empty"] == 0:
                break  # clean sweep: smaller layouts cannot beat it
        if best is None:
            return
        run: LayoutRun = best["run"]
        collected = list(best["collected"])
        if best["empty"] > 0 and len(runs) > 1:
            filled = {i for i, _ in collected}
            donors: dict[int, names.Candidate] = {}
            for other in runs:
                if other is run:
                    continue
                for idx, cand in other.collected:
                    rect = other.rects[idx]
                    base, base_ov = -1, 0.0
                    for i in range(run.slot_limit):
                        ov = _x_overlap(rect, run.rects[i])
                        if ov > base_ov:
                            base, base_ov = i, ov
                    if base < 0 or base_ov < 0.5 or base in filled:
                        continue
                    if base not in donors or cand.score > donors[base].score:
                        donors[base] = cand
            if donors:
                collected += list(donors.items())
                best = _build(run, collected, geo.MAX_REWARD_SLOTS, "slot-merged")
                collected = list(best["collected"])
        if best["empty"] > 0 and best["exact"] >= 1:
            taken = {c.name for _, c in collected}
            filled = {i for i, _ in collected}
            rescued = []
            for i in range(run.slot_limit):
                r = run.reads[i]
                if i in filled or not r or r.candidates or not r.near_miss:
                    continue
                nm = r.near_miss
                if not _usable_after_margin(nm) or nm.name in taken:
                    continue
                taken.add(nm.name)
                rescued.append((i, nm))
            if rescued:
                collected += rescued
                best = _build(run, collected, geo.MAX_REWARD_SLOTS, "slot-rescued")
                collected = list(best["collected"])
        texts = {i: (run.reads[i].text if run.reads[i] else "") for i in range(run.slot_limit)}
        result.items = [ScanItem(i, c.item, c.confidence, c.score, c.mode, texts.get(i, ""))
                        for i, c in collected][:geo.MAX_REWARD_SLOTS]
        result.layout = run.layout
        result.strategy = best["strategy"]
        result.score = best["score"]
        result.exact_count = best["exact"]
        result.slot_count = run.layout.count


def scan_frames(scanner: RewardScanner, frames: Iterable, ui_scale: float) -> list[ScanResult]:
    """Convenience for tests/debug: scan several frames without dedup."""
    return [scanner.scan(f, ui_scale, budget_s=None, dedup=False) for f in frames]
