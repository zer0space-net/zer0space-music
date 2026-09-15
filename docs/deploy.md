# Deploying zer0space Music

How this cluster actually deploys: stacks live in **Portainer**, images are
pulled from **GHCR**, and there is **no git clone on the nodes** — so `docker
build` on a node does nothing. Env changes go in the Portainer stack UI.

Two halves have to line up: this service, and the dashboard's `/music` gateway.
Neither does anything alone.

---

## What you need to prepare

Three secrets and one database. Nothing else — there is no API key anywhere in
this stack, because Deezer and YouTube are both reached unauthenticated.

### 1. The database

One-off, on **zs-state-01** (192.168.0.16). Its own database so a music schema
change can never touch dashboard data; the same `dashboard` role, so the
existing `db_password` secret is reused.

```sql
CREATE DATABASE zer0space_music OWNER dashboard;
```

The schema creates itself on first start (`CREATE TABLE IF NOT EXISTS`).

### 2. The Swarm secrets

Generate on any machine with Python:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

| Secret | Used by | Purpose |
|---|---|---|
| `music_service_token` | **both** | The shared token. Must be **byte-identical** on the music service and the dashboard. |
| `music_stream_secret` | music | Signs stream tickets. External so tickets survive a redeploy. |
| `db_password` | music | Already exists — the dashboard's. Nothing to do. |

Create them (note `printf %s`, so no trailing newline ends up in the value):

```bash
printf %s '<generated-value>' | docker secret create music_service_token -
printf %s '<generated-value>' | docker secret create music_stream_secret -
```

A trailing newline is the classic cause of a token that "looks right" and still
fails: `config.read_secret` strips it, but only if it is whitespace at the ends.

---

## Deploying the service

1. **Confirm CI is green** and the image exists:
   `ghcr.io/zer0space-net/zer0space-music:latest`.
   This repository's workflow uses `secrets.GITHUB_TOKEN`, **not** `CR_PAT` —
   `CR_PAT` is a repository secret on `zer0space-dashboard` only and resolves to
   an empty string here, failing with "Username and password required".

2. **Create the stack in Portainer** from [`docker-compose.yml`](../docker-compose.yml),
   named `music`. It attaches to the external network
   `dashboard_dashboard_net` — the Swarm-prefixed name, *not* `dashboard_net`.
   Getting that wrong produces a DNS error at the gateway that reads like the
   music service being down.

3. **Check the boot log.** It states exactly what it resolved:

   ```
   [music] database   dashboard@192.168.0.16:5432/zer0space_music
   [music] mount      /music  media same-origin
   [music] scraper    on (2 workers, cookies=no, proxy=no)
   [music] identity   service token required (from swarm secret)
   [music] tickets    signing key from swarm secret
   [music] database   connected, schema ready
   ```

   If `identity` says **`*** NO SERVICE TOKEN SET ***`**, stop: the service is
   trusting the user header from any caller. Fix the secret before going further.

---

## Wiring the dashboard gateway

The music service publishes no ports, so it is unreachable until the dashboard
proxies it.

1. On the **dashboard** stack in Portainer, set:

   ```
   MUSIC_URL=http://music:8000
   ```

   That is the Swarm service DNS name from the compose file (`music`), on the
   shared network.

2. Add `music_service_token` to the dashboard service's `secrets:` list — the
   **same secret object** the music stack uses.

3. **Redeploy the dashboard** so `src/music.py`, the route block and the sidebar
   entry ship. The merge alone does not deploy anything.

4. Confirm in the dashboard log:

   ```
   [music] gateway on /music (service=http://music:8000, token set)
   ```

   `NO SERVICE TOKEN` there means step 2 was missed: the gateway will forward
   requests the music service then rejects with 401.

Both are inert until configured: with `MUSIC_URL` unset, `/music` simply 404s
and the sidebar entry stays hidden, exactly like Crimson.

---

## Verifying

```bash
# From a node, inside the network — no ports are published.
docker run --rm --network dashboard_dashboard_net curlimages/curl \
  -s http://music:8000/healthz
# {"status":"ok"}
```

Then, signed in to the dashboard, open **zer0space.com/music**:

- [ ] The sidebar has a **Music** entry.
- [ ] The home page shows charts with cover art.
- [ ] The accent matches the theme picked in the dashboard.
- [ ] A track plays, and the seek bar can be dragged (that is Range working).
- [ ] Search returns songs, artists and albums.
- [ ] A liked song survives a reload (that is the database).
- [ ] On a phone: Add to Home Screen, play, **lock the screen** — audio
      continues and the lock screen shows the track. Full checklist in
      [background-playback.md](background-playback.md#testing-it-for-real).

---

## Local development

No cluster, no Postgres needed — the catalogue and playback work without a
database; only likes and playlists return 503.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt

MUSIC_BASE_PATH= DB_HOST=127.0.0.1 \
  .venv/bin/python -m uvicorn src.main:app --port 8000
```

With no `MUSIC_SERVICE_TOKEN` set, the identity header is trusted from anyone,
so the API can be poked directly:

```bash
curl -H 'X-Zer0space-User: 1' http://127.0.0.1:8000/api/home
```

For the **UI**, the browser cannot send that header, so put a tiny proxy in
front that adds it — this is also a faithful stand-in for the real gateway:

```js
// gateway.js — node gateway.js, then open http://127.0.0.1:8098
const http = require('http');
http.createServer((req, res) => {
  const headers = { ...req.headers };
  delete headers.host; delete headers.cookie; delete headers['accept-encoding'];
  headers['x-zer0space-user'] = '42';
  headers['x-zer0space-username'] = 'Siro';
  const up = http.request({ host: '127.0.0.1', port: 8000, method: req.method,
                            path: req.url, headers }, (u) => {
    res.writeHead(u.statusCode, u.headers); u.pipe(res);
  });
  req.pipe(up);
}).listen(8098);
```

> On Windows/Git Bash, do **not** pass `MUSIC_BASE_PATH=/`. MSYS rewrites a bare
> `/` into a Windows path and the app mounts itself at
> `/C:/Program Files/Git`. Use an empty value, as above.

---

## Operations

**Force a new image after a CI build:**

```bash
docker service update --force \
  --image ghcr.io/zer0space-net/zer0space-music:latest music_music
```

**Bust the catalogue cache** (after a bad Deezer spell, though empty results are
never cached):

```sql
DELETE FROM catalog_cache;
```

**Force every track to re-resolve** (after a yt-dlp bump):

```sql
UPDATE track_source SET source_url = NULL, expires_at = NULL, failures = 0;
```

Keeping the `provider_id` here is deliberate — the matches are still good, so
this makes the next play one extraction instead of a full search.

**Logs worth grepping:** `[music] scraper`, `[music] media upstream`,
`[music] resolve failed`.
