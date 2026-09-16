"""Podcast subscriptions and episode listing — the application layer over
:mod:`src.providers.podcast`, same split as ``library.py`` sits over the
catalogue providers.

Episodes are never stored: a feed is re-fetched (through :mod:`src.cache`,
so "opened the app three times today" is one request, not three) rather than
mirrored into Postgres, because the feed itself is the source of truth and a
copy would drift the moment an episode's title or audio URL changed upstream.

A track key here is ``podcast:<subscription id>:<guid hash>`` — the same
``"<provider>:<id>"`` shape every other track key in this app uses, which is
what lets /api/stream and /media branch on the prefix instead of needing a
parallel API surface for playback (see the ``track_key.startswith("podcast:")``
checks in main.py).
"""

from __future__ import annotations

import time
from typing import Any

from . import cache, config, db
from .providers import podcast
from .providers.base import ResolvedSource

MAX_SUBSCRIPTIONS = 50
# An hour: daily shows publish at most once a day, and re-parsing a feed on
# every "open the podcasts tab" would be a lot of unnecessary XML parsing for
# something that changes this rarely.
EPISODE_CACHE_TTL = 3600


class PodcastError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def close() -> None:
    """Closes providers/podcast.py's own outbound client. Called from
    main.py's shutdown alongside deezer.close()/stream.close()."""
    await podcast.close()


def _sid(raw: str) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise PodcastError("NOT_FOUND", "Subscription not found") from None


def _row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "feedUrl": row["feed_url"],
        "title": row["title"],
        # Not the raw feed-host URL — main.py fills in the relayed
        # /api/podcasts/{id}/cover address, or leaves this false. See
        # fetch_image's docstring in providers/podcast.py for why.
        "hasCover": bool(row["cover"]),
        "addedAt": row["added_at"].isoformat(),
    }


async def subscriptions(user_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch(
        "SELECT id, feed_url, title, cover, added_at FROM podcast_subscription "
        "WHERE user_id = $1 ORDER BY added_at DESC",
        user_id,
    )
    return [_row(row) for row in rows]


async def subscribe(user_id: str, feed_url: str) -> dict[str, Any]:
    feed_url = (feed_url or "").strip()
    if not feed_url.startswith(("http://", "https://")):
        raise PodcastError("PODCAST_BAD_URL", "That does not look like a feed URL")
    count = await db.fetchval(
        "SELECT COUNT(*) FROM podcast_subscription WHERE user_id = $1", user_id
    )
    if count >= MAX_SUBSCRIPTIONS:
        raise PodcastError("TOO_MANY", "Podcast subscription limit reached")

    try:
        feed = await podcast.fetch_feed(feed_url)
    except podcast.FeedUnavailable as err:
        raise PodcastError("PODCAST_UNAVAILABLE", str(err)) from err

    row = await db.fetchrow(
        """
        INSERT INTO podcast_subscription (user_id, feed_url, title, cover)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (user_id, feed_url) DO UPDATE
            SET title = EXCLUDED.title, cover = EXCLUDED.cover
        RETURNING id, feed_url, title, cover, added_at
        """,
        user_id,
        feed_url,
        feed.title,
        feed.cover,
    )
    return _row(row)


async def unsubscribe(user_id: str, subscription_id: str) -> None:
    result = await db.execute(
        "DELETE FROM podcast_subscription WHERE id = $1 AND user_id = $2",
        _sid(subscription_id),
        user_id,
    )
    if result.endswith(" 0"):
        raise PodcastError("NOT_FOUND", "Subscription not found")


async def _owned(user_id: str, subscription_id: str) -> dict[str, Any]:
    row = await db.fetchrow(
        "SELECT id, feed_url, title, cover FROM podcast_subscription WHERE id = $1 AND user_id = $2",
        _sid(subscription_id),
        user_id,
    )
    if row is None:
        raise PodcastError("NOT_FOUND", "Subscription not found")
    return dict(row)


async def _cached_episodes(subscription_id: int, feed_url: str) -> list[dict[str, Any]]:
    """The full internal shape, audio_url included — episodes() (client-
    facing) strips it out; resolve_episode() (the stream relay) is the only
    caller that needs it. One cache entry serves both."""

    async def _produce() -> list[dict[str, Any]]:
        feed = await podcast.fetch_feed(feed_url)
        return [
            {
                "guidHash": ep.guid_hash,
                "title": ep.title,
                "description": ep.description,
                "published": ep.published,
                "duration": ep.duration,
                "cover": ep.cover,
                "audioUrl": ep.audio_url,
                "mime": ep.mime,
            }
            for ep in feed.episodes
        ]

    return await cache.through(f"podcast-episodes:{subscription_id}", _produce, ttl=EPISODE_CACHE_TTL)


async def episodes(user_id: str, subscription_id: str) -> dict[str, Any]:
    sub = await _owned(user_id, subscription_id)
    try:
        eps = await _cached_episodes(sub["id"], sub["feed_url"])
    except podcast.FeedUnavailable as err:
        raise PodcastError("PODCAST_UNAVAILABLE", str(err)) from err
    return {
        "id": str(sub["id"]),
        "title": sub["title"],
        # No per-episode cover here on purpose: relaying one image per
        # subscription (main.py fills in the actual URL — see
        # api_podcast_cover) is already the workaround CSP forces for an
        # arbitrary feed host; relaying a second, per-episode image URl per
        # feed host would double that cost for art most shows do not
        # actually vary per episode.
        "hasCover": bool(sub["cover"]),
        "episodes": [
            {
                "key": f"podcast:{sub['id']}:{ep['guidHash']}",
                "title": ep["title"],
                "description": ep["description"],
                "published": ep["published"],
                "duration": ep["duration"],
            }
            for ep in eps
        ],
    }


async def cover_bytes(user_id: str, subscription_id: str) -> tuple[bytes, str] | None:
    """The subscription's own cover art, ownership-checked — never an
    arbitrary URL a client could pass in, which is what keeps this relay
    from being an open image-fetching proxy (SSRF via a "cover" parameter)."""
    try:
        sub = await _owned(user_id, subscription_id)
    except PodcastError:
        return None
    return await podcast.fetch_image(sub["cover"])


async def resolve_episode(user_id: str, track_key: str) -> ResolvedSource | None:
    """The ``ResolvedSource`` for a ``podcast:<sub id>:<guid hash>`` key, or
    ``None`` if the subscription is gone or the episode fell out of the
    feed's current window. Same shape resolve.py hands stream.relay() for a
    Deezer/YouTube track, so main.py's /media route needs only one relay
    code path for both."""
    _, sub_id, guid_hash = (track_key.split(":", 2) + ["", ""])[:3]
    try:
        sub = await _owned(user_id, sub_id)
    except PodcastError:
        return None
    try:
        eps = await _cached_episodes(sub["id"], sub["feed_url"])
    except podcast.FeedUnavailable:
        return None
    match = next((ep for ep in eps if ep["guidHash"] == guid_hash), None)
    if match is None:
        return None
    return ResolvedSource(
        url=match["audioUrl"],
        provider="podcast",
        provider_id=guid_hash,
        mime=match["mime"] or "audio/mpeg",
        duration=match["duration"],
        headers={"User-Agent": config.USER_AGENT},
        # Not IP-bound like a resolved YouTube URL, so this is generous —
        # only long enough that a stale ticket does not linger forever.
        expires_at=time.time() + 6 * 3600,
    )
