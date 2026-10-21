"""Where the app's own fetches may go: every connection, redirect hops
included, only to a public address and to the one that was checked; image
bodies read up to their cap and no further, and only in the formats the cards
draw; no ffmpeg input allowed to open a local file. Nothing here reaches the
network: name lookups and sockets are swapped for fakes.

Run: PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests
"""
from __future__ import annotations

import asyncio
import io
import socket

import httpcore
import httpx
import pytest
from PIL import Image

from app import card, plan, tunnel
from app import resolve as rs

PUBLIC_IP = "93.184.216.34"
NAMES = {"public.example": PUBLIC_IP, "inside.example": "10.0.0.5", "router.example": "192.168.8.1"}


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        ip = NAMES.get(host, host)
        try:
            socket.inet_aton(ip)
        except OSError:
            raise socket.gaierror(socket.EAI_NONAME, "no such name") from None
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(tunnel, "_client", None)
    card._img_cache.clear()


def answer(status: int, *headers: str, body: bytes = b"") -> list[bytes]:
    head = [f"HTTP/1.1 {status} X\r\n".encode(), *(f"{h}\r\n".encode() for h in headers),
            f"Content-Length: {len(body)}\r\n".encode(), b"Connection: close\r\n", b"\r\n"]
    return head + ([body] if body else [])


class Sockets(httpcore.AsyncMockBackend):
    """Answers every connection with the same bytes, and notes where each went."""

    def __init__(self, buffer: list[bytes]):
        super().__init__(buffer)
        self.hosts: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.hosts.append(host)
        return await super().connect_tcp(host, port, timeout, local_address, socket_options)


class SocketsSync(httpcore.MockBackend):
    def __init__(self, buffer: list[bytes]):
        super().__init__(buffer)
        self.hosts: list[str] = []

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.hosts.append(host)
        return super().connect_tcp(host, port, timeout, local_address, socket_options)


def tunnel_sockets(buffer: list[bytes]) -> Sockets:
    """The tunnel's own client, its sockets faked below the address check."""
    fake = Sockets(buffer)
    tunnel.client()._transport._pool._network_backend.inner = fake
    return fake


def get(url: str) -> httpx.Response:
    async def go():
        return await tunnel.client().get(url)
    return asyncio.run(go())


# ---------------------------------------------------------------- the hops ---

def test_the_socket_goes_to_the_address_that_was_checked():
    fake = tunnel_sockets(answer(200, body=b"ok"))
    assert get("http://public.example/a").text == "ok"
    assert fake.hosts == [PUBLIC_IP]


@pytest.mark.parametrize("inside", ["http://inside.example/x", "http://router.example/", "http://127.0.0.1:8081/in/k"])
def test_a_redirect_inside_the_network_is_refused_before_connecting(inside):
    fake = tunnel_sockets(answer(302, f"Location: {inside}"))
    with pytest.raises(rs.ResolveError):
        get("http://public.example/a")
    assert fake.hosts == [PUBLIC_IP]


def test_redirects_stop_after_a_few_hops():
    fake = tunnel_sockets(answer(302, "Location: http://public.example/again"))
    with pytest.raises(httpx.TooManyRedirects):
        get("http://public.example/a")
    assert len(fake.hosts) == rs.MAX_REDIRECTS + 1


def test_a_card_image_redirected_inside_is_not_fetched(monkeypatch):
    fake = SocketsSync(answer(302, "Location: http://inside.example/avatar.png"))

    def transport():
        t = rs.public_transport_sync()
        t._pool._network_backend.inner = fake
        return t

    monkeypatch.setattr(card, "public_transport_sync", transport)
    assert card.fetch_bytes("http://public.example/avatar.png") is None
    assert fake.hosts == [PUBLIC_IP]


# ------------------------------------------------------------------ images ---

def serve(monkeypatch, handler) -> None:
    monkeypatch.setattr(card, "public_transport_sync", lambda: httpx.MockTransport(handler))


def test_an_image_body_is_read_only_up_to_the_cap(monkeypatch):
    sent = []

    def body():
        for _ in range(64):
            sent.append(1)
            yield b"\0" * (1 << 20)

    monkeypatch.setattr(card, "IMG_MAX", 4 << 20)
    serve(monkeypatch, lambda req: httpx.Response(200, content=body()))
    assert card.fetch_bytes("https://public.example/huge.png") is None
    assert len(sent) <= 5


def test_an_image_that_says_it_is_too_big_is_not_read(monkeypatch):
    sent = []

    def body():
        sent.append(1)
        yield b"\0"

    serve(monkeypatch, lambda req: httpx.Response(200, headers={"Content-Length": str(card.IMG_MAX + 1)}, content=body()))
    assert card.fetch_bytes("https://public.example/huge.png") is None
    assert sent == []


def encoded(fmt: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (4, 4), "#1d9bf0").save(out, fmt)
    return out.getvalue()


@pytest.mark.parametrize("fmt,drawn", [("PNG", True), ("JPEG", True), ("WEBP", True), ("GIF", True),
                                       ("BMP", False), ("TIFF", False), ("PPM", False)])
def test_only_the_formats_the_cards_draw_are_decoded(monkeypatch, fmt, drawn):
    data = encoded(fmt)
    serve(monkeypatch, lambda req: httpx.Response(200, content=data))
    im = card.fetch_image(f"https://public.example/pic.{fmt.lower()}")
    assert (im is not None) == drawn


# ------------------------------------------------------------------ ffmpeg ---

def fmt(protocol: str, url: str) -> rs.Fmt:
    return rs.Fmt(id="x", url=url, ext="mp4", protocol=protocol, kind="progressive", vcodec="h264", acodec="aac",
                  height=720, width=1280, fps=30, tbr=0, abr=0, size=0, hdr=False, lang_pref=0, lang=None)


def test_no_ffmpeg_input_may_open_a_local_file():
    p = plan.Plan("ffmpeg", "mp3", "a.mp3", "a", inputs=[fmt("m3u8", "https://public.example/a.m3u8"),
                                                          fmt("https", "https://public.example/a.mp4")],
                  cover="https://public.example/cover.jpg")
    argv = plan.build_inputs(p, None, lambda f: "http://127.0.0.1:8081/in/k" if f.direct else f.url)
    inputs = [i for i, a in enumerate(argv) if a == "-i"]
    assert len(inputs) == 3
    prev = 0
    for i in inputs:
        before = argv[prev:i]
        assert "-protocol_whitelist" in before, argv[i + 1]
        allowed = before[before.index("-protocol_whitelist") + 1].split(",")
        assert "https" in allowed and "file" not in allowed and "data" not in allowed
        prev = i
