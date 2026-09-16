# CLAUDE.md — zer0space Music

Project context for Claude Code. Read this before changing anything in this repo.

## What this is

The music player of the zer0space homelab, served at `zer0space.com/music`,
gated by the dashboard's session. A Spotify-shaped UI over Deezer's public
catalogue, with audio resolved per track by a yt-dlp scraper and relayed by this
service.

It is the third app in the family, after `zer0space-dashboard` and
`zer0space-crimson-client`, and it deliberately copies the dashboard's stack and
conventions rather than inventing its own.

## THIS REPOSITORY IS PUBLIC

No credential, token, cookie file or connection string belongs in it.
`.env.example` holds placeholders. Everything real is a Docker Swarm secret,
resolved by `config.read_secret()` (secret file first, env var second). CI has a
grep that fails the build on a secret-shaped literal — do not work around it.

## Tech stack

| Layer | Choice |
|---|---|
| Runtime | Python 3.12 (alpine) |
| HTTP | FastAPI on Starlette, uvicorn, **2 workers** |
| Database | PostgreSQL via `asyncpg` — no ORM, plain parameterised SQL |
| HTTP out | `httpx` (async) |
| Scraper | `yt-dlp`, in a worker thread |
| Templates | Jinja2 — one template |
| Frontend | Vanilla JS, no framework, **no build step** |

`static/` is served as-is. Do not introduce a bundler or a framework without
being asked: the no-build property is what makes the served files identical to
the files in git.

There is **no test suite**. CI byte-compiles every module, imports the app,
parses the template, `node --check`s every script, checks de/en dictionary
parity, and greps for committed secrets. That is the whole safety net — if you
add non-trivial logic, say so rather than assuming it is covered.

## Seven things that are easy to break

### 1. The one `<audio>` element

`templates/app.html` has exactly one `<audio>`, and `static/js/player.js` only
ever assigns its `src`. **Never** create a new `Audio()` per track, and never
re-render the player bar. Doing either breaks background playback on iOS in a
way that does not reproduce on a desktop: the first song plays and the queue
stops at the end of it with the phone locked.

Related and equally load-bearing: no Web Audio API, MediaSession metadata set
before `play()`, `setPositionState()` on every tick. All four rules and their
reasoning are in `docs/background-playback.md`. **Read it before touching
`player.js`.**

### 2. Range requests

`src/stream.py` forwards `Range` upstream and returns 206 with `Content-Range`
and `Accept-ranges`. Safari probes with `Range: bytes=0-1` before committing to
a media URL and will not render a seek bar without range support. The dashboard
gateway's `_RESPONSE_ALLOW` must keep relaying those three headers, and
`Accept-Encoding: identity` must stay forced — a re-compressed body invalidates
the byte offsets Range is expressed in.

### 3. Identity is two locks, checked together

`src/identity.py` accepts `X-Zer0space-User` **only** when the shared service
token is presented with it. Never split those checks, never trust the header
because "only the dashboard can reach us", and never add a login here — identity
lives in the dashboard and a second copy is a second thing to get out of sync.

This is also why `docker-compose.yml` publishes **no ports**. That is not an
omission.

### 4. The cache rules

Both learned the expensive way on the Crimson backend:

- **Never cache an empty result.** One upstream blip otherwise freezes an empty
  home page for the whole TTL across every replica, and it looks like broken
  scrapers rather than a poisoned cache.
- **Cache the full pool, slice per caller.** Caching under a key that ignores
  `limit` stores an already-truncated list, so the first caller decides how many
  items everyone sees.

### 5. Every URL carries the gateway prefix

The app is mounted at `/music` by a reverse proxy. Server-side, read it from
`identity.base_path(request)` (the `X-Forwarded-Prefix` header); client-side,
from `window.ZS_BASE`. A root-relative `/api/...` resolves against the dashboard
instead. This is the same class of bug as the Crimson root-relative playlist
failure, and the PWA manifest's `start_url`/`scope` need it too.

### 6. Spotify import has no key to rotate — because it has no key at all

`src/providers/spotify.py` reads `open.spotify.com/embed/playlist/<id>`, the
public embed page, instead of the real Spotify Web API — no registered app, no
client id/secret. That also means it depends on the shape of a page Spotify
never promised to keep stable, same trade as yt-dlp against YouTube. If every
import starts failing at once, re-check the JSON path
(`props.pageProps.state.data.entity.trackList`) against a fresh fetch before
suspecting anything else. Full account, including the 100-track cap that page
itself imposes: `docs/providers.md`, "Spotify — playlist import".

### 7. A deploy is not live until `__version__` moves

