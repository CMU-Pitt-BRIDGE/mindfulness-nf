"""Tests for the NeurofeedbackScreen.

Tests 12-run sequence tracking, preflight, D-advance behavior.
Mocks orchestration calls (preflight, murfi, psychopy).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from textual.app import App
from textual.widgets import Static

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import (
    CheckResult,
    Color,
    NF_RUN_SEQUENCE,
    TrafficLight,
)
from mindfulness_nf.tui.screens.neurofeedback import (
    NeurofeedbackScreen,
    _PREFLIGHT_STEP,
)


class NeurofeedbackTestApp(App[None]):
    """Test app that immediately pushes NeurofeedbackScreen."""

    CSS_PATH = None  # type: ignore[assignment]

    def __init__(self, subjects_dir: Path, template_dir: Path) -> None:
        super().__init__()
        self.scanner_config = ScannerConfig()
        self.pipeline_config = PipelineConfig()
        self.subjects_dir = subjects_dir
        self.template_dir = template_dir
        self.subject_id: str = "sub-001"
        self.session_type: str = "neurofeedback"

    def on_mount(self) -> None:
        self.push_screen(NeurofeedbackScreen())


def _get_screen(app: App[None]) -> NeurofeedbackScreen:
    screen = app.screen
    assert isinstance(screen, NeurofeedbackScreen)
    return screen


def _all_pass_preflight() -> tuple[CheckResult, ...]:
    return tuple(
        CheckResult(name=f"Check {i}", passed=True, message="OK")
        for i in range(13)
    )


def _some_fail_preflight() -> tuple[CheckResult, ...]:
    results = list(_all_pass_preflight())
    results[0] = CheckResult(name="Check 0", passed=False, message="FAIL")
    return tuple(results)


# ---------------------------------------------------------------------------
# Non-TUI unit tests
# ---------------------------------------------------------------------------


def test_nf_has_12_runs() -> None:
    assert len(NF_RUN_SEQUENCE) == 12


def test_nf_run_names_match_spec() -> None:
    names = [name for name, _ in NF_RUN_SEQUENCE]
    assert names[0] == "Transfer Pre"
    assert names[6] == "Transfer Post"
    assert names[1] == "Feedback 1"
    assert names[11] == "Feedback 10"


def test_nf_feedback_flags() -> None:
    for name, fb in NF_RUN_SEQUENCE:
        if "Transfer" in name:
            assert fb is False
        else:
            assert fb is True


# ---------------------------------------------------------------------------
# TUI tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nf_initial_step_is_preflight(tmp_path: Path) -> None:
    (tmp_path / "sub-001").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.preflight.run_preflight", new_callable=AsyncMock, return_value=_all_pass_preflight()):
        app = NeurofeedbackTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            assert screen._current_run == _PREFLIGHT_STEP


@pytest.mark.asyncio
async def test_nf_d_advances_from_preflight(tmp_path: Path) -> None:
    (tmp_path / "sub-001").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.preflight.run_preflight", new_callable=AsyncMock, return_value=_all_pass_preflight()):
        app = NeurofeedbackTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for w in list(screen.workers):
                await w.wait()
            await pilot.pause()
            assert screen._preflight_passed is True
            with patch.object(screen, "_start_murfi_phase"):
                await pilot.press("d")
                await pilot.pause()
                assert screen._current_run == 0


@pytest.mark.asyncio
async def test_nf_d_blocked_on_failed_preflight(tmp_path: Path) -> None:
    (tmp_path / "sub-001").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.preflight.run_preflight", new_callable=AsyncMock, return_value=_some_fail_preflight()):
        app = NeurofeedbackTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for w in list(screen.workers):
                await w.wait()
            await pilot.pause()
            assert screen._preflight_passed is False
            await pilot.press("d")
            await pilot.pause()
            assert screen._current_run == _PREFLIGHT_STEP


@pytest.mark.asyncio
async def test_nf_tracks_12_runs(tmp_path: Path) -> None:
    (tmp_path / "sub-001").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.preflight.run_preflight", new_callable=AsyncMock, return_value=_all_pass_preflight()):
        app = NeurofeedbackTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            assert len(screen._run_completed) == 12
            assert len(screen._scale_factors) == 12
            assert len(screen._run_names) == 12


@pytest.mark.asyncio
async def test_nf_run_sequence_names(tmp_path: Path) -> None:
    (tmp_path / "sub-001").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.preflight.run_preflight", new_callable=AsyncMock, return_value=_all_pass_preflight()):
        app = NeurofeedbackTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            expected_names = tuple(name for name, _ in NF_RUN_SEQUENCE)
            assert screen._run_names == expected_names


@pytest.mark.asyncio
async def test_nf_d_blocked_during_psychopy(tmp_path: Path) -> None:
    (tmp_path / "sub-001").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.preflight.run_preflight", new_callable=AsyncMock, return_value=_all_pass_preflight()):
        app = NeurofeedbackTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for w in list(screen.workers):
                await w.wait()
            await pilot.pause()
            screen._current_run = 0
            screen._in_psychopy_phase = True
            screen.action_advance()
            assert screen._current_run == 0


@pytest.mark.asyncio
async def test_nf_yellow_requires_double_d(tmp_path: Path) -> None:
    (tmp_path / "sub-001").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.preflight.run_preflight", new_callable=AsyncMock, return_value=_all_pass_preflight()):
        app = NeurofeedbackTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for w in list(screen.workers):
                await w.wait()
            await pilot.pause()
            screen._current_run = 0
            screen._in_psychopy_phase = False
            screen._traffic_light = TrafficLight(Color.YELLOW, "Warning")
            screen._yellow_confirmed = False
            screen.action_advance()
            assert screen._yellow_confirmed is True
            assert screen._current_run == 0


@pytest.mark.asyncio
async def test_nf_step_tracker_shows_preflight(tmp_path: Path) -> None:
    (tmp_path / "sub-001").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.preflight.run_preflight", new_callable=AsyncMock, return_value=_all_pass_preflight()):
        app = NeurofeedbackTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            steps = app.screen.query_one("#nf-steps", Static)
            assert "Preflight" in steps.content
