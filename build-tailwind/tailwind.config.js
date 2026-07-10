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
        canvas: '#0f1319',
        card: '#171c25',
        card2: '#1d232e',
        border: '#2a3140',
      }
    }
  },
  plugins: [],
}
