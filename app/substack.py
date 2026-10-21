"""Read one Substack note into the same plain dict tweet.py makes of a post,
so card.py draws it and the embed host renders it as it does an x.com post;
or a user and their latest notes. The source is substack.com's own reader
API, which needs no login. Nothing is written anywhere; results sit in the
resolve cache.

A note maps onto a post closely: its likes, restacks and replies are the
counts, a note it quotes is the quote, and an article or link it carries is
a "card" drawn under the text.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from . import resolve as rs
from .tweet import UA

log = logging.getLogger("convert.substack")

SITE = "substack"
NOTE = "https://substack.com/api/v1/reader/comment/{id}"
PROFILE = "https://substack.com/api/v1/user/{handle}/public_profile"
FEED = "https://substack.com/api/v1/reader/feed/profile/{uid}"
VIDEO = "https://substack.com/api/v1/video/upload/{id}/src"   # answers with a redirect to the signed mp4
UPLOADS = "https://substack-post-media.s3.amazonaws.com/"
CDN = "https://substackcdn.com/image/fetch/w_{w},c_limit,f_jpg,q_auto:good/{url}"
MAX_DEPTH = 3
_BLOCKS = {"paragraph", "heading", "blockquote", "bullet_list", "ordered_list", "list_item", "code_block", "horizontal_rule"}


def note_url(handle: str, cid: str | int) -> str:
    return f"https://substack.com/@{handle}/note/c-{cid}"


def _img(url: str | None, width: int) -> str | None:
    """An uploaded image through Substack's own CDN, as a jpeg no wider than
    `width`: the original is often an iPhone's HEIC, which can't be drawn,
    and several thousand pixels across."""
    if url and url.startswith(UPLOADS):
        return CDN.format(w=width, url=quote(url, safe=""))
    return url


# --------------------------------------------------------------- fetching ---

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True, timeout=httpx.Timeout(15, read=20))
    return _client


class Missing(rs.ResolveError):
    pass


async def _api(url: str, what: str) -> dict:
    """Substack's answer, its failures told about `what` was asked for."""
    try:
        r = await client().get(url)
    except httpx.HTTPError as e:
        raise rs.ResolveError("substack couldn't be reached right now") from e
    if r.status_code == 404:
        raise Missing(f"that {what} doesn't exist, or was deleted")
    if r.status_code in (401, 403):
        raise rs.ResolveError(f"that {what} is private")
    if r.status_code == 429:
        raise rs.ResolveError("substack is rate limiting the cluster right now, try again in a minute")
    if r.status_code >= 400:
        raise rs.ResolveError(f"substack refused ({r.status_code})")
    try:
        return r.json()
    except ValueError:
        raise rs.ResolveError("substack sent something unreadable") from None


async def _note_raw(cid: str) -> dict:
    """The reader's item for a note: the note under "comment", the notes it
    answers under "parentComments"."""
    item = (await _api(NOTE.format(id=cid), "note")).get("item") or {}
    if not item.get("comment"):
        raise Missing("that note doesn't exist, or was deleted")
    return item


async def fetch(cid: str, depth: int) -> dict:
    """The note and, nested under "quote", up to `depth` notes it quotes."""
    depth = max(0, min(MAX_DEPTH, int(depth)))
    key = f"substack:{cid}:{depth}"
    hit = rs.cached(key)
    if hit is not None:
        return hit
    t0 = time.time()
    item = await _note_raw(cid)
    raw = item["comment"]
    post = _normalize(raw, _parent(raw, item.get("parentComments") or []))
    cur, d = post, depth
    while d > 0 and (q := _quoted(raw)):
        # the quoted note comes with its own attachments, but a note it
        # quotes in turn may not be filled in: the deeper ones are read whole
        if d > 1:
            try:
                q = (await _note_raw(str(q["id"])))["comment"]
            except rs.ResolveError:
                pass
        cur["quote"] = _normalize(q)
        cur, raw, d = cur["quote"], q, d - 1
    log.info("note %s depth %d %.1fs", cid, depth, time.time() - t0)
    rs._put(key, post)
    return post


_reading: dict[str, asyncio.Task] = {}   # timelines being read, so a page and its render share one read


async def timeline(handle: str, scan: int, want: int | None = None) -> dict:
    """{"user": the account, "posts": their own notes, newest first, each with
    the note it quotes}, read from their last `scan` feed entries, or until
    `want` notes are found. Restacks, replies and articles are left out."""
    key = f"substack-timeline:{handle.lower()}:{scan}:{want}"
    hit = rs.cached(key)
    if hit is not None:
        return hit
    t = _reading.get(key)
    if t is None:
        t = _reading[key] = asyncio.ensure_future(_timeline(handle, scan, want, key))
        t.add_done_callback(lambda _: _reading.pop(key, None))
    return await asyncio.shield(t)


async def _timeline(handle: str, scan: int, want: int | None, key: str) -> dict:
    t0 = time.time()
    u = await _api(PROFILE.format(handle=handle), "account")
    if not u.get("id") or not u.get("handle"):
        raise rs.ResolveError("that account doesn't exist")
    posts, seen, cursor = [], 0, None
    while True:
        url = FEED.format(uid=u["id"]) + (f"?cursor={cursor}" if cursor else "")
        try:
            page = await _api(url, "account")
        except rs.ResolveError:
            if not posts:
                raise
            break
        items = page.get("items") or []
        seen += len(items)
        for it in items:
            c = it.get("comment")
            # a restack of someone else's note is in the feed as that note
            if it.get("type") != "comment" or not c or c.get("user_id") != u["id"] or c.get("ancestor_path"):
                continue
            post = _normalize(c)
            if (q := _quoted(c)):
                post["quote"] = _normalize(q)
            posts.append(post)
        cursor = page.get("nextCursor")
        if not items or not cursor or seen >= scan or want and len(posts) >= want:
            break
    posts.sort(key=lambda p: p["created"], reverse=True)
    out = {"user": _user(u), "posts": posts}
    log.info("substack timeline @%s %d notes in %d entries %.1fs", handle, len(posts), seen, time.time() - t0)
    rs._put(key, out)
    return out


