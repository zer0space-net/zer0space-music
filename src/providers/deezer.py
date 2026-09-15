"""Deezer's public API — the catalogue.

Why Deezer and not Spotify: Spotify's Web API needs a registered application
and a client id/secret, i.e. an account. Deezer's ``api.deezer.com`` needs
nothing at all, and returns everything the UI wants — search across tracks,
albums, artists and playlists, cover art in four sizes, editorial charts, an
artist's top tracks and related artists, plus an ISRC per track, which is what
makes a confident match on the audio side possible.

What it does **not** give is playable audio. The ``preview`` field is a 30
second mp3 and nothing more; the full stream comes from the scraper. That split
is the whole architecture — see docs/architecture.md.

Everything here is read-only and unauthenticated, so there is no secret in this
module and nothing to rotate.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .. import config
from .base import Album, Artist, Track

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=config.DEEZER_API,
            timeout=httpx.Timeout(connect=5.0, read=12.0, write=10.0, pool=5.0),
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


class DeezerUnavailable(RuntimeError):
    """Deezer could not be reached or answered with an error envelope."""


async def _get(path: str, **params: Any) -> dict[str, Any]:
    """One GET, with Deezer's in-band error envelope turned into an exception.

    Deezer answers ``200 {"error": {...}}`` rather than an HTTP error code, so a
    plain ``raise_for_status`` would let a quota message through as if it were
    data — and it would then be cached as a valid empty result.
    """
    try:
        response = await client().get(path, params={k: v for k, v in params.items() if v is not None})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as err:
        raise DeezerUnavailable(f"{path}: {err}") from err
    if isinstance(payload, dict) and payload.get("error"):
        raise DeezerUnavailable(f"{path}: {payload['error']}")
    return payload if isinstance(payload, dict) else {"data": payload}


# --- Mapping ----------------------------------------------------------------
# Deezer's JSON is not stable in shape: a track carries a full `artist` object
# in one endpoint and a bare name in another, and `album` is missing entirely on
# an album's own tracklist (it is the album you asked for). Every accessor here
# therefore tolerates the field being absent.


def _artist(raw: dict[str, Any] | None, fallback_name: str = "") -> Artist:
    raw = raw or {}
    return Artist(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or fallback_name or "Unknown artist"),
        picture=str(raw.get("picture_medium") or raw.get("picture") or ""),
    )


def _album(raw: dict[str, Any] | None, artist: Artist | None = None) -> Album | None:
    if not raw:
        return None
    release = str(raw.get("release_date") or "")
    return Album(
        id=str(raw.get("id") or ""),
        title=str(raw.get("title") or ""),
        cover=str(raw.get("cover_big") or raw.get("cover_medium") or raw.get("cover") or ""),
        artist=_artist(raw.get("artist")) if raw.get("artist") else artist,
        year=release[:4],
        track_count=int(raw.get("nb_tracks") or 0),
    )


def to_track(raw: dict[str, Any], album: Album | None = None) -> Track | None:
    """Map one Deezer track object. ``None`` for anything unplayable."""
    track_id = raw.get("id")
    title = str(raw.get("title_short") or raw.get("title") or "").strip()
    if not track_id or not title:
        return None
    artist = _artist(raw.get("artist"), str(raw.get("artist_name") or ""))
    own_album = _album(raw.get("album"), artist) or album
    cover = ""
    if own_album:
        cover = own_album.cover
    return Track(
        key=f"deezer:{track_id}",
        title=title,
        artist=artist,
        album=own_album,
        duration=int(raw.get("duration") or 0),
        cover=cover,
        preview=str(raw.get("preview") or ""),
        explicit=bool(raw.get("explicit_lyrics")),
        isrc=str(raw.get("isrc") or ""),
    )


def _tracks(payload: dict[str, Any], album: Album | None = None) -> list[dict[str, Any]]:
    out = []
    for raw in payload.get("data") or []:
        track = to_track(raw, album)
        if track is not None:
            out.append(track.as_dict())
    return out


# --- Endpoints --------------------------------------------------------------


async def search(query: str, limit: int = 40) -> dict[str, list[dict[str, Any]]]:
    """Search everything at once.

    Four parallel requests rather than Deezer's combined endpoint, because the
    combined one only returns tracks and the UI wants artist and album rows too
    (that is what makes a search result look like Spotify's rather than a list).
    """
    limit = max(1, min(limit, config.SEARCH_LIMIT))
    tracks, albums, artists, playlists = await asyncio.gather(
        _get("/search/track", q=query, limit=limit),
        _get("/search/album", q=query, limit=min(limit, 20)),
        _get("/search/artist", q=query, limit=min(limit, 20)),
        _get("/search/playlist", q=query, limit=min(limit, 20)),
        return_exceptions=True,
    )

    def ok(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {"data": []}

    # A partial search is far better than none: if the album lane errors, the
    # track lane still renders. Only a total failure is escalated.
    results = {
        "tracks": _tracks(ok(tracks)),
        "albums": [a.as_dict() for a in (_album(r) for r in ok(albums).get("data") or []) if a],
        "artists": [_artist(r).as_dict() for r in ok(artists).get("data") or []],
        "playlists": [_playlist_stub(r) for r in ok(playlists).get("data") or []],
    }
    if not any(results.values()) and isinstance(tracks, Exception):
        raise DeezerUnavailable(str(tracks))
    return results


def _playlist_stub(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(raw.get("id") or ""),
        "title": str(raw.get("title") or ""),
        "cover": str(raw.get("picture_big") or raw.get("picture_medium") or ""),
        "trackCount": int(raw.get("nb_tracks") or 0),
        "creator": str((raw.get("user") or {}).get("name") or ""),
        "source": "deezer",
    }


async def charts(limit: int = 30) -> dict[str, Any]:
    """The editorial home page: top tracks, albums, artists and playlists."""
    payload = await _get("/chart", limit=limit)
    return {
        "tracks": _tracks(payload.get("tracks") or {}),
        "albums": [
            a.as_dict()
            for a in (_album(r) for r in (payload.get("albums") or {}).get("data") or [])
            if a
        ],
        "artists": [
            _artist(r).as_dict() for r in (payload.get("artists") or {}).get("data") or []
        ],
        "playlists": [
            _playlist_stub(r) for r in (payload.get("playlists") or {}).get("data") or []
        ],
    }


async def genres() -> list[dict[str, Any]]:
    payload = await _get("/genre")
    return [
        {
            "id": str(r.get("id") or ""),
            "name": str(r.get("name") or ""),
            "picture": str(r.get("picture_medium") or ""),
        }
        for r in payload.get("data") or []
        if str(r.get("id")) != "0"  # "All", which has no browsable content
    ]


async def album(album_id: str) -> dict[str, Any]:
    payload = await _get(f"/album/{album_id}")
    mapped = _album(payload)
    if mapped is None:
        raise DeezerUnavailable(f"album {album_id} has no usable body")
    return {
        **mapped.as_dict(),
        "genres": [g.get("name") for g in (payload.get("genres") or {}).get("data") or []],
        "tracks": _tracks(payload.get("tracks") or {}, mapped),
    }


async def artist(artist_id: str) -> dict[str, Any]:
    info, top, albums, related = await asyncio.gather(
        _get(f"/artist/{artist_id}"),
        _get(f"/artist/{artist_id}/top", limit=25),
        _get(f"/artist/{artist_id}/albums", limit=50),
        _get(f"/artist/{artist_id}/related", limit=12),
        return_exceptions=True,
    )
    if isinstance(info, Exception):
        raise DeezerUnavailable(str(info))

    def ok(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {"data": []}

    mapped = _artist(info)
    return {
        **mapped.as_dict(),
        "picture": str(info.get("picture_xl") or info.get("picture_big") or mapped.picture),
        "fans": int(info.get("nb_fan") or 0),
        "topTracks": _tracks(ok(top)),
        "albums": [
            a.as_dict() for a in (_album(r, mapped) for r in ok(albums).get("data") or []) if a
        ],
        "related": [_artist(r).as_dict() for r in ok(related).get("data") or []],
    }


async def playlist(playlist_id: str, limit: int = 200) -> dict[str, Any]:
    payload = await _get(f"/playlist/{playlist_id}", limit=limit)
    return {
        **_playlist_stub(payload),
        "description": str(payload.get("description") or ""),
        "tracks": _tracks(payload.get("tracks") or {}),
    }


async def genre_artists(genre_id: str, limit: int = 40) -> list[dict[str, Any]]:
    payload = await _get(f"/genre/{genre_id}/artists", limit=limit)
    return [_artist(r).as_dict() for r in payload.get("data") or []]


async def track(track_id: str) -> Track | None:
    """One track, as a domain object — used by the resolver, not by the UI."""
    payload = await _get(f"/track/{track_id}")
    return to_track(payload)
