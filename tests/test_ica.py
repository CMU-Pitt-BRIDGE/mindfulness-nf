"""Tests for ICA orchestration and mask registration.

All FSL subprocess calls are mocked -- these tests verify argument
construction, file handling, and callback behaviour without requiring
FSL to be installed.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from mindfulness_nf.orchestration.ica import RunInfo, list_runs, merge_runs, run_ica
from mindfulness_nf.orchestration.registration import register_masks


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs_discovers_runs(tmp_path: Path) -> None:
    """list_runs should discover runs and count volumes correctly."""
    img_dir = tmp_path / "img"
    img_dir.mkdir()

    # Create mock volume files for two runs.
    for vol in range(5):
        (img_dir / f"img-00003-{vol:05d}.nii").touch()
    for vol in range(10):
        (img_dir / f"img-00007-{vol:05d}.nii").touch()

    runs = await list_runs(tmp_path)

    assert len(runs) == 2
    assert runs[0] == RunInfo(run_name="run-03", volume_count=5, path=img_dir)
    assert runs[1] == RunInfo(run_name="run-07", volume_count=10, path=img_dir)


@pytest.mark.asyncio
async def test_list_runs_empty_when_no_img_dir(tmp_path: Path) -> None:
    """list_runs should return empty tuple when img/ doesn't exist."""
    runs = await list_runs(tmp_path)
    assert runs == ()


@pytest.mark.asyncio
async def test_list_runs_ignores_non_matching_files(tmp_path: Path) -> None:
    """list_runs should skip files that don't match the img pattern."""
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    (img_dir / "other_file.nii").touch()
    (img_dir / "img-00001-00000.txt").touch()  # Wrong extension in regex.
    (img_dir / "img-00002-00000.nii").touch()

    runs = await list_runs(tmp_path)

    assert len(runs) == 1
    assert runs[0].run_name == "run-02"
    assert runs[0].volume_count == 1


