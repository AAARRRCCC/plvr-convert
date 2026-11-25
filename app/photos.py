"""The photos of an x.com post, one at a time or all of them in one zip.

yt-dlp reads only a post's videos, so a post of photos is read from fxtwitter,
which lists every one at its original size. A zip is streamed as it is built:
each photo is read whole (they are a few MB at most), then sent as a stored
entry with its CRC and size in its own header, so any unzipper can read it.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
import zlib
from datetime import datetime
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx

from . import plan as planning
from . import resolve as rs
from . import tunnel, tweet

log = logging.getLogger("convert.photos")

MAX_PHOTOS = 100
PHOTO_MAX = 40 << 20
AHEAD = 4          # photos read at once, the one being sent among them
TRIES = 3
HOST = "pbs.twimg.com"
EXTS = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}


# ---------------------------------------------------------------- reading ---

async def post(tid: str) -> dict:
    """{"id", "handle", "title", "created", "photos": [{"url", "w", "h"}], "videos"}:
    every photo of the post at its original size, and how many videos or gifs it has."""
    key = f"photos:{tid}"
    hit = rs.cached(key)
    if hit is not None:
        return hit
    raw = await tweet._fetch_raw(tid)
    media = (raw.get("media") or {}).get("all") or []
    photos = []
    for m in media:
        u = urlparse(str(m.get("url") or ""))
        if m.get("type") == "photo" and u.scheme == "https" and u.hostname == HOST:
            photos.append({"url": u._replace(query="name=orig").geturl(), "w": int(m.get("width") or 0), "h": int(m.get("height") or 0)})
    a = raw.get("author") or {}
    text = "".join(s["text"] for s in tweet._spans(raw)).strip()
    out = {
        "id": str(raw.get("id") or tid),
        "handle": a.get("screen_name") or "",
        "name": a.get("name") or a.get("screen_name") or "",
        "title": text or f"a post by @{a.get('screen_name') or '?'}",
        "created": float(raw.get("created_timestamp") or time.time()),
        "url": raw.get("url") or f"https://x.com/i/status/{tid}",
        "photos": photos[:MAX_PHOTOS],
        "videos": sum(1 for m in media if m.get("type") in ("video", "gif")),
    }
    rs._put(key, out)
    return out


def describe(p: dict) -> dict:
    """What the page shows: the post and a thumbnail of each photo."""
    return {
        "title": p["title"], "uploader": f"{p['name']} @{p['handle']}", "url": p["url"], "extractor": "twitter",
        "photos": [{"index": i, "thumbnail": ph["url"].replace("name=orig", "name=small"), "large": ph["url"].replace("name=orig", "name=large"),
                    "w": ph["w"], "h": ph["h"]}
                   for i, ph in enumerate(p["photos"])],
    }


def _ext(url: str) -> str:
    e = urlparse(url).path.rsplit(".", 1)[-1].lower()
    return "jpg" if e == "jpeg" else e if e in EXTS else "jpg"


def _info(p: dict) -> dict:
    return {"title": p["title"], "id": p["id"], "extractor_key": "twitter"}


def filename(p: dict, o: dict, sel: list[int]) -> str:
    """The download's name in the chosen style: one photo's, or the zip's for several."""
    if len(sel) > 1:
        n = len(p["photos"])
        return planning.filename(_info(p), o, "zip", None, None, f"{n} photos" if len(sel) == n else f"{len(sel)} of {n} photos")
    index = sel[0]
    ext = _ext(p["photos"][index]["url"])
    n = f"{index + 1:02}"
    name = planning.filename(_info(p), o, ext, None, None, f"photo {n}")
    if o["name"] == "random" or (o["name"] == "custom" and not o.get("custom_name", "").strip()):
        name = f"{name[:-len(ext) - 1]}_{n}.{ext}"   # the random style drops the tag; keep photos apart
    return name


def member(p: dict, index: int) -> str:
    """A photo's name inside the zip: numbered by its place in the post, so
    the files sort in its order and a few picked out keep their numbers."""
    return f"{p['handle'] or 'x'}_{p['id']}_{index + 1:02}.{_ext(p['photos'][index]['url'])}"


