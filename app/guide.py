"""The guide: /help, every flag, and /workflows, the combinations the flags
alone don't make obvious. Each page is drawn as an image for a chat app's
preview, served as a web page to a browser and as plain text to anything
else, all from the content here.

The look is an old fansite's: a narrow page on a striped background, a glossy
header with Kasane Teto and a BETA sticker, and one forum table. The image is
drawn at 1x in DejaVu Sans, the Verdana of its day, and Silkscreen, then
doubled pixel for pixel. Fonts in app/fonts (DejaVu and OFL licences), Teto
in app/art.
"""
from __future__ import annotations

import html as _html
import io
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
TETO = HERE / "art" / "teto.png"
SILKSCREEN = HERE / "fonts" / "Silkscreen-Regular.ttf"
FILES = {"teto.png": (TETO, "image/png"), "silkscreen.ttf": (SILKSCREEN, "font/ttf")}   # what the page loads from /guide/
PINK = "#d4185e"   # the embed's colour bar


@dataclass
class Item:
    chip: str      # a flag, a combo or a link
    words: str     # what it does, in a few words


@dataclass
class Page:
    path: str
    title: str             # the chat app's embed title
    blurb: str             # and its description
    intro: str             # "" for none, and no example either
    example: str
    heading: str           # the table's
    items: list[Item]
    more: tuple[str, str]  # (words, the other page)


def _items(*pairs: tuple[str, str]) -> list[Item]:
    return [Item(c, w) for c, w in pairs]


HELP = Page(
    "help", "embed flags", "welcome to super awesomeyurisex dot com embed help page ! :D",
    "Swap x.com for {host}, add flags after ?",
    "{host}/user/status/123?media&q=0",
    "Flags",
    _items(("media", "media only"), ("quote", "the quoted post only"), ("q=0..3", "quote depth"),
           ("v2=image", "2nd video as a still (or mute, sound, hide)"), ("v=image", "every video"),
           ("lines=5", "cut the text"), ("stats=0", "no counts"), ("theme=light", "or dim"), ("lang=ja", "translate"),
           ("start=1:30&end=1:45", "clip"), ("render", "draw videos over {long}s"), ("plain", "x.com's own video"),
           ("raw", "open the file"), ("top", "a profile's most liked"), ("cancel", "stop a render"), ("help", "this page"),
           ("workflows", "the combos page")),
    ("More combos:", "{host}/workflows"),
)

WORKFLOWS = Page(
    "workflows", "embed workflows", "secret super awesomeyurisex dot com combo moves !! :3",
    "", "",
    "Combos",
    _items(("?quote&q=0&media&v1=hide", "2nd video of the quoted post"), ("?quote&q=0&media", "the quoted post's video"),
           ("?v2=sound", "post's video, quote's sound"), ("?v=image", "a still, no video"), ("?q=0", "drop the quote"),
           ("?lines=5&stats=0", "short and clean"), ("?start=2:10&end=2:25", "a clip of a long video"),
           ("?media", "just the gif"), ("?media&raw", "preview in a browser"), ("{host}/user?top", "someone's best posts")),
    ("Every flag:", "{host}/help"),
)

PAGES = {p.path: p for p in (HELP, WORKFLOWS)}


def _fill(p: Page, v: dict) -> Page:
    """The page with {host} and the rest filled in."""
    f = lambda s: s.format(**v)
    return Page(p.path, f(p.title), f(p.blurb), f(p.intro), f(p.example), p.heading,
                [Item(f(i.chip), f(i.words)) for i in p.items], (f(p.more[0]), f(p.more[1])))


# ------------------------------------------------------------------- text ---

def text(page: Page, **v) -> str:
    p = _fill(page, v)
    rows = [f"  {i.chip:<26} {i.words}" for i in p.items]
    intro = [p.intro, f"    {p.example}", ""] if p.intro else []
    return "\n".join([f"{v['host']} {p.title}", "", *intro, p.heading.upper(), *rows, "",
                      f"{p.more[0]} {p.more[1]}", ""])


# ------------------------------------------------------------------- html ---

