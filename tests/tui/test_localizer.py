"""Tests for the LocalizerScreen.

Tests D-advance behavior on green/yellow/red states.
Mocks orchestration calls (preflight, murfi, dicom_receiver).
Tests screen behavior (keypresses, state transitions), not orchestration logic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from textual.app import App
from textual.widgets import Static

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import CheckResult, Color, TrafficLight
from mindfulness_nf.tui.screens.localizer import LocalizerScreen
from mindfulness_nf.tui.widgets.status_light import StatusLight


class LocalizerTestApp(App[None]):
    """Test app that immediately pushes LocalizerScreen."""

    CSS_PATH = None  # type: ignore[assignment]

    def __init__(self, subjects_dir: Path, template_dir: Path) -> None:
        super().__init__()
        self.scanner_config = ScannerConfig()
        self.pipeline_config = PipelineConfig()
        self.subjects_dir = subjects_dir
        self.template_dir = template_dir
        self.subject_id: str = "sub-001"
        self.session_type: str = "localizer"

    def on_mount(self) -> None:
        self.push_screen(LocalizerScreen())


def _get_screen(app: App[None]) -> LocalizerScreen:
    screen = app.screen
    assert isinstance(screen, LocalizerScreen)
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


def test_localizer_has_4_steps() -> None:
    """LocalizerScreen defines exactly 4 steps."""
    assert len(LocalizerScreen.STEP_NAMES) == 4
    assert len(LocalizerScreen.STEP_EXPECTED) == 4


def test_localizer_step_names() -> None:
    """Localizer steps are named correctly."""
    assert LocalizerScreen.STEP_NAMES == ("Setup", "2-volume", "Rest 1", "Rest 2")


def test_localizer_expected_volumes() -> None:
    """Localizer expected volume counts match spec."""
    assert LocalizerScreen.STEP_EXPECTED == (0, 20, 250, 250)


# ---------------------------------------------------------------------------
# TUI tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_localizer_initial_step_is_preflight(tmp_path: Path) -> None:
    """LocalizerScreen starts at step 0 (preflight)."""
    (tmp_path / "sub-001").mkdir(parents=True)
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    with patch(
        "mindfulness_nf.orchestration.preflight.run_preflight",
        new_callable=AsyncMock,
        return_value=_all_pass_preflight(),
    ):
        app = LocalizerTestApp(subjects_dir=tmp_path, template_dir=template_dir)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            assert screen._current_step == 0


@pytest.mark.asyncio
async def test_localizer_d_advances_on_green_preflight(tmp_path: Path) -> None:
    """Pressing D with green preflight advances to step 1."""
    (tmp_path / "sub-001").mkdir(parents=True)
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    with patch(
        "mindfulness_nf.orchestration.preflight.run_preflight",
        new_callable=AsyncMock,
        return_value=_all_pass_preflight(),
    ):
        app = LocalizerTestApp(subjects_dir=tmp_path, template_dir=template_dir)
        async with app.run_test() as pilot:
            await pilot.pause()

            screen = _get_screen(app)
            for worker in list(screen.workers):
                await worker.wait()
            await pilot.pause()

            assert screen._preflight_passed is True

            with patch.object(screen, "_start_scan_step"):
                await pilot.press("d")
                await pilot.pause()
                assert screen._current_step == 1


@pytest.mark.asyncio
async def test_localizer_d_blocked_on_red_preflight(tmp_path: Path) -> None:
    """Pressing D with failed preflight does not advance."""
    (tmp_path / "sub-001").mkdir(parents=True)
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    with patch(
        "mindfulness_nf.orchestration.preflight.run_preflight",
        new_callable=AsyncMock,
        return_value=_some_fail_preflight(),
    ):
        app = LocalizerTestApp(subjects_dir=tmp_path, template_dir=template_dir)
        async with app.run_test() as pilot:
            await pilot.pause()

            screen = _get_screen(app)
            for worker in list(screen.workers):
                await worker.wait()
            await pilot.pause()

            assert screen._preflight_passed is False

            await pilot.press("d")
            await pilot.pause()
            assert screen._current_step == 0


@pytest.mark.asyncio
async def test_localizer_d_advances_on_green_scan(tmp_path: Path) -> None:
    """D advances from a scan step when traffic light is green."""
    (tmp_path / "sub-001").mkdir(parents=True)
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    with patch(
        "mindfulness_nf.orchestration.preflight.run_preflight",
        new_callable=AsyncMock,
        return_value=_all_pass_preflight(),
    ):
        app = LocalizerTestApp(subjects_dir=tmp_path, template_dir=template_dir)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for worker in list(screen.workers):
                await worker.wait()
            await pilot.pause()

            # Manually set up: step 1 with green
            screen._current_step = 1
            screen._traffic_light = TrafficLight(Color.GREEN, "OK")

            with patch.object(screen, "_stop_services", new_callable=AsyncMock):
                with patch.object(screen, "_start_scan_step"):
                    await pilot.press("d")
                    for worker in list(screen.workers):
                        await worker.wait()
                    await pilot.pause()
                    assert screen._current_step >= 2


@pytest.mark.asyncio
async def test_localizer_d_requires_double_on_yellow(tmp_path: Path) -> None:
    """D on yellow requires a second D to confirm."""
    (tmp_path / "sub-001").mkdir(parents=True)
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    with patch(
        "mindfulness_nf.orchestration.preflight.run_preflight",
        new_callable=AsyncMock,
        return_value=_all_pass_preflight(),
    ):
        app = LocalizerTestApp(subjects_dir=tmp_path, template_dir=template_dir)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for worker in list(screen.workers):
                await worker.wait()
            await pilot.pause()

            screen._current_step = 1
            screen._traffic_light = TrafficLight(Color.YELLOW, "Warning")
            screen._yellow_confirmed = False

            screen.action_advance()
            assert screen._yellow_confirmed is True
            assert screen._current_step == 1


@pytest.mark.asyncio
async def test_localizer_d_blocked_on_red_scan(tmp_path: Path) -> None:
    """D on red during scan step does not advance."""
    (tmp_path / "sub-001").mkdir(parents=True)
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    with patch(
        "mindfulness_nf.orchestration.preflight.run_preflight",
        new_callable=AsyncMock,
        return_value=_all_pass_preflight(),
    ):
        app = LocalizerTestApp(subjects_dir=tmp_path, template_dir=template_dir)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for worker in list(screen.workers):
                await worker.wait()
            await pilot.pause()

            screen._current_step = 1
            screen._traffic_light = TrafficLight(Color.RED, "Critical failure")

            screen.action_advance()
            assert screen._current_step == 1


@pytest.mark.asyncio
async def test_localizer_step_tracker_renders(tmp_path: Path) -> None:
    """Step tracker renders with arrow for current step."""
    (tmp_path / "sub-001").mkdir(parents=True)
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    with patch(
        "mindfulness_nf.orchestration.preflight.run_preflight",
        new_callable=AsyncMock,
        return_value=_all_pass_preflight(),
    ):
        app = LocalizerTestApp(subjects_dir=tmp_path, template_dir=template_dir)
        async with app.run_test() as pilot:
            await pilot.pause()
            steps_static = app.screen.query_one("#loc-steps", Static)
            assert "\u25b6" in steps_static.content
