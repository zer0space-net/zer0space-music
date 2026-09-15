"""Runtime configuration.

Everything the process needs to know about its environment is resolved here,
once, at import time — so the rest of the code never reads ``os.environ``
directly and there is exactly one place to look when a deployment behaves
differently than expected. Deliberately the same shape as the dashboard's
``src/config.py``, because an operator reads both.

Secret resolution order is **Docker Swarm secret file -> environment variable**,
never the other way round. A secret mounted at ``/run/secrets/<name>`` is the
authoritative value; the env var exists only so local development works without
a Swarm. This repository is **public**, so no real value ever appears in it:
``.env.example`` carries placeholders and nothing else.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import quote

SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))


def read_secret_source(secret_name: str, env_name: str) -> tuple[str | None, str | None]:
    """Resolve a secret and report where it came from.

    Returns ``(value, source)`` where ``source`` is ``"swarm secret"``, ``"env"``
    or ``None``. The boot log states the source so an operator can confirm at a
    glance that a restart-surviving key is in effect rather than a generated one.
    """
    try:
        value = (SECRETS_DIR / secret_name).read_text(encoding="utf-8").strip()
        if value:
            return value, "swarm secret"
    except OSError:
        pass
    env = os.environ.get(env_name)
    if env:
        return env, "env"
    return None, None


def read_secret(secret_name: str, env_name: str) -> str | None:
    """Swarm secret file first, env var second, ``None`` if neither exists."""
    return read_secret_source(secret_name, env_name)[0]


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


# --- Database ---------------------------------------------------------------
# Playlists, likes, play history and the resolved-source cache. The same
# PostgreSQL instance the dashboard uses (zs-state-01), but its own database by
# default, so a music schema change can never touch dashboard data.

DATABASE_URL = os.environ.get("DATABASE_URL") or None
DB_HOST = os.environ.get("DB_HOST", "192.168.0.16")
DB_PORT = _int("DB_PORT", 5432)
DB_NAME = os.environ.get("DB_NAME", "zer0space_music")
DB_USER = os.environ.get("DB_USER", "dashboard")
DB_PASS = read_secret("db_password", "DB_PASS") or ""
DB_POOL_MIN = _int("DB_POOL_MIN", 1)
DB_POOL_MAX = _int("DB_POOL_MAX", 8)


def dsn() -> str:
    """The connection string, assembled from whichever source is configured."""
    if DATABASE_URL:
        return DATABASE_URL
    auth = DB_USER
    if DB_PASS:
        auth = DB_USER + ":" + quote(DB_PASS, safe="")
    return f"postgresql://{auth}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


# --- Identity ---------------------------------------------------------------
# This service publishes no ports. It is reached only by the dashboard, which
# authenticates with a shared token and then names the signed-in user in a
# header. Trusting that header is only sound while the token is required — so
# the two are checked together, never separately. Same posture as zer0space-ai.

SERVICE_TOKEN = read_secret("music_service_token", "MUSIC_SERVICE_TOKEN") or ""
USER_HEADER = os.environ.get("MUSIC_USER_HEADER", "X-Zer0space-User")
USER_NAME_HEADER = os.environ.get("MUSIC_USER_NAME_HEADER", "X-Zer0space-Username")

# Development escape hatch: with no token configured the service accepts the
# identity header from anyone. Fine behind a closed Docker network on a laptop,
# never acceptable on the cluster — the boot log shouts about it.
REQUIRE_TOKEN = bool(SERVICE_TOKEN)

# --- Public mount -----------------------------------------------------------
# Where the browser thinks this app lives. The dashboard mounts it under
# /music, so every URL the templates and the API emit has to carry that prefix
# or the player's own fetches land on the dashboard instead. Taken from
# X-Forwarded-Prefix at request time where the gateway sends it; this is the
# fallback for a direct run.

BASE_PATH = "/" + os.environ.get("MUSIC_BASE_PATH", "/music").strip("/")

# Where the *media* relay lives. Empty means "same origin as the API", which is
# the tunnel path and the default the user chose. Point it at a direct or
# Tailscale host to move audio bytes off the Cloudflare tunnel without moving
# the API with them. See docs/architecture.md, "Where the bytes go".
MEDIA_BASE_URL = os.environ.get("MUSIC_MEDIA_BASE_URL", "").rstrip("/") or None

# --- Stream tickets ---------------------------------------------------------
# A resolved audio URL is a capability: it is bound to this host's IP and plays
# without any further auth, so the browser never sees one. It gets a signed
# ticket instead, which names the track and the user and expires.

STREAM_SECRET = read_secret("music_stream_secret", "MUSIC_STREAM_SECRET") or ""
STREAM_SECRET_EPHEMERAL = not STREAM_SECRET
if STREAM_SECRET_EPHEMERAL:
    # Generated rather than fatal, so a misconfigured deploy still plays music.
    # The cost is that tickets minted before a restart stop working, which for a
    # six-hour ticket means a player that has to ask for its stream URL again.
    STREAM_SECRET = secrets.token_urlsafe(32)

STREAM_TTL = _int("MUSIC_STREAM_TTL", 6 * 3600)

# --- Catalogue --------------------------------------------------------------

DEEZER_API = os.environ.get("MUSIC_DEEZER_API", "https://api.deezer.com").rstrip("/")
CATALOG_TTL = _int("MUSIC_CATALOG_TTL", 6 * 3600)
SEARCH_LIMIT = _int("MUSIC_SEARCH_LIMIT", 40)

# --- Scraper ----------------------------------------------------------------
# yt-dlp runs in a worker thread (it is synchronous and does real network I/O).
# The pool is small on purpose: YouTube rate-limits per IP, and a burst of
# parallel extractions is the fastest way to get this host throttled.

SCRAPER_ENABLED = _bool("MUSIC_SCRAPER_ENABLED", True)
SCRAPER_WORKERS = _int("MUSIC_SCRAPER_WORKERS", 2)
SCRAPER_TIMEOUT = _int("MUSIC_SCRAPER_TIMEOUT", 45)
# A resolved URL's own lifetime. YouTube's audio URLs carry an expiry of roughly
# six hours; re-resolving a little early is cheaper than handing the player a
# link that dies in the middle of a song.
SOURCE_TTL = _int("MUSIC_SOURCE_TTL", 4 * 3600)
# Optional: a cookies.txt to lift age gates and reduce bot checks. Mount it
# read-only; never commit one — it is a live credential for a Google account.
COOKIES_FILE = os.environ.get("MUSIC_COOKIES_FILE", "") or None
# Optional upstream proxy for the scraper only — not for Deezer, not for the
# media relay — for when this host's IP gets rate-limited.
SCRAPER_PROXY = os.environ.get("MUSIC_SCRAPER_PROXY", "") or None
# Preview fallback: when the scraper cannot resolve a track, Deezer's own 30s
# preview mp3 is played instead of nothing. Off means the track simply fails.
PREVIEW_FALLBACK = _bool("MUSIC_PREVIEW_FALLBACK", True)

# --- Misc -------------------------------------------------------------------

PORT = _int("PORT", 8000)
LOG_REQUESTS = _bool("MUSIC_LOG_REQUESTS", False)
USER_AGENT = os.environ.get(
    "MUSIC_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)


def boot_report() -> list[str]:
    """Lines the app prints on start, so a deploy is diagnosable from the logs."""
    token_src = read_secret_source("music_service_token", "MUSIC_SERVICE_TOKEN")[1]
    stream_src = read_secret_source("music_stream_secret", "MUSIC_STREAM_SECRET")[1]
    media = f"  media -> {MEDIA_BASE_URL}" if MEDIA_BASE_URL else "  media same-origin"
    lines = [
        f"[music] database   {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
        f"[music] mount      {BASE_PATH}{media}",
        f"[music] scraper    {'on' if SCRAPER_ENABLED else 'OFF'} "
        f"({SCRAPER_WORKERS} workers, cookies={'yes' if COOKIES_FILE else 'no'}, "
        f"proxy={'yes' if SCRAPER_PROXY else 'no'})",
    ]
    if REQUIRE_TOKEN:
        lines.append(f"[music] identity   service token required (from {token_src})")
    else:
        lines.append(
            "[music] identity   *** NO SERVICE TOKEN SET — the user header is "
            "trusted from any caller. Do not run this on the cluster. ***"
        )
    if STREAM_SECRET_EPHEMERAL:
        lines.append(
            "[music] tickets    generated at boot — stream tickets will not "
            "survive a restart. Set the music_stream_secret Swarm secret."
        )
    else:
        lines.append(f"[music] tickets    signing key from {stream_src}")
    return lines
