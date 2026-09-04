"""yt-dlp in, a plain description of the media out.

Everything the site knows about a link comes from one extract_info call; the
result is cached briefly so the download step does not pay for a second one.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import yt_dlp

log = logging.getLogger("convert.resolve")

MAX_PICKER = 24
CACHE_TTL = 15 * 60
CACHE_MAX = 200
EXTRACT_TIMEOUT = 90

AUDIO_EXTS = {"m4a", "mp3", "opus", "ogg", "oga", "wav", "aac", "flac", "weba"}


class ResolveError(Exception):
    """A message that is safe and useful to show the person who pasted the link."""


class _Logger:
    def debug(self, msg):  # yt-dlp routes info through debug too
        pass

    def warning(self, msg):
        log.debug("yt-dlp: %s", msg)

    def error(self, msg):
        log.debug("yt-dlp: %s", msg)


def assert_public(url: str) -> None:
    """Refuse anything that points inside the network. yt-dlp's generic extractor
    will fetch whatever it is given, so this is the SSRF gate at the app layer;
    the NetworkPolicy is the one that actually holds."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ResolveError("that doesn't look like a link")
    try:
        infos = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror:
        raise ResolveError("that host doesn't resolve") from None
    for _fam, _t, _p, _c, sa in infos:
        ip = ipaddress.ip_address(sa[0])
        if not ip.is_global:
            raise ResolveError("that address isn't reachable from here")


def ydl_opts() -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": True,
        "playlist_items": f"1:{MAX_PICKER}",
        "socket_timeout": 15,
        "retries": 2,
        "extractor_retries": 1,
        "geo_bypass": True,
        "logger": _Logger(),
        "cachedir": os.environ.get("YTDLP_CACHE", "/tmp/yt-dlp-cache"),
        # The extra formats are useless for streaming and slow the listing down.
        "extractor_args": {"youtube": {"skip": ["translated_subs"]}},
    }
    if os.environ.get("YTDLP_REMOTE_EJS", "1") == "1":
        # Let yt-dlp fetch its newest YouTube challenge-solver scripts (checksum
        # verified) so a stale image keeps working between rebuilds.
        opts["remote_components"] = ["ejs:github"]
    cookies = os.environ.get("COOKIES_FILE")
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    proxy = os.environ.get("YTDLP_PROXY")
    if proxy:
        opts["proxy"] = proxy
    return opts


# ---------------------------------------------------------------- formats ---

VIDEO_FAMILY = (("avc", "h264"), ("h264", "h264"), ("vp9", "vp9"), ("vp09", "vp9"),
                ("av01", "av1"), ("av1", "av1"), ("hev", "h265"), ("hvc", "h265"), ("h265", "h265"))
AUDIO_FAMILY = (("mp4a", "aac"), ("aac", "aac"), ("opus", "opus"), ("mp3", "mp3"),
                ("vorbis", "vorbis"), ("flac", "flac"), ("ec-3", "eac3"), ("ac-3", "ac3"))


def _family(codec: str | None, table) -> str | None:
    if not codec or codec == "none":
        return None
    c = codec.lower()
    for prefix, fam in table:
        if c.startswith(prefix):
            return fam
    return "other"


@dataclass
class Fmt:
    id: str
    url: str
    ext: str
    protocol: str
    kind: str  # video | audio | progressive
    vcodec: str | None
    acodec: str | None
    height: int
    width: int
    fps: float
    tbr: float
    abr: float
    size: int
    hdr: bool
    lang_pref: int
    lang: str | None
    headers: dict[str, str] = field(default_factory=dict)
    chunk: int = 0  # read in ranged pieces this big; the site throttles a plain GET

    @property
    def direct(self) -> bool:
        """A plain file over HTTP, which can be handed through untouched."""
        return self.protocol in ("http", "https")


def formats(info: dict) -> list[Fmt]:
    out: list[Fmt] = []
    for f in info.get("formats") or [info]:
        url = f.get("url")
        if not url:
            continue
        proto = (f.get("protocol") or urlparse(url).scheme or "").lower()
        ext = (f.get("ext") or "").lower()
        note = (f.get("format_note") or "").lower()
        if proto.startswith("http_dash") or proto == "mhtml" or ext in ("mhtml", "json", "sb") or "storyboard" in note:
            continue
        if proto.startswith("m3u8"):
            proto = "m3u8"
        elif proto not in ("http", "https"):
            continue  # rtmp, mms, f4m, dash manifests: ffmpeg could try, yt-dlp knows better; skip
        vc, ac = f.get("vcodec"), f.get("acodec")
        vfam, afam = _family(vc, VIDEO_FAMILY), _family(ac, AUDIO_FAMILY)
        # yt-dlp says "none" when it knows a stream is absent and None when it
        # doesn't know; for the unknowns, guess from the extension.
        has_v = vc != "none" if vc is not None else ext not in AUDIO_EXTS
        has_a = ac != "none" if ac is not None else True
        if not has_v and not has_a:
            continue
        kind = "progressive" if has_v and has_a else ("video" if has_v else "audio")
        if has_v and vfam is None:
            vfam = "other"
        if has_a and afam is None:
            afam = "other"
        headers = {k: v for k, v in (f.get("http_headers") or {}).items()
                   if k.lower() not in ("accept-encoding", "host", "content-length")}
        out.append(Fmt(
            id=str(f.get("format_id") or ""), url=url, ext=ext or ("m4a" if kind == "audio" else "mp4"),
            protocol=proto, kind=kind, vcodec=vfam if has_v else None, acodec=afam if has_a else None,
            height=int(f.get("height") or 0), width=int(f.get("width") or 0), fps=float(f.get("fps") or 0),
            tbr=float(f.get("tbr") or f.get("vbr") or 0), abr=float(f.get("abr") or (f.get("tbr") if kind == "audio" else 0) or 0),
            size=int(f.get("filesize") or f.get("filesize_approx") or 0),
            hdr=(f.get("dynamic_range") or "SDR").upper() != "SDR",
            lang_pref=int(f.get("language_preference") if f.get("language_preference") is not None else 0),
            lang=f.get("language"), headers=headers,
            chunk=int((f.get("downloader_options") or {}).get("http_chunk_size") or 0),
        ))
    return out


