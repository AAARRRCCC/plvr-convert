"""GIF detection, native GIF defaults, highest non-GIF defaults, and multi-GIF
Twitter posts. Nothing here reaches the network.

Run: PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import main
from app import plan as planning
from app import resolve as rs


def _twitter_gif(idx: int = 1, slug: str = "HYASFyDaAAAncmT") -> dict:
    return {
        "id": f"212571891466357964{idx}",
        "title": f"Aku - https://t.co/I67SCGB255 #{idx}",
        "uploader": "Aku",
        "extractor_key": "Twitter",
        "webpage_url": "https://x.com/akudesunee/status/2125719103809941910",
        "thumbnail": f"https://pbs.twimg.com/tweet_video_thumb/{slug}.jpg?name=orig",
        "formats": [{
            "format_id": "http",
            "ext": "mp4",
            "vcodec": None,
            "acodec": None,
            "width": None,
            "height": None,
            "fps": None,
            "url": f"https://video.twimg.com/tweet_video/{slug}.mp4",
            "protocol": "https",
        }],
    }


def _twitter_video() -> dict:
    return {
        "id": "999999",
        "title": "normal video",
        "uploader": "Someone",
        "extractor_key": "Twitter",
        "webpage_url": "https://x.com/someone/status/999999",
        "formats": [
            {"format_id": "http-480", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "width": 854, "height": 480, "fps": 30, "tbr": 800,
             "url": "https://video.twimg.com/ext_tw_video/999999/pu/vid/854x480/a.mp4", "protocol": "https"},
            {"format_id": "http-1080", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "width": 1920, "height": 1080, "fps": 60, "tbr": 4500,
             "url": "https://video.twimg.com/ext_tw_video/999999/pu/vid/1920x1080/b.mp4", "protocol": "https"},
            {"format_id": "http-2160", "ext": "mp4", "vcodec": "avc1", "acodec": "mp4a", "width": 3840, "height": 2160, "fps": 60, "tbr": 12000,
             "url": "https://video.twimg.com/ext_tw_video/999999/pu/vid/3840x2160/c.mp4", "protocol": "https"},
        ],
    }


def test_defaults_are_highest():
    o = planning.normalize({})
    assert o["quality"] == "max"
    assert o["aformat"] == "best"
    assert o["abitrate"] == "320"
    assert o["gif_width"] == 800
    assert o["gif_fps"] == 30


def test_normalize_accepts_native_gif_options():
    o = planning.normalize({"gif_width": "native", "gif_fps": "native"})
    assert o["gif_width"] == "native"
    assert o["gif_fps"] == "native"
    o2 = planning.normalize({"gif_width": "480", "gif_fps": "15"})
    assert o2["gif_width"] == 480
    assert o2["gif_fps"] == 15
    o3 = planning.normalize({"gif_width": "bogus", "gif_fps": "bogus"})
    assert o3["gif_width"] == 800
    assert o3["gif_fps"] == 30


def test_gif_detection_and_describe():
    g = _twitter_gif()
    d = rs.describe(g)
    assert d["is_gif"] is True
    assert d["has_video"] is True
    assert d["has_audio"] is False

    v = _twitter_video()
    dv = rs.describe(v)
    assert dv["is_gif"] is False
    assert dv["has_video"] is True
    assert dv["has_audio"] is True

    tenor = {
        "id": "t1", "title": "cat", "extractor_key": "Tenor",
        "formats": [{"url": "https://media.tenor.com/abc/cat.mp4", "ext": "mp4", "width": 498, "height": 498, "fps": 20}],
    }
    dt = rs.describe(tenor)
    assert dt["is_gif"] is True
    assert dt["has_audio"] is False
    assert dt["width"] == 498
    assert dt["fps"] == 20


def test_gif_plan_defaults_to_native_in_auto_mode():
    g = _twitter_gif()
    p = planning.make(g, planning.normalize({"mode": "auto"}))
    assert p.method == "ffmpeg"
    assert p.ext == "gif"
    assert p.label == "gif · native"
    vf = p.args[p.args.index("-vf") + 1]
    assert vf.startswith("split[a][b];")
    assert "fps=" not in vf
    assert "palettegen" in vf
    assert "-loop" in p.args


def test_direct_gif_proxies_when_native():
    direct = {
        "id": "g1", "title": "loop", "extractor_key": "generic",
        "formats": [{"url": "https://cdn.example.com/loop.gif", "ext": "gif", "protocol": "https"}],
    }
    p = planning.make(direct, planning.normalize({"mode": "auto"}))
    assert p.method == "proxy"
    assert p.ext == "gif"
    assert p.label == "gif · native · direct"


def test_gif_explicit_resize_and_fps():
    g = _twitter_gif()
    p = planning.make(g, planning.normalize({"mode": "gif", "gif_width": 480, "gif_fps": 15}))
    assert p.ext == "gif"
    assert p.label == "gif · 480px · 15fps"
    vf = p.args[p.args.index("-vf") + 1]
    assert "fps=15" in vf
    assert "scale='min(480,iw)':-2:flags=lanczos" in vf


def test_video_defaults_to_highest_quality_and_gif_settings():
    v = _twitter_video()
    p = planning.make(v, planning.normalize({}))
    assert p.ext == "mp4"
    assert p.src is not None and p.src.height == 2160

    pg = planning.make(v, planning.normalize({"mode": "gif"}))
    assert pg.ext == "gif"
    assert pg.label == "gif · 800px · 30fps"
    # 800w gif picks the 1080p stream rather than capping at 480p
    assert pg.inputs[0].height == 1080


def test_multi_gif_tweet_resolves_as_gifs_and_prepares_native(monkeypatch):
    playlist = {
        "_type": "playlist",
        "title": "Aku - https://t.co/I67SCGB255",
        "uploader": "Aku",
        "extractor_key": "Twitter",
        "entries": [
            _twitter_gif(1, "HYASFyDaAAAncmT"),
            _twitter_gif(2, "HYASKnlakAArTdb"),
            _twitter_gif(3, "HYASMMIbcAAilgp"),
            _twitter_gif(4, "HYASOnLaYAA3RZr"),
        ],
    }

    async def fake_photo_post(tid):
        return None

    async def fake_resolve(url):
        return "abc123", playlist

    monkeypatch.setattr(main, "_photo_post", fake_photo_post)
    monkeypatch.setattr(rs, "resolve", fake_resolve)

    client = TestClient(main.app)
    r = client.post("/api/resolve", json={"url": "https://x.com/akudesunee/status/2125719103809941910", "mode": "auto"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["picker"]) == 4
    assert all(item["is_gif"] is True for item in body["picker"])
    assert all(item["has_audio"] is False for item in body["picker"])

    for idx in range(4):
        pr = client.post("/api/prepare", json={"url": "https://x.com/akudesunee/status/2125719103809941910", "item": idx, "options": {"mode": "auto"}})
        assert pr.status_code == 200
        pb = pr.json()
        assert pb["ext"] == "gif"
        assert pb["label"] == "gif · native"
        assert pb["filename"].endswith(".gif")