`templates/app.html` appends `?v={{ version }}` (from `src/__init__.py`) to
every static JS/CSS URL, and `static/sw.js` caches `/static/` responses by
full request URL — so that query string is the only thing that makes a
deploy reach a tab that already has the app open. Forgetting to bump
`__version__` when JS or CSS changes means the new code sits on the server
correctly, but the service worker keeps serving the previous version's
cached response indefinitely (its own stale-while-revalidate refresh runs
against the *same* unversioned-if-you-forgot URL, so there is nothing to
invalidate the entry with). Bump it in the same commit as the change, not
after — "it looks deployed but the browser is still running yesterday's
code" is a hard bug to recognise from the symptoms alone.

## Database

PostgreSQL on **zs-state-01 (192.168.0.16:5432)**, database `zer0space_music`,
user `dashboard` — its own database, sharing the dashboard's role and its
`db_password` secret.

- Schema is created on start with `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF
  NOT EXISTS`. No migration framework; changes go in `SCHEMA` in `db.py` and
  must stay backwards compatible.
- Statements run **one at a time**, not as one multi-statement string — a batch
  runs in an implicit transaction and one failure would silently roll back the
  rest.
- Everything is async and parameterised (`$1`, `$2`). Never concatenate SQL.
- `db.py` converts every connection-level failure into `DatabaseUnavailable`
  before it leaves the module. Do **not** register a handler for `OSError`.
- **A database outage must not stop the music.** `cache.py` and the `_stored` /
  `_remember` helpers in `resolve.py` swallow `DatabaseUnavailable` and report a
  miss. Likes and playlists legitimately 503; browsing and playback do not.

Per-user rows are keyed by the dashboard's user id as `TEXT`. There is no users
table here on purpose.

## Internationalisation

German and English, same contract as the dashboard, sharing the same
`localStorage` key (`zs-lang`) so switching in either app switches both.

When you add or change a user-facing string, three places move together:

1. The key goes in **both** the `de` and `en` dictionaries — CI fails on drift.
2. In markup, use `data-i18n` / `-ph` / `-title` / `-aria`, with the German text
   inline as the pre-JS default.
3. In JavaScript call `t('key')`, never a literal.

Server messages are **not** translated server-side: every error carries a stable
`code` and the client resolves it to `err.<CODE>`. A new error response needs a
code and a matching key in both dictionaries.

Views built in JavaScript carry no `data-i18n` attributes, so `applyI18n()`
cannot reach them — they are re-rendered by the `languagechange:zs` listener in
`app.js`. Extend it if you add another JS-rendered view.

## Design

Spotify's **layout**, the dashboard's **colours**. `static/css/music.css` copies
the dashboard's tokens verbatim at the top; if those change, update these.

The accent follows the dashboard automatically: `boot.js` reads the dashboard's
own `localStorage['zs-theme']`, which works because the gateway serves this app
from the same origin. Do not replace that with an API call or a query parameter.

`[hidden] { display: none !important }` is declared before anything sets a
`display` — several things here are toggled with `el.hidden`, and any rule
setting `display` outranks the UA's `[hidden]` rule. Without it the preview
badge and the queue drawer are permanently visible.

## The scraper

`src/providers/ytmusic.py`. See `docs/providers.md` for the scoring table and
the format reasoning. The short version:

- **m4a is preferred over Opus deliberately** — Safari cannot play Opus, and
  iOS is the platform whose background playback is the point.
- **Duration is the match signal**, not the title.
- Never return a bad match: if the best candidate scores ≤ 0, return `None` and
  let the preview fallback handle it. Better no song than the wrong song.
- Extractions are serialised through a small semaphore — YouTube rate-limits
  per IP.

**`yt-dlp` is the one dependency that does not degrade gracefully.** A stale pin
stops resolving *everything* at once, with nothing in the diff to explain it.
If playback breaks across the board, bump it first.

## Deployment

Built by GitHub Actions to `ghcr.io/zer0space-net/zer0space-music:latest` and
deployed as a Portainer stack. **The workflow uses `secrets.GITHUB_TOKEN`, not
`CR_PAT`** — `CR_PAT` is a repository secret on `zer0space-dashboard` only and
resolves to an empty string here.

There is no clone on the cluster nodes, so `docker build` there does nothing.
Env changes go in the Portainer stack UI. Full runbook in `docs/deploy.md`.

The gateway half lives in `zer0space-dashboard`: `src/music.py`, the
`MUSIC_ENABLED` route block in `src/main.py`, and the sidebar entry in
`templates/dashboard.html`. Both halves must be deployed for anything to work.
