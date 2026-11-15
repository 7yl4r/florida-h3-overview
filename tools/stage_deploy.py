"""Stage the viewer and a GitHub Pages-sized data bundle.

The full local product tree is ~3.6 GB because every year carries res-8
detail shards (viewport tiers).  GitHub Pages caps a published site at about
1 GB, so the deploy bundle carries:

* every year's res-7 overview bundle and manifest,
* the derived overview layers (whole-record change/trend/stability),
* the imported demo, and
* a catalog regenerated against the staged tree.

Manifests are copied with their ``regions`` lists emptied, so the viewer never
requests shards that the site does not host; the overview tier stays complete.
Mark years' detail shards are served from a future object store, not Pages.

Usage::

    PYTHONPATH=src python tools/stage_deploy.py \
        --campaign campaigns/florida-south.json --out dist
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shutil
from pathlib import Path

from florida_h3.campaign import Campaign
from florida_h3.catalog import verify_catalog, write_catalog

VIEWER = Path("viewer/florida.html")


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True, separators=(",", ":"))
        fh.write("\n")


def _copy_bundle(src_dir: Path, entry: dict, dest_dir: Path) -> None:
    src = src_dir / entry["file"]
    if not src.exists():
        raise SystemExit(f"stage: missing bundle {src}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / entry["file"])


def stage(campaign: Campaign, out: Path) -> dict:
    data = out / "data" / "florida"
    if data.exists():
        shutil.rmtree(data)

    # Viewer at the site root.
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(VIEWER, out / "florida.html")
    (out / ".nojekyll").write_text("", encoding="utf-8")

    # Per-year overview bundles with region lists stripped.
    years = 0
    product_dir = campaign.product_dir
    if product_dir.exists():
        for child in sorted(product_dir.iterdir()):
            if not child.is_dir() or not child.name.isdigit():
                continue
            manifest_path = child / "manifest.json"
            if not manifest_path.exists():
                continue
            manifest = _load_json(manifest_path)
            overview = manifest.get("overview")
            if not overview:
                raise SystemExit(f"stage: {child.name} has no overview bundle")
            dest = data / "product" / child.name
            _copy_bundle(child, overview, dest)
            manifest["regions"] = []
            manifest["note"] = (
                "Deployed overview-only: res-8 detail shards are not hosted "
                "on GitHub Pages."
            )
            _write_json(dest / "manifest.json", manifest)
            years += 1
    if not years:
        raise SystemExit("stage: no year products to stage")

    # Derived overview layers only (region shards stay local).
    derived_dir = campaign.derived_dir
    derived = 0
    if (derived_dir / "manifest.json").exists():
        manifest = _load_json(derived_dir / "manifest.json")
        overview = manifest.get("overview")
        if overview:
            dest = data / "product" / "derived"
            _copy_bundle(derived_dir, overview, dest)
            manifest["regions"] = []
            manifest["files"] = [overview]
            _write_json(dest / "manifest.json", manifest)
            derived = 1
    else:
        print("stage: no derived layers yet (run make finalize first)")

    # Demo product, if imported.
    demo_src = campaign.data_root / "demo"
    if demo_src.exists():
        shutil.copytree(demo_src, data / "demo")

    # Regenerate the catalog against the staged tree so its hashes and byte
    # counts describe what the site actually serves.
    staged = dataclasses.replace(campaign, root=out.resolve())
    catalog = write_catalog(staged)
    errors = verify_catalog(staged)
    if errors:
        for error in errors:
            print(f"stage: ! {error}")
        raise SystemExit(f"stage: {len(errors)} validation error(s)")

    total = sum(p.stat().st_size for p in data.rglob("*") if p.is_file())
    total += (out / "florida.html").stat().st_size
    print(f"stage: {years} years, derived={'yes' if derived else 'no'}, "
          f"catalog years={len(catalog['years'])}, "
          f"bundle={total / 1e6:.0f} MB")
    return catalog


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--campaign", default="campaigns/florida-south.json")
    ap.add_argument("--out", default="dist",
                    help="staging directory (default: %(default)s)")
    args = ap.parse_args(argv)
    campaign = Campaign.load(args.campaign)
    stage(campaign, Path(args.out).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
