"""Relic reward overlay drawing: one compact block per reward card.

WFHelper draws a single 4-column panel (relic-rewards.md §11.2). On the Deck the
card rects are known exactly, so each block sits directly under (or over) its
in-game card at the card's x and width (§11.3). All blocks go into one RGBA
image for the "rewards" layer, placed at the union's top-left.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image, ImageDraw

from ..overlay import draw
from .reward_enrich import RewardEntry

LAYER = "rewards"
GAP = 4
MIN_BLOCK_W = 136
TOP_MARGIN = 4

RARITY_COLORS = {
    "Rare": (212, 168, 67, 255),      # #d4a843
    "Uncommon": (148, 163, 184, 255),  # #94a3b8
    "Common": (176, 160, 140, 255),    # #64748b is too dark on the Deck panel; warm grey instead
}
RARITY_LABELS = {"Rare": "RARE", "Uncommon": "UNCOM", "Common": "COMMON"}
BEST_BG = (40, 34, 18, 240)
EMPTY_BG = (14, 18, 30, 150)

_ABBREV = ((r"\bBlueprint\b", "BP"), (r"\bNeuroptics\b", "Neuro"), (r"\bSystems\b", "Sys"),
           (r"\bChassis\b", "Chas"), (r"\bReceiver\b", "Recv"))


def short_name(name: str, max_w: int, size: int) -> str:
    """Abbreviate until it fits ("Blueprint" -> "BP" first), then ellipsize."""
    out = name
    for pat, rep in _ABBREV:
        if draw.text_width(out, size, True) <= max_w:
            break
        out = re.sub(pat, rep, out)
    return draw.ellipsize(out, max_w, size, True)


class Card(draw.Card):
    """draw.Card whose rows stack: draw.Card.render (as of this writing) never
    advances y between rows, so every row lands on the first one."""

    def render(self) -> Image.Image:
        inner = self.width - self.pad * 2
        title_h = int(16 * 1.35) + 4 if self.title else 0
        h = self.pad * 2 + title_h + sum(self._row_height(r) for r in self.rows)
        img = Image.new("RGBA", (self.width, max(h, self.pad * 2 + 4)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, img.width - 1, img.height - 1), self.radius, fill=self.bg,
                            outline=self.highlight or self.border, width=2 if self.highlight else 1)
        y = self.pad
        if self.title:
            d.text((self.pad, y), draw.ellipsize(self.title, inner, 16, True), font=draw.font(16, True),
                   fill=self.title_color)
            y += title_h
        for row in self.rows:
            rh = self._row_height(row)
            if row.bg:
                d.rectangle((self.pad - 3, y, self.width - self.pad + 2, y + rh - 1), fill=row.bg)
            total = sum(c.weight for c in row.cells) or 1
            x = self.pad
            for c in row.cells:
                cw = int(inner * c.weight / total)
                t = draw.ellipsize(c.text, cw - 4, c.size, c.bold) if c.text else ""
                tw = draw.text_width(t, c.size, c.bold)
                tx = x if c.align == "left" else (x + cw - tw if c.align == "right" else x + (cw - tw) // 2)
                ty = y + row.pad_y + (rh - row.pad_y * 2 - int(c.size * 1.3)) // 2
                if t:
                    d.text((tx, ty), t, font=draw.font(c.size, c.bold), fill=c.color)
                x += cw
            y += rh
        return img


def _wrap(text: str, max_w: int, size: int) -> list[str]:
    lines: list[str] = []
    cur = ""
    for word in text.split():
        cand = f"{cur} {word}".strip()
        if cur and draw.text_width(cand, size, True) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def name_lines(name: str, max_w: int, size: int, max_lines: int = 2) -> list[str]:
    """The item name on at most two lines; abbreviates ("Blueprint" -> "BP") before cutting."""
    out = name
    lines = _wrap(out, max_w, size)
    for pat, rep in _ABBREV:
        if len(lines) <= max_lines and all(draw.text_width(l, size, True) <= max_w for l in lines):
            break
        out = re.sub(pat, rep, out)
        lines = _wrap(out, max_w, size)
    if len(lines) > max_lines:
        lines = lines[:max_lines - 1] + [" ".join(lines[max_lines - 1:])]
    return [draw.ellipsize(l, max_w, size, True) for l in lines]


def _plat_text(value: int | None, state: str) -> str:
    if value:
        return f"{value}p"
    return "N/A" if state == "missing" else "…"


@dataclass
class Placed:
    image: Image.Image
    x: int
    y: int


def render_block(e: RewardEntry | None, width: int, slot: int, is_best_plat: bool = False,
                 is_best_ducats: bool = False, show_ducats: bool = True, show_owned: bool = True,
                 is_best_mastery: bool = False, mastery_pick: bool = False) -> Image.Image:
    inner = width - 16
    if e is None:
        card = Card(width, bg=EMPTY_BG, border=(60, 70, 90, 160))
        card.add(draw.Cell(f"SLOT {slot + 1}", color=draw.MUTED, size=10, bold=True))
        card.add(draw.Cell("not read", color=draw.MUTED, size=12))
        return card.render()
    best_d = is_best_ducats and show_ducats
    # A part for gear you have never crafted or mastered outranks platinum;
    # the plat/ducat winners keep their tags but lose the filled background.
    top = is_best_mastery or (is_best_plat and not mastery_pick)
    if is_best_mastery:
        tag = draw.Cell("MASTERY", color=draw.ACCENT, size=10, bold=True)
    elif mastery_pick and is_best_plat:
        tag = draw.Cell("MOST PLAT", color=draw.GOLD, size=10, bold=True)
    elif is_best_plat and best_d:
        tag = draw.Cell("BEST PICK", color=draw.GOLD, size=10, bold=True)  # most plat and most ducats
    elif is_best_plat:
        tag = draw.Cell("BEST PLAT", color=draw.GOLD, size=10, bold=True)
    elif best_d:
        tag = draw.Cell("BEST DUCATS", color=draw.DUCAT, size=10, bold=True)
    else:
        tag = draw.Cell(f"SLOT {slot + 1}", color=draw.MUTED, size=10, bold=True)
    rarity = draw.Cell(RARITY_LABELS.get(e.rarity or "", (e.rarity or "").upper()),
                       color=RARITY_COLORS.get(e.rarity or "", draw.MUTED), size=10, bold=True, align="right",
                       weight=0.72)
    if is_best_mastery:
        highlight = draw.ACCENT
    elif is_best_plat:
        highlight = draw.GOLD
    else:
        highlight = draw.DUCAT if best_d else None
    card = Card(width, bg=BEST_BG if top else draw.BG, highlight=highlight)
    card.pad = 8
    card.add(tag, rarity)
    lines = name_lines(e.name, inner - 2, 12)
    for i in range(2):  # always two rows, so blocks in one row line up
        card.add(draw.Cell(lines[i] if i < len(lines) else "", color=draw.TEXT, size=12, bold=True))
        card.rows[-1].pad_y = 1
    value_cells = [draw.Cell(_plat_text(e.plat, e.price_state), color=draw.PLAT, size=15, bold=True)]
    if show_ducats:
        value_cells.append(draw.Cell(f"{e.ducats}d" if e.ducats else "–", color=draw.DUCAT, size=15, bold=True,
                                     align="right"))
    card.add(*value_cells)
    if show_owned:
        info = []
        if e.owned is not None:
            have = e.owned >= e.required
            info.append(draw.Cell(f"Own {e.owned}/{e.required}", color=draw.GOOD if have else draw.MUTED, size=11))
        if e.set_owned is not None and e.set_required:
            info.append(draw.Cell(f"Set {e.set_owned}/{e.set_required}", color=draw.MUTED, size=11, align="right"))
        elif e.building:
            info.append(draw.Cell("In foundry", color=draw.WARN, size=11, align="right"))
        if info:
            card.add(*info)
    extra = []
    if e.set_name:
        extra.append(draw.Cell(f"Set {_plat_text(e.set_plat, e.set_price_state)}", color=draw.PLAT, size=11,
                               weight=0.8))
    if e.vaulted:
        extra.append(draw.Cell("Vaulted", color=draw.WARN, size=11, align="right", weight=1.2))
    elif e.mastered is False:
        label = "Not crafted" if e.crafted is False else "Unmastered"
        extra.append(draw.Cell(label, color=draw.ACCENT, size=11, align="right", weight=1.2))
    if extra:
        card.add(*extra)
    return card.render()


def capture_to_overlay(capture_size: tuple[int, int], overlay_size: tuple[int, int]) -> tuple[float, float, float]:
    """(scale, off_x, off_y) mapping capture pixels onto the overlay.

    gamescope fits the game buffer into the output with one uniform scale and
    centres it (letterbox/pillarbox), and render-size screenshots come at the
    buffer's size, so a 16:9 capture on a 16:10 output needs scale + offset,
    not separate x/y ratios. Matching aspect ratios give zero offsets."""
    cw, ch = capture_size
    ow, oh = overlay_size
    if not cw or not ch:
        return 1.0, 0.0, 0.0
    s = min(ow / cw, oh / ch)
    return s, (ow - cw * s) / 2, (oh - ch * s) / 2


def layout_blocks(entries: list[RewardEntry], card_px: list[tuple[int, tuple[int, int, int, int]]],
                  capture_size: tuple[int, int], overlay_size: tuple[int, int], position: str = "below",
                  best_plat: int | None = None, best_ducats: int | None = None, show_ducats: bool = True,
                  show_owned: bool = True, best_mastery: int | None = None) -> list[Placed]:
    """One block per on-screen card. card_px: (slot, (x, y, w, h)) in capture pixels."""
    ow, oh = overlay_size
    s, dx, dy = capture_to_overlay(capture_size, overlay_size)
    by_slot = {e.slot: e for e in entries}
    rendered = []
    for slot, (x, y, w, h) in card_px:
        bw = max(MIN_BLOCK_W, round(w * s))
        img = render_block(by_slot.get(slot), bw, slot, slot == best_plat, slot == best_ducats, show_ducats, show_owned,
                           slot == best_mastery, mastery_pick=best_mastery is not None)
        cx = round((x + w / 2) * s + dx)
        rendered.append((img, cx - bw // 2, round(y * s + dy), round((y + h) * s + dy)))
    if not rendered:
        return []
    # One row: every block shares the tallest block's top/bottom alignment.
    tallest = max(img.height for img, *_ in rendered)
    out = []
    for img, bx, top, bottom in rendered:
        if position == "above":
            by = top - GAP - tallest
            if by < TOP_MARGIN:
                by = bottom + GAP
        else:
            by = bottom + GAP
            if by + tallest > oh:
                by = max(TOP_MARGIN, top - GAP - tallest)
        bx = min(max(0, bx), ow - img.width)
        out.append(Placed(img, bx, by))
    return out


def compose(placed: list[Placed]) -> tuple[Image.Image, int, int] | None:
    if not placed:
        return None
    x0 = min(p.x for p in placed)
    y0 = min(p.y for p in placed)
    x1 = max(p.x + p.image.width for p in placed)
    y1 = max(p.y + p.image.height for p in placed)
    canvas = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    for p in placed:
        canvas.alpha_composite(p.image, (p.x - x0, p.y - y0))
    return canvas, x0, y0


def render_message(text: str, sub: str = "", width: int = 300) -> Image.Image:
    card = Card(width, title="Relic rewards")
    card.add(draw.Cell(text, color=draw.TEXT, size=13))
    if sub:
        card.add(draw.Cell(sub, color=draw.MUTED, size=11))
    return card.render()


def toast_text(entries: list[RewardEntry], best_plat: int | None, best_ducats: int | None,
               best_mastery: int | None = None) -> tuple[str, str]:
    """Short toast for when the overlay window is not available."""
    by_slot = {e.slot: e for e in entries}
    if best_mastery is not None and best_mastery in by_slot:
        b = by_slot[best_mastery]
        title = f"For mastery: {short_name(b.name, 10_000, 13)}"
    elif best_plat is not None and best_plat in by_slot:
        b = by_slot[best_plat]
        title = f"Best: {short_name(b.name, 10_000, 13)} {b.plat}p"
    elif best_ducats is not None and best_ducats in by_slot:
        b = by_slot[best_ducats]
        title = f"Best ducats: {short_name(b.name, 10_000, 13)} {b.ducats}d"
    else:
        title = "Relic rewards"
    parts = []
    for e in sorted(entries, key=lambda e: e.slot):
        v = f"{e.plat}p" if e.plat else ("?" if e.price_state == "pending" else "-")
        d = f"/{e.ducats}d" if e.ducats else ""
        parts.append(f"{e.slot + 1}) {re.sub(r' Blueprint$', ' BP', e.name)} {v}{d}")
    return title, " · ".join(parts)
