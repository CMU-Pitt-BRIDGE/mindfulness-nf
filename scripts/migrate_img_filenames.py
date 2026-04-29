"""Rename existing img/img-<NNNNN>-<VVVVV>.nii files to task-keyed format.

The task+run-keyed filename format is ``img-<task>-<run>-<vol>.nii``. Legacy
data uses MURFI's native format ``img-<series>-<vol>.nii`` with a series
number that equals ``step.run`` post-old-rename. This script asks the
caller what task each legacy series represents and renames accordingly.

Usage:
    python scripts/migrate_img_filenames.py SUBJECT_DIR --map 00001=rest:1 00002=rest:2

Each --map arg is ``<series>=<task>:<run>``. Missing series are left alone.

Example for sub-morgan (only Rest 2 survived; Rest 1 was lost):
    python scripts/migrate_img_filenames.py murfi/subjects/sub-morgan --map 00002=rest:2
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


_NATIVE_RE = re.compile(r"^img-(\d{5})-(\d{5})\.nii$")


def parse_map(items: list[str]) -> dict[str, tuple[str, int]]:
    """Parse --map arguments into {series: (task, run)}."""
    result: dict[str, tuple[str, int]] = {}
    for item in items:
        try:
            series, rest = item.split("=", 1)
            task, run_str = rest.split(":", 1)
            run = int(run_str)
            if not re.match(r"^\d{5}$", series):
                raise ValueError(f"series must be 5 digits, got {series!r}")
            if not re.match(r"^[a-z0-9]+$", task):
                raise ValueError(f"task must be lowercase alphanumeric, got {task!r}")
        except ValueError as exc:
            msg = f"bad --map value {item!r}: {exc}"
            raise SystemExit(msg) from exc
        result[series] = (task, run)
    return result


def migrate(subject_dir: Path, series_map: dict[str, tuple[str, int]], dry_run: bool) -> int:
    img_dir = subject_dir / "img"
    if not img_dir.is_dir():
        msg = f"no img/ dir at {img_dir}"
        raise SystemExit(msg)

    renamed = 0
    unknown_series: set[str] = set()

    for src in sorted(img_dir.glob("img-*.nii")):
        m = _NATIVE_RE.match(src.name)
        if not m:
            continue
        series, vol = m.group(1), m.group(2)
        if series not in series_map:
            unknown_series.add(series)
            continue
        task, run = series_map[series]
        dst_name = f"img-{task}-{run:02d}-{vol}.nii"
        dst = img_dir / dst_name
        print(f"  MOVE  {src.name} -> {dst_name}")
        if not dry_run:
            src.replace(dst)
        renamed += 1

    if unknown_series:
        print(
            f"  SKIP  unmapped series: {sorted(unknown_series)} "
            f"(pass --map <series>=<task>:<run> to rename these)"
        )
    return renamed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subject_dir", type=Path)
    parser.add_argument(
        "--map",
        dest="mappings",
        action="append",
        default=[],
        help="series mapping, e.g. 00001=rest:1 (can be repeated)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    subject_dir = args.subject_dir.resolve()
    if not subject_dir.is_dir():
        print(f"error: not a directory: {subject_dir}", file=sys.stderr)
        raise SystemExit(1)

    series_map = parse_map(args.mappings)
    if not series_map:
        print("warning: no --map arguments; nothing will be renamed", file=sys.stderr)

    print(f"=== Migrating {subject_dir}{' (DRY RUN)' if args.dry_run else ''} ===")
    n = migrate(subject_dir, series_map, args.dry_run)
    print(f"\n=== Renamed {n} files ===")


if __name__ == "__main__":
    main()
