"""Orchestration for the PsychoPy ball-task subprocess.

Imperative shell: I/O is expected here.  Imports models from the functional
core (config.py).  PsychoPy code is UNTOUCHED — we only launch it as a
subprocess.
"""

from __future__ import annotations

import asyncio
import csv
import sys
from pathlib import Path


async def launch(
    subject: str,
    run_number: int,
    feedback: bool,
    duration: str = "15min",
    anchor: str = "",
    psychopy_dir: Path | None = None,
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

    process = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(psychopy_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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


def get_scale_factor(
    data_dir: Path,
    subject: str,
    previous_run: int,
    default: float = 10.0,
    min_hits: int = 3,
    max_hits: int = 5,
    increase: float = 1.25,
    decrease: float = 0.75,
) -> float:
    """Compute the adaptive scale factor from a previous run's CSV.

    Reads the CSV written by PsychoPy for the previous run, extracts the
    cumulative hit columns (``dmn_cumulative_hits``, ``cen_cumulative_hits``),
    and adjusts the previous run's scale factor up or down depending on hit
    counts.

    Parameters
    ----------
    data_dir:
        Root data directory (e.g. ``psychopy/balltask/data``).
    subject:
        Participant ID.
    previous_run:
        The run number to read from.
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
    csv_path = data_dir / subject / f"run{previous_run}.csv"

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
) -> float:
    """Read the scale_factor column from a previous run's CSV.

    Parameters
    ----------
    data_dir:
        Root data directory (e.g. ``psychopy/balltask/data``).
    subject:
        Participant ID.
    previous_run:
        The run number to read from.
    default:
        Fallback when the CSV is missing or has no ``scale_factor`` column.

    Returns
    -------
    float
        The scale factor from the previous run.
    """
    csv_path = data_dir / subject / f"run{previous_run}.csv"

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
