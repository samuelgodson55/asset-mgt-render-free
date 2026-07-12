/**
 * tailwind.config.js
 * -----------------------------------------------------------------------------
 * Single source of truth for the theme customizations that used to be
 * copy-pasted as an inline `tailwind.config = {...}` script into every one of
 * the 5 frontend HTML files (index/admin/manager/staff/customer.html).
 *
 * `content` tells Tailwind's JIT compiler which files to scan for class names
 * so it only generates the CSS actually used by the app (this is what keeps
 * the built file small instead of shipping the entire framework).
 */
module.exports = {
  content: [
    "../frontend/*.html",
    "../frontend/js/**/*.js",
  ],
  darkMode: 'class',
  theme: {
    // =========================================================================
    // RESPONSIVE BREAKPOINTS ("screens")
    // -------------------------------------------------------------------------
    // HOW THIS WORKS: every `sm:something` class in the HTML/JS (e.g.
    // `sm:flex-row`, `sm:table-cell`, `sm:px-6`) only takes effect once the
    // BROWSER VIEWPORT is AT LEAST as wide as whatever `sm` is set to below.
    // Below that width, the plain (non-prefixed) class wins instead -- e.g.
    // `flex-col sm:flex-row` is a column on a narrow screen and switches to a
    // row once the viewport crosses the `sm` value. That's the entire
    // mechanism behind every "mobile view" difference in this app: there is
    // no separate mobile template, just one HTML file per page where classes
    // conditionally apply based on this cutoff.
    //
    // TAILWIND'S BUILT-IN DEFAULTS (what you'd get with no override at all)
    // are:
    //   sm: 640px   md: 768px   lg: 1024px   xl: 1280px   2xl: 1536px
    //
    // THIS PROJECT OVERRIDES `sm` (set inside `extend` below -- see the
    // `screens:` line right under `extend: {`) to make the switch from
    // "mobile" to "desktop-ish" styling happen at a NARROWER width than
    // Tailwind's default 640px -- i.e. a wider range of small/narrow browser
    // windows now get the `sm:` (roomier, side-by-side) treatment instead of
    // the stacked mobile one, since 640px meant a lot of ordinary, easily-
    // resized desktop browser windows were being treated as "mobile" the
    // moment they were resized even slightly narrower than full-width.
    //
    // *** IMPORTANT GOTCHA IF YOU EDIT THIS: *** `screens` MUST stay nested
    // inside `extend: {...}` (a few lines below), NOT placed directly under
    // `theme: {...}` the way `colors`/`fontFamily` are further down. Putting
    // it directly under `theme` REPLACES Tailwind's entire screens list with
    // ONLY what you define there -- md:/lg:/xl:/2xl: (`md:block`, `lg:inline`,
    // `lg:grid-cols-2`, `lg:border-b-0`, `lg:border-r`, all currently used in
    // this app) would silently stop working everywhere. Inside `extend`,
    // Tailwind instead MERGES your override with its defaults, so `sm` is the
    // only one that changes and md/lg/xl/2xl stay exactly as Tailwind ships
    // them.
    //
    // TO CHANGE THIS YOURSELF LATER: edit the pixel number on the `sm:` line
    // a few lines down, then rebuild the compiled CSS from `build-tailwind/`:
    //     cd build-tailwind && npm install && npm run build
    // (npm run build writes the result to ../frontend/css/tailwind.css --
    // that compiled file, NOT this config file, is what the browser actually
    // loads, so a rebuild is required every time this number changes.)
    //
    // ONE MORE FILE TO KEEP IN SYNC: `frontend/css/theme.css` has a couple of
    // hand-written `@media (max-width: 479px)` blocks (mobile bottom-sheet
    // modals, the swipe-nav dot strip) that are plain CSS, not Tailwind
    // utilities -- Tailwind's JIT compiler has no way to generate those from
    // class names, so they can't read this config automatically. If you
    // change `sm` below, that file's `@media (max-width: ...)` value should
    // become (your new `sm` value - 1)px to stay lined up with it -- see the
    // comment directly above each of those blocks in theme.css for exactly
    // which lines to touch.
    extend: {
      screens: {
        sm: '480px', // was Tailwind's default 640px; lowered so more ordinary
                     // (non-phone) narrow browser windows get sm:'s side-by-
                     // side/roomier treatment instead of the stacked mobile one.
                     // md/lg/xl/2xl are untouched (see the "IMPORTANT GOTCHA"
                     // comment above for why this must stay inside `extend`).
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      colors: {
        // All of these resolve through CSS custom properties (see
        // css/theme.css) instead of fixed hex values, so that toggling
        // `.light` on <html> re-themes every element using them -- with
        // ZERO class-name changes needed across the HTML/JS. Dark mode's
        // variable values are identical to the previous fixed hex
        // constants, so dark mode is pixel-for-pixel unchanged.
        canvas: 'rgb(var(--canvas) / <alpha-value>)',
        card: 'rgb(var(--card) / <alpha-value>)',
        card2: 'rgb(var(--card2) / <alpha-value>)',
        border: 'rgb(var(--border) / <alpha-value>)',
        slate: {
          50: 'rgb(var(--slate-50) / <alpha-value>)',
          100: 'rgb(var(--slate-100) / <alpha-value>)',
          200: 'rgb(var(--slate-200) / <alpha-value>)',
          300: 'rgb(var(--slate-300) / <alpha-value>)',
          400: 'rgb(var(--slate-400) / <alpha-value>)',
          500: 'rgb(var(--slate-500) / <alpha-value>)',
          600: 'rgb(var(--slate-600) / <alpha-value>)',
          700: 'rgb(var(--slate-700) / <alpha-value>)',
          800: 'rgb(var(--slate-800) / <alpha-value>)',
          900: 'rgb(var(--slate-900) / <alpha-value>)',
          950: 'rgb(var(--slate-950) / <alpha-value>)',
        },
        // Only the "bright pastel on dark background" shades of each
        // accent hue get re-themed -- these are the ones used as plain
        // TEXT/icon color (e.g. text-blue-400) that would be unreadably
        // low-contrast if left unchanged on a white light-mode background.
        // The 500/600/700 shades (buttons, badge borders, opacity-tinted
        // backgrounds) already read fine on both light and dark surfaces
        // as-is, so they're deliberately left as Tailwind's real defaults.
        blue: { 300: 'rgb(var(--blue-300) / <alpha-value>)', 400: 'rgb(var(--blue-400) / <alpha-value>)' },
        emerald: { 300: 'rgb(var(--emerald-300) / <alpha-value>)', 400: 'rgb(var(--emerald-400) / <alpha-value>)' },
        amber: { 200: 'rgb(var(--amber-200) / <alpha-value>)', 300: 'rgb(var(--amber-300) / <alpha-value>)', 400: 'rgb(var(--amber-400) / <alpha-value>)' },
        rose: { 300: 'rgb(var(--rose-300) / <alpha-value>)', 400: 'rgb(var(--rose-400) / <alpha-value>)' },
        violet: { 300: 'rgb(var(--violet-300) / <alpha-value>)', 400: 'rgb(var(--violet-400) / <alpha-value>)' },
      }
    }
  },
  plugins: [],
}
