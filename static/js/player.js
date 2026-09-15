/* The playback engine.

   ============================================================================
   BACKGROUND PLAYBACK ON A PHONE — the part that is easy to get wrong
   ============================================================================
   Everything in this file that looks over-careful is there so that audio keeps
   running when the screen locks and the lock screen shows real controls. The
   rules, learned the hard way and documented at length in
   docs/background-playback.md:

   1. ONE <audio> element, created in the HTML, never replaced and never
      re-created. Assigning a new Audio() per track (the obvious design) drops
      the OS media session on every track change: iOS treats the new element as
      a new, un-gestured source and refuses to start it while backgrounded, so
      playback stops at the end of the first song with the phone in a pocket.
      This module therefore only ever writes `audio.src`.

   2. No Web Audio API. Routing through an AudioContext gives you an EQ and a
      visualiser, and it also makes iOS classify the page as "web audio" rather
      than "media playback" — which is suspended when the tab is backgrounded.
      A plain media element is what the OS keeps alive.

   3. MediaSession metadata is set on EVERY track change, before play() rather
      than after. That is what puts the title, artist and artwork on the lock
      screen and in the Control Center / Android notification, and what makes
      the hardware and headset buttons reach this app instead of the last one
      that played something.

   4. setPositionState() on every timeupdate (throttled). Without it the lock
      screen shows a dead scrubber, and on iOS the 15-second skip buttons
      silently do nothing.

   5. The stream URL is a ticket that already carries its own authorisation, so
      a request the OS media stack replays after the page is backgrounded still
      works — it does not depend on headers the page would have added.
   ============================================================================ */
