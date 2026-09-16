"""The zer0space Music service.

One FastAPI app: a Jinja-rendered shell at ``/``, a JSON API under ``/api`` and
the media relay at ``/media``. It is mounted by the dashboard at ``/music``, so
every absolute URL it emits carries the forwarded prefix rather than assuming
one — getting that wrong is what produced the Crimson grey-player bug, twice.

There is no login here and no session. The dashboard has already checked the
zer0space session and names the user in a header; :mod:`src.identity` is the
one place that decides whether to believe it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import cache, config, db, identity, library, playlist_import, podcasts, resolve, stream
from .providers import deezer

app = FastAPI(
    title="zer0space Music",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Bodies here are playlist edits and player state, not uploads. 512 kB is
# generous for both and keeps a malformed client from buffering unbounded JSON.
MAX_BODY = 512 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def fail(status: int, code: str, message: str) -> JSONResponse:
    """Every error carries a stable ``code``; the client translates it.

    Same contract as the dashboard: the server never sends translated prose, and
    an unknown code degrades to the English text rather than to a blank.
    """
    return JSONResponse({"error": message, "code": code}, status_code=status)


async def json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ApiError(413, "BODY_TOO_LARGE", "Request body too large")
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ApiError(400, "BAD_JSON", "Malformed JSON body") from None
    if not isinstance(payload, dict):
        raise ApiError(400, "BAD_JSON", "Expected a JSON object")
    return payload


# A full-library backup (every playlist, every track) is a different shape of
# request than the rest of this API — closer to an upload than a state edit —
# so it gets its own, much larger cap rather than stretching MAX_BODY for
# everyone. 2000 tracks (the playlist cap) at roughly 300 bytes each is already
# past 512 kB for a single playlist.
MAX_BACKUP_BODY = 8 * 1024 * 1024


async def backup_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BACKUP_BODY:
        raise ApiError(413, "BODY_TOO_LARGE", "Backup file too large")
    if not raw:
        raise ApiError(400, "BAD_JSON", "Empty backup file")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise ApiError(400, "BAD_JSON", "Malformed JSON body") from None
    if not isinstance(payload, dict):
        raise ApiError(400, "BAD_JSON", "Expected a JSON object")
    return payload


_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9 _.-]+")


def _download(data: dict[str, Any], filename: str) -> Response:
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    safe_name = (_UNSAFE_FILENAME.sub("", filename).strip() or "backup.json")[:150]
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


def me(request: Request) -> str:
    try:
        return identity.user_id(request)
    except identity.Unauthorized as err:
        raise ApiError(401, err.code, err.message) from err


# --- Lifespan ---------------------------------------------------------------


@app.on_event("startup")
async def _startup() -> None:
    for line in config.boot_report():
        print(line)
    try:
        await db.connect()
        print("[music] database   connected, schema ready")
    except db.DatabaseUnavailable as err:
        # Not fatal: the catalogue is browsable without a database, and a
        # container that refuses to start is harder to diagnose than one that
        # serves a clear 503 on the routes that need it.
        print(f"[music] database   UNAVAILABLE at startup: {err}")
    app.state.sweeper = asyncio.create_task(_sweeper())


@app.on_event("shutdown")
async def _shutdown() -> None:
    task = getattr(app.state, "sweeper", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await asyncio.gather(
        deezer.close(), stream.close(), playlist_import.close(), podcasts.close(), db.close(),
        return_exceptions=True,
    )


async def _sweeper() -> None:
    """Drop expired catalogue rows hourly. Without it the cache table only grows."""
    while True:
        await asyncio.sleep(3600)
        try:
            removed = await cache.sweep()
            if removed:
                print(f"[music] cache      swept {removed} expired rows")
        except db.DatabaseUnavailable:
            pass
        except Exception as err:  # noqa: BLE001
            print(f"[music] cache sweep failed: {err!r}")


# --- Error handling ---------------------------------------------------------


@app.exception_handler(ApiError)
async def _api_error(_request: Request, exc: ApiError) -> Response:
    return fail(exc.status, exc.code, exc.message)


@app.exception_handler(db.DatabaseUnavailable)
async def _db_down(request: Request, exc: Exception) -> Response:
    """A 503, not a 500: nothing is wrong with the request."""
    print(f"[music] DB unavailable on {request.method} {request.url.path}: {exc}")
    return fail(503, "DB_UNAVAILABLE", "Database unavailable — please try again later")


@app.exception_handler(deezer.DeezerUnavailable)
async def _catalog_down(request: Request, exc: Exception) -> Response:
    print(f"[music] catalogue unavailable on {request.url.path}: {exc}")
    return fail(503, "CATALOG_UNAVAILABLE", "The music catalogue is temporarily unreachable")


@app.exception_handler(library.LibraryError)
async def _library_error(_request: Request, exc: Exception) -> Response:
    err = exc if isinstance(exc, library.LibraryError) else None
    status = 404 if err and err.code == "NOT_FOUND" else 400
    return fail(status, err.code if err else "BAD_REQUEST", str(exc))


@app.exception_handler(podcasts.PodcastError)
async def _podcast_error(_request: Request, exc: Exception) -> Response:
    err = exc if isinstance(exc, podcasts.PodcastError) else None
    status = 404 if err and err.code == "NOT_FOUND" else 400
    return fail(status, err.code if err else "BAD_REQUEST", str(exc))


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> Response:
    # The exception text is deliberately not sent to the client.
    print(f"[music] error on {request.method} {request.url.path}: {exc!r}")
    return fail(500, "INTERNAL", "Internal error")


# --- Health -----------------------------------------------------------------


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Liveness only — deliberately does not touch PostgreSQL, so a database
    outage does not make Swarm restart a container that is behaving correctly."""
    return {"status": "ok"}


