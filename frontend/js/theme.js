// =============================================================================
// js/theme.js
// -----------------------------------------------------------------------------
// Dark/light mode toggle. js/theme-init.js (a separate, synchronous, non
// -module script -- see its own comment) already applies the right theme
// class before first paint; this module just wires up the toggle BUTTON so
// a person can flip it, and keeps the choice in localStorage so it sticks
// across pages and future visits.
// =============================================================================

const STORAGE_KEY = 'snipeit:theme';

export function getTheme() {
  return document.documentElement.classList.contains('light') ? 'light' : 'dark';
}

export function setTheme(theme) {
  const root = document.documentElement;

  // Briefly flag the root so theme.css's `html.theme-transitioning` rule
  // gives every element a short color transition for JUST this swap,
  // instead of every element on the page carrying a permanent transition
  // (which would make unrelated hovers/state changes feel sluggish).
  root.classList.add('theme-transitioning');
  window.setTimeout(() => root.classList.remove('theme-transitioning'), 220);

  root.classList.toggle('light', theme === 'light');
  try {
    window.localStorage.setItem(STORAGE_KEY, theme);
  } catch (e) {
    // Ignore -- worst case the choice only lasts for this page load.
  }

  // Keep mobile browser chrome (the address bar / status bar tint on
  // Android/iOS) in sync with whichever theme is now active, on any page
  // that has the meta tag (see initThemeToggle() below, which creates it
  // on demand if missing).
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', theme === 'light' ? '#f6f7f9' : '#0f1319');
}

export function toggleTheme() {
  setTheme(getTheme() === 'light' ? 'dark' : 'light');
}

// Wires every `[data-action="toggle-theme"]` button on the page (there's
// exactly one per page, in the navbar) and makes sure a `theme-color` meta
// tag exists so setTheme() above always has one to update.
export function initThemeToggle() {
  if (!document.querySelector('meta[name="theme-color"]')) {
    const meta = document.createElement('meta');
    meta.name = 'theme-color';
    meta.content = getTheme() === 'light' ? '#f6f7f9' : '#0f1319';
    document.head.appendChild(meta);
  }
}
