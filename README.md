# Florida H3 Overview

Southern half of Florida plus Florida EEZ waters — an H3 hexagon time-series
overview application.

This repository is intentionally separate from the frozen Rookery Bay pilot at
`../h3lab`. The pilot stays live and unchanged at `26c042f`; no more production
pushes are planned.

**Start here:** [PLAN.md](PLAN.md)

## Scope

Southern half of Florida (boundary at 27.4°N) plus Florida EEZ waters, as a
single polygon AOI computed in EPSG:3086 (Florida Albers). The committed AOI
covers ~239,655 km² (land ~42,694 km², EEZ ~197,462 km²).

## Status

Campaign configuration, pipeline, viewer, tests, and vendored `h3lab` code are
in place. The 2017 imported pilot dataset is available as a clearly labeled demo
for viewer testing; no real campaign product has been built yet (the full
southern-half + EEZ build is expensive).

**Start here:** [PLAN.md](PLAN.md)

## Layout

```text
campaigns/   Florida campaign configuration and AOI (committed)
src/         Florida pipeline code (vendored from h3lab, then evolved)
viewer/      Florida viewer (static HTML/JS; served over HTTP)
tests/       Tests for campaign loading, grids, manifests, and reproducibility
.github/     CI and optional GitHub Pages deploy workflows
data/        Generated products and caches (gitignored)
dist/        Deploy staging (gitignored)
```

## Data

Generated data belongs under `data/florida/` and is not committed. The catalog
at `data/florida/catalog.json` is the viewer entry point. The demo product from
the frozen pilot lives at `data/florida/demo/2017/`.

## Build and test

Requires `/home/tylar/miniconda3/bin/python` (Python 3.13) with the geospatial
and STAC stack.

```bash
# validate everything (lint, tests, viewer JS syntax)
make check

# run just the pytest suite
make test

# serve the repo over HTTP and open the viewer
make serve          # http://127.0.0.1:8000/viewer/florida.html?year=2017
```

## Deploy

The viewer is a static HTML/JS app that must be served over HTTP (browsers block
`fetch()` on `file://`).

```bash
# validate, then publish the viewer + demo product to the gh-pages branch
make deploy         # -> https://tylar.info/florida-h3-overview/florida.html?year=2017
```

`make deploy` runs the full validation suite before pushing, so a failed test or
syntax error blocks the deploy.

### GitHub Actions

- `.github/workflows/ci.yml` — runs pytest, py_compile, and viewer JS syntax
  checks on every push/PR to `main`.
- `.github/workflows/pages.yml` — optional GitHub Pages deploy workflow (no
  longer required; `make deploy` handles publishing locally).

`data/` is gitignored and local-only on `main`, but `make deploy` force-adds the
demo product to the `gh-pages` branch so the catalog loads automatically on the
deployed site.

For local testing, serve the repo and pass the catalog path explicitly:

```bash
make serve          # http://localhost:8000/viewer/florida.html
open "http://localhost:8000/viewer/florida.html?catalog=../data/florida/catalog.json&year=2017"
```

One-time: set GitHub Pages source to 'Deploy from a branch' → `gh-pages` in
repository settings (Pages tab). After that, `make deploy` is all you need — no
browser status-checking required.

## Non-goals for now

- No changes to the pilot repository.
- No per-shard repositories.
- No statewide res 9 build.

## Provenance

Vendored `h3lab` code comes from `/home/tylar/repos/h3lab` at commit `26c042f`;
see [docs/PROVENANCE.md](docs/PROVENANCE.md).
