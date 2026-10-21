"""A profile drawn as one image: the header x.com shows (banner, avatar, name,
bio, counts), then several of the user's own posts, each drawn as card.py
draws a post, in two columns. Videos are drawn as their stills, so it is
always a png. A Substack profile is the same, with its notes for posts.
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass

from PIL import Image, ImageDraw

from . import card

POSTS = int(os.environ.get("PROFILE_POSTS", 6))
RECENT_FROM = 60     # timeline entries read for the latest, reposts dropped
TOP_FROM = 60        # and for top, which picks the most liked of them
LINES = 10           # a post's text is cut here, with "Show more"
COLS = 2
BANNER = 180         # css px
AVATAR = 134


@dataclass
class Board:
    width: int
    height: int
    theme: dict
    header: card.Layout
    posts: list[tuple[int, int, card.Layout]]   # where each post goes, in device px
    rules: list[tuple[int, int, int, int]]      # the lines between posts and columns


def pick(posts: list[dict], top: bool) -> list[dict]:
    """The posts to show: the latest, or the most liked."""
    if top:
        posts = sorted(posts, key=lambda p: p["likes"], reverse=True)
    return posts[:POSTS]


def _still(post: dict, depth: int) -> dict:
    """A copy with every video drawn as its still, quoted `depth` deep. The
    post is the cached one, so it is never changed in place."""
    q = post.get("quote")
    return dict(post, media=[m if m["kind"] == "photo" else dict(m, still=True) for m in post["media"]],
                quote=_still(q, depth - 1) if q and depth > 0 else None)


def layout(user: dict, posts: list[dict], theme: str = "dark", stats: bool = True, lines: int = LINES,
           depth: int = 1, top: bool = False, pool: int = 0) -> Board:
    """Each post goes under the shorter column, so the two end up about even.
    `pool` is how many posts the top ones were picked from."""
    T = card.palette(theme, user.get("site"))
    S = card.S
    colw = card.COL * S
    width = COLS * colw + (COLS - 1) * S
    header = _header(user, width, T, top, len(posts), pool)
    heights = [header.height] * COLS
    placed, rules = [], []
    for p in posts:
        L = card.layout(_still(p, depth), theme, stats, max_lines=lines)
        k = heights.index(min(heights))
        x = k * (colw + S)
        if heights[k] > header.height:
            rules.append((x, heights[k], x + colw, heights[k] + S))
            heights[k] += S
        placed.append((x, heights[k], L))
        heights[k] += L.height
    height = max(heights)
    height += height % 2
    for k in range(1, COLS):
        x = k * (colw + S) - S
        rules.append((x, header.height, x + S, height))
    return Board(width, height, T, header, placed, rules)


def _header(user: dict, width: int, T: dict, top: bool, shown: int, pool: int) -> card.Layout:
    S = card.S
    F = card.fonts()
    L = card.Layout(width, 0, T)
    ops = L.ops

    def px(v):
        return int(round(v * S))

    W = width / S
    if user["banner"]:
        ops.append(("image", user["banner"], 0, 0, width, px(BANNER), 0, (False,) * 4, "cover"))
    else:
        ops.append(("box", 0, 0, width, px(BANNER), 0, T["line"], T["line"]))
    # the avatar overlaps the banner's foot, ringed in the background colour
    ay, ring = BANNER - AVATAR / 2, 4
    ops.append(("box", px(16 - ring), px(ay - ring), px(AVATAR + 2 * ring), px(AVATAR + 2 * ring), px(AVATAR / 2 + ring), T["bg"], T["bg"]))
    ops.append(("avatar", user["avatar"], px(16), px(ay), px(AVATAR)))
    y = BANNER + AVATAR / 2 + 12
    name, nw = card._line(user["name"], True, 20, T["text"], W - 32 - 26)
    ops.append(("lines", name, px(16), px(y + 2)))
    if user["verified"]:
        ops.append(("icon", "verified", px(16 + nw + 4), px(y + 3), px(20), T["blue"]))
    y += 26
    ops.append(("text", f"@{user['handle']}", "regular", 15, px(16), px(y), T["gray"]))
    y += 32
    if user["bio"]:
        bio = card.Text.make(user["bio"], 15, 20, False, T["text"], T["blue"], W - 32, 6, T["blue"])
        ops.append(("lines", bio, px(16), px(y)))
        y += bio.height + 12
    x = 16
    # a site other than x.com names its own counts; a figure it gives as text is shown as is
    counts = user.get("counts") or ((user["following"], "Following"), (user["followers"], "Followers"), (user["posts"], "posts"))
    for n, label in counts:
        num = n if isinstance(n, str) else card.count(n) or "0"
        ops.append(("text", num, "bold", 15, px(x), px(y), T["text"]))
        x += F.font("bold", 15 * S).getlength(num) / S + 4
        ops.append(("text", label, "regular", 15, px(x), px(y), T["gray"]))
        x += F.font("regular", 15 * S).getlength(label) / S + 20
    y += 36
    # a tab, as x.com marks the one open
    tab = user.get("tab", "Posts")
    tab = f"Top {tab.lower()}" if top else tab
    note = f"the {shown} most liked of their last {pool}" if top else f"their {shown} latest"
    tw = F.font("bold", 15 * S).getlength(tab) / S
    ops.append(("text", tab, "bold", 15, px(16), px(y), T["text"]))
    ops.append(("text", note, "regular", 15, px(16 + tw + 12), px(y), T["gray"]))
    y += 32
    ops.append(("box", px(16), px(y - 4), px(tw), px(4), px(2), T["blue"], T["blue"]))
    ops.append(("box", 0, px(y), width, S, 0, T["line"], T["line"]))
    L.height = px(y) + S
    return L


def render(b: Board) -> bytes:
    card.prefetch([b.header] + [L for _, _, L in b.posts])
    im = Image.new("RGBA", (b.width, b.height), b.theme["bg"])
    im.paste(card.draw(b.header, False), (0, 0))
    for x, y, L in b.posts:
        im.paste(card.draw(L, False), (x, y))
    d = ImageDraw.Draw(im)
    for x0, y0, x1, y1 in b.rules:
        d.rectangle((x0, y0, x1 - 1, y1 - 1), fill=b.theme["line"])
    out = io.BytesIO()
    im.save(out, "PNG", compress_level=6)
    return out.getvalue()
