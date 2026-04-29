"""ICA pipeline orchestration for resting-state network extraction.

Imperative shell: I/O is expected here. Imports models from the functional
core (PipelineConfig from config.py). All blocking FSL calls run in threads
via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mindfulness_nf.config import PipelineConfig
from mindfulness_nf.orchestration.layout import SubjectLayout

# Force FSL to emit uncompressed NIfTI (*.nii), mirroring
# ``export FSLOUTPUTTYPE=NIFTI`` in ``murfi/scripts/run_session.sh``.
# Use direct assignment rather than ``setdefault`` because a sourced
# ``/etc/profile.d/fsl.sh`` typically exports ``FSLOUTPUTTYPE=NIFTI_GZ``
# into the parent shell — setdefault would respect that and we'd end up
# with ``*.nii.gz`` outputs that downstream callers checking for ``*.nii``
# report as "expected X.nii to exist".
os.environ["FSLOUTPUTTYPE"] = "NIFTI"

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

# Post-rename step-keyed pattern for rest runs: img-rest-<run>-<vol>.nii.
# Only files whose task label is "rest" are discoverable by MELODIC; this
# is deliberate — Feedback/Transfer volumes (task=feedback, transferpre,
# etc.) live in img/ but must not be merged into the resting-state 4D.
_IMG_REST_RE = re.compile(r"^img-rest-(\d{2})-(\d{5})\.nii$")

# Placeholders in the multi-run .fsf template.
_MULTI_RUN_PLACEHOLDERS = ("DATA1", "DATA2", "OUTPUT", "REFERENCE_VOL")
# Placeholders in the single-run .fsf template.
_SINGLE_RUN_PLACEHOLDERS = ("DATA", "OUTPUT", "REFERENCE_VOL")


@dataclass(frozen=True, slots=True)
class RunInfo:
    """Metadata for a single resting-state run discovered on disk."""

    run_name: str
    volume_count: int
    path: Path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def list_runs(layout: SubjectLayout) -> tuple[RunInfo, ...]:
    """Scan the subject's ``img/`` directory for resting-state runs.

    Matches the post-rename task-keyed pattern ``img-rest-<run>-<vol>.nii``
    — only files produced by steps with ``task="rest"`` are returned, so
    Feedback / Transfer volumes (same directory, different task label)
    are correctly excluded from the MELODIC input set.
    """
    img_dir = layout.img_dir
    if not img_dir.is_dir():
        return ()

    # Group by run index. Only task=rest files are matched.
    run_counts: dict[int, int] = {}
    entries = await asyncio.to_thread(list, img_dir.iterdir())
    for entry in entries:
        m = _IMG_REST_RE.match(entry.name)
        if m:
            run_idx = int(m.group(1))
            run_counts[run_idx] = run_counts.get(run_idx, 0) + 1

    runs: list[RunInfo] = []
    for idx in sorted(run_counts):
        runs.append(
            RunInfo(
                run_name=f"run-{idx:02d}",
                volume_count=run_counts[idx],
                path=img_dir,
            )
        )
    return tuple(runs)


async def merge_runs(
    layout: SubjectLayout,
    run_indices: tuple[int, ...],
    *,
    tr: float = PipelineConfig().tr,
) -> Path:
    """Merge selected runs into 4-D NIfTI files using ``fslmerge``.

    For each run index, all matching ``img-<run>-*.nii`` volumes are merged
    into ``<rest_dir>/<bids_bold_name>``. ``rest_dir`` is SESSION-scoped
    (``sub-X/ses-Y/rest/``) so two sessions cannot clobber each other's
    merges. The BIDS filename uses ``layout.session_type`` — the hardcoded
    ``ses-localizer`` that used to appear in producer and consumer code
    has been removed everywhere.
    """
    img_dir = layout.img_dir
    rest_dir = layout.rest_dir
    bold_name = layout.bold_bids_name
    rest_dir.mkdir(parents=True, exist_ok=True)

    merged_paths: list[Path] = []
    for seq, run_idx in enumerate(run_indices, start=1):
        # Post-rename task-keyed pattern: img-rest-<run>-<vol>.nii.
        pattern = f"img-rest-{run_idx:02d}-"
        volumes = sorted(
            p
            for p in await asyncio.to_thread(list, img_dir.iterdir())
            if p.name.startswith(pattern) and p.suffix == ".nii"
        )
        if not volumes:
            msg = f"No volumes found for run index {run_idx} in {img_dir}"
            raise FileNotFoundError(msg)

        out_path = rest_dir / bold_name(task="rest", run=seq)
        cmd = ["fslmerge", "-tr", str(out_path), *[str(v) for v in volumes], str(tr)]
        await asyncio.to_thread(
            subprocess.run, cmd, check=True, capture_output=True, text=True
        )
        merged_paths.append(out_path)

    return merged_paths[0]


async def run_ica(
    layout: SubjectLayout,
    merged_paths: tuple[Path, ...],
    reference_vol: Path,
    *,
    template_path: Path,
    n_volumes: int = 250,
    on_progress: Callable[[str], None] | None = None,
) -> Path:
    """Generate a ``.fsf`` from a template and run FEAT/MELODIC.

    Parameters
    ----------
    layout:
        :class:`SubjectLayout` for the current session. ``rest_dir`` is
        session-scoped; the ``.fsf`` and ICA output live there.
    merged_paths:
        Tuple of 1 or 2 merged bold NIfTI paths (preprocessed).
    reference_vol:
        Skull-stripped median reference volume for ICA.
    template_path:
        Path to the ``.fsf`` template file (multi-run or single-run).
    n_volumes:
        Number of volumes per run (used to patch ``npts`` in the template).
    on_progress:
        Optional callback; called with step description strings.

    Returns
    -------
    Path
        The ICA output directory (``<rest_dir>/rs_network.gica``
        or ``rs_network.ica``).
    """

    def _report(msg: str) -> None:
        if on_progress is not None:
            on_progress(msg)

    rest_dir = layout.rest_dir
    fsf_out = rest_dir / layout.bold_bids_name(task="rest", run=1, suffix="bold.fsf")
    output_dir = rest_dir / "rs_network"

    _report("Generating .fsf design file")
    fsf_content = await asyncio.to_thread(template_path.read_text)

    # Substitute placeholders.
    multi_run = len(merged_paths) == 2
    if multi_run:
        fsf_content = fsf_content.replace("DATA1", str(merged_paths[0]))
        fsf_content = fsf_content.replace("DATA2", str(merged_paths[1]))
    else:
        fsf_content = fsf_content.replace("DATA", str(merged_paths[0]))

    fsf_content = fsf_content.replace("OUTPUT", str(output_dir))
    fsf_content = fsf_content.replace("REFERENCE_VOL", str(reference_vol))
    fsf_content = fsf_content.replace(
        "set fmri(npts) 250", f"set fmri(npts) {n_volumes}"
    )

    await asyncio.to_thread(fsf_out.write_text, fsf_content)

    _report("Running FEAT/MELODIC ICA")
    # FEAT launches a web browser (Firefox) at the end to display its report.
    # Firefox inherits FEAT's stdout/stderr and keeps those pipes open long
    # after FEAT itself exits — which makes ``subprocess.run(capture_output=True)``
    # hang forever waiting for EOF on the pipe. Redirect to log files instead
    # so pipe-holding GUI children never block our supervisor. The log files
    # are still available in ``rest/`` for post-hoc debugging.
    feat_stdout = rest_dir / "feat.stdout.log"
    feat_stderr = rest_dir / "feat.stderr.log"

    def _run_feat() -> None:
        with feat_stdout.open("wb") as out, feat_stderr.open("wb") as err:
            subprocess.run(
                ["feat", str(fsf_out)],
                check=True, stdout=out, stderr=err,
            )

    await asyncio.to_thread(_run_feat)

    # Determine actual output directory (FEAT appends .gica or .ica).
    if (output_dir.parent / "rs_network.gica").is_dir():
        ica_dir = output_dir.parent / "rs_network.gica"
    elif (output_dir.parent / "rs_network.ica").is_dir():
        ica_dir = output_dir.parent / "rs_network.ica"
    else:
        # Fallback: use whichever was created.
        ica_dir = output_dir

    _report("ICA complete")
    return ica_dir


async def extract_masks(
    ica_dir: Path,
    template_dir: Path,
    *,
    layout: SubjectLayout,
    examplefunc: Path,
    examplefunc_mask: Path,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[Path, Path]:
    """Select DMN and CEN components from ICA output.

    Correlates ICA components with template networks, selects best-matching
    ICs using lateralisation analysis for CEN, and produces thresholded
    binary masks.

    Parameters
    ----------
    ica_dir:
        ICA output directory (``rs_network.gica`` or ``rs_network.ica``).
    template_dir:
        Directory containing ``template_networks.nii``, ``DMNax_brainmaskero2.nii``,
        ``CENa_brainmaskero2.nii``, and ``MNI152_T1_2mm_brain``.
    layout:
        :class:`SubjectLayout` — produced masks land in ``mask_dir`` which
        is subject-scoped (cross-session consumed by Real-Time).
    examplefunc:
        Skull-stripped median reference volume (native space).
    examplefunc_mask:
        Brain mask for the reference volume.
    on_progress:
        Optional callback; called with step description strings.

    Returns
    -------
    tuple[Path, Path]
        ``(dmn_mask_path, cen_mask_path)`` -- thresholded binary masks
        in native functional space.
    """

    def _report(msg: str) -> None:
        if on_progress is not None:
            on_progress(msg)

    multi_run = ica_dir.name.endswith(".gica")
    template_networks = template_dir / "template_networks.nii"
    template_dmn = template_dir / "DMNax_brainmaskero2.nii"
    template_cen = template_dir / "CENa_brainmaskero2.nii"
    mni_brain = template_dir / "MNI152_T1_2mm_brain"

    reg_dir = ica_dir / "reg" if not multi_run else ica_dir / "groupmelodic.ica" / "reg"
    if multi_run:
        melodic_ica_dir = ica_dir / "groupmelodic.ica"
    else:
        melodic_ica_dir = ica_dir

    reg_dir.mkdir(parents=True, exist_ok=True)

    # Determine infile (melodic_IC location).
    if multi_run:
        raw_ic = melodic_ica_dir / "melodic_IC.nii"
    else:
        raw_ic = melodic_ica_dir / "filtered_func_data.ica" / "melodic_IC.nii"

    _report("Registering templates to native space")

    if multi_run:
        # Resample melodic_IC to match examplefunc dimensions.
        resampled_ic = melodic_ica_dir / "melodic_IC_examplefunc.nii"
        await asyncio.to_thread(
            subprocess.run,
            [
                "applywarp",
                f"--in={raw_ic}",
                f"--ref={examplefunc}",
                f"--out={resampled_ic}",
                "--interp=trilinear",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        infile = resampled_ic

        # Register from native to MNI and invert.
        func2mni_mat = reg_dir / "example_func2mni.mat"
        mni2func_mat = reg_dir / "mni2example_func.mat"
        mni2func_out = reg_dir / "mni2example_func.nii"

        await asyncio.to_thread(
            subprocess.run,
            [
                "flirt",
                "-in", str(examplefunc),
                "-ref", str(mni_brain),
                "-out", str(reg_dir / "example_func2mni"),
                "-omat", str(func2mni_mat),
                "-dof", "12",
                "-cost", "corratio",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        await asyncio.to_thread(
            subprocess.run,
            ["convert_xfm", "-omat", str(mni2func_mat), "-inverse", str(func2mni_mat)],
            check=True,
            capture_output=True,
            text=True,
        )

        # Warp templates to native space.
        template2func = reg_dir / "template_networks2example_func.nii"
        dmn2func = reg_dir / "template_dmn2example_func.nii"
        cen2func = reg_dir / "template_cen2example_func.nii"

        for src, dst in [
            (mni_brain, mni2func_out),
            (template_networks, template2func),
            (template_dmn, dmn2func),
            (template_cen, cen2func),
        ]:
            await asyncio.to_thread(
                subprocess.run,
                [
                    "flirt",
                    "-in", str(src),
                    "-ref", str(examplefunc),
                    "-out", str(dst),
                    "-init", str(mni2func_mat),
                    "-applyxfm",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
    else:
        # Single-run: ICs already in native space.
        infile = raw_ic

        mni2native_mat = reg_dir / "mni2native.mat"
        await asyncio.to_thread(
            subprocess.run,
            [
                "flirt",
                "-in", str(mni_brain),
                "-ref", str(examplefunc),
                "-out", str(reg_dir / "mni2native.nii"),
                "-omat", str(mni2native_mat),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        template2func = reg_dir / "template_networks_native.nii"
        dmn2func = reg_dir / "template_dmn_native.nii"
        cen2func = reg_dir / "template_cen_native.nii"

        for src, dst in [
            (template_networks, template2func),
            (template_dmn, dmn2func),
            (template_cen, cen2func),
        ]:
            await asyncio.to_thread(
                subprocess.run,
                [
                    "flirt",
                    "-in", str(src),
                    "-ref", str(examplefunc),
                    "-out", str(dst),
                    "-init", str(mni2native_mat),
                    "-applyxfm",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

    # Correlate ICs with templates (fslcc writes to stdout).
    _report("Correlating ICs with template networks")
    correlfile = melodic_ica_dir / "template_rsn_correlations_with_ICs.txt"
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "fslcc",
            "--noabs",
            "-p", "8",
            "-t", "-1",
            "-m", str(examplefunc_mask),
            str(infile),
            str(template2func),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    await asyncio.to_thread(correlfile.write_text, result.stdout)

    # Split ICs.
    _report("Splitting IC volumes")
    split_prefix = melodic_ica_dir / "melodic_IC_"
    await asyncio.to_thread(
        subprocess.run,
        ["fslsplit", str(infile), str(split_prefix)],
        check=True,
        capture_output=True,
        text=True,
    )

    # Run rsn_get IC selection.
    _report("Selecting DMN and CEN components")
    ica_version = "multi_run" if multi_run else "single_run"
    rsn_get_script = Path(__file__).resolve().parent.parent.parent / "murfi" / "scripts" / "rsn_get.py"
    # Use ``sys.executable`` rather than the literal string ``python`` so
    # the subprocess inherits the same interpreter (and site-packages) as
    # our TUI. Shell's ``python`` resolves via pyenv, which breaks when
    # ``.python-version`` specifies a version pyenv doesn't have locally —
    # our uv-managed .venv has it, but only this Python process knows where.
    # Pass ica_dir explicitly so rsn_get.py doesn't fall back to its legacy
    # ``../subjects/<subj>/rest/rs_network.*`` hardcode — after the layout
    # migration, ``rest/`` is session-scoped.
    await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            str(rsn_get_script),
            layout.subject_id,
            ica_version,
            str(ica_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(rsn_get_script.parent),
    )

    # Threshold masks.
    _report("Thresholding masks")
    dmn_uthresh = melodic_ica_dir / "dmn_uthresh.nii"
    cen_uthresh = melodic_ica_dir / "cen_uthresh_combined.nii"
    dmn_thresh = melodic_ica_dir / "dmn_thresh.nii"
    cen_thresh = melodic_ica_dir / "cen_thresh.nii"

    num_voxels_desired = 2000

    for uthresh, template_mask, thresh_out in [
        (dmn_uthresh, dmn2func, dmn_thresh),
        (cen_uthresh, cen2func, cen_thresh),
    ]:
        # Multiply by template mask.
        await asyncio.to_thread(
            subprocess.run,
            ["fslmaths", str(uthresh), "-mul", str(template_mask), str(uthresh)],
            check=True,
            capture_output=True,
            text=True,
        )
        # Get voxel count and compute percentile.
        stats_result = await asyncio.to_thread(
            subprocess.run,
            ["fslstats", str(uthresh), "-V"],
            capture_output=True,
            text=True,
        )
        n_voxels = int(stats_result.stdout.strip().split()[0])
        percentile = 100 * (1 - num_voxels_desired / n_voxels) if n_voxels > 0 else 0

        thresh_result = await asyncio.to_thread(
            subprocess.run,
            ["fslstats", str(uthresh), "-P", str(percentile)],
            capture_output=True,
            text=True,
        )
        thresh_value = thresh_result.stdout.strip()

        await asyncio.to_thread(
            subprocess.run,
            [
                "fslmaths", str(uthresh),
                "-thr", thresh_value,
                "-bin", str(thresh_out),
                "-odt", "short",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    # Copy to subject mask directory. mask_dir is subject-scoped: masks
    # produced in a Process session are consumed by later Real-Time
    # sessions for the same subject.
    mask_dir = layout.mask_dir
    mask_dir.mkdir(parents=True, exist_ok=True)
    dmn_native = mask_dir / "dmn_native_rest.nii"
    cen_native = mask_dir / "cen_native_rest.nii"

    for src, dst in [
        (dmn_thresh, dmn_native),
        (cen_thresh, cen_native),
        (dmn_thresh, mask_dir / "dmn_rest_original.nii"),
        (cen_thresh, mask_dir / "cen_rest_original.nii"),
    ]:
        content = await asyncio.to_thread(src.read_bytes)
        await asyncio.to_thread(dst.write_bytes, content)

    _report("Mask extraction complete")
    return (dmn_native, cen_native)
