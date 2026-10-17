"""The guide: /help, every flag on one page, and /workflows, the combinations
the flags alone don't make obvious. Each page is drawn as an image for a chat
app's preview, served as a web page to a browser and as plain text to anything
else, all from the content here.

The look is a 2008 fansite: a fixed-width page on a striped background, a
glossy banner with a BETA sticker, forum tables with gradient header bars,
80x15 buttons at the foot, and Kasane Teto (born April 1, 2008) as the mascot.
The image is drawn at 1x in DejaVu Sans, the Verdana of its day, and Silkscreen,
then doubled pixel for pixel. Fonts in app/fonts (DejaVu and OFL licences),
Teto in app/art.
"""
from __future__ import annotations

import html as _html
import io
import math
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
    chip: str                  # a flag or a link, set in monospace
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
    heading: str = ""              # the page's own name, in its tab and crumb
    tip: str = ""                  # the sidebar's "Did you know?"


HELP = Page(
    "help", "embed flags",
    "Swap x.com for {host} in a post link, then add flags after ? joined by &. Every flag is in the image; "
    "combos are at {host}/workflows",
    "every flag, and what it does",
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
    ("More combos, step by step:", "{host}/workflows"),
    heading="Help & FAQ",
    tip="Posted a link by mistake and it's still drawing? Post it again with ?cancel on the end.",
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
    ("Every flag on its own:", "{host}/help"),
    heading="Workflows",
    tip="Videos are counted from the top post down: with quote, the count starts at the quoted post.",
)

PAGES = {p.path: p for p in (HELP, WORKFLOWS)}
MOTTO = "x.com links that unfurl as the post itself"
TABS = [("Help & FAQ", "/help"), ("Workflows", "/workflows"), ("x.com", "https://x.com")]
BUTTONS = [("TETO", "APPROVED", "#d4185e", "#333333"), ("YURI", "POWERED", "#ff5c9d", "#5a1430"),
           ("FFMPEG", "INSIDE", "#2d7d2d", "#1c1c1c"), ("XHTML", "1.0", "#ff6600", "#898e79"), ("PYTHON", "3.13", "#3a6ea5", "#555555")]
FOOT = "Powered by Python & ffmpeg  ·  Best viewed in Firefox at 1024x768  ·  © 2008-2026 {host}"


def _fill(p: Page, v: dict) -> Page:
    """The page with {host} and the rest filled in."""
    f = lambda s: s.format(**v) if s else s
    return Page(p.path, f(p.title), f(p.blurb), f(p.tagline), [(f(a), f(b)) for a, b in p.steps],
                [Section(f(s.heading), f(s.note), [Item(f(i.chip), f(i.words), f(i.title)) for i in s.items]) for s in p.sections],
                (f(p.footer[0]), f(p.footer[1])), p.heading, f(p.tip))


def _star(n: int, outer: float, inner: float, cx: float = 0, cy: float = 0) -> list[tuple[float, float]]:
    """A starburst's corners, `n` spikes."""
    return [(cx + (outer if k % 2 == 0 else inner) * math.sin(math.pi * k / n), cy - (outer if k % 2 == 0 else inner) * math.cos(math.pi * k / n))
            for k in range(2 * n)]


# ------------------------------------------------------------------- text ---

def text(page: Page, **v) -> str:
    p = _fill(page, v)
    out = [f"{v['host']} {p.title}", "", p.tagline, ""]
    for a, b in p.steps:
        out += [a, f"    {b}"]
    for s in p.sections:
        out += ["", s.heading.upper()]
        if s.note:
            out.append(f"  ({s.note})")
        for i in s.items:
            if i.title:
                out += [f"  {i.title}", f"    {i.chip}", f"    {i.words}"]
            else:
                out.append(f"  {i.chip:<20} {i.words}")
    out += ["", f"{p.footer[0]} {p.footer[1]}", ""]
    return "\n".join(out)


# ------------------------------------------------------------------- html ---

