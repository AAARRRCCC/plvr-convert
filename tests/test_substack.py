"""Substack notes on the embed host: a note read into a post, a user's feed
read into a timeline, and the links that reach them. Nothing here reaches
substack.com: its API is swapped for fakes.

Run: PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests
"""
from __future__ import annotations

import asyncio
import re
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app import card, embed, profile, shard, shot, substack
from app import resolve as rs
from test_embed_scale import BROWSER, DISCORDBOT, TELEGRAM

HOST = "embed.example"
UPLOAD = "https://substack-post-media.s3.amazonaws.com/public/images/a_5712x4284.heic"


@pytest.fixture(autouse=True)
def reset(monkeypatch, tmp_path):
    shard.configure(1, "embed-0")
    monkeypatch.setattr(embed, "DIR", tmp_path)
    monkeypatch.setattr(embed, "GIF_DIR", tmp_path / "gif")
    (tmp_path / "gif").mkdir()
    rs._cache.clear()


def client() -> TestClient:
    return TestClient(embed.app, base_url=f"https://{HOST}")


def text(*paragraphs: list[dict]) -> dict:
    return {"type": "doc", "content": [{"type": "paragraph", "content": p} for p in paragraphs]}


def raw(i: int, handle: str = "writer", user_id: int = 1, **kw) -> dict:
    """A note as the reader API sends it: read on its own it names its author
    on itself."""
    return {"id": i, "handle": handle, "name": handle.title(), "photo_url": None, "user_id": user_id,
            "date": "2026-10-21T16:04:45.635Z", "body": f"note {i}", "body_json": text([{"type": "text", "text": f"note {i}"}]),
            "reaction_count": 5, "restacks": 2, "children_count": 3, "ancestor_path": "", "attachments": [], **kw}


# ------------------------------------------------------------ normalizing ---

def test_a_note_reads_as_a_post():
    quoted = dict(raw(7, handle="other"), user={"handle": "other", "name": "Other", "photo_url": None})
    for k in ("handle", "name"):
        quoted.pop(k)
    r = raw(9, attachments=[
        {"type": "image", "imageUrl": UPLOAD, "imageWidth": 4284, "imageHeight": 5712},
        {"type": "video", "media_upload_id": "up", "mediaUpload": {"state": "transcoded", "width": 1920, "height": 1080, "duration": 12.5}},
        {"type": "comment", "comment": quoted},
        {"type": "post", "post": {"title": "An article", "subtitle": "about things", "canonical_url": "https://w.substack.com/p/a",
                                  "cover_image": None, "audience": "only_paid"}, "publication": {"name": "The Pub"}},
        {"type": "missing", "reason": "unavailable"},
    ])
    p = substack._normalize(r)
    assert p["site"] == "substack" and p["url"] == "https://substack.com/@writer/note/c-9"
    assert (p["likes"], p["reposts"], p["replies"], p["views"]) == (5, 2, 3, None)
    photo, video = p["media"]
    # an upload goes through Substack's CDN as a jpeg: the original is often HEIC
    assert photo["url"].startswith("https://substackcdn.com/image/fetch/w_1200,") and "f_jpg" in photo["url"]
    assert (photo["w"], photo["h"]) == (4284, 5712)
    assert video["url"] == substack.VIDEO.format(id="up") and video["duration"] == 12.5
    assert p["card"] == {"url": "https://w.substack.com/p/a", "site": "The Pub", "title": "An article", "text": "about things",
                         "image": None, "paid": True}
    assert substack._normalize(substack._quoted(r))["handle"] == "other"


