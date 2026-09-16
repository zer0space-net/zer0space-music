"""Fetches and parses one RSS feed. Nothing here knows about subscriptions,
users or caching — that is :mod:`src.podcasts`. This module's only job is
turning feed bytes into :class:`Episode` objects, the same separation
``deezer.py`` keeps between "talk to the API" and "what the app does with it".

Stdlib ``xml.etree.ElementTree`` rather than a feed-parsing library: RSS 2.0
plus the itunes namespace extension (duration, image, summary) covers what
every podcast host in practice emits, and this project already avoids a
dependency where a stdlib module does the job (see spotify.py's regex+json
instead of an HTML parser).
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from .. import config

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0),
            headers={"User-Agent": config.USER_AGENT},
            follow_redirects=True,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


class FeedUnavailable(RuntimeError):
    """The feed could not be fetched, or does not parse as RSS."""


_ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
# Older shows can run to hundreds of daily episodes; the app only ever needs
# "recent enough to pick something to listen to", and RSS feeds are
# conventionally newest-first already.
MAX_EPISODES = 200


@dataclass(frozen=True)
class Episode:
    # sha1 of the feed's <guid> (or the audio URL, if a feed omits guid) —
    # stable across re-fetches, short enough to live inside a track key.
    guid_hash: str
    title: str
    description: str
    published: str
    duration: int
    audio_url: str
    mime: str
    cover: str


@dataclass(frozen=True)
class Feed:
    title: str
    description: str
    cover: str
    episodes: list[Episode] = field(default_factory=list)


def _text(el: ET.Element, tag: str) -> str:
    node = el.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


def _duration_seconds(raw: str) -> int:
    """itunes:duration is either plain seconds ("1830") or HH:MM:SS / MM:SS."""
    raw = (raw or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)
    try:
        parts = [int(p) for p in raw.split(":")]
    except ValueError:
        return 0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _parse_item(item: ET.Element, channel_cover: str) -> Episode | None:
    title = _text(item, "title")
    enclosure = item.find("enclosure")
    audio_url = enclosure.get("url") if enclosure is not None else ""
    if not title or not audio_url:
        return None  # not every <item> in a feed is a playable episode

    guid_node = item.find("guid")
    guid = (guid_node.text or "").strip() if guid_node is not None and guid_node.text else audio_url
    image = item.find(f"{_ITUNES_NS}image")
    cover = (image.get("href") if image is not None else "") or channel_cover

    return Episode(
        guid_hash=hashlib.sha1(guid.encode("utf-8")).hexdigest()[:16],
        title=title[:300],
        description=(_text(item, f"{_ITUNES_NS}summary") or _text(item, "description"))[:2000],
        published=_text(item, "pubDate"),
        duration=_duration_seconds(_text(item, f"{_ITUNES_NS}duration")),
        audio_url=audio_url,
        mime=(enclosure.get("type") if enclosure is not None else "") or "audio/mpeg",
        cover=cover[:500],
    )


async def fetch_image(url: str) -> tuple[bytes, str] | None:
    """Raw bytes of a cover image URL from a feed, or ``None``.

    Relayed same-origin rather than hotlinked: unlike Deezer's cover art,
    which always comes from ``*.dzcdn.net`` (one CSP ``img-src`` entry
    covers it), a podcast's artwork comes from whatever host that show's
    feed happens to be hosted on — a different domain per subscription,
    which an allowlist cannot practically keep up with.
    """
    if not url:
        return None
    try:
        response = await client().get(url)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if not content_type.startswith("image/"):
        return None
    return response.content, content_type


async def fetch_feed(feed_url: str) -> Feed:
    try:
        response = await client().get(feed_url)
        response.raise_for_status()
    except httpx.HTTPError as err:
        raise FeedUnavailable(f"Could not fetch the feed: {err}") from err

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as err:
        raise FeedUnavailable(f"Not a valid RSS feed: {err}") from err

    channel = root.find("channel")
    if channel is None:
        raise FeedUnavailable("Not a valid RSS feed (no <channel>)")

    image_url_node = channel.find("image/url")
    itunes_image = channel.find(f"{_ITUNES_NS}image")
    cover = (
        (itunes_image.get("href") if itunes_image is not None else "")
        or (image_url_node.text if image_url_node is not None and image_url_node.text else "")
        or ""
    )

    episodes = [
        episode
        for episode in (_parse_item(item, cover) for item in channel.findall("item"))
        if episode is not None
    ]
    if not episodes:
        raise FeedUnavailable("This feed has no playable episodes")

    return Feed(
        title=(_text(channel, "title") or "Untitled podcast")[:200],
        description=_text(channel, "description")[:2000],
        cover=cover[:500],
        episodes=episodes[:MAX_EPISODES],
    )