def _css() -> str:
    burst = ",".join(f"{50 + x:.1f}% {50 + y:.1f}%" for x, y in _star(14, 50, 38))
    return """
@font-face{font-family:Silkscreen;src:url(/guide/silkscreen.ttf) format("truetype")}
body{margin:0;padding:12px 8px 24px;background:#fbe3ec repeating-linear-gradient(45deg,#fbe3ec 0 4px,#f6d0de 4px 8px);
 font:11px/1.45 Verdana,"DejaVu Sans",Tahoma,Geneva,sans-serif;color:#222}
#wrap{max-width:760px;margin:0 auto;background:#fff;border:1px solid #9e2a52;box-shadow:3px 3px 0 #e9a9c2}
a{color:#0033cc}a:visited{color:#551a8b}
#banner{position:relative;height:130px;overflow:hidden;background:linear-gradient(#ff86b8,#d4185e);border-bottom:1px solid #7a0f35}
#banner:before{content:"";position:absolute;left:0;right:0;top:0;height:50%;background:rgba(255,255,255,.22)}
#banner h1{position:relative;margin:0;padding:28px 0 0 16px;font:bold 28px Verdana,"DejaVu Sans",sans-serif;color:#fff;text-shadow:1px 1px 0 #8a0f3e,2px 2px 0 #8a0f3e}
#banner .beta{display:inline-block;vertical-align:top;margin:4px 0 0 6px;padding:1px 4px;font:8px Silkscreen,monospace;color:#fff;
 background:linear-gradient(#ffb347,#ff7f00);border:1px solid #c45a00;text-shadow:none}
#banner p{position:relative;margin:6px 0 0 18px;color:#fff;text-shadow:1px 1px 0 #8a0f3e}
#banner img{position:absolute;right:18px;top:6px;height:480px}
#nav{margin:0;padding:0 0 0 8px;list-style:none;height:24px;background:linear-gradient(#5a1430,#26060f);border-bottom:1px solid #000}
#nav li{float:left;border-right:1px solid #7a2a4a}
#nav a{display:block;padding:0 12px;line-height:24px;color:#fff;font-weight:bold;text-decoration:none}
#nav li.on a{background:linear-gradient(#ffb3d1,#e8528a);color:#3a0a1d}
#nav .guest{float:right;border:0;padding:0 10px;line-height:24px;color:#f8c8dc;font:8px/24px Silkscreen,monospace}
#crumb{padding:4px 10px;background:#fff0f6;border-bottom:1px solid #efb6cc;color:#7a2a4a}
#cols{overflow:hidden;padding:10px}
#side{float:left;width:168px}
#main{margin-left:178px}
.box{border:1px solid #c94a7c;margin-bottom:10px;background:#fff7fb}
.tcat{background:linear-gradient(#ec5e95,#b51b52);color:#fff;font-weight:bold;padding:3px 8px;text-shadow:1px 1px 0 #7a0f35}
.box .body{padding:6px 8px}
.box ul{margin:0;padding:0;list-style:none}.box li:before{content:"";display:inline-block;width:5px;height:5px;background:#d4185e;margin:0 6px 1px 0}
.mascot{text-align:center}.mascot img{width:140px;display:block;margin:4px auto}
h2{margin:0 0 2px;font-size:16px;color:#b0003a}
.intro{margin:0 0 10px;color:#777;border-bottom:1px dotted #c94a7c;padding-bottom:6px}
table.tborder{width:100%;border-collapse:collapse;border:1px solid #c94a7c;margin-bottom:10px}
table.tborder td{padding:4px 6px;border-bottom:1px solid #f2c4d6;vertical-align:top}
table.tborder td.tcat{border:0}
tr.alt2 td{background:#fff2f7}td.flag{width:140px;border-right:1px solid #f2c4d6;white-space:nowrap}
.note td{background:#fffaf0;color:#777}
code{font:bold 11px "Courier New","DejaVu Sans Mono",monospace;color:#b0003a;overflow-wrap:anywhere}
.codebox{display:inline-block;margin:3px 0;padding:2px 5px;background:#fffbe0;border:1px dashed #e0bf55}.codebox code{color:#8a1538}
.n{color:#d4185e;font-weight:bold}
.new{display:flex;align-items:center;gap:10px;border:1px solid #e0bf55;background:#fffdf0;padding:6px 10px;margin-bottom:10px}
.burst{flex:none;width:54px;height:54px;display:grid;place-items:center;background:#e00000;clip-path:polygon(BURST)}
.burst b{width:50px;height:50px;display:grid;place-items:center;background:#ffe600;clip-path:polygon(BURST);color:#e00000;font:bold 11px Verdana,"DejaVu Sans",sans-serif}
#buttons{text-align:center;padding:0 10px 10px}
.ap{display:inline-flex;height:15px;margin:2px;border:1px solid #000;font:8px/13px Silkscreen,monospace;color:#fff;vertical-align:middle}
.ap b,.ap i{font-weight:normal;font-style:normal;padding:0 3px;border:1px solid rgba(255,255,255,.35)}
#foot{background:linear-gradient(#5a1430,#26060f);color:#f8c8dc;text-align:center;padding:10px;font-size:10px}
@media (max-width:600px){#side{float:none;width:auto}#main{margin:0}#banner img{opacity:.35}td.flag{white-space:normal;width:40%}}
""".replace("BURST", burst)


