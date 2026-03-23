"""Tests for the SessionSelectScreen.

Tests keypress routing (1-4) and invalid key handling.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from textual.app import App
from textual.widgets import Label

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.tui.screens.session_select import SessionSelectScreen


class SessionSelectTestApp(App[None]):
    CSS_PATH = None  # type: ignore[assignment]

    def __init__(self, tmp_path: Path) -> None:
        super().__init__()
        self.scanner_config = ScannerConfig()
        self.pipeline_config = PipelineConfig()
        self.subjects_dir = tmp_path
        self.template_dir = tmp_path / "template"
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
async def test_session_select_key_1_routes_localizer(tmp_path: Path) -> None:
    app = SessionSelectTestApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        with patch.object(app, "push_screen") as mock_push:
            await pilot.press("1")
            await pilot.pause()
        assert app.session_type == "localizer"
        assert mock_push.called
        from mindfulness_nf.tui.screens.localizer import LocalizerScreen
        assert isinstance(mock_push.call_args[0][0], LocalizerScreen)


@pytest.mark.asyncio
async def test_session_select_key_2_routes_process(tmp_path: Path) -> None:
    app = SessionSelectTestApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        with patch.object(app, "push_screen") as mock_push:
            await pilot.press("2")
            await pilot.pause()
        assert app.session_type == "process"
        assert mock_push.called
        from mindfulness_nf.tui.screens.process import ProcessScreen
        assert isinstance(mock_push.call_args[0][0], ProcessScreen)


@pytest.mark.asyncio
async def test_session_select_key_3_routes_neurofeedback(tmp_path: Path) -> None:
    app = SessionSelectTestApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        with patch.object(app, "push_screen") as mock_push:
            await pilot.press("3")
            await pilot.pause()
        assert app.session_type == "neurofeedback"
        assert mock_push.called
        from mindfulness_nf.tui.screens.neurofeedback import NeurofeedbackScreen
        assert isinstance(mock_push.call_args[0][0], NeurofeedbackScreen)


@pytest.mark.asyncio
async def test_session_select_key_4_routes_test(tmp_path: Path) -> None:
    app = SessionSelectTestApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        with patch.object(app, "push_screen") as mock_push:
            await pilot.press("4")
            await pilot.pause()
        assert app.session_type == "test"
        assert mock_push.called
        from mindfulness_nf.tui.screens.test import TestScreen
        assert isinstance(mock_push.call_args[0][0], TestScreen)


@pytest.mark.asyncio
async def test_session_select_invalid_key_ignored(tmp_path: Path) -> None:
    app = SessionSelectTestApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("5")
        await pilot.pause()
        assert app.session_type == ""
