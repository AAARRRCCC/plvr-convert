"""convert.plvr.net: paste a link, get a file.

POST /api/resolve   {url}                       -> what's there
POST /api/prepare   {url, item?, options}       -> a signed download link
GET  /api/download/<name>?t=<token>             -> the file, streamed
GET  /api/sites                                 -> what yt-dlp knows
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
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import plan as planning
from . import resolve as rs
from . import stream, tokens, tunnel

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
    streams open at once, so one person cannot occupy the whole pod."""

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


@app.post("/api/prepare")
async def api_prepare(req: Request):
    body = await req.json()
    url = str(body.get("url") or "").strip()
    item = body.get("item")
    opts = planning.normalize(body.get("options"))
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
    try:
        key, info = await rs.resolve(payload["u"])
        media = rs.pick(info, int(payload["i"]) if payload.get("i") is not None else None)
        p = planning.make(media, opts)
    except rs.ResolveError as e:
        return err(str(e), 422)
    if not gate.acquire(ip):
        return err("too many downloads running right now, try again in a moment", 429)
    try:
        if p.method == "proxy":
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
        "Content-Disposition": f"attachment; filename=\"{p.filename.encode('ascii', 'replace').decode()}\"; filename*=UTF-8''{quote(p.filename)}",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(body(), status_code=started.status, media_type=p.mime, headers=headers)


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


app.mount("/", StaticFiles(directory=STATIC), name="static")