(function () {
  'use strict';

  var audio = document.getElementById('audio');
  var el = {
    player: document.getElementById('player'),
    cover: document.getElementById('player-cover'),
    title: document.getElementById('player-title'),
    artist: document.getElementById('player-artist'),
    like: document.getElementById('player-like'),
    play: document.getElementById('play'),
    prev: document.getElementById('prev'),
    next: document.getElementById('next'),
    shuffle: document.getElementById('shuffle'),
    repeat: document.getElementById('repeat'),
    repeatOne: document.getElementById('repeat-one'),
    seek: document.getElementById('seek'),
    current: document.getElementById('time-current'),
    total: document.getElementById('time-total'),
    volume: document.getElementById('volume'),
    mute: document.getElementById('mute'),
    preview: document.getElementById('preview-badge')
  };

  var state = {
    queue: [],          // tracks as the API returns them
    order: [],          // indices into queue; shuffled or sequential
    cursor: -1,         // position within `order`
    track: null,
    context: '',        // where the queue came from, e.g. "album:123"
    repeat: 'off',      // off | all | one
    shuffle: false,
    seeking: false,     // the user is dragging the scrubber
    loadToken: 0        // guards against a slow load overwriting a newer one
  };

  var listeners = {};

  function emit(name, detail) {
    (listeners[name] || []).forEach(function (fn) { fn(detail); });
  }

  function on(name, fn) {
    (listeners[name] = listeners[name] || []).push(fn);
  }

  function fmt(seconds) {
    if (!isFinite(seconds) || seconds < 0) return '0:00';
    var m = Math.floor(seconds / 60);
    var s = Math.floor(seconds % 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  function setFill(input) {
    var max = Number(input.max) || 1;
    input.style.setProperty('--fill', ((Number(input.value) / max) * 100) + '%');
  }

  // --- MediaSession --------------------------------------------------------

  var hasSession = 'mediaSession' in navigator;

  function artworkFor(track) {
    var cover = API.safeUrl(track && (track.cover || (track.album && track.album.cover)));
    if (!cover) return [];
    /* Several sizes declared even though Deezer serves one URL per size: the OS
       picks the closest and a single entry gets upscaled badly on a tablet
       lock screen. The sizes are honest for Deezer's `cover_big` (500px). */
    return [
      { src: cover, sizes: '500x500', type: 'image/jpeg' },
      { src: cover, sizes: '256x256', type: 'image/jpeg' }
    ];
  }

  function updateSessionMetadata(track) {
    if (!hasSession || !track) return;
    try {
      navigator.mediaSession.metadata = new window.MediaMetadata({
        title: track.title || '',
        artist: (track.artist && track.artist.name) || '',
        album: (track.album && track.album.title) || 'zer0space Music',
        artwork: artworkFor(track)
      });
    } catch (e) { /* older browsers: controls still work, just without art */ }
  }

  function updateSessionState(playing) {
    if (!hasSession) return;
    try {
      navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
    } catch (e) { /* ignore */ }
  }

  var lastPositionSync = 0;

  function updatePositionState(force) {
    if (!hasSession || !navigator.mediaSession.setPositionState) return;
    var now = Date.now();
    if (!force && now - lastPositionSync < 1000) return;
    lastPositionSync = now;
    var duration = audio.duration;
    if (!isFinite(duration) || duration <= 0) return;
    try {
      navigator.mediaSession.setPositionState({
        duration: duration,
        playbackRate: audio.playbackRate || 1,
        position: Math.min(Math.max(audio.currentTime, 0), duration)
      });
    } catch (e) { /* a position past duration throws; ignore the frame */ }
  }

  function wireSessionHandlers() {
    if (!hasSession) return;
    var handlers = {
      play: function () { play(); },
      pause: function () { pause(); },
      stop: function () { pause(); },
      previoustrack: function () { previous(); },
      nexttrack: function () { next('user'); },
      seekbackward: function (d) { seekBy(-(d && d.seekOffset ? d.seekOffset : 10)); },
      seekforward: function (d) { seekBy(d && d.seekOffset ? d.seekOffset : 10); },
      seekto: function (d) {
        if (!d) return;
        if (d.fastSeek && audio.fastSeek) { audio.fastSeek(d.seekTime); }
        else { audio.currentTime = d.seekTime; }
        updatePositionState(true);
      }
    };
    Object.keys(handlers).forEach(function (action) {
      try {
        navigator.mediaSession.setActionHandler(action, handlers[action]);
      } catch (e) {
        /* Not every browser supports every action; an unsupported one throws
           rather than being ignored, and must not take the others with it. */
      }
    });
  }

  // --- Loading a track -----------------------------------------------------

  async function loadTrack(track, autoplay) {
    var token = ++state.loadToken;
    state.track = track;
    render();
    el.player.dataset.state = 'loading';

    var info;
    try {
      info = await API.stream(track.key);
    } catch (err) {
      if (token !== state.loadToken) return;
      el.player.dataset.state = 'idle';
      emit('error', err);
      /* A track with no source must not stall the queue: in a 50-song playlist
         one region-blocked song would otherwise end the listening session. */
      if (state.queue.length > 1) next('auto');
      return;
    }
    if (token !== state.loadToken) return;  // a newer track was picked meanwhile

    el.preview.hidden = !info.preview;
    if (info.preview) emit('notice', t('player.previewHint'));

    /* Metadata BEFORE the source is set and before play() — see rule 3. */
    updateSessionMetadata(track);

    audio.src = info.url;
    audio.load();

    if (autoplay) {
      try {
        await audio.play();
      } catch (err) {
        /* Autoplay policy: the very first play of a session must come from a
           user gesture. After that the element is "unlocked" and background
           track changes are allowed. Showing a paused player is the honest
           outcome — the listener taps once and everything after it works. */
        el.player.dataset.state = 'paused';
        updateSessionState(false);
        return;
      }
    }
    prefetchAhead();
  }

  function prefetchAhead() {
    var keys = [];
    for (var i = 1; i <= 3; i++) {
      var index = state.order[state.cursor + i];
      if (index !== undefined && state.queue[index]) keys.push(state.queue[index].key);
    }
    if (keys.length) API.prefetch(keys).catch(function () { /* best effort */ });
  }

  // --- Queue ---------------------------------------------------------------

  function buildOrder(startIndex) {
    var indices = state.queue.map(function (_, i) { return i; });
    if (!state.shuffle) {
      state.order = indices;
      state.cursor = startIndex;
      return;
    }
    /* Fisher-Yates over everything except the track being started, which is
       moved to the front — pressing shuffle on a song should play THAT song
       first and shuffle the rest, not jump somewhere else immediately. */
    var rest = indices.filter(function (i) { return i !== startIndex; });
    for (var i = rest.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = rest[i]; rest[i] = rest[j]; rest[j] = tmp;
    }
    state.order = startIndex >= 0 ? [startIndex].concat(rest) : rest;
    state.cursor = 0;
  }

  function playQueue(tracks, startIndex, context) {
    if (!tracks || !tracks.length) return;
    state.queue = tracks.slice();
    state.context = context || '';
    buildOrder(Math.max(0, Math.min(startIndex || 0, tracks.length - 1)));
    loadTrack(state.queue[state.order[state.cursor]], true);
    emit('queuechange');
  }

  function enqueue(track, playNext) {
    if (!state.queue.length) {
      playQueue([track], 0, 'queue');
      return;
    }
    state.queue.push(track);
    var index = state.queue.length - 1;
    if (playNext) state.order.splice(state.cursor + 1, 0, index);
    else state.order.push(index);
    emit('queuechange');
  }

  function currentTrack() {
    return state.track;
  }

  function next(reason) {
    if (!state.queue.length) return;

    if (state.repeat === 'one' && reason === 'auto') {
      audio.currentTime = 0;
      audio.play().catch(function () { /* ignore */ });
      return;
    }

    if (state.cursor + 1 < state.order.length) {
      state.cursor += 1;
    } else if (state.repeat === 'all') {
      state.cursor = 0;
    } else {
      /* End of the queue. Stop on the last track rather than wrapping — and
         leave it loaded so the lock screen still shows what just played. */
      audio.pause();
      el.player.dataset.state = 'paused';
      updateSessionState(false);
      return;
    }
    loadTrack(state.queue[state.order[state.cursor]], true);
    emit('queuechange');
  }

  function previous() {
    /* Spotify's behaviour: within the first 3 seconds go to the previous
       track, after that restart the current one. */
    if (audio.currentTime > 3 || state.cursor <= 0) {
      audio.currentTime = 0;
      updatePositionState(true);
      return;
    }
    state.cursor -= 1;
    loadTrack(state.queue[state.order[state.cursor]], true);
    emit('queuechange');
  }

  function play() {
    if (!state.track) return;
    audio.play().catch(function (err) { emit('error', { code: 'PLAY_BLOCKED', error: String(err) }); });
  }

  function pause() { audio.pause(); }

  function toggle() {
    if (audio.paused) play(); else pause();
  }

  function seekBy(delta) {
    if (!isFinite(audio.duration)) return;
    audio.currentTime = Math.min(Math.max(audio.currentTime + delta, 0), audio.duration);
    updatePositionState(true);
  }

  // --- Rendering -----------------------------------------------------------

  function render() {
    var track = state.track;
    if (!track) {
      el.title.textContent = t('player.nothing');
      el.artist.textContent = '';
      el.cover.removeAttribute('src');
      return;
    }
    el.title.textContent = track.title || '';
    el.artist.textContent = (track.artist && track.artist.name) || '';
    el.artist.href = track.artist && track.artist.id
      ? '#/artist/' + encodeURIComponent(track.artist.id) : '#/home';
    var cover = API.safeUrl(track.cover || (track.album && track.album.cover));
    if (cover) el.cover.src = cover; else el.cover.removeAttribute('src');
    emit('trackchange', track);
  }

  // --- Audio element events ------------------------------------------------

  audio.addEventListener('playing', function () {
    el.player.dataset.state = 'playing';
    updateSessionState(true);
    updatePositionState(true);
  });

  audio.addEventListener('pause', function () {
    if (!audio.ended) el.player.dataset.state = 'paused';
    updateSessionState(false);
    reportPlay();
    saveStateSoon();
  });

  audio.addEventListener('waiting', function () {
    el.player.dataset.state = 'loading';
  });

  audio.addEventListener('ended', function () {
    reportPlay(true);
    next('auto');
  });

  audio.addEventListener('loadedmetadata', function () {
    el.total.textContent = fmt(audio.duration);
    updatePositionState(true);
  });

  audio.addEventListener('timeupdate', function () {
    if (state.seeking) return;
    var duration = audio.duration;
    el.current.textContent = fmt(audio.currentTime);
    if (isFinite(duration) && duration > 0) {
      el.seek.value = String(Math.round((audio.currentTime / duration) * 1000));
      setFill(el.seek);
    }
    updatePositionState(false);
  });

  audio.addEventListener('error', function () {
    /* A 410 from the relay means the resolved URL expired between the ticket
       being minted and the fetch. One silent re-resolve fixes it; a second
       failure is real and the queue moves on. */
    if (!state.track) return;
    if (!state.track._retried) {
      state.track._retried = true;
      loadTrack(state.track, true);
      return;
    }
    state.track._retried = false;
    emit('error', { code: 'MEDIA_UNREACHABLE' });
    if (state.queue.length > 1) next('auto');
  });

  // --- Play reporting and state persistence --------------------------------

  var reported = null;

  function reportPlay(finished) {
    var track = state.track;
    if (!track) return;
    var seconds = Math.floor(audio.currentTime || 0);
    /* Only count a play once per track visit, and only past 20 seconds — the
       threshold keeps "recently played" free of songs that were skipped after
       two seconds of deciding against them. */
    if (!finished && seconds < 20) return;
    if (reported === track.key) return;
    reported = track.key;
    API.played(track, seconds).catch(function () { /* history is not critical */ });
  }

  on('trackchange', function () { reported = null; });

  var saveTimer = null;

  function saveStateSoon() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () {
      if (!state.track) return;
      API.saveState({
        track: state.track,
        position: audio.currentTime || 0,
        queue: state.order.map(function (i) { return state.queue[i]; }).filter(Boolean),
        index: state.cursor,
        context: state.context
      }).catch(function () { /* resume is a convenience, not a guarantee */ });
    }, 2500);
  }

  on('queuechange', saveStateSoon);

  /* `visibilitychange` rather than `unload`: on iOS a tab is frozen or killed
     without ever firing unload, and pagehide is the only reliable last word. */
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') {
      clearTimeout(saveTimer);
      saveStateSoon();
    }
  });

  // --- Controls ------------------------------------------------------------

  el.play.addEventListener('click', toggle);
  el.next.addEventListener('click', function () { next('user'); });
  el.prev.addEventListener('click', previous);

  el.shuffle.addEventListener('click', function () {
    state.shuffle = !state.shuffle;
    el.shuffle.setAttribute('aria-pressed', String(state.shuffle));
    var currentIndex = state.order[state.cursor];
    if (currentIndex !== undefined) buildOrder(currentIndex);
    API.savePrefs({ shuffle: state.shuffle }).catch(function () {});
    emit('queuechange');
  });

  el.repeat.addEventListener('click', function () {
    state.repeat = state.repeat === 'off' ? 'all' : state.repeat === 'all' ? 'one' : 'off';
    el.repeat.setAttribute('aria-pressed', String(state.repeat !== 'off'));
    el.repeatOne.hidden = state.repeat !== 'one';
    API.savePrefs({ repeat: state.repeat }).catch(function () {});
  });

  /* `input` while dragging updates only the label; the actual seek happens on
     `change`, so scrubbing does not fire a network range request per pixel. */
  el.seek.addEventListener('input', function () {
    state.seeking = true;
    setFill(el.seek);
    if (isFinite(audio.duration)) {
      el.current.textContent = fmt((Number(el.seek.value) / 1000) * audio.duration);
    }
  });

  el.seek.addEventListener('change', function () {
    state.seeking = false;
    if (isFinite(audio.duration) && audio.duration > 0) {
      audio.currentTime = (Number(el.seek.value) / 1000) * audio.duration;
      updatePositionState(true);
    }
  });

  el.volume.addEventListener('input', function () {
    audio.volume = Number(el.volume.value) / 100;
    audio.muted = audio.volume === 0;
    setFill(el.volume);
    el.mute.parentElement.classList.toggle('is-muted', audio.muted);
  });

  el.volume.addEventListener('change', function () {
    API.savePrefs({ volume: audio.volume }).catch(function () {});
  });

  el.mute.addEventListener('click', function () {
    audio.muted = !audio.muted;
    el.mute.parentElement.classList.toggle('is-muted', audio.muted);
    el.mute.setAttribute('aria-pressed', String(audio.muted));
  });

  /* Keyboard shortcuts, skipped while typing in a field so Space in the search
     box types a space instead of pausing the music. */
  document.addEventListener('keydown', function (event) {
    var tag = (event.target && event.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || event.target.isContentEditable) return;
    if (event.metaKey || event.ctrlKey || event.altKey) return;

    if (event.code === 'Space') { event.preventDefault(); toggle(); }
    else if (event.key === 'ArrowRight' && event.shiftKey) { next('user'); }
    else if (event.key === 'ArrowLeft' && event.shiftKey) { previous(); }
    else if (event.key === 'ArrowRight') { seekBy(5); }
    else if (event.key === 'ArrowLeft') { seekBy(-5); }
  });

  wireSessionHandlers();

  // --- Public surface ------------------------------------------------------

  window.Player = {
    audio: audio,
    on: on,
    playQueue: playQueue,
    enqueue: enqueue,
    play: play,
    pause: pause,
    toggle: toggle,
    next: next,
    previous: previous,
    current: currentTrack,
    isPlaying: function () { return !audio.paused && !audio.ended; },
    queue: function () {
      return {
        tracks: state.order.map(function (i) { return state.queue[i]; }).filter(Boolean),
        cursor: state.cursor
      };
    },
    jumpTo: function (positionInOrder) {
      if (positionInOrder < 0 || positionInOrder >= state.order.length) return;
      state.cursor = positionInOrder;
      loadTrack(state.queue[state.order[state.cursor]], true);
      emit('queuechange');
    },
    applyPrefs: function (prefs) {
      if (!prefs) return;
      if (typeof prefs.volume === 'number') {
        audio.volume = prefs.volume;
        el.volume.value = String(Math.round(prefs.volume * 100));
        setFill(el.volume);
      }
      if (prefs.repeat) {
        state.repeat = prefs.repeat;
        el.repeat.setAttribute('aria-pressed', String(state.repeat !== 'off'));
        el.repeatOne.hidden = state.repeat !== 'one';
      }
      if (typeof prefs.shuffle === 'boolean') {
        state.shuffle = prefs.shuffle;
        el.shuffle.setAttribute('aria-pressed', String(state.shuffle));
      }
    },
    /* Restore a previous session WITHOUT playing: autoplay would be blocked
       anyway, and a phone that starts blaring on page load is hostile. */
    restore: function (saved) {
      if (!saved || !saved.track) return;
      state.queue = (saved.queue && saved.queue.length) ? saved.queue : [saved.track];
      state.order = state.queue.map(function (_, i) { return i; });
      state.cursor = Math.max(0, Math.min(saved.index || 0, state.order.length - 1));
      state.context = saved.context || '';
      state.track = saved.track;
      render();
      updateSessionMetadata(saved.track);
      el.player.dataset.state = 'paused';
      emit('queuechange');
    },
    likeButton: el.like
  };
})();
