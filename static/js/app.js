/* Views, routing and everything the player does not own.

   A hash router, not the History API: this app is mounted under the dashboard's
   /music prefix by a reverse proxy, and pushState paths would have to be
   round-tripped through that gateway on every reload. A hash never leaves the
   browser.

   Rendering rule, inherited from the dashboard: everything that reaches
   innerHTML goes through API.esc(), and every URL through API.safeUrl().
   Track titles, artist names and cover URLs are all third-party strings from
   Deezer, and the CSP blocks inline <script> but not <img onerror>. */
(function () {
  'use strict';

  var view = document.getElementById('view');
  var esc = API.esc;
  var safeUrl = API.safeUrl;

  var store = {
    liked: new Set(),
    playlists: [],
    me: null,
    lastRoute: '',
    /* Tracks currently on screen, keyed by track key, so a click handler can
       find the full object without re-parsing it out of the DOM. */
    visible: new Map()
  };

  // --- Helpers -------------------------------------------------------------

  function toast(message, isError) {
    var host = document.getElementById('toasts');
    var node = document.createElement('div');
    node.className = 'toast' + (isError ? ' is-error' : '');
    node.textContent = message;
    host.appendChild(node);
    setTimeout(function () { node.remove(); }, 3600);
  }

  function fmtTime(seconds) {
    if (!seconds) return '—';
    var m = Math.floor(seconds / 60);
    var s = Math.floor(seconds % 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  function remember(tracks) {
    (tracks || []).forEach(function (track) {
      if (track && track.key) store.visible.set(track.key, track);
    });
  }

  function trackByKey(key) {
    return store.visible.get(key) || null;
  }

  var PLACEHOLDER =
    "data:image/svg+xml;charset=utf-8," +
    encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">' +
      '<rect width="64" height="64" fill="#0b1220"/>' +
      '<path d="M26 44V22l16-3v19" fill="none" stroke="#3d4f6f" stroke-width="2.6" ' +
      'stroke-linecap="round"/><circle cx="23" cy="44" r="4" fill="#3d4f6f"/>' +
      '<circle cx="39" cy="41" r="4" fill="#3d4f6f"/></svg>'
    );

  function cover(url) {
    return safeUrl(url) || PLACEHOLDER;
  }

  // --- Component markup ----------------------------------------------------

  function cardHtml(item, kind, sub) {
    var href = '#/' + kind + '/' + encodeURIComponent(item.id);
    return '' +
      '<a class="card" href="' + href + '">' +
        '<div class="card-art' + (kind === 'artist' ? ' round' : '') + '">' +
          '<img loading="lazy" src="' + esc(cover(item.cover || item.picture)) + '" alt="">' +
          (kind === 'artist' ? '' :
            '<button type="button" class="card-play" data-play-' + kind + '="' + esc(item.id) + '" ' +
            'aria-label="' + esc(t('player.play')) + '">' +
            '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor" aria-hidden="true">' +
            '<path d="M8 5.2 19 12 8 18.8z"/></svg></button>') +
        '</div>' +
        '<p class="card-title">' + esc(item.title || item.name) + '</p>' +
        '<p class="card-sub">' + esc(sub || '') + '</p>' +
      '</a>';
  }

  function trackRowHtml(track, index, options) {
    options = options || {};
    var liked = store.liked.has(track.key);
    var current = Player.current();
    var isCurrent = current && current.key === track.key;
    var artistHref = track.artist && track.artist.id
      ? '#/artist/' + encodeURIComponent(track.artist.id) : '';
    var albumHref = track.album && track.album.id
      ? '#/album/' + encodeURIComponent(track.album.id) : '';

    return '' +
      '<div class="track-row' + (isCurrent ? ' is-current' : '') + '" data-track="' + esc(track.key) + '" ' +
           'data-index="' + index + '" tabindex="0" role="button">' +
        '<div class="track-index">' +
          '<span class="num">' + (index + 1) + '</span>' +
          '<svg class="ico" viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true">' +
            '<path d="M8 5.2 19 12 8 18.8z"/></svg>' +
        '</div>' +
        '<div class="track-main">' +
          (options.art === false ? '' :
            '<img class="track-art" loading="lazy" src="' + esc(cover(track.cover)) + '" alt="">') +
          '<div class="track-text">' +
            '<div class="track-title">' +
              (track.explicit ? '<span class="explicit" title="Explicit">E</span>' : '') +
              esc(track.title) +
            '</div>' +
            (artistHref
              ? '<a class="track-artist" href="' + artistHref + '">' + esc(track.artist.name) + '</a>'
              : '<div class="track-artist">' + esc((track.artist && track.artist.name) || '') + '</div>') +
          '</div>' +
        '</div>' +
        '<div class="track-album">' +
          (albumHref && track.album
            ? '<a href="' + albumHref + '">' + esc(track.album.title) + '</a>'
            : esc((track.album && track.album.title) || '')) +
        '</div>' +
        '<div class="track-dur">' + fmtTime(track.duration) + '</div>' +
        '<div class="track-actions">' +
          '<button type="button" class="btn-icon heart" data-like="' + esc(track.key) + '" ' +
            'aria-pressed="' + liked + '" aria-label="' + esc(t('player.like')) + '">' +
            '<svg viewBox="0 0 24 24" width="16" height="16" fill="' + (liked ? 'currentColor' : 'none') +
            '" stroke="currentColor" stroke-width="1.8">' +
            '<path d="M12 20.3 4.6 12.9a4.5 4.5 0 0 1 6.4-6.3l1 1 1-1a4.5 4.5 0 1 1 6.4 6.3Z"/></svg>' +
          '</button>' +
          '<button type="button" class="btn-icon" data-menu="' + esc(track.key) + '" ' +
            'aria-label="' + esc(t('common.more')) + '">' +
            '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" aria-hidden="true">' +
            '<circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/>' +
            '<circle cx="19" cy="12" r="1.7"/></svg>' +
          '</button>' +
        '</div>' +
      '</div>';
  }

  function trackListHtml(tracks, options) {
    remember(tracks);
    if (!tracks.length) {
      return '<div class="empty"><p>' + esc(t('common.nothingHere')) + '</p></div>';
    }
    return '' +
      '<div class="tracks">' +
        '<div class="track-head">' +
          '<div style="text-align:right">#</div>' +
          '<div>' + esc(t('track.title')) + '</div>' +
          '<div>' + esc(t('track.album')) + '</div>' +
          '<div style="text-align:right">' + esc(t('track.duration')) + '</div>' +
          '<div></div>' +
        '</div>' +
        tracks.map(function (track, index) {
          return trackRowHtml(track, index, options);
        }).join('') +
      '</div>';
  }

  function sectionHtml(titleKey, inner, extra) {
    if (!inner) return '';
    return '' +
      '<section class="section">' +
        '<div class="section-head"><h2>' + esc(t(titleKey)) + '</h2>' + (extra || '') + '</div>' +
        inner +
      '</section>';
  }

  function gridHtml(items, kind, subFn) {
    if (!items || !items.length) return '';
    return '<div class="grid">' + items.map(function (item) {
      return cardHtml(item, kind, subFn ? subFn(item) : '');
    }).join('') + '</div>';
  }

  function loading() {
    view.innerHTML = '<div class="loading"><span class="spinner"></span><p>' +
      esc(t('common.loading')) + '</p></div>';
  }

  function errorView(err) {
    view.innerHTML = '<div class="empty"><h2>' + esc(I18N.tError(err.data || err)) + '</h2>' +
      '<button type="button" class="btn btn-ghost" id="retry">' + esc(t('common.retry')) + '</button></div>';
    var retry = document.getElementById('retry');
    if (retry) retry.addEventListener('click', function () { route(); });
  }

  // --- Views ---------------------------------------------------------------

  async function viewHome() {
    loading();
    var data;
    try { data = await API.home(); } catch (err) { return errorView(err); }

    var hour = new Date().getHours();
    var greetKey = hour < 11 ? 'home.goodMorning' : hour < 18 ? 'home.goodDay' : 'home.goodEvening';
    var name = data.greetingName ? ', ' + data.greetingName : '';

    var html = '<h1 class="greeting">' + esc(t(greetKey)) + esc(name) + '</h1>';

    if (data.recent && data.recent.length) {
      html += sectionHtml('home.recent', trackListHtml(data.recent.slice(0, 8)));
    }
    if (data.playlists && data.playlists.length) {
      html += sectionHtml('home.playlists',
        gridHtml(data.playlists.map(function (p) {
          return { id: p.id, title: p.name, cover: p.cover };
        }), 'playlist', function (item) {
          var found = data.playlists.filter(function (p) { return p.id === item.id; })[0];
          return found ? countLabel(found.trackCount) : '';
        }));
    }

    var charts = data.charts || {};
    html += sectionHtml('home.charts', trackListHtml((charts.tracks || []).slice(0, 10)));
    html += sectionHtml('home.topAlbums', gridHtml(charts.albums, 'album', function (a) {
      return (a.artist && a.artist.name) || '';
    }));
    if (data.topArtists && data.topArtists.length) {
      html += sectionHtml('home.topArtists', gridHtml(data.topArtists, 'artist'));
    }
    html += sectionHtml('home.chartArtists', gridHtml(charts.artists, 'artist'));
    html += sectionHtml('home.editorial', gridHtml(
      (charts.playlists || []).map(function (p) {
        return { id: 'dz' + p.id, title: p.title, cover: p.cover };
      }), 'catalog-playlist', function (item) { return ''; }));

    view.innerHTML = html;
  }

  function countLabel(n) {
    return n === 1 ? t('common.song') : t('common.songs', { n: n });
  }

  async function viewSearch(query) {
    var input = document.getElementById('search-input');
    if (input && input.value !== query) input.value = query;

    if (!query) {
      view.innerHTML = '<div class="empty"><h2>' + esc(t('search.start')) + '</h2></div>';
      return;
    }
    loading();
    var data;
    try { data = await API.search(query, 30); } catch (err) { return errorView(err); }

    var empty = !(data.tracks.length || data.albums.length || data.artists.length);
    if (empty) {
      view.innerHTML = '<div class="empty"><h2>' + esc(t('search.empty')) + '</h2></div>';
      return;
    }

    var html = '<h1 class="greeting">' + esc(t('search.title', { q: query })) + '</h1>';
    html += sectionHtml('search.tracks', trackListHtml(data.tracks.slice(0, 12)));
    html += sectionHtml('search.artists', gridHtml(data.artists, 'artist'));
    html += sectionHtml('search.albums', gridHtml(data.albums, 'album', function (a) {
      return [(a.artist && a.artist.name) || '', a.year].filter(Boolean).join(' · ');
    }));
    html += sectionHtml('search.playlists', gridHtml(
      (data.playlists || []).map(function (p) {
        return { id: 'dz' + p.id, title: p.title, cover: p.cover };
      }), 'catalog-playlist', function () { return ''; }));
    view.innerHTML = html;
  }

  async function viewAlbum(id) {
    loading();
    var data;
    try { data = await API.album(id); } catch (err) { return errorView(err); }
    remember(data.tracks);

    var sub = [
      (data.artist && data.artist.name) || '',
      data.year,
      countLabel(data.tracks.length)
    ].filter(Boolean).join(' · ');

    view.innerHTML = detailHead({
      kind: t('album.kind'),
      title: data.title,
      sub: sub,
      art: data.cover,
      playAll: 'album'
    }) + trackListHtml(data.tracks, { art: false });
  }

  async function viewArtist(id) {
    loading();
    var data;
    try { data = await API.artist(id); } catch (err) { return errorView(err); }
    remember(data.topTracks);

    var html = detailHead({
      kind: t('artist.kind'),
      title: data.name,
      sub: data.fans ? t('artist.fans', { n: data.fans.toLocaleString() }) : '',
      art: data.picture,
      round: true,
      playAll: 'artist'
    });
    html += sectionHtml('artist.topTracks', trackListHtml(data.topTracks.slice(0, 10)));
    html += sectionHtml('artist.albums', gridHtml(data.albums, 'album', function (a) { return a.year; }));
    html += sectionHtml('artist.related', gridHtml(data.related, 'artist'));
    view.innerHTML = html;
  }

  async function viewCatalogPlaylist(id) {
    loading();
    var data;
    try {
      data = await API.catalogPlaylist(String(id).replace(/^dz/, ''));
    } catch (err) { return errorView(err); }
    remember(data.tracks);

    view.innerHTML = detailHead({
      kind: t('playlist.kind'),
      title: data.title,
      sub: [data.creator, countLabel(data.tracks.length)].filter(Boolean).join(' · '),
      art: data.cover,
      playAll: 'catalog-playlist'
    }) + trackListHtml(data.tracks);
  }

  async function viewLiked() {
    loading();
    var data;
    try { data = await API.library(); } catch (err) { return errorView(err); }
    remember(data.liked);

    var html = detailHead({
      kind: t('playlist.kind'),
      title: t('library.liked'),
      sub: countLabel(data.liked.length),
      art: '',
      liked: true,
      playAll: data.liked.length ? 'liked' : ''
    });
    html += data.liked.length
      ? trackListHtml(data.liked)
      : '<div class="empty"><p>' + esc(t('library.emptyLiked')) + '</p></div>';
    view.innerHTML = html;
  }

  async function viewPlaylist(id) {
    loading();
    var data;
    try { data = await API.playlist(id); } catch (err) { return errorView(err); }
    remember(data.tracks);

    var html = detailHead({
      kind: t('playlist.kind'),
      title: data.name,
      sub: [data.description, countLabel(data.trackCount)].filter(Boolean).join(' · '),
      art: data.cover,
      playAll: data.tracks.length ? 'playlist' : '',
      ownId: id
    });
    html += data.tracks.length
      ? trackListHtml(data.tracks)
      : '<div class="empty"><p>' + esc(t('playlist.empty')) + '</p></div>';
    view.innerHTML = html;
  }

  async function viewLibrary() {
    loading();
    var data;
    try { data = await API.library(); } catch (err) { return errorView(err); }
    remember(data.recent);

    var html = '<h1 class="greeting">' + esc(t('library.title')) + '</h1>';

    var tiles = [{ id: '__liked', title: t('library.liked'), cover: '' }].concat(
      data.playlists.map(function (p) { return { id: p.id, title: p.name, cover: p.cover }; })
    );
    html += sectionHtml('library.playlists',
      '<div class="grid">' + tiles.map(function (item) {
        var href = item.id === '__liked' ? '#/liked' : '#/playlist/' + encodeURIComponent(item.id);
        var sub = item.id === '__liked'
          ? countLabel(data.liked.length)
          : countLabel((data.playlists.filter(function (p) { return p.id === item.id; })[0] || {}).trackCount || 0);
        return '<a class="card" href="' + href + '">' +
          '<div class="card-art"><img loading="lazy" src="' + esc(cover(item.cover)) + '" alt=""></div>' +
          '<p class="card-title">' + esc(item.title) + '</p>' +
          '<p class="card-sub">' + esc(sub) + '</p></a>';
      }).join('') + '</div>',
      '<button type="button" class="btn btn-ghost btn-sm" id="lib-new">' +
        esc(t('playlist.create')) + '</button>');

    if (data.recent.length) {
      html += sectionHtml('library.recent', trackListHtml(data.recent.slice(0, 20)));
    }
    view.innerHTML = html;

    var newBtn = document.getElementById('lib-new');
    if (newBtn) newBtn.addEventListener('click', promptNewPlaylist);
  }

  function detailHead(options) {
    var artHtml = options.liked
      ? '<div class="detail-art lib-cover-liked" style="display:grid;place-items:center">' +
        '<svg viewBox="0 0 24 24" width="64" height="64" fill="currentColor">' +
        '<path d="M12 20.7 4.3 13a4.8 4.8 0 0 1 6.8-6.8l.9.9.9-.9A4.8 4.8 0 1 1 19.7 13Z"/></svg></div>'
      : '<img class="detail-art' + (options.round ? ' round' : '') + '" src="' +
        esc(cover(options.art)) + '" alt="">';

    var actions = '';
    if (options.playAll) {
      actions += '<button type="button" class="btn-play-big" id="play-all" ' +
        'aria-label="' + esc(t('player.play')) + '">' +
        '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true">' +
        '<path d="M8 5.2 19 12 8 18.8z"/></svg></button>';
    }
    if (options.ownId) {
      actions += '<button type="button" class="btn btn-ghost btn-sm" data-rename="' +
        esc(options.ownId) + '">' + esc(t('common.rename')) + '</button>' +
        '<button type="button" class="btn btn-ghost btn-sm" data-delete-playlist="' +
        esc(options.ownId) + '">' + esc(t('common.delete')) + '</button>';
    }

    return '' +
      '<header class="detail-head">' + artHtml +
        '<div class="detail-meta">' +
          '<p class="detail-kind">' + esc(options.kind) + '</p>' +
          '<h1 class="detail-title">' + esc(options.title) + '</h1>' +
          '<p class="detail-sub">' + esc(options.sub || '') + '</p>' +
          '<div class="detail-actions">' + actions + '</div>' +
        '</div>' +
      '</header>';
  }

  // --- Playing from a view -------------------------------------------------

  function visibleTracks() {
    /* The order on screen is the order to play, so it is read back out of the
       DOM rather than kept in a parallel array that can drift from it. */
    return Array.prototype.map.call(
      view.querySelectorAll('.track-row'),
      function (row) { return trackByKey(row.dataset.track); }
    ).filter(Boolean);
  }

  function playFromRow(row) {
    var tracks = visibleTracks();
    var index = tracks.findIndex(function (track) { return track.key === row.dataset.track; });
    if (index < 0) return;
    Player.playQueue(tracks, index, location.hash);
  }

  // --- Library actions -----------------------------------------------------

  async function toggleLike(key) {
    var track = trackByKey(key) || Player.current();
    if (!track || track.key !== key) track = trackByKey(key);
    if (!track) return;

    var liked = store.liked.has(key);
    try {
      if (liked) {
        await API.unlike(key);
        store.liked.delete(key);
        toast(t('track.unliked'));
      } else {
        await API.like(track);
        store.liked.add(key);
        toast(t('track.liked'));
      }
    } catch (err) {
      return toast(I18N.tError(err.data || err), true);
    }
    refreshLikeButtons();
    updateLikedCount();
  }

  function refreshLikeButtons() {
    document.querySelectorAll('[data-like]').forEach(function (button) {
      var on = store.liked.has(button.dataset.like);
      button.setAttribute('aria-pressed', String(on));
      var svg = button.querySelector('svg');
      if (svg) svg.setAttribute('fill', on ? 'currentColor' : 'none');
    });
    var current = Player.current();
    if (current) {
      var on = store.liked.has(current.key);
      Player.likeButton.setAttribute('aria-pressed', String(on));
    }
  }

  function updateLikedCount() {
    var label = document.getElementById('liked-count');
    if (label) label.textContent = countLabel(store.liked.size);
  }

  async function refreshPlaylists() {
    try {
      var data = await API.playlists();
      store.playlists = data.playlists;
    } catch (err) { return; }

    var host = document.getElementById('playlist-list');
    host.innerHTML = store.playlists.map(function (p) {
      return '<a class="lib-item" href="#/playlist/' + encodeURIComponent(p.id) + '">' +
        '<img class="lib-cover" loading="lazy" src="' + esc(cover(p.cover)) + '" alt="">' +
        '<span class="lib-text"><strong>' + esc(p.name) + '</strong>' +
        '<small>' + esc(countLabel(p.trackCount)) + '</small></span></a>';
    }).join('');
  }

  // --- Modal ---------------------------------------------------------------

  var modal = document.getElementById('modal');
  var modalForm = document.getElementById('modal-form');
  var modalResolve = null;

  function openModal(title, bodyHtml, okLabel) {
    document.getElementById('modal-title').textContent = title;
    document.getElementById('modal-body').innerHTML = bodyHtml;
    document.getElementById('modal-ok').textContent = okLabel || t('common.save');
    modal.showModal();
    var first = modal.querySelector('input, button[data-pick]');
    if (first) first.focus();
    return new Promise(function (resolve) { modalResolve = resolve; });
  }

  function closeModal(value) {
    if (modalResolve) { modalResolve(value); modalResolve = null; }
    if (modal.open) modal.close();
  }

  modalForm.addEventListener('submit', function (event) {
    event.preventDefault();
    var data = {};
    modal.querySelectorAll('input, textarea').forEach(function (field) {
      data[field.name] = field.value;
    });
    closeModal(data);
  });
  document.getElementById('modal-cancel').addEventListener('click', function () { closeModal(null); });
  modal.addEventListener('cancel', function () { closeModal(null); });

  async function promptNewPlaylist(tracksToAdd) {
    var result = await openModal(t('playlist.create'),
      '<label for="pl-name">' + esc(t('playlist.name')) + '</label>' +
      '<input type="text" id="pl-name" name="name" maxlength="120" required>' +
      '<label for="pl-desc">' + esc(t('playlist.description')) + '</label>' +
      '<input type="text" id="pl-desc" name="description" maxlength="500">',
      t('common.create'));
    if (!result || !result.name.trim()) return null;

    try {
      var created = await API.createPlaylist(result.name, result.description);
      if (tracksToAdd && tracksToAdd.length) {
        await API.addToPlaylist(created.id, tracksToAdd);
      }
      toast(t('playlist.created'));
      await refreshPlaylists();
      return created;
    } catch (err) {
      toast(I18N.tError(err.data || err), true);
      return null;
    }
  }

  async function promptAddToPlaylist(track) {
    var options = store.playlists.map(function (p) {
      return '<button type="button" class="lib-item" data-pick="' + esc(p.id) + '">' +
        '<img class="lib-cover" src="' + esc(cover(p.cover)) + '" alt="">' +
        '<span class="lib-text"><strong>' + esc(p.name) + '</strong>' +
        '<small>' + esc(countLabel(p.trackCount)) + '</small></span></button>';
    }).join('');

    var body = '<div class="pick-list">' +
      '<button type="button" class="lib-item" data-pick="__new">' +
      '<span class="lib-cover"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/>' +
      '</svg></span><span class="lib-text"><strong>' + esc(t('playlist.create')) +
      '</strong></span></button>' + options + '</div>';

    document.getElementById('modal-ok').hidden = true;
    var promise = openModal(t('playlist.addTo'), body, '');

    modal.querySelectorAll('[data-pick]').forEach(function (button) {
      button.addEventListener('click', function () { closeModal({ pick: button.dataset.pick }); });
    });

    var result = await promise;
    document.getElementById('modal-ok').hidden = false;
    if (!result || !result.pick) return;

    if (result.pick === '__new') {
      await promptNewPlaylist([track]);
      return;
    }
    try {
      await API.addToPlaylist(result.pick, [track]);
      var target = store.playlists.filter(function (p) { return p.id === result.pick; })[0];
      toast(t('playlist.added', { name: target ? target.name : '' }));
      await refreshPlaylists();
    } catch (err) {
      toast(I18N.tError(err.data || err), true);
    }
  }

  // --- Context menu --------------------------------------------------------

  function closeContextMenu() {
    var open = document.querySelector('.context-menu');
    if (open) open.remove();
  }

  function openContextMenu(anchor, track) {
    closeContextMenu();
    var menu = document.createElement('div');
    menu.className = 'context-menu';
    menu.setAttribute('role', 'menu');

    var items = [
      { label: t('track.playNext'), action: function () { Player.enqueue(track, true); toast(t('track.queued')); } },
      { label: t('playlist.addTo'), action: function () { promptAddToPlaylist(track); } }
    ];
    if (track.artist && track.artist.id) {
      items.push({
        label: t('track.goArtist'),
        action: function () { location.hash = '#/artist/' + encodeURIComponent(track.artist.id); }
      });
    }
    if (track.album && track.album.id) {
      items.push({
        label: t('track.goAlbum'),
        action: function () { location.hash = '#/album/' + encodeURIComponent(track.album.id); }
      });
    }
    var playlistId = (location.hash.match(/^#\/playlist\/(.+)$/) || [])[1];
    if (playlistId) {
      items.push({
        label: t('playlist.removed'),
        danger: true,
        action: async function () {
          try {
            await API.removeFromPlaylist(decodeURIComponent(playlistId), track.key);
            toast(t('playlist.removed'));
            route();
            refreshPlaylists();
          } catch (err) { toast(I18N.tError(err.data || err), true); }
        }
      });
    }

    items.forEach(function (item) {
      var button = document.createElement('button');
      button.type = 'button';
      button.textContent = item.label;
      if (item.danger) button.className = 'danger';
      button.addEventListener('click', function () { closeContextMenu(); item.action(); });
      menu.appendChild(button);
    });

    document.body.appendChild(menu);
    var rect = anchor.getBoundingClientRect();
    /* Flip up / left when the menu would leave the viewport — on a phone the
       row is usually near the bottom edge. */
    var top = rect.bottom + 6;
    if (top + menu.offsetHeight > window.innerHeight - 8) top = rect.top - menu.offsetHeight - 6;
    var left = Math.min(rect.left, window.innerWidth - menu.offsetWidth - 8);
    menu.style.top = Math.max(8, top) + 'px';
    menu.style.left = Math.max(8, left) + 'px';
  }

  document.addEventListener('click', function (event) {
    if (!event.target.closest('.context-menu') && !event.target.closest('[data-menu]')) {
      closeContextMenu();
    }
  });

  // --- Delegated interaction ----------------------------------------------

  view.addEventListener('click', function (event) {
    var like = event.target.closest('[data-like]');
    if (like) { event.preventDefault(); event.stopPropagation(); return void toggleLike(like.dataset.like); }

    var menu = event.target.closest('[data-menu]');
    if (menu) {
      event.preventDefault();
      event.stopPropagation();
      var track = trackByKey(menu.dataset.menu);
      if (track) openContextMenu(menu, track);
      return;
    }

    /* A card's play button must not also follow the card's own href. */
    var cardPlay = event.target.closest('.card-play');
    if (cardPlay) {
      event.preventDefault();
      event.stopPropagation();
      return void playCard(cardPlay);
    }

    var playAll = event.target.closest('#play-all');
    if (playAll) {
      var tracks = visibleTracks();
      if (tracks.length) Player.playQueue(tracks, 0, location.hash);
      return;
    }

    var rename = event.target.closest('[data-rename]');
    if (rename) return void renamePlaylist(rename.dataset.rename);

    var remove = event.target.closest('[data-delete-playlist]');
    if (remove) return void deletePlaylist(remove.dataset.deletePlaylist);

    var row = event.target.closest('.track-row');
    if (row && !event.target.closest('a')) playFromRow(row);
  });

  /* Rows are focusable buttons, so they must answer the keyboard too. */
  view.addEventListener('keydown', function (event) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    var row = event.target.closest('.track-row');
    if (!row) return;
    event.preventDefault();
    playFromRow(row);
  });

  async function playCard(button) {
    var albumId = button.dataset.playAlbum;
    var playlistId = button.dataset.playPlaylist;
    var catalogId = button.dataset.playCatalogPlaylist;
    try {
      var data;
      if (albumId) data = await API.album(albumId);
      else if (playlistId) data = await API.playlist(playlistId);
      else if (catalogId) data = await API.catalogPlaylist(String(catalogId).replace(/^dz/, ''));
      else return;
      if (data.tracks && data.tracks.length) {
        remember(data.tracks);
        Player.playQueue(data.tracks, 0, location.hash);
      }
    } catch (err) {
      toast(I18N.tError(err.data || err), true);
    }
  }

  async function renamePlaylist(id) {
    var existing = store.playlists.filter(function (p) { return p.id === id; })[0] || { name: '', description: '' };
    var result = await openModal(t('common.rename'),
      '<label for="pl-name">' + esc(t('playlist.name')) + '</label>' +
      '<input type="text" id="pl-name" name="name" maxlength="120" value="' + esc(existing.name) + '" required>' +
      '<label for="pl-desc">' + esc(t('playlist.description')) + '</label>' +
      '<input type="text" id="pl-desc" name="description" maxlength="500" value="' +
        esc(existing.description || '') + '">');
    if (!result || !result.name.trim()) return;
    try {
      await API.renamePlaylist(id, result.name, result.description);
      await refreshPlaylists();
      route();
    } catch (err) { toast(I18N.tError(err.data || err), true); }
  }

  async function deletePlaylist(id) {
    var existing = store.playlists.filter(function (p) { return p.id === id; })[0] || { name: '' };
    if (!window.confirm(t('playlist.confirmDelete', { name: existing.name }))) return;
    try {
      await API.deletePlaylist(id);
      toast(t('playlist.deleted'));
      await refreshPlaylists();
      location.hash = '#/library';
    } catch (err) { toast(I18N.tError(err.data || err), true); }
  }

  // --- Queue drawer --------------------------------------------------------

  var queuePanel = document.getElementById('queue-panel');

  function renderQueue() {
    if (queuePanel.hidden) return;
    var data = Player.queue();
    var host = document.getElementById('queue-list');
    if (!data.tracks.length) {
      host.innerHTML = '<div class="empty"><p>' + esc(t('queue.empty')) + '</p></div>';
      return;
    }
    remember(data.tracks);
    host.innerHTML = data.tracks.map(function (track, index) {
      var label = index === data.cursor ? t('queue.nowPlaying') : '';
      return '<button type="button" class="lib-item' + (index === data.cursor ? ' is-active' : '') +
        '" data-queue-index="' + index + '">' +
        '<img class="lib-cover" loading="lazy" src="' + esc(cover(track.cover)) + '" alt="">' +
        '<span class="lib-text"><strong>' + esc(track.title) + '</strong>' +
        '<small>' + esc(label || (track.artist && track.artist.name) || '') + '</small></span></button>';
    }).join('');
  }

  document.getElementById('queue-toggle').addEventListener('click', function () {
    queuePanel.hidden = !queuePanel.hidden;
    renderQueue();
  });
  document.getElementById('queue-close').addEventListener('click', function () {
    queuePanel.hidden = true;
  });
  document.getElementById('queue-list').addEventListener('click', function (event) {
    var item = event.target.closest('[data-queue-index]');
    if (item) Player.jumpTo(Number(item.dataset.queueIndex));
  });

  // --- Router --------------------------------------------------------------

  var history = [];
  var historyIndex = -1;
  var navigatingInternally = false;

  function parse() {
    var hash = location.hash.replace(/^#\/?/, '');
    if (!hash) return { name: 'home', arg: '' };
    var parts = hash.split('/');
    return {
      name: parts[0],
      arg: parts.slice(1).map(decodeURIComponent).join('/')
    };
  }

  async function route() {
    var target = parse();
    closeContextMenu();
    store.visible.clear();

    document.querySelectorAll('.nav-item[data-route]').forEach(function (item) {
      item.classList.toggle('is-active', item.dataset.route === target.name);
    });

    view.scrollTop = 0;

    switch (target.name) {
      case 'search': await viewSearch(target.arg); break;
      case 'album': await viewAlbum(target.arg); break;
      case 'artist': await viewArtist(target.arg); break;
      case 'playlist': await viewPlaylist(target.arg); break;
      case 'catalog-playlist': await viewCatalogPlaylist(target.arg); break;
      case 'liked': await viewLiked(); break;
      case 'library': await viewLibrary(); break;
      default: await viewHome();
    }
    refreshLikeButtons();
    markCurrentRow();
  }

  window.addEventListener('hashchange', function () {
    if (!navigatingInternally) {
      history = history.slice(0, historyIndex + 1);
      history.push(location.hash);
      historyIndex = history.length - 1;
    }
    navigatingInternally = false;
    route();
  });

  document.getElementById('nav-back').addEventListener('click', function () {
    if (historyIndex <= 0) return;
    historyIndex -= 1;
    navigatingInternally = true;
    location.hash = history[historyIndex];
  });
  document.getElementById('nav-forward').addEventListener('click', function () {
    if (historyIndex >= history.length - 1) return;
    historyIndex += 1;
    navigatingInternally = true;
    location.hash = history[historyIndex];
  });

  // --- Search box ----------------------------------------------------------

  var searchInput = document.getElementById('search-input');
  var searchTimer = null;

  document.getElementById('search-form').addEventListener('submit', function (event) {
    event.preventDefault();
    location.hash = '#/search/' + encodeURIComponent(searchInput.value.trim());
  });

  searchInput.addEventListener('input', function () {
    clearTimeout(searchTimer);
    var value = searchInput.value.trim();
    /* 350 ms: long enough that typing a word is one request, short enough that
       the results feel live. Below three characters the result set is noise. */
    searchTimer = setTimeout(function () {
      if (value.length < 3) return;
      location.hash = '#/search/' + encodeURIComponent(value);
    }, 350);
  });

  // --- Sidebar (mobile) ----------------------------------------------------

  var sidebar = document.getElementById('sidebar');
  var scrim = document.getElementById('sidebar-scrim');

  function setSidebar(open) {
    sidebar.classList.toggle('is-open', open);
    scrim.hidden = !open;
  }
  document.getElementById('sidebar-open').addEventListener('click', function () { setSidebar(true); });
  document.getElementById('sidebar-close').addEventListener('click', function () { setSidebar(false); });
  scrim.addEventListener('click', function () { setSidebar(false); });
  sidebar.addEventListener('click', function (event) {
    if (event.target.closest('a[href^="#"]')) setSidebar(false);
  });

  document.getElementById('new-playlist').addEventListener('click', function () {
    promptNewPlaylist();
  });

  // --- Player wiring -------------------------------------------------------

  function markCurrentRow() {
    var current = Player.current();
    view.querySelectorAll('.track-row').forEach(function (row) {
      row.classList.toggle('is-current', !!current && row.dataset.track === current.key);
    });
  }

  Player.on('trackchange', function (track) {
    markCurrentRow();
    Player.likeButton.setAttribute('aria-pressed', String(store.liked.has(track.key)));
    renderQueue();
  });
  Player.on('queuechange', renderQueue);
  Player.on('error', function (err) { toast(I18N.tError(err.data || err), true); });
  Player.on('notice', function (message) { toast(message); });

  Player.likeButton.addEventListener('click', function () {
    var current = Player.current();
    if (!current) return;
    store.visible.set(current.key, current);
    toggleLike(current.key);
  });

  // --- Language ------------------------------------------------------------

  document.getElementById('lang-toggle').addEventListener('click', function () {
    I18N.toggle();
  });

  window.addEventListener('languagechange:zs', function () {
    document.getElementById('lang-label').textContent = I18N.lang.toUpperCase();
    /* Views are built in JavaScript and carry no data-i18n attributes, so
       applyI18n cannot reach them — they have to be re-rendered. */
    route();
    refreshPlaylists();
    updateLikedCount();
    renderQueue();
  });

  // --- Boot ----------------------------------------------------------------

  async function boot() {
    I18N.apply();
    I18N.checkParity();
    document.getElementById('lang-label').textContent = I18N.lang.toUpperCase();

    try {
      store.me = await API.me();
      Player.applyPrefs(store.me.prefs);
    } catch (err) {
      /* 401 already redirected in api.js; anything else still lets the
         catalogue render, so this is not fatal. */
    }

    try {
      var keys = await API.likedKeys();
      store.liked = new Set(keys.keys);
    } catch (err) { /* likes unavailable; hearts render unfilled */ }

    updateLikedCount();
    await refreshPlaylists();

    try {
      var saved = await API.getState();
      if (saved && saved.state) Player.restore(saved.state);
    } catch (err) { /* no previous session */ }

    if (!location.hash) location.hash = '#/home';
    history = [location.hash];
    historyIndex = 0;
    await route();

    /* The service worker only caches the app shell — never audio, never API
       responses. See static/sw.js for why. */
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register(API.base + '/static/sw.js', { scope: API.base + '/' })
        .catch(function () { /* non-fatal: the app works without it */ });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
