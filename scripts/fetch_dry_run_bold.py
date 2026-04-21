#!/usr/bin/env python3
"""Download real BOLD data from nilearn's ADHD rest dataset for dry-run.

Usage:
    uv run python scripts/fetch_dry_run_bold.py [--volumes N]

Downloads ~100MB once (cached by nilearn). Splits the 4D time-series into
per-volume NIfTIs under murfi/dry_run_cache_bold/nifti/ so
SimulatedScannerSource can push them to real MURFI at TR cadence.

The cache is optional: absent it, SimulatedScannerSource synthesizes
random-noise volumes (fine for MURFI/PsychoPy rehearsal, but not for
real FSL ICA in the Process session). Run this script once to enable
end-to-end Process-session rehearsal.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

CACHE_DIR = Path("murfi/dry_run_cache_bold")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="fetch_dry_run_bold")
    p.add_argument(
        "--volumes",
        type=int,
        default=150,
        help="Number of per-volume NIfTIs to produce (default: 150)",
    )
    p.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        import nibabel as nib
        from nilearn.datasets import fetch_adhd
    except ImportError as e:
        print(f"error: nilearn is required: {e}", file=sys.stderr)
        print("Install: uv sync --extra dry-run", file=sys.stderr)
        return 1

    print("Fetching ADHD rest dataset (n_subjects=1)... cached on first run.")
    data = fetch_adhd(n_subjects=1)
    func_path = Path(data.func[0])
    print(f"4D BOLD: {func_path}")

    img = nib.load(func_path)
    n_vols = img.shape[-1]
    requested = min(args.volumes, n_vols)
    print(f"4D shape {img.shape}; extracting {requested} volumes.")

    nifti_dir = args.cache_dir / "nifti"
    if nifti_dir.exists():
        import shutil

        shutil.rmtree(nifti_dir)
    nifti_dir.mkdir(parents=True)

    data4d = img.get_fdata()
    for i in range(requested):
        vol_data = data4d[..., i].astype(np.int16)
        vol_img = nib.Nifti1Image(vol_data, img.affine, img.header)
        vol_img.header.set_data_dtype(np.int16)
        out = nifti_dir / f"vol_{i + 1:04d}.nii"
        nib.save(vol_img, out)

    # Also stash the T1w if present
    if hasattr(data, "anat") and data.anat and data.anat[0]:
        anat = Path(data.anat[0])
        if anat.is_file():
            import shutil

            anat_dir = args.cache_dir / "anat"
            anat_dir.mkdir(exist_ok=True)
            shutil.copy2(anat, anat_dir / "T1w.nii.gz")

    print(f"Populated {requested} volume(s) into {nifti_dir}/")
    print("You can now run: uv run mindfulness-nf --dry-run --subject sub-rehearse")
    print("Process session will now use real BOLD for FSL stages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
