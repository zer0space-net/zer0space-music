"""The audio relay: signed tickets in, streamed bytes out.

Why the browser never gets the real URL:

* A resolved YouTube audio URL is **bound to the IP that resolved it**. Handing
  it to a phone on mobile data produces a 403, which in a player looks exactly
  like a broken track. The same constraint drives the Crimson backend's
  ``/voe_proxy``; this is the music-shaped version of it.
* It is also a bare capability — anyone holding it can pull the audio with no
  authentication at all.

So the player asks for a **ticket**: a signed, expiring string naming one track
and one user. It appends the ticket to ``<audio src>``; this module verifies it,
resolves the track (from cache, almost always) and streams the bytes back.

**Range requests are not optional.** Safari — every browser on iOS, and the one
that has to keep playing when the screen locks — probes a media URL with
``Range: bytes=0-1`` before it will commit, and refuses to show a seek bar on a
response that does not advertise ``Accept-Ranges``. A relay that ignores Range
produces audio that plays from the start and cannot be scrubbed, which is the
single most common way a self-hosted player feels broken on a phone.
"""

from __future__ import annotations

import re
from typing import AsyncIterator

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from itsdangerous import BadData, URLSafeTimedSerializer

from . import config
from .providers.base import ResolvedSource

_TICKET_SALT = "zs.music.stream"
_serializer: URLSafeTimedSerializer | None = None


def serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(config.STREAM_SECRET, salt=_TICKET_SALT)
    return _serializer


def mint(track_key: str, user_id: str) -> str:
    """A ticket for one track and one listener."""
    return serializer().dumps({"k": track_key, "u": user_id})


def redeem(ticket: str) -> tuple[str, str] | None:
    """``(track_key, user_id)`` for a valid ticket, else ``None``."""
    if not ticket or len(ticket) > 512:
        return None
    try:
        payload = serializer().loads(ticket, max_age=config.STREAM_TTL)
    except BadData:
        return None
    if not isinstance(payload, dict):
        return None
    key = str(payload.get("k") or "")
    user = str(payload.get("u") or "")
    if not key or not user:
        return None
    return key, user


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            # No read timeout: this is a byte stream that stays open for the
            # length of a song and can legitimately stall while a phone's buffer
            # is full. Connect and pool stay bounded so a dead CDN fails fast.
            timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
            follow_redirects=True,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


# Only these are copied from the CDN back to the browser. An allow list, not a
# deny list: the response is served from the dashboard's own origin, so anything
# relayed speaks with that origin's authority. A relayed ``set-cookie`` would let
# a media CDN write the zer0space session cookie.
_RESPONSE_ALLOW = {
    "accept-ranges",
    "content-length",
    "content-range",
    "content-type",
    "etag",
    "last-modified",
}

# Only the client headers a media fetch legitimately needs. Everything else —
# the session cookie above all — stops at this hop.
_REQUEST_FORWARD = {"range", "if-range", "if-none-match", "if-modified-since"}

_RANGE_OK = re.compile(r"^bytes=\d*-\d*(,\s*\d*-\d*)*$")


def _request_headers(request: Request, source: ResolvedSource) -> dict[str, str]:
    headers = dict(source.headers)
    for name in _REQUEST_FORWARD:
        value = request.headers.get(name)
        if not value:
            continue
        # The Range value is pasted into an outbound request; a malformed one is
        # rejected here rather than handed to the CDN to interpret.
        if name == "range" and not _RANGE_OK.match(value.strip()):
            continue
        headers[name] = value
    headers.setdefault("User-Agent", config.USER_AGENT)
    # Identity encoding: the bytes are already compressed audio, and a
    # transfer-encoding applied here would break the byte offsets Range depends on.
    headers["Accept-Encoding"] = "identity"
    return headers


def _response_headers(upstream: httpx.Response, source: ResolvedSource) -> dict[str, str]:
    out = {
        key.lower(): value
        for key, value in upstream.headers.items()
        if key.lower() in _RESPONSE_ALLOW
    }
    # The CDN often serves audio as application/octet-stream or video/mp4, and a
    # phone decides whether to even try decoding from this header. The mime the
    # scraper determined from the actual format is the accurate one.
    if source.mime:
        out["content-type"] = source.mime
    # Advertise range support even if the upstream was quiet about it — httpx
    # normalises HEAD/GET differences and Safari needs to see it before it will
    # render a seek bar.
    out.setdefault("accept-ranges", "bytes")
    # Private: the response is a per-user capability, and an intermediate cache
    # holding it would serve one listener's audio to another.
    out["cache-control"] = "private, max-age=0, no-store"
    out["x-accel-buffering"] = "no"
    out["content-security-policy"] = "default-src 'none'; sandbox"
    # A media URL is never markup, but a CDN that mislabels a response as
    # text/html would otherwise get it rendered on this origin.
    out["x-content-type-options"] = "nosniff"
    return out


async def relay(request: Request, source: ResolvedSource) -> Response:
    """Stream ``source`` back to the caller, honouring Range."""
    method = "HEAD" if request.method == "HEAD" else "GET"
    try:
        upstream_request = client().build_request(
            method, source.url, headers=_request_headers(request, source)
        )
        upstream = await client().send(upstream_request, stream=True)
    except httpx.RequestError as err:
        print(f"[music] media upstream unreachable: {err!r}")
        return JSONResponse(
            {"error": "The audio source could not be reached", "code": "MEDIA_UNREACHABLE"},
            status_code=502,
        )

    if upstream.status_code >= 400:
        # 403 here is nearly always an expired or IP-mismatched URL. The caller
        # drops the cached row and re-resolves, so the message is for the log.
        print(f"[music] media upstream {upstream.status_code} for {source.provider}")
        await upstream.aclose()
        return JSONResponse(
            {"error": "The audio source rejected the request", "code": "MEDIA_REJECTED"},
            status_code=502 if upstream.status_code >= 500 else 410,
        )

    headers = _response_headers(upstream, source)

    if method == "HEAD":
        await upstream.aclose()
        return Response(status_code=upstream.status_code, headers=headers)

    async def body() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body(), status_code=upstream.status_code, headers=headers
    )
