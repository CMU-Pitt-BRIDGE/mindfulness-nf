"""Tests for the SubjectEntryScreen.

Tests validation, existing/new subject detection, sub- prefix normalization.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from textual.app import App
from textual.widgets import Input, Static

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.tui.screens.subject_entry import (
    SubjectEntryScreen,
    normalize_subject_id,
    validate_subject_id,
)


# ---------------------------------------------------------------------------
# Pure function tests (no TUI needed)
# ---------------------------------------------------------------------------


class TestValidateSubjectId:
    def test_valid_alphanumeric(self) -> None:
        assert validate_subject_id("001") is None

    def test_valid_with_hyphens(self) -> None:
        assert validate_subject_id("sub-001") is None

    def test_valid_with_underscores(self) -> None:
        assert validate_subject_id("sub_001") is None

    def test_valid_mixed(self) -> None:
        assert validate_subject_id("sub-test_01") is None

    def test_rejects_spaces(self) -> None:
        error = validate_subject_id("sub 001")
        assert error is not None
        assert "spaces" in error.lower()

    def test_rejects_leading_dot(self) -> None:
        error = validate_subject_id(".hidden")
        assert error is not None
        assert "dot" in error.lower()

    def test_rejects_leading_dot_with_prefix(self) -> None:
        error = validate_subject_id("sub-.hidden")
        assert error is not None
        assert "dot" in error.lower()

    def test_rejects_special_chars(self) -> None:
        error = validate_subject_id("sub@001")
        assert error is not None

    def test_rejects_empty(self) -> None:
        error = validate_subject_id("")
        assert error is not None
        assert "empty" in error.lower()

    def test_rejects_just_prefix(self) -> None:
        error = validate_subject_id("sub-")
        assert error is not None


class TestNormalizeSubjectId:
    def test_prepends_sub_prefix(self) -> None:
        assert normalize_subject_id("001") == "sub-001"

    def test_keeps_existing_prefix(self) -> None:
        assert normalize_subject_id("sub-001") == "sub-001"

    def test_strips_whitespace(self) -> None:
        assert normalize_subject_id("  001  ") == "sub-001"

    def test_strips_whitespace_with_prefix(self) -> None:
        assert normalize_subject_id("  sub-001  ") == "sub-001"


# ---------------------------------------------------------------------------
# TUI tests
# ---------------------------------------------------------------------------


class SubjectEntryTestApp(App[None]):
    CSS_PATH = None  # type: ignore[assignment]

    def __init__(self, subjects_dir: Path, template_dir: Path) -> None:
        super().__init__()
        self.scanner_config = ScannerConfig()
        self.pipeline_config = PipelineConfig()
        self.subjects_dir = subjects_dir
        self.template_dir = template_dir
        self.subject_id: str = ""
        self.session_type: str = ""

    def on_mount(self) -> None:
        self.push_screen(SubjectEntryScreen())


@pytest.mark.asyncio
async def test_subject_entry_shows_input(tmp_path: Path) -> None:
    td = tmp_path / "template"; td.mkdir()
    app = SubjectEntryTestApp(subjects_dir=tmp_path, template_dir=td)
    async with app.run_test() as pilot:
        await pilot.pause()
        inputs = app.screen.query("Input")
        assert len(inputs) >= 1


@pytest.mark.asyncio
async def test_subject_entry_existing_subject(tmp_path: Path) -> None:
    (tmp_path / "sub-001").mkdir()
    td = tmp_path / "template"; td.mkdir()
    app = SubjectEntryTestApp(subjects_dir=tmp_path, template_dir=td)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.screen.query_one("#subject-input", Input)
        input_widget.value = "sub-001"
        await pilot.pause()
        status = app.screen.query_one("#subject-status", Static)
        assert "existing" in status.content.lower() or "Existing" in status.content


@pytest.mark.asyncio
async def test_subject_entry_new_subject(tmp_path: Path) -> None:
    td = tmp_path / "template"; td.mkdir()
    app = SubjectEntryTestApp(subjects_dir=tmp_path, template_dir=td)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.screen.query_one("#subject-input", Input)
        input_widget.value = "sub-999"
        await pilot.pause()
        status = app.screen.query_one("#subject-status", Static)
        assert "new" in status.content.lower() or "New" in status.content


@pytest.mark.asyncio
async def test_subject_entry_auto_prepend_sub(tmp_path: Path) -> None:
    td = tmp_path / "template"; td.mkdir()
    app = SubjectEntryTestApp(subjects_dir=tmp_path, template_dir=td)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.screen.query_one("#subject-input", Input)
        input_widget.value = "001"
        await pilot.pause()
        status = app.screen.query_one("#subject-status", Static)
        assert "sub-001" in status.content


@pytest.mark.asyncio
async def test_subject_entry_invalid_input_shows_error(tmp_path: Path) -> None:
    td = tmp_path / "template"; td.mkdir()
    app = SubjectEntryTestApp(subjects_dir=tmp_path, template_dir=td)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.screen.query_one("#subject-input", Input)
        input_widget.value = "sub 001"
        await pilot.pause()
        error = app.screen.query_one("#subject-error", Static)
        assert "spaces" in error.content.lower()


@pytest.mark.asyncio
async def test_subject_entry_submit_creates_directory(tmp_path: Path) -> None:
    td = tmp_path / "template"; td.mkdir()
    (td / "xml" / "xml_vsend").mkdir(parents=True)
    app = SubjectEntryTestApp(subjects_dir=tmp_path, template_dir=td)
    async with app.run_test() as pilot:
        await pilot.pause()
        input_widget = app.screen.query_one("#subject-input", Input)
        input_widget.value = "sub-new"
        await pilot.pause()
        with patch.object(app, "push_screen"):
            await pilot.press("enter")
            await pilot.pause()
        assert (tmp_path / "sub-new").is_dir()
        assert app.subject_id == "sub-new"
