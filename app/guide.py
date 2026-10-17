"""The guide: /help, every flag on one card, and /workflows, the combinations
the flags alone don't make obvious. Each page is drawn as an image for a chat
app's preview, served as a web page to a browser and as plain text to anything
else, all from the content here.

The look is the site's own: deep plum, pink and lavender, Nunito (app/fonts,
OFL), with sparkles and hearts drawn as shapes, since the face has no glyphs
for them.
"""
from __future__ import annotations

import html as _html
import io
import math
import random
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT = Path(__file__).parent / "fonts" / "Nunito[wght].ttf"


@dataclass
class Item:
    chip: str                  # a flag or a link, drawn as a pill
    words: str
    title: str | None = None   # a workflow's goal; a flag row has none


@dataclass
class Section:
    heading: str
    note: str | None
    items: list[Item]


@dataclass
class Page:
    path: str
    title: str                     # the chat app's embed title
    blurb: str                     # and its description
    tagline: str
    steps: list[tuple[str, str]]   # (what to do, an example)
    sections: list[Section]
    footer: tuple[str, str]        # (words, the link)


HELP = Page(
    "help", "embed flags",
    "Swap x.com for {host} in a post link, then add flags after ? joined by &. Every flag is in the image; "
    "combos are at {host}/workflows",
    "x.com links that unfurl as the post itself",
    [
        ("Swap x.com for {host} in a post link", "{host}/user/status/123"),
        ("Add flags after ?, joined by &", "{host}/user/status/123?media&q=0"),
        ("A share link already ends in ?s=20: add flags with &", ".../status/123?s=20&media"),
        ("Link a profile for their latest posts", "{host}/user"),
    ],
    [
        Section("Videos", "counted from the top: v1 is the first video in the post or its quote, v2 the next", [
            Item("v2=image", "a still, no playback"),
            Item("v2=mute", "plays without sound"),
            Item("v2=sound", "this one is heard and sets the length"),
            Item("v2=hide", "left out"),
            Item("v=image", "every video; a numbered flag still wins (v=image&v1=video)"),
        ]),
        Section("What's shown", None, [
            Item("media", "only the pictures and video; a gif alone comes out as just the gif"),
            Item("quote", "start from the quoted post, leaving out the one quoting it"),
            Item("q=0 .. q=3", "how many quote levels (default 1; q=0 drops the quote)"),
            Item("lines=5", "cut the text after 5 lines"),
            Item("stats=0", "no reply, repost and like row"),
            Item("theme=dim", "or theme=light"),
            Item("lang=ja", "translate the text: a code like es, or a name like japanese"),
        ]),
        Section("Timing", "a video over {long}s goes out as x.com's own file, unless it's clipped shorter", [
            Item("start=1:30&end=1:45", "clip the video (90, 1:30 and 1m30s all work)"),
            Item("render", "draw a long video anyway, up to {forced}"),
        ]),
        Section("Profiles", None, [
            Item("{host}/user", "their {posts} latest posts"),
            Item("top", "the {posts} most liked of their last {top_from} posts instead"),
        ]),
        Section("Other", None, [
            Item("plain", "x.com's own video instead of the drawing"),
            Item("raw", "open the drawn file itself, to try flags in a browser"),
            Item("cancel", "stop a render started by mistake"),
            Item("help", "this guide"),
        ]),
    ],
    ("More combos, step by step", "{host}/workflows"),
)