def test_note_text_keeps_links_mentions_and_paragraphs():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "marks": [{"type": "bold"}], "text": "Hi "},
                                          {"type": "substack_mention", "attrs": {"label": "Someone"}},
                                          {"type": "text", "text": ", see "},
                                          {"type": "text", "marks": [{"type": "link", "attrs": {"href": "https://a.b"}}], "text": "a.b"}]},
        {"type": "blockquote", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "quoted"}]}]},
        {"type": "paragraph"},
    ]}
    assert substack._spans(doc, "") == [{"text": "Hi ", "link": False}, {"text": "Someone", "link": True},
                                        {"text": ", see ", "link": False}, {"text": "a.b", "link": True},
                                        {"text": "\n\nquoted", "link": False}]
    assert substack._spans(None, " plain ") == [{"text": "plain", "link": False}]
    assert substack._spans(None, "") == []


def test_a_reply_names_who_it_answers():
    r = raw(3, ancestor_path="1.2")
    parents = [{"id": 1, "handle": "first"}, {"id": 2, "handle": "second"}]
    assert substack._parent(r, parents) == "second"
    assert substack._parent(raw(3), parents) is None


def test_fetch_reads_deeper_quotes_whole(monkeypatch):
    # attached, a quoted note names its author under "user"; the note it
    # quotes in turn isn't filled in, so it is read whole
    inner = raw(1, handle="inner")
    middle = raw(2, handle="middle", attachments=[{"type": "comment", "comment": dict(inner, user={"handle": "inner"})}])
    top = raw(3, attachments=[{"type": "comment", "comment": dict(middle, attachments=[])}])
    notes = {"1": inner, "2": middle, "3": top}
    asked = []

    async def api(url, what):
        cid = url.rsplit("/", 1)[1]
        asked.append(cid)
        return {"item": {"comment": notes[cid], "parentComments": []}}
    monkeypatch.setattr(substack, "_api", api)
    p = asyncio.run(substack.fetch("3", 2))
    assert p["quote"]["handle"] == "middle" and p["quote"]["quote"]["handle"] == "inner"
    assert asked == ["3", "2"]
    assert asyncio.run(substack.fetch("3", 2)) is p and asked == ["3", "2"]   # cached


def test_timeline_keeps_own_top_level_notes(monkeypatch):
    def item(note, kind="comment"):
        return {"type": kind, "comment": note}
    pages = {None: ([item(raw(5)), item(raw(4, user_id=2)), item(raw(3, ancestor_path="1")), item(None, "post")], "c2"),
             "c2": ([item(raw(2)), item(raw(1))], None)}
    asked = []

    async def api(url, what):
        if "public_profile" in url:
            return {"id": 1, "handle": "writer", "name": "Writer", "photo_url": UPLOAD, "bio": "hello",
                    "followerCount": 1234, "subscriberCount": "5K+"}
        cursor = url.split("cursor=")[1] if "cursor=" in url else None
        asked.append(cursor)
        items, nxt = pages[cursor]
        return {"items": items, "nextCursor": nxt}
    monkeypatch.setattr(substack, "_api", api)
    got = asyncio.run(substack.timeline("writer", 100, want=2))
    assert [p["id"] for p in got["posts"]] == ["5", "2", "1"] and asked == [None, "c2"]
    u = got["user"]
    assert u["counts"] == [(1234, "followers"), ("5K+", "subscribers")] and u["tab"] == "Notes"
    assert u["avatar"].startswith("https://substackcdn.com/image/fetch/w_400,")


def test_a_note_draws_in_substacks_colours():
    p = substack._normalize(raw(1, attachments=[{"type": "link", "linkMetadata": {"url": "https://a.b", "host": "a.b", "title": "A",
                                                                                   "description": "B", "image": None}}]))
    L = card.layout(p)
    icons = [op[1] for op in L.ops if op[0] == "icon"]
    assert icons == ["s_like", "s_reply", "s_restack", "s_share"]
    assert L.theme["blue"] == card.ACCENTS["substack"]
    card.render(L)


# ------------------------------------------------------------------ links ---

