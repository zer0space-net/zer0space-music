"""Per-user state: likes, playlists, history, playback position, preferences.

Every function takes the ``user_id`` the gateway forwarded and scopes its query
to it. There is no "current user" global and no row is ever addressed by id
alone — a playlist is always ``WHERE id = $1 AND user_id = $2``, so a guessed id
belonging to someone else returns nothing instead of their playlist.

Track bodies are stored denormalised as JSONB alongside the key. That is
deliberate: a playlist has to keep rendering when Deezer is unreachable, and a
liked song should not vanish from the UI because a catalogue lookup timed out.
"""

from __future__ import annotations

from typing import Any

from . import db

MAX_PLAYLISTS = 500
MAX_PLAYLIST_TRACKS = 2000
MAX_NAME = 120
MAX_DESCRIPTION = 500


class LibraryError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _track_key(track: dict[str, Any]) -> str:
    key = str(track.get("key") or "").strip()
    if not key or len(key) > 200:
        raise LibraryError("BAD_TRACK", "Track is missing a usable key")
    return key


def _slim(track: dict[str, Any]) -> dict[str, Any]:
    """Only the fields the UI renders. The client sends whatever it has; storing
    it verbatim would let a caller write unbounded JSON into our table."""
    album = track.get("album") or None
    artist = track.get("artist") or {}
    return {
        "key": _track_key(track),
        "title": str(track.get("title") or "")[:300],
        "artist": {
            "id": str(artist.get("id") or "")[:64],
            "name": str(artist.get("name") or "")[:300],
            "picture": str(artist.get("picture") or "")[:500],
        },
        "album": (
            {
                "id": str(album.get("id") or "")[:64],
                "title": str(album.get("title") or "")[:300],
                "cover": str(album.get("cover") or "")[:500],
            }
            if isinstance(album, dict)
            else None
        ),
        "duration": max(0, min(int(track.get("duration") or 0), 24 * 3600)),
        "cover": str(track.get("cover") or "")[:500],
        "explicit": bool(track.get("explicit")),
    }


# --- Likes ------------------------------------------------------------------


async def liked(user_id: str, limit: int = 500) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT track FROM liked_track
        WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2
        """,
        user_id,
        limit,
    )
    return [row["track"] for row in rows]


async def liked_keys(user_id: str) -> list[str]:
    """Just the keys — what the UI needs to fill in every heart icon at once."""
    rows = await db.fetch("SELECT track_key FROM liked_track WHERE user_id = $1", user_id)
    return [row["track_key"] for row in rows]


async def like(user_id: str, track: dict[str, Any]) -> None:
    slim = _slim(track)
    await db.execute(
        """
        INSERT INTO liked_track (user_id, track_key, track) VALUES ($1, $2, $3)
        ON CONFLICT (user_id, track_key) DO UPDATE SET track = EXCLUDED.track
        """,
        user_id,
        slim["key"],
        slim,
    )


async def unlike(user_id: str, track_key: str) -> None:
    await db.execute(
        "DELETE FROM liked_track WHERE user_id = $1 AND track_key = $2", user_id, track_key
    )


# --- Playlists --------------------------------------------------------------


async def playlists(user_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT p.id, p.name, p.description, p.cover, p.updated_at,
               COUNT(t.track_key)::int AS track_count,
               -- The first track's cover stands in when the playlist has no
               -- picture of its own, so the library grid is never a wall of
               -- empty squares.
               (SELECT t2.track->>'cover' FROM playlist_track t2
                 WHERE t2.playlist_id = p.id ORDER BY t2.position LIMIT 1) AS first_cover
        FROM playlist p
        LEFT JOIN playlist_track t ON t.playlist_id = p.id
        WHERE p.user_id = $1
        GROUP BY p.id
        ORDER BY p.updated_at DESC
        """,
        user_id,
    )
    return [
        {
            "id": str(row["id"]),
            "name": row["name"],
            "description": row["description"],
            "cover": row["cover"] or row["first_cover"] or "",
            "trackCount": row["track_count"],
            "updatedAt": row["updated_at"].isoformat(),
        }
        for row in rows
    ]


