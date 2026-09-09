"""The two ways bytes reach the browser: passed through from the site, or read
from an ffmpeg pipe. Nothing is written to disk.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import AsyncIterator

from . import id3, tunnel
from .plan import Plan, build_inputs, shell
from .resolve import Fmt, ResolveError, assert_public

log = logging.getLogger("convert.stream")

CHUNK = 1 << 16
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FIRST_BYTE_TIMEOUT = float(os.environ.get("FIRST_BYTE_TIMEOUT", 60))


class Started:
    """A stream that has produced its first bytes (so its response can be a 200)
    plus a way to read the rest."""

    def __init__(self, first: bytes, rest: AsyncIterator[bytes], status: int = 200, headers: dict | None = None):
        self.first, self.rest, self.status, self.headers = first, rest, status, headers or {}

    async def body(self) -> AsyncIterator[bytes]:
        if self.first:
            yield self.first
        async for chunk in self.rest:
            yield chunk


async def proxy(plan: Plan, range_header: str | None) -> Started:
    assert plan.src is not None
    got = await tunnel.fetch(plan.src, range_header)
    log.info("stream proxy %s: %s", plan.ext, plan.label)
    return Started(got.first, got.rest, got.status, got.headers)


async def cover_jpeg(url: str) -> bytes | None:
    """The thumbnail as a jpeg no wider than 600px, via a short ffmpeg run;
    None if it cannot be fetched or decoded; the audio is then sent without it."""
    try:
        assert_public(url)
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", url,
            "-frames:v", "1", "-vf", "scale='min(600,iw)':-2", "-c:v", "mjpeg", "-q:v", "4", "-f", "image2pipe", "pipe:1",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 15)
        return out if proc.returncode == 0 and 0 < len(out) < (1 << 20) else None
    except Exception:
        return None


def _urlfor(f: Fmt) -> str:
    """ffmpeg opens plain files through the loopback tunnel, so its reads and
    seeks become ranged requests with the site's headers; HLS stays direct."""
    return tunnel.register(f) if f.direct else f.url


async def ffmpeg(plan: Plan, start: float | None) -> Started:
    """Run the plan; if ffmpeg exits before producing its first byte, try the plan's fallbacks."""
    tried: list[str] = []
    prefix = b""
    if plan.ext == "mp3" and plan.tags:
        prefix = id3.build(plan.tags, await cover_jpeg(plan.cover) if plan.cover else None)
    for p in [plan, *plan.fallbacks]:
        for f in p.inputs:
            assert_public(f.url)
        argv = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"] + build_inputs(p, start, _urlfor) + p.args
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=CHUNK * 4)
        err: list[bytes] = []

        async def drain(proc=proc, err=err):
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                if len(err) < 40:
                    err.append(line)

        drainer = asyncio.create_task(drain())
        t0 = time.time()
        try:
            first = await asyncio.wait_for(proc.stdout.read(CHUNK), FIRST_BYTE_TIMEOUT)
        except asyncio.TimeoutError:
            first = b""
        if first:
            log.info("stream ffmpeg %s started in %.1fs: %s", p.ext, time.time() - t0, p.label)
            return Started(prefix + first, _rest(proc, drainer, err, p))
        await _kill(proc)
        await drainer
        msg = b"".join(err).decode(errors="replace").strip()
        tried.append(msg.splitlines()[-1][:200] if msg else "no output")
        log.warning("ffmpeg produced nothing (%s): %s | %s", p.label, tried[-1], shell(argv))
    raise ResolveError("the streams couldn't be read: " + (tried[-1] or "unknown error"))


async def _rest(proc, drainer, err, plan) -> AsyncIterator[bytes]:
    sent = CHUNK
    t0 = time.time()
    try:
        while True:
            chunk = await proc.stdout.read(CHUNK)
            if not chunk:
                break
            sent += len(chunk)
            yield chunk
        await proc.wait()
        await drainer
        if proc.returncode != 0:
            msg = b"".join(err).decode(errors="replace").strip().splitlines()
            log.warning("ffmpeg exit %s after %d bytes (%s): %s", proc.returncode, sent, plan.label, msg[-1][:200] if msg else "")
        else:
            log.info("stream done %s %.1fMB %.1fs", plan.ext, sent / 1e6, time.time() - t0)
    finally:
        await _kill(proc)
        drainer.cancel()


async def _kill(proc) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            pass
