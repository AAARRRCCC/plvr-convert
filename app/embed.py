"""The embed host: an x.com link with the domain swapped for this one.

    https://<this host>/<user>/status/<id>

A person opening it is sent on to the same path on x.com. A link-preview
fetcher gets the screenshot convert draws, with the post's whole text rather
than x.com's "Show more" cut.

Discord gets a page pointing at a Mastodon status (<link type=
"application/activity+json">); it then reads /api/v1/statuses/<id> and draws
the post the way it draws a Mastodon one, with the media at the embed's full
width, which is far larger than it shows a bare video or image link. The
media is the screenshot: an mp4 when the post or its quote has a video, a png
otherwise. A video longer than LONG_VIDEO takes too long to compose, so that
status carries the text and x.com's own media instead.

Every other fetcher gets Open Graph tags pointing at the same files, or for a
long video, the text and x.com's video.

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
from datetime import datetime, timezone
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
LONG_VIDEO = float(os.environ.get("LONG_VIDEO", 30))  # seconds
RENDER_TIMEOUT = 900
DEPTH = planning.DEFAULTS["shot_depth"]
FULL = 10_000                                        # lines of text: no cut

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
    post = await tweet.fetch(tid, DEPTH)
    s = await asyncio.to_thread(shot.make, post, dict(planning.DEFAULTS, mode="shot"), FULL)
    L = s.layout
    meta = {
        "id": tid, "url": post["url"], "name": post["name"], "handle": post["handle"],
        "text": tweet.plain_text(post), "width": L.width, "height": L.height,
        "ext": "mp4" if L.cells else "png",
    }
    async with _slots:
        # the poster is the card with each video's thumbnail drawn in: the png
        # itself for a post without video, the preview image for one with,
        # drawn while ffmpeg runs since Discord only waits on the mp4
        poster = asyncio.to_thread(card.render, L, False)
        if L.cells:
            png, _ = await asyncio.gather(poster, _mp4(s, DIR / f"{tid}.mp4"))
        else:
            png = await poster
        _write(DIR / f"{tid}.png", png)
    _write(DIR / f"{tid}.json", json.dumps(meta).encode())
    log.info("rendered %s %s %dx%d in %.1fs", tid, meta["ext"], L.width, L.height, time.time() - t0)
    await asyncio.to_thread(_prune)
    return meta


async def _mp4(s: shot.Shot, dest: Path) -> None:
    holes = await asyncio.to_thread(card.render, s.layout, True)
    part = dest.with_suffix(".part.mp4")
    # a whole file with the index up front, so players can seek straight away
    argv = shot.command(s, "superfast") + ["-movflags", "+faststart", str(part)]
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


def _lead(post: dict) -> dict | None:
    """The video the render's length and sound come from: the first real
    video in the post or its quotes, a gif only when there is nothing else."""
    vids = tweet.videos(post)
    return next((m for m in vids if m["kind"] == "video"), vids[0] if vids else None)


def _text(post: dict) -> str:
    text = tweet.plain_text(post)
    q = post.get("quote")
    if q:
        text += f"\n\n↘️ Quoting {q['name']} (@{q['handle']})\n{tweet.plain_text(q)}"
    return text


def _html(post: dict) -> str:
    """The text as Mastodon status content, the quoted post as a blockquote."""
    e = lambda s: html.escape(str(s), quote=True)
    lines = lambda p: "<br>".join(e(line) for line in tweet.plain_text(p).split("\n"))
    out = f"<p>{lines(post)}</p>" if tweet.plain_text(post) else ""
    q = post.get("quote")
    if q:
        out += f'<blockquote><b>Quoting <a href="{e(q["url"])}">{e(q["name"])}</a> @{e(q["handle"])}</b><br>{lines(q)}</blockquote>'
    return out


def _attachment(n: int, kind: str, url: str, preview: str | None, w: int, h: int) -> dict:
    return {"id": str(n), "type": kind, "url": url, "preview_url": preview or url, "remote_url": None,
            "description": None, "meta": {"original": {"width": w, "height": h}}}


def _native(post: dict) -> list[dict]:
    """x.com's own media: the post's, or the quoted post's when it has none."""
    items = post["media"] or (post.get("quote") or {}).get("media") or []
    kinds = {"photo": "image", "video": "video", "gif": "gifv"}
    return [_attachment(i, kinds[m["kind"]], m["url"], m.get("poster"), m["w"], m["h"]) for i, m in enumerate(items)]


