"""Subject directory management and crash recovery.

Creates subject directories, copies XML templates, and provides
atomic session-state persistence for crash recovery.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Volume filename pattern produced by MURFI: img-SSSSS-VVVVV.nii
_VOLUME_GLOB = "img-*-*.nii"


# ---------------------------------------------------------------------------
# Subject directory management
# ---------------------------------------------------------------------------


def create_subject(
    subjects_dir: Path, subject_id: str, template_dir: Path
) -> Path:
    """Create a new subject directory with standard subdirectories.

    Parameters
    ----------
    subjects_dir:
        Parent directory containing all subjects (e.g. ``…/subjects/``).
    subject_id:
        Unique identifier for the subject (e.g. ``sub-001``).
    template_dir:
        Path to the template directory that contains ``xml/xml_vsend/``.

    Returns
    -------
    Path
        The newly created subject directory.

    Raises
    ------
    FileExistsError
        If the subject directory already exists.
    """
    subject_dir = subjects_dir / subject_id

    if subject_dir.exists():
        msg = f"Subject directory already exists: {subject_dir}"
        raise FileExistsError(msg)

    subject_dir.mkdir(parents=True)

    # Create standard subdirectories (mirrors createxml.sh).
    for subdir in ("xml", "mask", "mask/qc", "img", "log", "xfm", "rest"):
        (subject_dir / subdir).mkdir(parents=True, exist_ok=True)

    # Copy XML templates from template_dir/xml/xml_vsend/.
    xml_source = template_dir / "xml" / "xml_vsend"
    xml_dest = subject_dir / "xml"
    if xml_source.is_dir():
        for src_file in xml_source.iterdir():
            if src_file.is_file():
                shutil.copy2(src_file, xml_dest / src_file.name)

    return subject_dir


def subject_exists(subjects_dir: Path, subject_id: str) -> bool:
    """Return True if a subject directory already exists."""
    return (subjects_dir / subject_id).is_dir()


# ---------------------------------------------------------------------------
# Session state persistence (crash recovery)
# ---------------------------------------------------------------------------


def save_session_state(
    state_file: Path,
    subject: str,
    session: str,
    last_completed_step: int,
) -> None:
    """Atomically write session state to *state_file* as JSON.

    The file is written to a temporary file in the same directory and
    then renamed, ensuring that a crash mid-write does not corrupt the
    state file.
    """
    state = {
        "subject": subject,
        "session": session,
        "last_completed_step": last_completed_step,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    state_file.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file then atomically rename.
    fd, tmp_path = tempfile.mkstemp(
        dir=state_file.parent, suffix=".tmp", prefix=".state_"
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp_path, state_file)
    except BaseException:
        # Clean up the temp file on any error.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_session_state(state_file: Path) -> dict | None:
    """Load session state from *state_file*.

    Returns
    -------
    dict | None
        The parsed JSON dict, or ``None`` if the file does not exist.
    """
    if not state_file.is_file():
        return None
    return json.loads(state_file.read_text())


# ---------------------------------------------------------------------------
# Data cleanup and validation
# ---------------------------------------------------------------------------


def clear_partial_data(subject_dir: Path, step_index: int) -> None:
    """Delete volume files from an interrupted step.

    MURFI writes volumes as ``img-SSSSS-VVVVV.nii`` in the ``img/``
    subdirectory.  Each step corresponds to a series number (1-based,
    zero-padded to 5 digits).  This function removes all volume files
    whose series number matches ``step_index + 1``.
    """
    img_dir = subject_dir / "img"
    if not img_dir.is_dir():
        return

    series = f"{step_index + 1:05d}"
    pattern = f"img-{series}-*.nii"
    for path in img_dir.glob(pattern):
        path.unlink()


def validate_step_data(
    subject_dir: Path, step_index: int, expected_volumes: int
) -> bool:
    """Check that volume files for a step are present and non-empty.

    Parameters
    ----------
    subject_dir:
        Subject directory containing ``img/``.
    step_index:
        Zero-based step index (maps to series ``step_index + 1``).
    expected_volumes:
        Number of volume files expected.

    Returns
    -------
    bool
        ``True`` if exactly *expected_volumes* non-empty ``.nii`` files
        exist for the step's series number.
    """
    img_dir = subject_dir / "img"
    if not img_dir.is_dir():
        return False

    series = f"{step_index + 1:05d}"
    pattern = f"img-{series}-*.nii"
    files = list(img_dir.glob(pattern))

    if len(files) != expected_volumes:
        return False

    return all(f.stat().st_size > 0 for f in files)