def page_html(page: Page, **v) -> str:
    p = _fill(page, v)
    e = lambda s: _html.escape(s, quote=True)
    host = v["host"]
    nav = "".join(f'<li{" class=on" if name == p.heading else ""}><a href="{e(href)}">{e(name)}</a></li>' for name, href in TABS)
    links = "".join(f'<li><a href="{e(href)}">{e(name)}</a></li>' for name, href in TABS)
    steps = "".join(f'<tr class="alt{n % 2 + 1}"><td><span class="n">{n + 1}.</span> {e(a)}<br><span class="codebox"><code>{e(b)}</code></span></td></tr>'
                    for n, (a, b) in enumerate(p.steps))
    tables = [f'<table class="tborder"><tr><td class="tcat">Getting started</td></tr>{steps}</table>']
    for s in p.sections:
        recipes = any(i.title for i in s.items)
        span = "" if recipes else " colspan=2"
        rows = [f'<tr><td class="tcat"{span}>{e(s.heading)}</td></tr>']
        if s.note:
            rows.append(f'<tr class="note"><td{span}>{e(s.note)}</td></tr>')
        for n, i in enumerate(s.items):
            if recipes:
                rows.append(f'<tr class="alt{n % 2 + 1}"><td><b>{e(i.title or "")}</b><br><span class="codebox"><code>{e(i.chip)}</code></span><br>'
                            f'<span style="color:#555">{e(i.words)}</span></td></tr>')
            else:
                rows.append(f'<tr class="alt{n % 2 + 1}"><td class="flag"><code>{e(i.chip)}</code></td><td>{e(i.words)}</td></tr>')
        tables.append(f'<table class="tborder">{"".join(rows)}</table>')
    link = p.footer[1]
    href = "/" + link.split("/", 1)[1] if "/" in link else link
    buttons = "".join(f'<span class="ap"><b style="background:{a}">{e(l)}</b><i style="background:{b}">{e(r)}</i></span>' for l, r, a, b in BUTTONS)
    return f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(p.heading)} :: {e(host)}</title>
