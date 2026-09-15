"""Catalogue and audio-source providers.

``deezer`` is the catalogue (what a track is); ``ytmusic`` is the scraper (how
to play it). See :mod:`src.providers.base` for why those are separate.
"""

from . import base, deezer, ytmusic

__all__ = ["base", "deezer", "ytmusic"]
