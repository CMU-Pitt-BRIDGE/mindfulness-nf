"""Post-step motion-parameter extraction.

After a MURFI-driven NF run completes and ``rename_step_volumes`` has
moved the per-TR NIfTIs into ``img-<task>-<run>-<vol>.nii``, this module
merges them into a 4D volume and runs ``mcflirt`` to derive motion
parameters per TR. The output lives under
``<session_dir>/derivatives/motion/<task>-<run>_motion.tsv`` (BIDS-ish).

This is best-effort: any failure (FSL not available, merge fails,
mcflirt fails) is logged but does not fail the step. The 4D merge is
deleted after motion extraction unless ``keep_merged=True``.

Why this matters: MURFI's GLM uses motion derivatives as regressors but
does NOT save the motion estimates to disk. Without this post-step,
researchers had no per-TR motion record at all (sub-morgan: zero
motion data). With raw NIfTIs preserved (post-Bug-C-fix), running
mcflirt post-hoc gives 6 motion params + framewise displacement per TR.
"""

from __future__ import annotations

import asyncio
import logging
import math
import shutil
import subprocess
from pathlib import Path

from mindfulness_nf.orchestration.subjects import step_volume_glob

logger = logging.getLogger(__name__)


# Skull radius in mm used for converting rotations (radians) to translations
# in the framewise-displacement formula (Power et al., 2012). Standard 50mm.
_FD_RADIUS_MM = 50.0


async def extract_motion_params(
    img_dir: Path,
    output_dir: Path,
    task: str,
    run: int,
    *,
    keep_merged: bool = False,
    timeout_seconds: float = 120.0,
) -> Path | None:
    """Merge per-TR volumes and dump motion parameters via mcflirt.

    Parameters
    ----------
    img_dir:
        Subject-scoped img/ directory (where rename_step_volumes wrote
        ``img-<task>-<run>-<vol>.nii`` files).
    output_dir:
        Where to write ``<task>-<run>_motion.tsv`` (created if missing).
    task, run:
        Task label + run number — selects which volumes to merge.
    keep_merged:
        If True, keep the intermediate 4D merged.nii. Default deletes it
        to save space.
    timeout_seconds:
        Hard cap on the FSL pipeline (fslmerge + mcflirt). 150 TRs of
        2mm BOLD at typical sizes finishes in ~20-40 s on modern CPUs.

    Returns
    -------
    Path | None
        The motion TSV path on success, or ``None`` if extraction was
        skipped or failed (a warning is logged in those cases).
    """
    if not img_dir.is_dir():
        logger.warning("motion: img_dir missing (%s); skipping", img_dir)
        return None
    pattern = step_volume_glob(task, run)
    volumes = sorted(img_dir.glob(pattern))
    if not volumes:
        logger.warning("motion: no volumes match %s in %s; skipping", pattern, img_dir)
        return None
    if shutil.which("fslmerge") is None or shutil.which("mcflirt") is None:
        logger.warning("motion: FSL not on PATH; skipping motion extraction")
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    merged = output_dir / f"{task}-{run:02d}_merged.nii"
    motion_tsv = output_dir / f"{task}-{run:02d}_motion.tsv"

    # 1. fslmerge -t : concatenate volumes along time.
    merge_cmd = ["fslmerge", "-t", str(merged), *[str(v) for v in volumes]]
    if not await _run_with_timeout(merge_cmd, timeout_seconds, "fslmerge"):
        return None

    # 2. mcflirt -plots : write motion params to <merged>_mcf.par.
    mcflirt_cmd = ["mcflirt", "-in", str(merged), "-plots"]
    if not await _run_with_timeout(mcflirt_cmd, timeout_seconds, "mcflirt"):
        if not keep_merged and merged.exists():
            merged.unlink()
        return None

    # 3. Convert .par → BIDS-ish TSV with framewise displacement column.
    par_path = merged.with_name(merged.name.replace(".nii", "_mcf.par"))
    if not par_path.is_file():
        # mcflirt sometimes appends suffix differently if input had .nii.gz
        alt = output_dir / f"{merged.stem}_mcf.par"
        par_path = alt if alt.is_file() else par_path
    if not par_path.is_file():
        logger.warning("motion: mcflirt produced no .par file (looked for %s)", par_path)
        return None

    rows = _parse_par(par_path)
    fd = _framewise_displacement(rows)
    _write_motion_tsv(motion_tsv, rows, fd)

    # Cleanup: keep the .par alongside the TSV (provenance), drop the
    # large merged 4D + the mcflirt-corrected 4D unless asked to keep.
    if not keep_merged:
        for p in (merged, merged.with_name(merged.name.replace(".nii", "_mcf.nii"))):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

    logger.info(
        "motion: %d TRs of motion params extracted to %s", len(rows), motion_tsv
    )
    return motion_tsv


async def _run_with_timeout(cmd: list[str], timeout: float, label: str) -> bool:
    """Run ``cmd`` async and return True on rc=0, else log + False."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            logger.warning("motion: %s timed out after %ss", label, timeout)
            return False
    except FileNotFoundError:
        logger.warning("motion: %s binary not found", label)
        return False
    except Exception as exc:  # noqa: BLE001 — diagnostic only
        logger.warning("motion: %s failed to launch: %s", label, exc)
        return False
    if proc.returncode != 0:
        logger.warning(
            "motion: %s exited %s; stderr=%s",
            label,
            proc.returncode,
            stderr.decode(errors="replace")[:300] if stderr else "",
        )
        return False
    return True


def _parse_par(par_path: Path) -> list[tuple[float, float, float, float, float, float]]:
    """Parse mcflirt's .par file: 6 columns per row (Rx Ry Rz Tx Ty Tz)."""
    rows: list[tuple[float, float, float, float, float, float]] = []
    for line in par_path.read_text().splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            rx, ry, rz, tx, ty, tz = (float(parts[i]) for i in range(6))
        except ValueError:
            continue
        rows.append((rx, ry, rz, tx, ty, tz))
    return rows


def _framewise_displacement(
    rows: list[tuple[float, float, float, float, float, float]],
) -> list[float]:
    """Power et al. 2012 framewise displacement: sum of |delta| of 6 motion
    params per TR, with rotations converted to mm via radius = 50mm."""
    fd = [0.0]
    for prev, cur in zip(rows, rows[1:]):
        drx, dry, drz = (cur[0] - prev[0], cur[1] - prev[1], cur[2] - prev[2])
        dtx, dty, dtz = (cur[3] - prev[3], cur[4] - prev[4], cur[5] - prev[5])
        # rotations to mm via small-angle approximation: arc = radius * angle
        arc = (
            abs(drx) * _FD_RADIUS_MM
            + abs(dry) * _FD_RADIUS_MM
            + abs(drz) * _FD_RADIUS_MM
        )
        translation = abs(dtx) + abs(dty) + abs(dtz)
        fd.append(arc + translation)
    return fd


def _write_motion_tsv(
    out_path: Path,
    rows: list[tuple[float, float, float, float, float, float]],
    fd: list[float],
) -> None:
    """Write a BIDS-ish motion TSV: rot_x rot_y rot_z trans_x trans_y trans_z framewise_displacement."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        fh.write("rot_x\trot_y\trot_z\ttrans_x\ttrans_y\ttrans_z\tframewise_displacement\n")
        for (rx, ry, rz, tx, ty, tz), f in zip(rows, fd):
            fh.write(
                f"{rx:.6f}\t{ry:.6f}\t{rz:.6f}\t"
                f"{tx:.6f}\t{ty:.6f}\t{tz:.6f}\t"
                f"{f:.6f}\n"
            )
