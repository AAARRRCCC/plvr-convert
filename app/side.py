"""The side-by-side screenshot: every picture and video from the post and its
quote on the left, the post itself (header, text, the quoted post, counts)
on the right, as x.com's photo viewer lays a post out. Wider than it is tall,
so a chat app that fits media into a landscape box shrinks it less than the
column card.

`make` measures it; `draw` renders it, with the video cells left transparent
for ffmpeg exactly as card.render does, so shot.command composes it.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

from . import card, shot
from .plan import Plan

TEXT_COL = 380            # css px: the right-hand column
MEDIA_W_MIN = 220         # css px
MEDIA_H_MIN = 300
ASPECT = 1.3              # width over height the whole image aims for: Discord's box is about 426x340


@dataclass
class Side:
    shot: shot.Shot       # its layout is the whole canvas, media ops and cells only
    text: card.Layout     # the right-hand column, drawn separately and pasted in

    @property
    def layout(self) -> card.Layout:
        return self.shot.layout


def _strip(post: dict) -> dict:
    p = dict(post, media=[])
    if p.get("quote"):
        p["quote"] = _strip(p["quote"])
    return p


def items(post: dict) -> list[dict]:
    """Every media item, the post's first, then each quote's, at most four."""
    out, p = [], post
    while p:
        out += p["media"]
        p = p.get("quote")
    return out[:4]


def make(post: dict, stats: bool, max_lines: int) -> Side | None:
    """None when there is no media to put beside the text."""
    its = items(post)
    if not its:
        return None
    S, PAD = card.S, card.PAD
    px = lambda v: int(round(v * S))
    T = card.layout(_strip(post), "dark", stats, max_lines=max_lines, col=TEXT_COL)
    text_h = T.height / S
    # the pane is as tall as the text beside it; the media is as wide as that
    # height allows for its shape, and no wider than keeps the whole near ASPECT
    pane_h = max(text_h - 2 * PAD, MEDIA_H_MIN)
    if len(its) == 1:
        w = pane_h * (its[0].get("w") or 16) / (its[0].get("h") or 9)
    else:
        w = pane_h * 16 / 9
    w = max(MEDIA_W_MIN, min(w, ASPECT * (pane_h + 2 * PAD) - TEXT_COL - PAD))
    boxes, bh = card.media_boxes(its, PAD, 0, w, (True,) * 4)
    height = max(T.height, px(bh + 2 * PAD))
    height += height % 2
    width = px(PAD + w) + T.width
    width += width % 2
    L = card.Layout(width, height, T.theme)
    top = (height / S - bh) / 2
    radius = 16
    for it, bx, by, bw, bh_, fit in boxes:
        by += top
        cr = (True,) * 4 if len(boxes) == 1 else (False,) * 4
        if it["kind"] == "photo":
            L.ops.append(("image", it["url"], px(bx), px(by), px(bw), px(bh_), px(radius), cr, "cover"))
        else:
            c = card.Cell(px(bx), px(by), px(bw), px(bh_), fit, it, px(radius), cr)
            L.cells.append(c)
            L.ops.append(("poster", it.get("poster"), px(bx), px(by), px(bw), px(bh_), px(radius), cr, fit))
    L.ops.append(("frame", px(PAD), px(top), px(w), px(bh), px(radius), (True,) * 4, T.theme["line"]))
    master = next((c for c in L.cells if c.item["kind"] == "video"), L.cells[0] if L.cells else None)
    dur = 0.0
    if master is not None:
        master.master = True
        dur = min(shot.MAX_DURATION, master.item.get("duration") or 0)
    p = Plan("shot", "mp4" if L.cells else "png", "side", "side", 0)
    p.args = [str(dur)]
    return Side(shot.Shot(post, L, p), T)


def draw(s: Side, holes: bool) -> bytes:
    """The media pane from card.render, the text column pasted to its right."""
    base = Image.open(io.BytesIO(card.render(s.layout, holes))).convert("RGBA")
    text = Image.open(io.BytesIO(card.render(s.text, False))).convert("RGBA")
    base.paste(text, (s.layout.width - s.text.width, (s.layout.height - s.text.height) // 2))
    out = io.BytesIO()
    base.save(out, "PNG", compress_level=6)
    return out.getvalue()
