"""Importing a Spotify playlist as one of our own.

Spotify names the tracks; it has no idea what we can actually play. Every
title Spotify's embed page hands back (see ``providers/spotify.py``) is
matched against our own catalogue with the same shape of scorer
``providers/ytmusic.py`` uses to match a Deezer track against a YouTube
upload — additive, duration-led, readable. A track that scores at or below
zero against every candidate is reported as unmatched rather than guessed at:
better an honest gap in the imported playlist than the wrong song sitting in
it forever.

Matching runs concurrently (bounded by a semaphore, not one giant
``asyncio.gather`` — Deezer's search endpoint is unauthenticated and shared
with everyone else browsing at the same time) but the *result* is put back in
the Spotify playlist's original order — the tasks do not finish in that
order, so this is not free, but a shuffled import would be a worse bug than a
slow one.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from . import library
from .providers import deezer, spotify


# Eight is comfortably inside "one request at a time is polite" for a public,
# unauthenticated API while still turning a 100-track playlist around in a
# handful of seconds rather than one search at a time.
MATCH_CONCURRENCY = 8
# Wider than ytmusic.py's 8s: that one matches a catalogue duration against a
# YouTube upload of the *same* master; this matches Spotify against Deezer,
# two different services that not infrequently index a different edit of the
# same song (radio edit vs. album version, a few seconds of a fade trimmed
# differently) as "the" track. Too tight here turns a right match into a
# false negative — see _best_match's fallback pass for the other half of that
# fix.
DURATION_TOLERANCE = 10
CANDIDATE_LIMIT = 12
# How many unmatched titles to hand back to the client. The playlist import
# response is not the place for a 100-line report; enough to spot-check.
MAX_UNMATCHED_REPORTED = 100


async def close() -> None:
    """Closes this module's own outbound client. Called from main.py's
    shutdown alongside deezer.close()/stream.close(), so callers here never
    need to know spotify.py keeps an httpx.AsyncClient of its own."""
    await spotify.close()


def _normalise(text: str) -> set[str]:
    cleaned = re.sub(r"[^\w\s]+", " ", (text or "").lower(), flags=re.UNICODE)
    return {token for token in cleaned.split() if len(token) > 1}


def _score(candidate: dict[str, Any], title: str, artist: str, duration: int) -> float:
    """How well one Deezer search result matches the Spotify track we asked for.

    Deezer's own fields are structured (a real ``artist.name``, an exact
    ``duration``), unlike a YouTube upload's noisy title — so title and artist
    overlap both carry full weight here rather than the artist being a
    fallback signal the way it is in ``ytmusic._score``.
    """
    score = 0.0
    candidate_duration = int(candidate.get("duration") or 0)
    if duration and candidate_duration:
        delta = abs(candidate_duration - duration)
        if delta <= 2:
            score += 6.0
        elif delta <= DURATION_TOLERANCE:
            score += 4.0 - (delta / DURATION_TOLERANCE)
        elif delta <= 30:
            score += 0.5
        else:
            # Still a penalty, not a hard reject — a very good title+artist
            # match against a genuinely different edit is better kept than
            # dropped; _best_match's own threshold is what filters the rest.
            score -= 2.5

    wanted_title = _normalise(title)
    got_title = _normalise(str(candidate.get("title") or ""))
    if wanted_title:
        score += 3.0 * (len(wanted_title & got_title) / len(wanted_title))

    wanted_artist = _normalise(artist)
    got_artist = _normalise(str((candidate.get("artist") or {}).get("name") or ""))
    if wanted_artist:
        score += 3.0 * (len(wanted_artist & got_artist) / len(wanted_artist))

    return score


async def _search(query: str) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    try:
        return await deezer.search_tracks(query, limit=CANDIDATE_LIMIT)
    except deezer.DeezerUnavailable:
        return []


async def _best_match(track: spotify.SpotifyTrack) -> dict[str, Any] | None:
    """Two passes, not one — "artist title" is the better query when Deezer
    has the track under the artist name Spotify gave, but a mismatched artist
    string (a different feat. order, "&" vs "and", a collab credited
    differently between the two services) can make that query miss something
    a plain title search would still find. Both candidate pools feed the same
    scorer, so a bad title-only match still needs the artist signal to win.
    """
    candidates = await _search(f"{track.artist} {track.title}".strip())
    if not candidates:
        candidates = await _search(track.title)
    elif max(_score(c, track.title, track.artist, track.duration) for c in candidates) <= 0:
        candidates = candidates + await _search(track.title)
    if not candidates:
        return None
    best = max(candidates, key=lambda c: _score(c, track.title, track.artist, track.duration))
    return best if _score(best, track.title, track.artist, track.duration) > 0 else None


async def import_spotify_playlist(user_id: str, url: str) -> dict[str, Any]:
    playlist_id = spotify.extract_playlist_id(url)
    if not playlist_id:
        raise library.LibraryError("SPOTIFY_BAD_URL", "That does not look like a Spotify playlist link")

    try:
        source = await spotify.fetch_playlist(playlist_id)
    except spotify.SpotifyUnavailable as err:
        raise library.LibraryError("SPOTIFY_UNAVAILABLE", str(err)) from err
    if not source.tracks:
        raise library.LibraryError("SPOTIFY_EMPTY", "That playlist has no tracks to import")

    semaphore = asyncio.Semaphore(MATCH_CONCURRENCY)

    async def resolve_one(index: int, track: spotify.SpotifyTrack) -> tuple[int, dict[str, Any] | None, spotify.SpotifyTrack]:
        async with semaphore:
            return index, await _best_match(track), track

    results = await asyncio.gather(*(resolve_one(i, t) for i, t in enumerate(source.tracks)))
    results.sort(key=lambda r: r[0])  # tasks finish out of order; the playlist must not

    matched = [candidate for _, candidate, _ in results if candidate is not None]
    unmatched = [
        {"title": track.title, "artist": track.artist}
        for _, candidate, track in results
        if candidate is None
    ]

    if not matched:
        raise library.LibraryError("SPOTIFY_NO_MATCHES", "None of these songs were found in the catalogue")

    description = f"Importiert von Spotify ({source.owner})" if source.owner else "Importiert von Spotify"
    playlist = await library.create_playlist(user_id, source.name, description)
    added = await library.add_tracks(user_id, playlist["id"], matched)
    playlist["trackCount"] = added

    return {
        "playlist": playlist,
        "matched": len(matched),
        "total": len(source.tracks),
        "unmatched": unmatched[:MAX_UNMATCHED_REPORTED],
        "truncated": source.truncated,
    }
