"""Reads a public Spotify playlist without the Spotify Web API.

There is no registered app here and no client id/secret — by choice, the same
one this project already made for the catalogue (see the module docstring in
``deezer.py``). Spotify's real Web API needs exactly that, so instead this
reads the same JSON a browser gets when it renders
``https://open.spotify.com/embed/playlist/<id>`` — the page Spotify itself
serves for embedding a playlist in someone else's site, unauthenticated,
specifically so it can be read by something that is not spotify.com.

That is also this module's whole risk profile: it depends on the shape of a
page Spotify did not promise to keep stable, the same trade this project
already made with yt-dlp against YouTube (see docs/providers.md, "When
playback breaks"). The failure mode is the same too — everything stops
resolving at once, not gradually — and the fix is the same shape: re-inspect
the page and update the JSON path below.

Verified against the live page while writing this (two real playlists, one at
50 tracks and one at 100): the embed page's ``trackList`` does not paginate
and does not expose a total distinct from its own length, so a playlist with
more than ``MAX_TRACKS`` tracks is silently truncated by Spotify's own page,
not by this code. ``SpotifyPlaylist.truncated`` is set to ``True`` whenever a
fetch comes back at exactly the cap, which is the only signal available.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

from .. import config

MAX_TRACKS = 100

# Both the share-link form (open.spotify.com/playlist/<id>, optionally with a
# locale prefix like /intl-de/ and a trailing ?si=... tracking param) and the
# spotify:playlist:<id> URI form paste-able from the app itself.
_ID_RE = re.compile(r"playlist[:/]([a-zA-Z0-9]{22})\b")

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
            # A generic browser UA — matches what the scraper already sends
            # yt-dlp's requests as (config.USER_AGENT). Spotify's own embed
            # page is public and meant to be fetched by other sites, but an
            # obvious bot UA is still needlessly asking to be blocked.
            headers={"User-Agent": config.USER_AGENT, "Accept-Language": "en"},
            follow_redirects=True,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


class SpotifyUnavailable(RuntimeError):
    """The playlist could not be read — bad url, not found, or page changed."""


@dataclass(frozen=True)
class SpotifyTrack:
    title: str
    artist: str
    duration: int  # seconds


@dataclass(frozen=True)
class SpotifyPlaylist:
    name: str
    owner: str
    tracks: list[SpotifyTrack] = field(default_factory=list)
    truncated: bool = False


def extract_playlist_id(url: str) -> str | None:
    match = _ID_RE.search((url or "").strip())
    return match.group(1) if match else None


def _parse_track(raw: dict) -> SpotifyTrack | None:
    if raw.get("entityType") != "track":
        return None  # a local file or a podcast episode mixed into the list
    title = str(raw.get("title") or "").strip()
    if not title:
        return None
    # "Artist One, Artist Two" — subtitle is Spotify's own comma-joined
    # display string; the first name is enough for a search query and for
    # showing the user what did not match.
    subtitle = str(raw.get("subtitle") or "")
    artist = subtitle.split(",")[0].strip()
    duration_ms = raw.get("duration") or 0
    return SpotifyTrack(title=title, artist=artist, duration=round(int(duration_ms) / 1000))


async def fetch_playlist(playlist_id: str) -> SpotifyPlaylist:
    try:
        response = await client().get(f"https://open.spotify.com/embed/playlist/{playlist_id}")
    except httpx.HTTPError as err:
        raise SpotifyUnavailable(f"Spotify unreachable: {err}") from err
    if response.status_code == 404:
        raise SpotifyUnavailable("Playlist not found (private, or deleted)")
    if response.status_code != 200:
        raise SpotifyUnavailable(f"Spotify answered {response.status_code}")

    match = _NEXT_DATA_RE.search(response.text)
    if not match:
        # The page structure changed — see the module docstring. Also what a
        # region-gated consent/interstitial page would hit, since that page
        # has no __NEXT_DATA__ block at all; the byte count is the fastest
        # way to tell the two apart in a log without dumping the whole body.
        print(f"[music] spotify import: no __NEXT_DATA__ in a {len(response.text)}-byte response")
        raise SpotifyUnavailable("Could not read the playlist page (page format changed)")
    try:
        payload = json.loads(match.group(1))
        entity = payload["props"]["pageProps"]["state"]["data"]["entity"]
    except (ValueError, KeyError, TypeError) as err:
        raise SpotifyUnavailable(f"Could not parse the playlist page: {err}") from err

    if entity.get("type") != "playlist":
        raise SpotifyUnavailable("That link is not a playlist")

    raw_tracks = entity.get("trackList") or []
    tracks = [t for t in (_parse_track(r) for r in raw_tracks) if t is not None]
    dropped = len(raw_tracks) - len(tracks)
    # This page's shape (and therefore how much of a playlist it embeds) has
    # already turned out to differ by the requesting network/region for at
    # least one real playlist — log the raw count so that is visible from the
    # server's own vantage point rather than only reproducible by whoever
    # tests it from wherever they happen to be sitting.
    print(
        f"[music] spotify import: playlist {playlist_id!r} -> "
        f"{len(raw_tracks)} raw entries, {len(tracks)} usable"
        + (f", {dropped} dropped (not entityType=track or no title)" if dropped else "")
    )
    return SpotifyPlaylist(
        name=str(entity.get("title") or "Imported playlist")[:120],
        owner=str(entity.get("subtitle") or ""),
        tracks=tracks[:MAX_TRACKS],
        truncated=len(raw_tracks) >= MAX_TRACKS,
    )
