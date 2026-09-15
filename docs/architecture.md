# Architecture

Why this service is shaped the way it is. For how to run it, see
[deploy.md](deploy.md); for the scraper specifically, [providers.md](providers.md);
for the mobile audio rules, [background-playback.md](background-playback.md).

## The split: catalogue and source are different things

The single most important decision here. **Deezer says what a song is; the
scraper finds something that plays it.** They are separate interfaces
([`providers/base.py`](../src/providers/base.py)) and neither knows about the
other.

|  | Catalogue (`deezer.py`) | Source (`ytmusic.py`) |
|---|---|---|
| Answers | title, artist, album, duration, cover, ISRC | a direct audio URL |
| Needs an account | no | no |
| Stable | yes — ids never change | no — URLs expire in hours |
| If it fails | the app cannot browse | one track falls back to a preview |

Why not one provider for both:

* **Spotify's API needs a registered app** (client id + secret), i.e. an
  account. Deezer's `api.deezer.com` needs nothing at all and returns richer
  browse data than YouTube Music does — proper album objects, artist pages,
  charts, four cover sizes and an ISRC per track.
* **YouTube has the audio.** Essentially everything, including the long tail
  Deezer's own streaming would not cover.

The seam also means the audio side is replaceable. A local-library scanner over
a NAS mount is a `SourceProvider` and nothing in the UI changes.

## Identity: two locks, never one

This service has **no accounts, no login, no session, and publishes no ports**.
The dashboard checks the zer0space session and then tells us who is calling:

```
Authorization: Bearer <MUSIC_SERVICE_TOKEN>   ← the lock
X-Zer0space-User: 42                          ← the claim
```

[`identity.py`](../src/identity.py) checks **both together**, always. Trusting
the header because "only the dashboard can reach us" is the assumption that
stops holding the first time the network layout changes, and the token is what
makes the header meaningful.

The gateway side ([`zer0space-dashboard/src/music.py`](https://github.com/zer0space-net/zer0space-dashboard/blob/main/src/music.py))
strips any client-supplied copy of either header — in **every** letter-casing,
since HTTP header names are case-insensitive but a dict is not — before setting
its own. Leaving one through is how a viewer reaches another user's playlists.

There is deliberately no users table here. Identity lives in the dashboard; a
second copy would be a second thing to get out of sync.

## Where the bytes go

A resolved audio URL is **bound to the IP that resolved it**, exactly like the
CDN tokens in the Crimson backend. Two consequences:

1. It cannot be given to the browser — a phone on mobile data gets a 403.
2. It is a bare capability; anyone holding it can pull the audio with no auth.

So the player never sees one. It asks `/api/stream/<key>` and receives a signed,
expiring **ticket**; the `<audio>` element points at `/music/media/<ticket>`, and
[`stream.py`](../src/stream.py) relays the bytes from this container.

```
phone ──► dashboard ──► music ──► googlevideo CDN
          (session)     (ticket)   (IP-bound URL)
      ◄── audio bytes, 206 Partial Content, all the way back ───
```

**Audio goes through the Cloudflare tunnel**, chosen deliberately. Crimson keeps
media off the tunnel for ToS §2.8 reasons, but audio is roughly 1/50 of video's
bitrate and the alternative (a Tailscale-only media host) means no music away
from home, which defeats the point of the phone support. `MUSIC_MEDIA_BASE_URL`
moves the media relay to a direct host without moving the API, if that trade
ever needs revisiting.

The ticket is **not** re-checked against the dashboard session — see
[background-playback.md](background-playback.md#why-the-audio-src-is-a-ticket)
for why that would break background playback.

## Caching, and the two rules it obeys

Both were learned the expensive way on the Crimson backend, and both are
re-stated in [`cache.py`](../src/cache.py) so they are not undone by accident:

**Never cache an empty result.** One upstream blip otherwise freezes an empty
home page in place for the whole TTL across every replica — and it looks exactly
like broken scrapers rather than a poisoned cache entry.

**Cache the full pool, slice per caller.** Caching under a key that ignores the
caller's `limit` stores an already-truncated list, so whichever caller asked
first decides how many items everyone else sees.

Three layers, with different lifetimes:

| Layer | Table | TTL | Holds |
|---|---|---|---|
| Catalogue | `catalog_cache` | 6 h (searches 30 min, genres 7 d) | Deezer responses |
| Track metadata | `catalog_cache` | 7 d | one track, for the stream path |
| Resolved source | `track_source` | 4 h for the URL, **forever for the match** | yt-dlp output |

The third row is the interesting one. A YouTube video id for a track is stable;
only the URL expires. Keeping the id past the URL's expiry turns a re-resolve
from *search + extract* into *extract*, which is the difference between four
seconds and one.

## Degrading rather than failing

A PostgreSQL outage must not stop the music. `cache.get/put` and
`resolve._stored/_remember` swallow `DatabaseUnavailable` and report a miss, so
browsing and playback keep working — the cost is that nothing is memoised while
the database is away. Likes and playlists do return 503, because there is no
honest way to serve them.

Similarly: a track that will not resolve falls back to Deezer's 30-second
preview and the player says so, rather than ending the listening session. And in
a 50-song playlist, a track with no source at all advances the queue instead of
stalling it.

## Concurrency

* **yt-dlp is synchronous** and does real network I/O, so it runs in a worker
  thread (`asyncio.to_thread`) behind a semaphore of `MUSIC_SCRAPER_WORKERS`.
  Small on purpose: YouTube rate-limits per IP, and a burst of parallel
  extractions is the fastest way to get this host throttled. (The Crimson
  FlareSolverr work learned the same lesson the hard way.)
* **Concurrent requests for the same track share one extraction**, via an
  in-flight task map in `resolve.py`. Without it, pressing play on a track the
  queue prefetch is already resolving starts a second scrape.
* **Two uvicorn workers**, unlike the dashboard's one. This service holds no
  in-process state — identity arrives per request, everything per-user is in
  Postgres — so a second worker is free, and it keeps a slow extraction from
  blocking unrelated requests.

## The frontend

Vanilla JS, no framework, no bundler, no build step — the served files are the
files in git, same as the dashboard. Load order matters:

```
boot (theme) → starfield → i18n → api → player → app
```

Two things worth knowing:

**The accent follows the dashboard automatically.** `boot.js` reads
`localStorage['zs-theme']` — the dashboard's own key. Because the gateway serves
this app from the *same origin*, that storage is shared. Pick a colour in the
dashboard, come back, and Music is already wearing it. No API call, no query
parameter, no schema change. A `storage` event listener picks up a change made
in another open tab.

**The player bar is outside the view.** `app.js` re-renders `#view` on every
route change, and the `<audio>` element must survive that — see rule 1 in
[background-playback.md](background-playback.md).
