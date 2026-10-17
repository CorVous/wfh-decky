"""Pillow drawing helpers shared by all overlays, so they look alike.

Build a card with `Card(width)`, add rows, then `card.render()` to get an
RGBA image for `OverlayManager.show`.
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "fonts")

# Midnight palette, after WFHelper's default theme.
BG = (14, 18, 30, 225)
BG_ALT = (24, 30, 48, 235)
BORDER = (86, 110, 160, 255)
TEXT = (232, 236, 245, 255)
MUTED = (150, 160, 182, 255)
ACCENT = (110, 170, 255, 255)
GOOD = (96, 214, 140, 255)
WARN = (240, 190, 80, 255)
BAD = (240, 100, 100, 255)
PLAT = (160, 200, 255, 255)
DUCAT = (226, 180, 90, 255)
GOLD = (255, 214, 102, 255)


@lru_cache(maxsize=32)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = ["Inter-Bold.ttf", "NotoSans-Bold.ttf"] if bold else ["Inter-Regular.ttf", "NotoSans-Regular.ttf"]
    for n in names:
        p = os.path.join(FONT_DIR, n)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    for p in (
        "/usr/share/fonts/noto/NotoSans-Bold.ttf" if bold else "/usr/share/fonts/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def text_width(text: str, size: int, bold: bool = False) -> int:
    f = font(size, bold)
    return int(f.getlength(text))


def ellipsize(text: str, max_w: int, size: int, bold: bool = False) -> str:
    if text_width(text, size, bold) <= max_w:
        return text
    while text and text_width(text + "…", size, bold) > max_w:
        text = text[:-1]
    return text + "…"


@dataclass
class Cell:
    text: str
    color: tuple = TEXT
    size: int = 14
    bold: bool = False
    align: str = "left"  # left | right | center
    weight: float = 1.0  # share of the row width


@dataclass
class Row:
    cells: list[Cell]
    bg: tuple | None = None
    pad_y: int = 3


@dataclass
class Card:
    width: int
    title: str | None = None
    title_color: tuple = ACCENT
    rows: list[Row] = field(default_factory=list)
    pad: int = 8
    radius: int = 8
    bg: tuple = BG
    border: tuple | None = BORDER
    highlight: tuple | None = None  # draws a thicker border in this color

    def add(self, *cells: Cell | str, bg: tuple | None = None) -> "Card":
        self.rows.append(Row([c if isinstance(c, Cell) else Cell(c) for c in cells], bg=bg))
        return self

    def _row_height(self, row: Row) -> int:
        size = max((c.size for c in row.cells), default=14)
        return int(size * 1.3) + row.pad_y * 2

    def render(self) -> Image.Image:
        inner = self.width - self.pad * 2
        title_h = int(16 * 1.35) + 4 if self.title else 0
        h = self.pad * 2 + title_h + sum(self._row_height(r) for r in self.rows)
        img = Image.new("RGBA", (self.width, max(h, self.pad * 2 + 4)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        border = self.highlight or self.border
        d.rounded_rectangle(
            (0, 0, img.width - 1, img.height - 1), self.radius, fill=self.bg,
            outline=border, width=2 if self.highlight else 1,
        )
        y = self.pad
        if self.title:
            d.text((self.pad, y), ellipsize(self.title, inner, 16, True), font=font(16, True), fill=self.title_color)
            y += title_h
        for row in self.rows:
            rh = self._row_height(row)
            if row.bg:
                d.rectangle((self.pad - 3, y, self.width - self.pad + 2, y + rh - 1), fill=row.bg)
            total = sum(c.weight for c in row.cells) or 1
            x = self.pad
            for c in row.cells:
                cw = int(inner * c.weight / total)
                t = ellipsize(c.text, cw - 4, c.size, c.bold)
                tw = text_width(t, c.size, c.bold)
                tx = x if c.align == "left" else (x + cw - tw if c.align == "right" else x + (cw - tw) // 2)
                ty = y + row.pad_y + (rh - row.pad_y * 2 - int(c.size * 1.3)) // 2
                d.text((tx, ty), t, font=font(c.size, c.bold), fill=c.color)
                x += cw
            y += rh
        return img


def badge(text: str, color: tuple = ACCENT, size: int = 13, bg: tuple = BG) -> Image.Image:
    """A small pill with one line of text."""
    w = text_width(text, size, True) + 14
    h = int(size * 1.3) + 8
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, w - 1, h - 1), h // 2, fill=bg, outline=color)
    d.text((7, 4), text, font=font(size, True), fill=color)
    return img


def fmt_plat(value: float | None) -> str:
    if value is None:
        return "–"
    return f"{value:.0f}p" if value >= 10 or value == int(value) else f"{value:.1f}p"
