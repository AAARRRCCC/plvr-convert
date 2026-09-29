"""Flags on an embed link, read from its query string:

    https://<embed host>/<user>/status/<id>?v2=image&q=2

Each one changes what the render shows. Anything this doesn't know, like the
s=20 and t=... x.com puts on a shared link, is ignored, and so is a known
flag with a value it can't read.

    v1 .. v9     one video: image (its still, no playback), mute, sound (this
                 one carries the audio and sets the length), hide, video
    v            every video, the same values; a numbered flag wins over it
    q            quote depth 0-3 (default 1)
    quote        start from the post it quotes, leaving the top post out; the
                 other flags, video numbers too, count from there
    stats        0 drops the reply/repost/like row
    theme        dark, dim, light
    media        the media alone, stacked edge to edge: no name, text or stats;
                 a lone gif is a real gif, from x.com's own file
    lines        cut the text after this many lines, with "Show more"
    start, end   clip the lead video: seconds, or m:ss / h:mm:ss
    plain        a video post goes out as x.com's own file, not the render
    raw          every visitor, a browser too, is sent to the rendered file
                 (or to x.com's own file, for a video too long to render)
    cancel       stop every render of this post that is still running
    help         the guide below, as plain text, instead of the post

Videos are counted from the top post down through its quotes, only the ones
that are there: on a post with a video quoting a post with a video, v1 is the
post's and v2 the quote's, but when the top post has none, v1 is the quote's.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlencode

TREATMENTS = ("video", "image", "mute", "sound", "hide")
THEMES = ("dark", "dim", "light")
MAX_VIDEOS = 9
MAX_LINES = 200
DEPTH = 1
NAMES = {"v", "q", "stats", "theme", "lines", "start", "end", "plain", "raw", "cancel", "media", "help", "quote"} | {f"v{n}" for n in range(1, MAX_VIDEOS + 1)}
_TRUE = {"", "1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}
_ALIAS = {"img": "image", "still": "image", "pic": "image", "photo": "image", "silent": "mute", "audio": "sound", "loud": "sound",
          "off": "hide", "none": "hide", "play": "video", "on": "video"}

HELP = """embed flags
===========

Swap x.com for {host} in a post link, then add flags after ? joined by &.
x.com's own s=20 / t=... are ignored.

    https://{host}/jaydiarie/status/2102564951177011259?s=20&v2=image

VIDEOS  (counted from the top: v1 is the first video there is, v2 the next)
    both posts have one: v1 = the post's, v2 = the quoted one's
    only the quoted tweet has one: that one is v1
  v2=image        a still, no playback
  v2=mute         plays silently
  v2=sound        this one gets the audio and sets the length
  v2=hide         removed
  v=image         every video (a numbered flag overrides it: v=image&v1=video)

