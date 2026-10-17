"""The embed app's links: the guide pages, profile links, a lone gif sent to
Discord, and the timeline read behind a profile. Nothing here reaches x.com:
the post and timeline readers are swapped for fakes.

Run: PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app import embed, flags, guide, profile, shard, tweet
from app import resolve as rs
from test_embed_scale import BROWSER, DISCORDBOT, TELEGRAM, Live

HOST = "embed.example"


@pytest.fixture(autouse=True)
def reset(monkeypatch, tmp_path):
    shard.configure(1, "embed-0")
    monkeypatch.setattr(embed, "DIR", tmp_path)


def client() -> TestClient:
    return TestClient(embed.app, base_url=f"https://{HOST}")


def post(i: int, likes: int = 0, media: list | None = None) -> dict:
    return {"id": str(i), "url": f"https://x.com/someone/status/{i}", "name": "Someone", "handle": "someone", "avatar": None,
            "verified": False, "verified_type": None, "spans": [{"text": f"post {i}", "link": False}], "translated": None,
            "created": 1_700_000_000.0 + i, "replies": 0, "reposts": 0, "likes": likes, "views": None, "bookmarks": 0,
            "replying_to": None, "media": media or [], "quote": None}


# ------------------------------------------------------------------ guide ---

def test_guide_for_a_chat_app():
    c = client()
    for path, page in (("/help", "help"), ("/someone/status/5?help", "help"), ("/workflows", "workflows")):
        r = c.get(path, headers={"User-Agent": DISCORDBOT})
        assert f'og:image" content="https://{HOST}/{page}.png?v=' in r.text, path
        assert f'theme-color" content="{guide.PINK}"' in r.text


def test_guide_for_a_browser_and_for_curl():
    c = client()
    r = c.get("/workflows", headers={"User-Agent": BROWSER, "Accept": "text/html,*/*"})
    assert r.headers["content-type"].startswith("text/html") and f'href="/help"' in r.text
    r = c.get("/help", headers={"User-Agent": "curl/8.0", "Accept": "*/*"})
    assert r.headers["content-type"].startswith("text/plain") and f"{HOST}/workflows" in r.text


def test_guide_images_and_font():
    c = client()
    for page in ("help", "workflows"):
        r = c.get(f"/{page}.png")
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert c.get("/guide/nunito.ttf").headers["content-type"] == "font/ttf"


def test_every_flag_is_in_the_help():
    words = {w for s in guide.HELP.sections for i in s.items for w in re.findall(r"[a-z]+\d*", i.chip)}
    assert {n for n in flags.NAMES if not re.fullmatch(r"v\d", n)} <= words


# ---------------------------------------------------------------- profile ---

USER = {"name": "Someone", "handle": "someone", "url": "https://x.com/someone", "avatar": None, "banner": None,
        "bio": [{"text": "hello", "link": False}], "verified": False, "followers": 5, "following": 2, "posts": 9}


@pytest.fixture
def timeline(monkeypatch):
    calls = []

    async def fake(handle, scan, want=None):
        calls.append((handle, scan, want))
        if handle.lower() == "nobody":
            raise rs.ResolveError("that account doesn't exist")
        return {"user": USER, "posts": [post(i, likes=i * 3 % 7) for i in range(9, 0, -1)]}
    monkeypatch.setattr(tweet, "timeline", fake)
    return calls


def test_profile_for_discord_is_its_image(timeline):
    with Live(embed.app) as live:
        r = httpx.get(f"{live.url}/Someone", headers={"User-Agent": DISCORDBOT, "Host": HOST})
        img = re.search(r'og:image" content="([^"]+)"', r.text).group(1)
        assert img == f"https://{HOST}/m/u_someone.png"
        assert "og:title" not in r.text          # Discord's page is the image alone
        png = httpx.get(live.url + urlsplit(img).path, timeout=30)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert timeline[0] == ("Someone", profile.RECENT_FROM, profile.POSTS)


def test_profile_top_and_other_fetchers(timeline):
    r = client().get("/someone?top&theme=light", headers={"User-Agent": TELEGRAM})
    assert f"https://{HOST}/m/q/theme=light&amp;top=1/u_someone.png" in r.text
    assert 'og:title" content="Someone (@someone)"' in r.text and 'og:url" content="https://x.com/someone"' in r.text
    assert ("someone", profile.TOP_FROM, None) in timeline


def test_profile_link_in_a_browser_goes_to_x():
    r = client().get("/someone?s=20", headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://x.com/someone?s=20"


def test_reserved_and_missing_profiles_go_to_x(timeline):
    c = client()
    for path in ("/home", "/explore", "/nobody"):
        r = c.get(path, headers={"User-Agent": DISCORDBOT}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == f"https://x.com{path}", path
    assert {h for h, *_ in timeline} == {"nobody"}


def test_profile_owner_key():
    assert embed._owner_key("123") == "123"
    assert embed._owner_key("u_Someone") == embed._owner_key("u_someone")
    assert embed._owner_key("u_someone").isdigit() and embed._owner_key("u_someone") != embed._owner_key("u_other")


def test_board_keeps_its_columns_even():
    posts = [post(i) for i in range(6)]
    b = profile.layout(USER, posts)
    xs = [x for x, _, _ in b.posts]
    assert len(b.posts) == 6 and set(xs) == {0, xs[-1]} and xs.count(0) == 3
    assert b.height >= max(y + L.height for _, y, L in b.posts) and b.height % 2 == 0


def test_top_picks_the_most_liked():
    posts = [post(i, likes=l) for i, l in enumerate([5, 50, 1, 40, 30, 2, 20, 10])]
    assert [p["likes"] for p in profile.pick(posts, True)] == [50, 40, 30, 20, 10, 5]
    assert [p["likes"] for p in profile.pick(posts, False)] == [5, 50, 1, 40, 30, 2]


def test_timeline_reads_pages_and_keeps_own_posts(monkeypatch):
    def raw(i, author="Pager", **kw):
        return {"id": str(i), "author": {"screen_name": author, "name": author}, "text": f"t{i}", "created_timestamp": 1_700_000_000 + i, **kw}
    pages = {None: ([raw(5), raw(4, reposted_by={"screen_name": "Pager"}), raw(3, author="else")], "c2"),
             "c2": ([raw(2), raw(1)], "c3"),
             "c3": ([raw(0)], None)}
    asked = []

    async def api(url, what):
        if "/2/profile/" not in url:
            return {"code": 200, "user": {"screen_name": "Pager", "name": "Pager", "avatar_url": "https://pbs.twimg.com/a_normal.jpg"}}
        cursor = parse_qs(urlsplit(url).query).get("cursor", [None])[0]
        asked.append(cursor)
        results, bottom = pages[cursor]
        return {"code": 200, "results": results, "cursor": {"bottom": bottom}}
    monkeypatch.setattr(tweet, "_api", api)
    got = asyncio.run(tweet.timeline("Pager", 100, want=2))
    assert [p["id"] for p in got["posts"]] == ["5", "2", "1"] and asked == [None, "c2"]
    assert got["user"]["avatar"] == "https://pbs.twimg.com/a_400x400.jpg"
    asked.clear()
    got = asyncio.run(tweet.timeline("pager", 3))          # three entries read: one page
    assert [p["id"] for p in got["posts"]] == ["5"] and asked == [None]


def test_timeline_asks_again_after_a_false_404(monkeypatch):
    misses = {"profile": 2, "statuses": 1}

    async def api(url, what):
        kind = "statuses" if "/2/profile/" in url else "profile"
        if misses[kind]:
            misses[kind] -= 1
            raise tweet.Missing("that account doesn't exist, or was deleted")
        if kind == "profile":
            return {"code": 200, "user": {"screen_name": "Flaky", "name": "Flaky"}}
        return {"code": 200, "results": [{"id": "1", "author": {"screen_name": "Flaky"}, "text": "hi"}], "cursor": {}}
    monkeypatch.setattr(tweet, "_api", api)
    got = asyncio.run(tweet.timeline("Flaky", 20))
    assert [p["id"] for p in got["posts"]] == ["1"] and misses == {"profile": 0, "statuses": 0}


def test_private_account(monkeypatch):
    async def api(url, what):
        return {"code": 200, "user": {"screen_name": "Locked", "protected": True}}
    monkeypatch.setattr(tweet, "_api", api)
    with pytest.raises(rs.ResolveError, match="private"):
        asyncio.run(tweet.timeline("Locked", 20))


# -------------------------------------------------------------------- gif ---

GIF = {"kind": "gif", "w": 480, "h": 270, "url": "https://video.twimg.com/tweet_video/x.mp4", "poster": None, "duration": 0.0}


@pytest.fixture
def gif_post(monkeypatch):
    async def fake(tid, f):
        return flags.apply(post(int(tid), media=[dict(GIF)]), f)
    monkeypatch.setattr(embed, "_post", fake)
    started = []
    monkeypatch.setattr(embed, "render", lambda tid, f: started.append(f.query()))
    return started


def test_lone_gif_goes_to_discord_as_the_gif_alone(gif_post):
    r = client().get("/someone/status/1?s=20&media", headers={"User-Agent": DISCORDBOT}, follow_redirects=False)
    # a page, not a redirect: Discord plays an image as a gif only when the
    # link it shows ends in .gif
    assert r.status_code == 200
    assert f'og:image" content="https://{HOST}/m/q/media=1/1.gif"' in r.text and 'content="image/gif"' in r.text
    assert "og:title" not in r.text
    assert gif_post == ["?media=1"]


def test_lone_gif_for_other_fetchers_is_the_real_gif(gif_post):
    r = client().get("/someone/status/1?media", headers={"User-Agent": TELEGRAM})
    assert f"https://{HOST}/m/q/media=1/1.gif" in r.text and 'content="image/gif"' in r.text


def test_gif_render_serves_only_its_gif(tmp_path):
    f = flags.parse({"media": ""})
    name = embed._name("7", f)
    (tmp_path / f"{name}.json").write_text(json.dumps({"id": "7", "ext": "gif", "query": f.query()}))
    (tmp_path / f"{name}.gif").write_bytes(b"GIF89a")
    c = client()
    r = c.get("/m/q/media=1/7.gif")
    assert r.status_code == 200 and r.headers["content-type"] == "image/gif" and r.content == b"GIF89a"
    assert c.get("/m/q/media=1/7.png").status_code == 404
    assert c.get("/m/q/media=1/7.mp4").status_code == 404
