"""End-to-end integration smoke test for the new SessionRunner-based flow.

Covers the operator path:

    SubjectEntryScreen -> SessionSelectScreen -> SessionScreen

The old per-session screens (``LocalizerScreen``/``NeurofeedbackScreen``/
``TestScreen``) were removed by todo-25 and replaced by the unified
:class:`SessionScreen`, which is what these tests now exercise.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Input

from mindfulness_nf.orchestration.scanner_source import NoOpScannerSource
from mindfulness_nf.tui.app import MindfulnessApp
from mindfulness_nf.tui.screens.session import SessionScreen
from mindfulness_nf.tui.screens.session_select import SessionSelectScreen


def _prime_template(tmp_path: Path) -> Path:
    """Create a minimal ``template/xml/xml_vsend/`` tree under ``tmp_path``."""
    template = tmp_path / "template"
    (template / "xml" / "xml_vsend").mkdir(parents=True, exist_ok=True)
    return template


@pytest.mark.asyncio
async def test_app_starts_with_subject_override_directly_to_session_select(
    tmp_path: Path,
) -> None:
    """With ``subject_override`` set, SubjectEntry is skipped.

    First screen should be :class:`SessionSelectScreen`.
    """
    _prime_template(tmp_path)

    app = MindfulnessApp(
        dry_run=False,
        subject_override="sub-001",
        subjects_dir=tmp_path,
        scanner_source=NoOpScannerSource(),
    )

    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SessionSelectScreen)
        assert app.subject_id == "sub-001"


@pytest.mark.asyncio
async def test_full_app_flow_subject_to_session_screen(tmp_path: Path) -> None:
    """Enter subject, press ``1`` for Localizer; SessionScreen should load.

    Uses :class:`NoOpScannerSource` so no real binaries are invoked.
    """
    _prime_template(tmp_path)

    app = MindfulnessApp(
        dry_run=False,
        subjects_dir=tmp_path,
        scanner_source=NoOpScannerSource(),
    )

    async with app.run_test() as pilot:
        await pilot.pause()

        # Type subject ID and submit.
        input_widget = app.screen.query_one(Input)
        input_widget.focus()
        await pilot.pause()
        await pilot.press(*list("sub-001"))
        await pilot.press("enter")
        await pilot.pause()

        # SessionSelectScreen should now be active.
        assert isinstance(app.screen, SessionSelectScreen)
        assert app.subject_id == "sub-001"

        # Press "1" to pick Localizer.
        await pilot.press("1")
        await pilot.pause()

        # SessionScreen should now be on top; session_type seeded to loc3.
        assert isinstance(app.screen, SessionScreen)
        assert app.session_type == "loc3"
