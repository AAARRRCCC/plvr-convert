"""A post's photos: the zip they are streamed as opens in any unzipper, in
the post's order, byte for byte; a zip that can't finish never looks whole;
photos come only from x.com's image host. Nothing here reaches the network.

Run: PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests
"""
from __future__ import annotations

import asyncio
import io
import os
import zipfile

import pytest

from app import photos
from app import plan as planning
from app import resolve as rs

POST = {"id": "2124576956968808953", "handle": "someone", "name": "Some One", "title": "twenty pictures", "created": 1.76e9,
        "url": "https://x.com/someone/status/2124576956968808953", "videos": 0,
        "photos": [{"url": f"https://pbs.twimg.com/media/P{i}.{'png' if i == 2 else 'jpg'}?name=orig", "w": 1200, "h": 900} for i in range(12)]}


SEL = list(range(12))


def _bytes(url: str) -> bytes:
    return url.encode() + os.urandom(3000)


def _run(sel=SEL):
    async def go():
        first, rest = await photos.first_and_rest(POST, sel)
        out = bytearray(first)
        async for chunk in rest:
            out += chunk
        return bytes(out)
    return asyncio.run(go())


def test_zip_round_trips(monkeypatch):
    sent = {}

    async def fetch(url):
        await asyncio.sleep(0.001 * (hash(url) % 5))   # out of order, so the writer has to keep the post's
        sent[url] = _bytes(url)
        return sent[url]

    monkeypatch.setattr(photos, "fetch", fetch)
    z = zipfile.ZipFile(io.BytesIO(_run()))
    assert z.testzip() is None
    names = z.namelist()
    assert names == [photos.member(POST, i) for i in range(12)] == sorted(names)
    assert names[2].endswith("_03.png")
    for i, info in enumerate(z.infolist()):
        assert info.compress_type == zipfile.ZIP_STORED
        assert not info.flag_bits & 0x08            # sizes in the local header, no data descriptor
        assert z.read(info) == sent[POST["photos"][i]["url"]]


def test_zip_that_fails_part_way_is_not_whole(monkeypatch):
    async def fetch(url):
        if "P7." in url:
            raise rs.ResolveError("a photo in that post is gone")
        return _bytes(url)

    monkeypatch.setattr(photos, "fetch", fetch)
    with pytest.raises(rs.ResolveError):
        _run()


def test_first_photo_failing_is_an_error_before_any_bytes(monkeypatch):
    async def fetch(url):
        raise rs.ResolveError("x.com refused a photo (403)")

    monkeypatch.setattr(photos, "fetch", fetch)
    with pytest.raises(rs.ResolveError):
        asyncio.run(photos.first_and_rest(POST, SEL))


def test_only_the_image_host():
    with pytest.raises(rs.ResolveError):
        asyncio.run(photos.fetch("https://example.com/media/x.jpg"))
    with pytest.raises(rs.ResolveError):
        asyncio.run(photos.fetch("http://pbs.twimg.com/media/x.jpg"))


def test_names():
    o = planning.normalize({"name": "random"})
    a, b = photos.filename(POST, o, [0]), photos.filename(POST, o, [1])
    assert a != b and a.endswith("_01.jpg") and b.endswith("_02.jpg")
    assert photos.filename(POST, o, SEL).endswith(".zip")
    o = planning.normalize({"name": "pretty"})
    assert photos.filename(POST, o, [2]) == "twenty pictures (photo 03).png"
    assert photos.filename(POST, o, SEL) == "twenty pictures (12 photos).zip"
    assert photos.filename(POST, o, [1, 4, 9]) == "twenty pictures (3 of 12 photos).zip"


def test_a_few_picked_keep_their_numbers(monkeypatch):
    async def fetch(url):
        return _bytes(url)

    monkeypatch.setattr(photos, "fetch", fetch)
    z = zipfile.ZipFile(io.BytesIO(_run([1, 4, 9])))
    assert z.testzip() is None
    assert [n[-6:] for n in z.namelist()] == ["02.jpg", "05.jpg", "10.jpg"]
