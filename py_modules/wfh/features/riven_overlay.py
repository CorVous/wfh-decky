"""Pillow rendering of the riven panels (docs/port/riven-scanner.md §16.2).

Two panels in 1280x800 design space, scaled to the overlay window:
- layer "riven"        left,  "CURRENT",  x=8,    y=48, w=264, at most ~680 px tall;
- layer "riven_right"  right, "NEW ROLL", x=1008, y=48, w=264, at most ~500 px tall
  so it ends above the FITS IN plate (y ~558 on a 16:9 canvas).
Both stay clear of the card column (x 281-997). The chat view shows the left panel only.

A panel view is a plain dict built by the orchestrator (riven_scanner.panel_views):
  label, weapon, roll_count, status (spinner text), error, warning, stats (graded),
  overall_grade, attribute_grade, best {positives, negatives}, listings (ranked rows),
  better (outline green), chosen (green border after a choice), arrows {name_lc: +1|-1}.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from ..overlay import draw as d

LAYER_LEFT = "riven"
LAYER_RIGHT = "riven_right"
DESIGN_W, DESIGN_H = 1280, 800
PANEL_W = 264
PANEL_TOP = 48
LEFT_X = 8
RIGHT_X = DESIGN_W - 8 - PANEL_W
LEFT_MAX_H = 680
RIGHT_MAX_H = 500
LISTINGS_SHOWN = 5

PAD = 10


def _rgb(hex_: str, a: int = 255) -> tuple:
    h = hex_.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), a)


PANEL_BG = _rgb("#060a12", 242)
SURFACE = _rgb("#111827", 240)
TEXT = _rgb("#e8edf5")
MUTED = _rgb("#a0aec0")
BORDER = (79, 158, 255, 64)
ACCENT = _rgb("#4f9eff")
OK = _rgb("#48c774")
WARN_C = _rgb("#f5a623")
BAD_C = _rgb("#ff5a5a")
CHOSEN = _rgb("#4ade80")

GRADE_BADGE = {"S": "#4ade80", "A+": "#6aab7a", "A": "#6aab7a", "A-": "#6aab7a", "B+": "#facc15", "B": "#facc15",
               "B-": "#facc15", "C+": "#f97316", "C": "#f97316", "C-": "#f97316", "F": "#ef4444"}
GRADE_DOT = {"S": "#d4a017", "A+": "#2ecc40", "A": "#27ae60", "A-": "#1e8449", "B+": "#3498db", "B": "#2980b9",
             "B-": "#2471a3", "C+": "#e67e22", "C": "#d35400", "C-": "#a04000", "F": "#e74c3c"}
ATTRIBUTE_COLORS = {"Great": "#4ade80", "Good": "#6aab7a", "OK": "#facc15", "Bad": "#ef4444"}

STAT_ABBREVIATIONS = {
    "critical chance": "CritCh", "critical damage": "CritDmg", "multishot": "Multi", "damage": "Dmg",
    "melee damage": "Dmg", "status chance": "Status", "attack speed": "AtkSpd", "electricity": "Elec",
    "toxin": "Toxin", "heat": "Heat", "cold": "Cold", "range": "Range", "zoom": "Zoom", "ammo maximum": "Ammo",
    "weapon recoil": "Recoil", "projectile speed": "ProjSpd", "finisher damage": "Finisher",
    "heavy attack efficiency": "HvyAtk", "combo duration": "Combo", "slide attack": "Slide",
    "reload speed": "Reload", "fire rate": "FireRate",
}
STAT_NAME_CANON = {
    "attack speed": "fire rate", "melee damage": "damage", "critical chance for slide attack": "slide attack",
    "slide": "slide attack", "recoil": "weapon recoil", "magazine": "magazine capacity",
    "heavy attack": "heavy attack efficiency",
}

STRINGS = {
    "current": "Current", "newRoll": "New roll", "roll": "Roll {count}", "scanning": "Scanning...",
    "scanningCurrent": "Scanning current stats...", "scanningRoll": "Scanning riven stats...",
    "rescanning": "Rescanning...", "waitingForRoll": "Waiting for roll...", "waitingForScan": "Waiting for scan...",
    "readFailed": "Couldn't read the riven. Keep the card visible and set Warframe's UI language to English.",
    "rollReadFailed": "Couldn't read the new roll. Keep both cards visible and set Warframe's UI language to English.",
    "textTooSmall": "The riven text is too small to read reliably. Set Warframe's Interface Scale to 100%.",
    "weaponMissing": "Couldn't read the weapon name, so there are no grades. Use Rescan in the Rivens menu to retry.",
    "bestPositives": "Best", "bestNegatives": "Neg", "matchPercent": "{percent}% match", "rolls": "Rolls: {count}",
    "similarOnWfm": "Similar on WFM", "noSimilarFound": "No similar rivens found",
}


def abbreviate(name: str) -> str:
    return STAT_ABBREVIATIONS.get(str(name).lower(), name)


def canonical(name: str) -> str:
    n = str(name or "").lower().strip()
    return STAT_NAME_CANON.get(n, n)


def format_value(stat: dict) -> str:
    """Card value: +153.4% / U+2212 44.3% / x0.84."""
    v = stat.get("value")
    if v is None:
        return "?"
    if stat.get("multiplier"):
        return f"x{_num(v)}"
    shown = stat.get("displayPositive", stat.get("positive", True))
    return f"{'+' if shown else chr(0x2212)}{_num(v)}%"


def _num(v: float) -> str:
    return str(int(v)) if float(v) == int(v) else f"{v:g}"


def bar_color(stat: dict) -> tuple:
    f = float(stat.get("rollFloat", 0.5) or 0)
    pct = 1 - f if not stat.get("positive", True) else f
    return OK if pct >= 0.85 else ACCENT if pct >= 0.6 else WARN_C if pct >= 0.35 else BAD_C


def wrap(text: str, max_w: int, size: int, bold: bool = False) -> list[str]:
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if cur and d.text_width(t, size, bold) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


# ---- panel renderer ----------------------------------------------------------------------------

class _Canvas:
    """Draw top to bottom onto a tall scratch image, then crop to the used height."""

    def __init__(self, width: int, max_h: int):
        self.w = width
        self.max_h = max_h
        self.img = Image.new("RGBA", (width, 1400), (0, 0, 0, 0))
        self.dr = ImageDraw.Draw(self.img)
        self.y = PAD

    def text(self, x: int, s: str, size: int, color: tuple, bold: bool = False, y: int | None = None) -> None:
        self.dr.text((x, self.y if y is None else y), s, font=d.font(size, bold), fill=color)

    def pill(self, x: int, y: int, s: str, color: tuple, size: int = 11, fill: tuple | None = None,
             outline: bool = True) -> int:
        tw = d.text_width(s, size, True)
        w, h = tw + 12, int(size * 1.3) + 5
        self.dr.rounded_rectangle((x, y, x + w - 1, y + h - 1), h // 2, fill=fill,
                                  outline=color if outline else None)
        self.dr.text((x + 6, y + 2), s, font=d.font(size, True), fill=color)
        return w


def _triangle(dr: ImageDraw.ImageDraw, x: int, y: int, up: bool, color: tuple, size: int = 7) -> None:
    if up:
        pts = [(x, y + size), (x + size, y + size), (x + size / 2, y)]
    else:
        pts = [(x, y), (x + size, y), (x + size / 2, y + size)]
    dr.polygon(pts, fill=color)


def render_panel(view: dict, width: int = PANEL_W, max_h: int = LEFT_MAX_H) -> Image.Image:
    c = _Canvas(width, max_h)
    inner = width - PAD * 2
    right_side = view.get("side") == "right"

    # header: label + Roll N
    label = str(view.get("label") or "").upper()
    c.text(PAD, label, 11, ACCENT if right_side else MUTED, True)
    if view.get("roll_count"):
        s = STRINGS["roll"].format(count=view["roll_count"])
        tw = d.text_width(s, 11, True) + 12
        c.pill(width - PAD - tw, c.y - 2, s, ACCENT, 11, fill=SURFACE)
    c.y += 18

    # weapon + overall grade + attribute grade
    badges: list[tuple[str, tuple]] = []
    if view.get("overall_grade") and view["overall_grade"] != "?":
        badges.append((view["overall_grade"], _rgb(GRADE_BADGE.get(view["overall_grade"], "#a0aec0"))))
    ag = view.get("attribute_grade")
    if ag and ag != "?":
        badges.append((ag, _rgb(ATTRIBUTE_COLORS.get(ag, "#a0aec0"))))
    bw = sum(d.text_width(t, 12, True) + 12 + 4 for t, _ in badges)
    weapon = view.get("weapon") or "Riven"
    c.text(PAD, d.ellipsize(weapon, inner - bw - 4, 17, True), 17, TEXT, True)
    x = width - PAD
    for t, col in reversed(badges):
        tw = d.text_width(t, 12, True) + 12
        x -= tw
        c.pill(x, c.y + 1, t, col, 12, fill=SURFACE)
        x -= 4
    c.y += 26
    c.dr.line((PAD, c.y, width - PAD, c.y), fill=BORDER, width=1)
    c.y += 6

    # status / errors
    if view.get("status"):
        c.dr.ellipse((PAD, c.y + 4, PAD + 8, c.y + 12), outline=ACCENT, width=2)
        c.text(PAD + 14, view["status"], 13, MUTED)
        c.y += 22
    for key, color in (("error", BAD_C), ("warning", WARN_C)):
        if view.get(key):
            lines = wrap(view[key], inner - 12, 12)
            h = len(lines) * 16 + 8
            c.dr.rounded_rectangle((PAD, c.y, width - PAD, c.y + h), 5, fill=SURFACE, outline=color)
            for i, ln in enumerate(lines):
                c.text(PAD + 6, ln, 12, color, y=c.y + 4 + i * 16)
            c.y += h + 6

    # stat rows
    arrows = view.get("arrows") or {}
    for st in view.get("stats") or []:
        grade = st.get("grade")
        dot = _rgb(GRADE_DOT.get(grade, "#a0aec0")) if grade and grade != "?" else MUTED
        c.dr.ellipse((PAD, c.y + 7, PAD + 7, c.y + 14), fill=dot)
        val = format_value(st)
        vx = PAD + 10
        val_w = 76
        tw = d.text_width(val, 16, True)
        c.text(vx + val_w - tw, val, 16, TEXT, True, y=c.y)
        nx = vx + val_w + 8
        arrow = arrows.get(str(st.get("name", "")).lower())
        gw = 0
        if grade:
            gs = grade
            gw = d.text_width(gs, 11, True) + 12
            c.pill(width - PAD - gw, c.y + 2, gs, _rgb(GRADE_BADGE.get(grade, "#a0aec0")), 11, fill=SURFACE)
        aw = 12 if arrow else 0
        name = d.ellipsize(str(st.get("name", "")), width - PAD - gw - 6 - aw - nx, 13)
        c.text(nx, name, 13, TEXT if st.get("positive", True) else _rgb("#f4a3a3"), y=c.y + 2)
        if arrow:
            ax = width - PAD - gw - 4 - 9
            _triangle(c.dr, ax, c.y + 6, arrow > 0, OK if arrow > 0 else BAD_C, 8)
        c.y += 21
        if "rollFloat" in st and grade and grade != "?":
            f = max(0.0, min(1.0, float(st["rollFloat"])))
            c.dr.rectangle((nx, c.y, width - PAD, c.y + 2), fill=(255, 255, 255, 28))
            c.dr.rectangle((nx, c.y, nx + int((width - PAD - nx) * f), c.y + 2), fill=bar_color(st))
        c.y += 7

    # best attributes
    best = view.get("best") or {}
    stats = view.get("stats") or []
    if stats and (best.get("positives") or best.get("negatives")):
        c.y += 2
        for key, side_label, want_pos, col in (("positives", STRINGS["bestPositives"], True, OK),
                                               ("negatives", STRINGS["bestNegatives"], False, BAD_C)):
            names = best.get(key) or []
            if not names:
                continue
            c.text(PAD, side_label, 11, col, True, y=c.y + 2)
            x = PAD + 34
            row_h = 20
            for n in names:
                ab = abbreviate(n)
                cw = d.text_width(ab, 11, True) + 12
                if x + cw > width - PAD:
                    x = PAD + 34
                    c.y += row_h
                hit = any(s.get("positive") == want_pos and canonical(s.get("name")) == canonical(n) for s in stats)
                c.pill(x, c.y, ab, col if hit else MUTED, 11, fill=(col[0], col[1], col[2], 56) if hit else None)
                x += cw + 4
            c.y += row_h + 2

    # similar listings (top N)
    listings = view.get("listings")
    if stats and listings is not None:
        c.y += 2
        c.dr.line((PAD, c.y, width - PAD, c.y), fill=BORDER, width=1)
        c.y += 5
        c.text(PAD, STRINGS["similarOnWfm"], 11, MUTED, True)
        c.y += 17
        if not listings:
            c.text(PAD, STRINGS["noSimilarFound"], 12, MUTED)
            c.y += 18
        for row in listings[:LISTINGS_SHOWN]:
            if c.y + 36 > max_h - PAD:
                break
            pct = int(row.get("match", 0))
            col = OK if pct >= 75 else WARN_C if pct >= 40 else BAD_C
            c.dr.rounded_rectangle((PAD, c.y, width - PAD, c.y + 34), 5, fill=SURFACE)
            c.text(PAD + 6, f"{pct}%", 13, col, True, y=c.y + 2)
            price = f"{int(row.get('price') or 0)}p"
            c.text(PAD + 52, price, 13, d.PLAT, True, y=c.y + 2)
            rolls = STRINGS["rolls"].format(count=int(row.get("rerolls") or 0))
            rw = d.text_width(rolls, 11)
            c.text(width - PAD - 6 - rw, rolls, 11, MUTED, y=c.y + 4)
            x = PAD + 6
            for st in row.get("stats") or []:
                ab = abbreviate(st.get("name", ""))
                if st.get("name", "").lower().startswith("damage vs "):
                    ab = "Dmg" + st["name"][10:13]
                col2 = TEXT if st.get("matched") else (MUTED[0], MUTED[1], MUTED[2], 128)
                if not st.get("positive", True):
                    col2 = _rgb("#f4a3a3") if st.get("matched") else (244, 163, 163, 110)
                w = d.text_width(ab, 10)
                if x + w > width - PAD - 6:
                    break
                c.text(x, ab, 10, col2, y=c.y + 19)
                if not st.get("matched"):
                    c.dr.line((x, c.y + 26, x + w, c.y + 26), fill=col2, width=1)
                x += w + 6
            c.y += 38

    h = min(max_h, c.y + PAD - 2)
    out = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(out)
    outline = CHOSEN if view.get("chosen") or view.get("better") else BORDER
    od.rounded_rectangle((0, 0, width - 1, h - 1), 10, fill=PANEL_BG, outline=outline,
                         width=3 if outline is CHOSEN else 1)
    body = c.img.crop((0, 0, width, h))
    out.alpha_composite(body)
    if outline is CHOSEN:  # redraw the outline above the content
        od.rounded_rectangle((0, 0, width - 1, h - 1), 10, outline=outline, width=3)
    return out


def scale_to_overlay(img: Image.Image, x: int, y: int, overlay_size: tuple[int, int]) -> tuple[Image.Image, int, int]:
    """Design-space (1280x800) panel at (x, y) -> (image, x, y) on the real overlay."""
    ow, oh = overlay_size
    s = min(ow / DESIGN_W, oh / DESIGN_H) or 1.0
    ox = (ow - DESIGN_W * s) / 2
    oy = (oh - DESIGN_H * s) / 2
    if abs(s - 1.0) > 0.01:
        img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)
    return img, int(ox + x * s), int(oy + y * s)


def render_message(title: str, body: str = "") -> Image.Image:
    """Small fallback card (e.g. models missing)."""
    card = d.Card(PANEL_W, title=title)
    for ln in wrap(body, PANEL_W - 20, 12):
        card.add(d.Cell(ln, d.MUTED, 12))
    return card.render()
