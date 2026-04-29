"""Session selection screen for the mindfulness neurofeedback pipeline.

Displays subject ID and provides 4 session type options via single keypress.
Each option routes to the unified :class:`SessionScreen`, parameterized by
the ``session_type`` string baked into a freshly-loaded :class:`SessionRunner`.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.screen import Screen
from textual.widgets import Label, Static


class SessionSelectScreen(Screen[str]):
    """Session type selection screen.

    Shows subject ID at top.  Single keypress ``1``-``4`` constructs a
    :class:`SessionRunner` for the chosen session and pushes
    :class:`SessionScreen`.  No Enter required.

    Keymap:

    * ``1`` - Localizer (``loc3``)
    * ``2`` - RT15      (``rt15``)
    * ``3`` - RT30      (``rt30``)
    * ``4`` - Process   (``process``)
    """

    BINDINGS = [
        Binding("escape", "app.request_quit", "Quit", show=False),
    ]

    DEFAULT_CSS = """
    SessionSelectScreen {
        align: center middle;
    }
    #session-container {
        width: 60;
        height: auto;
        border: solid $accent;
        padding: 2 4;
        background: $surface;
    }
    #session-subject {
        text-style: bold;
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }
    #session-title {
        text-align: center;
        width: 100%;
        margin-bottom: 2;
    }
    .session-option {
        margin-bottom: 1;
        padding: 0 2;
    }
    """

    def compose(self) -> ComposeResult:
        subject = ""
        if hasattr(self.app, "subject_id"):
            subject = self.app.subject_id or ""

        with Vertical(id="session-container"):
            yield Label(f"Subject: {subject}", id="session-subject")
            yield Label("Select Session Type", id="session-title")
            yield Static("[bold]1[/bold]  Localizer", classes="session-option")
            yield Static("[bold]2[/bold]  RT15", classes="session-option")
            yield Static("[bold]3[/bold]  RT30", classes="session-option")
            yield Static("[bold]4[/bold]  Process", classes="session-option")

    def on_key(self, event: Key) -> None:
        """Handle single keypress to select session type and push SessionScreen."""
        key = event.key
        app = self.app

        session_map: dict[str, str] = {
            "1": "loc3",
            "2": "rt15",
            "3": "rt30",
            "4": "process",
        }

        if key not in session_map:
            return

        event.prevent_default()
        event.stop()
        session_type = session_map[key]

        if hasattr(app, "session_type"):
            app.session_type = session_type

        # Local imports: keep this screen cheap to import and avoid
        # pulling orchestration/runner deps into TUI startup paths.
        from mindfulness_nf.orchestration.session_runner import SessionRunner
        from mindfulness_nf.orchestration.subjects import (
            bids_session_dir,
            create_subject_session_dir,
        )
        from mindfulness_nf.tui.screens.session import SessionScreen

        subjects_dir = app.subjects_dir
        subject_id = app.subject_id

        session_dir = bids_session_dir(subjects_dir, subject_id, session_type)
        if not session_dir.exists():
            template_dir = getattr(
                app, "template_dir", subjects_dir / "template"
            )
            create_subject_session_dir(
                subjects_dir, subject_id, session_type, template_dir
            )

        runner = SessionRunner.load_or_create(
            subject_dir=session_dir,
            session_type=session_type,
            pipeline=app.pipeline_config,
            scanner_config=app.scanner_config,
            scanner_source=app.scanner_source,
            dry_run=getattr(app, "dry_run", False),
            anchor=getattr(app, "anchor", ""),
        )
        app.push_screen(SessionScreen(runner))
