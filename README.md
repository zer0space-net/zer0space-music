# zer0space Music

The music player of the zer0space homelab. A Spotify-shaped web player, served
at **`zer0space.com/music`**, gated by the zer0space dashboard session.

Mostly Python: FastAPI + Jinja2 + vanilla JS, no framework and no build step —
the same stack as [`zer0space-dashboard`](https://github.com/zer0space-net/zer0space-dashboard),
so the two are read and operated the same way.

> **This repository is public.** No credential, token or cookie file belongs in
> it. Everything sensitive is a Docker Swarm secret; `.env.example` holds
> placeholders only. See [docs/deploy.md](docs/deploy.md).

---

## What it does

| | |
|---|---|
| **Catalogue** | Search, albums, artists, charts and editorial playlists from Deezer's public API — no account, no API key. |
| **Audio** | Resolved per track by a scraper (yt-dlp → YouTube Music), relayed through this service, never linked directly. |
| **Library** | Liked songs, your own playlists, recently played, top artists — per zer0space user, in PostgreSQL. |
| **Player** | Queue, shuffle, repeat (off/all/one), seeking, volume, keyboard shortcuts, a queue drawer. |
| **Mobile** | **Background playback with lock-screen controls** — see [docs/background-playback.md](docs/background-playback.md). |
| **Continuity** | What you were playing is remembered, so the phone picks up where the desktop stopped. |
| **Look** | Spotify's layout in zer0space's design language — and the accent **follows whatever theme you picked in the dashboard**, automatically. |
| **Languages** | German and English, switchable at runtime, shared with the dashboard's own toggle. |

## How it fits together

```
        browser  ──►  zer0space-dashboard  ──►  zer0space-music  ──►  Deezer API
     (one origin)      /music gateway            (no ports)        (metadata, covers)
                       session gate                   │
                       + service token                └──────────►  yt-dlp ──► YouTube
                                                                     (audio URL)
                                                  audio bytes flow back the same way
```

* The dashboard checks the zer0space session, then forwards every `/music/*`
  request with a shared **service token** and an **`X-Zer0space-User`** header.
* This service has **no login and no accounts of its own**, publishes **no
  ports**, and trusts that header *only* when the token is presented with it.
* Resolved audio URLs are **bound to this host's IP**, so they are never handed
  to the browser. The player gets a signed, expiring **ticket** instead, and
  this service relays the bytes.

Full reasoning in [docs/architecture.md](docs/architecture.md).

## Background playback on a phone

The headline feature, and the part that is easiest to get wrong. Audio keeps
playing when the screen locks, and the lock screen shows the real title, artist
and cover with working play / pause / skip / scrub.

Four rules make that work, all enforced in `static/js/player.js`:

1. **One `<audio>` element**, created in the HTML and never replaced — only its
   `src` changes. A new `Audio()` per track drops the OS media session and iOS
   then refuses to start the next song while backgrounded.
2. **No Web Audio API.** An `AudioContext` makes iOS classify the page as "web
   audio", which is suspended in the background. A plain media element is kept
   alive.
3. **MediaSession metadata set on every track change**, before `play()`.
4. **`setPositionState()` on every tick**, or the lock-screen scrubber is dead.

Plus a PWA manifest (`display: standalone`) so it can be installed to the home
screen. The full account, including what does *not* work and why, is in
[docs/background-playback.md](docs/background-playback.md).

## Layout

```
src/
├── config.py          Environment + Swarm secrets, resolved once at import
├── db.py              asyncpg pool, idempotent schema, query helpers
├── identity.py        Who is calling — the service token + user header gate
├── cache.py           Catalogue cache (never caches an empty result)
├── library.py         Likes, playlists, history, player state, preferences
├── resolve.py         Track -> playable source: cache, dedupe, fallback
├── stream.py          Signed tickets + the Range-aware audio relay
├── playlist_import.py Spotify playlist -> matched against our own catalogue
├── providers/
│   ├── base.py        Track/Album/Artist shapes; the SourceProvider contract
│   ├── deezer.py      The catalogue (public API, no key)
│   ├── ytmusic.py     The scraper (yt-dlp), matching and format choice
│   └── spotify.py     Reads a public playlist link, no API key (see docs/providers.md)
└── main.py            FastAPI app: routes, lifespan, error handling
static/
├── css/music.css      Spotify's grid on the dashboard's design tokens
├── js/  boot (theme) -> starfield -> i18n -> api -> player -> app
├── sw.js              Service worker: app shell only, never audio
└── img/               App icons
templates/app.html     The one page; every view is rendered client-side
docs/                  architecture / background-playback / providers / deploy
```

## Running it locally

Needs Python 3.12. PostgreSQL is optional — without it the catalogue and
playback still work, only likes and playlists are unavailable.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# No service token set = the user header is trusted from anyone.
# Fine on a laptop, never on the cluster. The boot log says so loudly.
MUSIC_BASE_PATH= DB_HOST=127.0.0.1 \
  .venv/bin/python -m uvicorn src.main:app --port 8000
```

Then send the identity yourself, since there is no dashboard in front:

```bash
curl -H 'X-Zer0space-User: 1' http://127.0.0.1:8000/api/home
```

For the UI, put a two-line proxy in front that adds that header — the browser
cannot. See [docs/deploy.md](docs/deploy.md#local-development).

## Deploying

Built by GitHub Actions to `ghcr.io/zer0space-net/zer0space-music:latest`,
deployed as a Portainer stack, reverse-proxied by the dashboard at `/music`.
The full runbook, including the three secrets and the dashboard-side settings,
is in **[docs/deploy.md](docs/deploy.md)**.

## If nothing plays any more

Check **`yt-dlp` first**. It tracks a moving target: when YouTube changes its
player an outdated pin stops resolving *every* track at once, with nothing in
this repo's diff to explain it. A weekly workflow opens an issue when a newer
release exists.

```bash
sed -i 's/^yt-dlp==.*/yt-dlp==<new>/' requirements.txt
# then, after CI is green:
docker service update --force --image ghcr.io/zer0space-net/zer0space-music:latest music_music
```

More symptoms and their causes in [docs/providers.md](docs/providers.md#when-playback-breaks).

## A note on sources

The catalogue is Deezer's public metadata API. Audio is resolved with
[yt-dlp](https://github.com/yt-dlp/yt-dlp) against publicly reachable YouTube
streams — the same mechanism every self-hosted music server uses, and the reason
no account is needed anywhere in this stack. That is a personal-use homelab
arrangement, not a redistribution service: it is gated behind the zer0space
login, and nothing is stored to disk. If a track cannot be resolved, the player
falls back to Deezer's own 30-second preview rather than failing.

The source layer is a [documented interface](src/providers/base.py) — a
local-library scanner is a `SourceProvider` and nothing in the UI would change.
