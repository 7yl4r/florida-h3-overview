# Florida H3 Overview

Statewide Florida H3 time-series application.

This repository is intentionally separate from the frozen Rookery Bay pilot at
`../h3lab`. The pilot stays live and unchanged at `26c042f`; no more production
pushes are planned.

**Start here:** [PLAN.md](PLAN.md)

## Status

Planning. The first milestone is a 2017 statewide res 7 overview, followed by
res 8 detail and then a year-by-year expansion.

## Layout

```text
campaigns/   Florida campaign configuration and AOI
src/         Florida pipeline code (vendored from h3lab, then evolved)
viewer/      Florida viewer
tests/       Tests for campaign loading, grids, manifests, and reproducibility
data/        Generated products and caches (gitignored)
```

## Data

Generated data belongs under `data/florida/` and is not committed. The catalog
at `data/florida/catalog.json` is the viewer entry point.

## Non-goals for now

- No Pages deployment.
- No changes to the pilot repository.
- No per-shard repositories.
- No statewide res 9 build.
