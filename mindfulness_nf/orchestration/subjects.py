"""Subject directory management and crash recovery.

Creates subject directories, copies XML templates, and provides
atomic session-state persistence for crash recovery.

This module hosts both the **legacy flat-layout helpers** (``save_session_state``,
``load_session_state``, ``clear_partial_data``, ``validate_step_data``,
``create_subject``) retained for backward compatibility, and the
**BIDS-layout helpers** used by :class:`SessionRunner`
(``create_subject_session_dir``, ``bids_session_dir``, ``session_state_path``,
``load_bids_session_state``, ``persist_bids_session_state``, ``bids_func_path``,
``clear_bids_run_files``). New code should use the BIDS helpers.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mindfulness_nf.models import (
    SessionState,
    StepConfig,
    StepKind,
    StepState,
    StepStatus,
)

logger = logging.getLogger(__name__)

# Volume filename pattern produced by MURFI: img-SSSSS-VVVVV.nii
_VOLUME_GLOB = "img-*-*.nii"

# JSON schema version for BIDS session_state.json.  Bump when shape changes.
_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Subject directory management
# ---------------------------------------------------------------------------


def create_subject(
    subjects_dir: Path, subject_id: str, template_dir: Path
) -> Path:
    """(Legacy flat layout) Create a new subject directory with standard subdirectories.

    .. deprecated::
        Use :func:`create_subject_session_dir` for the BIDS-compliant layout.
        This function is retained for backward compatibility.

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
    """(Legacy) Atomically write session state to *state_file* as JSON.

    .. deprecated::
        Use :func:`persist_bids_session_state` for the new BIDS layout.

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
    """(Legacy) Load session state from *state_file*.

    .. deprecated::
        Use :func:`load_bids_session_state` for the new BIDS layout.

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
    """(Legacy) Delete volume files from an interrupted step.

    .. deprecated::
        Use :func:`clear_bids_run_files` for the new BIDS layout.

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
    """(Legacy) Check that volume files for a step are present and non-empty.

    .. deprecated::
        Flat layout; new code should validate BIDS ``func/`` files directly.

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


# ---------------------------------------------------------------------------
# BIDS layout helpers (new canonical API)
# ---------------------------------------------------------------------------


def bids_session_dir(
    subjects_dir: Path, subject_id: str, session_type: str
) -> Path:
    """Return the expected path to a subject-session dir.

    Does not check existence.  Returns
    ``<subjects_dir>/<subject_id>/ses-<session_type>``.
    """
    return subjects_dir / subject_id / f"ses-{session_type}"


def session_state_path(session_dir: Path) -> Path:
    """Return ``<session_dir>/session_state.json``."""
    return session_dir / "session_state.json"


def bids_func_path(
    session_dir: Path,
    subject: str,
    session_type: str,
    task: str,
    run: int,
) -> Path:
    """Return the BIDS ``func/`` NIfTI path for a given run.

    Shape: ``<session_dir>/func/<subject>_ses-<session_type>_task-<task>_run-<NN>_bold.nii``.
    """
    filename = (
        f"{subject}_ses-{session_type}_task-{task}_run-{run:02d}_bold.nii"
    )
    return session_dir / "func" / filename


