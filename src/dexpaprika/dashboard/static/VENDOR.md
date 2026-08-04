# Vendored front-end assets

- `echarts.min.js.gz` — Apache ECharts **6.1.0**, Apache-2.0 licensed, gzip-compressed
  (359 KB; the raw 1.1 MB min.js exceeds the repo's 1 MB large-file gate, GIT_RULES §1.4).
  Source: `npm pack echarts@6.1.0` → `package/dist/echarts.min.js` (2026-08-04).
  - Served by the dashboard at `GET /static/echarts.min.js` with `Content-Encoding: gzip`
    (the browser decompresses transparently — also a faster load).
  - `dashboard export` decompresses it (stdlib `gzip`) and inlines the JS into the single
    HTML file, so the export opens offline with charts intact.
  NO CDN, no network at view time (S12b invariant). To update: re-`npm pack`, re-gzip -9,
  replace this file, bump the version here.
