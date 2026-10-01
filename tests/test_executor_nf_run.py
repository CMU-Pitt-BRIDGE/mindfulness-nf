"""Tests for :class:`NfRunStepExecutor` volume accounting around the phase gate.

MURFI and PsychoPy launches are mocked; the executor reads a real on-disk
MURFI log, so volume counting runs exactly as in a session.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import StepConfig, StepKind
from mindfulness_nf.orchestration.executor import StepProgress
from mindfulness_nf.orchestration.executors.nf_run import NfRunStepExecutor
from mindfulness_nf.orchestration.murfi import MurfiProcess
from mindfulness_nf.orchestration.scanner_source import NoOpScannerSource

_MOD = "mindfulness_nf.orchestration.executors.nf_run"
_READY = "listening for images on port 50000\n"
_VOLUME = "received image from scanner: series 2 acquisition {n}\n"


def _step() -> StepConfig:
    return StepConfig(
        name="Feedback 1",
        task="feedback",
        run=1,
        progress_target=150,
        progress_unit="volumes",
        xml_name="rtdmn.xml",
        kind=StepKind.NF_RUN,
        feedback=True,
    )


def _session(tmp_path: Path) -> tuple[Path, Path]:
    session_dir = tmp_path / "sub-001" / "ses-rt15"
    (session_dir / "log").mkdir(parents=True)
    (tmp_path / "sub-001" / "img").mkdir()
    log_path = session_dir / "log" / "murfi_rtdmn_feedback-01.log"
    log_path.write_text(_READY)
    return session_dir, log_path


def _proc(returncode: int | None) -> MagicMock:
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode
    proc.pid = 4242
    return proc


def _append_volumes(log_path: Path, first: int, count: int) -> None:
    with log_path.open("a") as fh:
        for n in range(first, first + count):
            fh.write(_VOLUME.format(n=n))


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


def _executor(session_dir: Path, source: NoOpScannerSource) -> NfRunStepExecutor:
    return NfRunStepExecutor(
        config=_step(),
        subject_dir=session_dir,
        pipeline=PipelineConfig(),
        scanner_config=ScannerConfig(),
        scanner_source=source,
    )


@pytest.mark.asyncio
async def test_gate_reports_volumes_that_arrive_before_d(tmp_path: Path) -> None:
    session_dir, log_path = _session(tmp_path)
    murfi = MurfiProcess(process=_proc(None), log_path=log_path, xml_name="rtdmn.xml")
    updates: list[StepProgress] = []

    with patch(f"{_MOD}.murfi_mod.start", AsyncMock(return_value=murfi)), patch(
        f"{_MOD}.murfi_mod.stop", AsyncMock()
    ):
        ex = _executor(session_dir, NoOpScannerSource())
        task = asyncio.create_task(ex.run(updates.append))
        await _wait_for(lambda: any(u.awaiting_advance for u in updates))
        _append_volumes(log_path, 1, 3)
        await _wait_for(lambda: updates[-1].value == 3)
        await ex.stop()
        outcome = await asyncio.wait_for(task, timeout=3.0)

    gate = updates[-1]
    assert gate.awaiting_advance is True
    assert gate.value == 3
    assert "scanner is sending (3 volumes received)" in (gate.detail or "")
    assert outcome.error == "cancelled"


@pytest.mark.asyncio
async def test_murfi_exit_at_gate_fails_step(tmp_path: Path) -> None:
    session_dir, log_path = _session(tmp_path)
    proc = _proc(None)
    murfi = MurfiProcess(process=proc, log_path=log_path, xml_name="rtdmn.xml")
    updates: list[StepProgress] = []

    with patch(f"{_MOD}.murfi_mod.start", AsyncMock(return_value=murfi)), patch(
        f"{_MOD}.murfi_mod.stop", AsyncMock()
    ):
        ex = _executor(session_dir, NoOpScannerSource())
        task = asyncio.create_task(ex.run(updates.append))
        await _wait_for(lambda: any(u.awaiting_advance for u in updates))
        proc.returncode = -6
        outcome = await asyncio.wait_for(task, timeout=3.0)

    assert outcome.succeeded is False
    assert outcome.error == "MURFI exited -6 before PsychoPy launched"


@pytest.mark.asyncio
async def test_psychopy_success_with_zero_volumes_fails_step(tmp_path: Path) -> None:
    session_dir, log_path = _session(tmp_path)
    murfi = MurfiProcess(process=_proc(None), log_path=log_path, xml_name="rtdmn.xml")
    updates: list[StepProgress] = []

    with patch(f"{_MOD}.murfi_mod.start", AsyncMock(return_value=murfi)), patch(
        f"{_MOD}.murfi_mod.stop", AsyncMock()
    ), patch(f"{_MOD}.psychopy_mod.launch", AsyncMock(return_value=_proc(0))):
        ex = _executor(session_dir, NoOpScannerSource())
        task = asyncio.create_task(ex.run(updates.append))
        await _wait_for(lambda: any(u.awaiting_advance for u in updates))
        ex.advance_phase()
        outcome = await asyncio.wait_for(task, timeout=3.0)

    assert outcome.succeeded is False
    assert outcome.error is not None
    assert "MURFI received 0 scanner volumes" in outcome.error


@pytest.mark.asyncio
async def test_psychopy_success_counts_volumes_from_before_and_after_d(
    tmp_path: Path,
) -> None:
    session_dir, log_path = _session(tmp_path)
    murfi = MurfiProcess(process=_proc(None), log_path=log_path, xml_name="rtdmn.xml")
    psychopy = _proc(None)
    source = NoOpScannerSource()
    updates: list[StepProgress] = []

    with patch(f"{_MOD}.murfi_mod.start", AsyncMock(return_value=murfi)), patch(
        f"{_MOD}.murfi_mod.stop", AsyncMock()
    ), patch(f"{_MOD}.psychopy_mod.launch", AsyncMock(return_value=psychopy)):
        ex = _executor(session_dir, source)
        task = asyncio.create_task(ex.run(updates.append))
        await _wait_for(lambda: any(u.awaiting_advance for u in updates))
        _append_volumes(log_path, 1, 2)
        await _wait_for(lambda: updates[-1].value == 2)
        ex.advance_phase()
        await _wait_for(lambda: updates[-1].phase == "psychopy")
        _append_volumes(log_path, 3, 148)
        await _wait_for(lambda: any(u.value == 150 for u in updates))
        psychopy.returncode = 0
        outcome = await asyncio.wait_for(task, timeout=3.0)

    assert outcome.succeeded is True
    assert outcome.error is None
    assert len(source.push_vsend_calls) == 1
