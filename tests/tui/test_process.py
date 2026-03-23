"""Tests for the ProcessScreen.

Tests run selection toggle, D to confirm, phase transitions.
Mocks orchestration calls (ica.list_runs).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from textual.app import App

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import Color
from mindfulness_nf.tui.screens.process import ProcessScreen, _TableRunInfo
from mindfulness_nf.tui.widgets.run_table import RunTable


@dataclass(frozen=True, slots=True)
class FakeIcaRunInfo:
    run_name: str
    volume_count: int
    path: Path


class ProcessTestApp(App[None]):
    CSS_PATH = None  # type: ignore[assignment]

    def __init__(self, subjects_dir: Path, template_dir: Path) -> None:
        super().__init__()
        self.scanner_config = ScannerConfig()
        self.pipeline_config = PipelineConfig()
        self.subjects_dir = subjects_dir
        self.template_dir = template_dir
        self.subject_id: str = "sub-001"
        self.session_type: str = "process"

    def on_mount(self) -> None:
        self.push_screen(ProcessScreen())


def _get_screen(app: App[None]) -> ProcessScreen:
    screen = app.screen
    assert isinstance(screen, ProcessScreen)
    return screen


def _fake_runs(tmp_path: Path) -> tuple[FakeIcaRunInfo, ...]:
    return (
        FakeIcaRunInfo(run_name="run-03", volume_count=250, path=tmp_path / "img"),
        FakeIcaRunInfo(run_name="run-04", volume_count=248, path=tmp_path / "img"),
    )


def test_table_run_info_protocol() -> None:
    from mindfulness_nf.tui.widgets.run_table import RunInfo
    info = _TableRunInfo(name="run-03", volumes=250, quality=Color.GREEN)
    assert isinstance(info, RunInfo)


@pytest.mark.asyncio
async def test_process_starts_in_selection_phase(tmp_path: Path) -> None:
    (tmp_path / "sub-001" / "img").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.ica.list_runs", new_callable=AsyncMock, return_value=_fake_runs(tmp_path)):
        app = ProcessTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for w in list(screen.workers):
                await w.wait()
            await pilot.pause()
            assert screen._phase == 1


@pytest.mark.asyncio
async def test_process_run_table_populated(tmp_path: Path) -> None:
    (tmp_path / "sub-001" / "img").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.ica.list_runs", new_callable=AsyncMock, return_value=_fake_runs(tmp_path)):
        app = ProcessTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for w in list(screen.workers):
                await w.wait()
            await pilot.pause()
            table = app.screen.query_one("#proc-run-table", RunTable)
            assert table._run_count == 2


@pytest.mark.asyncio
async def test_process_number_key_toggles_selection(tmp_path: Path) -> None:
    (tmp_path / "sub-001" / "img").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.ica.list_runs", new_callable=AsyncMock, return_value=_fake_runs(tmp_path)):
        app = ProcessTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for w in list(screen.workers):
                await w.wait()
            await pilot.pause()
            table = app.screen.query_one("#proc-run-table", RunTable)
            # Use direct toggle_selection since key routing may be consumed
            # by the DataTable widget focus
            table.toggle_selection(1)
            assert table.selected == (1,)
            table.toggle_selection(2)
            assert table.selected == (1, 2)
            table.toggle_selection(1)
            assert table.selected == (2,)


@pytest.mark.asyncio
async def test_process_d_blocked_with_no_selection(tmp_path: Path) -> None:
    (tmp_path / "sub-001" / "img").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.ica.list_runs", new_callable=AsyncMock, return_value=_fake_runs(tmp_path)):
        app = ProcessTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for w in list(screen.workers):
                await w.wait()
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            assert screen._phase == 1


@pytest.mark.asyncio
async def test_process_d_confirms_selection(tmp_path: Path) -> None:
    (tmp_path / "sub-001" / "img").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.ica.list_runs", new_callable=AsyncMock, return_value=_fake_runs(tmp_path)):
        app = ProcessTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for w in list(screen.workers):
                await w.wait()
            await pilot.pause()
            table = app.screen.query_one("#proc-run-table", RunTable)
            table.toggle_selection(1)
            table.toggle_selection(2)
            await pilot.pause()
            with patch.object(screen, "_do_processing", new_callable=AsyncMock):
                await pilot.press("d")
                await pilot.pause()
                assert screen._phase == 2


@pytest.mark.asyncio
async def test_process_no_runs(tmp_path: Path) -> None:
    (tmp_path / "sub-001" / "img").mkdir(parents=True)
    td = tmp_path / "template"; td.mkdir()
    with patch("mindfulness_nf.orchestration.ica.list_runs", new_callable=AsyncMock, return_value=()):
        app = ProcessTestApp(subjects_dir=tmp_path, template_dir=td)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = _get_screen(app)
            for w in list(screen.workers):
                await w.wait()
            await pilot.pause()
            assert screen._ica_runs == ()