@app.get("/healthz/db", include_in_schema=False)
async def healthz_db() -> Response:
    ok = await db.healthy()
    return JSONResponse({"database": "ok" if ok else "unavailable"}, 200 if ok else 503)


# --- The app shell ----------------------------------------------------------


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> Response:
    """The player shell. Everything after this is fetched by the client."""
    try:
        user = identity.user_id(request)
    except identity.Unauthorized:
        # Reached directly rather than through the gateway. There is no login
        # here to send them to, so point at the dashboard's front door.
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>zer0space Music</title>"
            "<body style='background:#04070e;color:#e9eefa;font:16px system-ui;"
            "display:grid;place-items:center;height:100vh;margin:0'>"
            "<p>Bitte über das <a style='color:#2f7dfb' href='/'>zer0space Dashboard</a> "
            "anmelden.</p>",
            status_code=401,
        )
    base = identity.base_path(request)
    return templates.TemplateResponse(
        request,
        "app.html",
        {
            "base": "" if base == "/" else base,
            "username": identity.user_name(request) or "",
            "user_id": user,
            "version": __import__("src", fromlist=["__version__"]).__version__,
        },
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest(request: Request) -> Response:
    """Built at request time, not served as a static file.

    ``start_url`` and ``scope`` have to carry the gateway's prefix, or an
    installed PWA opens the dashboard root instead of the player — and on iOS a
    scope mismatch is also what stops the service worker from registering.
    """
    base = identity.base_path(request)
    base = "" if base == "/" else base
    return JSONResponse(
        {
            "name": "zer0space Music",
            "short_name": "Music",
            "description": "Der Musikplayer des zer0space Homelabs",
            "start_url": f"{base}/",
            "scope": f"{base}/",
            "display": "standalone",
            "background_color": "#04070e",
            "theme_color": "#04070e",
            "orientation": "portrait-primary",
            "icons": [
                {
                    "src": f"{base}/static/img/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
                {
                    "src": f"{base}/static/img/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
            ],
        },
        headers={"Content-Type": "application/manifest+json"},
    )


# --- Catalogue --------------------------------------------------------------


@app.get("/api/me", include_in_schema=False)
async def api_me(request: Request) -> dict[str, Any]:
    user = me(request)
    try:
        preferences = await library.prefs(user)
    except db.DatabaseUnavailable:
        preferences = dict(library.DEFAULT_PREFS)
    return {
        "id": user,
        "name": identity.user_name(request),
        "prefs": preferences,
        "scraper": config.SCRAPER_ENABLED,
    }


@app.get("/api/home", include_in_schema=False)
async def api_home(request: Request) -> dict[str, Any]:
    """The landing rails: editorial charts plus this listener's own history."""
    user = me(request)
    charts = await cache.through("home:charts", lambda: deezer.charts(30))

    # The personal rails are best effort: a database blip should shrink the home
    # page, not empty it. The editorial rails come from the cache either way.
    recent: list[dict[str, Any]] = []
    artists: list[dict[str, Any]] = []
    playlists: list[dict[str, Any]] = []
    try:
        recent, artists, playlists = await asyncio.gather(
            library.recent(user, 20), library.top_artists(user, 10), library.playlists(user)
        )
    except db.DatabaseUnavailable:
        pass

    return {
        "charts": charts,
        "recent": recent,
        "topArtists": artists,
        "playlists": playlists[:8],
        "greetingName": identity.user_name(request),
    }


@app.get("/api/search", include_in_schema=False)
async def api_search(request: Request, q: str = "", limit: int = 30) -> dict[str, Any]:
    me(request)
    query = q.strip()
    if not query:
        return {"tracks": [], "albums": [], "artists": [], "playlists": []}
    if len(query) > 200:
        raise ApiError(400, "QUERY_TOO_LONG", "Search query too long")
    limit = max(1, min(limit, config.SEARCH_LIMIT))
    # Cached under the *query*, not the query plus limit, and sliced per caller —
    # caching the truncated list is how one caller's limit freezes for everyone.
    key = f"search:{query.lower()}"
    results = await cache.through(key, lambda: deezer.search(query, config.SEARCH_LIMIT), ttl=1800)
    return {section: items[:limit] for section, items in results.items()}


@app.get("/api/album/{album_id}", include_in_schema=False)
async def api_album(request: Request, album_id: str) -> dict[str, Any]:
    me(request)
    return await cache.through(f"album:{album_id}", lambda: deezer.album(album_id))


@app.get("/api/artist/{artist_id}", include_in_schema=False)
async def api_artist(request: Request, artist_id: str) -> dict[str, Any]:
    me(request)
    return await cache.through(f"artist:{artist_id}", lambda: deezer.artist(artist_id))


@app.get("/api/catalog-playlist/{playlist_id}", include_in_schema=False)
async def api_catalog_playlist(request: Request, playlist_id: str) -> dict[str, Any]:
    me(request)
    return await cache.through(f"dzplaylist:{playlist_id}", lambda: deezer.playlist(playlist_id))


@app.get("/api/genres", include_in_schema=False)
async def api_genres(request: Request) -> dict[str, Any]:
    me(request)
    return {"genres": await cache.through("genres", deezer.genres, ttl=7 * 24 * 3600)}


@app.get("/api/genre/{genre_id}", include_in_schema=False)
async def api_genre(request: Request, genre_id: str) -> dict[str, Any]:
    me(request)
    artists = await cache.through(
        f"genre:{genre_id}", lambda: deezer.genre_artists(genre_id, 40), ttl=24 * 3600
    )
    return {"artists": artists}


# --- Playback ---------------------------------------------------------------


def _media_root(request: Request) -> str:
    """This origin, with the gateway's forwarded prefix — what a same-origin
    URL handed to the browser (a stream ticket, a relayed podcast cover) has
    to be built on rather than a root-relative path, since the dashboard
    mounts this app under /music and a bare "/api/..." would resolve against
    the dashboard's own origin instead."""
    base = identity.base_path(request)
    base = "" if base == "/" else base
    return config.MEDIA_BASE_URL or f"{identity.public_origin(request)}{base}"


@app.get("/api/stream/{track_key:path}", include_in_schema=False)
async def api_stream(request: Request, track_key: str) -> dict[str, Any]:
    """Mint a stream ticket for one track and say where to play it.

    Returns a URL rather than the audio itself, so the ``<audio>`` element owns
    the byte range negotiation — which is what makes seeking and background
    playback work on a phone.
    """
    user = me(request)

    # A podcast key (podcast:<subscription id>:<guid hash>) never goes near
    # resolve.py — there is no catalogue lookup or scraper involved, the feed
    # already names a direct, playable URL. Same ticket + /media relay either
    # way, which is what lets the player treat an episode as an ordinary
    # track without knowing the difference.
    if track_key.startswith("podcast:"):
        source = await podcasts.resolve_episode(user, track_key)
        if source is None:
            raise ApiError(404, "NO_SOURCE", "No playable source found for this episode")
        duration = source.duration
    else:
        try:
            track = await resolve.track_for_key(track_key)
        except resolve.UnknownTrack:
            raise ApiError(404, "UNKNOWN_TRACK", "No such track") from None
        source = await resolve.source_for(track)
        if source is None:
            raise ApiError(404, "NO_SOURCE", "No playable source found for this track")
        duration = source.duration or track.duration

    media_root = _media_root(request)
    ticket = stream.mint(track_key, user)

    return {
        "url": f"{media_root}/media/{ticket}",
        "mime": source.mime,
        "duration": duration,
        "provider": source.provider,
        # The UI says so out loud rather than letting a 30-second track look
        # like a buggy full one.
        "preview": source.provider == "preview",
        "expiresIn": config.STREAM_TTL,
    }


@app.post("/api/prefetch", include_in_schema=False)
async def api_prefetch(request: Request) -> dict[str, str]:
    """Warm the next few tracks so the gap between songs stays short."""
    me(request)
    payload = await json_body(request)
    keys = [str(k) for k in (payload.get("keys") or [])][:5]
    await resolve.prefetch(keys)
    return {"status": "ok"}


@app.api_route("/media/{ticket}", methods=["GET", "HEAD"], include_in_schema=False)
async def media(request: Request, ticket: str) -> Response:
    """The audio relay.

    Deliberately **not** gated on the identity header: the ticket is the
    capability. An ``<audio>`` element started by a service worker, or a request
    replayed by the OS media stack after the page was backgrounded, does not
    always carry the headers the gateway adds — and a track that stops when the
    phone locks is precisely the bug this service exists to avoid.
    """
    redeemed = stream.redeem(ticket)
    if redeemed is None:
        return fail(403, "BAD_TICKET", "Stream ticket missing, expired or invalid")
    track_key, user = redeemed

    if track_key.startswith("podcast:"):
        source = await podcasts.resolve_episode(user, track_key)
        if source is None:
            return fail(404, "NO_SOURCE", "No playable source found for this episode")
        return await stream.relay(request, source)

    try:
        track = await resolve.track_for_key(track_key)
    except resolve.UnknownTrack:
        return fail(404, "UNKNOWN_TRACK", "No such track")

    source = await resolve.source_for(track)
    if source is None:
        return fail(404, "NO_SOURCE", "No playable source found for this track")
    return await stream.relay(request, source)


# --- Library ----------------------------------------------------------------


@app.get("/api/library", include_in_schema=False)
async def api_library(request: Request) -> dict[str, Any]:
    user = me(request)
    playlists, liked, recent = await asyncio.gather(
        library.playlists(user), library.liked(user), library.recent(user, 50)
    )
    return {"playlists": playlists, "liked": liked, "recent": recent}


@app.get("/api/liked-keys", include_in_schema=False)
async def api_liked_keys(request: Request) -> dict[str, Any]:
    return {"keys": await library.liked_keys(me(request))}


@app.post("/api/like", include_in_schema=False)
async def api_like(request: Request) -> dict[str, str]:
    user = me(request)
    payload = await json_body(request)
    track = payload.get("track")
    if not isinstance(track, dict):
        raise ApiError(400, "BAD_TRACK", "A track object is required")
    await library.like(user, track)
    return {"status": "ok"}


@app.post("/api/unlike", include_in_schema=False)
async def api_unlike(request: Request) -> dict[str, str]:
    user = me(request)
    payload = await json_body(request)
    await library.unlike(user, str(payload.get("key") or ""))
    return {"status": "ok"}


@app.get("/api/playlists", include_in_schema=False)
async def api_playlists(request: Request) -> dict[str, Any]:
    return {"playlists": await library.playlists(me(request))}


@app.post("/api/playlists", include_in_schema=False)
async def api_create_playlist(request: Request) -> dict[str, Any]:
    user = me(request)
    payload = await json_body(request)
    return await library.create_playlist(
        user, str(payload.get("name") or ""), str(payload.get("description") or "")
    )


@app.post("/api/playlists/import-spotify", include_in_schema=False)
async def api_import_spotify_playlist(request: Request) -> dict[str, Any]:
    """Reads a public Spotify playlist link and creates the matching playlist
    here from whatever our own catalogue has — see playlist_import.py. No
    Spotify account or API key involved on either end."""
    user = me(request)
    payload = await json_body(request)
    return await playlist_import.import_spotify_playlist(user, str(payload.get("url") or ""))


@app.get("/api/playlists/export", include_in_schema=False)
async def api_export_library(request: Request) -> Response:
    """Every playlist as one file — a real backup, not a share link."""
    data = await library.export_library(me(request))
    return _download(data, "zer0space-music-backup.json")


@app.post("/api/playlists/import-backup", include_in_schema=False)
async def api_import_backup(request: Request) -> dict[str, Any]:
    """Restores a playlist from a file — one of two shapes.

    A file this app's own export produced (library.EXPORT_FORMAT_PLAYLIST/
    _LIBRARY) already carries our catalogue key per track: straight
    create+add, no matching. A title/artist list (playlist_import.
    EXPORT_FORMAT_TRACKLIST) — hand-written, or generated by an LLM asked to
    list a playlist's tracks — has no key at all and goes through the same
    Deezer matching pass as the Spotify import. Same escape hatch either way
    from Spotify's own 100-track ceiling, since this route never touches
    Spotify's page at all.
    """
    user = me(request)
    payload = await backup_body(request)
    if payload.get("format") == playlist_import.EXPORT_FORMAT_TRACKLIST:
        return await playlist_import.import_track_list(user, payload)
    return await library.import_backup(user, payload)


@app.get("/api/playlists/{playlist_id}", include_in_schema=False)
async def api_playlist(request: Request, playlist_id: str) -> dict[str, Any]:
    return await library.playlist(me(request), playlist_id)


@app.get("/api/playlists/{playlist_id}/export", include_in_schema=False)
async def api_export_playlist(request: Request, playlist_id: str) -> Response:
    data = await library.export_playlist(me(request), playlist_id)
    return _download(data, str(data["name"]) + ".json")


@app.patch("/api/playlists/{playlist_id}", include_in_schema=False)
async def api_rename_playlist(request: Request, playlist_id: str) -> dict[str, str]:
    user = me(request)
    payload = await json_body(request)
    await library.rename_playlist(
        user, playlist_id, str(payload.get("name") or ""), str(payload.get("description") or "")
    )
    return {"status": "ok"}


@app.delete("/api/playlists/{playlist_id}", include_in_schema=False)
async def api_delete_playlist(request: Request, playlist_id: str) -> dict[str, str]:
    await library.delete_playlist(me(request), playlist_id)
    return {"status": "ok"}


@app.post("/api/playlists/{playlist_id}/tracks", include_in_schema=False)
async def api_add_tracks(request: Request, playlist_id: str) -> dict[str, Any]:
    user = me(request)
    payload = await json_body(request)
    tracks = payload.get("tracks")
    if isinstance(payload.get("track"), dict):
        tracks = [payload["track"]]
    if not isinstance(tracks, list) or not tracks:
        raise ApiError(400, "BAD_TRACK", "A track or tracks array is required")
    added = await library.add_tracks(user, playlist_id, tracks[:200])
    return {"status": "ok", "added": added}


@app.delete("/api/playlists/{playlist_id}/tracks/{track_key:path}", include_in_schema=False)
async def api_remove_track(request: Request, playlist_id: str, track_key: str) -> dict[str, str]:
    await library.remove_track(me(request), playlist_id, track_key)
    return {"status": "ok"}


@app.post("/api/playlists/{playlist_id}/reorder", include_in_schema=False)
async def api_reorder(request: Request, playlist_id: str) -> dict[str, str]:
    user = me(request)
    payload = await json_body(request)
    keys = [str(k) for k in (payload.get("keys") or [])]
    await library.reorder(user, playlist_id, keys)
    return {"status": "ok"}


# --- Session continuity -----------------------------------------------------


@app.post("/api/played", include_in_schema=False)
async def api_played(request: Request) -> dict[str, str]:
    user = me(request)
    payload = await json_body(request)
    track = payload.get("track")
    if isinstance(track, dict):
        await library.record_play(user, track, int(payload.get("seconds") or 0))
    return {"status": "ok"}


@app.get("/api/state", include_in_schema=False)
async def api_get_state(request: Request) -> dict[str, Any]:
    return {"state": await library.load_state(me(request))}


@app.post("/api/state", include_in_schema=False)
async def api_save_state(request: Request) -> dict[str, str]:
    user = me(request)
    payload = await json_body(request)
    await library.save_state(user, payload)
    return {"status": "ok"}


@app.post("/api/prefs", include_in_schema=False)
async def api_prefs(request: Request) -> dict[str, Any]:
    user = me(request)
    payload = await json_body(request)
    return {"prefs": await library.save_prefs(user, payload)}


# --- Podcasts -----------------------------------------------------------
#
# A different content type on purpose: episodes are never fed through the
# catalogue or the scraper (see podcasts.py) — an RSS enclosure is already a
# direct, playable URL. /api/stream and /media above are the only place the
# two worlds meet, branching on the "podcast:" key prefix.


@app.get("/api/podcasts", include_in_schema=False)
async def api_podcasts(request: Request) -> dict[str, Any]:
    subs = await podcasts.subscriptions(me(request))
    root = _media_root(request)
    for sub in subs:
        sub["cover"] = f"{root}/api/podcasts/{sub['id']}/cover" if sub.pop("hasCover") else ""
    return {"podcasts": subs}


@app.post("/api/podcasts", include_in_schema=False)
async def api_podcast_subscribe(request: Request) -> dict[str, Any]:
    user = me(request)
    payload = await json_body(request)
    sub = await podcasts.subscribe(user, str(payload.get("url") or ""))
    sub["cover"] = f"{_media_root(request)}/api/podcasts/{sub['id']}/cover" if sub.pop("hasCover") else ""
    return sub


@app.delete("/api/podcasts/{subscription_id}", include_in_schema=False)
async def api_podcast_unsubscribe(request: Request, subscription_id: str) -> dict[str, str]:
    await podcasts.unsubscribe(me(request), subscription_id)
    return {"status": "ok"}


@app.get("/api/podcasts/{subscription_id}/episodes", include_in_schema=False)
async def api_podcast_episodes(request: Request, subscription_id: str) -> dict[str, Any]:
    data = await podcasts.episodes(me(request), subscription_id)
    cover = f"{_media_root(request)}/api/podcasts/{data['id']}/cover" if data.pop("hasCover") else ""
    data["cover"] = cover
    for episode in data["episodes"]:
        episode["cover"] = cover
    return data


@app.get("/api/podcasts/{subscription_id}/cover", include_in_schema=False)
async def api_podcast_cover(request: Request, subscription_id: str) -> Response:
    """Relays the show's own cover art same-origin — see fetch_image's
    docstring for why this can't just be an <img src> to the feed's host."""
    fetched = await podcasts.cover_bytes(me(request), subscription_id)
    if fetched is None:
        return Response(status_code=404)
    body, content_type = fetched
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
