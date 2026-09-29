"""The embed host: an x.com link with the domain swapped for this one.

    https://<this host>/<user>/status/<id>

A person opening it is sent on to the same path on x.com. A link-preview
fetcher gets the screenshot convert draws, with the post's whole text rather
than x.com's "Show more" cut. When the post or its quote has a video, Discord
is redirected to the mp4 itself, so it plays like an upload. Otherwise Discord
gets a page whose only tag is the png: redirected to a bare image, it would
hide the link from the message. Every other fetcher gets Open Graph tags
pointing at the same files.

Discord is redirected at once, without waiting on the render: ffmpeg writes a
fragmented mp4, which plays from its first fragment, and a request that
arrives mid-render is streamed the file as it grows. When the render is done
the file is repacked with its index up front, and from then on it is served
whole, with ranges, since chat apps seek in a video instead of reading it
once. Discord reads the whole file before it shows the preview and gives up
after about ten seconds, so the bitrate is set to keep the file near
TARGET_MB, and a video longer than LONG_VIDEO, which takes longer than that
to compose, is not drawn: the post goes out as a plain embed, name, text and
x.com's own video file.

Flags in the link's query string (v2=image, q=0, theme=light, start=0:30 ...)
change the render; flags.py lists them. Each set of flags is its own render,
kept under the post's id plus a hash of the flags, and its files are linked
with the same flags in their query string, so a file request that comes
before the render can start it.

Renders are kept on disk for RENDER_TTL. Run with

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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse

from . import card, flags, shot, tunnel, tweet
from . import plan as planning
from . import resolve as rs
from .plan import FRAG

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("embed")
logging.getLogger("httpx").setLevel(logging.WARNING)

VERSION = os.environ.get("APP_VERSION", "dev")
DIR = Path(os.environ.get("EMBED_DIR", "/tmp/embed"))
RENDER_TTL = int(os.environ.get("RENDER_TTL", 3600))
MAX_BYTES = int(os.environ.get("EMBED_MAX_MB", 1500)) << 20
RENDERS = int(os.environ.get("RENDERS", 2))          # ffmpeg processes at once
QUEUE = int(os.environ.get("RENDER_QUEUE", 20))      # posts waiting or rendering
LONG_VIDEO = float(os.environ.get("LONG_VIDEO", 20))  # seconds
PRESET = os.environ.get("X264_PRESET", "superfast")
# Discord downloads the whole video while it builds the preview and gives up
# after about ten seconds, so the file is held to a size the house uplink
# sends well inside that: the bitrate comes from the video's length
TARGET_MB = float(os.environ.get("TARGET_MB", 10))
MAXRATE_KBPS = 3000
RENDER_TIMEOUT = 900
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


class Cancelled(Exception):
    pass


_jobs: dict[str, asyncio.Task] = {}
_slots = asyncio.Semaphore(RENDERS)


def _meta(name: str) -> dict | None:
    """The finished render's description, if there is a fresh one on disk."""
    p = DIR / f"{name}.json"
    try:
        if time.time() - p.stat().st_mtime > RENDER_TTL:
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _name(tid: str, f: flags.Flags) -> str:
    return tid + f.key()


def render(tid: str, f: flags.Flags) -> asyncio.Task:
    """The render of one post with these flags, started if it isn't already running."""
    name = _name(tid, f)
    t = _jobs.get(name)
    if t is None:
        if len(_jobs) >= QUEUE:
            raise Busy()
        t = _jobs[name] = asyncio.create_task(_render(tid, f))
        t.add_done_callback(lambda t: (_jobs.pop(name, None), t.cancelled() or t.exception()))
    return t


def cancel(tid: str) -> int:
    """Stop every running render of a post, whatever its flags; how many."""
    hit = [t for name, t in _jobs.items() if name.split("-")[0] == tid and not t.done()]
    for t in hit:
        t.cancel()
    return len(hit)


