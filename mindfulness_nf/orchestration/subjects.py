"""Subject directory management and crash recovery.

Creates subject directories, copies XML templates, and provides
atomic session-state persistence for crash recovery.

This module hosts both the **legacy flat-layout helpers** (``save_session_state``,
``load_session_state``, ``clear_partial_data``, ``validate_step_data``,
``create_subject``) retained for backward compatibility, and the
**BIDS-layout helpers** used by :class:`SessionRunner`
(``create_subject_session_dir``, ``bids_session_dir``, ``session_state_path``,
``load_bids_session_state``, ``persist_bids_session_state``, ``bids_func_path``,
``clear_bids_run_files``, ``write_provenance``). New code should use the
BIDS helpers.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
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

# MURFI's native output pattern: img-SSSSS-VVVVV.nii (series, volume indexes).
# Fresh MURFI processes always start at series 1, so successive steps would
# collide on the same series in the shared subject-scoped img/ dir.
_MURFI_NATIVE_GLOB = "img-[0-9]*-[0-9]*.nii"
_MURFI_NATIVE_RE = re.compile(r"^img-(\d{5})-(\d{5})\.nii$")

# Post-rename pattern: img-<task>-<run>-<vol>.nii. Task label + run-number
# is unique across the subject: Rest 1 (ses-loc3, task=rest, run=1) lives
# at img-rest-01-*.nii; Transfer Pre (ses-rt15, task=transferpre, run=1)
# lives at img-transferpre-01-*.nii. No cross-task collisions possible.
# Regression (see subject sub-morgan data loss, 2026-04-21): previously
# rename keyed on just step.run, so Rest 1 + Transfer Pre + Feedback 1 +
# Transfer Post — all run=1 — all wrote to img-00001-* and overwrote each
# other. Restarting any of them also deleted all the others via
# ``clear_bids_run_files`` matching on just run number.
_STEP_KEYED_RE = re.compile(r"^img-([a-z0-9]+)-(\d{2})-(\d{5})\.nii$")


def snapshot_img_dir(subject_dir: Path) -> frozenset[Path]:
    """Return the set of files currently in ``<subject_dir>/img/``.

    Used as a baseline before launching a MURFI-driven step. Compare with a
    fresh listing after the step ends to identify files MURFI wrote during
    this run — those get renamed via :func:`rename_step_volumes`.

    Snapshots BOTH the native MURFI-output pattern (``img-SSSSS-VVVVV.nii``)
    and the post-rename step-keyed pattern (``img-<task>-<run>-<vol>.nii``)
    so that re-runs of a completed step correctly identify which files
    MURFI produced in *this* attempt.
    """
    img_dir = subject_dir / "img"
    if not img_dir.is_dir():
        return frozenset()
    # Any img-*.nii that isn't MURFI's native curact/design output.
    result: set[Path] = set()
    for p in img_dir.glob("img-*.nii"):
        if _MURFI_NATIVE_RE.match(p.name) or _STEP_KEYED_RE.match(p.name):
            result.add(p)
    return frozenset(result)


def rename_step_volumes(
    subject_dir: Path,
    task: str,
    run_number: int,
    pre_existing: frozenset[Path],
) -> int:
    """Rename MURFI's native output to task+run-keyed format.

    After a MURFI-driven step completes, files MURFI wrote during the step
    follow the ``img-<series>-<vol>.nii`` pattern (series counter per
    MURFI process). This helper moves them to ``img-<task>-<run>-<vol>.nii``
    so that (a) successive steps can't overwrite each other by sharing a
    series number, and (b) ``clear_bids_run_files`` can delete only the
    files for a specific ``(task, run)`` without touching other steps'
    data from prior sessions.

    ``task`` must be a short lowercase label (e.g. ``"rest"``,
    ``"feedback"``, ``"transferpre"``). ``run_number`` is the step's
    task-scoped run index (1-based).

    Returns the number of files renamed. Files whose names already match
    the target pattern for this (task, run) are skipped silently
    (idempotent).
    """
    img_dir = subject_dir / "img"
    if not img_dir.is_dir():
        return 0
    # New files = those MURFI just wrote (not in pre_existing) that still
    # match MURFI's native pattern. Files already in post-rename pattern
    # from an earlier attempt are skipped.
    all_now = {p for p in img_dir.glob("img-*.nii") if _MURFI_NATIVE_RE.match(p.name)}
    new_files = sorted(all_now - pre_existing)
    renamed = 0
    for src in new_files:
        match = _MURFI_NATIVE_RE.match(src.name)
        if match is None:
            continue
        vol_idx = match.group(2)
        dst_name = f"img-{task}-{run_number:02d}-{vol_idx}.nii"
        dst = img_dir / dst_name
        # Overwrite is safe here: the destination, if it exists, is from a
        # prior attempt of this *same* (task, run). A deliberate re-run
        # wants the latest attempt.
        src.replace(dst)
        renamed += 1
    return renamed


def step_volume_glob(task: str, run_number: int) -> str:
    """Glob pattern for one step's task+run-keyed volumes."""
    return f"img-{task}-{run_number:02d}-*.nii"

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

    # Subject-level directory + sessions.tsv (idempotent; appends one row
    # per session_type so an RA can see session ordering without opening
    # each session_state.json).
    subject_dir.mkdir(parents=True, exist_ok=True)
    sessions_tsv = subject_dir / f"{subject_id}_sessions.tsv"
    existing_ids: set[str] = set()
    if sessions_tsv.exists():
        with sessions_tsv.open() as fh:
            for i, line in enumerate(fh):
                if i == 0 or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if parts:
                    existing_ids.add(parts[0])
    else:
        sessions_tsv.write_text("session_id\tacq_time\n")
    session_id = f"ses-{session_type}"
    if session_id not in existing_ids:
        with sessions_tsv.open("a") as fh:
            fh.write(f"{session_id}\t{datetime.now(timezone.utc).isoformat()}\n")

    # Subject-level README: copy the docs template at subject creation so
    # an RA who inherits only the subject dir can interpret every file.
    readme_dst = subject_dir / "README.md"
    if not readme_dst.exists():
        readme_src = (
            Path(__file__).resolve().parents[2]
            / "docs" / "SUBJECT_README_TEMPLATE.md"
        )
        if readme_src.is_file():
            try:
                shutil.copy2(readme_src, readme_dst)
            except OSError:
                logger.warning("failed to copy README template to %s", readme_dst)

    # Session subtree. Only the directories that are *actually* written to:
    # - log/                     MURFI + orchestrator logs for this session
    # - rest/                    Per-session 4D merges + preprocessing
    # - qc/                      Per-session QC overlays
    # - sourcedata/murfi/xml/    Snapshot of XMLs-as-run
    # - sourcedata/psychopy/     Behavioral data (PsychoPy writes here directly)
    #
    # Previously we also created func/, derivatives/masks/, sourcedata/murfi/img/,
    # and sourcedata/murfi/log/ — none were ever populated, so they confused
    # anyone inspecting the layout ("is data missing?"). Removed.
    session_dir.mkdir(parents=True)
    for sub in (
        "log",
        "rest",
        "qc",
        "sourcedata/murfi/xml",
        "sourcedata/psychopy",
    ):
        (session_dir / sub).mkdir(parents=True, exist_ok=True)

    # Copy XML templates into sourcedata/murfi/xml/. The flavor depends on
    # the session type: ``loc3`` uses DICOM mode (rest-state runs ingest
    # DICOMs via a receiver); ``rt15`` / ``rt30`` use vSend (NIfTI streamed
    # by MURFI's scanner emulator). ``process`` has no scanner step.
    flavor = _xml_flavor_for(session_type)
    xml_dest = session_dir / "sourcedata" / "murfi" / "xml"
    if flavor is not None:
        xml_source = template_dir / "xml" / flavor
        if xml_source.is_dir():
            for src_file in xml_source.iterdir():
                if src_file.is_file():
                    shutil.copy2(src_file, xml_dest / src_file.name)
        # For DICOM-flavor XMLs, point MURFI at the per-session receiver
        # output dir. The template has a hardcoded path that would send
        # MURFI watching the wrong directory.
        if flavor == "xml_dcm":
            dicom_receiver_dir = (session_dir / "sourcedata" / "dicom").resolve()
            for xml_file in xml_dest.glob("*.xml"):
                _rewrite_input_dicom_dir(xml_file, dicom_receiver_dir)

    return session_dir