WORKFLOWS = Page(
    "workflows", "embed workflows",
    "Flag combos for {host}: one video out of a quote, stills, clips, gifs, profiles. Every flag on its own is at {host}/help",
    "flag combos that aren't obvious from the flags alone",
    [
        ("Each combo goes on the end of a post link", "{host}/user/status/123?quote&media"),
        ("Videos count from the top post down through its quotes", "v1, v2, v3 ..."),
    ],
    [
        Section("Picking videos", None, [
            Item("?quote&q=0&media&v1=hide", "quote starts the count at the quoted post, so its videos are v1 and v2; q=0 leaves out "
                 "anything it quotes, media drops the text, v1=hide the first video", "Just the second video of a quoted post"),
            Item("?quote&q=0&media", "the quoted post's media and nothing else, not even the post quoting it", "Only the quoted post's video"),
            Item("?v2=sound", "when the post and its quote have a video each: the quote's is heard and sets the length, "
                 "the post's own loops silently", "The post's video with the quote's sound"),
            Item("?v1=hide", "the rest of the post stays as it is", "Hide one video, keep the rest"),
            Item("?v=image", "every video becomes its still, so the post comes out as an image", "A still screenshot of a video post"),
        ]),
        Section("Trimming the post", None, [
            Item("?q=0", "the post alone, without the post it quotes", "Drop the quote"),
            Item("?quote", "the quoted post drawn whole, as if it had been linked", "Only the quoted post"),
            Item("?q=3", "three quotes deep, each inside the post quoting it", "A chain of quotes"),
            Item("?lines=5&stats=0", "five lines of text and no counts under it", "A long post kept short"),
            Item("?lang=en", "the text in English, with \"Translated from ...\" under it; a name works too (lang=english)",
                 "Read it in your language"),
        ]),
        Section("Gifs and timing", None, [
            Item("?media", "on a post whose only media is a gif: Discord shows the gif by itself, nothing around it", "Just the gif"),
            Item("?start=2:10&end=2:25", "a clip under {long}s is drawn even when the whole video is longer", "A few seconds of a long video"),
            Item("?render", "draws a video over {long}s anyway, up to {forced}; the preview can come up short until it's done",
                 "A long video, drawn anyway"),
            Item("?plain", "x.com's own video file and player instead of the drawing", "The original video"),
        ]),
        Section("Profiles", None, [
            Item("{host}/user", "their {posts} latest posts, in two columns", "Someone's latest posts"),
            Item("{host}/user?top", "the {posts} most liked of their last {top_from} posts", "Their best posts"),
            Item("{host}/user?q=0&theme=light", "the flags for the look work on a profile too", "A lighter, plainer profile"),
        ]),
        Section("Before and after posting", None, [
            Item("?media&raw", "open it in a browser to see the drawn file before you post the link", "Try flags first"),
            Item("?cancel", "post the same link with cancel to stop a render started by mistake", "Stop a render"),
            Item("?help", "on any link, or /help on its own", "Every flag"),
        ]),
    ],
    ("Every flag on its own", "{host}/help"),
)

PAGES = {p.path: p for p in (HELP, WORKFLOWS)}


def _fill(p: Page, v: dict) -> Page:
    """The page with {host} and the rest filled in."""
    f = lambda s: s.format(**v) if s else s
    return Page(p.path, f(p.title), f(p.blurb), f(p.tagline), [(f(a), f(b)) for a, b in p.steps],
                [Section(f(s.heading), f(s.note), [Item(f(i.chip), f(i.words), f(i.title)) for i in s.items]) for s in p.sections],
                (f(p.footer[0]), f(p.footer[1])))


# ------------------------------------------------------------------- text ---

def text(page: Page, **v) -> str:
    p = _fill(page, v)
    out = [f"{v['host']} {p.title}", "", p.tagline, ""]
    for a, b in p.steps:
        out += [a + ":", f"    {b}"]
    for s in p.sections:
        out += ["", s.heading.upper()]
        if s.note:
            out.append(f"  ({s.note})")
        for i in s.items:
            if i.title:
                out += [f"  {i.title}", f"    {i.chip}", f"    {i.words}"]
            else:
                out.append(f"  {i.chip:<20} {i.words}")
    out += ["", f"{p.footer[0]}: {p.footer[1]}", ""]
    return "\n".join(out)


# ------------------------------------------------------------------- html ---

