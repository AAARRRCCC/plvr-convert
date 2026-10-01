"""The embed app's copies and metrics: ownership, the proxy between copies,
who a request is classified as, and where the metrics are served.

Run: PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app import embed, metrics, shard
from app import resolve as rs

BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"
DISCORDBOT = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
TELEGRAM = "TelegramBot (like TwitterBot)"
EVEN, ODD = "2099984681995309280", "2099984681995309281"


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    shard.configure(1, "embed-0")
    monkeypatch.setattr(shard, "_client", None)
    yield
    shard.configure(1, "embed-0")


# ---------------------------------------------------------------- ownership ---

def test_index_from_hostname():
    assert shard.index_of("embed-0") == 0
    assert shard.index_of("embed-12") == 12
    assert shard.index_of("my-pod-3") == 3
    assert shard.index_of("embed") is None
    assert shard.index_of("embed-abc") is None


def test_owner_is_id_mod_copies():
    shard.configure(2, "embed-1")
    assert shard.owner(EVEN) == 0 and shard.owner(ODD) == 1
    assert shard.owner("20") == 0 and shard.owner("21") == 1
    assert not shard.owned(EVEN) and shard.owned(ODD)
    shard.configure(3, "embed-2")
    assert shard.owner("20") == 2 and shard.owned("20")


def test_one_copy_or_unset_owns_everything():
    shard.configure(1, "embed-1")
    assert shard.COPIES == 1 and shard.owned(EVEN) and shard.owned(ODD)
    shard.configure(0, "whatever")
    assert shard.COPIES == 1 and shard.owned(ODD)


def test_bad_index_turns_sharding_off():
    shard.configure(2, "laptop")          # no number in the hostname
    assert shard.COPIES == 1
    shard.configure(2, "embed-5")         # a number past the last copy
    assert shard.COPIES == 1 and shard.owned(EVEN)


def test_env_index_beats_hostname():
    shard.configure(2, "laptop", index=1)
    assert shard.COPIES == 2 and shard.INDEX == 1


# ------------------------------------------------------------ servers in threads ---

class Live:
    """An ASGI app on a real socket, so a stream really streams."""

    def __init__(self, app, port: int = 0):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        return self

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(5)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_owner(log: dict):
    app = FastAPI()

    @app.get("/m/{name}")
    async def media(name: str, req: Request):
        log.setdefault("hits", []).append(dict(req.headers))

        async def chunks():
            try:
                for i in range(4):
                    yield f"chunk{i};".encode()
                    await asyncio.sleep(1.0)
            finally:
                log["closed"] = True
        return StreamingResponse(chunks(), media_type="video/mp4", headers={"Cache-Control": "no-store"})

    @app.get("/endless/{name}")
    async def endless(name: str):
        async def chunks():
            try:
                while True:
                    yield b"x" * 1024
                    await asyncio.sleep(0.05)
            finally:
                log["endless_closed"] = True
        return StreamingResponse(chunks(), media_type="video/mp4")

    @app.get("/api/v1/statuses/{tid}")
    async def status(tid: str, req: Request):
        log.setdefault("hits", []).append(dict(req.headers))
        return JSONResponse({"error": "from the owner"}, status_code=418, headers={"X-Owner": "yes"})

    @app.get("/{path:path}")
    async def page(path: str, req: Request):
        log.setdefault("hits", []).append(dict(req.headers))
        return RedirectResponse("https://example.com/rendered/file.mp4", status_code=302)
    return app


@pytest.fixture
def pair(monkeypatch):
    """Copy 1, the real app, with the owner (copy 0) a fake on a real port."""
    log: dict = {}
    with Live(fake_owner(log)) as owner:
        shard.configure(2, "embed-1", owner.url + "/")
        # the owner url has no {i}: both numbers map to it, only 0 is ever asked
        with Live(embed.app) as front:
            yield front, owner, log


# -------------------------------------------------------------------- proxy ---

def test_proxy_streams_and_passes_status_and_headers(pair):
    front, owner, log = pair
    t0 = time.time()
    with httpx.Client() as c, c.stream("GET", f"{front.url}/m/{EVEN}.mp4", headers={"User-Agent": DISCORDBOT, "Host": "embed.example"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"] == "video/mp4"
        assert r.headers["cache-control"] == "no-store"
        it = r.iter_bytes()
        first = next(it)
        t_first = time.time() - t0
        rest = b"".join(it)
    total = time.time() - t0
    assert first.startswith(b"chunk0;")
    assert b"chunk3;" in first + rest
    assert t_first < 0.9 and total > 2.5          # the first chunk came long before the last
    assert log["hits"][0]["user-agent"] == DISCORDBOT


def test_proxy_passes_status_headers_and_body(pair):
    front, owner, log = pair
    r = httpx.get(f"{front.url}/api/v1/statuses/{EVEN}")
    assert r.status_code == 418 and r.headers["x-owner"] == "yes" and r.json() == {"error": "from the owner"}


def test_proxy_passes_location_and_keeps_the_headers_the_app_reads(pair):
    front, owner, log = pair
    h = {"User-Agent": DISCORDBOT, "Host": "embed.example", "X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https",
         "X-Forwarded-Host": "embed.example", "CF-Connecting-IP": "203.0.113.9"}
    r = httpx.get(f"{front.url}/someone/status/{EVEN}?v2=image&s=20", headers=h, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://example.com/rendered/file.mp4"
    got = log["hits"][-1]
    want = {k.lower(): v for k, v in h.items()}
    for k in ("user-agent", "host", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "cf-connecting-ip"):
        assert got[k] == want[k], k
    assert got[shard.PROXIED] == "1"


def test_a_range_request_goes_through(pair):
    front, owner, log = pair
    httpx.get(f"{front.url}/m/{EVEN}.mp4", headers={"Range": "bytes=0-9"})
    assert log["hits"][-1]["range"] == "bytes=0-9"


def test_owned_post_is_not_proxied(pair, monkeypatch):
    front, owner, log = pair

    async def post(tid, f):
        raise rs.ResolveError("here")
    monkeypatch.setattr(embed, "_post", post)
    r = httpx.get(f"{front.url}/api/v1/statuses/{ODD}")      # odd: copy 1 owns it
    assert r.status_code == 404 and r.json() == {"error": "here"}
    assert "hits" not in log


def test_proxied_request_is_never_proxied_again(pair, monkeypatch):
    front, owner, log = pair

    async def post(tid, f):
        raise rs.ResolveError("here")
    monkeypatch.setattr(embed, "_post", post)
    r = httpx.get(f"{front.url}/api/v1/statuses/{EVEN}", headers={shard.PROXIED: "0"})
    assert r.json() == {"error": "here"}          # served by the copy that got it, not the owner
    assert "hits" not in log


def test_unreachable_owner_is_served_locally(monkeypatch):
    shard.configure(2, "embed-1", f"http://127.0.0.1:{free_port()}")

    async def post(tid, f):
        raise rs.ResolveError("here")
    monkeypatch.setattr(embed, "_post", post)
    before = sample("embed_proxied_total", result="fallback_local")
    with Live(embed.app) as front:
        t0 = time.time()
        r = httpx.get(f"{front.url}/api/v1/statuses/{EVEN}")
    assert r.status_code == 404 and r.json() == {"error": "here"}
    assert time.time() - t0 < 2
    assert sample("embed_proxied_total", result="fallback_local") == before + 1


def test_unresolvable_owner_is_served_locally(monkeypatch):
    shard.configure(2, "embed-1", "http://embed-nowhere.invalid:8080")

    async def post(tid, f):
        raise rs.ResolveError("here")
    monkeypatch.setattr(embed, "_post", post)
    with Live(embed.app) as front:
        r = httpx.get(f"{front.url}/api/v1/statuses/{EVEN}", timeout=10)
    assert r.json() == {"error": "here"}


def test_client_leaving_closes_the_owner_stream(pair):
    front, owner, log = pair
    with httpx.Client() as c:
        # a stream through the proxy on a path of the owner's own
        shard_url = f"{front.url}/m/{EVEN}.mp4"
        with c.stream("GET", shard_url) as r:
            next(r.iter_bytes())
    deadline = time.time() + 3
    while "closed" not in log and time.time() < deadline:
        time.sleep(0.05)
    assert log.get("closed")


def test_endless_stream_is_cancelled_on_disconnect():
    log: dict = {}
    with Live(fake_owner(log)) as owner:
        # the proxy keeps the path, so a front with the owner's own /endless route passes it straight on
        shard.configure(2, "embed-1", owner.url)
        app = FastAPI()

        @app.get("/endless/{tid}")
        async def e(tid: str, req: Request):
            return await shard.forward(req, tid)
        with Live(app) as front:
            with httpx.Client() as c, c.stream("GET", f"{front.url}/endless/{EVEN}") as r:
                got = 0
                for chunk in r.iter_bytes():
                    got += len(chunk)
                    if got > 4096:
                        break
        deadline = time.time() + 3
        while "endless_closed" not in log and time.time() < deadline:
            time.sleep(0.05)
        assert log.get("endless_closed")


# ------------------------------------------------------------------ metrics ---

def test_agent_classification():
    assert metrics.agent("TelegramBot (like TwitterBot)") == "telegram"
    assert metrics.agent("Slackbot-LinkExpanding 1.0") == "slack"
    assert metrics.agent("WhatsApp/2.23.20.0") == "whatsapp"
    assert metrics.agent("facebookexternalhit/1.1") == "facebook"
    assert metrics.agent("Mozilla/5.0 (compatible; SomethingBot/1.0)") == "other"
    assert metrics.agent("") == "other"


def test_host_capping(monkeypatch):
    monkeypatch.setattr(metrics, "HOSTS", frozenset({"known.example"}))
    assert metrics.domain("known.example") == "known.example"
    assert metrics.domain("Known.Example:443") == "known.example"
    assert metrics.domain("evil.example") == "other"
    assert metrics.domain("a" * 5000) == "other"
    assert metrics.domain(None) == "other"
    assert metrics.domain("known.example.evil.com") == "other"


def test_requests_counted_by_kind(monkeypatch):
    monkeypatch.setattr(metrics, "HOSTS", frozenset({"known.example"}))
    c = TestClient(embed.app)
    H = {"Host": "known.example"}

    def n(kind, me="false"):
        return sample("embed_requests_total", kind=kind, domain="known.example", me=me)
    d0, b0, w0, r0 = n("discord"), n("bot"), n("browser"), n("raw")
    c.get("/u/status/5?help", headers={**H, "User-Agent": DISCORDBOT})
    c.get("/u/status/5?help", headers={**H, "User-Agent": TELEGRAM})
    c.get("/u/status/5", headers={**H, "User-Agent": BROWSER}, follow_redirects=False)
    assert (n("discord"), n("bot"), n("browser")) == (d0 + 1, b0 + 1, w0 + 1)
    assert sample("embed_bot_requests_total", agent="telegram", domain="known.example") >= 1
    # a forged host is "other"
    o0 = sample("embed_requests_total", kind="browser", domain="other", me="false")
    c.get("/u/status/5", headers={"Host": "forged-%d.example" % time.time_ns(), "User-Agent": BROWSER}, follow_redirects=False)
    assert sample("embed_requests_total", kind="browser", domain="other", me="false") == o0 + 1


def test_me_cookie_separates_own_clicks(monkeypatch):
    monkeypatch.setattr(metrics, "HOSTS", frozenset({"known.example"}))
    c = TestClient(embed.app, base_url="https://known.example")
    r = c.get("/me", follow_redirects=False)
    assert r.status_code == 200 and "excluded from the counts" in r.text      # not the catch-all's redirect to x.com
    cookie = r.headers["set-cookie"].lower()
    for part in ("embed_me=1", "httponly", "secure", "samesite=lax", "path=/", "max-age=315360000"):
        assert part in cookie
    assert "domain=" not in cookie          # host-only: one cookie per domain

    def n(me):
        return sample("embed_requests_total", kind="browser", domain="known.example", me=me)
    t0, f0 = n("true"), n("false")
    c.get("/u/status/5", headers={"User-Agent": BROWSER}, follow_redirects=False)        # carries the cookie
    assert (n("true"), n("false")) == (t0 + 1, f0)
    c.cookies.clear()
    c.get("/u/status/5", headers={"User-Agent": BROWSER}, follow_redirects=False)
    assert (n("true"), n("false")) == (t0 + 1, f0 + 1)


def test_a_bot_is_never_me(monkeypatch):
    monkeypatch.setattr(metrics, "HOSTS", frozenset({"known.example"}))
    c = TestClient(embed.app, base_url="https://known.example")
    c.cookies.set("embed_me", "1")
    b = sample("embed_requests_total", kind="discord", domain="known.example", me="false")
    c.get("/u/status/5?help", headers={"User-Agent": DISCORDBOT})
    assert sample("embed_requests_total", kind="discord", domain="known.example", me="false") == b + 1


def test_metrics_are_not_on_the_public_app():
    c = TestClient(embed.app)
    for path in ("/metrics", "/metrics/", "/metrics?name[]=x"):
        r = c.get(path, headers={"User-Agent": BROWSER}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"].startswith("https://x.com/metrics")
        r = c.get(path, headers={"User-Agent": DISCORDBOT}, follow_redirects=False)
        assert "embed_requests_total" not in r.text


def test_metrics_served_on_their_own_port(monkeypatch):
    port = free_port()
    monkeypatch.setattr(metrics, "PORT", port)
    monkeypatch.setattr(metrics, "_started", False)
    metrics.start()
    text = httpx.get(f"http://127.0.0.1:{port}/metrics").text
    for name in ("embed_requests_total", "embed_renders_total", "embed_render_seconds_bucket", "embed_proxied_total",
                 "embed_renders_running", "embed_posts_rendered_total"):
        assert name in text
    # the 5 s bucket is there to answer "how often over 5 s"
    assert 'embed_render_seconds_bucket{ext="mp4",le="5.0"}' in text or 'le="5.0"' in text


def test_render_outcomes_are_counted():
    ok0 = sample("embed_renders_total", outcome="ok", ext="mp4")
    metrics.render_done("ok", "mp4", 6.0)
    metrics.render_done("error", "weird", 1.0)
    assert sample("embed_renders_total", outcome="ok", ext="mp4") == ok0 + 1
    assert sample("embed_renders_total", outcome="error", ext="unknown") >= 1
    assert sample("embed_render_seconds_bucket", ext="mp4", le="5.0") is not None


def test_busy_is_counted(monkeypatch):
    monkeypatch.setattr(embed, "QUEUE", 0)
    b = sample("embed_renders_total", outcome="busy", ext="unknown")
    with pytest.raises(embed.Busy):
        embed.render("1", embed.flags.parse({}))
    assert sample("embed_renders_total", outcome="busy", ext="unknown") == b + 1
