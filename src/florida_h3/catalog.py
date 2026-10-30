"""The catalog is the viewer's entry point.

It is deliberately small and derived: campaign metadata plus one entry per
built year, each pointing at that year's manifest.  The catalog does not repeat
the observation data and it does not decide what a year contains; the year
manifest does.  That keeps a year independently buildable, replaceable and
deletable.

Everything is written with sorted keys and no timestamp, so rebuilding an
unchanged product tree produces the same catalog bytes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .campaign import Campaign
from .binary import read_descriptor
from .shard import compute_content_hash

SCHEMA_VERSION = 1


def _hash_file(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            return compute_content_hash(fh.read())
    except OSError:
        return ""


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def year_entry(campaign: Campaign, year: int) -> dict | None:
    product = campaign.year_product(year)
    manifest_path = product / "manifest.json"
    if not manifest_path.exists():
        return None
    with manifest_path.open(encoding="utf-8") as fh:
        manifest = json.load(fh)

    overview = manifest.get("overview") or {}
    regions = manifest.get("regions") or []
    total = int(overview.get("bytes", 0)) + sum(
        int(r.get("bytes", 0)) for r in regions
    )
    entry = {
        "year": int(year),
        "manifest": _rel(manifest_path, campaign.data_root),
        "manifest_hash": _hash_file(manifest_path),
        "campaign_hash": manifest.get("campaign_hash"),
        "aoi_hash": manifest.get("aoi_hash"),
        "periods": len(manifest.get("periods", [])),
        "overview": overview or None,
        "regions": len(regions),
        "region_res": manifest.get("region_res"),
        "bytes": total,
        "stale": (
            manifest.get("campaign_hash") != campaign.hash
            or manifest.get("aoi_hash") != campaign.aoi_hash
        ),
    }
    return entry


def derived_entry(campaign: Campaign) -> dict | None:
    path = campaign.derived_dir / "manifest.json"
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as fh:
        manifest = json.load(fh)
    files = []
    for item in manifest.get("files", []):
        p = campaign.derived_dir / item["file"]
        if p.exists():
            files.append({
                "file": item["file"],
                "bytes": int(p.stat().st_size),
                "hash": _hash_file(p),
                "tier": item.get("tier"),
                "res": item.get("res"),
            })
    return {
        "manifest": _rel(path, campaign.data_root),
        "manifest_hash": _hash_file(path),
        "campaign_hash": manifest.get("campaign_hash"),
        "aoi_hash": manifest.get("aoi_hash"),
        "files": files,
        "methods": manifest.get("methods", []),
    }


def demo_entry(campaign: Campaign, year: int) -> dict | None:
    """A locally imported demo product, clearly marked as not campaign-built.

    The demo is a real product from the frozen pilot, but its AOI is the old
    South Florida bounding box rather than this campaign's polygon.  It is kept
    out of the campaign's own product tree and is exempt from campaign-hash
    checks; its bundles are still checked for size and hash.
    """
    product = campaign.data_root / "demo" / str(year)
    manifest_path = product / "manifest.json"
    if not manifest_path.exists():
        return None
    with manifest_path.open(encoding="utf-8") as fh:
        manifest = json.load(fh)
    overview = manifest.get("overview") or {}
    regions = manifest.get("regions") or []
    return {
        "year": int(year),
        "demo": True,
        "label": "South Florida 2017 pilot (demo; not the campaign AOI)",
        "manifest": _rel(manifest_path, campaign.data_root),
        "manifest_hash": _hash_file(manifest_path),
        "campaign_hash": None,
        "aoi_hash": None,
        "periods": len(manifest.get("periods", [])),
        "overview": overview or None,
        "regions": len(regions),
        "region_res": manifest.get("region_res"),
        "bytes": int(overview.get("bytes", 0)) + sum(
            int(r.get("bytes", 0)) for r in regions
        ),
        "stale": False,
        "note": (
            "Imported from h3lab@26c042f data/regional. The AOI is the old "
            "South Florida bbox (-82.6,24.4,-80.0,27.4), not the campaign "
            "polygon; use it to exercise the viewer, not to read EEZ water."
        ),
    }


def build_catalog(campaign: Campaign, demo: dict | None = None) -> dict:
    years = {}
    for year in sorted(campaign.build_years):
        entry = year_entry(campaign, year)
        if entry is not None:
            years[str(year)] = entry
    # Also pick up any year product that exists but is not in years.build,
    # because a product tree can outlive a campaign edit.
    if campaign.product_dir.exists():
        for child in sorted(campaign.product_dir.iterdir()):
            if not child.is_dir() or not child.name.isdigit():
                continue
            year = int(child.name)
            if str(year) in years:
                continue
            entry = year_entry(campaign, year)
            if entry is not None:
                years[str(year)] = entry
    # Demo products live outside product/ so they can never be mistaken for a
    # campaign build.
    demo_dir = campaign.data_root / "demo"
    if demo_dir.exists():
        for child in sorted(demo_dir.iterdir()):
            if not child.is_dir() or not child.name.isdigit():
                continue
            year = int(child.name)
            if str(year) in years:
                continue
            entry = demo_entry(campaign, year)
            if entry is not None:
                years[str(year)] = entry

    catalog = {
        "schema_version": SCHEMA_VERSION,
        "kind": "catalog",
        "name": campaign.name,
        "slug": campaign.slug,
        "campaign": campaign.to_dict(),
        "campaign_hash": campaign.hash,
        "aoi_hash": campaign.aoi_hash,
        "aoi_geojson": campaign.geometry,
        "generated_by": "florida_h3",
        "years": years,
        "derived": derived_entry(campaign),
    }
    if demo is not None:
        catalog["demo"] = demo
    return catalog


def write_catalog(campaign: Campaign, demo: dict | None = None) -> dict:
    catalog = build_catalog(campaign, demo=demo)
    campaign.catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with campaign.catalog_path.open("w", encoding="utf-8") as fh:
        json.dump(catalog, fh, sort_keys=True, separators=(",", ":"))
        fh.write("\n")
    return catalog


def load_catalog(path: str | Path) -> dict:
    with Path(path).open(encoding="utf-8") as fh:
        return json.load(fh)


def verify_catalog(campaign: Campaign) -> list[str]:
    """Check every catalog entry against the files it describes."""
    errors: list[str] = []
    if not campaign.catalog_path.exists():
        return [f"catalog not found: {campaign.catalog_path}"]
    catalog = load_catalog(campaign.catalog_path)
    if catalog.get("campaign_hash") != campaign.hash:
        errors.append("catalog campaign hash does not match the campaign")
    if catalog.get("aoi_hash") != campaign.aoi_hash:
        errors.append("catalog AOI hash does not match the campaign")

    for year, entry in (catalog.get("years") or {}).items():
        manifest_path = campaign.data_root / entry["manifest"]
        if not manifest_path.exists():
            errors.append(f"{year}: manifest missing: {manifest_path}")
            continue
        if _hash_file(manifest_path) != entry.get("manifest_hash"):
            errors.append(f"{year}: manifest hash mismatch")
        with manifest_path.open(encoding="utf-8") as fh:
            manifest = json.load(fh)
        is_demo = bool(entry.get("demo"))
        if not is_demo:
            if manifest.get("aoi_hash") != campaign.aoi_hash:
                errors.append(f"{year}: manifest AOI hash mismatch")

        items = []
        if manifest.get("overview"):
            items.append(manifest["overview"])
        items.extend(manifest.get("regions") or [])
        for item in items:
            path = manifest_path.parent / item["file"]
            if not path.exists():
                errors.append(f"{year}: missing bundle {item['file']}")
                continue
            if int(path.stat().st_size) != int(item["bytes"]):
                errors.append(f"{year}: size mismatch {item['file']}")
            if _hash_file(path) != item.get("hash"):
                errors.append(f"{year}: hash mismatch {item['file']}")
            try:
                desc = read_descriptor(str(path))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{year}: unreadable bundle {item['file']}: {exc}")
                continue
            if int(desc["nCells"]) != int(item["nCells"]):
                errors.append(f"{year}: cell count mismatch {item['file']}")
            if int(desc["nPeriods"]) != int(item["nPeriods"]):
                errors.append(f"{year}: period count mismatch {item['file']}")
            if not is_demo:
                if desc.get("campaign") != campaign.slug:
                    errors.append(f"{year}: bundle campaign mismatch {item['file']}")
                if desc.get("aoiHash") != campaign.aoi_hash:
                    errors.append(f"{year}: bundle AOI mismatch {item['file']}")
    return errors
