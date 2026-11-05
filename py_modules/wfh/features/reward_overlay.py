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
    "Common": (100, 116, 139, 255),    # #64748b
}
RARITY_LABELS = {"Rare": "RARE", "Uncommon": "UNCOMMON", "Common": "COMMON"}
DUCAT_TEXT = (247, 212, 122, 255)     # #f7d47a

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


def _plat_text(value: int | None, state: str, unit: bool = True) -> str:
    """Price label: "…" while it loads, "N/A" when it is missing (overlay.js:83-98)."""
    if value:
        return f"{value}p" if unit else str(value)
    return "N/A" if state == "missing" else "…"


@dataclass
class Placed:
    image: Image.Image
    x: int
    y: int


# WFHelper overlay.css: rarity labels and the card chrome.
CARD_BG = (16, 21, 32, 235)            # panel bg + rgba(255,255,255,.04)
CARD_BORDER = (212, 168, 67, 90)       # a card with an item: rgba(212,168,67,.28)
EMPTY_BORDER = (255, 255, 255, 40)     # rgba(255,255,255,.08)
EMPTY_ALPHA = 0.42                     # columns without an item
BEST_TINT = (40, 33, 16, 240)          # the best card: 10 % accent tint
MASTERY_TINT = (16, 30, 52, 240)
NOT_MASTERED = (96, 165, 250, 255)     # #60a5fa
FOUNDRY = (245, 166, 35, 255)          # #f5a623, the "building" orange
CHIP_SIZE = 10

PART_ABBREV = {
    "blueprint": "BP", "neuroptics": "Neuro", "systems": "Sys", "chassis": "Chas", "receiver": "Recv",
    "lower limb": "Lower", "upper limb": "Upper", "carapace": "Cara", "cerebrum": "Cere", "ornament": "Orn",
    "gauntlet": "Gaunt", "harness": "Harn",
}


def part_label(part: str, parent: str | None) -> str:
    """Set-part chip label: the part's own word ("Chassis" -> "Chas", the main blueprint -> "BP").

    WFHelper draws the part's icon here; the overlay has no item icons, so it names the part."""
    n = part or ""
    if parent and n.lower().startswith(parent.lower() + " "):
        n = n[len(parent) + 1:]
    n = re.sub(r"\s+Blueprint$", "", n) if n.lower() != "blueprint" else n
    return PART_ABBREV.get(n.lower(), n if len(n) <= 6 else n[:5] + ".")


def _parent_name(e: RewardEntry) -> str | None:
    return e.set_name[:-4] if e.set_name and e.set_name.endswith(" Set") else None


def reward_chips(e: RewardEntry, show_owned: bool = True, show_vaulted: bool = False) -> list[draw.Pill]:
    """WFHelper's chip row (relic-rewards.md §11.2): Own x/y; Mastered / Not Mastered; In foundry;
    Set x/y; Set value; Vaulted / Unvaulted (hidden by default)."""
    chips: list[draw.Pill] = []
    if show_owned and e.owned is not None:
        chips.append(draw.Pill(f"Own {e.owned}/{e.required}", draw.GOOD if e.owned >= e.required else draw.GOLD_MUTED))
    if e.mastered is True:
        chips.append(draw.Pill("Mastered", draw.GOOD))
    elif e.mastered is False:
        chips.append(draw.Pill("Not Mastered", NOT_MASTERED))
    if e.building:
        chips.append(draw.Pill("In foundry", FOUNDRY))
    if show_owned and e.set_owned is not None and e.set_required:
        done = e.set_owned >= e.set_required
        chips.append(draw.Pill(f"Set {e.set_owned}/{e.set_required}", draw.GOOD if done else draw.GOLD_MUTED))
    if e.set_name:
        chips.append(draw.Pill(f"Set {_plat_text(e.set_plat, e.set_price_state)}", draw.GOLD_ACCENT_HI))
    if show_vaulted and e.vaulted is not None:
        chips.append(draw.Pill("Vaulted" if e.vaulted else "Unvaulted", draw.BAD if e.vaulted else NOT_MASTERED))
    return chips


def part_chips(e: RewardEntry) -> list[draw.Pill]:
    """Up to 6 set-part chips: green when owned >= required, orange outline when building,
    filled when the part is this reward."""
    out = []
    parent = _parent_name(e)
    for part in e.set_parts[:6]:
        color = draw.GOOD if part.owned >= part.required else draw.GOLD_MUTED
        fill = (color[0], color[1], color[2], 90) if part.is_reward else None
        out.append(draw.Pill(f"{part_label(part.name, parent)} {part.owned}/{part.required}", color, fill,
                             FOUNDRY if part.building else (draw.GOLD_TEXT if part.is_reward else None)))
    return out