def create_subject_session_dir(
    subjects_dir: Path,
    subject_id: str,
    session_type: str,
    template_dir: Path,
) -> Path:
    """Create the full BIDS tree for a ``(subject_id, session_type)`` pair.

    Layout created::

        <subjects_dir>/<subject_id>/
        ├── <subject_id>_sessions.tsv         (stub)
        └── ses-<session_type>/
            ├── func/
            ├── sourcedata/
            │   ├── murfi/
            │   │   ├── xml/                  (copies of MURFI XML templates)
            │   │   ├── img/
            │   │   └── log/
            │   └── psychopy/
            └── derivatives/
                └── masks/

    Idempotent at the *subject* level: creating a *new* session for an
    existing subject is fine.  Creating an already-existing
    ``(subject_id, session_type)`` raises :class:`FileExistsError`.

    Parameters
    ----------
    subjects_dir:
        Parent directory holding all subjects.
    subject_id:
        BIDS subject label, e.g. ``"sub-001"``.
    session_type:
        Short session label, e.g. ``"loc3"`` / ``"rt15"`` / ``"rt30"`` /
        ``"process"``.  ``ses-`` prefix is added by this function.
    template_dir:
        Directory holding ``xml/xml_vsend/`` with MURFI XML templates.

    Returns
    -------
    Path
        The created ``ses-<session_type>`` directory.
    """
    subject_dir = subjects_dir / subject_id
    session_dir = bids_session_dir(subjects_dir, subject_id, session_type)

    if session_dir.exists():
        msg = f"Subject-session directory already exists: {session_dir}"
        raise FileExistsError(msg)

    # Subject-level directory + sessions.tsv stub (idempotent).
    subject_dir.mkdir(parents=True, exist_ok=True)
    sessions_tsv = subject_dir / f"{subject_id}_sessions.tsv"
    if not sessions_tsv.exists():
        sessions_tsv.write_text("session_id\tacq_time\n")

    # Session subtree.
    session_dir.mkdir(parents=True)
    for sub in (
        "func",
        "sourcedata/murfi/xml",
        "sourcedata/murfi/img",
        "sourcedata/murfi/log",
        "sourcedata/psychopy",
        "derivatives/masks",
    ):
        (session_dir / sub).mkdir(parents=True, exist_ok=True)

    # Copy XML templates into sourcedata/murfi/xml/.
    xml_source = template_dir / "xml" / "xml_vsend"
    xml_dest = session_dir / "sourcedata" / "murfi" / "xml"
    if xml_source.is_dir():
        for src_file in xml_source.iterdir():
            if src_file.is_file():
                shutil.copy2(src_file, xml_dest / src_file.name)

    return session_dir


# ---- session_state.json (de)serialization ---------------------------------


def _serialize_step(step: StepState) -> dict[str, Any]:
    cfg = step.config
    return {
        "config": {
            "name": cfg.name,
            "task": cfg.task,
            "run": cfg.run,
            "progress_target": cfg.progress_target,
            "progress_unit": cfg.progress_unit,
            "xml_name": cfg.xml_name,
            "kind": cfg.kind.value,
            "feedback": cfg.feedback,
            "fsl_command": cfg.fsl_command,
        },
        "status": step.status.value,
        "attempts": step.attempts,
        "progress_current": step.progress_current,
        "last_started": step.last_started,
        "last_finished": step.last_finished,
        "detail_message": step.detail_message,
        "error": step.error,
        "phase": step.phase,
        "awaiting_advance": step.awaiting_advance,
        "artifacts": step.artifacts,
    }


def _deserialize_step(data: dict[str, Any]) -> StepState:
    cfg_data = data["config"]
    config = StepConfig(
        name=cfg_data["name"],
        task=cfg_data["task"],
        run=cfg_data["run"],
        progress_target=cfg_data["progress_target"],
        progress_unit=cfg_data["progress_unit"],
        xml_name=cfg_data["xml_name"],
        kind=StepKind(cfg_data["kind"]),
        feedback=cfg_data.get("feedback", False),
        fsl_command=cfg_data.get("fsl_command"),
    )
    status = StepStatus(data["status"])
    error = data.get("error")
    # Coerce running → failed: the process that was running is gone; the
    # operator decides whether any partial data on disk is salvageable.
    if status is StepStatus.RUNNING:
        status = StepStatus.FAILED
        error = "interrupted by restart"
    return StepState(
        config=config,
        status=status,
        attempts=data.get("attempts", 0),
        progress_current=data.get("progress_current", 0),
        last_started=data.get("last_started"),
        last_finished=data.get("last_finished"),
        detail_message=data.get("detail_message"),
        error=error,
        phase=data.get("phase"),
        awaiting_advance=data.get("awaiting_advance", False),
        artifacts=data.get("artifacts"),
    )


