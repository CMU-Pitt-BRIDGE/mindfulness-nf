"""Orchestration for the PsychoPy ball-task subprocess.

Imperative shell: I/O is expected here.  Imports models from the functional
core (config.py).  PsychoPy code is UNTOUCHED — we only launch it as a
subprocess.
"""

from __future__ import annotations

import asyncio
import csv
import os
import sys
from pathlib import Path


async def launch(
    subject: str,
    run_number: int,
    feedback: bool,
    duration: str = "15min",
    anchor: str = "",
    psychopy_dir: Path | None = None,
    data_dir: Path | None = None,
    session_type: str | None = None,
    task: str | None = None,
) -> asyncio.subprocess.Process:
    """Launch the PsychoPy ball-task as a subprocess.

    Parameters
    ----------
    subject:
        Participant ID (e.g. ``"sub-001"``).
    run_number:
        Run number (1-based).
    feedback:
        Whether to display neurofeedback.
    duration:
        Session duration, ``"15min"`` or ``"30min"``.
    anchor:
        Participant mindfulness anchor phrase.
    psychopy_dir:
        Path to the ``psychopy/balltask/`` directory.  Defaults to
        ``<project_root>/psychopy/balltask/``.
    data_dir:
        Root under which PsychoPy writes behavioral CSVs / BIDS TSVs.
        Defaults (when ``None``) to ``<psychopy_dir>/data/`` for ad-hoc
        runs outside the session framework. In the orchestrated flow the
        caller passes ``layout.psychopy_data_dir`` so the data lands
        inside the subject session tree (anti-mingling + RA handoff).
        The path is passed to the script via the
        ``MINDFULNESS_NF_PSYCHOPY_DATA_DIR`` env var; the script uses it
        as the root, creating ``<data_dir>/<subject>/`` as needed.
    session_type:
        BIDS session label (e.g. ``"rt15"``). When provided, routes to
        the BIDS TSV writer via ``MINDFULNESS_NF_SESSION_TYPE``. Without
        this the writer would hardcode ``ses-nf`` regardless of which
        real session ran — two different sessions for the same subject
        would collide on the same TSV filename.
    task:
        BIDS task label (``"feedback"``, ``"transferpre"``, ``"transferpost"``).
        When provided, overrides the script's legacy run-number → task
        inference (which assumed ``run==1 → transferpre``,
        ``run==2 → transferpost``, etc — incorrect for our rt15 protocol
        where Feedback 1 is run=2 and Transfer Post is run=7).

    Returns
    -------
    asyncio.subprocess.Process
        The running PsychoPy process handle.
    """
    if psychopy_dir is None:
        psychopy_dir = Path(__file__).resolve().parents[2] / "psychopy" / "balltask"

    feedback_str = "Feedback" if feedback else "No Feedback"

    args: list[str] = [
        sys.executable,
        "rt-network_feedback.py",
        subject,
        str(run_number),
        feedback_str,
        duration,
    ]
    if anchor:
        args.extend(anchor.split())

    env = os.environ.copy()
    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        env["MINDFULNESS_NF_PSYCHOPY_DATA_DIR"] = str(data_dir.resolve())
    if session_type is not None:
        env["MINDFULNESS_NF_SESSION_TYPE"] = session_type
    if task is not None:
        env["MINDFULNESS_NF_TASK"] = task

    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(psychopy_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    return process


async def wait(process: asyncio.subprocess.Process) -> int:
    """Wait for a PsychoPy subprocess to exit.

    Parameters
    ----------
    process:
        The process returned by :func:`launch`.

    Returns
    -------
    int
        The process exit code.

    Raises
    ------
    asyncio.CancelledError
        Re-raised after cleanup if the wait is cancelled.
    """
    try:
        await process.wait()
    except asyncio.CancelledError:
        process.terminate()
        await process.wait()
        raise
    assert process.returncode is not None
    return process.returncode


def _resolve_roi_csv(
    data_dir: Path, subject: str, previous_run: int, task: str | None = None
) -> Path | None:
    """Locate the ``_roi_outputs.csv`` for the previous NF run.

    The PsychoPy script writes files as
    ``<data_dir>/<subject>/<subject>_DMN_<task>_<run>_roi_outputs.csv``.
    Previously this function looked for ``run{N}.csv`` — a literal that
    never existed on disk, so every call silently returned ``default``
    and the adaptive-difficulty feature was a no-op for every subject.

    When ``task`` is provided, resolve directly. Otherwise glob for the
    most recent matching file (handles both feedback and no-feedback
    runs by preferring ``feedback_<run>`` over other task labels).
    """
    subject_dir = data_dir / subject
    if not subject_dir.is_dir():
        return None
    if task is not None:
        candidate = subject_dir / f"{subject}_DMN_{task}_{previous_run}_roi_outputs.csv"
        return candidate if candidate.exists() else None
    # No task hint — try the common cases in order.
    for task_guess in ("feedback", "transferpre", "transferpost", "No_Feedback", "Feedback"):
        candidate = subject_dir / f"{subject}_DMN_{task_guess}_{previous_run}_roi_outputs.csv"
        if candidate.exists():
            return candidate
    return None


def get_scale_factor(
    data_dir: Path,
    subject: str,
    previous_run: int,
    default: float = 10.0,
    min_hits: int = 3,
    max_hits: int = 5,
    increase: float = 1.25,
    decrease: float = 0.75,
    task: str | None = None,
) -> float:
    """Compute the adaptive scale factor from a previous run's CSV.

    Reads the ``_roi_outputs.csv`` file written by PsychoPy for the
    previous run, extracts the cumulative hit columns
    (``dmn_cumulative_hits``, ``cen_cumulative_hits``), and adjusts the
    previous run's scale factor up or down depending on hit counts.

    Parameters
    ----------
    data_dir:
        Root data directory (e.g. ``ses-*/sourcedata/psychopy`` or the
        legacy ``psychopy/balltask/data``).
    subject:
        Participant ID (with or without ``sub-`` prefix).
    previous_run:
        The run number to read from (task-scoped).
    task:
        Optional task label for direct file resolution (e.g. ``"feedback"``).
        When omitted, best-effort glob.
    default:
        Fallback scale factor when the CSV is missing or incomplete.
    min_hits:
        If total hits (CEN + DMN) are below this, scale factor is increased.
    max_hits:
        If either CEN or DMN hits exceed this, scale factor is decreased.
    increase:
        Multiplier to *increase* the scale factor (e.g. 1.25).
    decrease:
        Multiplier to *decrease* the scale factor (e.g. 0.75).

    Returns
    -------
    float
        The adjusted scale factor for the next run.
    """
    csv_path = _resolve_roi_csv(data_dir, subject, previous_run, task)
    if csv_path is None:
        return default

    try:
        rows = _read_csv(csv_path)
    except (FileNotFoundError, OSError):
        return default

    if not rows:
        return default

    try:
        dmn_hits = max(float(row["dmn_cumulative_hits"]) for row in rows)
        cen_hits = max(float(row["cen_cumulative_hits"]) for row in rows)
        previous_scale = float(rows[0]["scale_factor"])
    except (KeyError, ValueError):
        return default

    # Decrease if too many hits in either direction
    if dmn_hits > max_hits or cen_hits > max_hits:
        return previous_scale * decrease

    # Increase if not enough total hits
    if cen_hits + dmn_hits < min_hits:
        return previous_scale * increase

    # In range — keep the same
    return previous_scale


def get_previous_scale_factor(
    data_dir: Path,
    subject: str,
    previous_run: int,
    default: float = 10.0,
    task: str | None = None,
) -> float:
    """Read the scale_factor column from a previous run's ROI CSV.

    See :func:`get_scale_factor` for filename resolution details.
    """
    csv_path = _resolve_roi_csv(data_dir, subject, previous_run, task)
    if csv_path is None:
        return default

    try:
        rows = _read_csv(csv_path)
    except (FileNotFoundError, OSError):
        return default

    if not rows:
        return default

    try:
        return float(rows[0]["scale_factor"])
    except (KeyError, ValueError):
        return default


def _read_csv(path: Path) -> list[dict[str, str]]:
    """Read a CSV file into a list of dicts (one per row)."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)
