"""End-of-arbitration summary: payload (arbiSummary.ts) and the Pillow panel.

WFHelper draws a 420x252 CSS-px window (renderer/arbi-overlay.*). The Deck LCD
is 1280x800, so the same 420x252 fits 1:1 (33% x 31%). Gamescope gives the
external overlay no input, so the close/drag/"Detailed Stats" buttons become a
footer hint; Dismiss and Open details live in the QAM (docs/port/arbitration.md §8).
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

from ..overlay import draw
from .arbi_parser import scenario_table, luck, type_label

PANEL_W, PANEL_H = 420, 252
BASE_W = 1280                       # Deck LCD width the panel is designed for
POSITIONS = ("top-center", "top-right", "top-left", "left-middle")
DEFAULT_POSITION = "top-center"     # top-right holds Warframe's minimap
MIN_SCALE, MAX_SCALE = 0.75, 1.25
HIGH_SATURATION_MIN_COUNT = 15

# WFHelper arbi-overlay.css colours
BG = (6, 10, 18, 236)
BORDER = (79, 158, 255, 90)
ORANGE = (245, 166, 35, 255)
ACCENT = (79, 158, 255, 255)
TEXT = (232, 237, 245, 255)
MUTED = (160, 174, 192, 255)
SEP = (79, 158, 255, 45)


def _finite_nonneg(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v) if math.isfinite(v) and v >= 0 else None


def build_summary_payload(run: dict) -> dict | None:
    """Overlay payload for an eligible completed live run, else None."""
    if run.get("source") != "live":
        return None
    if (run.get("rotations") or 0) < 2:
        return None
    if run.get("endReason") not in ("mission-end", "aborted"):
        return None
    stats = run.get("stats")
    if not stats:
        return None
    pct = sum(b.get("pct", 0) for b in stats.get("saturationBuckets") or []
              if b.get("minCount", 0) >= HIGH_SATURATION_MIN_COUNT)
    duration = _finite_nonneg(run.get("durationSec"))

    def per_min(value) -> float | None:
        if not duration or _finite_nonneg(value) is None:
            return None
        return _finite_nonneg(value / duration * 60)

    players: list[str] = []
    for name in run.get("players") or []:
        n = name.strip() if isinstance(name, str) else ""
        if n and n not in players:
            players.append(n)
    players = [p[:64] for p in players[:4]]
    drones = run.get("drones") or 0
    mean = stats.get("expectedVitusMean") or 0
    std = stats.get("expectedVitusStd") or 0
    return {
        "id": run.get("id"), "node": run.get("node"), "missionType": run.get("missionType"),
        "missionTypeRaw": run.get("missionTypeRaw"), "durationSec": run.get("durationSec"),
        "rotations": run.get("rotations"), "drones": drones, "totalEnemies": run.get("totalEnemies"),
        "expectedVitusMean": mean, "expectedVitusStd": std,
        "vitusActual": _finite_nonneg(run.get("vitusActual")),
        "expectedVitusPerMin": per_min(mean), "killsPerMin": per_min(run.get("totalEnemies")),
        "killsPerDrone": _finite_nonneg(run.get("totalEnemies", 0) / drones) if drones > 0 else None,
        "avgDroneIntervalSec": _finite_nonneg(stats.get("avgDroneIntervalSec")),
        "players": players, "squadSize": len(players) or None, "endReason": run.get("endReason"),
        "pctTimeAt15Plus": math.floor(pct * 10 + 0.5) / 10,
        "scenarios": scenario_table(mean, std),
    }


def format_duration(sec) -> str:
    s = int(max(0, sec or 0))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def _fmt(v, dp: int = 0, suffix: str = "") -> str:
    if v is None or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        return "?"
    return f"{v:,.{dp}f}{suffix}"


def summary_toast(p: dict) -> tuple[str, str]:
    body = (f"{p['node']}: {p['rotations']} rot, {p['drones']} drones, {p['totalEnemies']} kills, "
            f"~{p['expectedVitusMean']:.0f} Vitus expected ({format_duration(p['durationSec'])})")
    return "Arbitration complete", body


END_REASON_LABEL = {"mission-end": "Mission complete", "aborted": "Aborted"}


def summary_cells(p: dict, fields: dict | None = None) -> list[tuple[str, str, tuple]]:
    """KPI grid cells (value, label, colour). WFHelper shows Expected Vitus, Drone Kills, Total Kills
    and Time at 15+ Enemies; Actual Vitus, the per-minute rates, Kills/Drone and the average drone
    interval are hidden by default (overlayLayout.ts hiddenByDefault) and come with fields["extra_stats"]."""
    f = fields or {}
    mean, std = p.get("expectedVitusMean") or 0, p.get("expectedVitusStd") or 0
    cells = [
        (f"{mean:.1f} ±{std:.1f}", "Expected Vitus", ACCENT),
        (_fmt(p.get("drones")), "Drone Kills", TEXT),
        (_fmt(p.get("totalEnemies")), "Total Kills", TEXT),
        (_fmt(p.get("pctTimeAt15Plus"), 1, "%"), "Time at 15+ Enemies", TEXT),
    ]
    if f.get("extra_stats"):
        cells += [
            (_fmt(p.get("vitusActual")), "Actual Vitus", TEXT),
            (_fmt(p.get("expectedVitusPerMin"), 2), "Exp. Vitus / Min", TEXT),
            (_fmt(p.get("killsPerMin"), 1), "Enemies / Min", TEXT),
            (_fmt(p.get("killsPerDrone"), 1), "Kills / Drone", TEXT),
            (_fmt(p.get("avgDroneIntervalSec"), 1, "s"), "Avg Drone Interval", TEXT),
        ]
    return cells


def render_summary(p: dict, scale: float = 1.0, fields: dict | None = None) -> Image.Image:
    """The 420-px-wide panel (times ``scale``), drawn directly at the target size.

    fields: optional lines WFHelper hides by default: extra_stats (KPI cells), squad, end_reason,
    plus the plugin's vitus_range (likely Vitus range, or the luck of the actual count)."""
    f = fields or {}
    k = max(0.5, float(scale or 1.0))
    W = round(PANEL_W * k)

    def s(v: float) -> int:
        return int(round(v * k))

    cells = summary_cells(p, f)
    grid_rows = (len(cells) + 2) // 3
    y_grid = 78
    y_after = y_grid + grid_rows * 48 + 2           # separator under the KPI grid
    lines = int(bool(f.get("vitus_range"))) + int(bool(f.get("squad"))) + int(bool(f.get("end_reason")))
    H_design = max(PANEL_H if lines >= 2 or grid_rows > 2 else 0, y_after + 8 + lines * 20 + 30)
    H = s(H_design)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, W - 1, H - 1), s(10), fill=BG, outline=BORDER, width=max(1, s(1.5)))
    pad = s(14)
    inner = W - pad * 2

    def text(x, y, t, size, color, bold=False, anchor="la", max_w=None):
        fs = s(size)
        if max_w is not None:
            t = draw.ellipsize(t, max_w, fs, bold)
        d.text((x, y), t, font=draw.font(fs, bold), fill=color, anchor=anchor)
        return draw.text_width(t, fs, bold)

    # header: diamond + kicker, type badge on the right
    y = s(12)
    cx, cy, r = pad + s(5), y + s(7), s(5)
    d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)], fill=ORANGE)
    text(pad + s(15), y, "ARBITRATION COMPLETE", 12, ORANGE, bold=True)
    tl = type_label(p.get("missionType"), p.get("missionTypeRaw")).upper()
    if p.get("endReason") == "aborted":  # a partial run must not read as a finished one
        tl += " · ABORTED"
    bw = draw.text_width(tl, s(11), True) + s(14)
    d.rounded_rectangle((W - pad - bw, y - s(1), W - pad, y + s(17)), s(9), outline=ACCENT, width=max(1, s(1)))
    text(W - pad - bw // 2, y + s(8), tl, 11, ACCENT, bold=True, anchor="mm")

    # node + duration / rotations
    y = s(32)
    right = format_duration(p.get("durationSec"))
    rot = f"{p.get('rotations', 0)} rotations"
    rw = max(draw.text_width(right, s(14), True), draw.text_width(rot, s(12)))
    text(pad, y, p.get("node") or "Arbitration", 22, TEXT, bold=True, max_w=inner - rw - s(10))
    text(W - pad, y + s(2), right, 14, TEXT, bold=True, anchor="ra")
    text(W - pad, y + s(20), rot, 12, MUTED, anchor="ra")
    d.line((pad, s(68), W - pad, s(68)), fill=SEP, width=max(1, s(1)))

    # KPI grid: 3 columns, value over label
    col_w = inner / 3
    for i, (val, label, color) in enumerate(cells):
        cx0 = pad + int(col_w * (i % 3))
        cy0 = s(y_grid) + (i // 3) * s(48)
        text(cx0, cy0, val, 20, color, bold=True, max_w=int(col_w) - s(6))
        text(cx0, cy0 + s(25), label.upper(), 11, MUTED, max_w=int(col_w) - s(6))
    d.line((pad, s(y_after), W - pad, s(y_after)), fill=SEP, width=max(1, s(1)))

    y = s(y_after + 8)
    mean, std = p.get("expectedVitusMean") or 0, p.get("expectedVitusStd") or 0
    if f.get("vitus_range"):
        actual = p.get("vitusActual")
        lk = luck(actual, mean, std) if actual is not None else None
        if lk:
            col = tuple(int(lk["color"][i:i + 2], 16) for i in (1, 3, 5)) + (255,)
            w0 = text(pad, y, f"Vitus {actual:.0f}: ", 13, TEXT, bold=True)
            text(pad + w0, y, lk["text"], 13, col, bold=True, max_w=inner - w0)
        else:
            sc = {r["key"]: r["total"] for r in p.get("scenarios") or scenario_table(mean, std)}
            w0 = text(pad, y, "Likely ", 13, MUTED)
            w1 = text(pad + w0, y, f"{sc['belowAvg']}–{sc['aboveAvg']} Vitus", 13, TEXT, bold=True)
            text(pad + w0 + w1, y, f"  (worst {sc['worstCase']} · god {sc['godRoll']})", 12, MUTED,
                 max_w=inner - w0 - w1)
        y += s(20)
    if f.get("squad"):
        players = p.get("players") or []
        squad = f"Squad ({len(players)}): {', '.join(players)}" if players else "Squad: unknown"
        text(pad, y, squad, 12, MUTED, max_w=inner)
        y += s(20)
    if f.get("end_reason"):
        reason = p.get("endReason")
        text(pad, y, f"End: {END_REASON_LABEL.get(reason, reason or '?')}", 12, MUTED, max_w=inner)
        y += s(20)

    # footer: WFHelper's "Right-click drag to move" + "Detailed Stats" button; the Deck overlay
    # takes no input, so it says where the details are.
    hint = "Detailed stats: QAM › Arbitrations"
    text(W - pad, H - s(24), hint, 11, (110, 124, 150, 255), anchor="ra")
    return img


def place(position: str, overlay_size: tuple[int, int], panel: Image.Image) -> tuple[int, int]:
    """Top-left corner for the panel in overlay pixels (margins 16 / 8 as in WFHelper)."""
    ow, oh = overlay_size
    f = ow / BASE_W
    mx, my = int(16 * f), int(8 * f)
    w, h = panel.size
    if position == "top-right":
        x, y = ow - w - mx, my
    elif position == "top-left":
        x, y = mx, my
    elif position == "left-middle":
        x, y = mx, (oh - h) // 2
    else:
        x, y = (ow - w) // 2, my
    return max(0, min(x, ow - w)), max(0, min(y, oh - h))


def overlay_scale(setting, overlay_size: tuple[int, int]) -> float:
    try:
        sc = float(setting)
    except (TypeError, ValueError):
        sc = 1.0
    if not math.isfinite(sc):
        sc = 1.0
    sc = min(MAX_SCALE, max(MIN_SCALE, sc))
    return sc * overlay_size[0] / BASE_W