def render_block(e: RewardEntry | None, width: int, slot: int, is_best_plat: bool = False,
                 show_ducats: bool = True, show_owned: bool = True, is_best_mastery: bool = False,
                 mastery_pick: bool = False, show_vaulted: bool = False, min_height: int = 0) -> Image.Image:
    """One reward card, laid out like WFHelper's .reward-slot: slot label and rarity, name (2 lines),
    platinum and ducats, then the chips and the set-part chips."""
    pad = 8
    inner = width - pad * 2
    if e is None:
        h = max(min_height, 64)
        img = Image.new("RGBA", (width, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        a = EMPTY_ALPHA
        d.rounded_rectangle((0, 0, width - 1, h - 1), 8, fill=(CARD_BG[0], CARD_BG[1], CARD_BG[2], int(235 * a)),
                            outline=(255, 255, 255, int(40 * a) + 20))
        muted = (*draw.GOLD_MUTED[:3], int(255 * a) + 40)
        d.text((pad, pad), f"SLOT {slot + 1}", font=draw.font(10, True), fill=muted)
        d.text((pad, pad + 18), "-", font=draw.font(15, True), fill=muted)
        return img
    # The best card (most platinum; ties to the lower slot) gets the accent border and tint. With
    # "prioritize mastery" on, a part of gear never crafted nor mastered takes that place.
    top = is_best_mastery or (is_best_plat and not mastery_pick)
    if is_best_mastery:
        tag, tag_col, highlight, bg = "MASTERY", NOT_MASTERED, NOT_MASTERED, MASTERY_TINT
    elif is_best_plat:
        tag, tag_col, highlight = ("MOST PLAT" if mastery_pick else "BEST PICK"), draw.GOLD_ACCENT, draw.GOLD_ACCENT
        bg = BEST_TINT if top else CARD_BG
    else:
        tag, tag_col, highlight, bg = f"SLOT {slot + 1}", draw.GOLD_MUTED, None, CARD_BG
    lines = name_lines(e.name, inner - 2, 13)
    chips = draw.flow_pills(reward_chips(e, show_owned, show_vaulted), inner, CHIP_SIZE)
    parts = draw.flow_pills(part_chips(e), inner, CHIP_SIZE) if show_owned else []
    chip_h = int(CHIP_SIZE * 1.3) + 4 + 3
    y_name = pad + 16
    y_vals = y_name + 2 * 17 + 2
    y_chips = y_vals + 24
    h = y_chips + chip_h * len(chips) + (chip_h * len(parts) + 2 if parts else 0) + pad - 3
    h = max(h, min_height)
    img = Image.new("RGBA", (width, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, width - 1, h - 1), 8, fill=bg, outline=highlight or CARD_BORDER,
                        width=2 if highlight else 1)
    d.text((pad, pad), tag, font=draw.font(10, True), fill=tag_col)
    rar = RARITY_LABELS.get(e.rarity or "", (e.rarity or "").upper())
    if rar:
        d.text((width - pad - draw.text_width(rar, 10, True), pad), rar, font=draw.font(10, True),
               fill=RARITY_COLORS.get(e.rarity or "", draw.GOLD_MUTED))
    for i, ln in enumerate(lines[:2]):
        d.text((pad, y_name + i * 17), ln, font=draw.font(13, True), fill=draw.GOLD_TEXT)
    # platinum icon + price, ducat icon + ducats
    x = pad
    x += draw.plat_icon(d, x, y_vals + 4, 12) + 4
    ptxt = _plat_text(e.plat, e.price_state, unit=False)
    d.text((x, y_vals), ptxt, font=draw.font(16, True), fill=draw.GOLD_TEXT if e.plat else draw.GOLD_MUTED)
    if show_ducats:
        dtxt = str(e.ducats) if e.ducats else "N/A"
        dw = draw.text_width(dtxt, 16, True)
        dx = width - pad - dw
        d.text((dx, y_vals), dtxt, font=draw.font(16, True), fill=DUCAT_TEXT if e.ducats else draw.GOLD_MUTED)
        draw.ducat_icon(d, dx - 16, y_vals + 4, 12)
    y = y_chips
    for row in chips:
        for p, px in row:
            draw.draw_pill(d, pad + px, y, p, CHIP_SIZE, inner - px, bg)
        y += chip_h
    if parts:
        y += 2
        for row in parts:
            for p, px in row:
                draw.draw_pill(d, pad + px, y, p, CHIP_SIZE, inner - px, bg)
            y += chip_h
    return img


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
                  best_plat: int | None = None, show_ducats: bool = True, show_owned: bool = True,
                  best_mastery: int | None = None, show_vaulted: bool = False) -> list[Placed]:
    """One block per on-screen card. card_px: (slot, (x, y, w, h)) in capture pixels.

    Every block in the row gets the tallest block's height, like WFHelper's equal grid columns."""
    ow, oh = overlay_size
    s, dx, dy = capture_to_overlay(capture_size, overlay_size)
    by_slot = {e.slot: e for e in entries}
    specs = []
    for slot, (x, y, w, h) in card_px:
        bw = max(MIN_BLOCK_W, round(w * s))
        kw = dict(e=by_slot.get(slot), width=bw, slot=slot, is_best_plat=slot == best_plat, show_ducats=show_ducats,
                  show_owned=show_owned, is_best_mastery=slot == best_mastery,
                  mastery_pick=best_mastery is not None, show_vaulted=show_vaulted)
        cx = round((x + w / 2) * s + dx)
        specs.append((kw, cx - bw // 2, round(y * s + dy), round((y + h) * s + dy)))
    if not specs:
        return []
    tallest = max(render_block(**kw).height for kw, *_ in specs)
    out = []
    for kw, bx, top, bottom in specs:
        img = render_block(**kw, min_height=tallest)
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