<style>{_css()}</style></head>
<body><div id="wrap">
<div id="banner"><h1>{e(host)}<span class="beta">BETA</span></h1><p>{e(MOTTO)}</p><img src="/guide/teto.png" alt="Kasane Teto with a baguette"></div>
<ul id="nav">{nav}<li class="guest">Welcome, Guest!</li></ul>
<div id="crumb"><a href="/help">{e(host)}</a> &raquo; <b>{e(p.heading)}</b></div>
<div id="cols">
<div id="side">
<div class="box"><div class="tcat">Mascot</div><div class="body mascot"><img src="/guide/teto.png" alt="Kasane Teto with a baguette"><b>Kasane Teto</b><br><span style="color:#777">est. April 1, 2008</span></div></div>
<div class="box"><div class="tcat">Navigation</div><div class="body"><ul>{links}</ul></div></div>
<div class="box"><div class="tcat">Did you know?</div><div class="body">{e(p.tip)}</div></div>
</div>
<div id="main">
<h2>{e(p.heading)}</h2><p class="intro">{e(p.tagline)}</p>
{"".join(tables)}
<div class="new"><span class="burst"><b>NEW!</b></span><span>{e(p.footer[0])} <a href="{e(href)}">{e(link)}</a></span></div>
</div>
</div>
<div id="buttons">{buttons}</div>
<div id="foot">{e(FOOT.format(host=host))}</div>
</div></body></html>"""


# -------------------------------------------------------------------- png ---

W, MARGIN = 800, 20            # the image, and the stripes either side of the page
PAGE_W = W - 2 * MARGIN
SIDE, GUT, LH = 168, 10, 15    # sidebar width, gutter, line height
BANNER = 130
C = {
    "stripes": ("#fbe3ec", "#f6d0de"), "page": "#ffffff", "edge": "#9e2a52", "shadow": "#e9a9c2",
    "banner": ("#ff86b8", "#d4185e"), "banner_ink": "#8a0f3e",
    "nav": ("#5a1430", "#26060f"), "tab": ("#ffb3d1", "#e8528a"), "tab_ink": "#3a0a1d", "nav_line": "#7a2a4a", "guest": "#f8c8dc",
    "crumb": "#fff0f6", "crumb_line": "#efb6cc", "crumb_ink": "#7a2a4a",
    "head": ("#ec5e95", "#b51b52"), "head_ink": "#7a0f35",
    "alt": ("#ffffff", "#fff2f7"), "cell": "#f2c4d6", "table": "#c94a7c", "note": "#fffaf0",
    "text": "#222222", "muted": "#777777", "flag": "#b0003a", "link": "#0033cc",
    "code": "#fffbe0", "code_edge": "#e0bf55", "code_ink": "#8a1538", "side": "#fff7fb", "new": "#fffdf0",
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
    as a 2008 screen showed it."""

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

    def para(self, x: int, y: int, s: str, font, fill, width: int) -> int:
        lines = _wrap(s, font, width)
        for i, line in enumerate(lines):
            self.text(x, y + i * LH, line, font, fill)
        return len(lines) * LH

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

    def code(self, x: int, y: int, s: str, width: int) -> tuple[int, int]:
        """A flag or link in a dashed yellow box; its width and height."""
        f = _f("mono")
        lines = _wrap_chip(s, f, width - 10)
        w, h = int(max(f.getlength(l) for l in lines)) + 10, len(lines) * LH + 5
        if self.d is not None:
            self.rect((x, y, x + w - 1, y + h - 1), fill=C["code"])
            for k in range(0, w, 4):
                self.line((x + k, y, min(x + k + 1, x + w - 1), y), C["code_edge"])
                self.line((x + k, y + h - 1, min(x + k + 1, x + w - 1), y + h - 1), C["code_edge"])
            for k in range(0, h, 4):
                self.line((x, y + k, x, min(y + k + 1, y + h - 1)), C["code_edge"])
                self.line((x + w - 1, y + k, x + w - 1, min(y + k + 1, y + h - 1)), C["code_edge"])
            for i, l in enumerate(lines):
                self.text(x + 5, y + 2 + i * LH, l, f, C["code_ink"])
        return w, h

    def bar(self, x: int, y: int, w: int, title: str) -> int:
        """A table's gradient header bar; its height."""
        self.grad((x, y, x + w, y + 20), *C["head"])
        self.text(x + 8, y + 4, title, _f("bold"), "#ffffff", C["head_ink"])
        return 20


