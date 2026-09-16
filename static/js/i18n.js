/* German / English, same contract as the dashboard's static/js/i18n.js.

   Three rules carried over verbatim, because breaking them is how the dashboard
   ended up with half-translated pages:

   1. Every key exists in BOTH dictionaries. checkParity() reports drift in the
      console; a key in one but not the other is a bug, not a fallback.
   2. Markup carries the key in an attribute (data-i18n, data-i18n-ph,
      data-i18n-title, data-i18n-aria), with the German text inline as the
      pre-JS default.
   3. JavaScript calls t('key'), never a literal. t('key', {n: 3}) fills
      {n}-style slots.

   Server errors are not translated on the server: every failure carries a
   stable `code`, and tError() resolves it to an `err.<CODE>` key. An unknown
   code falls back to the server's English text, so a missing key degrades to
   readable rather than blank.

   The chosen language lives in localStorage under the dashboard's own key
   ('zs-lang'), so switching language in either app switches it in both. */
(function () {
  'use strict';

  var STORE = 'zs-lang';

  var de = {
    'nav.home': 'Start',
    'nav.search': 'Suchen',
    'nav.library': 'Bibliothek',
    'nav.playlists': 'Playlists',
    'nav.liked': 'Lieblingssongs',
    'nav.dashboard': 'Dashboard',
    'nav.close': 'Schließen',
    'nav.openMenu': 'Menü öffnen',
    'nav.back': 'Zurück',
    'nav.forward': 'Vor',

    'common.loading': 'Lädt …',
    'common.cancel': 'Abbrechen',
    'common.close': 'Schließen',
    'common.save': 'Speichern',
    'common.create': 'Erstellen',
    'common.delete': 'Löschen',
    'common.rename': 'Umbenennen',
    'common.more': 'Mehr',
    'common.songs': '{n} Songs',
    'common.song': '1 Song',
    'common.retry': 'Erneut versuchen',
    'common.nothingHere': 'Hier ist noch nichts.',

    'search.placeholder': 'Was möchtest du hören?',
    'search.title': 'Suchergebnisse für „{q}“',
    'search.empty': 'Nichts gefunden. Anderer Suchbegriff?',
    'search.start': 'Such nach Songs, Alben oder Künstlern.',
    'search.tracks': 'Songs',
    'search.albums': 'Alben',
    'search.artists': 'Künstler',
    'search.playlists': 'Playlists',
    'search.browse': 'Stöbern',

    'home.goodMorning': 'Guten Morgen',
    'home.goodDay': 'Guten Tag',
    'home.goodEvening': 'Guten Abend',
    'home.recent': 'Zuletzt gehört',
    'home.charts': 'Aktuelle Charts',
    'home.topAlbums': 'Top-Alben',
    'home.topArtists': 'Deine Künstler',
    'home.chartArtists': 'Angesagte Künstler',
    'home.playlists': 'Deine Playlists',
    'home.editorial': 'Ausgewählte Playlists',

    'library.title': 'Deine Bibliothek',
    'library.liked': 'Lieblingssongs',
    'library.playlists': 'Playlists',
    'library.recent': 'Zuletzt gehört',
    'library.emptyLiked': 'Noch keine Lieblingssongs. Tipp auf das Herz neben einem Song.',
    'library.emptyPlaylists': 'Noch keine Playlists.',

    'playlist.create': 'Neue Playlist',
    'playlist.name': 'Name',
    'playlist.description': 'Beschreibung',
    'playlist.created': 'Playlist erstellt',
    'playlist.deleted': 'Playlist gelöscht',
    'playlist.confirmDelete': '„{name}“ wirklich löschen?',
    'playlist.addTo': 'Zu Playlist hinzufügen',
    'playlist.added': 'Zu „{name}“ hinzugefügt',
    'playlist.removed': 'Aus der Playlist entfernt',
    'playlist.empty': 'Diese Playlist ist noch leer.',
    'playlist.pickOne': 'Playlist auswählen',
    'playlist.importSpotify': 'Von Spotify importieren',
    'playlist.importUrl': 'Link zur Spotify-Playlist',
    'playlist.importUrlPh': 'https://open.spotify.com/playlist/…',
    'playlist.importing': 'Playlist wird importiert …',
    'playlist.importDone': '„{name}“ importiert — {matched} von {total} Songs gefunden',
    'playlist.importTruncated': ' (nur die ersten {max} Songs, Spotify zeigt nicht mehr her)',
    'playlist.importUnmatchedTitle': '{n} Songs nicht gefunden',
    'playlist.importUnmatchedIntro': 'Diese Songs aus der Spotify-Playlist gibt es (unter diesem Titel/Artist) nicht im Katalog:',
    'err.SPOTIFY_BAD_URL': 'Das sieht nicht nach einem Spotify-Playlist-Link aus.',
    'err.SPOTIFY_UNAVAILABLE': 'Spotify ist gerade nicht erreichbar, oder die Playlist ist privat.',
    'err.SPOTIFY_EMPTY': 'Diese Playlist enthält keine Songs.',
    'playlist.shufflePlay': 'Zufallswiedergabe starten',
    'playlist.export': 'Exportieren',
    'playlist.exportAll': 'Alle exportieren',
    'playlist.importBackup': 'Backup importieren',
    'playlist.backupImported': '{n} Playlist(s) wiederhergestellt',
    'err.BAD_BACKUP': 'Das ist keine gültige zer0space-Music-Backup-Datei.',
    'err.SPOTIFY_NO_MATCHES': 'Keiner dieser Songs wurde im Katalog gefunden.',

    'album.kind': 'Album',
    'artist.kind': 'Künstler',
    'playlist.kind': 'Playlist',
    'artist.topTracks': 'Beliebte Songs',
    'artist.albums': 'Alben',
    'artist.related': 'Ähnliche Künstler',
    'artist.fans': '{n} Fans',

    'track.title': 'Titel',
    'track.album': 'Album',
    'track.duration': 'Dauer',
    'track.liked': 'Zu Lieblingssongs hinzugefügt',
    'track.unliked': 'Aus Lieblingssongs entfernt',
    'track.playNext': 'Als Nächstes spielen',
    'track.queued': 'Zur Warteschlange hinzugefügt',
    'track.goArtist': 'Zum Künstler',
    'track.goAlbum': 'Zum Album',

    'player.nothing': 'Nichts läuft',
    'player.play': 'Abspielen',
    'player.pause': 'Pause',
    'player.next': 'Nächster Titel',
    'player.previous': 'Vorheriger Titel',
    'player.shuffle': 'Zufallswiedergabe',
    'player.repeat': 'Wiederholen',
    'player.seek': 'Position',
    'player.volume': 'Lautstärke',
    'player.mute': 'Stumm',
    'player.like': 'Zu Lieblingssongs',
    'player.queue': 'Warteschlange',
    'player.previewOnly': 'Nur Vorschau',
    'player.previewHint': 'Für diesen Song wurde nur die 30-Sekunden-Vorschau gefunden.',

    'queue.title': 'Warteschlange',
    'queue.nowPlaying': 'Läuft gerade',
    'queue.next': 'Als Nächstes',
    'queue.empty': 'Die Warteschlange ist leer.',

    'lang.toggle': 'Sprache wechseln',

    'err.NO_SOURCE': 'Für diesen Song wurde keine abspielbare Quelle gefunden.',
    'err.UNKNOWN_TRACK': 'Dieser Song ist unbekannt.',
    'err.BAD_TICKET': 'Der Wiedergabe-Link ist abgelaufen. Bitte neu starten.',
    'err.MEDIA_UNREACHABLE': 'Die Audioquelle ist nicht erreichbar.',
    'err.MEDIA_REJECTED': 'Die Audioquelle hat die Anfrage abgelehnt.',
    'err.CATALOG_UNAVAILABLE': 'Der Musikkatalog ist gerade nicht erreichbar.',
    'err.DB_UNAVAILABLE': 'Die Datenbank ist nicht erreichbar.',
    'err.SERVICE_AUTH': 'Anmeldung fehlgeschlagen — bitte im Dashboard neu anmelden.',
    'err.NO_IDENTITY': 'Bitte über das zer0space Dashboard anmelden.',
    'err.NOT_FOUND': 'Nicht gefunden.',
    'err.BAD_NAME': 'Bitte einen Namen eingeben.',
    'err.TOO_MANY': 'Limit erreicht.',
    'err.BAD_TRACK': 'Ungültiger Song.',
    'err.INTERNAL': 'Unerwarteter Fehler.',
    'err.NETWORK': 'Keine Verbindung zum Music-Dienst.'
  };

  var en = {
    'nav.home': 'Home',
    'nav.search': 'Search',
    'nav.library': 'Library',
    'nav.playlists': 'Playlists',
    'nav.liked': 'Liked songs',
    'nav.dashboard': 'Dashboard',
    'nav.close': 'Close',
    'nav.openMenu': 'Open menu',
    'nav.back': 'Back',
    'nav.forward': 'Forward',

    'common.loading': 'Loading …',
    'common.cancel': 'Cancel',
    'common.close': 'Close',
    'common.save': 'Save',
    'common.create': 'Create',
    'common.delete': 'Delete',
    'common.rename': 'Rename',
    'common.more': 'More',
    'common.songs': '{n} songs',
    'common.song': '1 song',
    'common.retry': 'Try again',
    'common.nothingHere': 'Nothing here yet.',

    'search.placeholder': 'What do you want to listen to?',
    'search.title': 'Results for "{q}"',
    'search.empty': 'Nothing found. Try another search?',
    'search.start': 'Search for songs, albums or artists.',
    'search.tracks': 'Songs',
    'search.albums': 'Albums',
    'search.artists': 'Artists',
    'search.playlists': 'Playlists',
    'search.browse': 'Browse',

    'home.goodMorning': 'Good morning',
    'home.goodDay': 'Good afternoon',
    'home.goodEvening': 'Good evening',
    'home.recent': 'Recently played',
    'home.charts': 'Charts',
    'home.topAlbums': 'Top albums',
    'home.topArtists': 'Your artists',
    'home.chartArtists': 'Trending artists',
    'home.playlists': 'Your playlists',
    'home.editorial': 'Featured playlists',

    'library.title': 'Your library',
    'library.liked': 'Liked songs',
    'library.playlists': 'Playlists',
    'library.recent': 'Recently played',
    'library.emptyLiked': 'No liked songs yet. Tap the heart next to a song.',
    'library.emptyPlaylists': 'No playlists yet.',

    'playlist.create': 'New playlist',
    'playlist.name': 'Name',
    'playlist.description': 'Description',
    'playlist.created': 'Playlist created',
    'playlist.deleted': 'Playlist deleted',
    'playlist.confirmDelete': 'Really delete "{name}"?',
    'playlist.addTo': 'Add to playlist',
    'playlist.added': 'Added to "{name}"',
    'playlist.removed': 'Removed from the playlist',
    'playlist.empty': 'This playlist is still empty.',
    'playlist.pickOne': 'Choose a playlist',
    'playlist.importSpotify': 'Import from Spotify',
    'playlist.importUrl': 'Spotify playlist link',
    'playlist.importUrlPh': 'https://open.spotify.com/playlist/…',
    'playlist.importing': 'Importing playlist …',
    'playlist.importDone': '"{name}" imported — {matched} of {total} songs found',
    'playlist.importTruncated': ' (first {max} songs only, that is all Spotify shows)',
    'playlist.importUnmatchedTitle': '{n} songs not found',
    'playlist.importUnmatchedIntro': 'These songs from the Spotify playlist are not in the catalogue (under this title/artist):',
    'err.SPOTIFY_BAD_URL': 'That does not look like a Spotify playlist link.',
    'err.SPOTIFY_UNAVAILABLE': 'Spotify is unreachable right now, or the playlist is private.',
    'err.SPOTIFY_EMPTY': 'That playlist has no songs.',
    'playlist.shufflePlay': 'Start shuffle play',
    'playlist.export': 'Export',
    'playlist.exportAll': 'Export all',
    'playlist.importBackup': 'Import backup',
    'playlist.backupImported': '{n} playlist(s) restored',
    'err.BAD_BACKUP': 'Not a valid zer0space Music backup file.',
    'err.SPOTIFY_NO_MATCHES': 'None of these songs were found in the catalogue.',

    'album.kind': 'Album',
    'artist.kind': 'Artist',
    'playlist.kind': 'Playlist',
    'artist.topTracks': 'Popular',
    'artist.albums': 'Albums',
    'artist.related': 'Similar artists',
    'artist.fans': '{n} fans',

    'track.title': 'Title',
    'track.album': 'Album',
    'track.duration': 'Duration',
    'track.liked': 'Added to liked songs',
    'track.unliked': 'Removed from liked songs',
    'track.playNext': 'Play next',
    'track.queued': 'Added to the queue',
    'track.goArtist': 'Go to artist',
    'track.goAlbum': 'Go to album',

    'player.nothing': 'Nothing playing',
    'player.play': 'Play',
    'player.pause': 'Pause',
    'player.next': 'Next track',
    'player.previous': 'Previous track',
    'player.shuffle': 'Shuffle',
    'player.repeat': 'Repeat',
    'player.seek': 'Position',
    'player.volume': 'Volume',
    'player.mute': 'Mute',
    'player.like': 'Add to liked songs',
    'player.queue': 'Queue',
    'player.previewOnly': 'Preview only',
    'player.previewHint': 'Only the 30-second preview was found for this song.',

    'queue.title': 'Queue',
    'queue.nowPlaying': 'Now playing',
    'queue.next': 'Next up',
    'queue.empty': 'The queue is empty.',

    'lang.toggle': 'Switch language',

    'err.NO_SOURCE': 'No playable source was found for this song.',
    'err.UNKNOWN_TRACK': 'Unknown song.',
    'err.BAD_TICKET': 'The playback link expired. Please start again.',
    'err.MEDIA_UNREACHABLE': 'The audio source could not be reached.',
    'err.MEDIA_REJECTED': 'The audio source rejected the request.',
    'err.CATALOG_UNAVAILABLE': 'The music catalogue is temporarily unreachable.',
    'err.DB_UNAVAILABLE': 'The database is unavailable.',
    'err.SERVICE_AUTH': 'Authentication failed — please sign in to the dashboard again.',
    'err.NO_IDENTITY': 'Please sign in through the zer0space dashboard.',
    'err.NOT_FOUND': 'Not found.',
    'err.BAD_NAME': 'Please enter a name.',
    'err.TOO_MANY': 'Limit reached.',
    'err.BAD_TRACK': 'Invalid song.',
    'err.INTERNAL': 'Unexpected error.',
    'err.NETWORK': 'Cannot reach the Music service.'
  };

  var dicts = { de: de, en: en };
  var lang = 'de';

  try {
    var stored = localStorage.getItem(STORE);
    if (stored === 'de' || stored === 'en') lang = stored;
  } catch (e) { /* default stands */ }

  function t(key, vars) {
    var value = (dicts[lang] && dicts[lang][key]) || (de[key]) || key;
    if (!vars) return value;
    return value.replace(/\{(\w+)\}/g, function (match, name) {
      return Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match;
    });
  }

  function applyI18n(root) {
    var scope = root || document;
    scope.querySelectorAll('[data-i18n]').forEach(function (el) {
      el.textContent = t(el.getAttribute('data-i18n'));
    });
    scope.querySelectorAll('[data-i18n-ph]').forEach(function (el) {
      el.setAttribute('placeholder', t(el.getAttribute('data-i18n-ph')));
    });
    scope.querySelectorAll('[data-i18n-title]').forEach(function (el) {
      el.setAttribute('title', t(el.getAttribute('data-i18n-title')));
    });
    scope.querySelectorAll('[data-i18n-aria]').forEach(function (el) {
      el.setAttribute('aria-label', t(el.getAttribute('data-i18n-aria')));
    });
    document.documentElement.lang = lang;
  }

  function setLang(next) {
    if (next !== 'de' && next !== 'en') return;
    lang = next;
    try { localStorage.setItem(STORE, next); } catch (e) { /* ignore */ }
    applyI18n();
    /* Views built in JavaScript carry no data-i18n attributes, so applyI18n
       cannot reach them. app.js listens for this and re-renders. */
    window.dispatchEvent(new CustomEvent('languagechange:zs', { detail: { lang: next } }));
  }

  /* An API error object -> readable text. */
  function tError(data) {
    if (!data) return t('err.INTERNAL');
    var code = data.code;
    if (code && dicts[lang]['err.' + code]) return t('err.' + code);
    return data.error || t('err.INTERNAL');
  }

  function checkParity() {
    var missing = [];
    Object.keys(de).forEach(function (k) { if (!(k in en)) missing.push('en: ' + k); });
    Object.keys(en).forEach(function (k) { if (!(k in de)) missing.push('de: ' + k); });
    if (missing.length) console.warn('[i18n] missing keys:', missing);
    return missing;
  }

  window.t = t;
  window.I18N = {
    get lang() { return lang; },
    setLang: setLang,
    toggle: function () { setLang(lang === 'de' ? 'en' : 'de'); },
    apply: applyI18n,
    tError: tError,
    checkParity: checkParity
  };

  /* The dashboard in another tab can change the language too. */
  window.addEventListener('storage', function (event) {
    if (event.key === STORE && event.newValue !== lang) setLang(event.newValue);
  });
})();
