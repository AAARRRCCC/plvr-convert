"""Read one post from x.com into a plain dict: author, text with its links,
media, counts, and the post it quotes (as deep as asked). The source is the
fxtwitter API, which needs no login and answers for text-only posts, which
yt-dlp does not. Nothing is written anywhere; results sit in the resolve cache.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from . import resolve as rs

log = logging.getLogger("convert.tweet")

UA = "OpenAI File Downloader, XaiImageApiFetch/1.0"
API = "https://api.fxtwitter.com/status/{id}"
MAX_DEPTH = 3
_STATUS = re.compile(r"^https?://(?:www\.|mobile\.)?(?:x|twitter|vxtwitter|fxtwitter|fixupx|fixvx|twittpr)\.com/(?:[A-Za-z0-9_]+|i/web)/status(?:es)?/(\d+)", re.I)


def status_id(url: str) -> str | None:
    """The numeric id in an x.com / twitter.com status link, or None."""
    m = _STATUS.match(url.strip())
    return m.group(1) if m else None


# --------------------------------------------------------------- fetching ---

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True, timeout=httpx.Timeout(15, read=20))
    return _client


async def _fetch_raw(tid: str) -> dict:
    try:
        r = await client().get(API.format(id=tid))
    except httpx.HTTPError as e:
        raise rs.ResolveError("x.com couldn't be reached right now") from e
    if r.status_code == 404:
        raise rs.ResolveError("that post doesn't exist, or was deleted")
    if r.status_code in (401, 403):
        raise rs.ResolveError("that post is private")
    if r.status_code == 429:
        raise rs.ResolveError("x.com is rate limiting the cluster right now, try again in a minute")
    if r.status_code >= 400:
        raise rs.ResolveError(f"x.com refused ({r.status_code})")
    try:
        j = r.json()
    except ValueError:
        raise rs.ResolveError("x.com sent something unreadable") from None
    if j.get("code") == 401:
        raise rs.ResolveError("that post is private")
    if j.get("code") == 404 or not j.get("tweet"):
        raise rs.ResolveError("that post doesn't exist, or was deleted")
    return j["tweet"]


async def fetch(tid: str, depth: int) -> dict:
    """The post and, nested under "quote", up to `depth` posts it quotes."""
    depth = max(0, min(MAX_DEPTH, int(depth)))
    key = f"tweet:{tid}:{depth}"
    hit = rs.cached(key)
    if hit is not None:
        return hit
    t0 = time.time()
    raw = await _fetch_raw(tid)
    post = _normalize(raw)
    cur, q, d = post, raw.get("quote"), depth
    while d > 0 and q:
        # fxtwitter nests one level; the deeper posts are read on their own
        if d > 1 and "quote" not in q:
            try:
                q = await _fetch_raw(str(q.get("id")))
            except rs.ResolveError:
                pass
        cur["quote"] = _normalize(q)
        cur, q, d = cur["quote"], q.get("quote"), d - 1
    log.info("tweet %s depth %d %.1fs", tid, depth, time.time() - t0)
    rs._put(key, post)
    return post


# ------------------------------------------------------------ normalizing ---

MAX_SIDE = 1280   # a screenshot's video cell is at most 1036px wide, so 720p is enough


def _pick_variant(m: dict) -> tuple[str | None, int]:
    """The highest-bitrate mp4 of a video no larger than MAX_SIDE on its long
    side (the smallest one if every variant is larger), and its bitrate.
    Decoding a 4K source is most of the cost of composing a screenshot."""
    best, br = None, -1
    small, small_side = None, None
    for v in m.get("variants") or []:
        if "mp4" not in str(v.get("content_type") or "") and not str(v.get("url", "")).split("?")[0].endswith(".mp4"):
            continue
        b = int(v.get("bitrate") or 0)
        dims = re.search(r"/(\d+)x(\d+)/", str(v.get("url", "")))
        side = max(int(dims.group(1)), int(dims.group(2))) if dims else 0
        if side > MAX_SIDE:
            if small_side is None or side < small_side:
                small, small_side = v.get("url"), side
            continue
        if b > br:
            best, br = v.get("url"), b
    if best is None and small is not None:
        best, br = small, 0
    if best is None and str(m.get("url", "")).split("?")[0].endswith(".mp4"):
        best = m["url"]
    return best, max(br, 0)


def _media(raw: dict) -> list[dict]:
    out = []
    for m in ((raw.get("media") or {}).get("all") or [])[:4]:
        kind = m.get("type")
        if kind not in ("photo", "video", "gif"):
            continue
        item: dict[str, Any] = {"kind": kind, "w": int(m.get("width") or 0), "h": int(m.get("height") or 0)}
        if kind == "photo":
            item["url"] = m.get("url")
        else:
            url, _ = _pick_variant(m)
            if not url:
                continue
            item["url"] = url
            item["poster"] = m.get("thumbnail_url")
            item["duration"] = float(m.get("duration") or 0)
        if not item["url"]:
            continue
        out.append(item)
    return out


def _spans(raw: dict) -> list[dict]:
    """The text as shown: [{text, link}] with t.co links replaced by their
    display form, mentions and hashtags marked, and trailing media links cut."""
    rt = raw.get("raw_text") or {}
    text = rt.get("text") if rt.get("text") is not None else (raw.get("text") or "")
    cps = list(text)
    rng = rt.get("display_text_range") or [0, len(cps)]
    start, end = max(0, int(rng[0])), min(len(cps), int(rng[1]))
    facets = sorted((f for f in (rt.get("facets") or []) if f.get("indices") and f.get("type") in ("url", "mention", "hashtag", "cashtag", "symbol")), key=lambda f: f["indices"][0])
    spans, pos = [], start
    for f in facets:
        a, b = int(f["indices"][0]), int(f["indices"][1])
        if a < pos or a >= end:
            continue
        if a > pos:
            spans.append({"text": "".join(cps[pos:a]), "link": False})
        shown = "".join(cps[a:min(b, end)])
        if f["type"] == "url" and f.get("display"):
            shown = str(f["display"])
        spans.append({"text": shown, "link": True})
        pos = min(b, end)
    if pos < end:
        spans.append({"text": "".join(cps[pos:end]), "link": False})
    if not facets and not spans:
        spans = [{"text": "".join(cps[start:end]), "link": False}]
    for s in spans:
        s["text"] = html.unescape(s["text"])
    # trailing whitespace that only led into a cut media link
    while spans and not spans[-1]["text"].strip() and not spans[-1]["link"]:
        spans.pop()
    if spans:
        spans[-1]["text"] = spans[-1]["text"].rstrip()
    return spans


def _normalize(raw: dict) -> dict:
    a = raw.get("author") or {}
    ver = a.get("verification") or {}
    created = raw.get("created_timestamp")
    if not created:
        try:
            created = datetime.strptime(raw.get("created_at", ""), "%a %b %d %H:%M:%S %z %Y").timestamp()
        except ValueError:
            created = time.time()
    return {
        "id": str(raw.get("id") or ""),
        "url": raw.get("url") or f"https://x.com/i/status/{raw.get('id')}",
        "name": a.get("name") or a.get("screen_name") or "",
        "handle": a.get("screen_name") or "",
        "avatar": a.get("avatar_url"),
        "verified": bool(ver.get("verified")),
        "verified_type": ver.get("type"),
        "spans": _spans(raw),
        "created": float(created),
        "replies": int(raw.get("replies") or 0),
        "reposts": int(raw.get("retweets") or 0),
        "likes": int(raw.get("likes") or 0),
        "views": int(raw.get("views") or 0) if raw.get("views") is not None else None,
        "bookmarks": int(raw.get("bookmarks") or 0),
        "replying_to": raw.get("replying_to"),
        "media": _media(raw),
        "quote": None,
    }


def plain_text(post: dict) -> str:
    return "".join(s["text"] for s in post["spans"])


def videos(post: dict) -> list[dict]:
    """Every playable item in the post and its quotes, the post's own first."""
    out = []
    p: dict | None = post
    while p:
        out += [m for m in p["media"] if m["kind"] in ("video", "gif")]
        p = p.get("quote")
    return out


def relative(created: float, now: float | None = None) -> str:
    """The short age x.com shows in a timeline: 3s, 15h, Sep 15, Sep 15, 2024."""
    now = now or time.time()
    d = max(0, int(now - created))
    if d < 60:
        return f"{d}s"
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    then, today = datetime.fromtimestamp(created, timezone.utc), datetime.fromtimestamp(now, timezone.utc)
    day = f"{then.strftime('%b')} {then.day}"
    return day if then.year == today.year else f"{day}, {then.year}"
