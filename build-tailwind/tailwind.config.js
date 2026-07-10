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
    extend: {
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
