"""One-shot migration: move rest/ and qc/ from subject root into session dirs.

Before this migration:
    sub-X/
        rest/   ← 4D merges (filenames hardcoded ses-localizer)
        qc/     ← QC overlays
        ses-process/
            ... (rest/qc created but empty)

After this migration:
    sub-X/
        ses-process/
            rest/   ← 4D merges (filenames renamed to actual session)
            qc/     ← QC overlays

Also deletes empty aspirational directories created by the old
``create_subject_session_dir`` (``func/``, ``derivatives/masks/``,
``sourcedata/murfi/{img,log}/``).

Usage:
    python scripts/migrate_layout.py SUBJECT_DIR [--target-session SESSION_TYPE]

Example:
    python scripts/migrate_layout.py murfi/subjects/sub-process-rehearse

If ``--target-session`` is omitted, the script auto-detects: it uses the
subject's sole session dir if there's exactly one, otherwise prefers
``ses-process`` (where 4D merges normally originate), otherwise errors.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path


# Filenames encode the session: sub-X_ses-localizer_task-rest_run-01_bold.nii.
# The migration rewrites the ses- component.
_SES_IN_NAME_RE = re.compile(r"_ses-[^_]+_")

# Empty aspirational dirs we previously created but never populated.
_DEAD_DIRS = (
    "func",
    "derivatives/masks",
    "sourcedata/murfi/img",
    "sourcedata/murfi/log",
)


def detect_target_session(subject_dir: Path) -> str:
    """Pick the session these rest/qc files belong to."""
    session_dirs = sorted(
        p.name.removeprefix("ses-")
        for p in subject_dir.iterdir()
        if p.is_dir() and p.name.startswith("ses-")
    )
    if not session_dirs:
        msg = (
            f"No session directories under {subject_dir}. "
            f"Create one first (e.g. run the pipeline once) or pass "
            f"--target-session explicitly."
        )
        raise SystemExit(msg)
    if len(session_dirs) == 1:
        return session_dirs[0]
    if "process" in session_dirs:
        # Process session is where rest/qc normally originate.
        return "process"
    msg = (
        f"Multiple sessions found under {subject_dir}: {session_dirs}. "
        f"Pass --target-session to pick one."
    )
    raise SystemExit(msg)


def rewrite_ses_in_filename(name: str, new_session: str) -> str:
    """Rewrite ``_ses-<anything>_`` in *name* to ``_ses-<new_session>_``."""
    return _SES_IN_NAME_RE.sub(f"_ses-{new_session}_", name, count=1)


def migrate_subdir(
    src_dir: Path, dest_dir: Path, new_session: str, dry_run: bool
) -> int:
    """Move files from ``src_dir`` to ``dest_dir``, rewriting ses- token.

    Returns the count of files moved. Subdirectories are preserved
    (rest/ in particular has nested ``rs_network.gica/``, ``epi2reg/``, etc).
    """
    if not src_dir.is_dir():
        return 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for entry in sorted(src_dir.iterdir()):
        if entry.is_file():
            new_name = rewrite_ses_in_filename(entry.name, new_session)
            dest = dest_dir / new_name
            print(f"  MOVE  {entry} -> {dest}")
            if not dry_run:
                shutil.move(str(entry), str(dest))
            moved += 1
        elif entry.is_dir():
            # Recurse for nested trees (e.g. rest/rs_network.gica/).
            # Rename the subdir itself if its name contains a ses- token.
            sub_name = rewrite_ses_in_filename(entry.name, new_session)
            sub_dest = dest_dir / sub_name
            print(f"  DIR   {entry} -> {sub_dest}")
            if not dry_run:
                shutil.move(str(entry), str(sub_dest))
            moved += 1
    if not dry_run:
        try:
            src_dir.rmdir()  # Only works if empty; harmless otherwise.
        except OSError:
            pass
    return moved


def prune_dead_dirs(session_dir: Path, dry_run: bool) -> int:
    """Delete the empty aspirational directories if they exist."""
    removed = 0
    for rel in _DEAD_DIRS:
        p = session_dir / rel
        if not p.exists():
            continue
        if any(p.rglob("*")):
            print(f"  KEEP  {p} (not empty — inspect manually)")
            continue
        print(f"  PRUNE {p}")
        if not dry_run:
            shutil.rmtree(p)
        removed += 1
    # Remove now-empty parent dirs (``derivatives/``, ``sourcedata/murfi/``).
    for rel in ("derivatives", "sourcedata/murfi"):
        p = session_dir / rel
        if p.is_dir() and not any(p.iterdir()):
            print(f"  PRUNE {p} (empty parent)")
            if not dry_run:
                p.rmdir()
    return removed


def migrate_subject(
    subject_dir: Path, target_session: str | None, dry_run: bool
) -> None:
    print(f"=== Migrating {subject_dir} ===")
    if target_session is None:
        target_session = detect_target_session(subject_dir)
    print(f"Target session: ses-{target_session}")

    session_dir = subject_dir / f"ses-{target_session}"
    if not session_dir.is_dir():
        msg = f"session dir missing: {session_dir}"
        raise SystemExit(msg)

    # 1. rest/ at subject root → session_dir/rest/
    print("\n-- rest/ --")
    moved = migrate_subdir(
        subject_dir / "rest", session_dir / "rest", target_session, dry_run
    )
    print(f"  moved {moved} entries")

    # 2. qc/ at subject root → session_dir/qc/
    print("\n-- qc/ --")
    moved = migrate_subdir(
        subject_dir / "qc", session_dir / "qc", target_session, dry_run
    )
    print(f"  moved {moved} entries")

    # 3. Prune aspirational empty dirs across ALL session dirs.
    print("\n-- prune empty aspirational dirs --")
    for ses_dir in sorted(subject_dir.glob("ses-*")):
        if not ses_dir.is_dir():
            continue
        print(f"session {ses_dir.name}:")
        prune_dead_dirs(ses_dir, dry_run)

    print(f"\n=== Done{' (DRY RUN)' if dry_run else ''} ===")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "subject_dir",
        type=Path,
        help="Path to a subject dir (e.g. murfi/subjects/sub-process-rehearse)",
    )
    parser.add_argument(
        "--target-session",
        default=None,
        help="Session label (e.g. 'process') that owns the rest/qc data. "
        "Auto-detected when possible.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview moves without touching disk.",
    )
    args = parser.parse_args()
    subject_dir = args.subject_dir.resolve()
    if not subject_dir.is_dir():
        print(f"error: not a directory: {subject_dir}", file=sys.stderr)
        raise SystemExit(1)
    migrate_subject(subject_dir, args.target_session, args.dry_run)


if __name__ == "__main__":
    main()
