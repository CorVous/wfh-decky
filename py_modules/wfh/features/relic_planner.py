"""Relic planner overlay: ranks the player's owned relics when the relic picker opens.

Port of WFHelper ipc/overlay/relicSelection.ts + the picker part of
services/eeLogMonitor.ts (docs/port/relic-planner.md).

Flow
----
EE.log picker line (ThemedProjectionManager LoadingCompleteEnd / PopulateInventoryGrid)
  -> 300 ms debounce, 3 s cooldown -> trigger("eelog")
  -> "scanning" panel (auto-hide 20 s); cached era -> rows at once
  -> refinement on the worker pool: era from activeMissionTag (EE.log) first,
     OCR label cross-check / full OCR fallback on a gamescope capture
  -> owned relics of that era ranked by squad EV at the best owned refinement
  -> panel on the right edge (layer "planner"), auto-hide after
     relic_planner.duration_s (rows) or 4.5 s (nothing to show).
InitMapping line >= 800 ms after open -> picker closed -> hide.

Era OCR (§4) crops fractions of the captured frame. Warframe's 16:10 layout on
the Deck is unverified, so the filter label is read in both the full-frame box
and the 16:9-canvas box (y offset 40 px at 1280x800) and the better read wins.

The overlay cannot take input under gamescope: squad size, filters, rewards
toggle and "show now" / "hide" live in the QAM (src/pages/Relics.tsx) and the
Relics page's "use in overlay" push (relics.push_filters in menus/relics.py).
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw

from .. import capture, log, rpc
from .. import relic_value as rv
from ..eelog import FILE
from ..overlay import draw

_log = log.get("relic_planner")

LAYER = "planner"

# eeLogMonitor.ts:55-123
PICKER_OPEN = [
    re.compile(r"\bThemedProjectionManager\.lua:\s*LoadingCompleteEnd\b", re.I),
    re.compile(r"\bProjection[A-Za-z_]*\.lua:\s*LoadingCompleteEnd\b", re.I),
    re.compile(r"\bThemedProjectionManager\.lua:\s*PopulateInventoryGrid\b", re.I),
    re.compile(r"\bProjectionManager\.lua:\s*PopulateInventoryGrid\b", re.I),
    re.compile(r"\bProjection[A-Za-z_]*\.lua:\s*PopulateInventoryGrid\b", re.I),
]
PICKER_CLOSE = re.compile(r"\bInitMapping for all devices with bindings\b", re.I)
MISSION_TAG = re.compile(r"""(?:^|["\s])activeMissionTag["\s]*[=:]\s*"?([A-Za-z0-9_]+)""")
MISSION_END = re.compile(r"Sys \[Info\]: EOM missionLocationUnlocked=|TopMenu\.lua: Abort:")

TRIGGER_DELAY_S = 0.300
PICKER_COOLDOWN_S = 3.0
CLOSE_COOLDOWN_S = 0.5
ENTRY_WINDOW_S = 0.8
MIN_EELOG_TRIGGER_GAP_S = 0.9
REOPEN_SUPPRESS_S = 3.0
MISSION_TIER_CACHE_TTL_S = 25 * 60
RECOMMENDATION_CACHE_TTL_S = 10.0
ERA_DETECTION_TIMEOUT_S = 2.4
ERA_LABEL_TIMEOUT_S = 1.5
ERA_TOTAL_TIMEOUT_S = 3.0
ERA_RETRY_DELAY_S = 0.7
ERA_MIN_RETRY_S = 0.6
ERA_START_DELAY_S = 0.1
MIN_ERA_OCR_ATTEMPT_S = 0.9
AUTO_HIDE_FAILURE_S = 4.5
AUTO_HIDE_SUCCESS_S = 120.0
EVENT_ROWS = 12
AUTO_HIDE_DETECTING_S = 20.0
ERA_CONFIDENT = 0.9

# Panel geometry, in 1280x800 design pixels (scaled to the overlay size).
DESIGN_W, DESIGN_H = 1280, 800
PANEL_W = 380
PANEL_MAX_H = 720
PANEL_MARGIN = 8
PANEL_TOP = 40
PRICES_MISSING_NOTE = 5  # show "prices loading" on the panel from this many unpriced rewards


# ---- era OCR: text matching (rewardScannerMatch.ts:11-17, 563-704) -----------------

ERA_TOKENS = (("LITH", "lith"), ("MESO", "meso"), ("NEO", "neo"), ("AXI", "axi"), ("REQUIEM", "requiem"))


def normalize_ocr_phrase(s: str) -> str:
    s = re.sub(r"[^A-Z0-9\s]+", " ", str(s or "").upper())
    return re.sub(r"\s+", " ", s).strip()


def normalize_for_ocr(s: str) -> str:
    s = str(s or "").upper()
    s = re.sub(r"[1|!]", "I", s).replace("0", "O").replace("5", "S")
    return re.sub(r"[^A-Z]", "", s).strip()


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def match_era_token(word: str) -> tuple[str, float] | None:
    for text, tok in ERA_TOKENS:
        if word == text:
            return tok, 1.0
    if len(word) < 3:
        return None
    for text, tok in ERA_TOKENS:
        if text.startswith(word) or word.startswith(text):
            return tok, 0.82
    for text, tok in ERA_TOKENS:
        if levenshtein(word, text) <= 1:
            return tok, 0.76
    return None


def distinct_era_mentions(normalized: str) -> set[str]:
    found: set[str] = set()
    for raw in normalized.split(" "):
        w = normalize_for_ocr(raw)
        if w == "OMNIA":
            found.add("omnia")
            continue
        hit = match_era_token(w)
        if hit:
            found.add(hit[0])
    return found