CSS = """
@font-face{font-family:Nunito;src:url(/guide/nunito.ttf) format("truetype");font-weight:200 1000;font-display:swap}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(60rem 40rem at 90% -10%,#5a2357 0,transparent 60%),radial-gradient(50rem 40rem at -10% 110%,#3b2a6e 0,transparent 60%),#1b0b24;
 color:#fdeef6;font:600 16px/1.5 Nunito,ui-rounded,system-ui,sans-serif;min-height:100vh}
main{max-width:1080px;margin:0 auto;padding:48px 16px 64px}
h1{margin:0;font-size:clamp(32px,7vw,52px);font-weight:900;line-height:1.1;background:linear-gradient(90deg,#ff9fd0,#c7b2ff);
 -webkit-background-clip:text;background-clip:text;color:transparent;overflow-wrap:anywhere}
h1 small{display:block;font-size:.42em;letter-spacing:.06em;text-transform:uppercase;-webkit-text-fill-color:#c7b2ff}
.tag{color:#bfa5cc;font-weight:700;font-size:19px;margin:8px 0 28px}
.card{background:#2e183eee;border:1.5px solid #5c3476;border-radius:22px;padding:22px}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:18px 28px;margin-bottom:22px}
.step{display:flex;gap:12px;align-items:flex-start}
.step b{flex:none;width:28px;height:28px;border-radius:50%;background:#ff9fd0;color:#2a1236;font-weight:900;display:grid;place-items:center;font-size:15px}
.step p{margin:0;font-weight:800}
code{font:800 15px/1.4 Nunito,ui-rounded,system-ui,sans-serif;color:#ff9fd0;background:#40204f;border-radius:999px;padding:3px 11px;
 display:inline-block;margin-top:6px;overflow-wrap:anywhere;max-width:100%}
.cols{columns:2 420px;column-gap:22px}
section{break-inside:avoid;margin-bottom:22px}
h2{margin:0 0 6px;font-size:14px;font-weight:900;letter-spacing:.14em;text-transform:uppercase;color:#c7b2ff}
h2::before{content:"\\2661";color:#ff9fd0;margin-right:8px;letter-spacing:0}
.note{color:#bfa5cc;font-weight:700;font-size:14px;margin:0 0 10px}
dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:10px 14px;align-items:baseline}
dt{margin:0}dt code{margin:0}dd{margin:0}
.recipe{padding:14px 0;border-top:1px solid #5c347688}.recipe:first-of-type{border-top:0;padding-top:4px}
.recipe h3{margin:0;font-size:17px;font-weight:800}
.recipe p{margin:6px 0 0;color:#bfa5cc}
footer{text-align:center;color:#bfa5cc;font-weight:700;font-size:17px}
footer a{text-decoration:none}footer a code{font-size:17px;padding:6px 16px;color:#c7b2ff}
@media (max-width:560px){dl{grid-template-columns:1fr;gap:2px}dd{margin-bottom:8px}}
"""


def page_html(page: Page, **v) -> str:
    p = _fill(page, v)
    e = lambda s: _html.escape(s, quote=True)
    steps = "".join(f'<div class="step"><b>{n}</b><div><p>{e(a)}</p><code>{e(b)}</code></div></div>' for n, (a, b) in enumerate(p.steps, 1))
    secs = []
    for s in p.sections:
        body = [f"<h2>{e(s.heading)}</h2>"]
        if s.note:
            body.append(f'<p class="note">{e(s.note)}</p>')
        if any(i.title for i in s.items):
            body += [f'<div class="recipe"><h3>{e(i.title or "")}</h3><code>{e(i.chip)}</code><p>{e(i.words)}</p></div>' for i in s.items]
        else:
            body.append("<dl>" + "".join(f"<dt><code>{e(i.chip)}</code></dt><dd>{e(i.words)}</dd>" for i in s.items) + "</dl>")
        secs.append(f'<section class="card">{"".join(body)}</section>')
    link = p.footer[1]
    href = "/" + link.split("/", 1)[1] if "/" in link else link
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#1b0b24">
<title>{e(p.title)} · {e(v['host'])}</title>
<style>{CSS}</style></head>
<body><main>
<h1><small>{e(p.title)}</small>{e(v['host'])}</h1>
<p class="tag">{e(p.tagline)}</p>
<div class="card steps">{steps}</div>
<div class="cols">{"".join(secs)}</div>
<footer>{e(p.footer[0])} <a href="{e(href)}"><code>{e(link)}</code></a></footer>
</main></body></html>"""


# -------------------------------------------------------------------- png ---

S = 2                          # device pixels per css pixel
WIDTH, MARGIN, GAP, PAD = 1080, 44, 22, 22
BG_TOP, BG_BOTTOM = (42, 18, 54), (23, 10, 32)
CARD, EDGE = (46, 24, 62, 236), (92, 52, 118, 255)
PINK, LAVENDER, CREAM, MUTED, CHIP, PLUM = "#ff9fd0", "#c7b2ff", "#fdeef6", "#bfa5cc", "#40204f", "#2a1236"

_fonts: dict[tuple[int, int], ImageFont.FreeTypeFont] = {}


def _font(size: float, weight: int) -> ImageFont.FreeTypeFont:
    key = (round(size * S), weight)
    f = _fonts.get(key)
    if f is None:
        f = _fonts[key] = ImageFont.truetype(str(FONT), key[0])
        f.set_variation_by_axes([weight])
    return f


def _wrap(s: str, f: ImageFont.FreeTypeFont, width: float) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        nxt = f"{cur} {word}" if cur else word
        if cur and f.getlength(nxt) > width:
            lines.append(cur)
            cur = word
        else:
            cur = nxt
    return lines + [cur] if cur else lines


def _wrap_chip(s: str, f: ImageFont.FreeTypeFont, width: float) -> list[str]:
    """A flag or link broken after & ? or / when it doesn't fit."""
    lines, cur = [], ""
    for piece in [p for p in re.split(r"(?<=[&?/])", s) if p]:
        if cur and f.getlength(cur + piece) > width:
            lines.append(cur)
            cur = piece
        else:
            cur += piece
    return lines + [cur] if cur else lines


