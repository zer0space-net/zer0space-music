# Background playback on a phone

The requirement: put the phone in your pocket, lock the screen, and the music
keeps playing — with the title, artist and cover on the lock screen and working
play / pause / skip / scrub buttons.

This is the part of a self-hosted player that is hardest to get right, because
every wrong version *works on the desktop* and fails only on a real phone with
the screen off. This document records what the rules are, why each one exists,
and what still does not work.

Everything described here lives in [`static/js/player.js`](../static/js/player.js)
and [`templates/app.html`](../templates/app.html).

---

## The four rules

### 1. One `<audio>` element, for the life of the page

The element is in the HTML, inside the player bar, and is **never replaced**:

```html
<audio id="audio" preload="metadata" playsinline crossorigin="anonymous"></audio>
```

Changing track assigns `audio.src` and nothing else. The player bar is also
never re-rendered, and the views are a separate DOM subtree, so navigating
around the app cannot destroy it.

**Why.** The obvious design is `new Audio(url)` per track. On iOS that breaks
background playback in a way that is invisible on a desktop: the operating
system attaches the media session to a *specific element*, and the permission to
play without a user gesture ("activation") is a property of that element too.
A brand-new element, created while the page is backgrounded, has neither — so
the first song plays, and the queue silently stops at the end of it with the
phone in a pocket. Reusing one element keeps both the session and the
activation.

### 2. No Web Audio API

No `AudioContext`, no `createMediaElementSource`, no analyser node. If a
visualiser or an equaliser is ever wanted here, it needs re-testing on a locked
iPhone before it ships.

**Why.** Routing a media element through an `AudioContext` changes how iOS
classifies the page: from "media playback" (which continues in the background,
holds the audio focus, and appears in Control Center) to "web audio" (which is
suspended when the tab is backgrounded). The graph costs you the feature.

### 3. MediaSession metadata on every track change — *before* `play()`

```js
navigator.mediaSession.metadata = new MediaMetadata({
  title, artist, album, artwork: [{ src: cover, sizes: '500x500', type: 'image/jpeg' }]
});
```

Set in `loadTrack()`, before `audio.src` is assigned.

**Why.** This is what the lock screen, the Control Center, the Android
notification, the smartwatch and the car head unit all read. Without it the
phone shows the page title and a blank square, and the hardware buttons —
headset pause, steering-wheel skip — go to whichever app set a session last,
which is usually Spotify. Setting it *before* playback starts avoids a window
where the OS shows stale metadata for the previous song.

Action handlers are registered once, each in its own `try`, because an
unsupported action **throws** rather than being ignored, and one unsupported
handler would otherwise take the rest with it:

```
play · pause · stop · previoustrack · nexttrack · seekbackward · seekforward · seekto
```

### 4. `setPositionState()` on every tick

Throttled to once a second, plus forced on seek and on `playing`.

**Why.** Without it the lock-screen scrubber is a dead bar at zero, and on iOS
the ±15 s buttons do nothing at all. It is also the only way the OS learns the
track's duration, so the remaining-time readout stays blank without it.

It is wrapped in `try`, because reporting a position past `duration` throws —
which happens routinely for one frame at the end of a track.

---

## The supporting pieces

### The PWA manifest

[`src/main.py`](../src/main.py) builds `/manifest.webmanifest` **at request
time** rather than serving a static file, because `start_url` and `scope` have
to carry the gateway's `/music` prefix. `display: standalone` is the load-bearing
field: added to the home screen, the player runs as its own app, which on iOS
keeps the media session attached far longer than a Safari tab that has been
backgrounded for a while.

A scope mismatch is also what silently stops the service worker registering, so
the prefix matters twice.

### The service worker

[`static/sw.js`](../static/sw.js) caches **only the app shell** — CSS, JS, the
icon. It explicitly does **not** touch `/api/`, `/media/`, or any request
carrying a `Range` header.

**Why not audio.** Three reasons, any one of them sufficient: stream URLs are
per-user capabilities with an expiry; a cache would quietly fill the phone with
music nobody asked to download; and a worker that does not implement 206
Partial Content correctly is a classic way to break seeking on iOS.

Background playback does **not** depend on the service worker. It comes from
rules 1–4; the worker only makes the installed app start instantly.

### Range requests

Seeking is a server-side feature as much as a client one. The relay in
[`src/stream.py`](../src/stream.py) forwards `Range` upstream and returns 206
with `Content-Range` and `Accept-Ranges: bytes`.

Safari probes a media URL with `Range: bytes=0-1` before it will commit to
playing, and refuses to show a seek bar at all on a response that does not
advertise range support. A relay that ignores `Range` produces audio that plays
from the start and cannot be scrubbed — the single most common way a self-hosted
player feels broken on a phone.

`Accept-Encoding: identity` is forced on the upstream request for the same
reason: a re-compressed body would invalidate the byte offsets that Range is
expressed in.

### Why the `<audio>` src is a ticket

Resolved audio URLs are bound to the IP that resolved them, so they cannot be
handed to a phone on mobile data. The player gets
`/music/media/<signed-ticket>` instead, and the ticket **carries its own
authorisation** — it is not validated against the dashboard session.

That is deliberate: a request the OS media stack replays after the page has been
backgrounded does not reliably carry the headers the page would have added. A
cookie-gated media URL is the same failure the Crimson stack hit with AirPlay,
where the Apple TV fetched the playlist itself and got a 401.

---

## What does not work, and why

| | |
|---|---|
| **Playing while the browser is fully closed** | Not possible on any platform. Background audio means backgrounded, not terminated. Install it to the home screen so it is its own app. |
| **The very first play needs a tap** | Autoplay policy on every mobile browser. After one user-initiated play the element is unlocked and later track changes work unattended — which is why the code never creates a second element. |
| **Offline playback** | Nothing is cached. The catalogue and the audio both need the network. |
| **Gapless playback** | There is a short gap at track changes while the next source resolves. Mitigated by prefetching the next three tracks (`/api/prefetch`), not eliminated — that would need two alternating elements, which conflicts with rule 1. |
| **iOS Low Power Mode** | Can suspend background audio for a backgrounded *Safari tab*. The installed (standalone) app is far more reliable. |

## Testing it for real

A desktop browser will not catch a regression here. The check that matters:

1. Open `zer0space.com/music` on the phone, Share → **Add to Home Screen**.
2. Open it from the home-screen icon (not Safari), play something.
3. **Lock the screen.** Audio must continue.
4. Wake the screen without unlocking: title, artist and cover must be on the
   lock screen; play/pause/skip must work; the scrubber must move and be
   draggable.
5. Let a track end with the screen still locked — **the next one must start**.
   This is the step that catches a broken rule 1, and the only one that does.
6. Plug in headphones and use the inline remote — that exercises the
   MediaSession action handlers rather than the page's own buttons.
