"""Lay out a Substack note the way substack.com shows it on the note's own
page, for card.py to draw: the avatar beside the name, its age and the
handle, with Subscribe and the menu dots on the right; the text the width of
the column under them; one image at its own size, or several in a row of
equal height that the column's edge cuts off; a quoted note, an article, or
a passage picked out of one as Substack's cards; the action row; and under a
rule, the likes and restacks and the date.

With `feed` a note is laid out as a list of notes shows it (a profile's
board): the text beside the avatar under the name, and nothing below the
action row.

Sizes are substack.com's, in css px, drawn at card.S device px each.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import card
from .card import S, Cell, Layout, Text, _line

PAD = 16
W = card.COL - 2 * PAD     # the column: 566 here, 568 on substack.com
AVATAR = 36
ROW = 300                  # several media stand in a row this tall
TALL = 420                 # one is drawn at its own size, no taller than this
RADIUS = 12
TZ = os.environ.get("NOTE_TZ", "America/New_York")   # the zone the date under a note is told in
# an article's or a passage's card is dark in either theme, its text light
ON_CARD, ON_CARD_DIM, PASSAGE = "#ffffff", "#c8c8c8", "#232525"


def px(v: float) -> int:
    return int(round(v * S))


def _zone():
    try:
        return ZoneInfo(TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def age(created: float, now: float | None = None) -> str:
    """How old a note is, as Substack puts it by the name: 5m, 7h, 6d, then
    the date."""
    now = now or time.time()
    d = max(0, int(now - created))
    if d < 3600:
        return f"{max(1, d // 60)}m"
    if d < 86400:
        return f"{d // 3600}h"
    if d < 7 * 86400:
        return f"{d // 86400}d"
    z = _zone()
    then = datetime.fromtimestamp(created, z)
    day = f"{then:%b} {then.day}"
    return day if then.year == datetime.fromtimestamp(now, z).year else f"{day}, {then.year}"


def stamp(created: float, now: float | None = None) -> str:
    """The date under a note, in NOTE_TZ: Nov 6 at 9:03 AM, with the year
    when it isn't this one."""
    z = _zone()
    then = datetime.fromtimestamp(created, z)
    day = f"{then:%b} {then.day}"
    if then.year != datetime.fromtimestamp(now or time.time(), z).year:
        day += f", {then.year}"
    return f"{day} at {(then.hour - 1) % 12 + 1}:{then.minute:02d} {'AM' if then.hour < 12 else 'PM'}"


def _tally(n: int, what: str) -> str:
    return f"{n:,} {what}{'' if n == 1 else 's'}" if n else ""


def _rgba(color: str, a: int = 255) -> tuple:
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), a)


def _clip(text: str, size: float, lh: float, bold, color: str, width: float, n: int) -> Text:
    """Text cut at `n` lines with an ellipsis, as Substack's line clamp does."""
    t = Text.make([{"text": text, "link": False}], size, lh, bold, color, color, width, n, color)
    if t.truncated:
        t.lines = t.lines[:n]
        t.height = n * lh
        role = card.face(bold)
        dots = {"kind": "text", "s": "…", "role": role, "color": color, "brk": False,
                "w": card.fonts().font(role, size * S).getlength("…") / S}
        last = t.lines[-1]
        while last and (sum(a["w"] for a in last) + dots["w"] > width or last[-1]["kind"] == "space"):
            last.pop()
        last.append(dots)
    return t


