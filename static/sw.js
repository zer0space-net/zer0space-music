/* Service worker — the app shell only.

   Its whole job is to make the installed PWA open instantly and survive a flaky
   connection on the way to the tram. It does NOT:

   * cache audio. Stream URLs are per-user capabilities with an expiry; a cached
     one would serve a stale or foreign response and would quietly fill the
     device's storage with music nobody asked to download.
   * cache API responses. Likes, playlists and "recently played" are shared state
     that has to be current, and a cached /api/me would keep showing a user who
     signed out in the dashboard.
   * intercept /media. Range requests are the whole point of that route (see
     src/stream.py); passing them through a worker that does not implement 206
     correctly is a classic way to break seeking on iOS. They are left to the
     browser, which handles them natively.

   Background playback does not depend on this file at all — that comes from the
   <audio> element plus MediaSession (see static/js/player.js). What the manifest
   and this worker add is standalone display, which on iOS is what keeps the
   media session attached after the app is backgrounded for a long time. */

var VERSION = 'zs-music-v1';
var SHELL = [
  'static/css/music.css',
  'static/js/boot.js',
  'static/js/i18n.js',
  'static/js/api.js',
  'static/js/player.js',
  'static/js/app.js',
  'static/img/icon.svg',
  'static/js/starfield.js'
];

/* The worker is registered with the gateway's mount as its scope, so relative
   URLs resolve under /music/ without this file needing to know the prefix. */
function shellUrls() {
  return SHELL.map(function (path) { return new URL(path, self.registration.scope).toString(); });
}

self.addEventListener('install', function (event) {
  event.waitUntil(
    caches.open(VERSION)
      .then(function (cache) { return cache.addAll(shellUrls()); })
      /* One missing file must not leave the app permanently un-installable. */
      .catch(function () { return undefined; })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.map(function (key) {
        return key === VERSION ? undefined : caches.delete(key);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function (event) {
  var request = event.request;
  if (request.method !== 'GET') return;

  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  /* Everything dynamic goes straight to the network, untouched. */
  if (url.pathname.indexOf('/api/') !== -1 ||
      url.pathname.indexOf('/media/') !== -1 ||
      request.headers.has('range')) {
    return;
  }

  /* Static shell: cache first, and refresh the entry in the background so a
     deploy is picked up on the next load rather than needing a hard reload. */
  if (url.pathname.indexOf('/static/') !== -1) {
    event.respondWith(
      caches.match(request).then(function (hit) {
        var network = fetch(request).then(function (response) {
          if (response && response.ok) {
            var copy = response.clone();
            caches.open(VERSION).then(function (cache) { cache.put(request, copy); });
          }
          return response;
        }).catch(function () { return hit; });
        return hit || network;
      })
    );
    return;
  }

  /* The document itself: network first, so a signed-out session redirects
     properly instead of being masked by a cached shell. */
  event.respondWith(
    fetch(request).catch(function () {
      return caches.match(request).then(function (hit) {
        return hit || new Response('', { status: 504, statusText: 'Offline' });
      });
    })
  );
});