def era_from_text(text: str) -> tuple[str | None, float]:
    normalized = normalize_ocr_phrase(text)
    if not normalized:
        return None, 0.0
    best: tuple[str | None, float] = (None, 0.0)
    for w in (normalize_for_ocr(x) for x in normalized.split(" ")):
        if len(w) < 2:
            continue
        hit = match_era_token(w)
        if not hit:
            continue
        if hit[1] >= 1:
            return hit[0], 1.0
        if hit[1] > best[1]:
            best = hit
    return best


def era_from_label(text: str) -> tuple[str | None, float]:
    """Filter tab: "All"/"Omnia" -> omnia (the selected tab reads "All" on Omnia screens)."""
    normalized = normalize_ocr_phrase(text)
    if not normalized:
        return None, 0.0
    best: tuple[str | None, float] = (None, 0.0)
    for raw in normalized.split(" "):
        w = normalize_for_ocr(raw)
        if w in ("ALL", "OMNIA"):
            return "omnia", 1.0
        if len(w) == 3 and w.replace("I", "L") == "ALL" and best[1] < 0.95:
            best = ("omnia", 0.95)
    hit = era_from_text(normalized)
    return hit if hit[1] > best[1] else best


def era_from_band(text: str) -> tuple[str | None, float]:
    normalized = normalize_ocr_phrase(text)
    if not normalized or len(distinct_era_mentions(normalized)) > 1:
        return None, 0.0
    return era_from_text(normalized)


def era_from_tile(text: str) -> tuple[str | None, float]:
    normalized = normalize_ocr_phrase(text)
    if not normalized or len(distinct_era_mentions(normalized)) > 1:
        return None, 0.0
    era, conf = era_from_text(normalized)
    if not era or conf <= 0:
        return era, conf
    if normalized.startswith(("LITH ", "MESO ", "NEO ", "AXI ", "REQUIEM ")):
        conf += 0.08
    if " RELIC" in normalized:
        conf += 0.06
    return era, min(1.0, conf)


# ---- era OCR: crops (rewardScannerSupport.ts:27-53, rewardScannerImage.ts:183-209) ---

LABEL_RECT = (0.02, 0.085, 0.19, 0.058)
# Literal x values as in rewardScannerSupport.ts (0.02 + 0.18*i is 0.19999… in floats).
TILE_RECTS = [(x, 0.5, 0.18, 0.42) for x in (0.02, 0.20, 0.38, 0.56, 0.74)]
BANDS = [(0.12, 0.12), (0.16, 0.13), (0.20, 0.14)]
REFERENCE_ASPECT = 16 / 9


def canvas_scale_y(w: int, h: int) -> float:
    s = (w / REFERENCE_ASPECT) / h
    return s if s < 1 else 1.0