CSS = """
@font-face{font-family:Silkscreen;src:url(/guide/silkscreen.ttf) format("truetype")}
body{margin:0;padding:12px 8px 24px;background:#fbe3ec repeating-linear-gradient(45deg,#fbe3ec 0 4px,#f6d0de 4px 8px);
 font:11px/1.45 Verdana,"DejaVu Sans",Tahoma,Geneva,sans-serif;color:#222}
#wrap{max-width:520px;margin:0 auto;background:#fff;border:1px solid #9e2a52;box-shadow:3px 3px 0 #e9a9c2;padding-bottom:4px}
a{color:#0033cc}
#head{position:relative;text-align:center;padding:10px 0 14px;background:linear-gradient(#ff86b8,#d4185e);border-bottom:1px solid #7a0f35}
#head:before{content:"";position:absolute;left:0;right:0;top:0;height:50%;background:rgba(255,255,255,.22)}
#head img{position:relative;height:150px;display:block;margin:0 auto 4px}
#head h1{position:relative;margin:0;font:bold 26px Verdana,"DejaVu Sans",sans-serif;color:#fff;text-shadow:1px 1px 0 #8a0f3e,2px 2px 0 #8a0f3e;overflow-wrap:anywhere}
.beta{display:inline-block;vertical-align:top;margin:4px 0 0 6px;padding:1px 4px;font:8px Silkscreen,monospace;color:#fff;
 background:linear-gradient(#ffb347,#ff7f00);border:1px solid #c45a00;text-shadow:none}
.intro{text-align:center;margin:12px 10px;font-weight:bold}
.codebox{display:inline-block;margin-top:5px;padding:2px 5px;background:#fffbe0;border:1px dashed #e0bf55;font-weight:normal;overflow-wrap:anywhere}
table{width:calc(100% - 20px);margin:0 10px;border-collapse:collapse;border:1px solid #c94a7c}
td{padding:4px 6px;border-bottom:1px solid #f2c4d6;vertical-align:top}
tr:nth-child(even) td{background:#fff2f7}
td.tcat{background:linear-gradient(#ec5e95,#b51b52);color:#fff;font-weight:bold;padding:3px 8px;text-shadow:1px 1px 0 #7a0f35;border:0}
td.flag{border-right:1px solid #f2c4d6;white-space:nowrap;width:1%}
code{font:bold 11px "Courier New","DejaVu Sans Mono",monospace;color:#b0003a}
.codebox code{color:#8a1538}
.gap{height:12px}
.more{text-align:center;margin:12px 10px 10px}
@media (max-width:420px){td.flag{white-space:normal}}
"""


def page_html(page: Page, **v) -> str:
    p = _fill(page, v)
    e = lambda s: _html.escape(s, quote=True)
    rows = "".join(f'<tr><td class="flag"><code>{e(i.chip)}</code></td><td>{e(i.words)}</td></tr>' for i in p.items)
    words, link = p.more
    return f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(p.heading)} :: {e(v['host'])}</title>
