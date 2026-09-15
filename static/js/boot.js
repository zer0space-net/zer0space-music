/* Applies the dashboard's theme before first paint, and sets the base path.

   This app is served from the SAME ORIGIN as the dashboard (the gateway mounts
   it at /music), which is the whole trick: localStorage is shared, so the accent
   the user picked over there is readable here with no API call, no query
   parameter and no schema change. Pick a new colour in the dashboard, come back,
   and Music is already wearing it.

   Loaded first and deferred in <head>: without it the page renders one frame in
   the default blue and then snaps to the user's colour.

   Kept deliberately identical in shape to zer0space-dashboard/static/js/boot.js
   — same key ('zs-theme'), same preset names, same custom-hex handling. If that
   file changes, this one changes with it. */
(function () {
  'use strict';

  var PRESETS = ['aurora', 'cyan', 'violet', 'ember', 'mint', 'rose'];

  function apply(value) {
    if (!value) return;
    var root = document.documentElement;
    if (PRESETS.indexOf(value) !== -1) {
      root.setAttribute('data-theme', value);
      root.style.removeProperty('--accent');
    } else if (/^#[0-9a-f]{6}$/i.test(value)) {
      root.setAttribute('data-theme', 'custom');
      root.style.setProperty('--accent', value);
    }
  }

  try {
    apply(localStorage.getItem('zs-theme'));
  } catch (e) { /* storage blocked — the default accent is a fine outcome */ }

  /* The dashboard may be open in another tab. `storage` fires only in the tabs
     that did NOT write the value, which is exactly this one. */
  window.addEventListener('storage', function (event) {
    if (event.key === 'zs-theme') apply(event.newValue);
  });

  /* Every fetch and every asset URL has to carry the gateway's mount prefix.
     Reading it from the served markup rather than hardcoding '/music' means the
     app still works if the dashboard ever mounts it somewhere else — and when
     it is run directly on :8000 for development, where the prefix is empty. */
  window.ZS_BASE = document.documentElement.getAttribute('data-base') || '';

  window.ZS_THEME = {
    presets: PRESETS,
    apply: apply,
    current: function () {
      try { return localStorage.getItem('zs-theme') || 'aurora'; } catch (e) { return 'aurora'; }
    }
  };
})();
