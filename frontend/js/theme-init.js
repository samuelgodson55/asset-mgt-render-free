// =============================================================================
// js/theme-init.js
// -----------------------------------------------------------------------------
// Deliberately a tiny, external, NON-module, SYNCHRONOUS script (same
// reasoning as js/auth-guard.js -- external rather than inline so the CSP
// doesn't need 'unsafe-inline', and loaded before css/tailwind.css so it
// can add `.light` to <html> BEFORE the browser's first paint).
//
// Without this, the page would render in whichever theme tailwind.css/
// theme.css default to (dark), then a moment later main.js's module script
// would load, read localStorage, and flip the theme -- visible as a jarring
// flash of dark-then-light (or vice versa) on every single page load for
// anyone who has light mode saved. Running synchronously in <head>, before
// any paint, avoids that entirely.
//
// Preference order: an explicit saved choice always wins; otherwise fall
// back to the OS/browser's prefers-color-scheme, so a first-time visitor
// gets a sensible default instead of always landing in dark mode.
(function () {
  try {
    var saved = window.localStorage.getItem('snipeit:theme');
    var theme = saved === 'light' || saved === 'dark'
      ? saved
      : (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    if (theme === 'light') document.documentElement.classList.add('light');
  } catch (e) {
    // localStorage/matchMedia can throw in some private-browsing modes --
    // fail open to the default dark theme rather than let this crash the
    // page before anything else has even loaded.
  }
})();
