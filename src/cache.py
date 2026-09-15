"""The catalogue cache.

Deezer is a public API with no key and a rate limit, so browse responses are
cached in PostgreSQL rather than fetched per page view. Two rules here are not
decoration — both are bugs this organisation has already shipped once, on the
Crimson backend, and spent a day diagnosing:

**Never cache an empty result.** One upstream blip would otherwise freeze an
empty home page in place for the whole TTL, across every replica, and it looks
exactly like broken scrapers rather than a cache entry.

**Cache the full pool, slice per caller.** Caching under a key that ignores the
caller's ``limit`` stores an already-truncated list, so whichever caller asked
first decides how many items everyone else sees until the entry expires.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from . import config, db


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get(key: str) -> Any | None:
    """The cached payload for ``key``, or ``None`` if absent or expired.

    A database outage is reported as a cache miss, not raised. The cache is an
    optimisation; browsing and playback must survive PostgreSQL being briefly
    away, and the alternative is a player that stops mid-playlist because a
    lookup table was unreachable.
    """
    try:
        row = await db.fetchrow(
            "SELECT payload FROM catalog_cache WHERE key = $1 AND expires_at > now()",
            key,
        )
    except db.DatabaseUnavailable:
        return None
    if row is None:
        return None
    payload = row["payload"]
    # The codec in db.py decodes jsonb for us, but a row written before it was
    # installed (or by hand in psql) can still arrive as text.
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    return payload.get("v") if isinstance(payload, dict) else None


async def put(key: str, value: Any, ttl: int | None = None) -> None:
    """Store ``value`` under ``key``. Empty values are dropped, not stored."""
    if value is None or (isinstance(value, (list, dict, str)) and len(value) == 0):
        return
    expires = _now() + timedelta(seconds=ttl or config.CATALOG_TTL)
    try:
        await db.execute(
            """
            INSERT INTO catalog_cache (key, payload, expires_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (key) DO UPDATE
                SET payload = EXCLUDED.payload,
                    expires_at = EXCLUDED.expires_at,
                    created_at = now()
            """,
            key,
            {"v": value},
            expires,
        )
    except db.DatabaseUnavailable:
        # Same reasoning as get(): failing to memoise is not a failed request.
        pass


async def through(
    key: str, producer: Callable[[], Awaitable[Any]], ttl: int | None = None
) -> Any:
    """Read ``key``, or call ``producer`` and cache whatever it returns.

    A producer that raises is *not* cached and the exception propagates — the
    caller decides whether a failed catalogue fetch is fatal or falls back.
    """
    hit = await get(key)
    if hit is not None:
        return hit
    value = await producer()
    await put(key, value, ttl)
    return value


async def sweep() -> int:
    """Delete expired rows. Called periodically from the lifespan task."""
    result = await db.execute("DELETE FROM catalog_cache WHERE expires_at <= now()")
    try:
        return int(result.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0
