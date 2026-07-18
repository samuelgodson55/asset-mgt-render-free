# build-tailwind

This folder is **build tooling only** -- it is never shipped or run inside
the app's Docker images. Its only job is to produce one file:

    frontend/css/tailwind.css

which every page in `frontend/*.html` now loads with a plain

```html
<link rel="stylesheet" href="css/tailwind.css">
```

## Why this exists

Previously every HTML file pulled Tailwind from the internet at runtime:

```html
<script src="https://cdn.tailwindcss.com"></script>
<script> tailwind.config = { ... } </script>
```

That's the "Play CDN" build of Tailwind -- it ships the *entire* framework
plus a JIT compiler to the browser, then recompiles your utility classes to
CSS on every page load, in every visitor's browser. Tailwind's own docs say
this is meant for demos/prototyping, not production, for a few concrete
reasons this project ran into:

- **An outage or network block on `cdn.tailwindcss.com` breaks the whole
  UI** -- the app has zero styling until that script loads, since nothing
  is compiled ahead of time.
- **Slower first paint**, since the browser has to download the compiler,
  then compile your classes, before anything looks right.
- **A wider Content-Security-Policy** was required just to allow it:
  `'unsafe-eval'` (for the in-browser JIT compiler) and the CDN host itself
  in `script-src`. Compiling locally means neither is needed anymore --
  see `nginx/default.conf.template`'s CSP header, which has been tightened
  to drop both.

Compiling locally instead means the exact same utility classes get turned
into one small, minified, cacheable `.css` file *once*, at build time, and
every visitor just downloads that static file like any other asset -- no
CDN dependency, no runtime compilation, no extra CSP allowances.

## How to rebuild it

Whenever you add/change/remove Tailwind class names anywhere in
`frontend/*.html` or `frontend/js/**/*.js`, regenerate the compiled
stylesheet:

```bash
cd build-tailwind
npm install     # first time only
npm run build
```

This scans every HTML file and every JS file under `frontend/` (see the
`content` array in `tailwind.config.js`) for class names actually in use,
and writes the minified result to `../frontend/css/tailwind.css`. Nothing
else in the repo needs to change -- the `<link>` tag in each HTML file
already points at that path.

For active development, `npm run watch` rebuilds automatically on save
instead of needing a manual `npm run build` after every edit.

## Theme customization

The colors/fonts that used to be duplicated as an inline
`tailwind.config = {...}` block in all 5 HTML files now live in exactly
one place: `tailwind.config.js` in this folder. Edit it there, rebuild,
and every page picks up the change consistently.

## Nothing to configure in Docker

`frontend/Dockerfile` already just does `COPY frontend/ /usr/share/nginx/html/`
-- since `frontend/css/tailwind.css` is a plain committed file (not
generated inside the Docker build), it gets copied in like any other
static asset with zero changes needed to the Dockerfile or docker-compose
setup. Just make sure you've run `npm run build` here and committed the
resulting `frontend/css/tailwind.css` before deploying.