# ------------------------------------------------------------ normalizing ---

def _parent(raw: dict, parents: list[dict]) -> str | None:
    """The handle of the note this one answers: the last in its ancestor path."""
    path = str(raw.get("ancestor_path") or "")
    if not path:
        return None
    pid = path.split(".")[-1]
    p = next((p for p in parents if str(p.get("id")) == pid), parents[-1] if parents else None)
    return (p or {}).get("handle") or None


def _quoted(raw: dict) -> dict | None:
    """The note this one quotes, as it comes attached."""
    for a in raw.get("attachments") or []:
        if a.get("type") == "comment" and a.get("comment"):
            return a["comment"]
    return None


def _spans(doc: dict | None, fallback: str) -> list[dict]:
    """The text as shown: [{text, link}], links and mentions marked, a blank
    line between paragraphs. Bold and italic are drawn plain."""
    if not doc:
        return [{"text": fallback.strip(), "link": False}] if fallback.strip() else []
    spans: list[dict] = []

    def add(text: str, link: bool) -> None:
        if not text:
            return
        if spans and not link and not spans[-1]["link"]:
            spans[-1]["text"] += text
        else:
            spans.append({"text": text, "link": link})

    def walk(n: dict) -> None:
        t = n.get("type")
        if t in _BLOCKS and spans and not spans[-1]["text"].endswith("\n"):
            add("\n\n", False)
        if t == "text":
            add(n.get("text") or "", any(m.get("type") == "link" for m in n.get("marks") or []))
        elif t == "substack_mention":
            add((n.get("attrs") or {}).get("label") or "", True)
        elif t == "hard_break":
            add("\n", False)
        else:
            if t == "list_item":
                add("• ", False)
            for ch in n.get("content") or []:
                walk(ch)

    walk(doc)
    while spans and not spans[-1]["link"] and not spans[-1]["text"].strip():
        spans.pop()
    if spans:
        spans[-1]["text"] = spans[-1]["text"].rstrip()
        spans[0]["text"] = spans[0]["text"].lstrip()
    return spans or _spans(None, fallback)


def _media(raw: dict) -> list[dict]:
    out = []
    for a in raw.get("attachments") or []:
        if a.get("type") == "image" and a.get("imageUrl"):
            out.append({"kind": "photo", "url": _img(a["imageUrl"], 1200), "w": int(a.get("imageWidth") or 0), "h": int(a.get("imageHeight") or 0)})
        elif a.get("type") == "video" and a.get("media_upload_id"):
            up = a.get("mediaUpload") or {}
            if up.get("state") not in (None, "transcoded"):
                continue
            out.append({"kind": "video", "url": VIDEO.format(id=a["media_upload_id"]), "poster": None,
                        "w": int(up.get("width") or 0), "h": int(up.get("height") or 0), "duration": float(up.get("duration") or 0)})
    return out[:4]


def _card(raw: dict) -> dict | None:
    """The article or link the note carries, as a preview card."""
    for a in raw.get("attachments") or []:
        if a.get("type") == "post" and a.get("post"):
            p, pub = a["post"], a.get("publication") or {}
            return {"url": p.get("canonical_url"), "site": pub.get("name") or "", "title": p.get("title") or "",
                    "text": p.get("subtitle") or p.get("description") or "", "image": _img(p.get("cover_image"), 1200),
                    "paid": p.get("audience") not in (None, "everyone")}
        if a.get("type") == "link" and a.get("linkMetadata"):
            m = a["linkMetadata"]
            return {"url": m.get("url"), "site": m.get("host") or "", "title": m.get("title") or "",
                    "text": m.get("description") or "", "image": _img(m.get("image"), 1200), "paid": False}
    return None


def _when(s: str | None) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def _normalize(raw: dict, replying_to: str | None = None) -> dict:
    # a note read on its own names its author on itself, an attached one under "user"
    u = raw.get("user") or raw
    handle = u.get("handle") or ""
    return {
        "id": str(raw.get("id") or ""),
        "site": SITE,
        "url": note_url(handle, raw.get("id")),
        "name": u.get("name") or handle,
        "handle": handle,
        "avatar": _img(u.get("photo_url"), 200),
        "verified": False,
        "verified_type": None,
        "spans": _spans(raw.get("body_json"), raw.get("body") or ""),
        "translated": None,
        "created": _when(raw.get("date")),
        "replies": int(raw.get("children_count") or 0),
        "reposts": int(raw.get("restacks") or 0),
        "likes": int(raw.get("reaction_count") or 0),
        "views": None,
        "bookmarks": 0,
        "replying_to": replying_to,
        "media": _media(raw),
        "card": _card(raw),
        "quote": None,
    }


def _user(u: dict) -> dict:
    counts: list[tuple[Any, str]] = [(int(u.get("followerCount") or 0), "followers")]
    if u.get("subscriberCount"):
        # a rough figure already, like "460K+"
        counts.append((str(u["subscriberCount"]), "subscribers"))
    return {
        "site": SITE,
        "name": u.get("name") or u.get("handle") or "",
        "handle": u.get("handle") or "",
        "url": f"https://substack.com/@{u.get('handle')}",
        "avatar": _img(u.get("photo_url"), 400),
        "banner": None,
        "bio": [{"text": u["bio"].strip(), "link": False}] if (u.get("bio") or "").strip() else [],
        "verified": False,
        "counts": counts,
        "tab": "Notes",
    }