def crop_rect(w: int, h: int, rect: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """cropRect: plain fractions of the full frame -> (x, y, w, h) pixels."""
    x, y, rw, rh = rect
    x = min(max(x, 0.0), 0.98)
    y = min(max(y, 0.0), 0.98)
    rw = min(max(rw, 0.02), 1 - x)
    rh = min(max(rh, 0.02), 1 - y)
    px, py = int(w * x), int(h * y)
    return px, py, min(max(24, int(w * rw)), w - px), min(max(24, int(h * rh)), h - py)


def crop_canvas_rect(w: int, h: int, rect: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    """The same rect on a 16:9 canvas centred in a taller frame (Deck 16:10)."""
    x, y, rw, rh = rect
    s = canvas_scale_y(w, h)
    px = int(w * x)
    py = int(h * (0.5 + (y - 0.5) * s))
    return px, py, min(max(24, int(w * rw)), w - px), min(max(24, int(h * rh * s)), h - py)


def crop_band(w: int, h: int, top: float, height: float) -> tuple[int, int, int, int]:
    s = canvas_scale_y(w, h)
    py = int(h * (0.5 + (top - 0.5) * s))
    return 0, py, w, min(max(18, int(h * height * s)), h - py)


# Only reads from WFHelper's own label rect may override the EE.log mission tier or
# set the 25-minute OCR cache. The 16:9-canvas box is an uncalibrated guess for the
# Deck's 16:10 frame: its reads count like any other OCR read until a real Deck
# screenshot shows which box holds the label.
TRUSTED_LABEL_IDS = frozenset({"filter-label"})


def label_boxes(w: int, h: int) -> list[tuple[str, tuple[int, int, int, int]]]:
    out = [("filter-label", crop_rect(w, h, LABEL_RECT))]
    canvas = crop_canvas_rect(w, h, LABEL_RECT)
    if canvas != out[0][1]:
        out.append(("filter-label-canvas", canvas))
    return out


def enhanced(rgb: np.ndarray) -> np.ndarray:
    """Upscale 2x (Lanczos), luminance, LUT (lum-72)/(214-72) ^ 0.9, grey RGB."""
    h, w = rgb.shape[:2]
    nw, nh = min(w * 2, 4096), min(h * 2, 4096)
    up = np.asarray(Image.fromarray(rgb).resize((nw, nh), Image.LANCZOS), dtype=np.float32)
    lum = (114 * up[:, :, 2] + 587 * up[:, :, 1] + 299 * up[:, :, 0] + 500) / 1000
    out = np.round(255 * np.clip((lum - 72) / (214 - 72), 0, 1) ** 0.9).astype(np.uint8)
    return np.repeat(out[:, :, None], 3, axis=2)


@dataclass
class EraRead:
    era: str | None = None
    confidence: float = 0.0
    candidate: str | None = None
    text: str = ""
    variant: str | None = None
    elapsed_ms: int = 0
    attempts: int = 0

    def to_json(self) -> dict:
        return {"era": self.era, "confidence": round(self.confidence, 3), "candidateId": self.candidate,
                "textPreview": self.text[:240], "ocrVariant": self.variant, "elapsedMs": self.elapsed_ms}


def detect_era(img: Image.Image | np.ndarray, read: Callable[[np.ndarray], str], timeout_s: float = ERA_DETECTION_TIMEOUT_S,
               label_only: bool = False, clock: Callable[[], float] = time.monotonic,
               self_signatures: set[str] | None = None, t0: float | None = None) -> EraRead:
    """Label (raw, enhanced) -> tiles 1-5 -> header bands; stop at >= 0.99; best wins.

    read(rgb crop) -> recognised text (""/None for no read). Each attempt after the
    first needs >= 0.9 s of budget left, like WFHelper's MIN_ERA_OCR_ATTEMPT_MS.
    t0: when the budget started (before the capture, like WFHelper starting its
    clock before captureScreenFast); defaults to now. A single read cannot be
    interrupted, so one slow read can still overrun the budget by its own length.
    """
    t0 = clock() if t0 is None else t0
    rgb = np.asarray(img.convert("RGB")) if isinstance(img, Image.Image) else np.asarray(img)[:, :, :3]
    h, w = rgb.shape[:2]
    best = EraRead()

    def budget_ok() -> bool:
        left = timeout_s - (clock() - t0)
        return left > 0 and (best.attempts == 0 or left >= MIN_ERA_OCR_ATTEMPT_S)

    def attempt(cid: str, box, matcher) -> bool:
        x, y, bw, bh = box
        crop = np.ascontiguousarray(rgb[y:y + bh, x:x + bw])
        for variant in ("raw", "enhanced"):
            if not budget_ok():
                return True
            best.attempts += 1
            try:
                text = read(crop if variant == "raw" else enhanced(crop)) or ""
            except Exception as e:  # noqa: BLE001 - a failed read is no read
                _log.debug("era read %s failed: %s", cid, e)
                text = ""
            text = re.sub(r"\s+", " ", text).strip()
            if self_signatures and _is_self_read(text, self_signatures):
                continue
            era, conf = matcher(text)
            if era and conf > best.confidence:
                best.era, best.confidence, best.candidate, best.text, best.variant = era, conf, cid, text, variant
            if best.confidence >= 0.99:
                return True
        return False

    done = False
    for cid, box in label_boxes(w, h):
        if attempt(cid, box, era_from_label):
            done = True
            break
    if not done and not label_only and best.confidence < ERA_CONFIDENT:
        for i, rect in enumerate(TILE_RECTS, 1):
            if attempt(f"tile-slot-{i}", crop_rect(w, h, rect), era_from_tile):
                done = True
                break
    if not done and not label_only and best.confidence < ERA_CONFIDENT:
        for top, height in BANDS:
            if attempt("header-band", crop_band(w, h, top, height), era_from_band):
                break
    best.elapsed_ms = int((clock() - t0) * 1000)
    return best


def _is_self_read(text: str, signatures: set[str]) -> bool:
    norm = normalize_ocr_phrase(text)
    return any(sig in norm for sig in signatures)


def row_signature(label: str) -> str | None:
    sig = normalize_ocr_phrase(label)
    return sig if len(sig.split(" ")) >= 3 else None


# ---- panel rendering ---------------------------------------------------------------

RARITY_COLOR = {"Rare": draw.GOLD, "Uncommon": (190, 200, 215, 255), "Common": (205, 150, 110, 255)}
CARD_BG = (24, 30, 48, 240)
CARD_BORDER = (70, 84, 120, 255)
BEST_TINT = (48, 42, 24, 245)


def _pill(d: ImageDraw.ImageDraw, x: int, y: int, text: str, color: tuple, size: int = 10) -> int:
    f = draw.font(size, True)
    tw = int(f.getlength(text))
    w, h = tw + 10, size + 7
    d.rounded_rectangle((x, y, x + w, y + h), h // 2, fill=(color[0], color[1], color[2], 40), outline=color)
    d.text((x + 5, y + 2), text, font=f, fill=color)
    return w


def _fmt(v: float | None, unit: str) -> str:
    if v is None:
        return "–"
    return f"{v:.1f}{unit}"


def render_panel(payload: dict, show_rewards: bool = False, max_height: int = PANEL_MAX_H,
                 width: int = PANEL_W) -> Image.Image:
    """The planner panel at design scale (1280x800 pixels)."""
    pad = 10
    inner = width - 2 * pad
    rows: list[dict] = payload.get("rows") or []
    state = payload.get("state", "rows")
    header_h = 50
    card_h = 44
    reward_h = 17
    gap = 5
    footer_h = 24

    def card_height(r: dict) -> int:
        return card_h + (len(r.get("rewards") or []) * reward_h + 4 if show_rewards else 0)

    body_lines: list[tuple[str, tuple]] = []
    if state == "scanning":
        body_lines = [("Detecting relic era and ranking", draw.TEXT), ("your owned relics…", draw.TEXT)]
    elif not rows:
        body_lines = [(t, draw.MUTED) for t in _wrap(payload.get("message") or "No owned relics to rank.", inner, 14)]
    elif int(payload.get("pricesMissing") or 0) >= PRICES_MISSING_NOTE:
        body_lines = [(f"Prices loading ({int(payload['pricesMissing'])} missing, counted as 0)", draw.WARN)]

    shown: list[dict] = []
    body_h = len(body_lines) * 20 + 12 if body_lines else 0
    used = header_h + body_h
    for r in rows:
        ch = card_height(r) + gap
        if used + ch + footer_h + pad > max_height:
            break
        shown.append(r)
        used += ch
    more = len(rows) - len(shown)
    height = used + footer_h + pad
    height = min(max(height, 110), max_height)

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, width - 1, height - 1), 10, fill=draw.BG, outline=draw.BORDER)

    # header
    d.text((pad, 8), "Relic planner", font=draw.font(17, True), fill=draw.ACCENT)
    era = payload.get("era")
    era_txt = (era or "all eras").upper()
    ef = draw.font(12, True)
    ew = int(ef.getlength(era_txt)) + 10
    _pill(d, width - pad - ew, 10, era_txt, draw.GOLD if era else draw.MUTED, 12)
    sub = []
    if payload.get("squadSize"):
        sub.append(f"Squad {payload['squadSize']}")
    if payload.get("totalOwnedCount") is not None:
        sub.append(f"{payload['totalOwnedCount']} relics owned")
    if payload.get("eraSource"):
        sub.append({"eelog": "era from mission", "ocr": "era from screen", "cache": "era cached",
                    "hint": "era from filters", "manual": "era picked in QAM"}.get(payload["eraSource"], payload["eraSource"]))
    d.text((pad, 30), draw.ellipsize("  ·  ".join(sub), inner, 12), font=draw.font(12), fill=draw.MUTED)

    y = header_h
    if body_lines:
        y += 6
        for t, c in body_lines:
            d.text((pad, y), draw.ellipsize(t, inner, 14), font=draw.font(14), fill=c)
            y += 20
        y += 6
    best_plat = max((r["plat"] for r in rows if r.get("plat") is not None), default=None)
    best_i = next((j for j, r in enumerate(shown) if best_plat is not None and r.get("plat") == best_plat), -1)
    for i, r in enumerate(shown):
        ch = card_height(r)
        is_best = i == best_i
        d.rounded_rectangle((pad, y, width - pad - 1, y + ch), 6,
                            fill=BEST_TINT if is_best else CARD_BG,
                            outline=draw.GOLD if is_best else CARD_BORDER, width=2 if is_best else 1)
        cx = pad + 8
        # line 1: "3x Axi A16 Radiant" + vault pill
        pill_txt = "VAULTED" if r.get("vaulted") else "UNVAULTED"
        pill_col = draw.BAD if r.get("vaulted") else (96, 165, 250, 255)
        pf = draw.font(10, True)
        pw = int(pf.getlength(pill_txt)) + 10
        title_w = inner - 16 - pw - 6
        cnt = f"{r.get('count', 0)}x "
        d.text((cx, y + 3), cnt, font=draw.font(15, True), fill=draw.MUTED)
        cw = draw.text_width(cnt, 15, True)
        title = draw.ellipsize(f"{r['name']} {r.get('qualityLabel', '')}", title_w - cw, 15, True)
        d.text((cx + cw, y + 3), title, font=draw.font(15, True), fill=draw.TEXT)
        _pill(d, width - pad - 8 - pw, y + 5, pill_txt, pill_col)
        # line 2: EV plat / ducats, rank
        ly = y + 23
        d.text((cx, ly), "EV", font=draw.font(12), fill=draw.MUTED)
        x2 = cx + 24
        ptxt = _fmt(r.get("plat"), "p")
        d.text((x2, ly - 1), ptxt, font=draw.font(14, True), fill=draw.PLAT)
        x2 += draw.text_width(ptxt, 14, True) + 14
        dtxt = _fmt(r.get("ducat"), "d")
        d.text((x2, ly - 1), dtxt, font=draw.font(14, True), fill=draw.DUCAT)
        rank = f"#{i + 1}"
        rank_x = width - pad - 8 - draw.text_width(rank, 12)
        d.text((rank_x, ly), rank, font=draw.font(12), fill=draw.MUTED)
        if r.get("masteryParts"):
            mf = draw.font(10, True)
            mw = int(mf.getlength("MASTERY")) + 10
            _pill(d, rank_x - 8 - mw, ly + 1, "MASTERY", draw.ACCENT)
        if show_rewards and r.get("rewards"):
            ry = y + card_h - 2
            for rw in r["rewards"]:
                chance = f"{rw.get('chance', 0):.0f}%" if rw.get("chance", 0) >= 10 else f"{rw.get('chance', 0):.1f}%"
                owned = rw.get("owned")
                if rw.get("name") in (r.get("masteryParts") or ()):
                    own_txt, own_col = "mastery", draw.ACCENT
                else:
                    own_txt = "need" if rw.get("needed") else ("?" if owned is None else f"own {owned}")
                    own_col = draw.GOOD if rw.get("needed") else draw.MUTED
                price = rw.get("platinum")
                ptx = f"{price:.0f}p" if price is not None else ""
                right_w = 118
                name = draw.ellipsize(rw.get("name", ""), inner - 16 - right_w, 12)
                d.text((cx, ry), name, font=draw.font(12), fill=RARITY_COLOR.get(rw.get("rarity"), draw.TEXT))
                rx = width - pad - 8
                d.text((rx - draw.text_width(own_txt, 11), ry + 1), own_txt, font=draw.font(11), fill=own_col)
                rx -= 46
                d.text((rx - draw.text_width(chance, 11), ry + 1), chance, font=draw.font(11), fill=draw.MUTED)
                rx -= 40
                if ptx:
                    d.text((rx - draw.text_width(ptx, 11, True), ry + 1), ptx, font=draw.font(11, True), fill=draw.PLAT)
                ry += reward_h
        y += ch + gap

    # footer
    fy = height - footer_h
    d.line((pad, fy, width - pad, fy), fill=(60, 70, 100, 255))
    foot = f"+{more} more  ·  " if more > 0 else ""
    foot += "Filters & squad: QAM › Relics"
    d.text((pad, fy + 5), draw.ellipsize(foot, inner, 12), font=draw.font(12), fill=draw.MUTED)
    return img


def _wrap(text: str, width: int, size: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        cand = f"{line} {word}".strip()
        if draw.text_width(cand, size) <= width or not line:
            line = cand
        else:
            out.append(line)
            line = word
    if line:
        out.append(line)
    return out


def scale_to_overlay(img: Image.Image, overlay_size: tuple[int, int]) -> tuple[Image.Image, int, int]:
    """Design-space panel -> (image, x, y) on an overlay of overlay_size."""
    ow, oh = overlay_size
    s = min(ow / DESIGN_W, oh / DESIGN_H) or 1.0
    if abs(s - 1.0) > 0.01:
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)
    x = ow - img.width - int(PANEL_MARGIN * s)
    y = int(PANEL_TOP * s)
    return img, x, y


# ---- controller --------------------------------------------------------------------

_OCR_POOL: ThreadPoolExecutor | None = None
_OCR_POOL_LOCK = threading.Lock()


def _ocr_pool() -> ThreadPoolExecutor:
    """One thread for era reads, so an abandoned slow read never runs two at once."""
    global _OCR_POOL
    with _OCR_POOL_LOCK:
        if _OCR_POOL is None:
            _OCR_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wfh-planner-ocr")
        return _OCR_POOL


@dataclass
class PlannerState:
    log_tier: str | None = None          # from activeMissionTag
    ocr_tier: str | None = None          # OCR filter-label era, cached 25 min
    ocr_tier_at: float = 0.0
    picker_open: bool = False
    last_pattern_at: float = -1e9
    last_open_at: float = -1e9
    last_close_at: float = -1e9
    last_trigger_at: float = -1e9
    dismissed_until: float = -1e9
    pending: bool = False
    token: int = 0
    signatures: set[str] = field(default_factory=set)


class RelicPlanner:
    def __init__(self, app, clock: Callable[[], float] = time.monotonic,
                 grab: Callable[[], Image.Image] | None = None,
                 read_text: Callable[[np.ndarray], str] | None = None,
                 schedule: Callable[[float, Callable[[], None]], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.app = app
        self.clock = clock
        self.grab = grab or capture.grab
        self._read_text = read_text
        self._schedule = schedule or self._timer
        self.sleep = sleep
        self.s = PlannerState()
        self._lock = threading.RLock()
        self._cache: tuple[str, float, list[dict], int, int] | None = None
        self._sent_rows: tuple[int, str | None, str | None] | None = None  # (token, era, eraSource)
        self._last_toast: tuple | None = None
        self.last: dict | None = None  # last payload sent, compact (QAM mirror)
        self.prices_missing = 0

    # -- plumbing --
    @staticmethod
    def _timer(delay: float, fn: Callable[[], None]) -> None:
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()

    def setting(self, key: str, default: Any = None) -> Any:
        return self.app.settings.get(f"relic_planner.{key}", default)

    def read_text(self, crop: np.ndarray) -> str:
        if self._read_text is not None:
            return self._read_text(crop)
        from .. import ocr

        res = ocr.read_strip(crop, self.app.models_dir)
        return res.text if res else ""

    def ocr_available(self) -> bool:
        if self._read_text is not None:
            return True
        try:
            from .. import ocr

            return ocr.available(self.app.models_dir)
        except Exception:  # noqa: BLE001
            return False

    # -- EE.log (tailer thread: must return fast) --
    def on_line(self, line: str) -> None:
        if "activeMissionTag" in line:
            m = MISSION_TAG.search(line)
            if m:
                self.set_mission_tag(m.group(1))
                return
        if MISSION_END.search(line):
            self.set_mission_tag("EndOfMission")
            return
        is_open = any(p.search(line) for p in PICKER_OPEN)
        is_close = not is_open and PICKER_CLOSE.search(line) is not None
        if not (is_open or is_close):
            return
        # While the Proton real-time stream is live, picker lines from the file are late echoes.
        try:
            if self.app.eelog.source == FILE and self.app.eelog.realtime_active():
                return
        except AttributeError:
            pass
        if is_open:
            self.picker_pattern()
        else:
            self.picker_close_line()

    def set_mission_tag(self, tag: str) -> None:
        era = rv.tag_era(tag)
        with self._lock:
            if era != self.s.log_tier:
                _log.info("mission tier %s (tag %s)", era or "cleared", tag)
            self.s.log_tier = era

    def picker_pattern(self) -> None:
        now = self.clock()
        with self._lock:
            self.s.last_pattern_at = now
            if now - self.s.last_open_at < PICKER_COOLDOWN_S or self.s.pending:
                return
            self.s.pending = True
        self._schedule(TRIGGER_DELAY_S, self._picker_opened)

    def _picker_opened(self) -> None:
        with self._lock:
            self.s.pending = False
            self.s.last_open_at = self.clock()
            self.s.picker_open = True
        self.trigger("eelog")

    def picker_close_line(self) -> None:
        now = self.clock()
        with self._lock:
            if not self.s.picker_open:
                return
            if now - self.s.last_close_at < CLOSE_COOLDOWN_S or now - self.s.last_open_at < ENTRY_WINDOW_S:
                return
            self.s.last_close_at = now
            self.s.picker_open = False
        self.close()

    # -- controller --
    def enabled(self) -> bool:
        return bool(self.setting("enabled", True))

    def trigger(self, source: str = "eelog", era_override: str | None = None) -> int | None:
        """onRelicSelectionTrigger: show the scanning panel and start the refinement."""
        now = self.clock()
        with self._lock:
            if source == "eelog":
                if self.s.dismissed_until > now:
                    return None
                if now - self.s.last_trigger_at < MIN_EELOG_TRIGGER_GAP_S:
                    return None
                if not self.setting("auto_trigger", True):
                    return None
            if not self.enabled():
                return None
            self.s.last_trigger_at = now
            self.s.token += 1
            token = self.s.token
            baseline = self.s.dismissed_until
        if era_override:
            # A manual era is final: rank once, no detection pass.
            era = None if era_override == "all" else era_override
            try:
                self._send_rows(token, source, era, "manual", EraRead(era, 1.0))
            except Exception:  # noqa: BLE001
                _log.exception("planner manual rows failed")
                self._send({"state": "error", "source": source, "era": None, "rows": [],
                            "message": "Relic planner failed; see the plugin log."}, AUTO_HIDE_FAILURE_S, token)
            return token
        self._send({"state": "scanning", "source": source, "era": None, "rows": [],
                    "squadSize": self.squad_size()}, AUTO_HIDE_DETECTING_S, token, toast=False)
        with self._lock:
            log_tier = self.s.log_tier
        cached = self.cached_era()
        if cached:
            try:
                self._send_rows(token, source, cached, "eelog" if cached == log_tier else "cache", EraRead(cached, 1.0))
            except Exception:  # noqa: BLE001
                _log.exception("planner fallback rows failed")
        self._schedule(ERA_START_DELAY_S, lambda: self.app.submit(self.run_refinement, token, source, baseline, None))
        return token

    def cached_era(self) -> str | None:
        with self._lock:
            if self.s.log_tier:
                return self.s.log_tier
            if self.s.ocr_tier and self.clock() - self.s.ocr_tier_at < MISSION_TIER_CACHE_TTL_S:
                return self.s.ocr_tier
        return None

    def _filters(self) -> dict | None:
        f = self.setting("filters")
        return f if isinstance(f, dict) else None

    def squad_size(self) -> int:
        f = self._filters()
        if f and f.get("squadSize"):
            return int(f["squadSize"])
        try:
            return min(4, max(1, int(self.setting("squad_size", rv.OVERLAY_SQUAD_DEFAULT))))
        except (TypeError, ValueError):
            return rv.OVERLAY_SQUAD_DEFAULT

    def decide_era(self, token: int, era_override: str | None = None) -> tuple[str | None, str | None, EraRead]:
        """runRefinement steps 1-6: (effective era, source, read)."""
        if era_override:
            era = None if era_override == "all" else era_override
            return era, "manual", EraRead(era, 1.0)
        f = self._filters()
        hint = rv.normalize_era(f.get("tierFilter")) if f and f.get("tierFilter") else None
        with self._lock:
            log_tier = self.s.log_tier
            ocr_cached = self.s.ocr_tier if self.clock() - self.s.ocr_tier_at < MISSION_TIER_CACHE_TTL_S else None
            sigs = set(self.s.signatures)
        use_ocr = bool(self.setting("ocr_fallback", True)) and self.ocr_available()
        if log_tier:
            era, src, read = log_tier, "eelog", EraRead(log_tier, 1.0)
            if use_ocr:
                label = self._ocr(ERA_LABEL_TIMEOUT_S, True, sigs)
                if (label.era and label.confidence >= ERA_CONFIDENT and label.candidate in TRUSTED_LABEL_IDS
                        and label.era != era and label.era != "requiem"):
                    _log.info("filter label %s overrides mission tier %s", label.era, era)
                    era, src, read = label.era, "ocr", label
                    self._cache_ocr_era(label.era)
            return era, src, read
        if ocr_cached:
            return ocr_cached, "cache", EraRead(ocr_cached, 1.0)
        if hint:
            return hint, "hint", EraRead(hint, 0.75)
        if not use_ocr:
            return None, None, EraRead()
        t0 = self.clock()
        read = self._ocr(ERA_DETECTION_TIMEOUT_S, False, sigs)
        if not read.era:
            retry = ERA_TOTAL_TIMEOUT_S - (self.clock() - t0) - ERA_RETRY_DELAY_S
            if retry >= ERA_MIN_RETRY_S and token == self.s.token:
                self.sleep(ERA_RETRY_DELAY_S)
                read = self._ocr(min(ERA_DETECTION_TIMEOUT_S, retry), False, sigs)
        if read.era and read.confidence >= ERA_CONFIDENT and read.candidate in TRUSTED_LABEL_IDS:
            self._cache_ocr_era(read.era)
        if read.era and read.confidence >= ERA_CONFIDENT:
            return read.era, "ocr", read
        return hint, ("hint" if hint else None), read

    def _cache_ocr_era(self, era: str) -> None:
        with self._lock:
            self.s.ocr_tier, self.s.ocr_tier_at = era, self.clock()

    def _ocr(self, timeout: float, label_only: bool, sigs: set[str]) -> EraRead:
        """Capture + era OCR inside one budget: the clock starts before the capture."""
        t0 = self.clock()
        try:
            img = self.grab()
        except Exception as e:  # noqa: BLE001 - capture unavailable is "no read"
            _log.info("planner capture failed: %s", e)
            return EraRead(elapsed_ms=int((self.clock() - t0) * 1000))
        if self.clock() - t0 >= timeout:
            _log.info("planner capture used the whole %.1f s era budget", timeout)
            return EraRead(elapsed_ms=int((self.clock() - t0) * 1000))
        if bool(self.app.settings.get("debug_save_captures", False)):
            try:
                import os

                d = os.path.join(self.app.runtime_dir, "captures")
                os.makedirs(d, exist_ok=True)
                img.save(os.path.join(d, f"planner-{int(time.time())}.png"))
            except Exception:  # noqa: BLE001
                pass
        deadline = t0 + timeout
        return detect_era(img, lambda crop: self._bounded_read(crop, deadline), timeout, label_only,
                          self.clock, sigs, t0=t0)

    def _bounded_read(self, crop: np.ndarray, deadline: float) -> str:
        """One OCR read limited to the time left in the budget (WFHelper races each
        read against min(ocrTimeout, remaining)). A read that overruns is abandoned
        (it finishes on the OCR thread) and counts as no read."""
        if self._read_text is not None:  # tests: synchronous fake reads
            return self._read_text(crop)
        left = deadline - self.clock()
        if left <= 0:
            return ""
        fut = _ocr_pool().submit(self.read_text, crop)
        try:
            return fut.result(timeout=left)
        except FutureTimeout:
            _log.info("planner era read overran the budget (%.2f s left)", left)
            return ""

    def run_refinement(self, token: int, source: str, baseline: float, era_override: str | None = None) -> None:
        if token != self.s.token:
            return
        try:
            era, era_src, read = self.decide_era(token, era_override)
            if token != self.s.token or self.s.dismissed_until > baseline:
                return
            with self._lock:
                sent = self._sent_rows
            if sent is not None and sent[0] == token and sent[1] == era and sent[2] == era_src:
                # trigger() already showed this era from the cache/log tier and the
                # label cross-check did not change it: no second render/event/toast.
                return
            self._send_rows(token, source, era, era_src, read)
        except Exception:  # noqa: BLE001
            _log.exception("relic planner refinement failed")
            if token == self.s.token:
                self._send({"state": "error", "source": source, "era": None, "rows": [],
                            "message": "Relic planner failed; see the plugin log."}, AUTO_HIDE_FAILURE_S, token)

    # -- recommendations --
    def recommendations(self, era: str | None) -> tuple[list[dict], int, bool]:
        """(rows, totalOwnedCount, has_inventory): owned relics of era ranked by EV.

        totalOwnedCount counts every owned relic of the era before the pushed
        filters, like buildRecommendations. self.prices_missing is the number of
        reward slugs without any price (cached or snapshot) in the last ranking."""
        from .. import inventory as inv_mod
        from ..data.relics import relics

        owned = inv_mod.owned_relics() if inv_mod.inventory() is not None else None
        if owned is None:
            return [], 0, False
        filters = self._filters()
        squad = self.squad_size()
        key = f"{era or 'all'}|{squad}|{rv.fingerprint(owned)}|{sorted((filters or {}).items(), key=str)!r}|{self.setting('show_rewards', False)}|{self.setting('prioritize_mastery', True)}"
        now = self.clock()
        with self._lock:
            if self._cache and self._cache[0] == key and now - self._cache[1] < RECOMMENDATION_CACHE_TTL_S:
                self.prices_missing = self._cache[4]
                return self._cache[2], self._cache[3], True
        valuer = rv.default_valuer()
        db = relics()
        if filters:
            rows = rv.build_rows(db, owned, squad=squad, era=era, quality_mode=filters.get("qualityMode", "owned"),
                                 pinned=filters.get("pinnedQualities"), valuer=valuer, fallback_unowned=True)
            total = sum(r["ownedTotal"] for r in rows)
            rows = rv.select_rows(rows, filters, valuer)
        else:
            rows = rv.rank_by_ev(rv.build_rows(db, owned, squad=squad, era=era, best_quality=True, valuer=valuer))
            total = sum(r["ownedTotal"] for r in rows)
        if self.setting("prioritize_mastery", True):
            rows = self._mastery_first(rows, inv_mod.inventory())
        rows = [rv.public(r) for r in rows]
        missing = len(valuer.missing)
        self.prices_missing = missing
        rv.save_price_snapshot()
        if valuer.missing and self.setting("prefetch_prices", True):
            self._prefetch(valuer.missing)
        with self._lock:
            self._cache = (key, now, rows, total, missing)
        return rows, total, True

    @staticmethod
    def _mastery_first(rows: list[dict], inv) -> list[dict]:
        """Relics holding a part of gear never crafted or mastered move to the
        top (stable, so EV or the pushed sort still orders each half)."""
        from ..data.items import items
        from .reward_enrich import MasteryNeeds

        try:
            needs = MasteryNeeds(items(), inv)
        except Exception as e:  # noqa: BLE001 - ranking must not fail on item data
            _log.warning("mastery ranking unavailable: %s", e)
            return rows
        for r in rows:
            relic = r.get("_relic")
            names_ = sorted({rw.item_name for rw in (relic.rewards if relic else []) if rw.item_name})
            r["masteryParts"] = [n for n in names_ if needs.needed(n)]
        return sorted(rows, key=lambda r: not r["masteryParts"])

    def _prefetch(self, slugs) -> None:
        try:
            from ..data import market

            market.prefetch(sorted(slugs))
        except Exception:  # noqa: BLE001
            _log.debug("price prefetch failed", exc_info=True)

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def _send_rows(self, token: int, source: str, era: str | None, era_src: str | None, read: EraRead) -> None:
        rows, total, has_inv = self.recommendations(era)
        if token != self.s.token:
            return
        payload = {"state": "rows", "source": source, "era": era, "eraSource": era_src, "rows": rows,
                   "totalOwnedCount": total, "squadSize": self.squad_size(), "hasInventory": has_inv,
                   "ocrUnavailable": not self.ocr_available(), "detection": read.to_json(),
                   "pricesMissing": self.prices_missing if rows else 0}
        if not rows:
            if not has_inv:
                payload["message"] = "No inventory yet. Sync it from QAM › Inventory."
            elif era:
                payload["message"] = f"No owned {rv.era_label(era)} relics to recommend."
            elif payload["ocrUnavailable"]:
                payload["message"] = "Could not tell the relic era: the OCR model is missing."
            else:
                payload["message"] = (f"Could not detect the relic era yet (OCR {read.elapsed_ms} ms, "
                                      f"confidence {read.confidence:.2f}).")
        ttl = self.rows_ttl(source) if rows else AUTO_HIDE_FAILURE_S
        with self._lock:
            self.s.signatures = {s for s in (row_signature(r["label"]) for r in rows[:40]) if s}
            self._sent_rows = (token, era, era_src)
        self._send(payload, ttl, token)

    def rows_ttl(self, source: str) -> float:
        """relic_planner.duration_s; a panel opened by the picker lines stays at least
        AUTO_HIDE_SUCCESS_S (WFHelper's OVERLAY_AUTO_HIDE_SUCCESS_MS) because the
        InitMapping close line hides it when the picker closes."""
        try:
            ttl = float(self.setting("duration_s", 20) or 20)
        except (TypeError, ValueError):
            ttl = 20.0
        return max(ttl, AUTO_HIDE_SUCCESS_S) if source == "eelog" else ttl

    @staticmethod
    def compact(payload: dict) -> dict:
        """Event/state mirror: no rewards, top EVENT_ROWS rows, plus the row count."""
        rows = payload.get("rows") or []
        out = {k: v for k, v in payload.items() if k != "rows"}
        out["count"] = len(rows)
        out["rows"] = [{k: v for k, v in r.items() if k not in ("rewards", "owned")} for r in rows[:EVENT_ROWS]]
        return out

    def _send(self, payload: dict, ttl: float, token: int, toast: bool = True) -> None:
        payload = {**payload, "token": token, "at": int(time.time() * 1000)}
        small = self.compact(payload)
        self.last = small
        self.app.emit("relics.planner", small)
        if not self.app.settings.get("overlays_enabled", True):
            return
        ov = self.app.overlays
        st = ov.status()
        if not st.get("connected") and not st.get("headless"):
            if toast and payload.get("state") == "rows":
                self._toast(payload)
            return
        img = render_panel(payload, bool(self.setting("show_rewards", False)),
                           int(self.setting("max_height", PANEL_MAX_H) or PANEL_MAX_H))
        img, x, y = scale_to_overlay(img, ov.size)
        ov.show(LAYER, img, x, y, ttl=ttl, z=10)

    def _toast(self, payload: dict) -> None:
        rows = payload.get("rows") or []
        key = (payload.get("token"), payload.get("era"), tuple((r["key"], r.get("quality")) for r in rows[:3]),
               payload.get("message"))
        with self._lock:
            if key == self._last_toast:
                return  # same result for the same trigger: one toast only
            self._last_toast = key
        era = rv.era_label(payload.get("era"))
        if not rows:
            self.app.toast("Relic planner", payload.get("message") or f"No owned {era} relics")
            return
        body = ", ".join(f"{r['name']} {rv.REFINEMENT_SHORT.get(r['quality'], '')} {_fmt(r.get('plat'), 'p')}"
                         for r in rows[:3])
        self.app.toast(f"Best {era} relics", body)

    def close(self) -> None:
        """Picker closed: hide, forget the OCR era (the log tier survives)."""
        with self._lock:
            self.s.ocr_tier, self.s.ocr_tier_at = None, 0.0
            self.s.token += 1
        self.app.overlays.clear(LAYER)
        self.app.emit("relics.planner", {"state": "closed", "rows": [], "at": int(time.time() * 1000)})

    def dismiss(self) -> None:
        """User hid the panel (QAM): hide and suppress EE.log reopen for 3 s."""
        with self._lock:
            self.s.dismissed_until = self.clock() + REOPEN_SUPPRESS_S
            self.s.token += 1
        self.app.overlays.clear(LAYER)

    def state(self) -> dict:
        with self._lock:
            return {"logTier": self.s.log_tier, "ocrTier": self.cached_era() if not self.s.log_tier else None,
                    "pickerOpen": self.s.picker_open, "squadSize": self.squad_size(),
                    "filtersPushed": self._filters() is not None, "last": self.last}


# ---- module wiring -----------------------------------------------------------------

_planner: RelicPlanner | None = None


def planner() -> RelicPlanner | None:
    return _planner


def setup(app) -> None:
    global _planner
    _planner = RelicPlanner(app)
    app.services["relic_planner"] = _planner
    app.eelog.subscribe(_planner.on_line)

    def on_settings(_data: dict) -> None:
        _planner.invalidate()
        if not app.settings.get("relic_planner.enabled", True):
            app.overlays.clear(LAYER)

    app.settings.on_change(on_settings)
    try:
        from .. import inventory as inv_mod

        inv_mod.on_update(lambda _inv: (_planner.invalidate(), app.submit(prefetch_owned_prices)))
    except Exception:  # noqa: BLE001
        _log.debug("inventory hook unavailable", exc_info=True)
    try:
        from .. import ocr

        if app.settings.get("relic_planner.ocr_fallback", True):
            ocr.warmup(app.models_dir)
    except Exception:  # noqa: BLE001
        pass


PREFETCH_MAX_AGE_S = 6 * 3600


def owned_reward_slugs() -> list[str]:
    from .. import inventory as inv_mod
    from ..data.relics import relics

    owned = inv_mod.owned_relics()
    db = relics()
    slugs: set[str] = set()
    for name in rv.owned_by_group(owned):
        relic = db.get(name)
        if relic:
            slugs.update(r.wfm_slug for r in relic.rewards if r.wfm_slug)
    return sorted(slugs)


def prefetch_owned_prices() -> int:
    """Queue prices for the rewards of owned relics that are missing or older than 6 h."""
    from ..app import get_app
    from ..data import market

    if not get_app().settings.get("relic_planner.prefetch_prices", True):
        return 0
    m = market.get()
    todo = [s for s in owned_reward_slugs() if not m.cached(s, None, max_age=PREFETCH_MAX_AGE_S)[0]]
    return market.prefetch(todo) if todo else 0


def _need() -> RelicPlanner:
    if _planner is None:
        raise RuntimeError("relic planner not initialised")
    return _planner


@rpc.method("relics.planner_state")
def _rpc_state() -> dict:
    return _need().state()


@rpc.method("relics.planner_show")
async def _rpc_show(era: str | None = None) -> dict:
    """Show the panel now (the QAM stand-in for WFHelper's manual trigger)."""
    from ..app import get_app

    p = _need()
    want = str(era or "auto").lower()
    override = "all" if want == "all" else rv.normalize_era(want)  # None = detect (auto)
    token = await get_app().run_blocking(p.trigger, "manual", override)
    return {"token": token, "enabled": p.enabled()}


@rpc.method("relics.planner_hide")
def _rpc_hide() -> dict:
    _need().dismiss()
    return {"ok": True}


@rpc.method("relics.prefetch_prices")
async def _rpc_prefetch() -> dict:
    from ..app import get_app

    n = await get_app().run_blocking(prefetch_owned_prices)
    return {"queued": n}
