"""The embed host: an x.com link with the domain swapped for this one.

    https://<this host>/<user>/status/<id>

A person opening it is sent on to the same path on x.com. A link-preview
fetcher gets the screenshot convert draws: Discord is redirected to the file
itself (an mp4 when the post or its quote has a video, a png otherwise), so it
shows up like an uploaded attachment; every other fetcher gets a page of Open
Graph tags pointing at the same files.

Renders are kept on disk for RENDER_TTL and served with ranges, since chat
apps seek in a video instead of reading it once. Run with

    uvicorn app.embed:app
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import card, shot, tunnel, tweet
from . import plan as planning
from . import resolve as rs

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("embed")
logging.getLogger("httpx").setLevel(logging.WARNING)

VERSION = os.environ.get("APP_VERSION", "dev")
DIR = Path(os.environ.get("EMBED_DIR", "/tmp/embed"))
RENDER_TTL = int(os.environ.get("RENDER_TTL", 3600))
MAX_BYTES = int(os.environ.get("EMBED_MAX_MB", 1500)) << 20
RENDERS = int(os.environ.get("RENDERS", 2))          # ffmpeg processes at once
QUEUE = int(os.environ.get("RENDER_QUEUE", 20))      # posts waiting or rendering
DISCORD_WAIT = float(os.environ.get("DISCORD_WAIT", 8))
RENDER_TIMEOUT = 900

# Discord's unfurler sends its own UA and, for some fetches, one of these
# fixed old Firefox strings.
DISCORD = re.compile(r"Discordbot|rv:38\.0\) Gecko/20100101 Firefox/38\.0|rv:92\.0\) Gecko/20100101 Firefox/92\.0")
BOTS = re.compile(r"bot\b|bot/|crawler|spider|facebookexternalhit|whatsapp|telegram|slack|skype|embedly|iframely|mastodon|cardyb|bluesky|vkshare|pinterest|preview", re.I)
STATUS = re.compile(r"^/(?:[A-Za-z0-9_]{1,15}|i(?:/web)?)/status(?:es)?/(\d{1,20})(?:/.*)?$")


@asynccontextmanager
async def lifespan(app):
    DIR.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(tunnel.serve())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="embed", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


# --------------------------------------------------------------- renders ---

class Busy(Exception):
    pass


_jobs: dict[str, asyncio.Task] = {}
_slots = asyncio.Semaphore(RENDERS)


def _meta(tid: str) -> dict | None:
    """The finished render's description, if there is a fresh one on disk."""
    p = DIR / f"{tid}.json"
    try:
        if time.time() - p.stat().st_mtime > RENDER_TTL:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def render(tid: str) -> asyncio.Task:
    """The render of one post, started if it isn't already running."""
    t = _jobs.get(tid)
    if t is None:
        if len(_jobs) >= QUEUE:
            raise Busy()
        t = _jobs[tid] = asyncio.create_task(_render(tid))
        t.add_done_callback(lambda t: (_jobs.pop(tid, None), t.cancelled() or t.exception()))
    return t


async def media(tid: str) -> dict:
    m = _meta(tid)
    if m is not None:
        return m
    return await asyncio.shield(render(tid))


async def _render(tid: str) -> dict:
    t0 = time.time()
    post = await tweet.fetch(tid, planning.DEFAULTS["shot_depth"])
    s = await asyncio.to_thread(shot.make, post, dict(planning.DEFAULTS, mode="shot"))
    L = s.layout
    meta = {
        "id": tid, "url": post["url"], "name": post["name"], "handle": post["handle"],
        "text": tweet.plain_text(post), "width": L.width, "height": L.height,
        "ext": "mp4" if L.cells else "png",
    }
    async with _slots:
        # the poster is the card with each video's thumbnail drawn in: the png
        # itself for a post without video, the preview image for one with
        poster = await asyncio.to_thread(card.render, L, False)
        _write(DIR / f"{tid}.png", poster)
        if L.cells:
            await _mp4(s, DIR / f"{tid}.mp4")
    _write(DIR / f"{tid}.json", json.dumps(meta).encode())
    log.info("rendered %s %s %dx%d in %.1fs", tid, meta["ext"], L.width, L.height, time.time() - t0)
    await asyncio.to_thread(_prune)
    return meta


async def _mp4(s: shot.Shot, dest: Path) -> None:
    holes = await asyncio.to_thread(card.render, s.layout, True)
    part = dest.with_suffix(".part.mp4")
    # a whole file with the index up front, so players can seek straight away
    argv = shot.command(s) + ["-movflags", "+faststart", str(part)]
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(holes), RENDER_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        part.unlink(missing_ok=True)
        raise rs.ResolveError("the video took too long to compose")
    if proc.returncode != 0:
        part.unlink(missing_ok=True)
        lines = err.decode(errors="replace").strip().splitlines()
        raise rs.ResolveError("the video couldn't be composed: " + (lines[-1][:160] if lines else "unknown error"))
    part.replace(dest)