def _plain(post: dict) -> str:
    """A plain embed for a long video: name, text, and the video as x.com serves it."""
    e = lambda s: html.escape(str(s), quote=True)
    v = _lead(post)
    title = f"{post['name']} (@{post['handle']})"
    tags = [
        ("og:title", title), ("og:description", _text(post)[:1000]), ("og:url", post["url"]), ("og:type", "video.other"),
        ("og:video", v["url"]), ("og:video:secure_url", v["url"]), ("og:video:type", "video/mp4"),
        ("og:video:width", v["w"]), ("og:video:height", v["h"]),
    ]
    if v.get("poster"):
        tags += [("og:image", v["poster"]), ("og:image:width", v["w"]), ("og:image:height", v["h"])]
    head = "\n".join(f'<meta property="{k}" content="{e(val)}">' for k, val in tags)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{e(title)}</title>
<meta name="theme-color" content="#000000">
<meta name="twitter:card" content="player">
{head}
<meta http-equiv="refresh" content="0; url={e(post['url'])}">
</head><body></body></html>"""


def _activity(req: Request, post: dict) -> str:
    """Discord's page: the link that makes it read /api/v1/statuses/<id>."""
    e = lambda s: html.escape(str(s), quote=True)
    title = f"{post['name']} (@{post['handle']})"
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{e(title)}</title>
<meta name="theme-color" content="#000000">
<meta property="og:title" content="{e(title)}">
<meta property="og:description" content="{e(_text(post)[:1000])}">
<link type="application/activity+json" href="{e(_base(req))}/users/{e(post['handle'])}/statuses/{e(post['id'])}">
<meta http-equiv="refresh" content="0; url={e(post['url'])}">
</head><body></body></html>"""


def _long(post: dict) -> bool:
    lead = _lead(post)
    return bool(lead and (lead.get("duration") or 0) > LONG_VIDEO)


def _to_x(req: Request) -> RedirectResponse:
    q = f"?{req.url.query}" if req.url.query else ""
    return RedirectResponse(f"https://x.com{req.url.path}{q}", status_code=302)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": VERSION, "rendering": len(_jobs)}


@app.get("/api/v1/statuses/{tid}")
async def status(tid: str, req: Request):
    """The post as a Mastodon status, the shape Discord reads for these embeds."""
    if not re.fullmatch(r"\d{1,20}", tid):
        return JSONResponse({"error": "Record not found"}, status_code=404)
    try:
        post = await tweet.fetch(tid, DEPTH)
    except rs.ResolveError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    base = _base(req)
    content, media_ = _html(post), _native(post)
    if not _long(post):
        try:
            meta = _meta(tid)
            if meta is None:
                render(tid)
                meta = await asyncio.to_thread(_pending, tid, post)
            # the screenshot already shows the text, so the status carries none
            kind = "video" if meta["ext"] == "mp4" else "image"
            content = ""
            media_ = [_attachment(0, kind, f"{base}/m/{tid}.{meta['ext']}", f"{base}/m/{tid}.png", meta["width"], meta["height"])]
        except Busy:
            pass
    created = datetime.fromtimestamp(post["created"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "id": tid, "url": post["url"], "uri": post["url"], "created_at": created, "edited_at": None,
        "content": content, "spoiler_text": "", "sensitive": False, "visibility": "public", "language": None,
        "in_reply_to_id": None, "in_reply_to_account_id": None, "reblog": None, "application": {"name": None, "website": None},
        "media_attachments": media_, "mentions": [], "tags": [], "emojis": [],
        "account": {
            "id": post["handle"], "username": post["handle"], "acct": post["handle"], "display_name": post["name"],
            "url": f"https://x.com/{post['handle']}", "uri": f"https://x.com/{post['handle']}",
            "avatar": post["avatar"], "avatar_static": post["avatar"], "locked": False, "bot": False, "emojis": [], "fields": [],
        },
    }


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
        post_ = await tweet.fetch(tid, DEPTH)
        if discord:
            # the status Discord asks for next points at the render; start it now
            if not _long(post_) and _meta(tid) is None:
                try:
                    render(tid)
                except Busy:
                    pass
            return HTMLResponse(_activity(req, post_))
        if _long(post_):
            return HTMLResponse(_plain(post_))
        meta = _meta(tid)
        if meta is None:
            # the tags need only the size, so they go out while the render
            # runs and the file requests that follow wait on it
            render(tid)
            meta = await asyncio.to_thread(_pending, tid, post_)
    except Busy:
        return _to_x(req)
    except rs.ResolveError as e:
        log.info("post %s: %s", tid, e)
        return _to_x(req)
    return HTMLResponse(_og(req, meta))


def _pending(tid: str, post: dict) -> dict:
    """What the tags need while the render is still running."""
    L = card.layout(post, "dark", planning.DEFAULTS["shot_stats"], max_lines=FULL)
    return {"id": tid, "url": post["url"], "name": post["name"], "handle": post["handle"], "text": tweet.plain_text(post),
            "width": L.width, "height": L.height, "ext": "mp4" if L.cells else "png"}