async def playlist(user_id: str, playlist_id: str) -> dict[str, Any]:
    row = await db.fetchrow(
        "SELECT id, name, description, cover, updated_at FROM playlist WHERE id = $1 AND user_id = $2",
        _pid(playlist_id),
        user_id,
    )
    if row is None:
        raise LibraryError("NOT_FOUND", "Playlist not found")
    tracks = await db.fetch(
        "SELECT track FROM playlist_track WHERE playlist_id = $1 ORDER BY position",
        row["id"],
    )
    items = [t["track"] for t in tracks]
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "cover": row["cover"] or (items[0].get("cover") if items else "") or "",
        "trackCount": len(items),
        "updatedAt": row["updated_at"].isoformat(),
        "tracks": items,
    }


def _pid(raw: str) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        raise LibraryError("NOT_FOUND", "Playlist not found") from None


async def create_playlist(user_id: str, name: str, description: str = "") -> dict[str, Any]:
    name = (name or "").strip()[:MAX_NAME]
    if not name:
        raise LibraryError("BAD_NAME", "A playlist needs a name")
    count = await db.fetchval("SELECT COUNT(*) FROM playlist WHERE user_id = $1", user_id)
    if count >= MAX_PLAYLISTS:
        raise LibraryError("TOO_MANY", "Playlist limit reached")
    row = await db.fetchrow(
        """
        INSERT INTO playlist (user_id, name, description) VALUES ($1, $2, $3)
        RETURNING id, name, description, updated_at
        """,
        user_id,
        name,
        (description or "").strip()[:MAX_DESCRIPTION],
    )
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "description": row["description"],
        "cover": "",
        "trackCount": 0,
        "updatedAt": row["updated_at"].isoformat(),
    }


async def rename_playlist(user_id: str, playlist_id: str, name: str, description: str) -> None:
    name = (name or "").strip()[:MAX_NAME]
    if not name:
        raise LibraryError("BAD_NAME", "A playlist needs a name")
    result = await db.execute(
        """
        UPDATE playlist SET name = $3, description = $4, updated_at = now()
        WHERE id = $1 AND user_id = $2
        """,
        _pid(playlist_id),
        user_id,
        name,
        (description or "").strip()[:MAX_DESCRIPTION],
    )
    if result.endswith(" 0"):
        raise LibraryError("NOT_FOUND", "Playlist not found")


async def delete_playlist(user_id: str, playlist_id: str) -> None:
    result = await db.execute(
        "DELETE FROM playlist WHERE id = $1 AND user_id = $2", _pid(playlist_id), user_id
    )
    if result.endswith(" 0"):
        raise LibraryError("NOT_FOUND", "Playlist not found")


async def _own(user_id: str, playlist_id: str) -> int:
    pid = await db.fetchval(
        "SELECT id FROM playlist WHERE id = $1 AND user_id = $2", _pid(playlist_id), user_id
    )
    if pid is None:
        raise LibraryError("NOT_FOUND", "Playlist not found")
    return int(pid)


async def add_tracks(user_id: str, playlist_id: str, tracks: list[dict[str, Any]]) -> int:
    pid = await _own(user_id, playlist_id)
    existing = await db.fetchval(
        "SELECT COUNT(*) FROM playlist_track WHERE playlist_id = $1", pid
    )
    if existing + len(tracks) > MAX_PLAYLIST_TRACKS:
        raise LibraryError("TOO_MANY", "Playlist is full")
    position = await db.fetchval(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM playlist_track WHERE playlist_id = $1",
        pid,
    )
    rows = []
    for offset, track in enumerate(tracks):
        slim = _slim(track)
        rows.append((pid, slim["key"], slim, position + offset))
    await db.executemany(
        """
        INSERT INTO playlist_track (playlist_id, track_key, track, position)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (playlist_id, track_key) DO NOTHING
        """,
        rows,
    )
    await db.execute("UPDATE playlist SET updated_at = now() WHERE id = $1", pid)
    return len(rows)


async def remove_track(user_id: str, playlist_id: str, track_key: str) -> None:
    pid = await _own(user_id, playlist_id)
    await db.execute(
        "DELETE FROM playlist_track WHERE playlist_id = $1 AND track_key = $2", pid, track_key
    )
    await db.execute("UPDATE playlist SET updated_at = now() WHERE id = $1", pid)


