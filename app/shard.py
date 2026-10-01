"""Several copies of the embed app, each post rendered by one of them.

Chat apps fetch a link more than once, and behind one Service those fetches
land on different copies, which would each render the post. So a post belongs
to copy `int(id) % EMBED_COPIES`, and a copy that gets a request for a post it
doesn't own passes it on, in-cluster, to the owner and streams the answer back:
the owner behaves as if the request had come straight to it.

The copies are a StatefulSet, so a copy's number is the end of its hostname
(embed-1 is 1) and its siblings are at stable names, set by EMBED_OWNER_URL
with {i} for the copy's number. With EMBED_COPIES unset or 1 none of this
happens.

If the owner can't be reached (connection refused, no DNS, a connect timeout:
a rollout, a node down) the request is served here instead of failing.
"""
from __future__ import annotations

import logging
import os
import re

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from . import metrics

log = logging.getLogger("embed.shard")

PROXIED = "x-embed-proxied"      # set on a request passed between copies, so it is never passed on again
DEFAULT_URL = "http://embed-{i}.embed-pods.embed.svc.cluster.local:8080"
CONNECT_TIMEOUT = float(os.environ.get("PROXY_CONNECT_TIMEOUT", 1))
READ_TIMEOUT = 960.0             # a render may take RENDER_TIMEOUT before the owner has anything to say
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te", "trailer",
       "transfer-encoding", "upgrade", "content-length"}
DROP_BACK = (HOP - {"content-length"}) | {"date", "server"}

COPIES = 1
INDEX = 0
URL = DEFAULT_URL
_client: httpx.AsyncClient | None = None


def index_of(hostname: str) -> int | None:
    """A StatefulSet pod's number: the digits after the last dash."""
    m = re.search(r"-(\d+)$", hostname)
    return int(m.group(1)) if m else None


def configure(copies: int, hostname: str, url: str = DEFAULT_URL, index: int | None = None) -> None:
    """Set how many copies there are and which one this is. Anything that
    doesn't add up leaves sharding off, which is the single-copy behaviour."""
    global COPIES, INDEX, URL
    URL = url
    COPIES, INDEX = 1, 0
    if copies <= 1:
        return
    i = index if index is not None else index_of(hostname)
    if i is None or not 0 <= i < copies:
        log.error("EMBED_COPIES=%d but this copy's number is %r (hostname %r): sharding is off", copies, i, hostname)
        return
    COPIES, INDEX = copies, i
    log.info("copy %d of %d", INDEX, COPIES)


def from_env() -> None:
    import socket
    idx = os.environ.get("EMBED_INDEX")
    configure(int(os.environ.get("EMBED_COPIES", 1) or 1), socket.gethostname(), os.environ.get("EMBED_OWNER_URL", DEFAULT_URL),
              int(idx) if idx not in (None, "") else None)


def owner(tid: str) -> int:
    return int(tid) % COPIES


def owned(tid: str) -> bool:
    return COPIES <= 1 or owner(tid) == INDEX


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT), follow_redirects=False,
                                    trust_env=False, limits=httpx.Limits(max_connections=200, max_keepalive_connections=20))
    return _client


async def close() -> None:
    global _client
    c, _client = _client, None
    if c is not None:
        await c.aclose()


def _target(req: Request, i: int) -> str:
    raw = req.scope.get("raw_path")
    path = raw.decode("latin-1") if raw else req.url.path
    q = req.scope.get("query_string", b"").decode("latin-1")
    return URL.format(i=i).rstrip("/") + path + ("?" + q if q else "")


async def _body(resp: httpx.Response):
    """The owner's answer as it comes. Closing it when the client goes (the
    generator is cancelled, or the response's background task runs) drops the
    connection, and the owner sees its own client leave."""
    try:
        async for chunk in resp.aiter_raw():
            yield chunk
    finally:
        await resp.aclose()


async def forward(req: Request, tid: str) -> StreamingResponse | None:
    """The owner's answer to this request, or None when it is to be served
    here: this copy owns the post, the request already came from a sibling,
    or the owner can't be reached."""
    if COPIES <= 1 or req.headers.get(PROXIED) or owned(tid):
        return None
    i = owner(tid)
    headers = [(k, v) for k, v in req.headers.items() if k not in HOP]
    headers.append((PROXIED, str(INDEX)))
    c = client()
    try:
        resp = await c.send(c.build_request("GET", _target(req, i), headers=headers), stream=True)
    except httpx.TransportError as e:
        log.warning("copy %d of %s unreachable (%s: %s): serving it here", i, tid, type(e).__name__, e)
        metrics.PROXIED.labels("fallback_local").inc()
        return None
    metrics.PROXIED.labels("owner").inc()
    out = {k: v for k, v in resp.headers.items() if k not in DROP_BACK}
    return StreamingResponse(_body(resp), status_code=resp.status_code, headers=out, background=BackgroundTask(resp.aclose))
