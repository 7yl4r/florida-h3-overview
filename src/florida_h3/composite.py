"""Per-bin composite cache.

Vendored from h3lab@26c042f (``src/h3lab/composite.py``).  The cache is an
internal analysis artifact, not a published product, so it uses compressed
NumPy archives.  Published bundles use the byte-reproducible H3LB format.
"""

from __future__ import annotations

import json
import os

import numpy as np

from .constants import BANDS


def save_composite(path: str, res: int, year: int, bin_num: int,
                   reduced: dict, meta: dict | None = None):
    payload = {f"refl_{band}": reduced["refl"][band] for band in BANDS}
    payload["valid_fraction"] = reduced["valid_fraction"]
    payload["n_obs"] = reduced["n_obs"]
    payload["observed"] = reduced["observed"]
    info = {
        "res": int(res),
        "year": int(year),
        "bin": int(bin_num),
        "bands": list(BANDS),
    }
    if meta:
        info.update(meta)
    payload["meta"] = np.array([json.dumps(info, sort_keys=True)])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    np.savez_compressed(path, **payload)


def load_composite(path: str):
    z = np.load(path, allow_pickle=False)
    meta = json.loads(str(z["meta"][0]))
    refl = {band: z[f"refl_{band}"] for band in BANDS if f"refl_{band}" in z}
    return meta, refl, {
        "valid_fraction": z["valid_fraction"],
        "n_obs": z["n_obs"],
        "observed": z["observed"],
    }
