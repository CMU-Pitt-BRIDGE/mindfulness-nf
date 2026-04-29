"""Tests for per-step img/ rename (task+run collision fix).

Two-layer bug this guards against:

1. MURFI assigns its own series number per process; each step launches a
   fresh MURFI subprocess so consecutive steps reuse the same series
   number and overwrite each other's ``img/img-<series>-<vol>.nii`` files.

2. Keying renamed files on *just* ``step.run`` is also unsafe: multiple
   steps across different sessions share ``run=1`` (Rest 1 in ses-loc3,
   Transfer Pre / Feedback 1 / Transfer Post in ses-rt15). Renaming all
   to ``img-00001-*`` collides those, and ``clear_bids_run_files`` on
   restart wiped everything (lost sub-morgan's Rest 1, 2026-04-21).

Fix: after each scan step completes, rename MURFI's native output to
``img-<task>-<run>-<vol>.nii``. Task + run uniquely identify a step across
the subject; no cross-task collisions possible.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import StepConfig, StepKind
from mindfulness_nf.orchestration.executors.vsend import VsendStepExecutor
from mindfulness_nf.orchestration.murfi import MurfiProcess
from mindfulness_nf.orchestration.scanner_source import NoOpScannerSource


def _make_step(run: int, name: str = "Rest") -> StepConfig:
    return StepConfig(
        name=name,
        task="rest",
        run=run,
        progress_target=3,
        progress_unit="volumes",
        xml_name="rest.xml",
        kind=StepKind.VSEND_SCAN,
    )


def _fake_murfi(log_path: Path, returncode: int | None = None) -> MurfiProcess:
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode
    proc.pid = 12345
    return MurfiProcess(process=proc, log_path=log_path, xml_name="rest.xml")


@pytest.mark.asyncio
async def test_vsend_rest_run_renames_to_task_run_keyed(tmp_path: Path) -> None:
    """Running Rest 1 (task=rest, step.run=1) leaves img/ containing
    img-rest-01-*.nii regardless of what series number MURFI chose.
    """
    subjects_root = tmp_path / "subjects"
    session_dir = subjects_root / "sub-001" / "ses-loc3"
    (session_dir / "log").mkdir(parents=True)
    (session_dir / "sourcedata" / "murfi" / "xml").mkdir(parents=True)
    (session_dir / "sourcedata" / "murfi" / "xml" / "rest.xml").write_text("<xml/>")
    log_path = session_dir / "log" / "murfi_rest.log"

    img_dir = session_dir.parent / "img"
    img_dir.mkdir(parents=True)
    murfi_series = 2  # what MURFI happens to pick for this step

    # Mock murfi.start so it simulates MURFI: writes the log lines and
    # the img/ files *after* the executor has snapshotted the dir.
    async def _fake_start(*_a, **_kw):
        log_path.write_text(
            "\n".join("received image from scanner" for _ in range(3)) + "\n"
        )
        for vol in (1, 2, 3):
            (img_dir / f"img-{murfi_series:05d}-{vol:05d}.nii").write_bytes(b"FAKE")
        return _fake_murfi(log_path, returncode=0)

    with patch(
        "mindfulness_nf.orchestration.executors.vsend.murfi_mod.start",
        side_effect=_fake_start,
    ), patch(
        "mindfulness_nf.orchestration.executors.vsend.murfi_mod.stop",
        AsyncMock(),
    ):
        executor = VsendStepExecutor(
            config=_make_step(run=1),
            subject_dir=session_dir,
            pipeline=PipelineConfig(),
            scanner_config=ScannerConfig(),
            scanner_source=NoOpScannerSource(),
        )
        outcome = await asyncio.wait_for(
            executor.run(lambda _p: None), timeout=3.0
        )

    assert outcome.succeeded is True
    files = sorted(p.name for p in img_dir.glob("img-*.nii"))
    # The MURFI-chosen series-00002 files must have been renamed to the
    # task+run-keyed form. ``task="rest"`` from _make_step.
    assert files == [
        "img-rest-01-00001.nii",
        "img-rest-01-00002.nii",
        "img-rest-01-00003.nii",
    ], f"got {files}"


@pytest.mark.asyncio
async def test_rest_2_does_not_overwrite_rest_1(tmp_path: Path) -> None:
    """Consecutive rest runs produce distinct img file sets on disk:
    Rest 1 → img-00001-*.nii, Rest 2 → img-00002-*.nii. This is the bug
    from the phantom scanner test, where both collapsed onto series 00002.
    """
    subjects_root = tmp_path / "subjects"
    session_dir = subjects_root / "sub-001" / "ses-loc3"
    (session_dir / "log").mkdir(parents=True)
    (session_dir / "sourcedata" / "murfi" / "xml").mkdir(parents=True)
    (session_dir / "sourcedata" / "murfi" / "xml" / "rest.xml").write_text("<xml/>")
    log_path = session_dir / "log" / "murfi_rest.log"
    img_dir = session_dir.parent / "img"
    img_dir.mkdir(parents=True)

    async def _run_one_step(run_number: int) -> None:
        # Each "MURFI" re-picks series 00002 (the observed real-scanner bug).
        async def _fake_start(*_a, **_kw):
            log_path.write_text(
                "\n".join("received image from scanner" for _ in range(3)) + "\n"
            )
            for vol in (1, 2, 3):
                (img_dir / f"img-00002-{vol:05d}.nii").write_bytes(b"FAKE")
            return _fake_murfi(log_path, returncode=0)

        with patch(
            "mindfulness_nf.orchestration.executors.vsend.murfi_mod.start",
            side_effect=_fake_start,
        ), patch(
            "mindfulness_nf.orchestration.executors.vsend.murfi_mod.stop",
            AsyncMock(),
        ):
            executor = VsendStepExecutor(
                config=_make_step(run=run_number, name=f"Rest {run_number}"),
                subject_dir=session_dir,
                pipeline=PipelineConfig(),
                scanner_config=ScannerConfig(),
                scanner_source=NoOpScannerSource(),
            )
            outcome = await asyncio.wait_for(
                executor.run(lambda _p: None), timeout=3.0
            )
            assert outcome.succeeded

    await _run_one_step(run_number=1)
    await _run_one_step(run_number=2)

    files = sorted(p.name for p in img_dir.glob("img-*.nii"))
    assert files == [
        "img-rest-01-00001.nii",
        "img-rest-01-00002.nii",
        "img-rest-01-00003.nii",
        "img-rest-02-00001.nii",
        "img-rest-02-00002.nii",
        "img-rest-02-00003.nii",
    ], f"got {files}"


@pytest.mark.asyncio
async def test_rest_and_nf_steps_with_same_run_do_not_collide(tmp_path: Path) -> None:
    """Rest 1 (ses-loc3, task=rest, run=1) and Transfer Pre (ses-rt15,
    task=transferpre, run=1) must NOT overwrite each other's img files.

    Regression guard: sub-morgan lost Rest 1's 250 raw volumes because
    both steps renamed their output to ``img-00001-*.nii``, and restarting
    Transfer Pre then deleted everything via ``clear_bids_run_files``.
    """
    subjects_root = tmp_path / "subjects"
    subject_root = subjects_root / "sub-001"
    img_dir = subject_root / "img"
    img_dir.mkdir(parents=True)

    async def _run_for_session(ses: str, task: str) -> None:
        session_dir = subject_root / f"ses-{ses}"
        (session_dir / "log").mkdir(parents=True, exist_ok=True)
        (session_dir / "sourcedata" / "murfi" / "xml").mkdir(parents=True, exist_ok=True)
        (session_dir / "sourcedata" / "murfi" / "xml" / "rest.xml").write_text("<xml/>")
        log_path = session_dir / "log" / f"murfi_rest_{task}.log"

        async def _fake_start(*_a, **_kw):
            log_path.write_text(
                "\n".join("received image from scanner" for _ in range(2)) + "\n"
            )
            for vol in (1, 2):
                (img_dir / f"img-00001-{vol:05d}.nii").write_bytes(b"FAKE_" + task.encode())
            return _fake_murfi(log_path, returncode=0)

        step = StepConfig(
            name=f"{task} run-1",
            task=task,
            run=1,
            progress_target=2,
            progress_unit="volumes",
            xml_name="rest.xml",
            kind=StepKind.VSEND_SCAN,
        )

        with patch(
            "mindfulness_nf.orchestration.executors.vsend.murfi_mod.start",
            side_effect=_fake_start,
        ), patch(
            "mindfulness_nf.orchestration.executors.vsend.murfi_mod.stop",
            AsyncMock(),
        ):
            executor = VsendStepExecutor(
                config=step,
                subject_dir=session_dir,
                pipeline=PipelineConfig(),
                scanner_config=ScannerConfig(),
                scanner_source=NoOpScannerSource(),
            )
            outcome = await asyncio.wait_for(
                executor.run(lambda _p: None), timeout=3.0
            )
            assert outcome.succeeded

    await _run_for_session(ses="loc3", task="rest")
    await _run_for_session(ses="rt15", task="transferpre")

    files = sorted(p.name for p in img_dir.glob("img-*.nii"))
    assert files == [
        "img-rest-01-00001.nii",
        "img-rest-01-00002.nii",
        "img-transferpre-01-00001.nii",
        "img-transferpre-01-00002.nii",
    ], f"got {files}"
    # Content check: rest and transferpre were distinct payloads.
    assert (img_dir / "img-rest-01-00001.nii").read_bytes() == b"FAKE_rest"
    assert (img_dir / "img-transferpre-01-00001.nii").read_bytes() == b"FAKE_transferpre"