async def media(tid: str, f: flags.Flags) -> dict:
    m = _meta(_name(tid, f))
    if m is not None:
        return m
    t = render(tid, f)
    try:
        return await asyncio.shield(t)
    except asyncio.CancelledError:
        # the render was stopped, not this request
        if t.cancelled() and not _current_cancelled():
            raise Cancelled() from None
        raise


def _current_cancelled() -> bool:
    task = asyncio.current_task()
    return bool(task and task.cancelling())


async def _post(tid: str, f: flags.Flags) -> dict:
    """The post as the flags have it: read to their quote depth, each video marked."""
    return flags.apply(await tweet.fetch(tid, f.depth), f)


def _make(post: dict, f: flags.Flags) -> shot.Shot:
    return shot.make(post, dict(planning.DEFAULTS, mode="shot", shot_stats=f.stats), f.lines or FULL, f.theme, f.start, f.end, f.media)


async def _render(tid: str, f: flags.Flags) -> dict:
    t0 = time.time()
    name = _name(tid, f)
    post = await _post(tid, f)
    s = await asyncio.to_thread(_make, post, f)
    L = s.layout
    meta = _describe(tid, post, L, f)
    try:
        async with _slots:
            # the poster is the card with each video's thumbnail drawn in: the png
            # itself for a post without video, the preview image for one with,
            # drawn while ffmpeg runs since Discord only waits on the mp4
            poster = asyncio.to_thread(card.render, L, False)
            if L.cells:
                png, _ = await asyncio.gather(poster, _mp4(s, DIR / f"{name}.mp4"))
            else:
                png = await poster
            _write(DIR / f"{name}.png", png)
    except asyncio.CancelledError:
        log.info("cancelled %s%s after %.1fs", tid, f.query(), time.time() - t0)
        try:
            (DIR / f"{name}.mp4").unlink(missing_ok=True)
        except OSError:
            pass   # still open for a stream; the prune takes it later
        raise
    _write(DIR / f"{name}.json", json.dumps(meta).encode())
    log.info("rendered %s%s %s %dx%d in %.1fs", tid, f.query(), meta["ext"], L.width, L.height, time.time() - t0)
    await asyncio.to_thread(_prune)
    return meta


