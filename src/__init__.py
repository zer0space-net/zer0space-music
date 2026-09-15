"""zer0space Music — the homelab music service.

Serves a Spotify-shaped player at ``/music``, gated by the zer0space dashboard.
Catalogue metadata comes from Deezer's public API, audio is resolved by the
scraper in :mod:`src.providers.ytmusic` and relayed by :mod:`src.stream`.
"""

__version__ = "1.0.0"
