# The catalogue and the scraper

Everything about where music comes from. See [architecture.md](architecture.md)
for why the two are separate.

---

## Deezer — the catalogue

[`src/providers/deezer.py`](../src/providers/deezer.py). Public, unauthenticated,
no key and nothing to rotate.

| Used for | Endpoint |
|---|---|
| Search | `/search/{track,album,artist,playlist}` |
| Home | `/chart` |
| Album page | `/album/{id}` |
| Artist page | `/artist/{id}` + `/top`, `/albums`, `/related` |
| Editorial playlists | `/playlist/{id}` |
| Genres | `/genre`, `/genre/{id}/artists` |
| One track (stream path) | `/track/{id}` |

Two things this module is careful about:

**Deezer reports errors with HTTP 200.** A quota or a bad id comes back as
`200 {"error": {...}}`, so a plain `raise_for_status()` would let a quota
message through as data — and it would then be cached as a valid empty result.
`_get()` turns the in-band envelope into an exception.

**Search runs four lanes in parallel, and tolerates partial failure.** If the
album lane errors, the track lane still renders; only a total failure is
escalated. Four separate calls rather than Deezer's combined endpoint because
the combined one returns tracks only, and a search result that shows the artist
and the albums is what makes it feel like Spotify's rather than a flat list.

A note on results: searching the full phrase `daft punk get lucky` legitimately
returns **no artists** — no artist is named that. Searching `daft punk` returns
them. That is correct behaviour, not a bug.

Track keys are `deezer:<id>`, and that string is the primary key everywhere —
`liked_track`, `playlist_track`, `track_source`, and the id the player asks to
stream. The prefix exists so a second catalogue could coexist without id
collisions.

---

## yt-dlp — the scraper

[`src/providers/ytmusic.py`](../src/providers/ytmusic.py). Takes a catalogue
track, finds a YouTube video of it, and returns a direct audio URL.

### The pipeline

```
Track ──► ytsearch5:"<artist> <title>" ──► score candidates ──► pick best
                                                                    │
      ResolvedSource ◄── choose audio format ◄── extract that video ◄┘
```

`artist title`, in that order: YouTube's own search ranks it far better for
music than `title artist`.

### Scoring — why not just take the first result

`_score()` is additive and deliberately readable; it is the function to print
when a track resolves to the wrong song.

| Signal | Weight | Reasoning |
|---|---|---|
| **Duration within 2 s** | +6 | The strongest signal by far — it is exact on both sides. Deezer already told us the length. |
| Duration within 8 s | +4 sliding | Same recording, different encode or a trailing silence. |
| Duration off by >30 s | −4 | A different recording: a live version, an extended mix, a reaction video. |
| Title token overlap | up to +3 | After stripping upload noise (`(Official Video)`, `[HD]`, `Lyric Video`…). |
| Artist in title or channel | up to +2 | |
| Channel ends in `- Topic` | +2.5 | YouTube's auto-generated music uploads: the label's own audio, no video track, no intro. The best possible match. |
| Unrequested variant | −3 | `live`, `remix`, `cover`, `karaoke`, `sped up`, `nightcore`, `8d`… — when the catalogue title did not ask for it. |

Anything longer than 15 minutes is rejected outright (full-album uploads and
DJ sets match track titles happily). If the best candidate still scores ≤ 0,
the resolver returns nothing and the player falls back to the 30-second
preview — **better no song than the wrong song**.

### Format choice: m4a over opus, on purpose

```python
"format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"
```

YouTube's *best* audio is usually Opus in a WebM container. **Safari cannot play
it** — and Safari is every browser on iOS, the platform whose background
playback is a headline feature of this service. A format the phone cannot decode
is not higher quality, it is silence. AAC in m4a plays everywhere.

The chain still ends in a plain `bestaudio` so an upload with no m4a rendition
resolves rather than failing, and `stream.py` reports the true mime either way.

### Caching the match separately from the URL

A YouTube video id for a track is stable forever; the URL it yields expires in
about six hours and is IP-bound. `track_source` therefore keeps `provider_id`
past `expires_at`, and a re-resolve reuses it — one extraction (~1 s) instead of
a search plus an extraction (~3–4 s).

If a remembered id then fails, it is retried once from scratch: the video may
have been taken down, which is a different failure from a stale URL.

### Rate limiting and failure handling

* Extractions are serialised through a semaphore (`MUSIC_SCRAPER_WORKERS`,
  default 2). YouTube rate-limits per IP.
* One retry, then fail fast. A song that will not resolve should fall back to
  the preview, not hold a worker for a minute.
* After `MAX_FAILURES` (3) consecutive failures a track goes into a one-hour
  cooldown and serves the preview directly, so a region-blocked song does not
  cost a 45-second timeout every time a playlist reaches it.
* Age gates, region blocks and removed videos are logged and return `None` —
  expected outcomes, not faults.

### Optional configuration

| Variable | For |
|---|---|
| `MUSIC_COOKIES_FILE` | A Netscape `cookies.txt` to lift age gates and reduce bot checks. **Never commit one** — it is a live credential for a Google account, and this repo is public. Mount it as a Swarm secret, read-only. |
| `MUSIC_SCRAPER_PROXY` | An upstream proxy for the scraper only (not Deezer, not the media relay), if this host's IP gets rate-limited. |
| `MUSIC_SCRAPER_ENABLED=false` | Turns the scraper off entirely: a browsable catalogue that plays only 30-second previews. |

---

## Spotify — playlist import, not a catalogue source

[`src/providers/spotify.py`](../src/providers/spotify.py) +
[`src/playlist_import.py`](../src/playlist_import.py). Reads a public Spotify
playlist and creates the matching playlist here, resolved entirely against
Deezer — Spotify is never used to browse or play anything.

