"""zer0space Music — the homelab music service.

Serves a Spotify-shaped player at ``/music``, gated by the zer0space dashboard.
Catalogue metadata comes from Deezer's public API, audio is resolved by the
scraper in :mod:`src.providers.ytmusic` and relayed by :mod:`src.stream`.
"""

# Bump this whenever static/js or static/css changes. templates/app.html
# appends it as ?v= on every asset URL — the cache key the service worker
# (static/sw.js) actually keys its stale-while-revalidate cache on — so a
# forgotten bump here is a deploy that lands on the server but not in any
# tab that already has the app open. This was unused dead weight in the
# template until a real user hit exactly that: literal "nav.podcasts" text
# and the wrong view, both stale JS served from before those existed.
__version__ = "1.1.1"
