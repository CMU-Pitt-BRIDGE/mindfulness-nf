"""Mask registration to 2-volume reference space.

Imperative shell: I/O is expected here. All blocking FSL subprocess calls
run in threads via :func:`run_interruptible` for operator-cancellable behavior.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path

from mindfulness_nf.orchestration.fsl_subprocess import run_interruptible
from mindfulness_nf.orchestration.layout import SubjectLayout


async def register_masks(
    layout: SubjectLayout,
    dmn_mask: Path,
    cen_mask: Path,
    *,
    on_progress: Callable[[str], None] | None = None,
    stop_event: asyncio.Event | None = None,
) -> tuple[Path, Path]:
    """Register DMN and CEN masks from resting-state space to study_ref space.

    Performs the following steps (mirroring the ``register`` step in
    ``feedback.sh``):

    1. Finds the latest ``series*_ref.nii`` in ``<subject_dir>/xfm/``.
    2. Skull-strips the reference.
    3. Computes a FLIRT transform from the resting-state median to the
       study reference.
    4. Applies that transform to each mask with nearest-neighbour
       interpolation.
    5. Masks the result with an eroded brain mask to keep voxels inside
       the brain.

    Parameters
    ----------
    layout:
        :class:`SubjectLayout` for the session. ``xfm_dir`` and
        ``mask_dir`` are subject-scoped; ``qc_dir`` is session-scoped
        (QC overlays belong to the session that produced them).
    dmn_mask:
        Path to the DMN mask in resting-state native space
        (``dmn_rest_original.nii``).
    cen_mask:
        Path to the CEN mask in resting-state native space
        (``cen_rest_original.nii``).
    on_progress:
        Optional callback; called with step description strings.

    Returns
    -------
    tuple[Path, Path]
        ``(dmn_registered, cen_registered)`` -- final MURFI-ready masks
        (``mask/dmn.nii``, ``mask/cen.nii``).
    """

    def _report(msg: str) -> None:
        if on_progress is not None:
            on_progress(msg)

    xfm_dir = layout.xfm_dir
    mask_dir = layout.mask_dir
    qc_dir = layout.qc_dir
    mask_dir.mkdir(parents=True, exist_ok=True)
    qc_dir.mkdir(parents=True, exist_ok=True)

    # Find latest series reference (``series*_ref.nii`` in xfm/).
    _report("Finding latest series reference")
    ref_files = sorted(
        p
        for p in await asyncio.to_thread(list, xfm_dir.iterdir())
        if p.name.startswith("series")
        and p.name.endswith("_ref.nii")
        and not p.name.endswith("_brain.nii")
    )
    if not ref_files:
        msg = f"No series reference file found in {xfm_dir}"
        raise FileNotFoundError(msg)

    latest_ref = ref_files[-1]
    latest_ref_stem = latest_ref.with_suffix("")  # Drop .nii for FSL naming.

    study_ref = xfm_dir / "study_ref.nii"

    # Copy localizer_ref if it doesn't exist.
    localizer_ref = xfm_dir / "localizer_ref.nii"
    if not localizer_ref.exists() and study_ref.exists():
        content = await asyncio.to_thread(study_ref.read_bytes)
        await asyncio.to_thread(localizer_ref.write_bytes, content)

    # Copy latest ref to study_ref.
    _report("Updating study_ref")
    content = await asyncio.to_thread(latest_ref.read_bytes)
    await asyncio.to_thread(study_ref.write_bytes, content)

    # Skull-strip reference.
    _report("Skull-stripping reference volume")
    brain_path = Path(f"{latest_ref_stem}_brain")
    await run_interruptible(
        [
            "bet",
            str(latest_ref_stem),
            str(brain_path),
            "-R", "-f", "0.4", "-g", "0", "-m",
        ],
        stop_event=stop_event,
    )

    # QC image (non-critical: ignore failures so missing `slices` / bad
    # GIF generation doesn't fail the whole registration).
    brain_mask_path = Path(f"{latest_ref_stem}_brain_mask")
    await run_interruptible(
        [
            "slices",
            str(latest_ref),
            f"{brain_mask_path}.nii",
            "-o", str(qc_dir / "2vol_skullstrip_brain_mask_check.gif"),
        ],
        stop_event=stop_event,
        check=False,
    )

    # Compute rest-to-study_ref transform.
    _report("Computing rest-to-study_ref registration")
    epi2reg_dir = xfm_dir / "epi2reg"
    epi2reg_dir.mkdir(parents=True, exist_ok=True)

    examplefunc = layout.rest_dir / layout.bold_bids_name(
        task="rest", run=1, suffix="bold_mcflirt_median_bet.nii"
    )
    rest2ref_mat = epi2reg_dir / "rest2studyref.mat"

    await run_interruptible(
        [
            "flirt",
            "-in", str(examplefunc),
            "-ref", str(brain_path),
            "-out", str(epi2reg_dir / "rest2studyref_brain"),
            "-omat", str(rest2ref_mat),
        ],
        stop_event=stop_event,
    )

    await run_interruptible(
        [
            "slices",
            str(epi2reg_dir / "rest2studyref_brain"),
            str(brain_path),
            "-o", str(qc_dir / "rest_warp_to_2vol_native_check.gif"),
        ],
        stop_event=stop_event,
        check=False,
    )

    # Register each mask.
    registered: list[Path] = []
    for mask_name, mask_path in [("dmn", dmn_mask), ("cen", cen_mask)]:
        _report(f"Registering {mask_name.upper()} to study_ref")

        temp_out = mask_dir / f"{mask_name}_temp"

        # Apply transform with nearest-neighbour interpolation.
        await run_interruptible(
            [
                "flirt",
                "-in", str(mask_path),
                "-ref", str(brain_path),
                "-out", str(temp_out),
                "-init", str(rest2ref_mat),
                "-applyxfm",
                "-interp", "nearestneighbour",
                "-datatype", "short",
            ],
            stop_event=stop_event,
        )

        # Erode brain mask (4x) to keep voxels inside brain.
        ero4_path = Path(f"{latest_ref_stem}_brain_mask_ero4")
        await run_interruptible(
            [
                "fslmaths",
                f"{brain_mask_path}",
                "-ero", "-ero", "-ero", "-ero",
                str(ero4_path),
            ],
            stop_event=stop_event,
        )

        # Multiply by eroded mask. The pipeline runs with
        # FSLOUTPUTTYPE=NIFTI (ica.py sets this at module load), so FSL
        # writes ``.nii`` not ``.nii.gz``.
        studyref_out = mask_dir / f"{mask_name}_studyref"
        await run_interruptible(
            [
                "fslmaths",
                f"{temp_out}.nii",
                "-mul", str(ero4_path),
                f"{studyref_out}.nii",
                "-odt", "short",
            ],
            stop_event=stop_event,
        )

        # Final brain mask cleanup.
        brain_bin = Path(f"{latest_ref_stem}_brain_bin")
        await run_interruptible(
            ["fslmaths", str(brain_path), "-bin", str(brain_bin)],
            stop_event=stop_event,
        )
        await run_interruptible(
            [
                "fslmaths",
                f"{studyref_out}.nii",
                "-mul", str(brain_bin),
                f"{studyref_out}.nii",
                "-odt", "short",
            ],
            stop_event=stop_event,
        )

        # Copy to final MURFI mask.
        final_mask = mask_dir / f"{mask_name}.nii"
        src_bytes = await asyncio.to_thread(
            Path(f"{studyref_out}.nii").read_bytes,
        )
        await asyncio.to_thread(final_mask.write_bytes, src_bytes)
        registered.append(final_mask)

        # Clean up temp files. Loop both extensions defensively — FSL may
        # produce either depending on build/env, and these unlinks are
        # .exists()-gated so extras are harmless.
        for suffix in (".nii", ".nii.gz"):
            temp = Path(f"{temp_out}{suffix}")
            if temp.exists():
                temp.unlink()
        studyref_gz = Path(f"{studyref_out}.nii.gz")
        if studyref_gz.exists():
            studyref_gz.unlink()

    # Clean up erosion and bin files.
    for pattern in ("_brain_mask_ero4", "_brain_bin"):
        for suffix in (".nii", ".nii.gz"):
            p = Path(f"{latest_ref_stem}{pattern}{suffix}")
            if p.exists():
                p.unlink()

    _report("Registration complete")
    return (registered[0], registered[1])
