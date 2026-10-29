# Florida H3 Overview — Campaign Plan

**Status:** planning; repository initialized  
**Date:** 2026-10-29  
**Repository:** `/home/tylar/repos/florida-h3-overview`  
**Frozen upstream reference:** `/home/tylar/repos/h3lab` at `26c042f`

---

## 0. The structural change: this is a separate application and repository

The Florida overview is **not** a branch of the Rookery Bay pilot. It starts here,
in its own folder and git repository under `../repos/`.

This has several consequences:

- The pilot at `h3lab` is frozen at `26c042f`. Do not modify `main`, Pages,
  `viewer/pilot.html`, the pilot bundles, or `data/rookery_bay_h3_*`.
- No more pushes to production. The live pilot stays as it is.
- This repository owns the Florida campaign configuration, pipeline, viewer,
  tests, and generated catalog.
- The new application has **no runtime dependency on the frozen pilot**. Reusable
  code is vendored/copied from `h3lab` at the pinned commit, with provenance
  recorded. The pilot is a reference, not a library that keeps changing
  underneath the Florida build.
- Florida data lives under this repository's `data/` directory and is gitignored.
  Only code, campaign config, schemas, and tests are committed.

The rest of this document is the plan that was previously recommended, updated
for the separate-repository decision.

---

## 1. What we are building

A statewide Florida version of the existing identification tool: a hexagon map
where each cell can be read as a time series, and where a human can see where and
when something changed.

The first goal is a **statewide overview**, not statewide maximum detail. The
application should open on all of Florida, show a complete year at res 7, and let
the user zoom into res 8 detail that is loaded on demand.

This remains tool A (identification). Detailed event characterisation is still a
separate future tool.

---

## 2. Scope: a Florida polygon, not a bounding box

Use a **Florida state boundary plus a ~10 km offshore buffer**, not the state
bounding box. The bounding box is mostly Gulf and Atlantic water and would
roughly triple the work.

Use **Florida Albers, EPSG:3086**, for the analysis raster and projected
geometry. The pilot's UTM 17N is correct for Rookery Bay and South Florida, but
the Florida panhandle spans UTM zone 16. A statewide equal-area projection keeps
cell/raster aggregation consistent across the state.

Keep the 30 m working pixel size to match Landsat Collection 2 Level-2.

Quick H3 full-containment counts using a Census state boundary:

| scope | area | res 6 | res 7 | res 8 |
|---|---:|---:|---:|---:|
| South Florida bbox | 86,585 km² | 2,516 | 18,115 | 128,236 |
| Florida state + 10 km buffer | 184,285 km² | 5,178 | 37,579 | 266,507 |
| Florida state bbox | ~555,000 km² | 15,805 | 118,601 | ~3× the polygon |

These counts are a planning estimate from a quick H3 test, not a final grid.
They should be re-measured in the new repository with the committed AOI polygon
and the exact containment rule.

For the first year, use Landsat 7 and 8, matching the existing 2017 South Florida
build. Add Landsat 5 and 9 when the build moves earlier or later than 2017.

---

## 3. Product tiers

Make **year** the top-level product partition. Each year is independently
buildable, reproducible, and deletable.

| tier | resolution | role | per year | full record |
|---|---|---:|---:|---:|
| statewide overview | res 7 | first load; statewide map | ~17 MB | ~450 MB |
| detail | res 8 | viewport-loaded shards | ~135 MB | ~3.5 GB |
| optional coarse | res 6 | one-file all-years overview | ~2.5 MB | ~65 MB |

Sizes are scaled from the measured 2017 South Florida build (64.5 MB product,
15.27 GB mosaic, ~3.5 hours). They are order-of-magnitude estimates, not Florida
measurements.

Res 7 is the statewide view because a res 7 cell is ~5.2 km² and ~2.8 km across:
coarse enough for a state map, fine enough to see regional patterns. Res 8 is
the detail tier and is loaded only for the viewport. Res 6 is optional and only
worth adding if a single all-years overview file becomes necessary.

Do not build res 9 statewide. Res 9 and finer are on-demand products for selected
regions.

---

## 4. Repository and code organization

