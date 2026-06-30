#!/usr/bin/env python3
"""Populate the dry-run cache from a real completed session.

Usage:
    uv run python scripts/populate_dry_run_cache.py <source_session_dir>

Example:
    uv run python scripts/populate_dry_run_cache.py \\
        murfi/subjects/sub-001/ses-rt15

Copies the source session's ``sourcedata/murfi/img/`` tree into
``murfi/dry_run_cache/``.  The cache is gitignored; re-running the script
clobbers the existing cache.
"""
from __future__ import annotations
import argparse
import shutil
import sys
from pathlib import Path


CACHE_DIR = Path("murfi/dry_run_cache")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="populate_dry_run_cache")
    p.add_argument(
        "source",
        type=Path,
        help="Path to a real BIDS session dir (e.g., murfi/subjects/sub-001/ses-rt15)",
    )
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=CACHE_DIR,
        help=f"Destination cache dir (default: {CACHE_DIR})",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    source_img = args.source / "sourcedata" / "murfi" / "img"
    if not source_img.is_dir():
        print(
            f"error: source does not contain sourcedata/murfi/img/: {source_img}",
            file=sys.stderr,
        )
        return 1

    volumes = sorted(source_img.glob("*.nii*"))
    if not volumes:
        print(
            f"warning: no .nii volumes under {source_img}",
            file=sys.stderr,
        )

    # Clobber and recreate
    if args.cache_dir.exists():
        shutil.rmtree(args.cache_dir)
    args.cache_dir.mkdir(parents=True)

    # The cache has two subdirs per SimulatedScannerSource:
    #   cache_dir/nifti/    for push_vsend (NIfTIs)
    #   cache_dir/dicom/    for push_dicom (DICOMs, optional)
    nifti_dir = args.cache_dir / "nifti"
    nifti_dir.mkdir()

    for src in volumes:
        dst = nifti_dir / src.name
        shutil.copy2(src, dst)

    print(f"Populated {len(volumes)} volume(s) into {nifti_dir}/")
    print(f"You can now run: uv run mindfulness-nf --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
