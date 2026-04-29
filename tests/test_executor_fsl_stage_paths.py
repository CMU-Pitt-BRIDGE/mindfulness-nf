"""FslStageExecutor must construct a SubjectLayout from the BIDS session dir.

The runner passes the session dir (``sub-X/ses-Y``) as ``subject_dir``. The
executor now constructs a :class:`SubjectLayout` from that, which provides
typed access to both subject-scoped (``img/``, ``xfm/``, ``mask/``) and
session-scoped (``rest/``, ``qc/``) paths. No more ``.parent`` ladders or
hardcoded ``ses-localizer`` literals.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import StepConfig, StepKind
from mindfulness_nf.orchestration.executors.fsl_stage import FslStageExecutor
from mindfulness_nf.orchestration.layout import SubjectLayout


@pytest.mark.asyncio
async def test_fslmerge_stage_constructs_layout_and_finds_subject_img_dir(
    tmp_path: Path,
) -> None:
    """With a BIDS session dir passed in, the executor builds a SubjectLayout
    and the ICA helpers receive ``layout`` — which exposes ``img_dir`` at
    the subject root and ``rest_dir`` at the session root."""
    subject_root = tmp_path / "sub-001"
    session_dir = subject_root / "ses-process"
    session_dir.mkdir(parents=True)
    img_dir = subject_root / "img"
    img_dir.mkdir()
    for v in range(1, 251):
        (img_dir / f"img-00002-{v:05d}.nii").write_bytes(b"FAKE")

    step = StepConfig(
        name="Merge rests",
        task="merge",
        run=None,
        progress_target=100,
        progress_unit="percent",
        xml_name=None,
        kind=StepKind.PROCESS_STAGE,
        fsl_command="fslmerge",
    )

    captured: dict = {}

    async def _fake_list_runs(layout: SubjectLayout):
        captured["list_runs_layout"] = layout
        from mindfulness_nf.orchestration.ica import RunInfo

        return (
            RunInfo(run_name="run-02", volume_count=250, path=layout.img_dir),
        )

    async def _fake_merge_runs(
        layout: SubjectLayout, run_indices, tr: float
    ) -> Path:
        captured["merge_runs_layout"] = layout
        merged = layout.rest_dir / "merged.nii"
        merged.parent.mkdir(parents=True, exist_ok=True)
        merged.write_bytes(b"FAKE_MERGED_NIFTI")
        return merged

    async def _noop_preprocess(self, merged: Path) -> None:
        stem = merged.with_suffix("")
        (Path(f"{stem}_mcflirt_median_bet.nii")).write_bytes(b"FAKE_REFERENCE")

    with (
        patch(
            "mindfulness_nf.orchestration.executors.fsl_stage.ica_mod.list_runs",
            side_effect=_fake_list_runs,
        ),
        patch(
            "mindfulness_nf.orchestration.executors.fsl_stage.ica_mod.merge_runs",
            side_effect=_fake_merge_runs,
        ),
        patch.object(FslStageExecutor, "_preprocess_reference", _noop_preprocess),
    ):
        executor = FslStageExecutor(
            config=step,
            subject_dir=session_dir,  # runner passes SESSION dir
            pipeline=PipelineConfig(),
            scanner_config=ScannerConfig(),
        )
        outcome = await asyncio.wait_for(executor.run(lambda _p: None), timeout=5.0)

    assert outcome.succeeded is True, f"fslmerge stage failed: {outcome.error!r}"
    # Both helpers received the SAME SubjectLayout — the single source of truth.
    assert captured["list_runs_layout"] is captured["merge_runs_layout"]
    layout = captured["list_runs_layout"]
    assert layout.subject_id == "sub-001"
    assert layout.session_type == "process"
    # img_dir resolves to the subject root; rest_dir to the session root.
    assert layout.img_dir == img_dir.resolve()
    assert layout.rest_dir == (session_dir / "rest").resolve()
