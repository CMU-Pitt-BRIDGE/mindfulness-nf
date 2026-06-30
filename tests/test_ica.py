"""Tests for ICA orchestration and mask registration.

All FSL subprocess calls are mocked -- these tests verify argument
construction, file handling, and callback behaviour without requiring
FSL to be installed.

Uses ``SubjectLayout`` with session_type="process" (matches the real
pipeline: Process session produces the DMN/CEN masks consumed by later
Real-Time sessions for the same subject).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mindfulness_nf.orchestration.ica import RunInfo, list_runs, merge_runs, run_ica
from mindfulness_nf.orchestration.layout import SubjectLayout
from mindfulness_nf.orchestration.registration import register_masks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def layout(tmp_path: Path) -> SubjectLayout:
    """Build a layout for ``sub-001`` / ``ses-process`` under tmp_path."""
    (tmp_path / "sub-001" / "ses-process").mkdir(parents=True)
    return SubjectLayout(
        subjects_root=tmp_path,
        subject_id="sub-001",
        session_type="process",
    )


def _make_layout(tmp_path: Path, subject_id: str, session_type: str) -> SubjectLayout:
    (tmp_path / subject_id / f"ses-{session_type}").mkdir(parents=True)
    return SubjectLayout(
        subjects_root=tmp_path,
        subject_id=subject_id,
        session_type=session_type,
    )


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs_discovers_runs(layout: SubjectLayout) -> None:
    """list_runs should discover rest runs and count volumes correctly.

    Uses the post-rename task-keyed pattern ``img-rest-<run>-<vol>.nii``.
    """
    img_dir = layout.img_dir
    img_dir.mkdir(parents=True)

    for vol in range(5):
        (img_dir / f"img-rest-03-{vol:05d}.nii").touch()
    for vol in range(10):
        (img_dir / f"img-rest-07-{vol:05d}.nii").touch()

    runs = await list_runs(layout)

    assert len(runs) == 2
    assert runs[0] == RunInfo(run_name="run-03", volume_count=5, path=img_dir)
    assert runs[1] == RunInfo(run_name="run-07", volume_count=10, path=img_dir)


@pytest.mark.asyncio
async def test_list_runs_ignores_non_rest_tasks(layout: SubjectLayout) -> None:
    """Only task=rest files are counted as MELODIC inputs.

    Feedback / Transfer volumes live in the same subject-scoped img/ dir
    but must NOT be merged into the resting-state 4D used by MELODIC.
    """
    img_dir = layout.img_dir
    img_dir.mkdir(parents=True)
    for vol in range(3):
        (img_dir / f"img-rest-01-{vol:05d}.nii").touch()
    # These should be ignored:
    for vol in range(3):
        (img_dir / f"img-feedback-01-{vol:05d}.nii").touch()
    for vol in range(3):
        (img_dir / f"img-transferpre-01-{vol:05d}.nii").touch()

    runs = await list_runs(layout)
    assert len(runs) == 1
    assert runs[0].run_name == "run-01"
    assert runs[0].volume_count == 3


@pytest.mark.asyncio
async def test_list_runs_empty_when_no_img_dir(layout: SubjectLayout) -> None:
    runs = await list_runs(layout)
    assert runs == ()


@pytest.mark.asyncio
async def test_list_runs_ignores_non_matching_files(layout: SubjectLayout) -> None:
    img_dir = layout.img_dir
    img_dir.mkdir(parents=True)
    (img_dir / "other_file.nii").touch()
    (img_dir / "img-rest-01-00000.txt").touch()  # Wrong extension.
    # Legacy MURFI-native format (pre-rename) is NOT matched by list_runs:
    (img_dir / "img-00002-00000.nii").touch()
    # The only valid match:
    (img_dir / "img-rest-02-00000.nii").touch()

    runs = await list_runs(layout)

    assert len(runs) == 1
    assert runs[0].run_name == "run-02"
    assert runs[0].volume_count == 1


# ---------------------------------------------------------------------------
# merge_runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_runs_calls_fslmerge(layout: SubjectLayout) -> None:
    """merge_runs should call fslmerge with correct arguments."""
    img_dir = layout.img_dir
    img_dir.mkdir(parents=True)

    vol_files = []
    for vol in range(3):
        p = img_dir / f"img-rest-03-{vol:05d}.nii"
        p.touch()
        vol_files.append(p)

    with patch("mindfulness_nf.orchestration.ica.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)

        result = await merge_runs(layout, (3,), tr=1.2)

    assert mock_run.call_count == 1
    cmd = mock_run.call_args[0][0]

    assert cmd[0] == "fslmerge"
    assert cmd[1] == "-tr"
    # Output path is SESSION-scoped now (ses-process/rest/), not subject-root.
    assert str(layout.rest_dir) in cmd[2]
    assert cmd[2].endswith("_bold.nii")
    for i, vol_path in enumerate(sorted(vol_files)):
        assert cmd[3 + i] == str(vol_path)
    assert cmd[-1] == "1.2"

    assert result.parent == layout.rest_dir


@pytest.mark.asyncio
async def test_merge_runs_creates_rest_dir(layout: SubjectLayout) -> None:
    """merge_runs should create (session-scoped) rest/ directory if missing."""
    img_dir = layout.img_dir
    img_dir.mkdir(parents=True)
    (img_dir / "img-rest-01-00000.nii").touch()

    with patch("mindfulness_nf.orchestration.ica.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        await merge_runs(layout, (1,))

    assert layout.rest_dir.is_dir()


@pytest.mark.asyncio
async def test_merge_runs_raises_on_missing_volumes(layout: SubjectLayout) -> None:
    img_dir = layout.img_dir
    img_dir.mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match="No volumes found"):
        await merge_runs(layout, (99,))


@pytest.mark.asyncio
async def test_merge_runs_uses_session_type_in_filename(tmp_path: Path) -> None:
    """BIDS filename must encode actual session_type, not hardcoded 'localizer'.

    Regression: producer AND consumer both used ``ses-localizer`` literal,
    so they agreed but diverged from the session. Now the session_type
    comes from the layout and flows through both.
    """
    layout = _make_layout(tmp_path, "sub-042", "rt15")
    img_dir = layout.img_dir
    img_dir.mkdir(parents=True)
    (img_dir / "img-rest-01-00000.nii").touch()

    with patch("mindfulness_nf.orchestration.ica.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        result = await merge_runs(layout, (1,))

    assert "ses-rt15" in result.name
    assert "ses-localizer" not in result.name


# ---------------------------------------------------------------------------
# run_ica
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_ica_substitutes_fsf_template(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path, "sub-001", "process")
    rest_dir = layout.rest_dir
    rest_dir.mkdir(parents=True)

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

    ica_output = rest_dir / "rs_network.gica"
    ica_output.mkdir()

    with patch("mindfulness_nf.orchestration.ica.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)

        result = await run_ica(
            layout,
            (merged_a, merged_b),
            ref_vol,
            template_path=template,
            n_volumes=200,
        )

    fsf_path = rest_dir / "sub-001_ses-process_task-rest_run-01_bold.fsf"
    assert fsf_path.exists()
    fsf_content = fsf_path.read_text()

    assert str(merged_a) in fsf_content
    assert str(merged_b) in fsf_content
    assert str(rest_dir / "rs_network") in fsf_content
    assert str(ref_vol) in fsf_content
    assert "set fmri(npts) 200" in fsf_content
    assert "DATA1" not in fsf_content
    assert "DATA2" not in fsf_content

    feat_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "feat"]
    assert len(feat_calls) == 1

    assert result == ica_output


@pytest.mark.asyncio
async def test_run_ica_single_run_template(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path, "sub-002", "process")
    rest_dir = layout.rest_dir
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
            layout,
            (merged,),
            ref_vol,
            template_path=template,
            n_volumes=180,
        )

    fsf_path = rest_dir / "sub-002_ses-process_task-rest_run-01_bold.fsf"
    fsf_content = fsf_path.read_text()

    assert str(merged) in fsf_content
    assert "DATA" not in fsf_content.replace(str(merged), "")
    assert "set fmri(npts) 180" in fsf_content
    assert result == ica_output


@pytest.mark.asyncio
async def test_run_ica_on_progress_called(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path, "sub-003", "process")
    rest_dir = layout.rest_dir
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
            layout,
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


def _fsl_side_effect_factory() -> callable:
    """Build an async side_effect that emulates flirt/bet/fslmaths file I/O."""

    async def mock_run_interruptible(cmd, **kwargs):
        if cmd[0] == "fslmaths":
            for arg in cmd[1:]:
                if arg.endswith(".nii") or arg.endswith(".nii.gz"):
                    Path(arg).parent.mkdir(parents=True, exist_ok=True)
                    if not Path(arg).exists():
                        Path(arg).write_bytes(b"fsl_output")
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
            bet_out = cmd[2]
            mask_out = Path(f"{bet_out}_mask.nii")
            mask_out.parent.mkdir(parents=True, exist_ok=True)
            if not mask_out.exists():
                mask_out.write_bytes(b"mask_data")
            bet_nii = Path(f"{bet_out}.nii")
            if not bet_nii.exists():
                bet_nii.write_bytes(b"bet_output")
        return 0

    return mock_run_interruptible


@pytest.mark.asyncio
async def test_register_masks_calls_flirt(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path, "sub-001", "process")
    xfm_dir = layout.xfm_dir
    mask_dir = layout.mask_dir
    rest_dir = layout.rest_dir
    for d in (xfm_dir, mask_dir, rest_dir):
        d.mkdir(parents=True)

    (xfm_dir / "series3_ref.nii").write_bytes(b"ref_data")
    (xfm_dir / "study_ref.nii").write_bytes(b"study_data")
    examplefunc = rest_dir / layout.bold_bids_name(
        task="rest", run=1, suffix="bold_mcflirt_median_bet.nii"
    )
    examplefunc.write_bytes(b"func_data")

    dmn_mask = mask_dir / "dmn_rest_original.nii"
    cen_mask = mask_dir / "cen_rest_original.nii"
    dmn_mask.write_bytes(b"dmn_data")
    cen_mask.write_bytes(b"cen_data")

    progress_steps: list[str] = []

    with patch(
        "mindfulness_nf.orchestration.registration.run_interruptible",
        side_effect=_fsl_side_effect_factory(),
    ):
        dmn_out, cen_out = await register_masks(
            layout,
            dmn_mask,
            cen_mask,
            on_progress=progress_steps.append,
        )

    assert any("study_ref" in s.lower() for s in progress_steps)
    assert any("skull" in s.lower() for s in progress_steps)
    assert any("DMN" in s.upper() for s in progress_steps)
    assert any("CEN" in s.upper() for s in progress_steps)
    assert "Registration complete" in progress_steps

    assert dmn_out == mask_dir / "dmn.nii"
    assert cen_out == mask_dir / "cen.nii"


@pytest.mark.asyncio
async def test_register_masks_raises_on_missing_ref(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path, "sub-001", "process")
    xfm_dir = layout.xfm_dir
    xfm_dir.mkdir(parents=True)

    dmn = layout.mask_dir / "dmn.nii"
    cen = layout.mask_dir / "cen.nii"

    with pytest.raises(FileNotFoundError, match="No series reference"):
        await register_masks(layout, dmn, cen)


@pytest.mark.asyncio
async def test_register_masks_on_progress_without_callback(tmp_path: Path) -> None:
    layout = _make_layout(tmp_path, "sub-001", "process")
    xfm_dir = layout.xfm_dir
    mask_dir = layout.mask_dir
    rest_dir = layout.rest_dir
    for d in (xfm_dir, mask_dir, rest_dir):
        d.mkdir(parents=True)

    (xfm_dir / "series1_ref.nii").write_bytes(b"ref_data")
    examplefunc = rest_dir / layout.bold_bids_name(
        task="rest", run=1, suffix="bold_mcflirt_median_bet.nii"
    )
    examplefunc.write_bytes(b"func_data")

    dmn_mask = mask_dir / "dmn_rest_original.nii"
    cen_mask = mask_dir / "cen_rest_original.nii"
    dmn_mask.write_bytes(b"dmn_data")
    cen_mask.write_bytes(b"cen_data")

    with patch(
        "mindfulness_nf.orchestration.registration.run_interruptible",
        side_effect=_fsl_side_effect_factory(),
    ):
        dmn_out, cen_out = await register_masks(layout, dmn_mask, cen_mask)

    assert dmn_out.name == "dmn.nii"
    assert cen_out.name == "cen.nii"


@pytest.mark.asyncio
async def test_register_masks_uses_session_scoped_rest_dir(tmp_path: Path) -> None:
    """Regression: examplefunc path is now session-scoped (ses-X/rest/), not subject-root."""
    layout = _make_layout(tmp_path, "sub-X", "process")
    xfm_dir = layout.xfm_dir
    mask_dir = layout.mask_dir
    rest_dir = layout.rest_dir
    for d in (xfm_dir, mask_dir, rest_dir):
        d.mkdir(parents=True)

    (xfm_dir / "series1_ref.nii").write_bytes(b"ref_data")

    dmn = mask_dir / "dmn_rest_original.nii"
    cen = mask_dir / "cen_rest_original.nii"
    dmn.write_bytes(b"d")
    cen.write_bytes(b"c")

    # Do NOT create examplefunc under the subject-root legacy path. It must
    # be under the session-scoped rest_dir for registration to find it.
    with patch(
        "mindfulness_nf.orchestration.registration.run_interruptible",
        side_effect=_fsl_side_effect_factory(),
    ):
        # flirt will reference examplefunc path; if it's not under
        # session-scoped rest_dir, the side_effect records whatever path
        # registration passed in. Assert it was session-scoped.
        captured_paths: list[str] = []

        async def capture(cmd, **kwargs):
            captured_paths.extend(cmd)
            return await _fsl_side_effect_factory()(cmd, **kwargs)

        with patch(
            "mindfulness_nf.orchestration.registration.run_interruptible",
            side_effect=capture,
        ):
            # examplefunc needs to exist for flirt to read it; create it.
            examplefunc = rest_dir / layout.bold_bids_name(
                task="rest", run=1, suffix="bold_mcflirt_median_bet.nii"
            )
            examplefunc.write_bytes(b"f")
            await register_masks(layout, dmn, cen)

    assert any(str(rest_dir) in p for p in captured_paths if isinstance(p, str))
