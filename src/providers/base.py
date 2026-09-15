"""The shapes every provider speaks, and the contract a source provider signs.

The catalogue side (what you browse) and the audio side (what actually plays)
are deliberately separate interfaces. Deezer knows everything about a track
except how to play it; the scraper knows how to play a track and almost nothing
about it. Keeping them apart is what lets either be swapped — a local-library
scanner is a ``SourceProvider``, and nothing in the UI has to know.

Every dict that reaches the browser is built by one of the ``as_dict`` methods
here, so the frontend has exactly one track shape to render.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


def _clean(value: Any) -> str:
    return (str(value) if value is not None else "").strip()


@dataclass(frozen=True)
class Artist:
    id: str
    name: str
    picture: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "picture": self.picture}


@dataclass(frozen=True)
class Album:
    id: str
    title: str
    cover: str = ""
    artist: Artist | None = None
    year: str = ""
    track_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "cover": self.cover,
            "artist": self.artist.as_dict() if self.artist else None,
            "year": self.year,
            "trackCount": self.track_count,
        }


@dataclass(frozen=True)
class Track:
    """One playable thing.

    ``key`` is the stable identity used everywhere — as a primary key in
    ``liked_track`` / ``playlist_track`` / ``track_source``, and as the id the
    player asks to stream. It is ``"<provider>:<id>"`` so two catalogues can
    coexist without their ids colliding.
    """

    key: str
    title: str
    artist: Artist
    album: Album | None = None
    duration: int = 0
    cover: str = ""
    preview: str = ""
    explicit: bool = False
    isrc: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "artist": self.artist.as_dict(),
            "album": self.album.as_dict() if self.album else None,
            "duration": self.duration,
            "cover": self.cover or (self.album.cover if self.album else ""),
            "explicit": self.explicit,
            # `preview` is Deezer's 30-second clip. It is NOT the stream URL —
            # the player asks for a ticket instead — but it is what the preview
            # fallback plays when the scraper comes up empty, and what makes a
            # hover-preview possible later.
            "hasPreview": bool(self.preview),
        }

    @property
    def search_query(self) -> str:
        """What the scraper looks for. Artist first: YouTube's own search ranks
        ``artist title`` far better than ``title artist`` for music."""
        return f"{self.artist.name} {self.title}".strip()


@dataclass(frozen=True)
class ResolvedSource:
    """A direct, playable audio URL plus what the relay needs to fetch it.

    ``headers`` matters more than it looks: several CDNs hand back a 403 unless
    the Referer and User-Agent match the ones used during extraction, so they
    travel with the URL instead of being re-guessed by the relay.
    """

    url: str
    provider: str
    provider_id: str = ""
    mime: str = "audio/mpeg"
    bitrate: int = 0
    duration: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    expires_at: float = 0.0


class SourceProvider(Protocol):
    """Turns a :class:`Track` into something the relay can stream."""

    name: str

    async def resolve(self, track: Track, provider_id: str = "") -> ResolvedSource | None:
        """Find playable audio for ``track``.

        ``provider_id`` is a previously matched id (e.g. a YouTube video id).
        When present the provider should reuse it and skip the search step — the
        match is stable, only the URL expires.

        Returns ``None`` when nothing was found. Raising is reserved for
        genuinely broken state; a track with no source is an ordinary outcome.
        """
        ...
