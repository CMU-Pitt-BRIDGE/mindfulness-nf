"""End-to-end integration smoke tests.

Launches MindfulnessApp with Textual's run_test() pilot,
walks through subject entry -> session select -> localizer,
and verifies screen transitions and quit confirmation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from textual.widgets import Input

from mindfulness_nf.models import CheckResult
from mindfulness_nf.tui.app import MindfulnessApp
from mindfulness_nf.tui.screens.session_select import SessionSelectScreen
from mindfulness_nf.tui.screens.subject_entry import SubjectEntryScreen
from mindfulness_nf.tui.screens.test import TestScreen


def _all_pass_preflight() -> list[CheckResult]:
    return [
        CheckResult(name=f"Check {i}", passed=True, message="OK")
        for i in range(13)
    ]


@pytest.mark.asyncio
async def test_full_localizer_flow(tmp_path: Path) -> None:
    """Smoke test: subject entry -> session select -> localizer screen."""
    subjects_dir = tmp_path / "subjects"
    subjects_dir.mkdir()
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    (template_dir / "xml" / "xml_vsend").mkdir(parents=True)

    app = MindfulnessApp(
        test_mode=True,
        subjects_dir=subjects_dir,
        template_dir=template_dir,
    )

    mock_preflight = AsyncMock(return_value=_all_pass_preflight())

    with patch(
        "mindfulness_nf.orchestration.preflight.run_preflight",
        mock_preflight,
    ):
        async with app.run_test() as pilot:
            # Step 1: SubjectEntryScreen should be active
            await pilot.pause()
            assert isinstance(app.screen, SubjectEntryScreen)

            # Type a subject ID into the input
            input_widget = app.screen.query_one("#subject-input", Input)
            input_widget.value = "sub-int01"
            await pilot.pause()

            # Press Enter to submit
            await pilot.press("enter")
            await pilot.pause()

            # Step 2: Should have transitioned to SessionSelectScreen
            assert isinstance(app.screen, SessionSelectScreen)
            assert app.subject_id == "sub-int01"

            # Subject directory should have been created
            assert (subjects_dir / "sub-int01").is_dir()

            # Press "1" for localizer
            await pilot.press("1")
            await pilot.pause()

            # Step 3: In test_mode, all sessions route to TestScreen
            assert isinstance(app.screen, TestScreen)
            assert app.session_type == "localizer"


@pytest.mark.asyncio
async def test_quit_confirmation(tmp_path: Path) -> None:
    """Smoke test: Escape opens quit dialog, cancel dismisses it."""
    subjects_dir = tmp_path / "subjects"
    subjects_dir.mkdir()
    template_dir = tmp_path / "template"
    template_dir.mkdir()

    app = MindfulnessApp(
        test_mode=True,
        subjects_dir=subjects_dir,
        template_dir=template_dir,
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SubjectEntryScreen)

        # Press Escape to trigger quit confirmation
        # (SubjectEntryScreen binds escape -> app.request_quit)
        await pilot.press("escape")
        await pilot.pause()

        # QuitConfirmScreen should be on the stack
        from mindfulness_nf.tui.app import QuitConfirmScreen

        assert isinstance(app.screen, QuitConfirmScreen)

        # Click Cancel to dismiss
        await pilot.click("#quit-no")
        await pilot.pause()

        # Should be back on SubjectEntryScreen
        assert isinstance(app.screen, SubjectEntryScreen)


@pytest.mark.asyncio
async def test_quit_confirm_exits(tmp_path: Path) -> None:
    """Smoke test: Escape then Quit button exits the app."""
    subjects_dir = tmp_path / "subjects"
    subjects_dir.mkdir()
    template_dir = tmp_path / "template"
    template_dir.mkdir()

    app = MindfulnessApp(
        test_mode=True,
        subjects_dir=subjects_dir,
        template_dir=template_dir,
    )

    async with app.run_test() as pilot:
        await pilot.pause()

        # Press Escape to trigger quit confirmation
        await pilot.press("escape")
        await pilot.pause()

        # Click Quit to confirm exit
        await pilot.click("#quit-yes")
        await pilot.pause()
