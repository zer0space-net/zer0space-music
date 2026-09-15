"""The scraper: turns a catalogue track into a playable audio URL via yt-dlp.

Deezer says *what* a song is; this module finds *something that plays it*. It
searches YouTube for the track, scores the candidates, picks one, and hands back
the direct audio URL of the best audio-only format.

Four decisions here are load-bearing:

**m4a is strongly preferred over opus/webm.** YouTube's best audio format is
usually Opus in a WebM container, and Safari — every browser on iOS — cannot
play it. Since background playback on a phone is a headline feature of this
service, a format the phone cannot decode is not "higher quality", it is
silence. AAC in m4a plays everywhere.

**Duration is the match signal, not the title.** Titles are noisy ("Official
Video", "HD", "Lyrics", a topic channel's exact copy); duration is a number
Deezer already gave us. A candidate more than a few seconds off is a different
recording — a live version, an extended mix, or a reaction video.

**The match is cached separately from the URL.** A YouTube video id for a track
is stable forever; the URL it yields expires in about six hours and is bound to
the IP that requested it. So ``track_source`` keeps the id past the URL's expiry
and a re-resolve becomes one cheap extraction instead of a search plus an
extraction.

**Extraction is serialised through a small semaphore.** yt-dlp is synchronous
and does real network I/O, so it runs in a worker thread; and YouTube rate-limits
per IP, so running many at once is the fastest route to getting this host
throttled. This mirrors the FlareSolverr lesson from the Crimson backend.

The IP-binding also decides the delivery shape: a resolved URL only works from
the host that resolved it, so it can never be handed to the browser. It is
relayed by :mod:`src.stream` from this same container — the same same-origin
relay pattern the Crimson backend uses for VOE.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from .. import config
from .base import ResolvedSource, Track

try:  # pragma: no cover - import guard so the app still boots without yt-dlp
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError, ExtractorError

    AVAILABLE = True
except ImportError:  # pragma: no cover
    YoutubeDL = None  # type: ignore[assignment]
    DownloadError = ExtractorError = Exception  # type: ignore[misc,assignment]
    AVAILABLE = False

name = "ytmusic"

# How many candidates to pull from one search. Five is enough for the scorer to
# have a real choice and small enough that the search stays one request.
SEARCH_RESULTS = 5
# A candidate whose length differs from the catalogue's by more than this is a
# different recording, not a worse copy of the same one.
DURATION_TOLERANCE = 8
# Live sets, hour-long mixes and full album uploads all match a track title.
MAX_DURATION = 15 * 60

_semaphore: asyncio.Semaphore | None = None


def _gate() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, config.SCRAPER_WORKERS))
    return _semaphore


# Noise that appears in uploaded titles but never in a catalogue title. Stripped
# before comparison so "Song (Official Music Video)" still matches "Song".
_NOISE = re.compile(
    r"\((?:[^)]*\b(?:official|video|audio|lyric[s]?|hd|4k|mv|visualizer|"
    r"explicit|clean|remaster(?:ed)?)\b[^)]*)\)"
    r"|\[[^\]]*\b(?:official|video|audio|lyric[s]?|hd|4k|mv)\b[^\]]*\]"
    r"|\b(?:official\s+(?:music\s+)?video|lyric\s+video|audio\s+only)\b",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
# Versions that are genuinely a different recording. Matching one of these when
# the query did not ask for it is a strong negative signal.
_VARIANT = re.compile(
    r"\b(live|remix|cover|karaoke|instrumental|sped\s*up|slowed|nightcore|"
    r"8d|reverb|mashup|tribute|acoustic|demo|rehearsal)\b",
    re.IGNORECASE,
)


def _normalise(text: str) -> set[str]:
    cleaned = _NOISE.sub(" ", text or "")
    cleaned = _PUNCT.sub(" ", cleaned.lower())
    return {token for token in cleaned.split() if len(token) > 1}


def _score(candidate: dict[str, Any], track: Track) -> float:
    """How well one search result matches the track we asked for.

    Additive and deliberately readable — this is the function to print when a
    track resolves to the wrong song.
    """
    title = str(candidate.get("title") or "")
    uploader = str(candidate.get("uploader") or candidate.get("channel") or "")
    duration = int(candidate.get("duration") or 0)
    if not duration or duration > MAX_DURATION:
        return -1.0

    score = 0.0

    # 1. Duration — the strongest signal, because it is exact on both sides.
    if track.duration:
        delta = abs(duration - track.duration)
        if delta <= 2:
            score += 6.0
        elif delta <= DURATION_TOLERANCE:
            score += 4.0 - (delta / DURATION_TOLERANCE)
        elif delta <= 30:
            score += 0.5
        else:
            score -= 4.0

    # 2. Title overlap, after stripping upload noise.
    wanted = _normalise(track.title)
    got = _normalise(title)
    if wanted:
        score += 3.0 * (len(wanted & got) / len(wanted))

    # 3. The artist should appear in the title or be the channel.
    artist_tokens = _normalise(track.artist.name)
    if artist_tokens:
        in_title = len(artist_tokens & got) / len(artist_tokens)
        in_channel = len(artist_tokens & _normalise(uploader)) / len(artist_tokens)
        score += 2.0 * max(in_title, in_channel)

    # 4. "Artist - Topic" channels are YouTube's auto-generated music uploads:
    #    the label's own audio, no video track, no intro. Exactly what we want.
    if uploader.strip().endswith("- Topic"):
        score += 2.5
    if candidate.get("channel_is_verified") or candidate.get("uploader_verified"):
        score += 0.5

    # 5. A variant the catalogue title did not ask for is the wrong recording.
    if _VARIANT.search(title) and not _VARIANT.search(track.title):
        score -= 3.0

    return score


def _ydl_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        # Prefer AAC in m4a; see the module docstring. The chain still ends in a
        # plain `bestaudio` so an upload with no m4a rendition resolves rather
        # than failing, and stream.py reports the real mime either way.
        "format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # Never touch the filesystem: this container runs read-only as UID 10001
        # and we only ever want the metadata, not the bytes.
        "skip_download": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 15,
        # One retry only. A song that will not resolve should fail fast and fall
        # back to the preview, not hold a worker for a minute.
        "retries": 1,
        "extractor_retries": 1,
        "ignoreerrors": False,
        "geo_bypass": True,
        # Skip the formats that need an extra round trip and that we never pick.
        "extractor_args": {"youtube": {"skip": ["dash", "translated_subs"]}},
        "user_agent": config.USER_AGENT,
    }
    if config.COOKIES_FILE:
        options["cookiefile"] = config.COOKIES_FILE
    if config.SCRAPER_PROXY:
        options["proxy"] = config.SCRAPER_PROXY
    return options


def _search_sync(query: str) -> list[dict[str, Any]]:
    """Flat search: titles, durations and ids only, no per-video extraction."""
    options = _ydl_options() | {"extract_flat": "in_playlist"}
    with YoutubeDL(options) as ydl:
        payload = ydl.extract_info(f"ytsearch{SEARCH_RESULTS}:{query}", download=False)
    entries = (payload or {}).get("entries") or []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("id")]


def _extract_sync(video_id: str) -> dict[str, Any] | None:
    with YoutubeDL(_ydl_options()) as ydl:
        return ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )


def _pick_format(info: dict[str, Any]) -> dict[str, Any] | None:
    """The audio-only format yt-dlp settled on, or the best one we can find.

    ``requested_formats`` appears when a format selector resolved to a
    video+audio pair; for an audio-only selector the top level already carries
    the url. The manual scan is the fallback for an extractor that populated
    neither, and it re-applies the m4a preference rather than taking whatever
    sorts first.
    """
    if info.get("url") and info.get("acodec") not in (None, "none"):
        return info
    for candidate in info.get("requested_formats") or []:
        if candidate.get("acodec") not in (None, "none"):
            return candidate

    audio_only = [
        fmt
        for fmt in info.get("formats") or []
        if fmt.get("url")
        and fmt.get("acodec") not in (None, "none")
        and fmt.get("vcodec") in (None, "none")
    ]
    if not audio_only:
        return None
    audio_only.sort(
        key=lambda fmt: (
            fmt.get("ext") == "m4a" or str(fmt.get("acodec") or "").startswith("mp4a"),
            float(fmt.get("abr") or 0),
        ),
        reverse=True,
    )
    return audio_only[0]


def _resolve_sync(track: Track, provider_id: str) -> ResolvedSource | None:
    """The whole synchronous pipeline. Runs in a worker thread."""
    video_id = provider_id
    if not video_id:
        candidates = _search_sync(track.search_query)
        if not candidates:
            return None
        best = max(candidates, key=lambda entry: _score(entry, track))
        if _score(best, track) <= 0:
            # Everything the search returned looks like a different recording.
            # Better to fall back to the 30s preview than to play the wrong song.
            return None
        video_id = str(best["id"])

    info = _extract_sync(video_id)
    if not info:
        return None
    chosen = _pick_format(info)
    if not chosen or not chosen.get("url"):
        return None

    # The CDN checks these on the media request; re-guessing them in the relay is
    # how you get a 403 that looks like an expired URL.
    headers = dict(chosen.get("http_headers") or info.get("http_headers") or {})
    headers.setdefault("User-Agent", config.USER_AGENT)

    ext = str(chosen.get("ext") or "")
    mime = {
        "m4a": "audio/mp4",
        "mp4": "audio/mp4",
        "webm": "audio/webm",
        "opus": "audio/ogg",
        "mp3": "audio/mpeg",
    }.get(ext, "audio/mpeg")

    return ResolvedSource(
        url=str(chosen["url"]),
        provider=name,
        provider_id=video_id,
        mime=mime,
        bitrate=int(float(chosen.get("abr") or 0)),
        duration=int(info.get("duration") or track.duration or 0),
        headers=headers,
        expires_at=time.time() + config.SOURCE_TTL,
    )


async def resolve(track: Track, provider_id: str = "") -> ResolvedSource | None:
    """Find playable audio for ``track``. ``None`` is an ordinary outcome."""
    if not (config.SCRAPER_ENABLED and AVAILABLE):
        return None
    async with _gate():
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_resolve_sync, track, provider_id),
                timeout=config.SCRAPER_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print(f"[music] scraper timeout for {track.key} ({track.search_query!r})")
            return None
        except (DownloadError, ExtractorError) as err:
            # Age gates, region blocks, removed videos — expected, not a fault.
            print(f"[music] scraper could not resolve {track.key}: {err}")
            return None
        except Exception as err:  # noqa: BLE001
            # yt-dlp raises a wide variety on a bad day; one failing track must
            # never take the request down with it.
            print(f"[music] scraper error for {track.key}: {err!r}")
            return None