# ------------------------------------------------------------------ media ---

def describe(info: dict) -> dict:
    """What the page shows for one piece of media."""
    fs = formats(info)
    heights = sorted({f.height for f in fs if f.kind != "audio" and f.height}, reverse=True)
    vcodecs = sorted({f.vcodec for f in fs if f.vcodec and f.vcodec != "other"})
    thumb = info.get("thumbnail")
    if not thumb and info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url")
    return {
        "id": info.get("id"),
        "title": info.get("title") or info.get("id") or "untitled",
        "uploader": info.get("uploader") or info.get("channel") or info.get("creator"),
        "duration": info.get("duration"),
        "thumbnail": thumb,
        "extractor": (info.get("extractor_key") or info.get("extractor") or "").lower(),
        "url": info.get("webpage_url") or info.get("original_url"),
        "heights": heights,
        "codecs": vcodecs,
        "has_video": any(f.kind != "audio" for f in fs),
        "has_audio": any(f.kind != "video" for f in fs),
        "live": bool(info.get("is_live")),
    }


# ------------------------------------------------------------------ cache ---

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def cache_key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:20]


def _put(key: str, info: dict) -> None:
    with _lock:
        now = time.time()
        for k in [k for k, (t, _) in _cache.items() if now - t > CACHE_TTL]:
            del _cache[k]
        while len(_cache) >= CACHE_MAX:
            del _cache[next(iter(_cache))]
        _cache[key] = (now, info)


def cached(key: str) -> dict | None:
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] <= CACHE_TTL:
            return hit[1]
    return None


def _extract(url: str) -> dict:
    with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            raise ResolveError(_friendly(str(e))) from None
    if not info:
        raise ResolveError("nothing found at that link")
    return ydl.sanitize_info(info)


def _friendly(msg: str) -> str:
    m = msg.lower()
    if "unsupported url" in m:
        return "that site isn't supported, or the link isn't a video page"
    if "private video" in m or "login required" in m or "sign in" in m:
        return "that video needs a login to see"
    if "not available" in m or "unavailable" in m or "removed" in m or "404" in m:
        return "that video isn't available"
    if "age" in m and "restrict" in m:
        return "that video is age restricted"
    if "live" in m:
        return "live streams can't be downloaded"
    if "geo" in m or "not available in your country" in m:
        return "that video is blocked in the cluster's country"
    if "rate" in m and "limit" in m or "429" in m:
        return "the site is rate limiting the cluster right now, try again in a minute"
    if "timed out" in m or "timeout" in m:
        return "the site took too long to answer"
    # yt-dlp prefixes "ERROR: [extractor] id: message"; keep the message, first sentence
    import re
    tail = re.sub(r"^(ERROR:\s*)?(\[[^\]]+\]\s*)?([\w-]+:\s*)?", "", msg.strip()).split("\n")[0]
    tail = re.split(r"(?<=[a-z])\.\s", tail)[0]
    return (tail[:160] or "couldn't read that link").rstrip(".")


async def resolve(url: str) -> tuple[str, dict]:
    """Return (cache key, info) for a link, extracting if needed."""
    assert_public(url)
    key = cache_key(url)
    info = cached(key)
    if info is None:
        t0 = time.time()
        try:
            info = await asyncio.wait_for(asyncio.to_thread(_extract, url), EXTRACT_TIMEOUT)
        except asyncio.TimeoutError:
            raise ResolveError("the site took too long to answer") from None
        log.info("resolve %s %.1fs %s", (info.get("extractor_key") or "?").lower(), time.time() - t0, info.get("_type", "video"))
        _put(key, info)
    return key, info


def entries(info: dict) -> list[dict]:
    """The single item, or the items of a small collection (a carousel, a thread)."""
    if info.get("_type") == "playlist":
        return [e for e in (info.get("entries") or []) if e and (e.get("formats") or e.get("url"))][:MAX_PICKER]
    return [info]


def pick(info: dict, item: int | None) -> dict:
    es = entries(info)
    if not es:
        raise ResolveError("nothing downloadable at that link")
    if len(es) == 1 and item in (None, 0):
        return es[0]
    if item is None or not 0 <= item < len(es):
        raise ResolveError("pick which one you want")
    return es[item]


def extractor_names() -> list[str]:
    names = set()
    for ie in yt_dlp.list_extractors():
        if not ie.working() or ie.IE_NAME == "generic":
            continue
        names.add(ie.IE_NAME.split(":")[0])
    return sorted(names, key=str.lower)