def layout(post: dict, theme: str = "dark", stats: bool = True, now: float | None = None,
           max_lines: int = card.MAX_LINES, feed: bool = False) -> Layout:
    T = card.palette(theme, "substack")
    L = Layout(card.COL * S, 0, T)
    ops = L.ops

    def put(it: dict, x: float, y: float, w: float, h: float, corners: tuple, show: float | None = None) -> None:
        """One media item; `show` is how much of its width is left showing
        where the row is cut off. A video cut off is drawn as its still."""
        clip = (px(x + show) - px(x),) if show is not None and show < w else ()
        if it["kind"] == "photo":
            ops.append(("image", it["url"], px(x), px(y), px(w), px(h), px(RADIUS), corners, "cover", *clip))
            return
        if not clip and not it.get("still"):
            L.cells.append(Cell(px(x), px(y), px(w), px(h), "cover", it, px(RADIUS), corners))
        ops.append(("poster", it.get("poster"), px(x), px(y), px(w), px(h), px(RADIUS), corners, "cover", *clip))

    def media(items: list[dict], x: float, y: float, w: float) -> float:
        if len(items) == 1:
            # an image at half its pixels, as Substack takes every upload
            # for a 2x one; a video as wide as it can be
            it = items[0]
            iw, ih = it.get("w") or 0, it.get("h") or 0
            native = iw / 2 if it["kind"] == "photo" else w
            bw = min(native, w, TALL * iw / ih) if iw and ih else w
            bh = bw * ih / iw if iw and ih else w * 9 / 16
            put(it, x, y, bw, bh, (True,) * 4)
            return bh
        cx, end = x, x + w
        for it in items:
            a = (it.get("w") or 16) / (it.get("h") or 9)
            iw = ROW * min(max(a, card.ASPECT[0]), card.ASPECT[1])
            show = min(iw, end - cx)
            if show < 1:
                break
            whole = show >= iw
            put(it, cx, y, iw, ROW, (True, whole, whole, True), show)
            cx += iw + 8
        if cx - 8 > end:
            # the button that scrolls the row on
            ops.append(("fill", px(end - 44), px(y + ROW / 2 - 16), px(32), px(32), px(16), (38, 38, 38, 230)))
            ops.append(("icon", "s_next", px(end - 36), px(y + ROW / 2 - 8), px(16), ON_CARD))
        return ROW

    def quote(q: dict, x: float, y: float, w: float, depth: int) -> float:
        """A quoted note as Substack's card; returns its height."""
        top, at = y, len(ops)
        ops.append(None)   # the card, once its height is known
        ix, iw = x + 12, w - 24
        y += 12
        ops.append(("avatar", q["avatar"], px(ix), px(y), px(20)))
        when, ww = _line(age(q["created"], now), False, 13, T["gray"], 100)
        name, nw = _line(q["name"] or q["handle"], "medium", 13, T["text"], max(40, iw - 28 - 8 - ww))
        ops.append(("lines", name, px(ix + 28), px(y)))
        ops.append(("lines", when, px(ix + 28 + nw + 8), px(y)))
        y += 20
        if q["spans"]:
            t = Text.make(q["spans"], 13, 18.2, False, T["text"], T["blue"], iw, card.MAX_LINES_QUOTE, T["blue"])
            ops.append(("lines", t, px(ix), px(y + 8)))
            y += 8 + t.height
        if q["media"]:
            y += 8
            y += media(q["media"], ix, y, iw)
        if q.get("quote") and depth > 0:
            y += 8
            y += quote(q["quote"], ix, y, iw, depth - 1)
        if q.get("card"):
            y += 8
            y += attachment(q["card"], ix, y, iw)
        y += 12
        ops[at] = ("box", px(x), px(top), px(w), px(y - top), px(RADIUS), T["line"], T["card"])
        return y - top

    def attachment(c: dict, x: float, y: float, w: float) -> float:
        return passage(c, x, y, w) if c.get("selection") else article(c, x, y, w)

    def article(c: dict, x: float, y: float, w: float) -> float:
        """An article or link: its cover fading into the card, where it is
        from, its title, subtitle and opening lines over the bottom, and the
        save button."""
        ih = w / 1.91 if c.get("image") else 0
        tx, tw = x + 16, w - 32 - 40
        rows = []   # (Text, x offset, height, gap above)
        logo = c.get("logo")
        if c["site"]:
            st, _ = _line(c["site"].upper(), "semibold", 11, ON_CARD_DIM, tw - (28 if logo else 0))
            rows.append((st, 28 if logo else 0, 20, 0))
        if c["title"]:
            rows.append((_clip(c["title"], 17, 24, "semibold", ON_CARD, tw, 2), 0, None, 8))
        if c["text"]:
            rows.append((_clip(c["text"], 13, 20, False, ON_CARD_DIM, tw, 1), 0, None, 8))
        if c.get("excerpt"):
            rows.append((_clip(c["excerpt"], 13, 20, False, ON_CARD, tw, 2), 0, None, 8))
        lead = ih * 0.68 if ih else 16
        body = sum((h or t.height) + (gap if i else 0) for i, (t, _, h, gap) in enumerate(rows))
        h = lead + max(body, 32) + 16
        ops.append(("postcard", c.get("image"), px(x), px(y), px(w), px(h), px(ih), px(RADIUS), c.get("tint")))
        cy = y + lead
        for i, (t, dx, rh, gap) in enumerate(rows):
            cy += gap if i else 0
            if dx:
                ops.append(("fill", px(tx), px(cy), px(20), px(20), px(4), (255, 255, 255, 255)))
                ops.append(("image", logo, px(tx), px(cy), px(20), px(20), px(4), (True,) * 4, "cover"))
            ops.append(("lines", t, px(tx + dx), px(cy)))
            cy += rh or t.height
        bx, by = x + w - 48, y + h - 48
        ops.append(("fill", px(bx), px(by), px(32), px(32), px(8), (182, 182, 182, 51)))
        ops.append(("icon", "s_save", px(bx + 7), px(by + 7), px(18), ON_CARD))
        return h

    def passage(c: dict, x: float, y: float, w: float) -> float:
        """A passage picked out of an article: a quote mark, the passage in
        serif, and where it is from beside the save button."""
        t = _clip(c["selection"], 17, 28, "serif", ON_CARD, w - 40, 12)
        h = 20 + 24 + 12 + t.height + 24 + 20 + 20
        ops.append(("box", px(x), px(y), px(w), px(h), px(RADIUS), None, PASSAGE))
        ops.append(("icon", "s_quote", px(x + 20), px(y + 24.5), px(24), ON_CARD))
        ops.append(("lines", t, px(x + 20), px(y + 56)))
        ry = y + 56 + t.height + 24
        if c["site"]:
            st, _ = _line(c["site"], False, 13, ON_CARD_DIM, w - 40 - 40)
            ops.append(("lines", st, px(x + 20), px(ry)))
        ops.append(("icon", "s_save", px(x + w - 37), px(ry + 1), px(18), ON_CARD))
        return h

    # header
    y0 = PAD
    ops.append(("avatar", post["avatar"], px(PAD), px(y0), px(AVATAR)))
    hx = PAD + AVATAR + 12
    more = PAD + W - 18
    ops.append(("icon", "s_more", px(more), px(y0 + (2 if feed else 10)), px(16), T["more"]))
    room = more - 10
    if post.get("subscribe") and not feed:
        t, tw = _line("Subscribe", "semibold", 13, ON_CARD, 200)
        bx = more - 10 - (tw + 24)
        ops.append(("fill", px(bx), px(y0 + 2), px(tw + 24), px(32), px(8), _rgba(T["blue"])))
        ops.append(("lines", t, px(bx + 12), px(y0 + 8)))
        room = bx - 12
    when, ww = _line(age(post["created"], now), False, 13, T["gray"], 100)
    name, nw = _line(post["name"] or post["handle"], "medium", 15, T["text"], max(60, room - hx - 8 - ww))
    ny = y0 + 1 if feed else y0 - 2
    ops.append(("lines", name, px(hx), px(ny)))
    # a smaller face set in the same line: down a little, to share the baseline
    ops.append(("lines", when, px(hx + nw + 8), px(ny + 0.6)))
    if feed:
        x0, y, gap = hx, ny + 20, 4
    else:
        handle, _ = _line(f"@{post['handle']}", False, 13, T["gray"], room - hx)
        ops.append(("lines", handle, px(hx), px(y0 + 19)))
        x0, y, gap = PAD, y0 + 38, 12
    w = PAD + W - x0

    if post.get("replying_to"):
        y += gap
        ops.append(("text", f"Replying to @{post['replying_to']}", "regular", 13, px(x0), px(y), T["gray"], T["blue"]))
        y += 16
        gap = 4
    if post["spans"]:
        y += gap
        t = Text.make(post["spans"], 15, 21, False, T["text"], T["blue"], w, max_lines, T["blue"])
        ops.append(("lines", t, px(x0), px(y)))
        y += t.height
    if post["media"]:
        # a row of several sits a little further down than one alone
        y += 24 if len(post["media"]) > 1 else 12
        y += media(post["media"], x0, y, w)
    if post.get("quote"):
        y += 12
        y += quote(post["quote"], x0, y, w, 9)
    if post.get("card"):
        y += 12
        y += attachment(post["card"], x0, y, w)

    if stats:
        y += 12 if feed else 18
        gx = x0
        for kind, n in (("s_like", post["likes"]), ("s_reply", post["replies"]), ("s_restack", post["reposts"])):
            ops.append(("icon", kind, px(gx), px(y + 1), px(18), T["gray"]))
            gx += 18
            if n:
                t, tw = _line(card.count(n), False, 13, T["gray"], 100)
                ops.append(("lines", t, px(gx + 8), px(y)))
                gx += 8 + tw
            gx += 24
        ops.append(("icon", "s_share", px(gx), px(y + 1), px(18), T["gray"]))
        y += 20
        if not feed:
            y += 18
            ops.append(("box", px(x0), px(y), px(w), S, 0, None, T["line"]))
            y += 17
            tally = " · ".join(s for s in (_tally(post["likes"], "Like"), _tally(post["reposts"], "Restack")) if s)
            if tally:
                t, _ = _line(tally, False, 13, T["gray"], w / 2)
                ops.append(("lines", t, px(x0), px(y)))
            t, tw = _line(stamp(post["created"], now), False, 13, T["gray"], w / 2)
            ops.append(("lines", t, px(x0 + w - tw), px(y)))
            y += 20
    y += PAD
    h = px(y)
    L.height = h + (h % 2)
    return card._lead_cell(L)