async def _mp4(s: shot.Shot, dest: Path) -> None:
    """Compose straight into dest as a fragmented mp4, so it can be streamed
    while it grows; the json written after it is what marks it finished."""
    holes = await asyncio.to_thread(card.render, s.layout, True)
    # a keyframe every 2s: each fragment starts at one, so the first is out quickly
    dur = float(s.plan.args[0]) if s.plan.args else 0
    kbps = int(min(MAXRATE_KBPS, max(400, TARGET_MB * 8000 / dur - 160))) if dur else MAXRATE_KBPS
    argv = shot.command(s, PRESET) + ["-g", "60", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k", "-f", "mp4", "-movflags", FRAG, str(dest)]
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(holes), RENDER_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise rs.ResolveError("the video took too long to compose")
    except asyncio.CancelledError:
        # cancelling the task leaves ffmpeg running unless it is killed here
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        lines = err.decode(errors="replace").strip().splitlines()
        raise rs.ResolveError("the video couldn't be composed: " + (lines[-1][:160] if lines else "unknown error"))
    # repack with the index up front for players that seek; a stream still
    # reading the fragmented file keeps its handle, and if the swap fails
    # the fragmented file plays as it is
    packed = dest.with_name(dest.stem + ".packed.mp4")
    proc = await asyncio.create_subprocess_exec(shot.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(dest),
                                                "-c", "copy", "-movflags", "+faststart", str(packed),
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    if await proc.wait() == 0:
        try:
            packed.replace(dest)
        except OSError:
            packed.unlink(missing_ok=True)
    else:
        packed.unlink(missing_ok=True)


async def _tail(path: Path, job: asyncio.Task):
    """The file as ffmpeg writes it, until the render is done."""
    while not path.exists():
        if job.done():
            return
        await asyncio.sleep(0.1)
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 16)
            if chunk:
                yield chunk
            elif job.done():
                rest = f.read()
                if rest:
                    yield rest
                return
            else:
                await asyncio.sleep(0.1)


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


def _og(req: Request, m: dict, bare: bool = False) -> str:
    """The Open Graph page; `bare` is the image and nothing else, for Discord."""
    base = _base(req)
    e = lambda s: html.escape(str(s), quote=True)
    title = f"{m['name']} (@{m['handle']})"
    png, mp4 = _file(base, m, "png"), _file(base, m, "mp4")
    if bare:
        return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="theme-color" content="#000000">
<meta name="twitter:card" content="summary_large_image">
<meta property="og:image" content="{e(png)}">
<meta property="og:image:type" content="image/png">
<meta property="og:image:width" content="{m['width']}">
<meta property="og:image:height" content="{m['height']}">
</head><body></body></html>"""
    tags = [
        ("og:site_name", "x.com"), ("og:title", title), ("og:description", m["text"][:300]), ("og:url", m["url"]),
        ("og:image", png), ("og:image:type", "image/png"),
        ("og:image:width", m["width"]), ("og:image:height", m["height"]),
    ]
    if m["ext"] == "mp4":
        tags += [("og:type", "video.other"), ("og:video", mp4), ("og:video:secure_url", mp4),
                 ("og:video:type", "video/mp4"), ("og:video:width", m["width"]), ("og:video:height", m["height"])]
    card_kind = "player" if m["ext"] == "mp4" else "summary_large_image"
    head = "\n".join(f'<meta property="{k}" content="{e(v)}">' for k, v in tags)
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{e(title)}</title>
<meta name="theme-color" content="#000000">
<meta name="twitter:card" content="{card_kind}">
<meta name="twitter:image" content="{e(png)}">
{head}
<meta http-equiv="refresh" content="0; url={e(m['url'])}">
</head><body></body></html>"""


def _file(base: str, m: dict, ext: str) -> str:
    return f"{base}/m/{m['id']}.{ext}{m.get('query', '')}"


def _lead(post: dict) -> dict | None:
    """The video the render's length and sound come from."""
    return tweet.lead(tweet.videos(post))


def _length(v: dict, f: flags.Flags) -> float:
    """How long the render of a post led by `v` runs, after the clip."""
    full = v.get("duration") or 0
    end = min(f.end, full) if f.end and full else f.end or full
    return max(0.0, end - min(f.start, end))


def _text(post: dict) -> str:
    text = tweet.plain_text(post)
    q = post.get("quote")
    if q:
        text += f"\n\n↘️ Quoting {q['name']} (@{q['handle']})\n{tweet.plain_text(q)}"
    return text


def _plain(req: Request, post: dict, q: str = "") -> str:
    """A plain embed: name, text, and the video as x.com serves it. Discord
    shows no description on an embed with a video, so the text also goes in
    the oEmbed author name, which it does show."""
    e = lambda s: html.escape(str(s), quote=True)
    v = _lead(post)
    title = f"{post['name']} (@{post['handle']})"
    text = _text(post)
    tags = [
        ("og:title", title), ("og:description", text[:1000]), ("og:url", post["url"]), ("og:type", "video.other"),
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
<link rel="alternate" type="application/json+oembed" href="{e(_base(req))}/o/{e(post['id'])}.json{e(q)}">
{head}
<meta http-equiv="refresh" content="0; url={e(post['url'])}">
</head><body></body></html>"""


def _to_x(req: Request) -> RedirectResponse:
    return RedirectResponse(f"https://x.com{req.url.path}{flags.strip(req.query_params)}", status_code=302)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": VERSION, "rendering": len(_jobs)}


@app.get("/o/{name}")
async def oembed(name: str, req: Request):
    m = re.fullmatch(r"(\d{1,20})\.json", name)
    if not m:
        return Response(status_code=404)
    try:
        post = await _post(m.group(1), flags.parse(req.query_params))
    except rs.ResolveError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    # Discord shows about three lines of an author name and cuts it at 256
    # characters, so the line breaks go
    text = " ".join(_text(post).split())
    if len(text) > 256:
        text = text[:255].rstrip() + "…"
    return {"version": "1.0", "type": "link", "author_name": text, "author_url": post["url"]}


@app.get("/m/{name}")
async def file(name: str, req: Request):
    m = re.fullmatch(r"(\d{1,20})\.(mp4|png)", name)
    if not m:
        return Response(status_code=404)
    tid, ext = m.groups()
    f = flags.parse(req.query_params)
    stem = _name(tid, f)
    try:
        if ext == "mp4" and _meta(stem) is None:
            job = render(tid, f)
            if (await _layout_ext(tid, f)) == "mp4":
                log.info("streaming %s%s mid-render", tid, f.query())
                return StreamingResponse(_tail(DIR / f"{stem}.mp4", job), media_type="video/mp4", headers={"Cache-Control": "no-store"})
        meta = await media(tid, f)
    except Busy:
        return Response(status_code=503, headers={"Retry-After": "30"})
    except Cancelled:
        return JSONResponse({"error": "the render was cancelled"}, status_code=410)
    except rs.ResolveError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    if ext == "mp4" and meta["ext"] != "mp4":
        return Response(status_code=404)
    return FileResponse(DIR / f"{stem}.{ext}", media_type=f"video/mp4" if ext == "mp4" else "image/png",
                        headers={"Cache-Control": f"public, max-age={RENDER_TTL}"})


_cancelled: dict[str, tuple[float, int]] = {}
_help_png: dict[str, bytes] = {}


def _host(req: Request) -> str:
    return req.headers.get("host") or req.url.hostname or ""


def _help_text(req: Request) -> str:
    return flags.HELP.format(host=_host(req), long=f"{LONG_VIDEO:g}")


def _card(req: Request, title: str, text: str, image: str | None = None) -> HTMLResponse:
    """A plain link embed: a title, a line of text, and an image if given."""
    e = lambda s: html.escape(str(s), quote=True)
    tags = [("og:site_name", _host(req)), ("og:title", title), ("og:description", text)]
    if image:
        tags.append(("og:image", image))
    head = "\n".join(f'<meta property="{k}" content="{e(v)}">' for k, v in tags)
    return HTMLResponse(f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{e(title)}</title>
<meta name="theme-color" content="#1d9bf0">
<meta name="twitter:card" content="{'summary_large_image' if image else 'summary'}">
{head}
</head><body></body></html>""", headers={"Cache-Control": "no-store"})


@app.get("/help.png")
async def help_png(req: Request):
    host = _host(req)
    png = _help_png.get(host)
    if png is None:
        png = _help_png[host] = await asyncio.to_thread(_draw_help, _help_text(req))
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=3600"})


def _draw_help(text: str) -> bytes:
    """The guide as a dark card: headings in white, each flag in blue, its
    meaning in grey, in a monospace face so the columns stay lined up."""
    from PIL import Image, ImageDraw
    import io
    F = card.fonts()
    size, lh, pad = 26, 38, 40
    reg, bold = F.font("mono", size), F.font("mono-bold", size)
    T = card.THEMES["dark"]
    lines = text.rstrip("\n").splitlines()
    lines = [l for l in lines if not (l and set(l) <= {"="})]
    width = int(max(reg.getlength(l) for l in lines)) + 2 * pad
    im = Image.new("RGB", (width, len(lines) * lh + 2 * pad), T["bg"])
    d = ImageDraw.Draw(im)
    for i, l in enumerate(lines):
        y = pad + i * lh + lh - 10
        if not l.strip():
            continue
        if not l.startswith(" "):
            # a heading, or the opening lines; the first line is the title
            head = i == 0 or l.split("(")[0].strip().isupper()
            d.text((pad, y), l, font=bold if head else reg, fill=T["text"] if head else T["gray"], anchor="ls")
        elif l.startswith("    "):
            d.text((pad, y), l, font=reg, fill=T["blue"] if "://" in l else T["gray"], anchor="ls")
        else:
            # "  flag   meaning": the flag up to the first run of two spaces
            body = l[2:]
            cut = body.find("  ")
            flag, rest = (body, "") if cut < 0 else (body[:cut], body[cut:])
            x = pad + reg.getlength("  ")
            d.text((x, y), flag, font=bold, fill=T["blue"], anchor="ls")
            d.text((x + reg.getlength(flag), y), rest, font=reg, fill=T["text"], anchor="ls")
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


@app.get("/{path:path}")
async def post(path: str, req: Request):
    ua = req.headers.get("user-agent", "")
    m = STATUS.match(req.url.path)
    discord = bool(DISCORD.search(ua))
    f = flags.parse(req.query_params)
    bot = discord or bool(BOTS.search(ua))
    if f.help or req.url.path.rstrip("/") == "/help":
        if bot:
            return _card(req, "embed flags", f"Swap x.com for {_host(req)} in a post link, then add flags after ? joined by &. "
                                             f"The whole guide is in the image, and as text at {_base(req)}/help", f"{_base(req)}/help.png?v={VERSION}")
        return PlainTextResponse(_help_text(req), headers={"Cache-Control": "no-store"})
    if m and f.cancel:
        tid = m.group(1)
        n = cancel(tid)
        # a chat app's unfurler fetches a link more than once: the later
        # fetches report the cancel the first one made
        if n:
            _cancelled[tid] = (time.time(), n)
        elif time.time() - _cancelled.get(tid, (0, 0))[0] < 120:
            n = _cancelled[tid][1]
        log.info("cancel %s: %d render(s)", tid, n)
        msg = f"stopped {n} render{'s' if n != 1 else ''} of {tid}" if n else f"nothing of {tid} is rendering"
        if bot:
            return _card(req, "render cancelled" if n else "nothing to cancel", msg)
        return PlainTextResponse(msg + "\n", headers={"Cache-Control": "no-store"})
    if not m or not (bot or f.raw):
        return _to_x(req)
    tid = m.group(1)
    try:
        meta = _meta(_name(tid, f))
        if meta is None or f.plain:
            post_ = await _post(tid, f)
            lead = _lead(post_)
            if lead and (f.plain or _length(lead, f) > LONG_VIDEO):
                # too long to render: x.com's own file, or a plain embed of it
                if f.raw or f.media and discord:
                    return RedirectResponse(lead["url"], status_code=302)
                return HTMLResponse(_plain(req, post_, f.query()))
        if meta is None:
            # the tags and the redirect need only the size and kind, so they go
            # out while the render runs; the file requests that follow stream
            # the mp4 as it is written, or wait on the png
            render(tid, f)
            meta = await asyncio.to_thread(_pending, tid, post_, f)
    except Busy:
        return _to_x(req)
    except rs.ResolveError as e:
        log.info("post %s: %s", tid, e)
        return _to_x(req)
    if f.raw or discord and meta["ext"] == "mp4":
        return RedirectResponse(_file(_base(req), meta, meta["ext"]), status_code=302)
    return HTMLResponse(_og(req, meta, bare=discord))


async def _layout_ext(tid: str, f: flags.Flags) -> str:
    """Whether a post renders to mp4 or png, from its layout alone."""
    post = await _post(tid, f)
    return (await asyncio.to_thread(_pending, tid, post, f))["ext"]


def _pending(tid: str, post: dict, f: flags.Flags) -> dict:
    """What the tags need while the render is still running."""
    return _describe(tid, post, card.layout(post, f.theme, f.stats, max_lines=f.lines or FULL, media_only=f.media), f)


def _describe(tid: str, post: dict, L: card.Layout, f: flags.Flags) -> dict:
    return {"id": tid, "url": post["url"], "name": post["name"], "handle": post["handle"], "text": tweet.plain_text(post),
            "width": L.width, "height": L.height, "ext": "mp4" if L.cells else "png", "query": f.query()}