```text
florida-h3-overview/
  README.md
  PLAN.md
  campaigns/
    florida.json
    florida.geojson
  src/florida_h3/
    campaign.py
    grid.py
    mosaic.py
    bins.py
    aggregate.py
    change.py
    export.py
    derive.py
    run.py
  viewer/
    florida.html
  tests/
  data/                 # gitignored; generated products and caches
    florida/
      catalog.json
      cache/
      product/
```

The exact module split can follow the vendored `h3lab` code, but the campaign
configuration must not be a set of module-level constants. It is a first-class
input.

`campaigns/florida.json` should describe:

- campaign name and schema version
- AOI polygon path and buffer distance
- analysis CRS and pixel size
- resolutions to build
- year range and bin model
- source platforms
- output root

The application reads the campaign, computes the grid, and writes a catalog. It
does not silently override the AOI with a hardcoded bbox.

### Code reuse from `h3lab`

Vendor the reusable pipeline and viewer code from `h3lab` at `26c042f`:

- `grid.py`, `bins.py`, `mosaic.py`, `aggregate.py`, `change.py`,
  `export_region.py`, and the relevant parts of `regional.py`
- `viewer/regional.html` as the starting point for `viewer/florida.html`

Record the source commit in `docs/PROVENANCE.md` or in the README. Do not copy
the Rookery Bay pilot data, the pilot viewer, or the pilot's hardcoded AOI
constants.

---

## 5. Data layout

Keep one local product tree plus a catalog. Do not create a repository per shard.

```text
data/florida/
  catalog.json
  cache/
    stac/2017.json
    mosaic/2017/bin000/...
    h3/2017/res7/bin000.npz
    h3/2017/res8/bin000.npz
  product/
    2017/
      manifest.json
      overview_res7.bin.gz
      region_<parent>_res8.bin.gz
    2018/
      ...
    derived/
      manifest.json
      change_res8.bin.gz
      assessment_res7.bin.gz
      assessment_res8/<parent>.bin.gz
```

- `catalog.json` is the entry point: campaign metadata, years, tier paths,
  sizes, hashes, source versions, AOI hash, and code commit.
- `product/<year>/` contains observations for that year. This is the unit that
  can be built, validated, and deleted independently.
- `product/derived/` contains whole-record layers that cannot be computed
  correctly one year at a time.
- `data/` is gitignored.

If remote hosting is needed later, use **one bucket with prefixes**, not one
repository per shard. The deferred `publish_datasets.py` approach is not part of
this plan.

---

## 6. Observations vs derived layers

This is the most important data-model change for a multi-year Florida build.

**Observations** are per year: bands, indices, observation counts, and any change
field that only needs that year.

**Derived layers** are whole-record: change points, trend, seasonality,
stability, and per-cell assessment. These span years and must be computed from
the full series, not from one year in isolation.

Compute derived layers from the per-cell `h3/*.npz` cache after the years exist.
That cache is small — roughly 150 MB/year for all Florida — so keep it. Delete
the 30 m mosaic after each year is validated. The mosaic is the large transient
(~32 GB/year); the persistent product plus analysis cache is only a few GB for
the full record.

This mirrors the pilot's split between spectra and assessment, which is working
well.

---

## 7. Build order

1. **2017 statewide res 7 only.**
   This is the first real wider view. It uses the existing Landsat 7/8 path,
   gives a complete Irma year, and avoids res 8 cost while the scope, CRS,
   viewer, and sizes are validated.
2. **2017 res 8 detail** for all Florida, or for 3–5 priority regions first.
   This is the first complete statewide vertical slice.
3. **2015–2021** at res 7 statewide, with res 8 detail for priority regions.
4. **Full 2000–2026** at res 7; res 8 year-by-year, on demand.
5. Res 9 only for selected regions, never statewide by default.

The existing South Florida 2017 build took about 3.5 hours. All Florida is roughly
2× the area and scene count, so expect ~7–8 hours/year. The full record is a
multi-day compute campaign, not a single run.

Each year should follow the same stage order as the pilot:

```text
stac -> mosaic -> h3 -> product -> derive -> validate -> delete mosaic
```

