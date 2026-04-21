"""Tests for the SessionSelectScreen.

Tests keypress routing (1-4) and invalid key handling.  Each key now
routes to the unified :class:`SessionScreen` with a distinct
``session_type`` (``loc3`` / ``rt15`` / ``rt30`` / ``process``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from textual.app import App
from textual.widgets import Label

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.orchestration.scanner_source import NoOpScannerSource
from mindfulness_nf.tui.screens.session_select import SessionSelectScreen


class SessionSelectTestApp(App[None]):
    CSS_PATH = None  # type: ignore[assignment]

    def __init__(self, tmp_path: Path) -> None:
        super().__init__()
        self.scanner_config = ScannerConfig()
        self.pipeline_config = PipelineConfig()
        self.subjects_dir = tmp_path
        self.template_dir = tmp_path / "template"
        self.template_dir.mkdir(exist_ok=True)
        (self.template_dir / "xml" / "xml_vsend").mkdir(
            parents=True, exist_ok=True
        )
        self.scanner_source = NoOpScannerSource()
        self.subject_id: str = "sub-001"
        self.session_type: str = ""

    def on_mount(self) -> None:
        self.push_screen(SessionSelectScreen())


@pytest.mark.asyncio
async def test_session_select_shows_options(tmp_path: Path) -> None:
    app = SessionSelectTestApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        statics = app.screen.query(".session-option")
        assert len(statics) == 4


@pytest.mark.asyncio
async def test_session_select_shows_subject_id(tmp_path: Path) -> None:
    app = SessionSelectTestApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        subject_label = app.screen.query_one("#session-subject", Label)
        assert "sub-001" in subject_label.content


@pytest.mark.asyncio
async def test_session_select_key_1_routes_loc3(tmp_path: Path) -> None:
    app = SessionSelectTestApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        with patch.object(app, "push_screen") as mock_push:
            await pilot.press("1")
            await pilot.pause()
        assert app.session_type == "loc3"
        assert mock_push.called
        from mindfulness_nf.tui.screens.session import SessionScreen
        pushed = mock_push.call_args[0][0]
        assert isinstance(pushed, SessionScreen)
        assert pushed._runner.state.session_type == "loc3"


@pytest.mark.asyncio
async def test_session_select_key_2_routes_rt15(tmp_path: Path) -> None:
    app = SessionSelectTestApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        with patch.object(app, "push_screen") as mock_push:
            await pilot.press("2")
            await pilot.pause()
        assert app.session_type == "rt15"
        assert mock_push.called
        from mindfulness_nf.tui.screens.session import SessionScreen
        pushed = mock_push.call_args[0][0]
        assert isinstance(pushed, SessionScreen)
        assert pushed._runner.state.session_type == "rt15"


@pytest.mark.asyncio
async def test_session_select_key_3_routes_rt30(tmp_path: Path) -> None:
    app = SessionSelectTestApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        with patch.object(app, "push_screen") as mock_push:
            await pilot.press("3")
            await pilot.pause()
        assert app.session_type == "rt30"
        assert mock_push.called
        from mindfulness_nf.tui.screens.session import SessionScreen
        pushed = mock_push.call_args[0][0]
        assert isinstance(pushed, SessionScreen)
        assert pushed._runner.state.session_type == "rt30"


@pytest.mark.asyncio
async def test_session_select_key_4_routes_process(tmp_path: Path) -> None:
    app = SessionSelectTestApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        with patch.object(app, "push_screen") as mock_push:
            await pilot.press("4")
            await pilot.pause()
        assert app.session_type == "process"
        assert mock_push.called
        from mindfulness_nf.tui.screens.session import SessionScreen
        pushed = mock_push.call_args[0][0]
        assert isinstance(pushed, SessionScreen)
        assert pushed._runner.state.session_type == "process"


@pytest.mark.asyncio
async def test_session_select_invalid_key_ignored(tmp_path: Path) -> None:
    app = SessionSelectTestApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("5")
        await pilot.pause()
        assert app.session_type == ""