_INPUT_DICOM_DIR_RE = re.compile(
    r'(<option\s+name="inputDicomDir"\s*>)[^<]*(</option>)'
)


def _rewrite_input_dicom_dir(xml_file: Path, target: Path) -> bool:
    """Rewrite ``<option name="inputDicomDir">...</option>`` in *xml_file*
    to point at *target*. Returns True if the file was modified."""
    content = xml_file.read_text()
    new_content, subs = _INPUT_DICOM_DIR_RE.subn(
        rf"\g<1> {target} \g<2>", content
    )
    if subs == 0 or new_content == content:
        return False
    xml_file.write_text(new_content)
    return True


def _xml_flavor_for(session_type: str) -> str | None:
    """Return the template XML subdirectory name for a given session type,
    or ``None`` if the session doesn't need MURFI XMLs (e.g. ``process``).

    All session types that do real-time acquisition use ``xml_vsend`` —
    rest in loc3 streams via MURFI's scanner TCP input on port 50000, same
    as 2vol/rtdmn in rt15/rt30. ``xml_dcm`` is reserved for post-hoc DICOM
    workflows (currently not wired to any session).
    """
    match session_type:
        case "loc3" | "rt15" | "rt30":
            return "xml_vsend"
        case _:
            return None


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

    * Everything under ``<session_dir>/rest/`` whose name starts with
      ``<subject>_ses-<session_type>_task-<task>_run-<NN>_bold`` — i.e. the
      4D merge + FSL-preprocessing intermediates.
    * Raw MURFI volumes in ``<subject_dir>/img/`` for the step's run
      number (via ``rename_step_volumes`` convention: ``img-<run>-*.nii``).

    Safe to call when the files don't exist.  Only matches files for the
    specific ``(task, run)`` — other steps' data is left alone.
    """
    if step.task is None or step.run is None:
        return

    run_str = f"{step.run:02d}"
    rest_dir = session_dir / "rest"
    if rest_dir.is_dir():
        prefix = (
            f"{subject}_ses-{session_type}_task-{step.task}"
            f"_run-{run_str}_bold"
        )
        for path in rest_dir.iterdir():
            if path.name.startswith(prefix):
                try:
                    path.unlink()
                except OSError:
                    logger.exception("failed to unlink %s", path)

    # Raw MURFI volumes: post-rename_step_volumes filename encodes BOTH
    # task + run, so this glob is task-specific. Only files for this
    # step's (task, run) get deleted — Rest 1 (task=rest, run=1) is NOT
    # affected when restarting Transfer Pre (task=transferpre, run=1).
    # Regression: the old glob was ``img-{run:05d}-*.nii`` which matched
    # Rest 1 when restarting any run=1 rt15 step; lost sub-morgan's Rest 1.
    img_dir = session_dir.parent / "img"
    if img_dir.is_dir():
        for path in img_dir.glob(step_volume_glob(step.task, step.run)):
            try:
                path.unlink()
            except OSError:
                logger.exception("failed to unlink %s", path)


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def write_provenance(session_dir: Path, cli_argv: list[str] | None = None) -> Path:
    """Write a ``provenance.json`` snapshot at session start.

    Captures what-ran-when so RAs and future-us can answer "which code
    produced this data?" Values captured:

    * ``timestamp`` — ISO-8601 UTC
    * ``git_sha`` / ``git_branch`` — best-effort (empty when not a repo)
    * ``hostname`` / ``platform`` / ``python`` — execution environment
    * ``cli_argv`` — how the pipeline was invoked

    Idempotent: running twice overwrites with the latest snapshot. That's
    the right behavior — if the pipeline is re-started for a session, the
    latest provenance record is the interesting one.

    Returns the path written.
    """
    out = session_dir / "provenance.json"
    session_dir.mkdir(parents=True, exist_ok=True)

    def _git(args: list[str]) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=2.0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git(["rev-parse", "HEAD"]),
        "git_branch": _git(["rev-parse", "--abbrev-ref", "HEAD"]),
        "git_dirty": bool(_git(["status", "--porcelain"])),
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "cli_argv": cli_argv if cli_argv is not None else list(sys.argv),
    }
    _atomic_write_json(out, payload)
    return out


# Avoid unused-import warnings when StepKind is imported only for legacy tests.
_ = StepKind