LOOK
  q=0 .. q=3      how many quote levels (default 1; q=0 drops the quote)
  quote           only the quoted tweet, without the one quoting it
                  (quote&media: just the quoted tweet's video)
  stats=0         no reply/repost/like row
  theme=dim       or theme=light
  lines=5         cut the text after 5 lines
  media           the video/images alone, nothing else of the post
                  (a gif on its own comes out as a real gif you can save)

TIMING
  start=1:30&end=1:45   clip the video (90, 1:30 and 1m30s all work)
                        over {long}s goes out as x.com's own video; a shorter clip renders

OTHER
  plain           x.com's own video instead of the render
  raw             open the rendered file itself (to try flags in a browser)
  cancel          stop a render of this post started by accident
  help            this
"""


@dataclass(frozen=True)
class Flags:
    depth: int = DEPTH
    stats: bool = True
    theme: str = "dark"
    lines: int | None = None
    media: bool = False
    quote: bool = False
    start: float = 0.0
    end: float | None = None
    videos: tuple[tuple[int, str], ...] = ()   # (n, treatment), n=0 for every video
    plain: bool = False
    raw: bool = False
    cancel: bool = False
    help: bool = False

    def treatment(self, n: int) -> str:
        per = dict(self.videos)
        return per.get(n, per.get(0, "video"))

    def query(self) -> str:
        """The flags that change the render, as a query string for its file
        links ("" for none), in one order so each render has one address."""
        q = []
        if self.quote:
            q.append(("quote", 1))
        if self.depth != DEPTH:
            q.append(("q", self.depth))
        if not self.stats:
            q.append(("stats", 0))
        if self.theme != "dark":
            q.append(("theme", self.theme))
        if self.lines is not None:
            q.append(("lines", self.lines))
        if self.media:
            q.append(("media", 1))
        if self.start:
            q.append(("start", _num(self.start)))
        if self.end is not None:
            q.append(("end", _num(self.end)))
        q += [("v" if n == 0 else f"v{n}", t) for n, t in self.videos]
        return "?" + urlencode(q) if q else ""

    def key(self) -> str:
        """The part of a render's file name that tells these flags apart."""
        q = self.query()
        return "-" + hashlib.sha1(q.encode()).hexdigest()[:12] if q else ""


def _num(v: float) -> str:
    return f"{v:.3f}".rstrip("0").rstrip(".")


def seconds(s: str) -> float | None:
    """90, 12.5, 1:30, 1:02:03, 1m30s, 45s; None if it's none of those."""
    s = s.strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)", s)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", s)
    if m and any(m.groups()):
        return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)
    return None


def _bool(v: str) -> bool | None:
    v = v.strip().lower()
    return True if v in _TRUE else False if v in _FALSE else None


def parse(params: Mapping[str, str]) -> Flags:
    kw: dict = {}
    vids: dict[int, str] = {}
    for k, v in params.items():
        k = k.lower()
        if k not in NAMES:
            continue
        v = str(v).strip()
        if k == "v" or k[0] == "v" and k[1:].isdigit():
            t = _ALIAS.get(v.lower(), v.lower())
            if t in TREATMENTS:
                vids[0 if k == "v" else int(k[1:])] = t
        elif k == "q" and v.isdigit():
            kw["depth"] = min(3, int(v))
        elif k in ("stats", "plain", "raw", "cancel", "media", "help", "quote") and _bool(v) is not None:
            kw[k] = _bool(v)
        elif k == "theme" and v.lower() in THEMES:
            kw["theme"] = v.lower()
        elif k == "lines" and v.isdigit() and int(v) > 0:
            kw["lines"] = min(MAX_LINES, int(v))
        elif k in ("start", "end") and seconds(v) is not None:
            kw[k] = seconds(v)
    if kw.get("end") is not None and kw["end"] <= kw.get("start", 0):
        kw.pop("end")
    # an explicit "video" only means something against a v= for all of them
    everyone = vids.get(0, "video")
    vids = {n: t for n, t in vids.items() if n == 0 and t != "video" or n > 0 and t != everyone}
    return Flags(videos=tuple(sorted(vids.items())), **kw)


def strip(params: Mapping[str, str]) -> str:
    """The query string with these flags taken out, for passing on to x.com."""
    rest = [(k, v) for k, v in params.items() if k.lower() not in NAMES]
    return "?" + urlencode(rest) if rest else ""


def apply(post: dict, f: Flags) -> dict:
    """A copy of the post with each video marked as the flags say. The post is
    the cached one, so it is never changed in place."""
    if not f.videos:
        return post
    post = copy.deepcopy(post)
    n = 0
    p: dict | None = post
    while p:
        kept = []
        for m in p["media"]:
            if m["kind"] in ("video", "gif"):
                n += 1
                t = f.treatment(n)
                if t == "hide":
                    continue
                if t == "image":
                    m["still"] = True
                elif t == "mute":
                    m["mute"] = True
                elif t == "sound":
                    m["sound"] = True
            kept.append(m)
        p["media"] = kept
        p = p.get("quote")
    return post