The `derive` stage runs after all years that it needs are present. It should be
able to read the accumulated `h3` cache without re-reading the mosaics.

---

## 8. Viewer

The existing `viewer/regional.html` already does the hard parts: manifest
loading, res 7 overview, viewport-loaded res 8 shards, and a two-level timeline.
Copy it into this repository and generalize it rather than writing a new viewer.

The Florida viewer should:

- load `catalog.json`, then the selected year's manifest;
- load the res 7 statewide overview first;
- load res 8 shards by viewport, with an LRU cache;
- load derived layers separately, so change/trend can be shown without loading
  every year;
- provide a year selector and 16-day bin timeline;
- preserve URL state for year, bin, resolution, and selected cell;
- keep the pilot viewer untouched as the reference.

The viewer is local-only for now. No Pages deployment.

---

## 9. Code changes needed in this repository

1. Add the campaign loader and `campaigns/florida.json`.
2. Add polygon AOI support to grid construction and the analysis raster.
3. Switch the analysis CRS to EPSG:3086 for this campaign.
4. Make output and cache paths year-scoped.
5. Add the whole-record `derive` stage.
6. Generalize the regional viewer and add `viewer/florida.html`.
7. Add tests for campaign loading, polygon grid counts, year partitioning,
   manifest round-trip, and byte reproducibility.
8. Add a provenance note for vendored `h3lab` code.

---

## 10. Storage and compute budget

| item | per year | full record |
|---|---:|---:|
| 30 m mosaic (transient) | ~32 GB | ~850 GB if kept |
| per-cell H3 cache (keep) | ~150 MB | ~4 GB |
| res 7 product | ~17 MB | ~450 MB |
| res 8 product | ~135 MB | ~3.5 GB |
| derived layers | small | tens of MB |

With year-by-year processing and mosaic deletion, peak disk is roughly one
year's mosaic plus the accumulated product/cache, not the full-record mosaic.
That makes the full record feasible on a workstation with a few hundred GB free.

---

## 11. First milestone

The first milestone is deliberately small:

1. Tag the pilot at `26c042f` as `pilot-v1` in `h3lab`.
2. Confirm this repository is the new home and commit this plan.
3. Add `campaigns/florida.json` and the state polygon.
4. Re-measure the grid: expect ~37.6k res 7 cells and ~266.5k res 8 cells for
   the buffered state polygon.
5. Build **2017 res 7 statewide only**.
6. Open it in the generalized viewer.
7. Review the statewide map and sizes before committing to 2017 res 8.

Acceptance for the milestone:

- the statewide res 7 overview loads in the viewer;
- the cell count matches the committed AOI;
- the product size is in the expected range;
- the 2017 Irma signal is visible at state scale;
- the pilot repository is unchanged.

---

## 12. Open decisions

These do not block the first milestone, but should be settled before the
multi-year build:

- **First year range:** 2017 only first (recommended), or jump to 2015–2021.
- **Res 8 scope:** all Florida for 2017, or priority regions first.
- **Optional res 6 tier:** add only if a single all-years overview file is
  needed.
- **MODIS:** keep Landsat-only for now; consider MODIS at res 7 later for a daily
  statewide overview.
- **Remote hosting:** local-only until the product and viewer are stable; if
  needed later, one bucket with prefixes.

---

## Decision summary

| id | decision |
|---|---|
| FL-001 | Florida overview is a separate folder and git repo: `florida-h3-overview`. |
| FL-002 | The `h3lab` pilot is frozen at `26c042f`; no prod pushes. |
| FL-003 | Use a Florida state polygon plus ~10 km offshore buffer, not the state bbox. |
| FL-004 | Use Florida Albers, EPSG:3086, for the statewide analysis raster. |
| FL-005 | Product tiers: res 7 statewide overview, res 8 viewport detail, optional res 6 coarse overview. |
| FL-006 | Year is the top-level product partition. |
| FL-007 | Observations are per year; change/trend/assessment are whole-record derived layers. |
| FL-008 | One local product tree plus catalog; no per-shard repositories. |
| FL-009 | First build: 2017 statewide res 7 only. |