def _aa(size: tuple[int, int], paint, k: int = 3) -> Image.Image:
    """An antialiased mask: `paint` draws on a k-times larger canvas."""
    big = Image.new("L", (size[0] * k, size[1] * k), 0)
    paint(ImageDraw.Draw(big), k)
    return big.resize(size, Image.LANCZOS)


def _rrect(im: Image.Image, box, r: float, fill, edge=None, width: float = 0) -> None:
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    outer = _aa((w, h), lambda d, k: d.rounded_rectangle((0, 0, w * k - 1, h * k - 1), radius=r * k, fill=255))
    if edge is not None and width:
        im.paste(Image.new("RGBA", (w, h), edge), (x0, y0), outer)
        inner = _aa((w, h), lambda d, k: d.rounded_rectangle((width * k, width * k, (w - width) * k - 1, (h - width) * k - 1),
                                                             radius=max(0, r - width) * k, fill=255))
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        layer.paste(Image.new("RGBA", (w, h), fill), (0, 0), inner)
        im.alpha_composite(layer, (x0, y0))
    else:
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        layer.paste(Image.new("RGBA", (w, h), fill), (0, 0), outer)
        im.alpha_composite(layer, (x0, y0))


def _shape(im: Image.Image, cx: float, cy: float, r: float, color, points) -> None:
    """A shape from `points(t)` on the unit square, centred at cx, cy."""
    n = max(2, int(r * 2) + 2)
    pts = [points(i / 96 * 2 * math.pi) for i in range(96)]
    mask = _aa((n, n), lambda d, k: d.polygon([((x + 1) / 2 * n * k, (y + 1) / 2 * n * k) for x, y in pts], fill=255), 4)
    layer = Image.new("RGBA", (n, n), color[:3] + (0,))
    layer.putalpha(Image.eval(mask, lambda a: a * color[3] // 255))
    x, y = int(cx - n / 2), int(cy - n / 2)
    if 0 <= x and 0 <= y and x + n <= im.width and y + n <= im.height:
        im.alpha_composite(layer, (x, y))


def _sparkle(t: float) -> tuple[float, float]:
    c, s = math.cos(t), math.sin(t)
    return c * abs(c) ** 2, s * abs(s) ** 2


def _heart(t: float) -> tuple[float, float]:
    x = 16 * math.sin(t) ** 3
    y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
    return x / 17, -y / 17 + 0.08


def _rgba(c: str, a: int = 255) -> tuple[int, int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), a


class _Canvas:
    """Measures when `im` is None, draws when it isn't: each block runs twice,
    once for the height and once to paint."""

    def __init__(self, im: Image.Image | None):
        self.im = im
        self.d = ImageDraw.Draw(im) if im is not None else None
        self.boxes: list[tuple[float, float, float, float]] = []   # where the content is, in css px

    def text(self, x: float, y: float, s: str, size: float, weight: int, color, lh: float) -> None:
        if self.d is None:
            return
        f = _font(size, weight)
        asc, desc = f.getmetrics()
        self.d.text((x * S, y * S + (lh * S - asc - desc) / 2), s, font=f, fill=color, anchor="la")

    def para(self, x: float, y: float, s: str, size: float, weight: int, color, width: float, lh: float) -> float:
        """Wrapped text; its height."""
        lines = _wrap(s, _font(size, weight), width * S)
        for i, line in enumerate(lines):
            self.text(x, y + i * lh, line, size, weight, color, lh)
        return len(lines) * lh

    def chip(self, x: float, y: float, s: str, width: float, size: float = 15, color=PINK) -> tuple[float, float]:
        """A flag or link as a pill; its width and height."""
        f = _font(size, 800)
        lines = _wrap_chip(s, f, (width - 22) * S)
        w = max(f.getlength(l) for l in lines) / S + 22
        lh = size * 1.45
        h = len(lines) * lh + 8
        if self.im is not None:
            _rrect(self.im, (x * S, y * S, (x + w) * S, (y + h) * S), min(h, lh + 8) / 2 * S, _rgba(CHIP))
            for i, l in enumerate(lines):
                self.text(x + 11, y + 4 + i * lh, l, size, 800, color, lh)
        return w, h

    def heading(self, x: float, y: float, s: str) -> float:
        if self.im is not None:
            _shape(self.im, (x + 7) * S, (y + 10) * S, 7 * S, _rgba(PINK), _heart)
            f = _font(14, 900)
            cx = (x + 22) * S
            for ch in s.upper():
                self.d.text((cx, (y + 3) * S), ch, font=f, fill=LAVENDER, anchor="la")
                cx += f.getlength(ch) + 2.2 * S
        return 22


def _steps(c: _Canvas, x: float, y: float, w: float, steps: list[tuple[str, str]]) -> float:
    cw = (w - 2 * PAD - 28) / 2
    top = y + PAD
    for row in range(0, len(steps), 2):
        hs = []
        for k, (a, b) in enumerate(steps[row:row + 2]):
            sx = x + PAD + k * (cw + 28)
            if c.im is not None:
                _shape(c.im, (sx + 14) * S, (top + 14) * S, 14 * S, _rgba(PINK), lambda t: (math.cos(t), math.sin(t)))
                num = str(row + k + 1)
                f = _font(15, 900)
                c.d.text(((sx + 14) * S, (top + 14) * S), num, font=f, fill=PLUM, anchor="mm")
            th = c.para(sx + 40, top + 2, a, 16, 800, CREAM, cw - 40, 23)
            _, ch = c.chip(sx + 40, top + 2 + th + 6, b, cw - 40)
            hs.append(2 + th + 6 + ch)
        top += max(hs) + 18
    return top - 18 + PAD - y


def _section(c: _Canvas, x: float, y: float, w: float, s: Section) -> float:
    iw = w - 2 * PAD
    top = y + PAD
    top += c.heading(x + PAD, top, s.heading) + 4
    if s.note:
        top += c.para(x + PAD, top, s.note, 14, 700, MUTED, iw, 20) + 8
    if any(i.title for i in s.items):
        for n, i in enumerate(s.items):
            if n:
                if c.im is not None:
                    c.d.line(((x + PAD) * S, (top + 8) * S, (x + w - PAD) * S, (top + 8) * S), fill=(92, 52, 118, 160), width=S)
                top += 18
            top += c.para(x + PAD, top, i.title, 17, 800, CREAM, iw, 24) + 6
            top += c.chip(x + PAD, top, i.chip, iw)[1] + 6
            top += c.para(x + PAD, top, i.words, 15, 600, MUTED, iw, 21)
    else:
        widths = [_Canvas(None).chip(0, 0, i.chip, iw)[0] for i in s.items]
        # a chip too wide for a column of its own has its words under it
        col = max((cw for cw in widths if cw <= iw * 0.36), default=0) + 12
        for n, i in enumerate(s.items):
            cw, ch = c.chip(x + PAD, top, i.chip, iw)
            if cw + 12 > col:
                # a chip wider than the column: its words go under it
                top += ch + 4 + c.para(x + PAD + 8, top + ch + 4, i.words, 15.5, 600, CREAM, iw - 8, 22) + 10
            else:
                th = c.para(x + PAD + col, top + 3, i.words, 15.5, 600, CREAM, iw - col, 22)
                top += max(ch, th + 3) + 10
        top -= 10
    return top + PAD - y


def _layout(c: _Canvas, p: Page, host: str) -> float:
    w = WIDTH - 2 * MARGIN
    y = MARGIN
    if c.im is not None:
        _title(c.im, MARGIN, y, host)
        _shape(c.im, (MARGIN + _font(50, 900).getlength(host) / S + 26) * S, (y + 18) * S, 13 * S, _rgba(PINK), _sparkle)
        _shape(c.im, (MARGIN + _font(50, 900).getlength(host) / S + 48) * S, (y + 40) * S, 7 * S, _rgba(LAVENDER), _sparkle)
    sub = f"{p.title}  ·  {p.tagline}"
    c.text(MARGIN + 2, y + 62, sub, 19, 700, MUTED, 26)
    c.boxes.append((MARGIN, y, MARGIN + max(_font(50, 900).getlength(host) / S + 60, _font(19, 700).getlength(sub) / S), y + 88))
    y += 62 + 26 + 22
    h = _steps(_Canvas(None), MARGIN, y, w, p.steps)
    c.boxes.append((MARGIN, y, MARGIN + w, y + h))
    if c.im is not None:
        _rrect(c.im, (MARGIN * S, y * S, (MARGIN + w) * S, (y + h) * S), 22 * S, CARD, EDGE, 1.5 * S)
    _steps(c, MARGIN, y, w, p.steps)
    y += h + GAP
    cw = (w - GAP) / 2
    cols = [y, y]
    for s in p.sections:
        k = 0 if cols[0] <= cols[1] else 1
        x = MARGIN + k * (cw + GAP)
        h = _section(_Canvas(None), x, cols[k], cw, s)
        c.boxes.append((x, cols[k], x + cw, cols[k] + h))
        if c.im is not None:
            _rrect(c.im, (x * S, cols[k] * S, (x + cw) * S, (cols[k] + h) * S), 22 * S, CARD, EDGE, 1.5 * S)
            _section(c, x, cols[k], cw, s)
        cols[k] += h + GAP
    y = max(cols) + 6
    f = _font(17, 700)
    words = p.footer[0]
    lw = f.getlength(words) / S
    chip_w = _Canvas(None).chip(0, 0, p.footer[1], w, 17, LAVENDER)[0]
    x = MARGIN + (w - lw - 14 - chip_w) / 2
    c.boxes.append((x - 30, y, x + lw + 14 + chip_w + 30, y + 34))
    if c.im is not None:
        _shape(c.im, (x - 20) * S, (y + 17) * S, 8 * S, _rgba(PINK), _sparkle)
        _shape(c.im, (x + lw + 14 + chip_w + 20) * S, (y + 17) * S, 8 * S, _rgba(PINK), _sparkle)
    c.text(x, y + 4, words, 17, 700, MUTED, 26)
    c.chip(x + lw + 14, y, p.footer[1], w, 17, LAVENDER)
    return y + 34 + MARGIN


def _title(im: Image.Image, x: float, y: float, s: str) -> None:
    """The host in a pink-to-lavender gradient."""
    f = _font(50, 900)
    asc, desc = f.getmetrics()
    w, h = int(f.getlength(s)) + 8, asc + desc + 8
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).text((2, 2), s, font=f, fill=255, anchor="la")
    a, b = _rgba(PINK), _rgba(LAVENDER)
    row = Image.new("RGBA", (w, 1))
    row.putdata([tuple(int(a[j] + (b[j] - a[j]) * i / max(1, w - 1)) for j in range(4)) for i in range(w)])
    im.paste(row.resize((w, h)), (int(x * S), int(y * S)), mask)


def _background(w: int, h: int, keep: list[tuple[float, float, float, float]]) -> Image.Image:
    """Plum, lighter at the top, with a pink glow up on the right, a lavender
    one down on the left, and sparkles strewn about, clear of the `keep` boxes."""
    k = 8
    sw, sh = max(1, w // k), max(1, h // k)
    bg = Image.new("RGBA", (sw, sh))
    bg.putdata([tuple(int(BG_TOP[j] + (BG_BOTTOM[j] - BG_TOP[j]) * yy / max(1, sh - 1)) for j in range(3)) + (255,)
                for yy in range(sh) for _ in range(sw)])
    glow = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(glow)
    d.ellipse((sw * 0.55, -sh * 0.25, sw * 1.25, sh * 0.35), fill=(255, 120, 190, 70))
    d.ellipse((-sw * 0.3, sh * 0.6, sw * 0.45, sh * 1.2), fill=(150, 120, 255, 60))
    bg.alpha_composite(glow.filter(ImageFilter.GaussianBlur(max(sw, sh) / 10)))
    im = bg.resize((w, h), Image.BICUBIC)
    rnd = random.Random(7)
    for _ in range(int(w * h / 90000)):
        r = rnd.choice((3, 4, 5, 7, 9)) * S
        color = _rgba(rnd.choice((PINK, LAVENDER, CREAM)), rnd.randint(40, 120))
        x, y = rnd.uniform(0, w), rnd.uniform(0, h)
        if not any(x0 * S - r < x < x1 * S + r and y0 * S - r < y < y1 * S + r for x0, y0, x1, y1 in keep):
            _shape(im, x, y, r, color, _sparkle)
    return im


def png(page: Page, **v) -> bytes:
    p = _fill(page, v)
    measure = _Canvas(None)
    h = _layout(measure, p, v["host"])
    im = _background(WIDTH * S, int(h * S), measure.boxes)
    _layout(_Canvas(im), p, v["host"])
    out = io.BytesIO()
    im.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()
