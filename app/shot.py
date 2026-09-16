"""The screenshot mode: a post from x.com drawn as it looks on the page. With a
video anywhere in it the result is an mp4 in which the video plays inside the
card (the post's own first video carries the sound; any others loop, muted);
without one it is a png.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator

from . import card, tweet
from .plan import FRAG, Plan, filename, shell
from .resolve import Fmt, ResolveError, assert_public
from .stream import CHUNK, FFMPEG, FIRST_BYTE_TIMEOUT, Started, _kill, _rest, _urlfor

log = logging.getLogger("convert.shot")

MAX_DURATION = 600   # x.com's own cap is ten minutes for most accounts


@dataclass
class Shot:
    post: dict
    layout: card.Layout
    plan: Plan

    @property
    def cells(self):
        return self.layout.cells


def describe(post: dict) -> dict:
    """What the page shows once the link is read, in the shape of resolve.describe."""
    vids = tweet.videos(post)
    thumb = post["media"][0].get("poster") or post["media"][0].get("url") if post["media"] else None
    if not thumb and post.get("quote") and post["quote"]["media"]:
        m = post["quote"]["media"][0]
        thumb = m.get("poster") or m.get("url")
    text = tweet.plain_text(post)
    return {
        "id": post["id"], "kind": "post",
        "title": text or f"a post by @{post['handle']}",
        "uploader": f"{post['name']} @{post['handle']}",
        "duration": max((m.get("duration") or 0) for m in vids) if vids else None,
        "thumbnail": thumb or post.get("avatar"),
        "extractor": "twitter", "url": post["url"],
        "heights": [], "codecs": [], "has_video": bool(vids), "has_audio": any(m["kind"] == "video" for m in vids), "live": False,
        "quoted": bool(post.get("quote")), "photos": sum(1 for m in post["media"] if m["kind"] == "photo"),
    }


def make(post: dict, o: dict) -> Shot:
    L = card.layout(post, "dark", o["shot_stats"])
    info = {"title": tweet.plain_text(post)[:80] or post["handle"], "id": post["id"], "extractor_key": "twitter"}
    size = f"{L.width}×{L.height}"
    if L.cells:
        master = next(c for c in L.cells if c.master)
        dur = min(MAX_DURATION, master.item.get("duration") or 0) or None
        others = len(L.cells) - 1
        label = f"screenshot · mp4 · {size}" + (f" · {others} more video{'s' if others > 1 else ''}, muted" if others else "")
        p = Plan("shot", "mp4", filename(info, o, "mp4", None, None, "screenshot"), label, 0)
        p.args = [str(dur or 0)]
        return Shot(post, L, p)
    p = Plan("shot", "png", filename(info, o, "png", None, None, "screenshot"), f"screenshot · png · {size}", 0)
    return Shot(post, L, p)


def _fmt(item: dict) -> Fmt:
    return Fmt(id="shot", url=item["url"], ext="mp4", protocol="https", kind="progressive", vcodec="h264", acodec="aac",
               height=item.get("h") or 0, width=item.get("w") or 0, fps=0, tbr=0, abr=0, size=0, hdr=False, lang_pref=0, lang=None)


def _graph(L: card.Layout) -> tuple[list[str], str, int]:
    """The ffmpeg filter graph: each video scaled into its cell over a black
    canvas, the card on top. Returns (filter parts, output label, master index)."""
    parts = [f"color=c=black:s={L.width}x{L.height}:r=30[bg]"]
    prev = "bg"
    master_idx = 1
    for i, c in enumerate(L.cells):
        n = i + 1
        w, h = c.w - c.w % 2, c.h - c.h % 2
        if c.fit == "cover":
            sc = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
        else:
            sc = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
        parts.append(f"[{n}:v]{sc},setsar=1,fps=30[v{n}]")
        parts.append(f"[{prev}][v{n}]overlay={c.x}:{c.y}:eof_action=pass[b{n}]")
        prev = f"b{n}"
        if c.master:
            master_idx = n
    parts.append(f"[{prev}][0:v]overlay=0:0:eof_action=repeat,format=yuv420p[out]")
    return parts, "out", master_idx


async def start(shot: Shot) -> Started:
    L, p = shot.layout, shot.plan
    t0 = time.time()
    png = await asyncio.to_thread(card.render, L, bool(L.cells))
    log.info("card drawn %dx%d in %.1fs (%d video cells)", L.width, L.height, time.time() - t0, len(L.cells))
    if not L.cells:
        async def nothing() -> AsyncIterator[bytes]:
            return
            yield b""
        return Started(png, nothing(), 200, {"content-length": str(len(png))})

    argv = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "png_pipe", "-i", "pipe:0"]
    for c in L.cells:
        assert_public(c.item["url"])
        f = _fmt(c.item)
        if not c.master:
            argv += ["-stream_loop", "-1"]
        argv += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5", "-i", _urlfor(f)]
    parts, out, master = _graph(L)
    dur = float(p.args[0]) if p.args else 0
    argv += ["-filter_complex", ";".join(parts), "-map", f"[{out}]", "-map", f"{master}:a?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-profile:v", "high", "-level", "5.1", "-r", "30",
             "-c:a", "aac", "-b:a", "160k", "-shortest"]
    if dur:
        argv += ["-t", f"{dur:.3f}"]
    argv += ["-f", "mp4", "-movflags", FRAG, "pipe:1"]
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=CHUNK * 4)
    err: list[bytes] = []

    async def feed():
        try:
            proc.stdin.write(png)
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def drain():
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            if len(err) < 40:
                err.append(line)

    asyncio.create_task(feed())
    drainer = asyncio.create_task(drain())
    try:
        first = await asyncio.wait_for(proc.stdout.read(CHUNK), FIRST_BYTE_TIMEOUT)
    except asyncio.TimeoutError:
        first = b""
    if first:
        log.info("shot ffmpeg started in %.1fs: %s", time.time() - t0, p.label)
        return Started(first, _rest(proc, drainer, err, p))
    await _kill(proc)
    await drainer
    msg = b"".join(err).decode(errors="replace").strip()
    log.warning("shot ffmpeg produced nothing: %s | %s", msg.splitlines()[-1][:200] if msg else "no output", shell(argv))
    raise ResolveError("the video couldn't be composed: " + ((msg.splitlines()[-1][:160]) if msg else "unknown error"))