<style>{CSS}</style></head>
<body><div id="wrap">
<div id="head"><img src="/guide/teto.png" alt="Kasane Teto with a baguette"><h1>{e(v['host'])}<span class="beta">BETA</span></h1></div>
{f'<div class="intro">{e(p.intro)}<br><span class="codebox"><code>{e(p.example)}</code></span></div>' if p.intro else '<div class="gap"></div>'}
<table><tr><td class="tcat" colspan=2>{e(p.heading)}</td></tr>{rows}</table>
<div class="more"><b>{e(words)}</b> <a href="/{e(link.split("/", 1)[-1])}">{e(link)}</a></div>
</div></body></html>"""


# -------------------------------------------------------------------- png ---

W, MARGIN = 600, 40            # the image, and the stripes either side of the page
PAGE_W = W - 2 * MARGIN
GUT, LH = 10, 15               # gutter, line height
TETO_H = 150
C = {
    "stripes": ("#fbe3ec", "#f6d0de"), "page": "#ffffff", "edge": "#9e2a52", "shadow": "#e9a9c2",
    "banner": ("#ff86b8", "#d4185e"), "banner_ink": "#8a0f3e",
    "head": ("#ec5e95", "#b51b52"), "head_ink": "#7a0f35",
    "alt": ("#fff2f7", "#ffffff"), "cell": "#f2c4d6", "table": "#c94a7c",
    "text": "#222222", "flag": "#b0003a", "link": "#0033cc",
    "code": "#fffbe0", "code_edge": "#e0bf55", "code_ink": "#8a1538",
}
_FILES = {"sans": "DejaVuSans.ttf", "bold": "DejaVuSans-Bold.ttf", "mono": "DejaVuSansMono-Bold.ttf", "pixel": "Silkscreen-Regular.ttf"}
_fonts: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _f(kind: str, size: int = 11) -> ImageFont.FreeTypeFont:
    f = _fonts.get((kind, size))
    if f is None:
        # basic layout: raqm's subpixel advances leave uneven gaps in unsmoothed text
        f = _fonts[(kind, size)] = ImageFont.truetype(str(HERE / "fonts" / _FILES[kind]), size, layout_engine=ImageFont.Layout.BASIC)
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


def _rgb(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


class _Canvas:
    """Measures when `im` is None, draws when it isn't: each block runs twice,
    once for the height and once to paint. Text is drawn without smoothing,
    as old screens showed it."""

    def __init__(self, im: Image.Image | None):
        self.im = im
        self.d = ImageDraw.Draw(im) if im is not None else None
        if self.d is not None:
            self.d.fontmode = "1"

    def text(self, x: int, y: int, s: str, font, fill, shadow=None) -> None:
        if self.d is None:
            return
        if shadow:
            self.d.text((x + 1, y + 1), s, font=font, fill=shadow)
        self.d.text((x, y), s, font=font, fill=fill)

    def rect(self, box, fill=None, outline=None) -> None:
        if self.d is not None:
            self.d.rectangle(box, fill=fill, outline=outline)

    def line(self, box, fill) -> None:
        if self.d is not None:
            self.d.line(box, fill=fill)

    def grad(self, box, top: str, bottom: str, gloss: bool = False) -> None:
        if self.im is None:
            return
        x0, y0, x1, y1 = box
        h = y1 - y0
        a, b = _rgb(top), _rgb(bottom)
        col = Image.new("RGB", (1, h))
        col.putdata([tuple(int(a[j] + (b[j] - a[j]) * i / max(1, h - 1)) for j in range(3)) for i in range(h)])
        self.im.paste(col.resize((x1 - x0, h)), (x0, y0))
        if gloss:
            self.im.paste((255, 255, 255), (x0, y0, x1, y0 + h // 2), Image.new("L", (x1 - x0, h // 2), 56))

    def code(self, x: int, y: int, s: str) -> int:
        """A link in a dashed yellow box, centred on x; its height."""
        f = _f("mono")
        w, h = int(f.getlength(s)) + 10, LH + 5
        x -= w // 2
        if self.d is not None:
            self.rect((x, y, x + w - 1, y + h - 1), fill=C["code"])
            for k in range(0, w, 4):
                self.line((x + k, y, min(x + k + 1, x + w - 1), y), C["code_edge"])
                self.line((x + k, y + h - 1, min(x + k + 1, x + w - 1), y + h - 1), C["code_edge"])
            for k in range(0, h, 4):
                self.line((x, y + k, x, min(y + k + 1, y + h - 1)), C["code_edge"])
                self.line((x + w - 1, y + k, x + w - 1, min(y + k + 1, y + h - 1)), C["code_edge"])
            self.text(x + 5, y + 2, s, f, C["code_ink"])
        return h


def _table(c: _Canvas, x: int, y: int, w: int, heading: str, items: list[Item]) -> int:
    """A forum table: a gradient bar, then a row per flag and what it does."""
    top = y
    c.grad((x, y, x + w, y + 20), *C["head"])
    c.text(x + 8, y + 4, heading, _f("bold"), "#ffffff", C["head_ink"])
    y += 20
    mono, sans = _f("mono"), _f("sans")
    col = int(min(max(mono.getlength(i.chip) for i in items) + 12, w * 0.58))
    for n, i in enumerate(items):
        chip, words = _wrap_chip(i.chip, mono, col - 12), _wrap(i.words, sans, w - col - 12)
        h = max(len(chip), len(words)) * LH + 8
        c.rect((x, y, x + w - 1, y + h - 1), fill=C["alt"][n % 2])
        c.line((x + col, y, x + col, y + h - 1), C["cell"])
        c.line((x, y + h - 1, x + w - 1, y + h - 1), C["cell"])
        for k, l in enumerate(chip):
            c.text(x + 6, y + 4 + k * LH, l, mono, C["flag"])
        for k, l in enumerate(words):
            c.text(x + col + 6, y + 4 + k * LH, l, sans, C["text"])
        y += h
    c.rect((x, top, x + w - 1, y - 1), outline=C["table"])
    return y - top


def _centred(c: _Canvas, y: int, s: str, font, fill, shadow=None) -> None:
    c.text(MARGIN + (PAGE_W - int(font.getlength(s))) // 2, y, s, font, fill, shadow)


def _layout(c: _Canvas, p: Page, host: str) -> int:
    x0, y0 = MARGIN, 12
    right = x0 + PAGE_W
    mid = x0 + PAGE_W // 2
    # header: Teto, then the host smoothed as a Photoshop logo was, with BETA
    head = 10 + TETO_H + 4 + 30 + 12
    c.grad((x0, y0, right, y0 + head), *C["banner"], gloss=True)
    if c.im is not None:
        pic = Image.open(TETO, formats=("PNG",)).convert("RGBA")
        pic = pic.resize((round(pic.width * TETO_H / pic.height), TETO_H), Image.LANCZOS)
        c.im.paste(pic, (mid - pic.width // 2, y0 + 10), pic)
    title = _f("bold", 26)
    tw = int(title.getlength(host))
    tx, ty = mid - (tw + 8 + 34) // 2, y0 + 10 + TETO_H + 4
    if c.d is not None:
        c.d.fontmode = "L"
        for d in (2, 1):
            c.d.text((tx + d, ty + d), host, font=title, fill=C["banner_ink"])
        c.d.text((tx, ty), host, font=title, fill="#ffffff")
        c.d.fontmode = "1"
    bx = tx + tw + 8
    c.grad((bx, ty + 4, bx + 34, ty + 16), "#ffb347", "#ff7f00")
    c.rect((bx, ty + 4, bx + 33, ty + 15), outline="#c45a00")
    c.text(bx + 4, ty + 6, "BETA", _f("pixel", 8), "#ffffff")
    c.line((x0, y0 + head - 1, right - 1, y0 + head - 1), "#7a0f35")
    y = y0 + head + 12
    # how, and an example
    if p.intro:
        _centred(c, y, p.intro, _f("bold"), C["text"])
        y += LH + 5
        y += c.code(mid, y, p.example) + 12
    # the table
    y += _table(c, x0 + GUT, y, PAGE_W - 2 * GUT, p.heading, p.items) + 12
    # the other page
    words, link = p.more
    bold, sans = _f("bold"), _f("sans")
    lw = int(bold.getlength(words)) + 6 + int(sans.getlength(link))
    lx = mid - lw // 2
    c.text(lx, y, words, bold, C["text"])
    lx += int(bold.getlength(words)) + 6
    c.text(lx, y, link, sans, C["link"])
    c.line((lx, y + 13, lx + int(sans.getlength(link)), y + 13), C["link"])
    y += LH + 12
    c.rect((x0 - 1, y0 - 1, right, y), outline=C["edge"])
    return y + 1


def png(page: Page, **v) -> bytes:
    p = _fill(page, v)
    h = _layout(_Canvas(None), p, v["host"]) + 14
    im = Image.new("RGB", (W, h), C["stripes"][0])
    d = ImageDraw.Draw(im)
    for k in range(-h, W, 8):
        d.polygon([(k, 0), (k + 4, 0), (k + 4 + h, h), (k + h, h)], fill=C["stripes"][1])
    # the page's hard shadow, then the page
    d.rectangle((MARGIN + 3, 15, MARGIN + PAGE_W + 3, h - 14 + 3), fill=C["shadow"])
    d.rectangle((MARGIN, 12, MARGIN + PAGE_W - 1, h - 14), fill=C["page"])
    _layout(_Canvas(im), p, v["host"])
    im = im.resize((W * 2, h * 2), Image.NEAREST)
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()
