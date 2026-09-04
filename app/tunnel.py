"""Fetching upstream files the way the sites want them fetched.

Two jobs live here. `fetch` reads one upstream file, honouring a Range and
splitting the read into ranged chunks when the site throttles unranged
requests (YouTube: ~20 KB/s without a Range header, full speed with one).
`serve` is a loopback HTTP server, bound to 127.0.0.1 only, that exposes
registered inputs to ffmpeg so ffmpeg's own reads and seeks become those
same well-behaved requests instead of one long throttled GET.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from typing import AsyncIterator

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from .resolve import Fmt, ResolveError, assert_public

log = logging.getLogger("convert.tunnel")

CHUNK = 1 << 16
PORT = int(os.environ.get("TUNNEL_PORT", 8081))
INPUT_TTL = 6 * 3600

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=60), http2=False)
    return _client


class Fetched:
    """An upstream read that has begun: status and headers for the response, and the body."""

    def __init__(self, status: int, headers: dict, first: bytes, rest: AsyncIterator[bytes]):
        self.status, self.headers, self.first, self.rest = status, headers, first, rest
        self.length = int(headers["content-length"]) if str(headers.get("content-length", "")).isdigit() else None

    async def body(self) -> AsyncIterator[bytes]:
        if self.first:
            yield self.first
        async for chunk in self.rest:
            yield chunk


def _parse_range(h: str | None) -> tuple[int, int | None] | None:
    if not h:
        return None
    m = re.fullmatch(r"bytes=(\d+)-(\d*)", h.strip())
    if not m:
        return None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


async def _first(it: AsyncIterator[bytes]) -> bytes:
    try:
        return await it.__anext__()
    except StopAsyncIteration:
        return b""


async def fetch(f: Fmt, range_header: str | None) -> Fetched:
    assert_public(f.url)
    headers = dict(f.headers)
    headers["Accept-Encoding"] = "identity"
    rng = _parse_range(range_header)
    if not f.chunk:
        return await _plain(f.url, headers, range_header if rng else None)
    return await _chunked(f, headers, rng)


async def _plain(url: str, headers: dict, range_header: str | None) -> Fetched:
    if range_header:
        headers["Range"] = range_header
    resp = await client().send(client().build_request("GET", url, headers=headers), stream=True)
    if resp.status_code >= 400:
        await resp.aclose()
        raise ResolveError(f"the site refused the file ({resp.status_code})")
    it = resp.aiter_bytes(CHUNK)
    first = await _first(it)
    out = {k: resp.headers[k] for k in ("content-length", "content-range", "accept-ranges", "last-modified", "etag") if k in resp.headers}

    async def rest():
        try:
            async for chunk in it:
                yield chunk
        except httpx.HTTPError as e:
            log.warning("upstream read ended early: %s", type(e).__name__)
        finally:
            await resp.aclose()

    return Fetched(resp.status_code if resp.status_code in (200, 206) else 200, out, first, rest())


async def _chunked(f: Fmt, headers: dict, rng: tuple[int, int | None] | None) -> Fetched:
    """Read [start, end] as a series of ranged requests of f.chunk bytes each."""
    start, end = rng if rng else (0, None)
    size = f.chunk

    async def one(a: int, b: int) -> httpx.Response:
        h = dict(headers, Range=f"bytes={a}-{b}")
        resp = await client().send(client().build_request("GET", f.url, headers=h), stream=True)
        if resp.status_code >= 400:
            await resp.aclose()
            raise ResolveError(f"the site refused the file ({resp.status_code})")
        return resp

    last = end if end is not None else start + size - 1
    resp = await one(start, min(last, start + size - 1))
    total = None
    m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", resp.headers.get("content-range", ""))
    if resp.status_code == 206 and m and m.group(3) != "*":
        total = int(m.group(3))
    if resp.status_code != 206 or total is None:
        # the site ignored the Range; just pass through what it sent
        it = resp.aiter_bytes(CHUNK)
        first = await _first(it)
        out = {k: resp.headers[k] for k in ("content-length", "content-range") if k in resp.headers}

        async def rest_plain():
            try:
                async for c in it:
                    yield c
            finally:
                await resp.aclose()

        return Fetched(resp.status_code, out, first, rest_plain())

    stop = min(end, total - 1) if end is not None else total - 1  # inclusive
    it = resp.aiter_bytes(CHUNK)
    first = await _first(it)

    async def rest(resp=resp, it=it):
        pos = start
        try:
            while True:
                got = len(first) if pos == start else 0
                async for c in it:
                    got += len(c)
                    yield c
                await resp.aclose()
                pos += got
                if pos > stop or got == 0:
                    return
                resp = await one(pos, min(stop, pos + size - 1))
                it = resp.aiter_bytes(CHUNK)
        except httpx.HTTPError as e:
            log.warning("upstream chunk read ended early: %s", type(e).__name__)
        finally:
            await resp.aclose()

    if rng:
        out = {"content-range": f"bytes {start}-{stop}/{total}", "content-length": str(stop - start + 1), "accept-ranges": "bytes"}
        return Fetched(206, out, first, rest())
    return Fetched(200, {"content-length": str(total), "accept-ranges": "bytes"}, first, rest())


# --------------------------------------------------------------- loopback ---

_inputs: dict[str, tuple[Fmt, float]] = {}


def register(f: Fmt) -> str:
    now = time.time()
    for k in [k for k, (_, t) in _inputs.items() if t < now]:
        del _inputs[k]
    key = secrets.token_urlsafe(18)
    _inputs[key] = (f, now + INPUT_TTL)
    return f"http://127.0.0.1:{PORT}/in/{key}"


async def _serve_input(req: Request):
    hit = _inputs.get(req.path_params["key"])
    if not hit or hit[1] < time.time() or (req.client and req.client.host not in ("127.0.0.1", "::1")):
        return Response(status_code=404)
    f = hit[0]
    if req.method == "HEAD":
        return Response(status_code=200, headers={"accept-ranges": "bytes"})
    try:
        got = await fetch(f, req.headers.get("range"))
    except ResolveError as e:
        log.warning("loopback input failed: %s", e)
        return Response(status_code=502)
    return StreamingResponse(got.body(), status_code=got.status, headers=got.headers, media_type="application/octet-stream")


loopback = Starlette(routes=[Route("/in/{key}", _serve_input, methods=["GET", "HEAD"])])


async def serve() -> None:
    import uvicorn
    cfg = uvicorn.Config(loopback, host="127.0.0.1", port=PORT, log_level="warning", access_log=False, lifespan="off")
    server = uvicorn.Server(cfg)
    log.info("loopback tunnel on 127.0.0.1:%d", PORT)
    await server.serve()