# ---------------------------------------------------------------------------
# merge_runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_runs_calls_fslmerge(tmp_path: Path) -> None:
    """merge_runs should call fslmerge with correct arguments."""
    img_dir = tmp_path / "img"
    img_dir.mkdir()

    # Create mock volumes for run index 3.
    vol_files = []
    for vol in range(3):
        p = img_dir / f"img-00003-{vol:05d}.nii"
        p.touch()
        vol_files.append(p)

    with patch("mindfulness_nf.orchestration.ica.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)

        result = await merge_runs(tmp_path, (3,), tr=1.2)

    assert mock_run.call_count == 1
    args = mock_run.call_args
    cmd = args[0][0]

    assert cmd[0] == "fslmerge"
    assert cmd[1] == "-tr"
    # Output path should be in rest/ directory.
    assert "rest" in cmd[2]
    assert cmd[2].endswith("_bold.nii")
    # Volumes should be sorted.
    for i, vol_path in enumerate(sorted(vol_files)):
        assert cmd[3 + i] == str(vol_path)
    # TR at the end.
    assert cmd[-1] == "1.2"

    # Result should point into rest/ directory.
    assert result.parent == tmp_path / "rest"


@pytest.mark.asyncio
async def test_merge_runs_creates_rest_dir(tmp_path: Path) -> None:
    """merge_runs should create rest/ directory if it doesn't exist."""
    img_dir = tmp_path / "img"
    img_dir.mkdir()
    (img_dir / "img-00001-00000.nii").touch()

    with patch("mindfulness_nf.orchestration.ica.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        await merge_runs(tmp_path, (1,))

    assert (tmp_path / "rest").is_dir()


@pytest.mark.asyncio
async def test_merge_runs_raises_on_missing_volumes(tmp_path: Path) -> None:
    """merge_runs should raise FileNotFoundError for missing run index."""
    img_dir = tmp_path / "img"
    img_dir.mkdir()

    with pytest.raises(FileNotFoundError, match="No volumes found"):
        await merge_runs(tmp_path, (99,))


# ---------------------------------------------------------------------------
# run_ica
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_ica_substitutes_fsf_template(tmp_path: Path) -> None:
    """run_ica should substitute placeholders in the .fsf template."""
    subject_dir = tmp_path / "sub-001"
    rest_dir = subject_dir / "rest"
    rest_dir.mkdir(parents=True)

    # Create a minimal multi-run template.
    template = tmp_path / "template.fsf"
    template.write_text(
        'set feat_files(1) "DATA1"\n'
        'set feat_files(2) "DATA2"\n'
        'set fmri(outputdir) "OUTPUT"\n'
        'set fmri(regstandard) "REFERENCE_VOL"\n'
        "set fmri(npts) 250\n"
    )

    merged_a = rest_dir / "run-01_bold.nii"
    merged_b = rest_dir / "run-02_bold.nii"
    ref_vol = rest_dir / "median_bet.nii"

    # Create the ICA output directory that FEAT would produce.
    ica_output = rest_dir / "rs_network.gica"
    ica_output.mkdir()

    with patch("mindfulness_nf.orchestration.ica.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)

        result = await run_ica(
            subject_dir,
            (merged_a, merged_b),
            ref_vol,
            template_path=template,
            n_volumes=200,
        )

    # Check .fsf was written with substitutions.
    fsf_path = rest_dir / "sub-001_ses-localizer_task-rest_run-01_bold.fsf"
    assert fsf_path.exists()
    fsf_content = fsf_path.read_text()

    assert str(merged_a) in fsf_content
    assert str(merged_b) in fsf_content
    assert str(rest_dir / "rs_network") in fsf_content
    assert str(ref_vol) in fsf_content
    assert "set fmri(npts) 200" in fsf_content
    assert "DATA1" not in fsf_content
    assert "DATA2" not in fsf_content

    # FEAT should have been called.
    feat_calls = [
        c for c in mock_run.call_args_list if c[0][0][0] == "feat"
    ]
    assert len(feat_calls) == 1

    assert result == ica_output


@pytest.mark.asyncio
async def test_run_ica_single_run_template(tmp_path: Path) -> None:
    """run_ica should handle single-run template substitution."""
    subject_dir = tmp_path / "sub-002"
    rest_dir = subject_dir / "rest"
    rest_dir.mkdir(parents=True)

    template = tmp_path / "single_template.fsf"
    template.write_text(
        'set feat_files(1) "DATA"\n'
        'set fmri(outputdir) "OUTPUT"\n'
        'set fmri(regstandard) "REFERENCE_VOL"\n'
        "set fmri(npts) 250\n"
    )

    merged = rest_dir / "run-01_bold.nii"
    ref_vol = rest_dir / "median_bet.nii"

    ica_output = rest_dir / "rs_network.ica"
    ica_output.mkdir()

    with patch("mindfulness_nf.orchestration.ica.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)

        result = await run_ica(
            subject_dir,
            (merged,),
            ref_vol,
            template_path=template,
            n_volumes=180,
        )

    fsf_path = rest_dir / "sub-002_ses-localizer_task-rest_run-01_bold.fsf"
    fsf_content = fsf_path.read_text()

    assert str(merged) in fsf_content
    assert "DATA" not in fsf_content.replace(str(merged), "")
    assert "set fmri(npts) 180" in fsf_content
    assert result == ica_output


# ---------------------------------------------------------------------------
# on_progress callbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_ica_on_progress_called(tmp_path: Path) -> None:
    """run_ica should call on_progress with step descriptions."""
    subject_dir = tmp_path / "sub-003"
    rest_dir = subject_dir / "rest"
    rest_dir.mkdir(parents=True)

    template = tmp_path / "template.fsf"
    template.write_text(
        'set feat_files(1) "DATA"\n'
        'set fmri(outputdir) "OUTPUT"\n'
        'set fmri(regstandard) "REFERENCE_VOL"\n'
        "set fmri(npts) 250\n"
    )

    ica_output = rest_dir / "rs_network.ica"
    ica_output.mkdir()

    progress_steps: list[str] = []

    with patch("mindfulness_nf.orchestration.ica.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)

        await run_ica(
            subject_dir,
            (rest_dir / "run-01.nii",),
            rest_dir / "ref.nii",
            template_path=template,
            on_progress=progress_steps.append,
        )

    assert "Generating .fsf design file" in progress_steps
    assert "Running FEAT/MELODIC ICA" in progress_steps
    assert "ICA complete" in progress_steps


# ---------------------------------------------------------------------------
# register_masks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_masks_calls_flirt(tmp_path: Path) -> None:
    """register_masks should call flirt and produce final mask paths."""
    subject_dir = tmp_path / "sub-001"
    xfm_dir = subject_dir / "xfm"
    mask_dir = subject_dir / "mask"
    rest_dir = subject_dir / "rest"
    for d in (xfm_dir, mask_dir, rest_dir):
        d.mkdir(parents=True)

    # Create required files.
    (xfm_dir / "series3_ref.nii").write_bytes(b"ref_data")
    (xfm_dir / "study_ref.nii").write_bytes(b"study_data")
    examplefunc = rest_dir / "sub-001_ses-localizer_task-rest_run-01_bold_mcflirt_median_bet.nii"
    examplefunc.write_bytes(b"func_data")

    dmn_mask = mask_dir / "dmn_rest_original.nii"
    cen_mask = mask_dir / "cen_rest_original.nii"
    dmn_mask.write_bytes(b"dmn_data")
    cen_mask.write_bytes(b"cen_data")

    progress_steps: list[str] = []

    def mock_subprocess_run(cmd, **kwargs):
        result = MagicMock(returncode=0, stdout="1500 1500.0", stderr="")

        # When gunzip is called, create the uncompressed output.
        if cmd[0] == "gunzip":
            gz_path = Path(cmd[-1])
            if gz_path.exists():
                gz_path.unlink()
            # Create the .nii version.
            nii_path = Path(str(gz_path).replace(".nii.gz", ".nii"))
            nii_path.write_bytes(b"mask_data")

        # When fslmaths creates output, ensure files exist for subsequent steps.
        if cmd[0] == "fslmaths":
            # Find the output path (typically second-to-last or after -mul/-bin).
            for i, arg in enumerate(cmd):
                if arg.endswith(".nii") or arg.endswith(".nii.gz"):
                    Path(arg).parent.mkdir(parents=True, exist_ok=True)
                    if not Path(arg).exists():
                        Path(arg).write_bytes(b"fsl_output")

        # When flirt creates output, ensure output files exist.
        if cmd[0] in ("flirt", "bet"):
            for i, arg in enumerate(cmd):
                if arg == "-out" and i + 1 < len(cmd):
                    out = Path(cmd[i + 1])
                    out.parent.mkdir(parents=True, exist_ok=True)
                    if not out.exists():
                        out.write_bytes(b"fsl_output")
                    # Also create .nii variant.
                    nii = Path(f"{cmd[i+1]}.nii")
                    if not nii.exists():
                        nii.write_bytes(b"fsl_output")
                if arg == "-omat" and i + 1 < len(cmd):
                    out = Path(cmd[i + 1])
                    out.parent.mkdir(parents=True, exist_ok=True)
                    if not out.exists():
                        out.write_bytes(b"mat_data")

        # bet -m creates a mask file.
        if cmd[0] == "bet" and "-m" in cmd:
            bet_out = cmd[2]
            mask_out = Path(f"{bet_out}_mask.nii")
            mask_out.parent.mkdir(parents=True, exist_ok=True)
            if not mask_out.exists():
                mask_out.write_bytes(b"mask_data")

        return result

    with patch(
        "mindfulness_nf.orchestration.registration.subprocess.run",
        side_effect=mock_subprocess_run,
    ):
        dmn_out, cen_out = await register_masks(
            subject_dir,
            dmn_mask,
            cen_mask,
            on_progress=progress_steps.append,
        )

    # Verify progress callbacks were called.
    assert any("study_ref" in s.lower() for s in progress_steps)
    assert any("skull" in s.lower() for s in progress_steps)
    assert any("DMN" in s.upper() for s in progress_steps)
    assert any("CEN" in s.upper() for s in progress_steps)
    assert "Registration complete" in progress_steps

    # Verify output paths.
    assert dmn_out == mask_dir / "dmn.nii"
    assert cen_out == mask_dir / "cen.nii"


@pytest.mark.asyncio
async def test_register_masks_raises_on_missing_ref(tmp_path: Path) -> None:
    """register_masks should raise when no series reference is found."""
    subject_dir = tmp_path / "sub-001"
    xfm_dir = subject_dir / "xfm"
    xfm_dir.mkdir(parents=True)

    dmn = subject_dir / "mask" / "dmn.nii"
    cen = subject_dir / "mask" / "cen.nii"

    with pytest.raises(FileNotFoundError, match="No series reference"):
        await register_masks(subject_dir, dmn, cen)


@pytest.mark.asyncio
async def test_register_masks_on_progress_without_callback(tmp_path: Path) -> None:
    """register_masks should work fine with on_progress=None."""
    subject_dir = tmp_path / "sub-001"
    xfm_dir = subject_dir / "xfm"
    mask_dir = subject_dir / "mask"
    rest_dir = subject_dir / "rest"
    for d in (xfm_dir, mask_dir, rest_dir):
        d.mkdir(parents=True)

    (xfm_dir / "series1_ref.nii").write_bytes(b"ref_data")
    examplefunc = rest_dir / "sub-001_ses-localizer_task-rest_run-01_bold_mcflirt_median_bet.nii"
    examplefunc.write_bytes(b"func_data")

    dmn_mask = mask_dir / "dmn_rest_original.nii"
    cen_mask = mask_dir / "cen_rest_original.nii"
    dmn_mask.write_bytes(b"dmn_data")
    cen_mask.write_bytes(b"cen_data")

    def mock_subprocess_run(cmd, **kwargs):
        result = MagicMock(returncode=0, stdout="1500 1500.0", stderr="")
        if cmd[0] == "gunzip":
            gz_path = Path(cmd[-1])
            if gz_path.exists():
                gz_path.unlink()
            nii_path = Path(str(gz_path).replace(".nii.gz", ".nii"))
            nii_path.write_bytes(b"mask_data")
        if cmd[0] in ("flirt", "bet"):
            for i, arg in enumerate(cmd):
                if arg == "-out" and i + 1 < len(cmd):
                    out = Path(cmd[i + 1])
                    out.parent.mkdir(parents=True, exist_ok=True)
                    if not out.exists():
                        out.write_bytes(b"fsl_output")
                    nii = Path(f"{cmd[i+1]}.nii")
                    if not nii.exists():
                        nii.write_bytes(b"fsl_output")
                if arg == "-omat" and i + 1 < len(cmd):
                    out = Path(cmd[i + 1])
                    out.parent.mkdir(parents=True, exist_ok=True)
                    if not out.exists():
                        out.write_bytes(b"mat_data")
        if cmd[0] == "bet" and "-m" in cmd:
            mask_out = Path(f"{cmd[2]}_mask.nii")
            mask_out.parent.mkdir(parents=True, exist_ok=True)
            if not mask_out.exists():
                mask_out.write_bytes(b"mask_data")
        if cmd[0] == "fslmaths":
            for arg in cmd:
                if arg.endswith(".nii") or arg.endswith(".nii.gz"):
                    Path(arg).parent.mkdir(parents=True, exist_ok=True)
                    if not Path(arg).exists():
                        Path(arg).write_bytes(b"fsl_output")
        return result

    with patch(
        "mindfulness_nf.orchestration.registration.subprocess.run",
        side_effect=mock_subprocess_run,
    ):
        # Should not raise even without on_progress.
        dmn_out, cen_out = await register_masks(
            subject_dir, dmn_mask, cen_mask
        )

    assert dmn_out.name == "dmn.nii"
    assert cen_out.name == "cen.nii"


# ---------------------------------------------------------------------------
# RunInfo dataclass
# ---------------------------------------------------------------------------


def test_run_info_is_frozen() -> None:
    """RunInfo should be a frozen dataclass."""
    ri = RunInfo(run_name="run-01", volume_count=250, path=Path("/tmp"))
    with pytest.raises(AttributeError):
        ri.run_name = "changed"  # type: ignore[misc]
