"""From a track key to a playable URL, with the cache and the fallback around it.

The scraper itself lives in :mod:`src.providers.ytmusic`. This module is the
policy around it:

* remember the match and the URL in ``track_source``;
* reuse a stored match when only the URL has expired, so a re-resolve is one
  extraction instead of a search plus an extraction;
* collapse concurrent requests for the same track into a single extraction —
  without this, hitting play on a track that is already resolving starts a
  second scrape, and the queue prefetch makes that routine;
* fall back to the catalogue's 30-second preview rather than returning silence.

Nothing here talks to the browser. What the player receives is a signed ticket
(see :mod:`src.stream`), never one of these URLs — they are bound to this host's
IP and would 403 from anywhere else.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from . import cache, config, db
from .providers import deezer, ytmusic
from .providers.base import ResolvedSource, Track

# One in-flight extraction per track key, shared by every waiter.
_inflight: dict[str, asyncio.Task[ResolvedSource | None]] = {}

# After this many consecutive failures a track stops being retried on every
# play and is only re-attempted once its row ages out. Keeps a region-blocked
# song from costing a 45-second timeout every time a playlist reaches it.
MAX_FAILURES = 3
FAILURE_COOLDOWN = 3600


class UnknownTrack(LookupError):
    """The track key does not name anything this service can look up."""


async def track_for_key(key: str) -> Track:
    """The catalogue object behind a track key, cached.

    The player sends a key, not a track body, so this is on the path of every
    stream request — and on a cold cache a 12-second Deezer lookup would sit in
    front of the first note of every song.
    """
    provider, _, ident = key.partition(":")
    if provider != "deezer" or not ident.isdigit():
        raise UnknownTrack(key)

    cached = await cache.get(f"track:{key}")
    if cached:
        album = cached.get("album") or None
        return Track(
            key=key,
            title=cached["title"],
            artist=deezer._artist(cached.get("artist")),
            album=deezer._album(album) if album else None,
            duration=int(cached.get("duration") or 0),
            cover=cached.get("cover") or "",
            preview=cached.get("preview") or "",
            isrc=cached.get("isrc") or "",
        )

    track = await deezer.track(ident)
    if track is None:
        raise UnknownTrack(key)
    await cache.put(
        f"track:{key}",
        {
            "title": track.title,
            "artist": {
                "id": track.artist.id,
                "name": track.artist.name,
                "picture_medium": track.artist.picture,
            },
            "album": (
                {
                    "id": track.album.id,
                    "title": track.album.title,
                    "cover_big": track.album.cover,
                    "nb_tracks": track.album.track_count,
                }
                if track.album
                else None
            ),
            "duration": track.duration,
            "cover": track.cover,
            # The preview URL is part of what makes the fallback work, so it is
            # cached with the rest rather than re-fetched when the scraper fails.
            "preview": track.preview,
            "isrc": track.isrc,
        },
        ttl=7 * 24 * 3600,
    )
    return track


async def _stored(key: str) -> dict | None:
    """The remembered match for a track, or ``None``.

    Like the catalogue cache, a database outage reads as "nothing remembered"
    rather than raising: the scraper can still resolve the track from scratch,
    and a player that stops because a memo table was unreachable is a worse
    outcome than one that re-does the work.
    """
    try:
        row = await db.fetchrow(
            """
            SELECT provider, provider_id, source_url, mime, bitrate, duration,
                   expires_at, failures, updated_at
            FROM track_source WHERE track_key = $1
            """,
            key,
        )
    except db.DatabaseUnavailable:
        return None
    return dict(row) if row else None


async def _remember(key: str, source: ResolvedSource) -> None:
    try:
        await _remember_write(key, source)
    except db.DatabaseUnavailable:
        pass


async def _remember_write(key: str, source: ResolvedSource) -> None:
    await db.execute(
        """
        INSERT INTO track_source
            (track_key, provider, provider_id, source_url, mime, bitrate,
             duration, expires_at, failures, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 0, now())
        ON CONFLICT (track_key) DO UPDATE SET
            provider = EXCLUDED.provider,
            provider_id = EXCLUDED.provider_id,
            source_url = EXCLUDED.source_url,
            mime = EXCLUDED.mime,
            bitrate = EXCLUDED.bitrate,
            duration = EXCLUDED.duration,
            expires_at = EXCLUDED.expires_at,
            failures = 0,
            updated_at = now()
        """,
        key,
        source.provider,
        source.provider_id,
        source.url,
        source.mime,
        source.bitrate,
        source.duration,
        datetime.fromtimestamp(source.expires_at, tz=timezone.utc),
    )


async def _remember_failure(key: str, provider_id: str) -> None:
    """Count the failure but keep the match — the id is still probably right."""
    try:
        await _remember_failure_write(key, provider_id)
    except db.DatabaseUnavailable:
        pass


async def _remember_failure_write(key: str, provider_id: str) -> None:
    await db.execute(
        """
        INSERT INTO track_source (track_key, provider, provider_id, failures, updated_at)
        VALUES ($1, $2, $3, 1, now())
        ON CONFLICT (track_key) DO UPDATE SET
            failures = track_source.failures + 1,
            provider_id = COALESCE(NULLIF(EXCLUDED.provider_id, ''),
                                   track_source.provider_id),
            source_url = NULL,
            updated_at = now()
        """,
        key,
        ytmusic.name,
        provider_id,
    )


def _fresh(stored: dict | None) -> ResolvedSource | None:
    """A stored row that can be played as-is, if there is one."""
    if not stored or not stored.get("source_url"):
        return None
    expires = stored.get("expires_at")
    if not expires or expires <= datetime.now(timezone.utc):
        return None
    return ResolvedSource(
        url=stored["source_url"],
        provider=stored["provider"],
        provider_id=stored.get("provider_id") or "",
        mime=stored.get("mime") or "audio/mpeg",
        bitrate=int(stored.get("bitrate") or 0),
        duration=int(stored.get("duration") or 0),
        # Headers are not stored: they are derived from the configured
        # User-Agent, and a stale Referer is worse than none.
        headers={"User-Agent": config.USER_AGENT},
        expires_at=expires.timestamp(),
    )


def _preview(track: Track) -> ResolvedSource | None:
    if not (config.PREVIEW_FALLBACK and track.preview):
        return None
    return ResolvedSource(
        url=track.preview,
        provider="preview",
        provider_id=track.key,
        mime="audio/mpeg",
        duration=30,
        headers={"User-Agent": config.USER_AGENT},
        # Deezer's preview CDN links are long-lived; an hour is plenty and keeps
        # the ticket from outliving a scraper that has since started working.
        expires_at=time.time() + 3600,
    )


async def _resolve_uncached(track: Track, stored: dict | None) -> ResolvedSource | None:
    source = await ytmusic.resolve(track, provider_id=(stored or {}).get("provider_id") or "")
    if source is None and stored and stored.get("provider_id"):
        # The remembered match failed. It may be the id that went bad (video
        # taken down) rather than the extraction, so try once more from scratch.
        source = await ytmusic.resolve(track, provider_id="")
    if source is None:
        await _remember_failure(track.key, (stored or {}).get("provider_id") or "")
        return None
    await _remember(track.key, source)
    return source


async def source_for(track: Track) -> ResolvedSource | None:
    """The playable source for ``track`` — cached, deduplicated, with fallback."""
    stored = await _stored(track.key)

    fresh = _fresh(stored)
    if fresh is not None:
        return fresh

    if stored and stored.get("failures", 0) >= MAX_FAILURES:
        updated = stored.get("updated_at")
        cooling = (
            updated is not None
            and (datetime.now(timezone.utc) - updated).total_seconds() < FAILURE_COOLDOWN
        )
        if cooling:
            return _preview(track)

    # Collapse concurrent resolves of the same track into one extraction.
    task = _inflight.get(track.key)
    if task is None:
        task = asyncio.create_task(_resolve_uncached(track, stored))
        _inflight[track.key] = task
        task.add_done_callback(lambda _t, key=track.key: _inflight.pop(key, None))

    try:
        source = await asyncio.shield(task)
    except Exception as err:  # noqa: BLE001
        print(f"[music] resolve failed for {track.key}: {err!r}")
        source = None
    return source or _preview(track)


async def prefetch(keys: list[str]) -> None:
    """Warm the next few tracks in the queue, best effort and never blocking.

    Gapless-ish playback needs the next track's URL ready before the current one
    ends; a cold resolve is several seconds. Failures are swallowed — this is an
    optimisation, and the real request will report any problem properly.
    """
    for key in keys[:3]:
        try:
            track = await track_for_key(key)
        except (UnknownTrack, Exception):  # noqa: BLE001
            continue
        stored = await _stored(key)
        if _fresh(stored) is not None:
            continue
        asyncio.create_task(_swallow(source_for(track)))


async def _swallow(coro) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001
        pass
