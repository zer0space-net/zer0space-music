"""PostgreSQL access: pool, idempotent schema bootstrap, query helpers.

Same conventions as the dashboard's ``src/db.py``, for the same reasons:

* **No ORM.** Plain parameterised SQL with ``$1`` placeholders. Never build a
  statement by string concatenation.
* **Statements run one at a time**, not as one multi-statement string. A batch
  runs in an implicit transaction, so one failing statement would silently roll
  back every other statement with it.
* **Every connection-level failure is converted** into :class:`DatabaseUnavailable`
  before it leaves this module, so ``main.py`` needs exactly one handler for it.
  Do not register a handler for ``OSError`` — that would turn every unrelated
  socket error in the process into "database unavailable".

The schema is created with ``CREATE TABLE IF NOT EXISTS`` plus
``ADD COLUMN IF NOT EXISTS``. There is no migration framework; changes go in
``SCHEMA`` below and must stay backwards compatible with the live database.
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable, Sequence

import asyncpg

from . import config


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached. Always a 503, never a 500."""


_pool: asyncpg.Pool | None = None

# Connection-level failures. Anything in here means "the database is not
# reachable"; anything else is a real query error and belongs to the caller.
_CONNECTION_ERRORS = (
    OSError,
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.ConnectionDoesNotExistError,
    asyncpg.InterfaceError,
    asyncio.TimeoutError,
)


SCHEMA: tuple[str, ...] = (
    # --- Catalogue cache ----------------------------------------------------
    # Deezer answers are cached so browsing does not hammer a public API that
    # rate-limits, and so the home page still renders when it is down. Empty
    # results are deliberately NOT written here: a provider blip would otherwise
    # poison the cache for hours, which is a lesson this org already paid for
    # once on the Crimson backend.
    """
    CREATE TABLE IF NOT EXISTS catalog_cache (
        key         TEXT PRIMARY KEY,
        payload     JSONB       NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at  TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS catalog_cache_expires ON catalog_cache (expires_at)",
    # --- Resolved audio sources --------------------------------------------
    # The scraper's output. `track_key` is our stable id for a track
    # (``deezer:<id>``); `source_url` is the direct audio URL yt-dlp returned.
    # The URL expires, the *match* does not — so `provider_id` (the YouTube
    # video id we matched this track to) is kept past `expires_at` and reused,
    # which turns a re-resolve into one cheap extraction instead of a search.
    """
    CREATE TABLE IF NOT EXISTS track_source (
        track_key    TEXT PRIMARY KEY,
        provider     TEXT        NOT NULL,
        provider_id  TEXT,
        source_url   TEXT,
        mime         TEXT,
        bitrate      INTEGER,
        duration     INTEGER,
        expires_at   TIMESTAMPTZ,
        failures     INTEGER     NOT NULL DEFAULT 0,
        updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # --- Per-user state -----------------------------------------------------
    # `user_id` is the dashboard's user id, forwarded by the gateway. There is
    # no users table here on purpose: this service has no accounts of its own
    # and must not become a second place where identity lives.
    """
    CREATE TABLE IF NOT EXISTS liked_track (
        user_id    TEXT        NOT NULL,
        track_key  TEXT        NOT NULL,
        track      JSONB       NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, track_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS liked_track_user ON liked_track (user_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS playlist (
        id          BIGSERIAL   PRIMARY KEY,
        user_id     TEXT        NOT NULL,
        name        TEXT        NOT NULL,
        description TEXT        NOT NULL DEFAULT '',
        cover       TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS playlist_user ON playlist (user_id, updated_at DESC)",
    # `position` is a plain integer rewritten on reorder. A fractional ranking
    # would avoid the rewrite, but playlists here are hundreds of rows, not
    # millions, and an integer column is the one a human can read in psql.
    """
    CREATE TABLE IF NOT EXISTS playlist_track (
        playlist_id BIGINT      NOT NULL REFERENCES playlist (id) ON DELETE CASCADE,
        track_key   TEXT        NOT NULL,
        track       JSONB       NOT NULL,
        position    INTEGER     NOT NULL,
        added_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (playlist_id, track_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS playlist_track_order ON playlist_track (playlist_id, position)",
    # History drives "recently played" and the home page's personal rails.
    """
    CREATE TABLE IF NOT EXISTS play_history (
        id         BIGSERIAL   PRIMARY KEY,
        user_id    TEXT        NOT NULL,
        track_key  TEXT        NOT NULL,
        track      JSONB       NOT NULL,
        played_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        seconds    INTEGER     NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS play_history_user ON play_history (user_id, played_at DESC)",
    # Playback state, one row per user, so a phone can pick up where the desktop
    # stopped. Written on pause/track change, not continuously.
    """
    CREATE TABLE IF NOT EXISTS player_state (
        user_id    TEXT        PRIMARY KEY,
        state      JSONB       NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Per-user preferences (repeat/shuffle/volume/quality). Display-only, but
    # unlike the dashboard's language toggle these follow the account, because
    # the phone and the desktop are the same listener.
    """
    CREATE TABLE IF NOT EXISTS user_prefs (
        user_id    TEXT        PRIMARY KEY,
        prefs      JSONB       NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
)


async def connect() -> None:
    """Open the pool and bootstrap the schema. Called from the lifespan handler."""
    global _pool
    if _pool is not None:
        return
    try:
        _pool = await asyncpg.create_pool(
            dsn=config.dsn(),
            min_size=config.DB_POOL_MIN,
            max_size=config.DB_POOL_MAX,
            command_timeout=30,
            # JSONB in, dicts out. Without this every payload would arrive as a
            # string and every caller would have to remember to json.loads it.
            init=_init_connection,
        )
    except _CONNECTION_ERRORS as err:
        raise DatabaseUnavailable(str(err)) from err
    await _bootstrap()


async def _init_connection(conn: asyncpg.Connection) -> None:
    import json

    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def _bootstrap() -> None:
    """Create everything that does not exist yet, one statement at a time."""
    for statement in SCHEMA:
        await execute(statement)


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
    _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise DatabaseUnavailable("connection pool is not open")
    return _pool


async def execute(query: str, *args: Any) -> str:
    try:
        async with pool().acquire() as conn:
            return await conn.execute(query, *args)
    except _CONNECTION_ERRORS as err:
        raise DatabaseUnavailable(str(err)) from err


async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    try:
        async with pool().acquire() as conn:
            return await conn.fetch(query, *args)
    except _CONNECTION_ERRORS as err:
        raise DatabaseUnavailable(str(err)) from err


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    try:
        async with pool().acquire() as conn:
            return await conn.fetchrow(query, *args)
    except _CONNECTION_ERRORS as err:
        raise DatabaseUnavailable(str(err)) from err


async def fetchval(query: str, *args: Any) -> Any:
    try:
        async with pool().acquire() as conn:
            return await conn.fetchval(query, *args)
    except _CONNECTION_ERRORS as err:
        raise DatabaseUnavailable(str(err)) from err


async def executemany(query: str, args: Iterable[Sequence[Any]]) -> None:
    rows = list(args)
    if not rows:
        return
    try:
        async with pool().acquire() as conn:
            await conn.executemany(query, rows)
    except _CONNECTION_ERRORS as err:
        raise DatabaseUnavailable(str(err)) from err


async def healthy() -> bool:
    """Used by ``/healthz/db``; never by the container health check.

    The liveness probe deliberately does not touch PostgreSQL — a health check
    that failed during a database outage would have Swarm restart a container
    that is behaving exactly as designed.
    """
    try:
        return await fetchval("SELECT 1") == 1
    except DatabaseUnavailable:
        return False