async def fetch(url: str) -> bytes:
    """A photo's bytes, read up to PHOTO_MAX and no further."""
    u = urlparse(url)
    if u.scheme != "https" or u.hostname != HOST:
        raise rs.ResolveError("that isn't an x.com photo")
    err = "x.com didn't send the photo"
    for n in range(TRIES):
        try:
            req = tunnel.client().build_request("GET", url, headers={"User-Agent": tweet.UA, "Accept-Encoding": "identity"})
            resp = await tunnel.client().send(req, stream=True)
            try:
                size = resp.headers.get("content-length", "")
                if resp.status_code == 404:
                    raise rs.ResolveError("a photo in that post is gone")
                if resp.status_code >= 400:
                    err = f"x.com refused a photo ({resp.status_code})"
                elif size.isdigit() and int(size) > PHOTO_MAX:
                    raise rs.ResolveError("a photo in that post is too big")
                else:
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        if len(buf) > PHOTO_MAX:
                            raise rs.ResolveError("a photo in that post is too big")
                    return bytes(buf)
            finally:
                await resp.aclose()
        except httpx.HTTPError as e:
            err = "x.com didn't send the photo"
            log.info("photo %s try %d: %s", url[-30:], n + 1, type(e).__name__)
        await asyncio.sleep(0.4 * (n + 1))
    raise rs.ResolveError(err)


# -------------------------------------------------------------------- zip ---

def _dos(ts: float) -> tuple[int, int]:
    d = datetime.fromtimestamp(max(ts, 315532800))   # zip dates start in 1980
    return (d.hour << 11) | (d.minute << 5) | (d.second // 2), ((d.year - 1980) << 9) | (d.month << 5) | d.day


class Zip:
    """A stored (uncompressed) zip written front to back: `add` returns the
    bytes of one entry, `end` the central directory. Photos are already
    compressed, so deflating them would only cost time. No zip64: the photos
    of one post come nowhere near 4 GB."""

    def __init__(self, ts: float):
        self.time, self.date = _dos(ts)
        self.offset = 0
        self.central: list[bytes] = []

    def add(self, name: str, data: bytes) -> bytes:
        n = name.encode()
        flags = 0 if name.isascii() else 0x800
        crc = zlib.crc32(data)
        head = struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, 0, self.time, self.date, crc, len(data), len(data), len(n), 0) + n
        self.central.append(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flags, 0, self.time, self.date, crc,
                                        len(data), len(data), len(n), 0, 0, 0, 0, 0o100644 << 16, self.offset) + n)
        self.offset += len(head) + len(data)
        return head + data

    def end(self) -> bytes:
        cd = b"".join(self.central)
        return cd + struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(self.central), len(self.central), len(cd), self.offset, 0)


async def first_and_rest(p: dict, sel: list[int]) -> tuple[bytes, AsyncIterator[bytes]]:
    """The zip's first entry, read before the response starts so a photo that
    can't be read is a plain error, and an iterator over the rest. A photo
    that fails later ends the stream unfinished: a zip without its central
    directory is a broken download, not one that looks whole and is missing photos."""
    z = Zip(p["created"])

    def get(k: int) -> asyncio.Future:
        return asyncio.ensure_future(fetch(p["photos"][sel[k]]["url"]))

    tasks = [get(k) for k in range(min(AHEAD, len(sel)))]
    try:
        first = z.add(member(p, sel[0]), await tasks[0])
    except BaseException:
        for t in tasks:
            t.cancel()
        raise

    async def rest() -> AsyncIterator[bytes]:
        t0, sent = time.time(), len(first)
        try:
            for k in range(1, len(sel)):
                if k + AHEAD - 1 < len(sel):
                    tasks.append(get(k + AHEAD - 1))
                chunk = z.add(member(p, sel[k]), await tasks[k])
                sent += len(chunk)
                yield chunk
            yield z.end()
            log.info("zip %s %d of %d photos %.1fMB %.1fs", p["id"], len(sel), len(p["photos"]), sent / 1e6, time.time() - t0)
        except rs.ResolveError as e:
            log.warning("zip %s stopped at a photo: %s", p["id"], e)
            raise   # the connection drops, so the browser marks the download failed
        finally:
            for t in tasks:
                t.cancel()

    return first, rest()