def _write(p: Path, data: bytes) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(p)


def _prune() -> None:
    """Drop renders past their TTL, then the oldest until under MAX_BYTES."""
    now = time.time()
    files = []
    for p in DIR.iterdir():
        try:
            st = p.stat()
        except OSError:
            continue
        if now - st.st_mtime > RENDER_TTL + 600:
            p.unlink(missing_ok=True)
        else:
            files.append((st.st_mtime, st.st_size, p))
    total = sum(f[1] for f in files)
    for _, size, p in sorted(files):
        if total <= MAX_BYTES:
            break
        if p.stem.split(".")[0] in _jobs:
            continue
        # the json goes with its files, so a render is either whole or absent
        (DIR / f"{p.stem.split('.')[0]}.json").unlink(missing_ok=True)
        p.unlink(missing_ok=True)
        total -= size


# ------------------------------------------------------------------ pages ---

def _base(req: Request) -> str:
    return f"https://{req.headers.get('host') or req.url.hostname}"


def _og(req: Request, m: dict) -> str:
    base = _base(req)
    e = lambda s: html.escape(str(s), quote=True)
    title = f"{m['name']} (@{m['handle']})"
    tags = [
        ("og:site_name", "x.com"), ("og:title", title), ("og:description", m["text"][:300]), ("og:url", m["url"]),
        ("og:image", f"{base}/m/{m['id']}.png"), ("og:image:type", "image/png"),
        ("og:image:width", m["width"]), ("og:image:height", m["height"]),
    ]
    if m["ext"] == "mp4":
        tags += [("og:type", "video.other"), ("og:video", f"{base}/m/{m['id']}.mp4"), ("og:video:secure_url", f"{base}/m/{m['id']}.mp4"),
                 ("og:video:type", "video/mp4"), ("og:video:width", m["width"]), ("og:video:height", m["height"])]
    card_kind = "player" if m["ext"] == "mp4" else "summary_large_image"
    head = "\n".join(f'<meta property="{k}" content="{e(v)}">' for k, v in tags)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{e(title)}</title>
<meta name="theme-color" content="#000000">
<meta name="twitter:card" content="{card_kind}">
<meta name="twitter:image" content="{e(base)}/m/{e(m['id'])}.png">
{head}
<meta http-equiv="refresh" content="0; url={e(m['url'])}">
</head><body></body></html>"""


def _to_x(req: Request) -> RedirectResponse:
    q = f"?{req.url.query}" if req.url.query else ""
    return RedirectResponse(f"https://x.com{req.url.path}{q}", status_code=302)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": VERSION, "rendering": len(_jobs)}


@app.get("/m/{name}")
async def file(name: str):
    m = re.fullmatch(r"(\d{1,20})\.(mp4|png)", name)
    if not m:
        return Response(status_code=404)
    tid, ext = m.groups()
    try:
        meta = await media(tid)
    except Busy:
        return Response(status_code=503, headers={"Retry-After": "30"})
    except rs.ResolveError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    if ext == "mp4" and meta["ext"] != "mp4":
        return Response(status_code=404)
    return FileResponse(DIR / f"{tid}.{ext}", media_type=f"video/mp4" if ext == "mp4" else "image/png",
                        headers={"Cache-Control": f"public, max-age={RENDER_TTL}"})


@app.get("/{path:path}")
async def post(path: str, req: Request):
    ua = req.headers.get("user-agent", "")
    m = STATUS.match(req.url.path)
    discord = bool(DISCORD.search(ua))
    if not m or not (discord or BOTS.search(ua)):
        return _to_x(req)
    tid = m.group(1)
    try:
        meta = _meta(tid)
        if meta is None:
            job = render(tid)
            if discord:
                # Discord gives up on a slow link; past the wait it gets the
                # tags instead, and the video is ready by the time anyone plays it
                done, _ = await asyncio.wait({job}, timeout=DISCORD_WAIT)
                if not done:
                    post_ = await tweet.fetch(tid, planning.DEFAULTS["shot_depth"])
                    return HTMLResponse(_og(req, _pending(tid, post_)))
            meta = await asyncio.shield(job)
    except Busy:
        return _to_x(req)
    except rs.ResolveError as e:
        log.info("post %s: %s", tid, e)
        return _to_x(req)
    if discord:
        return RedirectResponse(f"{_base(req)}/m/{tid}.{meta['ext']}", status_code=302)
    return HTMLResponse(_og(req, meta))


def _pending(tid: str, post: dict) -> dict:
    """What the tags need while the render is still running."""
    L = card.layout(post, "dark", planning.DEFAULTS["shot_stats"])
    return {"id": tid, "url": post["url"], "name": post["name"], "handle": post["handle"], "text": tweet.plain_text(post),
            "width": L.width, "height": L.height, "ext": "mp4" if L.cells else "png"}
