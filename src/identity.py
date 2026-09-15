"""Who is calling, and may they.

This service has **no accounts and no login**. It publishes no ports and is
reachable only from the dashboard, which has already checked the zer0space
session and names the user in a header.

Two things are checked together and must never be split apart:

1. The caller presents the shared service token (``Authorization: Bearer``).
2. The caller names a user (``X-Zer0space-User``).

Trusting a header because "only the dashboard can reach us" is exactly the
assumption that stops holding the first time the network layout changes. The
token is what makes the header meaningful; a request with one and not the other
is rejected.
"""

from __future__ import annotations

import hmac

from fastapi import Request

from . import config


class Unauthorized(Exception):
    """No valid identity on the request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _token_ok(request: Request) -> bool:
    if not config.REQUIRE_TOKEN:
        # Local development only; the boot log warns loudly about this.
        return True
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented:
        return False
    # Constant time: the token is a fixed secret, and a length-leaking or
    # early-exiting comparison on a value an attacker can retry is the textbook
    # way to recover one byte at a time.
    return hmac.compare_digest(presented.strip(), config.SERVICE_TOKEN)


def user_id(request: Request) -> str:
    """The dashboard user this request speaks for, or raise :class:`Unauthorized`."""
    if not _token_ok(request):
        raise Unauthorized("SERVICE_AUTH", "Music service token missing or wrong")
    raw = (request.headers.get(config.USER_HEADER) or "").strip()
    if not raw:
        raise Unauthorized("NO_IDENTITY", "No zer0space user on the request")
    # The value becomes a primary-key component in every per-user table, so it
    # is bounded here rather than trusted to be small. The dashboard sends a
    # numeric id; anything wildly longer is a caller doing something else.
    if len(raw) > 128:
        raise Unauthorized("NO_IDENTITY", "Malformed zer0space user id")
    return raw


def user_name(request: Request) -> str:
    """The display name, for greetings only. Never used as a key."""
    return (request.headers.get(config.USER_NAME_HEADER) or "").strip()[:128]


def base_path(request: Request) -> str:
    """The public path prefix this request arrived under.

    The gateway sends ``X-Forwarded-Prefix: /music``; every URL handed to the
    browser has to carry it or the player's own fetches land on the dashboard.
    This is the same failure that produced the Crimson grey-player bug, so the
    prefix is read from the request rather than assumed.
    """
    prefix = (request.headers.get("x-forwarded-prefix") or "").strip()
    if not prefix:
        return config.BASE_PATH
    return "/" + prefix.strip("/")


def public_origin(request: Request) -> str:
    """scheme://host as the browser sees us, from the gateway's forwarded headers."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0]
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    host = host.split(",")[0].strip()
    if not host:
        return ""
    return f"{proto.strip()}://{host}"