def _flags(c: _Canvas, x: int, y: int, w: int, s: Section) -> int:
    top = y
    y += c.bar(x, y, w, s.heading)
    mono, sans = _f("mono"), _f("sans")
    if s.note:
        h = len(_wrap(s.note, sans, w - 12)) * LH + 8
        c.rect((x, y, x + w - 1, y + h - 1), fill=C["note"])
        c.para(x + 6, y + 4, s.note, sans, C["muted"], w - 12)
        c.line((x, y + h - 1, x + w - 1, y + h - 1), C["cell"])
        y += h
    col = 140
    for n, i in enumerate(s.items):
        flag, words = _wrap_chip(i.chip, mono, col - 12), _wrap(i.words, sans, w - col - 12)
        h = max(len(flag), len(words)) * LH + 8
        c.rect((x, y, x + w - 1, y + h - 1), fill=C["alt"][n % 2])
        c.line((x + col, y, x + col, y + h - 1), C["cell"])
        c.line((x, y + h - 1, x + w - 1, y + h - 1), C["cell"])
        for k, l in enumerate(flag):
            c.text(x + 6, y + 4 + k * LH, l, mono, C["flag"])
        for k, l in enumerate(words):
            c.text(x + col + 6, y + 4 + k * LH, l, sans, C["text"])
        y += h
    c.rect((x, top, x + w - 1, y - 1), outline=C["table"])
    return y - top


def _recipes(c: _Canvas, x: int, y: int, w: int, s: Section) -> int:
    top = y
    y += c.bar(x, y, w, s.heading)
    bold, sans = _f("bold"), _f("sans")
    for n, i in enumerate(s.items):
        h = 6 + len(_wrap(i.title, bold, w - 12)) * LH + 3 + _Canvas(None).code(0, 0, i.chip, w - 12)[1] + 3 + len(_wrap(i.words, sans, w - 12)) * LH + 5
        c.rect((x, y, x + w - 1, y + h - 1), fill=C["alt"][n % 2])
        c.line((x, y + h - 1, x + w - 1, y + h - 1), C["cell"])
        ty = y + 6
        ty += c.para(x + 6, ty, i.title, bold, C["text"], w - 12) + 3
        ty += c.code(x + 6, ty, i.chip, w - 12)[1] + 3
        c.para(x + 6, ty, i.words, sans, "#555555", w - 12)
        y += h
    c.rect((x, top, x + w - 1, y - 1), outline=C["table"])
    return y - top


def _steps(c: _Canvas, x: int, y: int, w: int, steps: list[tuple[str, str]]) -> int:
    top = y
    y += c.bar(x, y, w, "Getting started")
    bold, sans = _f("bold"), _f("sans")
    for n, (a, b) in enumerate(steps):
        num = f"{n + 1}."
        nw = int(bold.getlength(num)) + 6
        h = 6 + len(_wrap(a, sans, w - 12 - nw)) * LH + 3 + _Canvas(None).code(0, 0, b, w - 12 - nw)[1] + 5
        c.rect((x, y, x + w - 1, y + h - 1), fill=C["alt"][n % 2])
        c.line((x, y + h - 1, x + w - 1, y + h - 1), C["cell"])
        c.text(x + 6, y + 6, num, bold, "#d4185e")
        th = c.para(x + 6 + nw, y + 6, a, sans, C["text"], w - 12 - nw)
        c.code(x + 6 + nw, y + 6 + th + 3, b, w - 12 - nw)
        y += h
    c.rect((x, top, x + w - 1, y - 1), outline=C["table"])
    return y - top


