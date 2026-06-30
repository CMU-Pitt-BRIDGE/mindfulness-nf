"""Entry point for mindfulness-nf."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mindfulness-nf",
        description="fMRI neurofeedback operator TUI",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run with a simulated scanner against murfi/dry_run_cache/",
    )
    parser.add_argument(
        "--subject",
        dest="subject",
        default=None,
        help="Subject ID to use (skips SubjectEntryScreen)",
    )
    parser.add_argument(
        "--subjects-dir",
        dest="subjects_dir",
        default="murfi/subjects",
        help="Override subjects directory (default: murfi/subjects)",
    )
    parser.add_argument(
        "--dry-run-cache",
        dest="dry_run_cache",
        default=None,
        help=(
            "Optional path to a pre-populated dry-run cache. "
            "If omitted, synthetic volumes are fabricated on demand."
        ),
    )
    parser.add_argument(
        "--anchor",
        dest="anchor",
        default="",
        help=(
            "Mindfulness anchor phrase shown to the subject during NF runs. "
            "Passed through to PsychoPy. Can also be set via the "
            "MINDFULNESS_NF_ANCHOR environment variable."
        ),
    )
    # Back-compat: legacy --test flag used to gate the deleted TestScreen.
    parser.add_argument(
        "--test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # --dry-run no longer requires a pre-populated cache: when --dry-run-cache
    # is omitted, SimulatedScannerSource fabricates volumes on demand under a
    # tmpdir. Operators who *do* have a recorded cache can still point at it.
    cache = Path(args.dry_run_cache) if args.dry_run_cache else None

    # Default subject override for dry-run so operators don't have to type
    # a sub-ID every rehearsal.
    subject_override = args.subject
    if args.dry_run and subject_override is None:
        subject_override = "sub-dry-run"

    # Defer import so --help does not require the full app tree.
    from mindfulness_nf.tui.app import MindfulnessApp

    # Resolve to absolute so every Path derived downstream (session dirs,
    # MURFI-native img/xfm/rest/mask/, FSL tool inputs, fsf DATA/REFERENCE
    # substitutions) is absolute. FSL in particular runs FEAT from a
    # working dir it cwd-chdirs into, and will then fail to resolve any
    # relative input path — see e.g. "No image files match:
    # murfi/subjects/.../bold" during fslmaths inside FEAT.
    subjects_dir = Path(args.subjects_dir).resolve()

    # Anchor precedence: --anchor CLI arg > $MINDFULNESS_NF_ANCHOR > empty.
    anchor = args.anchor or os.environ.get("MINDFULNESS_NF_ANCHOR", "")

    app = MindfulnessApp(
        test_mode=args.test,
        dry_run=args.dry_run,
        subject_override=subject_override,
        subjects_dir=subjects_dir,
        dry_run_cache_dir=cache,
        anchor=anchor,
    )
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