async def reorder(user_id: str, playlist_id: str, keys: list[str]) -> None:
    """Rewrite positions from the order the client sent.

    Only keys already in the playlist move; anything else in the list is
    ignored, so a stale client cannot insert a track through this path.
    """
    pid = await _own(user_id, playlist_id)
    await db.executemany(
        "UPDATE playlist_track SET position = $3 WHERE playlist_id = $1 AND track_key = $2",
        [(pid, key, index) for index, key in enumerate(keys[:MAX_PLAYLIST_TRACKS])],
    )
    await db.execute("UPDATE playlist SET updated_at = now() WHERE id = $1", pid)


# --- History and state ------------------------------------------------------


async def record_play(user_id: str, track: dict[str, Any], seconds: int = 0) -> None:
    slim = _slim(track)
    await db.execute(
        """
        INSERT INTO play_history (user_id, track_key, track, seconds)
        VALUES ($1, $2, $3, $4)
        """,
        user_id,
        slim["key"],
        slim,
        max(0, min(int(seconds or 0), 24 * 3600)),
    )


async def recent(user_id: str, limit: int = 30) -> list[dict[str, Any]]:
    """Most recent distinct tracks. ``DISTINCT ON`` rather than ``GROUP BY`` so
    the full JSONB body comes along without being an aggregate."""
    rows = await db.fetch(
        """
        SELECT DISTINCT ON (track_key) track, played_at
        FROM play_history WHERE user_id = $1
        ORDER BY track_key, played_at DESC
        """,
        user_id,
    )
    ordered = sorted(rows, key=lambda row: row["played_at"], reverse=True)
    return [row["track"] for row in ordered[:limit]]


async def top_artists(user_id: str, limit: int = 12) -> list[dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT track->'artist'->>'name' AS name,
               track->'artist'->>'id'   AS id,
               MAX(track->'artist'->>'picture') AS picture,
               COUNT(*)::int AS plays
        FROM play_history
        WHERE user_id = $1 AND track->'artist'->>'name' IS NOT NULL
        GROUP BY 1, 2
        ORDER BY plays DESC
        LIMIT $2
        """,
        user_id,
        limit,
    )
    return [
        {"id": r["id"] or "", "name": r["name"], "picture": r["picture"] or "", "plays": r["plays"]}
        for r in rows
    ]


async def save_state(user_id: str, state: dict[str, Any]) -> None:
    """Remember what is playing, so the phone can pick up where the desktop left off."""
    queue = state.get("queue") or []
    trimmed = {
        "track": _slim(state["track"]) if isinstance(state.get("track"), dict) else None,
        "position": max(0.0, min(float(state.get("position") or 0), 24 * 3600)),
        "queue": [_slim(t) for t in queue[:200] if isinstance(t, dict)],
        "index": max(0, min(int(state.get("index") or 0), 200)),
        "context": str(state.get("context") or "")[:200],
    }
    await db.execute(
        """
        INSERT INTO player_state (user_id, state) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state, updated_at = now()
        """,
        user_id,
        trimmed,
    )


async def load_state(user_id: str) -> dict[str, Any] | None:
    row = await db.fetchrow("SELECT state FROM player_state WHERE user_id = $1", user_id)
    return row["state"] if row else None


DEFAULT_PREFS = {"volume": 0.9, "repeat": "off", "shuffle": False, "crossfade": 0}


async def prefs(user_id: str) -> dict[str, Any]:
    row = await db.fetchrow("SELECT prefs FROM user_prefs WHERE user_id = $1", user_id)
    return {**DEFAULT_PREFS, **(row["prefs"] if row else {})}


async def save_prefs(user_id: str, values: dict[str, Any]) -> dict[str, Any]:
    merged = await prefs(user_id)
    if "volume" in values:
        merged["volume"] = max(0.0, min(float(values["volume"]), 1.0))
    if values.get("repeat") in ("off", "all", "one"):
        merged["repeat"] = values["repeat"]
    if "shuffle" in values:
        merged["shuffle"] = bool(values["shuffle"])
    if "crossfade" in values:
        merged["crossfade"] = max(0, min(int(values["crossfade"]), 12))
    await db.execute(
        """
        INSERT INTO user_prefs (user_id, prefs) VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE SET prefs = EXCLUDED.prefs, updated_at = now()
        """,
        user_id,
        merged,
    )
    return merged
