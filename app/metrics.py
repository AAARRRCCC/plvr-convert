"""Traffic and render metrics for the embed host.

Served on their own port (METRICS_PORT, 9100), never on the public one: the
embed app's catch-all route answers anything on its public port, so a /metrics
route there would be world-readable. The cluster's Service doesn't carry this
port; Prometheus scrapes the pod directly.

Label values are all from fixed sets, so a hostile request can't grow them:
the domain is the request host only if it is in EMBED_HOSTS, else "other".

A person can leave their own clicks out of the counts by opening /me once in
each browser: it sets a cookie that marks that browser's requests me="true".
"""
from __future__ import annotations

import logging
import os
import re
import threading
from contextlib import contextmanager

from prometheus_client import Counter, Gauge, Histogram, start_http_server

log = logging.getLogger("embed.metrics")

PORT = int(os.environ.get("METRICS_PORT", 9100) or 0)    # 0: no metrics server
HOSTS = frozenset(h.strip().lower() for h in os.environ.get("EMBED_HOSTS", "").split(",") if h.strip())
ME_COOKIE = "embed_me"
ME_MAX_AGE = 10 * 365 * 24 * 3600

# the chat apps told apart, by their user agent; any other bot is "other"
AGENTS = (
    ("telegram", re.compile(r"telegram", re.I)),
    ("slack", re.compile(r"slack", re.I)),
    ("whatsapp", re.compile(r"whatsapp", re.I)),
    ("facebook", re.compile(r"facebookexternalhit|facebot", re.I)),
    ("twitter", re.compile(r"twitterbot", re.I)),
    ("mastodon", re.compile(r"mastodon", re.I)),
    ("bluesky", re.compile(r"bluesky|cardyb", re.I)),
    ("skype", re.compile(r"skype", re.I)),
    ("pinterest", re.compile(r"pinterest", re.I)),
    ("google", re.compile(r"googlebot|google-", re.I)),
)
AGENT_NAMES = tuple(n for n, _ in AGENTS) + ("other",)
KINDS = ("discord", "bot", "browser", "raw", "other")

REQUESTS = Counter("embed_requests_total", "Requests to a post link, by who sent them",
                   ["kind", "domain", "me"])
BOT_REQUESTS = Counter("embed_bot_requests_total", "Post link requests from bots other than Discord, by app",
                       ["agent", "domain"])
FILE_REQUESTS = Counter("embed_file_requests_total", "Requests for a render's file, oembed or status",
                        ["route", "domain", "me"])
RENDERS = Counter("embed_renders_total", "Renders, by how they ended", ["outcome", "ext"])
RENDER_SECONDS = Histogram("embed_render_seconds", "Wall time of a finished render, queue wait included", ["ext"],
                           buckets=(1, 2, 3, 4, 5, 7, 10, 15, 30, 60))
RENDER_WAIT_SECONDS = Histogram("embed_render_wait_seconds", "Time a render waited for a free slot",
                                buckets=(0.05, 0.5, 1, 2, 5, 10, 30, 60, 120))
POSTS = Counter("embed_posts_rendered_total", "Renders started: a post, with its flags, that was not in the cache")
PROXIED = Counter("embed_proxied_total", "Requests for another copy's post, by what became of them", ["result"])
MP4_FIRST_BYTE = Histogram("embed_mp4_first_byte_seconds", "From the request for a mid-render mp4 to its first byte",
                           buckets=(0.5, 1, 2, 3, 5, 7, 10, 15, 30, 60))
RUNNING = Gauge("embed_renders_running", "Renders holding a slot")
JOBS = Gauge("embed_render_jobs", "Posts rendering or waiting for a slot")
QUEUED = Gauge("embed_renders_queued", "Posts waiting for a slot")

for _k in ("ok", "error", "cancelled", "busy"):
    for _e in ("png", "mp4", "gif", "unknown"):
        RENDERS.labels(_k, _e)
for _e in ("png", "mp4", "gif"):
    RENDER_SECONDS.labels(_e)
for _r in ("owner", "fallback_local"):
    PROXIED.labels(_r)

_running = 0
_lock = threading.Lock()


def bind_jobs(count) -> None:
    """Hand the gauges the live count of render jobs."""
    JOBS.set_function(count)
    QUEUED.set_function(lambda: max(0, count() - _running))


@contextmanager
def running():
    global _running
    with _lock:
        _running += 1
    RUNNING.inc()
    try:
        yield
    finally:
        with _lock:
            _running -= 1
        RUNNING.dec()


def domain(host: str | None) -> str:
    """The request host, if it is one we serve; else "other"."""
    h = (host or "").lower().split(":")[0].rstrip(".")
    return h if h in HOSTS else "other"


def agent(ua: str) -> str:
    for name, rx in AGENTS:
        if rx.search(ua):
            return name
    return "other"


def is_me(cookies) -> bool:
    return cookies.get(ME_COOKIE) == "1"


def request(kind: str, host: str | None, cookies, ua: str = "") -> None:
    """One request to a post link. Bots never carry the cookie, and a bot is
    never "me", whatever it sends."""
    d = domain(host)
    bot = kind in ("discord", "bot")
    REQUESTS.labels(kind, d, "false" if bot else str(is_me(cookies)).lower()).inc()
    if kind == "bot":
        BOT_REQUESTS.labels(agent(ua), d).inc()


def file_request(route: str, host: str | None, cookies, ua: str = "", bot: bool = False) -> None:
    FILE_REQUESTS.labels(route, domain(host), "false" if bot else str(is_me(cookies)).lower()).inc()


def render_done(outcome: str, ext: str, seconds: float) -> None:
    RENDERS.labels(outcome, ext if ext in ("png", "mp4", "gif") else "unknown").inc()
    if outcome == "ok":
        RENDER_SECONDS.labels(ext).observe(seconds)


_started = False


def start() -> None:
    """Serve /metrics on PORT, in a thread; once."""
    global _started
    if _started or not PORT:
        return
    try:
        start_http_server(PORT)
    except OSError as e:
        log.error("metrics server not started on :%d: %s", PORT, e)
        return
    _started = True
    log.info("metrics on :%d", PORT)