def _box(c: _Canvas, x: int, y: int, w: int, title: str, body) -> int:
    """A sidebar box: a header bar over a body `body(c, x, y, w)` paints and measures."""
    top = y
    y += c.bar(x, y, w, title)
    h = body(_Canvas(None), x, y, w)
    c.rect((x, y, x + w - 1, y + h - 1), fill=C["side"])
    body(c, x, y, w)
    c.rect((x, top, x + w - 1, y + h - 1), outline=C["table"])
    return y + h - top


def _teto(height: int, crop: float = 1.0) -> Image.Image:
    """Teto at `height` px, the top `crop` of her."""
    im = Image.open(TETO).convert("RGBA")
    im = im.crop((0, 0, im.width, round(im.height * crop)))
    return im.resize((max(1, round(im.width * height / im.height)), height), Image.LANCZOS)


def _mascot(c: _Canvas, x: int, y: int, w: int) -> int:
    pic = _teto(278)
    if c.im is not None:
        c.im.paste(pic, (x + (w - pic.width) // 2, y + 6), pic)
    ty = y + 6 + pic.height + 4
    for s, font, fill in (("Kasane Teto", _f("bold"), C["text"]), ("est. April 1, 2008", _f("sans"), C["muted"])):
        c.text(x + (w - int(font.getlength(s))) // 2, ty, s, font, fill)
        ty += LH
    return ty + 6 - y


def _links(c: _Canvas, x: int, y: int, w: int) -> int:
    ty = y + 6
    for name, _ in TABS:
        c.rect((x + 8, ty + 5, x + 12, ty + 9), fill="#d4185e")
        c.text(x + 18, ty, name, _f("sans"), C["link"])
        c.line((x + 18, ty + 13, x + 18 + int(_f("sans").getlength(name)), ty + 13), C["link"])
        ty += LH + 2
    return ty + 4 - y


def _burst(c: _Canvas, cx: int, cy: int) -> None:
    """A "NEW!" starburst."""
    if c.d is None:
        return
    c.d.polygon(_star(14, 27, 20, cx, cy), fill="#ffe600", outline="#e00000")
    c.d.text((cx, cy), "NEW!", font=_f("bold"), fill="#e00000", anchor="mm")


def _button(c: _Canvas, x: int, y: int, left: str, right: str, a: str, b: str) -> int:
    """An 80x15 antipixel button: a label on one colour, a word on another."""
    f = _f("pixel", 8)
    lw = int(f.getlength(left)) + 7
    if c.d is not None:
        c.rect((x, y, x + 79, y + 14), fill="#000000")
        c.rect((x + 1, y + 1, x + lw, y + 13), fill=a, outline="#ffffff")
        c.rect((x + lw + 1, y + 1, x + 78, y + 13), fill=b, outline="#ffffff")
        c.text(x + 4, y + 3, left, f, "#ffffff")
        c.text(x + lw + 1 + (78 - lw - int(f.getlength(right))) // 2, y + 3, right, f, "#ffffff")
    return 80


def _layout(c: _Canvas, p: Page, host: str) -> int:
    x0, y0 = MARGIN, 12
    right = x0 + PAGE_W
    # banner: gloss, the host with a BETA sticker, the tagline, Teto
    c.grad((x0, y0, right, y0 + BANNER), *C["banner"], gloss=True)
    title = _f("bold", 28)
    if c.d is not None:
        # the logo smoothed, as one made in Photoshop was
        c.d.fontmode = "L"
        for dx, dy in ((2, 2), (1, 1)):
            c.d.text((x0 + 16 + dx, y0 + 26 + dy), host, font=title, fill=C["banner_ink"])
        c.d.text((x0 + 16, y0 + 26), host, font=title, fill="#ffffff")
        c.d.fontmode = "1"
    bx = x0 + 16 + int(title.getlength(host)) + 8
    c.grad((bx, y0 + 30, bx + 34, y0 + 42), "#ffb347", "#ff7f00")
    c.rect((bx, y0 + 30, bx + 33, y0 + 41), outline="#c45a00")
    c.text(bx + 4, y0 + 32, "BETA", _f("pixel", 8), "#ffffff")
    c.text(x0 + 18, y0 + 68, MOTTO, _f("sans"), "#ffffff", C["banner_ink"])
    if c.im is not None:
        pic = _teto(BANNER + 120, 0.52)
        pic = pic.crop((0, 0, pic.width, BANNER - 7))      # cut at the banner's foot
        c.im.paste(pic, (right - pic.width - 18, y0 + 6), pic)
        c.line((x0, y0 + BANNER - 1, right - 1, y0 + BANNER - 1), "#7a0f35")
    y = y0 + BANNER
    # tabs, the open one lit
    c.grad((x0, y, right, y + 24), *C["nav"])
    tx = x0 + 8
    for name, _ in TABS:
        tw = int(_f("bold").getlength(name)) + 24
        if name == p.heading:
            c.grad((tx, y, tx + tw, y + 24), *C["tab"])
        c.text(tx + 12, y + 6, name, _f("bold"), C["tab_ink"] if name == p.heading else "#ffffff")
        c.line((tx + tw, y, tx + tw, y + 23), C["nav_line"])
        tx += tw + 1
    guest = "Welcome, Guest!"
    c.text(right - 10 - int(_f("pixel", 8).getlength(guest)), y + 8, guest, _f("pixel", 8), C["guest"])
    y += 24
    # crumb
    c.rect((x0, y, right - 1, y + 21), fill=C["crumb"])
    c.line((x0, y + 21, right - 1, y + 21), C["crumb_line"])
    crumb = f"{host}  »  "
    c.text(x0 + 10, y + 4, crumb, _f("sans"), C["crumb_ink"])
    c.text(x0 + 10 + int(_f("sans").getlength(crumb)), y + 4, p.heading, _f("bold"), C["crumb_ink"])
    y += 22 + GUT
    # sidebar
    sx, sy = x0 + GUT, y
    for title, body in (("Mascot", _mascot), ("Navigation", _links),
                        ("Did you know?", lambda c2, bx, by, bw: c2.para(bx + 8, by + 6, p.tip, _f("sans"), C["text"], bw - 16) + 12)):
        sy += _box(c, sx, sy, SIDE, title, body) + GUT
    # main column
    mx = sx + SIDE + GUT
    mw = right - GUT - mx
    my = y
    c.text(mx, my, p.heading, _f("bold", 16), C["flag"])
    my += 22
    my += c.para(mx, my, p.tagline, _f("sans"), C["muted"], mw) + 4
    for k in range(0, mw, 2):
        c.line((mx + k, my, mx + k, my), C["table"])
    my += GUT
    my += _steps(c, mx, my, mw, p.steps) + GUT
    for s in p.sections:
        my += (_recipes if any(i.title for i in s.items) else _flags)(c, mx, my, mw, s) + GUT
    # the NEW! callout pointing at the other page
    words, link = p.footer
    c.rect((mx, my, mx + mw - 1, my + 63), fill=C["new"], outline=C["code_edge"])
    _burst(c, mx + 36, my + 32)
    c.text(mx + 74, my + 17, words, _f("bold"), C["text"])
    lf = _f("sans")
    c.text(mx + 74, my + 33, link, lf, C["link"])
    c.line((mx + 74, my + 46, mx + 74 + int(lf.getlength(link)), my + 46), C["link"])
    my += 64 + GUT
    y = max(sy, my)
    # the buttons, then the footer bar
    total = len(BUTTONS) * 84 - 4
    bx = x0 + (PAGE_W - total) // 2
    for b in BUTTONS:
        bx += _button(c, bx, y, *b) + 4
    y += 15 + GUT
    c.grad((x0, y, right, y + 30), *C["nav"])
    foot = FOOT.format(host=host)
    c.text(x0 + (PAGE_W - int(_f("sans", 10).getlength(foot))) // 2, y + 9, foot, _f("sans", 10), C["guest"])
    y += 30
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
