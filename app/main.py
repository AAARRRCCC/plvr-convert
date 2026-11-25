"""convert.plvr.net: a self-hosted video downloader and remuxer.

POST /api/resolve   {url, mode?}                 -> the media at that link (mode "shot": the post itself;
                                                   mode "photos", or an x.com post of only photos: each photo)
POST /api/prepare   {url, item?, options}       -> a signed download link
                    {url, photos, options}      -> photos of a post (a list of indexes, or "all"): one as itself, more as a zip
GET  /api/download/<name>?t=<token>             -> the file, streamed
GET  /api/sites                                 -> the sites yt-dlp supports
GET  /shortcuts/{photos,files}.shortcut          -> the signed iOS companion shortcuts
GET  /healthz
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote

import yt_dlp
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import plan as planning
from . import resolve as rs
from . import photos, shot, stream, tokens, tunnel, tweet

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("convert")
logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every upstream url at INFO

STATIC = Path(__file__).resolve().parent.parent / "static"
VERSION = os.environ.get("APP_VERSION", "dev")
MAX_STREAMS = int(os.environ.get("MAX_STREAMS", 6))
MAX_STREAMS_PER_IP = int(os.environ.get("MAX_STREAMS_PER_IP", 3))
RESOLVES_PER_MIN = int(os.environ.get("RESOLVES_PER_MIN", 20))

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    # the loopback tunnel ffmpeg reads its inputs through; same process, same loop
    task = asyncio.create_task(tunnel.serve())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="convert.plvr.net", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


# ------------------------------------------------------------ throttling ---

class Gate:
    """Per-client and global caps: a resolve budget per minute, and a cap on
    streams open at once, so one client cannot use all of the pod's capacity."""

    def __init__(self):
        self.recent: dict[str, deque] = defaultdict(deque)
        self.open: dict[str, int] = defaultdict(int)
        self.total = 0

    def resolve_ok(self, ip: str) -> bool:
        q, now = self.recent[ip], time.time()
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RESOLVES_PER_MIN:
            return False
        q.append(now)
        return True

    def acquire(self, ip: str) -> bool:
        if self.total >= MAX_STREAMS or self.open[ip] >= MAX_STREAMS_PER_IP:
            return False
        self.total += 1
        self.open[ip] += 1
        return True

    def release(self, ip: str) -> None:
        self.total -= 1
        self.open[ip] -= 1
        if self.open[ip] <= 0:
            del self.open[ip]


gate = Gate()


def client_ip(req: Request) -> str:
    h = req.headers
    return (h.get("cf-connecting-ip") or (h.get("x-forwarded-for") or "").split(",")[0].strip() or (req.client.host if req.client else "?"))


def err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


# --------------------------------------------------------------- the api ---

@app.post("/api/resolve")
async def api_resolve(req: Request):
    ip = client_ip(req)
    if not gate.resolve_ok(ip):
        return err("slow down a little: too many links in a minute", 429)
    body = await req.json()
    url = str(body.get("url") or "").strip()
    if not url:
        return err("paste a link first")
    if str(body.get("mode") or "") == "shot":
        return await _resolve_post(url, body.get("options"))
    if str(body.get("mode") or "") == "photos":
        return await _resolve_photos(url)
    if (tid := tweet.status_id(url)) and (p := await _photo_post(tid)):
        return {"key": rs.cache_key(url), "photos": photos.describe(p)}
    try:
        key, info = await rs.resolve(url)
    except rs.ResolveError as e:
        return err(str(e), 422)
    except Exception:
        log.exception("resolve failed")
        return err("something broke reading that link", 500)
    es = rs.entries(info)
    if not es:
        return err("nothing downloadable at that link", 422)
    if len(es) == 1:
        return {"key": key, "media": rs.describe(es[0])}
    return {"key": key, "picker": [dict(rs.describe(e), index=i) for i, e in enumerate(es)],
            "title": info.get("title"), "uploader": info.get("uploader") or info.get("channel")}


async def _resolve_post(url: str, options) -> JSONResponse | dict:
    tid = tweet.status_id(url)
    if not tid:
        return err("screenshots work for x.com and twitter.com post links", 422)
    opts = planning.normalize(options)
    try:
        post = await tweet.fetch(tid, opts["shot_depth"])
    except rs.ResolveError as e:
        return err(str(e), 422)
    except Exception:
        log.exception("post fetch failed")
        return err("something broke reading that post", 500)
    return {"key": rs.cache_key(url), "media": shot.describe(post)}