**No registered app, no client id/secret.** Spotify's real Web API needs one;
this instead reads the same JSON a browser gets rendering
`https://open.spotify.com/embed/playlist/<id>` — the page Spotify serves
specifically for embedding a playlist in someone else's site, unauthenticated.
That is also the whole risk: this depends on a page Spotify never promised to
keep stable, exactly the trade already made with yt-dlp against YouTube (see
below). If import starts failing for every playlist at once, the JSON path in
`spotify.fetch_playlist` — `props.pageProps.state.data.entity` — is the first
thing to re-check against a fresh fetch of that URL.

### The pipeline

```
paste a link ─► extract the 22-char playlist id ─► GET the embed page
                                                          │
     one playlist here ◄── add matched tracks ◄── score N candidates each
                                                          │
                                              search Deezer, per Spotify track
                                              (bounded concurrency, MATCH_CONCURRENCY=8)
```

### Scoring

Same additive shape as `ytmusic._score`, but title and artist both carry full
weight rather than one being a fallback for the other — Deezer's `title` and
`artist.name` are structured fields, not a noisy upload title, so there is no
YouTube-style "strip `(Official Video)`" step needed.

| Signal | Weight |
|---|---|
| Duration within 2 s | +6 |
| Duration within 10 s | +4 sliding |
| Duration off by >30 s | −2.5 (a penalty, not a hard reject) |
| Title token overlap | up to +3 |
| Artist name overlap | up to +3 |

Wider duration tolerance than `ytmusic._score`'s 8s on purpose: that one
matches a catalogue duration against a YouTube upload of the *same* master,
this matches an external title against Deezer, which not infrequently
indexes a different edit (radio edit vs. album version) as "the" track.

`_best_match` also runs a second pass — a title-only search — whenever the
"artist title" query comes back empty or scores ≤ 0 against everything it
found. A mismatched artist string (feat. order, "&" vs "and", a collab
credited differently between services, or no artist at all from a
hand-written tracklist) was silently losing tracks Deezer does have.

A candidate scoring ≤ 0 even after both passes is reported as unmatched
rather than added — see the module docstring in `playlist_import.py`. The
unmatched list is shown to the user after import, not just counted.

### The 100-track cap is Spotify's, not ours

The embed page's `trackList` does not paginate and exposes no total distinct
from its own length. Tested against a 50-track and a 100-track playlist while
building this: the first came back complete, the second came back at exactly
100 with no signal of how much longer the real playlist is. `MAX_TRACKS = 100`
in `spotify.py` and `SpotifyPlaylist.truncated` just names that ceiling rather
than pretending it is not there — a playlist import can only ever see what
this page shows it.

### No secret to rotate, nothing to configure

Unlike the scraper, there is no `MUSIC_SPOTIFY_*` setting and nothing in
`docker-compose.yml` — the feature works or it does not, per-request, with
nothing an operator needs to set up first.

### Past the 100-track cap: a title/artist list instead of a Spotify link

`playlist_import.import_track_list` (in the same module) is the escape hatch
for the ceiling above — it runs the exact same Deezer matching pass, just
against a list that never went through Spotify's page at all, so it is not
subject to whatever cap that page imposes. Reached through the existing
"Import backup" file picker in the UI (`POST /api/playlists/import-backup`
in `main.py`), which tells this format apart from a real backup
(`library.EXPORT_FORMAT_PLAYLIST`/`_LIBRARY`, key-per-track, no matching) by
its declared `format`:

```json
{
  "format": "zer0space-music-tracklist",
  "name": "My playlist",
  "description": "optional",
  "tracks": [
    { "title": "Song title", "artist": "Artist name" },
    { "title": "Another song", "artist": "Another artist" }
  ]
}
```

`artist` and `description` are optional (an artist-less title still gets a
title-only search); `duration` (seconds) is optional too and sharpens the
match if known, but a hand-written or LLM-generated list never has it and
the scorer works fine without it. Capped at `MAX_TRACKLIST_ENTRIES` (500)
per file — one Deezer round trip per track, bounded by `MATCH_CONCURRENCY`.

---

## When playback breaks

### Everything stops playing at once

**Check the `yt-dlp` pin first.** It tracks a moving target. When YouTube
changes its player an outdated version stops resolving *every* track, with
nothing in this repo's diff to explain it.

This is not hypothetical: during development a pin ~20 months old failed every
single extraction with `ERROR: [youtube] <id>: The page needs to be reloaded.`
The search step still worked and found the right video, which makes it look like
a matching bug rather than a version problem. Upgrading fixed it immediately.

```bash
sed -i 's/^yt-dlp==.*/yt-dlp==<new>/' requirements.txt
# after CI is green:
docker service update --force --image ghcr.io/zer0space-net/zer0space-music:latest music_music
```

`.github/workflows/bump-ytdlp.yml` opens an issue weekly when a newer release
exists, so the pin does not rot unnoticed.

### One track will not play

Normal. Region block, age gate, or a removed video. It falls back to the preview
and the player shows "Nur Vorschau". Check the logs for
`[music] scraper could not resolve <key>`.

### A track plays the wrong song

The scorer picked badly. Log the candidates and their scores in `_resolve_sync`;
usually it means the catalogue duration is wrong, or every upload of that song
is a remix. Tighten `DURATION_TOLERANCE` or extend `_VARIANT`.

### Audio plays but cannot be seeked

A `Range` problem, not a scraper problem. See
[background-playback.md](background-playback.md#range-requests) — check that the
relay returns `206` with `Content-Range`, and that nothing in front of it is
re-compressing the body.

### 410 mid-song

The resolved URL expired between the ticket being minted and the fetch. The
player re-resolves once automatically. If it happens constantly, lower
`MUSIC_SOURCE_TTL`.
