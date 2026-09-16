/* The JSON client, plus the two escaping helpers everything else must go through.

   Every URL is built from ZS_BASE (the gateway's mount prefix). A root-relative
   '/api/...' would resolve against the dashboard's origin root and hit the
   dashboard's own API instead of ours — the same class of bug that produced the
   Crimson root-relative playlist failure.

   There is no CSRF token here: the gateway exempts /music from the dashboard's
   double-submit check (this app has no cookie-authenticated state of its own —
   the gateway authenticates every hop with the service token). */
(function () {
  'use strict';

  var BASE = window.ZS_BASE || '';

  function url(path) {
    return BASE + path;
  }

  /* Anything user- or catalogue-controlled that reaches innerHTML goes through
     this. Track titles, artist names and playlist names are all third-party
     strings; the CSP blocks inline <script> but not <img onerror>. */
  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /* Cover art URLs come from Deezer's API — third-party data ending up in a
     src/href attribute. Only http(s) survives, so a javascript: or data: URL
     cannot ride in on a catalogue field. */
  function safeUrl(value) {
    var raw = String(value == null ? '' : value).trim();
    if (!raw) return '';
    if (/^https?:\/\//i.test(raw)) return raw;
    if (raw.charAt(0) === '#') return raw;
    return '';
  }

  function ApiError(data, status) {
    this.name = 'ApiError';
    this.data = data || {};
    this.status = status;
    this.code = this.data.code || 'INTERNAL';
    this.message = this.data.error || 'Request failed';
  }
  ApiError.prototype = Object.create(Error.prototype);

  async function request(method, path, body) {
    var options = {
      method: method,
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin'
    };
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }

    var response;
    try {
      response = await fetch(url(path), options);
    } catch (err) {
      throw new ApiError({ code: 'NETWORK', error: String(err) }, 0);
    }

    if (response.status === 204) return null;

    var data = null;
    try {
      data = await response.json();
    } catch (e) {
      data = null;
    }

    if (!response.ok) {
      /* The gateway answers 401 when the zer0space session is gone. There is no
         login in this app, so the only correct move is back to the front door —
         and a reload is what picks up a session that was merely refreshed. */
      if (response.status === 401) {
        window.location.href = '/login';
      }
      throw new ApiError(data, response.status);
    }
    return data;
  }

  window.API = {
    base: BASE,
    url: url,
    esc: esc,
    safeUrl: safeUrl,
    ApiError: ApiError,

    get: function (path) { return request('GET', path); },
    post: function (path, body) { return request('POST', path, body || {}); },
    patch: function (path, body) { return request('PATCH', path, body || {}); },
    del: function (path) { return request('DELETE', path); },

    // --- Catalogue
    me: function () { return request('GET', '/api/me'); },
    home: function () { return request('GET', '/api/home'); },
    search: function (q, limit) {
      return request('GET', '/api/search?q=' + encodeURIComponent(q) + '&limit=' + (limit || 30));
    },
    album: function (id) { return request('GET', '/api/album/' + encodeURIComponent(id)); },
    artist: function (id) { return request('GET', '/api/artist/' + encodeURIComponent(id)); },
    catalogPlaylist: function (id) {
      return request('GET', '/api/catalog-playlist/' + encodeURIComponent(id));
    },
    genres: function () { return request('GET', '/api/genres'); },

    // --- Playback
    stream: function (key) { return request('GET', '/api/stream/' + encodeURIComponent(key)); },
    prefetch: function (keys) { return request('POST', '/api/prefetch', { keys: keys }); },
    played: function (track, seconds) {
      return request('POST', '/api/played', { track: track, seconds: seconds || 0 });
    },

    // --- Library
    library: function () { return request('GET', '/api/library'); },
    likedKeys: function () { return request('GET', '/api/liked-keys'); },
    like: function (track) { return request('POST', '/api/like', { track: track }); },
    unlike: function (key) { return request('POST', '/api/unlike', { key: key }); },
    playlists: function () { return request('GET', '/api/playlists'); },
    createPlaylist: function (name, description) {
      return request('POST', '/api/playlists', { name: name, description: description || '' });
    },
    playlist: function (id) { return request('GET', '/api/playlists/' + encodeURIComponent(id)); },
    renamePlaylist: function (id, name, description) {
      return request('PATCH', '/api/playlists/' + encodeURIComponent(id),
        { name: name, description: description || '' });
    },
    deletePlaylist: function (id) {
      return request('DELETE', '/api/playlists/' + encodeURIComponent(id));
    },
    addToPlaylist: function (id, tracks) {
      return request('POST', '/api/playlists/' + encodeURIComponent(id) + '/tracks',
        { tracks: tracks });
    },
    importSpotifyPlaylist: function (url) {
      return request('POST', '/api/playlists/import-spotify', { url: url });
    },
    importBackup: function (data) {
      return request('POST', '/api/playlists/import-backup', data);
    },
    removeFromPlaylist: function (id, key) {
      return request('DELETE', '/api/playlists/' + encodeURIComponent(id) +
        '/tracks/' + encodeURIComponent(key));
    },

    // --- Session continuity
    getState: function () { return request('GET', '/api/state'); },
    saveState: function (state) { return request('POST', '/api/state', state); },
    savePrefs: function (prefs) { return request('POST', '/api/prefs', prefs); }
  };
})();