async def _resolve_photos(url: str) -> JSONResponse | dict:
    """Every photo of the post, whatever else it has."""
    tid = tweet.status_id(url)
    if not tid:
        return err("photos work for x.com and twitter.com post links", 422)
    try:
        p = await photos.post(tid)
    except rs.ResolveError as e:
        return err(str(e), 422)
    except Exception:
        log.exception("photos %s", tid)
        return err("something broke reading that post", 500)
    if not p["photos"]:
        return err("that post has no photos", 422)
    return {"key": rs.cache_key(url), "photos": photos.describe(p)}


async def _photo_post(tid: str) -> dict | None:
    """The post, if it is photos and nothing else; yt-dlp reads every other
    post, and reads this one too when fxtwitter can't be reached."""
    try:
        p = await photos.post(tid)
    except rs.ResolveError as e:
        log.info("photos %s: %s, trying yt-dlp", tid, e)
        return None
    except Exception:
        log.exception("photos %s", tid)
        return None
    return p if p["photos"] and not p["videos"] else None


async def _photos_for(url: str, which) -> tuple[dict, list[int]]:
    """The post and the photos asked for ("all", or a list of indexes), as
    indexes in the post's order."""
    tid = tweet.status_id(url)
    p = await photos.post(tid) if tid else None
    if not p or not p["photos"]:
        raise rs.ResolveError("there are no photos at that link")
    n = len(p["photos"])
    if which == "all":
        return p, list(range(n))
    if not isinstance(which, list) or not which or not all(type(i) is int and 0 <= i < n for i in which):
        raise rs.ResolveError("pick which photos you want")
    return p, sorted(set(which))


async def _shot_for(url: str, opts: dict) -> shot.Shot:
    tid = tweet.status_id(url)
    if not tid:
        raise rs.ResolveError("screenshots work for x.com and twitter.com post links")
    post = await tweet.fetch(tid, opts["shot_depth"])
    return await asyncio.to_thread(shot.make, post, opts)


@app.post("/api/prepare")
async def api_prepare(req: Request):
    body = await req.json()
    url = str(body.get("url") or "").strip()
    item = body.get("item")
    opts = planning.normalize(body.get("options"))
    if body.get("photos") is not None:
        try:
            p, sel = await _photos_for(url, body["photos"])
        except rs.ResolveError as e:
            return err(str(e), 422)
        name = photos.filename(p, opts, sel)
        n = len(p["photos"])
        payload = {"u": url, "p": "all" if len(sel) == n else sel, "o": {k: v for k, v in opts.items() if v != planning.DEFAULTS.get(k)}}
        t = tokens.sign(payload)
        one = p["photos"][sel[0]]
        label = (f"photo {sel[0] + 1} of {n} · {one['w']}x{one['h']}" if len(sel) == 1
                 else f"{n} photos · zip" if len(sel) == n else f"{len(sel)} of {n} photos · zip")
        return {"token": t, "url": f"/api/download/{quote(name)}?t={t}", "method": "proxy", "ext": name.rsplit(".", 1)[-1],
                "filename": name, "label": label, "size": 0}
    if opts["mode"] == "shot":
        try:
            s = await _shot_for(url, opts)
        except rs.ResolveError as e:
            return err(str(e), 422)
        except Exception:
            log.exception("shot prepare failed")
            return err("couldn't lay out that post", 500)
        payload = {"u": url, "i": None, "o": {k: v for k, v in opts.items() if v != planning.DEFAULTS.get(k)}}
        t = tokens.sign(payload)
        return {"token": t, "url": f"/api/download/{quote(s.plan.filename)}?t={t}", **s.plan.describe(), "width": s.layout.width, "height": s.layout.height}
    try:
        key, info = await rs.resolve(url)
        media = rs.pick(info, int(item) if item is not None else None)
        p = planning.make(media, opts)
    except rs.ResolveError as e:
        return err(str(e), 422)
    except Exception:
        log.exception("prepare failed")
        return err("couldn't work out how to download that", 500)
    payload = {"u": url, "i": item, "o": {k: v for k, v in opts.items() if v != planning.DEFAULTS.get(k)}}
    t = tokens.sign(payload)
    return {"token": t, "url": f"/api/download/{quote(p.filename)}?t={t}", **p.describe()}