def _serialize_session_state(state: SessionState) -> dict[str, Any]:
    """Render ``SessionState`` as the JSON dict persisted on disk.

    Each step's config is embedded in full so resume is robust to code drift
    (see spec §Resume behavior).
    """
    return {
        "schema_version": _SCHEMA_VERSION,
        "subject": state.subject,
        "session_type": state.session_type,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "cursor": state.cursor,
        "steps": [_serialize_step(s) for s in state.steps],
    }


def _deserialize_session_state(data: dict[str, Any]) -> SessionState:
    version = data.get("schema_version")
    if version != _SCHEMA_VERSION:
        msg = (
            f"unknown schema_version: {version!r} "
            f"(expected {_SCHEMA_VERSION})"
        )
        raise ValueError(msg)

    steps = tuple(_deserialize_step(s) for s in data["steps"])
    return SessionState(
        subject=data["subject"],
        session_type=data["session_type"],
        cursor=data["cursor"],
        steps=steps,
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as JSON to ``path`` atomically (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=".state_"
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_bids_session_state(session_dir: Path) -> SessionState | None:
    """Load ``session_state.json`` from a BIDS session directory.

    Behaviour:

    * Returns ``None`` if the file does not exist.
    * Raises :class:`ValueError` on unknown ``schema_version``.
    * Coerces any ``status=running`` step to ``failed`` with
      ``error="interrupted by restart"`` (the process that was running is
      gone).

    The returned state is **not** persisted back; the caller (usually
    :class:`SessionRunner`) handles ``updated_at`` and re-persisting.
    """
    state_file = session_state_path(session_dir)
    if not state_file.is_file():
        return None
    data = json.loads(state_file.read_text())
    return _deserialize_session_state(data)


def persist_bids_session_state(
    session_dir: Path, state: SessionState
) -> None:
    """Serialize ``state`` and atomically write it to ``session_state.json``."""
    _atomic_write_json(
        session_state_path(session_dir), _serialize_session_state(state)
    )


def clear_bids_run_files(
    session_dir: Path,
    subject: str,
    session_type: str,
    step: StepConfig,
) -> None:
    """Remove BIDS files produced for one step's run.

    Deletes:

    * Everything under ``<session_dir>/func/`` whose name starts with
      ``<subject>_ses-<session_type>_task-<task>_run-<NN>_bold`` — i.e. the
      NIfTI and its JSON sidecar.
    * Raw MURFI volumes in ``<session_dir>/sourcedata/murfi/img/`` for the
      step's series number (1-based; zero-padded to 5 digits) *if* the step
      config exposes ``run``.

    Safe to call when the files don't exist.  Only matches files for the
    specific ``(task, run)`` — other steps' data is left alone.
    """
    if step.task is None or step.run is None:
        return

    run_str = f"{step.run:02d}"
    func_dir = session_dir / "func"
    if func_dir.is_dir():
        prefix = (
            f"{subject}_ses-{session_type}_task-{step.task}"
            f"_run-{run_str}_bold"
        )
        for path in func_dir.iterdir():
            if path.name.startswith(prefix):
                try:
                    path.unlink()
                except OSError:
                    logger.exception("failed to unlink %s", path)

    # Raw MURFI volumes: series number = run (MURFI writes series 1-based).
    img_dir = session_dir / "sourcedata" / "murfi" / "img"
    if img_dir.is_dir():
        series = f"{step.run:05d}"
        for path in img_dir.glob(f"img-{series}-*.nii"):
            try:
                path.unlink()
            except OSError:
                logger.exception("failed to unlink %s", path)


# Avoid unused-import warnings when StepKind is imported only for legacy tests.
_ = StepKind
