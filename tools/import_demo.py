#!/usr/bin/env python3
"""Import the frozen pilot's 2017 South Florida product as a local demo.

The demo is a real product, but it is *not* built from this campaign's polygon:
its AOI is the old South Florida bounding box.  It lives under
``data/florida/demo/<year>/`` so it can never be confused with a campaign build,
and the catalog marks it ``demo: true``.

    PYTHONPATH=src python tools/import_demo.py
    PYTHONPATH=src python tools/import_demo.py --force
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from florida_h3.campaign import Campaign  # noqa: E402
from florida_h3.catalog import write_catalog  # noqa: E402
from florida_h3.shard import compute_content_hash  # noqa: E402


def _hash(path: Path) -> str:
    with path.open("rb") as fh:
        return compute_content_hash(fh.read())


def import_demo(source: Path, campaign: Campaign, year: int,
                force: bool = False) -> Path:
    source = source.resolve()
    manifest_path = source / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"demo manifest not found: {manifest_path}")
    with manifest_path.open(encoding="utf-8") as fh:
        manifest = json.load(fh)

    dest = campaign.data_root / "demo" / str(year)
    if dest.exists() and not force:
        raise SystemExit(f"{dest} already exists; pass --force to replace it")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    items = []
    if manifest.get("overview"):
        items.append(manifest["overview"])
    items.extend(manifest.get("regions") or [])

    copied = 0
    total = 0
    for item in items:
        name = item["file"]
        src = source / name
        if not src.exists():
            raise SystemExit(f"demo bundle missing: {src}")
        size = src.stat().st_size
        if size != int(item["bytes"]):
            raise SystemExit(
                f"{src}: size {size} does not match manifest {item['bytes']}"
            )
        digest = _hash(src)
        if digest != item.get("hash"):
            raise SystemExit(f"{src}: hash {digest} does not match manifest")
        shutil.copy2(src, dest / name)
        copied += 1
        total += size

    # Copy the manifest last, after every bundle it describes exists.
    shutil.copy2(manifest_path, dest / "manifest.json")

    # The demo manifest is pilot-shaped: it has no campaign block and its AOI
    # is the old bbox.  Add a small sidecar so a reader does not have to infer
    # that from the catalog entry.
    sidecar = {
        "kind": "demo",
        "source_repository": "h3lab",
        "source_commit": "26c042f",
        "source_path": "data/regional",
        "year": int(year),
        "note": (
            "South Florida pilot product. AOI is the old bbox "
            "(-82.6,24.4,-80.0,27.4), not the Florida-south campaign polygon."
        ),
    }
    with (dest / "demo.json").open("w", encoding="utf-8") as fh:
        json.dump(sidecar, fh, indent=2, sort_keys=True)
        fh.write("\n")

    catalog = write_catalog(campaign)
    entry = catalog["years"].get(str(year))
    if not entry or not entry.get("demo"):
        raise SystemExit(f"catalog did not pick up the demo year {year}")
    print(f"imported {copied} bundles ({total / 1e6:.1f} MB) to {dest}")
    print(f"catalog: {campaign.catalog_path}")
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--campaign", default="campaigns/florida-south.json")
    ap.add_argument("--source", default=str(REPO.parent / "h3lab" / "data" / "regional"),
                    help="pilot product directory (default: %(default)s)")
    ap.add_argument("--year", type=int, default=2017)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)

    campaign = Campaign.load(REPO / args.campaign)
    import_demo(Path(args.source), campaign, args.year, args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