@app.get("/api/download/{name}")
async def api_download(name: str, t: str, req: Request):
    payload = tokens.verify(t)
    if not payload:
        return err("that link has expired, paste the video again", 403)
    ip = client_ip(req)
    opts = planning.normalize(payload.get("o"))
    if payload.get("p") is not None:
        return await _download_photos(payload, opts, ip)
    s = None
    try:
        if opts["mode"] == "shot":
            s = await _shot_for(payload["u"], opts)
            p = s.plan
        else:
            key, info = await rs.resolve(payload["u"])
            media = rs.pick(info, int(payload["i"]) if payload.get("i") is not None else None)
            p = planning.make(media, opts)
    except rs.ResolveError as e:
        return err(str(e), 422)
    if not gate.acquire(ip):
        return err("too many downloads running right now, try again in a moment", 429)
    try:
        if s is not None:
            started = await shot.start(s)
        elif p.method == "proxy":
            started = await stream.proxy(p, req.headers.get("range"))
        else:
            started = await stream.ffmpeg(p, opts["start"])
    except rs.ResolveError as e:
        gate.release(ip)
        return err(str(e), 502)
    except Exception:
        gate.release(ip)
        log.exception("stream failed to start")
        return err("the download couldn't start", 502)

    async def body():
        try:
            async for chunk in started.body():
                yield chunk
        finally:
            gate.release(ip)

    headers = {
        **started.headers,
        "Content-Disposition": _attachment(p.filename),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(body(), status_code=started.status, media_type=p.mime, headers=headers)


def _attachment(filename: str) -> str:
    return f"attachment; filename=\"{filename.encode('ascii', 'replace').decode()}\"; filename*=UTF-8''{quote(filename)}"


async def _download_photos(payload: dict, opts: dict, ip: str):
    try:
        p, sel = await _photos_for(payload["u"], payload["p"])
    except rs.ResolveError as e:
        return err(str(e), 422)
    name = photos.filename(p, opts, sel)
    i = sel[0] if len(sel) == 1 else None
    if not gate.acquire(ip):
        return err("too many downloads running right now, try again in a moment", 429)
    headers = {"Content-Disposition": _attachment(name), "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    try:
        if i is not None:
            data = await photos.fetch(p["photos"][i]["url"])
        else:
            first, rest = await photos.first_and_rest(p, sel)
    except rs.ResolveError as e:
        gate.release(ip)
        return err(str(e), 502)
    except Exception:
        gate.release(ip)
        log.exception("photos failed to start")
        return err("the download couldn't start", 502)
    if i is not None:
        gate.release(ip)
        log.info("photo %s %d of %d %.1fMB", p["id"], i + 1, len(p["photos"]), len(data) / 1e6)
        return Response(data, media_type=photos.EXTS[name.rsplit(".", 1)[-1]], headers=headers)

    async def body():
        try:
            yield first
            async for chunk in rest:
                yield chunk
        finally:
            gate.release(ip)

    return StreamingResponse(body(), media_type="application/zip", headers=headers)


_sites: list[str] | None = None


@app.get("/api/sites")
async def api_sites():
    global _sites
    if _sites is None:
        _sites = await asyncio.to_thread(rs.extractor_names)
    return JSONResponse({"count": len(_sites), "names": _sites, "ytdlp": yt_dlp.version.__version__},
                        headers={"Cache-Control": "public, max-age=3600"})


def _ffmpeg_version() -> str | None:
    try:
        out = subprocess.run([stream.FFMPEG, "-version"], capture_output=True, text=True, timeout=5).stdout
        return out.split()[2] if out.startswith("ffmpeg version") else None
    except Exception:
        return None


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": VERSION, "ytdlp": yt_dlp.version.__version__, "ffmpeg": _ffmpeg_version(),
            "deno": bool(shutil.which("deno")), "streams": gate.total}


# --------------------------------------------------------------- the page ---

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


SHORTCUTS = {"photos", "files"}


@app.get("/shortcuts/{which}.shortcut")
async def shortcut(which: str):
    # iOS names an imported shortcut after its file: "convert to photos". The
    # files are signed (tools/make_shortcuts.py says how); iOS refuses unsigned.
    if which not in SHORTCUTS:
        return err("no such shortcut", 404)
    return FileResponse(STATIC / "shortcuts" / f"{which}.shortcut", media_type="application/octet-stream",
                        filename=f"convert to {which}.shortcut", headers={"Cache-Control": "public, max-age=3600"})


@app.middleware("http")
async def vendor_cache(req: Request, call_next):
    # the vendored ffmpeg.wasm lives under versioned paths, so a browser keeps
    # the 32 MB core for good instead of fetching it each visit
    res = await call_next(req)
    if req.url.path.startswith("/vendor/") and res.status_code == 200:
        res.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return res


app.mount("/", StaticFiles(directory=STATIC), name="static")