@pytest.fixture
def notes(monkeypatch):
    calls = []

    async def fetch(cid, depth):
        calls.append((cid, depth))
        if cid == "404":
            raise substack.Missing("that note doesn't exist, or was deleted")
        return substack._normalize(raw(int(cid)))

    async def timeline(handle, scan, want=None):
        calls.append((handle, scan, want))
        return {"user": substack._user({"id": 1, "handle": handle, "name": "Writer"}), "posts": [substack._normalize(raw(i)) for i in range(3)]}
    monkeypatch.setattr(substack, "fetch", fetch)
    monkeypatch.setattr(substack, "timeline", timeline)
    return calls


def test_a_browser_goes_on_to_substack():
    c = client()
    for path in ("/@writer/note/c-123?utm_source=x", "/note/c-123", "/@writer"):
        r = c.get(path, headers={"User-Agent": BROWSER}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == f"https://substack.com{path}", path
    r = c.get("/writer/status/123", headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert r.headers["location"] == "https://x.com/writer/status/123"


def test_a_note_for_discord_is_its_image(notes):
    c = client()
    r = c.get("/@writer/note/c-123?q=2", headers={"User-Agent": DISCORDBOT})
    assert f'og:image" content="https://{HOST}/m/q/q=2/ss_123.png"' in r.text
    assert notes[0] == ("123", 2)
    r = c.get("/m/q/q=2/ss_123.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_a_note_for_other_fetchers_names_substack(notes):
    r = client().get("/note/c-123", headers={"User-Agent": TELEGRAM})
    assert 'og:site_name" content="substack.com"' in r.text
    assert 'og:url" content="https://substack.com/@writer/note/c-123"' in r.text


def test_a_missing_note_goes_to_substack(notes):
    r = client().get("/@writer/note/c-404", headers={"User-Agent": DISCORDBOT}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://substack.com/@writer/note/c-404"


def test_a_substack_profile_for_discord(notes):
    c = client()
    r = c.get("/@Writer?top", headers={"User-Agent": DISCORDBOT})
    img = re.search(r'og:image" content="([^"]+)"', r.text).group(1)
    assert img == f"https://{HOST}/m/q/top=1/su_writer.png"
    assert ("Writer", profile.TOP_FROM, None) in notes
    r = c.get("/m/q/top=1/su_writer.png")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_note_and_tweet_renders_never_share_a_name():
    f = embed.flags.parse({})
    assert embed._name("ss_123", f) != embed._name("123", f)
    assert embed._owner_key("ss_123").isdigit() and embed._site("ss_123") == "substack" and embed._site("123") == "x"
    assert embed._pname("su_Writer", f) == "su_writer" and embed._pname("u_Writer", f) == "u_writer"


# ----------------------------------------------------------------- videos ---

LONG = {"type": "video", "media_upload_id": "up", "mediaUpload": {"state": "transcoded", "width": 1080, "height": 1920, "duration": 77.2}}


@pytest.fixture
def long_note(monkeypatch):
    async def fetch(cid, depth):
        return substack._normalize(raw(int(cid), attachments=[dict(LONG)]))
    monkeypatch.setattr(substack, "fetch", fetch)
    started = []
    monkeypatch.setattr(embed, "render", lambda tid, f: started.append(tid))
    return started


def test_a_long_note_video_is_rendered_by_default(long_note):
    r = client().get("/@writer/note/c-5", headers={"User-Agent": DISCORDBOT}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"https://{HOST}/m/ss_5.mp4"
    assert long_note == ["ss_5"]


def test_plain_or_past_max_render_is_substacks_own_file(long_note, monkeypatch):
    r = client().get("/@writer/note/c-5?plain", headers={"User-Agent": TELEGRAM})
    assert f'og:video" content="{substack.VIDEO.format(id="up")}"' in r.text
    monkeypatch.setattr(embed, "MAX_RENDER", 60)
    r = client().get("/@writer/note/c-5", headers={"User-Agent": TELEGRAM})
    assert 'og:video" content="https://substack.com/api/v1/video' in r.text and long_note == []


class Reply:
    def __init__(self, status: int, location: str = ""):
        self.status_code, self.headers = status, {"location": location} if location else {}
        self.is_redirect = status in (301, 302, 307, 308)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


HIGH = "https://stream.mux.com/Ab12/high.mp4?token=t.k.n"


def video_item() -> dict:
    return substack._normalize(raw(1, attachments=[dict(LONG)]))["media"][0]


def test_files_behind_a_note_video(monkeypatch):
    asked = []

    async def get(url, follow_redirects=True):
        asked.append(url)
        return Reply(302, HIGH)
    monkeypatch.setattr(substack.client(), "get", get)
    monkeypatch.setattr(substack.tunnel.client(), "stream", lambda m, url, headers: (asked.append(url), Reply(206))[1])
    monkeypatch.setattr(rs, "assert_public", lambda url: None)
    got = asyncio.run(substack.files(video_item()))
    assert got == [{"url": "https://stream.mux.com/Ab12/medium.mp4?token=t.k.n", "w": 480, "h": 854},
                   {"url": HIGH, "w": 1080, "h": 1920}]
    assert asked == [substack.VIDEO.format(id="up"), got[0]["url"]]
    # no medium.mp4 for this asset: high alone
    monkeypatch.setattr(substack.tunnel.client(), "stream", lambda m, url, headers: Reply(404))
    assert asyncio.run(substack.files(video_item())) == [{"url": HIGH, "w": 1080, "h": 1920}]


def test_files_when_the_src_is_not_mux(monkeypatch):
    async def get(url, follow_redirects=True):
        return Reply(302, "https://elsewhere.example/v.mp4")
    monkeypatch.setattr(substack.client(), "get", get)
    assert asyncio.run(substack.files(video_item())) == []


def test_the_smallest_file_that_fills_the_cell_is_read():
    item = dict(video_item(), variants=[{"url": "m", "w": 480, "h": 854}, {"url": "h", "w": 1080, "h": 1920}])
    assert shot.source(item, 466, 830, "contain") == "m"      # a 1x cell
    assert shot.source(item, 1036, 1658, "contain") == "h"    # a 2x cell
    assert shot.source(item, 2000, 4000, "contain") == "h"    # bigger than any: the largest
    assert shot.source(video_item(), 466, 830, "contain") == substack.VIDEO.format(id="up")


def test_a_long_video_is_drawn_smaller():
    L = card.layout(substack._normalize(raw(1, attachments=[dict(LONG)])))
    assert embed._scale(L, 10) == 1.0 and embed._scale(L, 0) == 1.0
    assert embed._scale(L, 77) == embed.MIN_SCALE
    k = embed._scale(L, 30)
    assert embed.MIN_SCALE < k < 1
    small = shot.scaled(L, k)
    assert small.width % 2 == small.height % 2 == 0
    c, s = L.cells[0], small.cells[0]
    # the cell still covers the hole drawn at this size
    assert s.x <= c.x * k and s.y <= c.y * k and s.x + s.w >= (c.x + c.w) * k and s.y + s.h >= (c.y + c.h) * k
    assert s.w % 2 == s.h % 2 == 0 and s.item is c.item
    png = card.render(L, True, (small.width, small.height))
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(png)).size == (small.width, small.height)


def test_a_note_video_has_its_still_for_a_poster():
    up = dict(LONG["mediaUpload"], user_id=11, thumbnail_id=3)
    item = substack._normalize(raw(1, attachments=[dict(LONG, mediaUpload=up)]))["media"][0]
    still = "https://substack-video.s3.amazonaws.com/video_upload/user/11/up/transcoded-00003.png"
    assert item["poster"].startswith("https://substackcdn.com/image/fetch/w_1200,") and item["poster"].endswith(quote(still, safe=""))
    # no thumbnail named: no poster, drawn black as before
    assert video_item()["poster"] is None
