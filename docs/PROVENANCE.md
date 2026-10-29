# Provenance

The Florida application reuses generic pipeline and viewer code from the frozen
Rookery Bay pilot.

- Source repository: `/home/tylar/repos/h3lab`
- Source commit: `26c042f`
- Source state: frozen pilot; `main` and Pages are not to be modified.

## Reused

- `src/h3lab/grid.py` — H3 grid construction and label raster
- `src/h3lab/bins.py` — 16-day bin model and year helpers
- `src/h3lab/mosaic.py` — scene compositing and mosaics
- `src/h3lab/aggregate.py` — pixel-to-cell aggregation
- `src/h3lab/change.py` — change fields
- `src/h3lab/export_region.py` — overview/shard export and manifest format
- `src/h3lab/regional.py` — stage orchestration
- `viewer/regional.html` — manifest-driven viewer shell

## Not reused

- Rookery Bay pilot data
- `viewer/pilot.html` and the pilot bundles
- The pilot's hardcoded AOI and resolution constants

Vendored files should retain a short header or adjacent note identifying the
source commit. Changes made for the Florida campaign should be documented in this
repository, not upstream.
