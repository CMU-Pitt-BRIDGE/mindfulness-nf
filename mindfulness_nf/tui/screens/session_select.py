"""Session selection screen for the mindfulness neurofeedback pipeline.

Displays subject ID and provides 4 session type options via single keypress.
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

    Shows subject ID at top.  Single keypress 1-4 routes to the
    correct screen.  No Enter required.
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
            subject = self.app.subject_id

        with Vertical(id="session-container"):
            yield Label(f"Subject: {subject}", id="session-subject")
            yield Label("Select Session Type", id="session-title")
            yield Static("[bold]1[/bold]  Localizer", classes="session-option")
            yield Static("[bold]2[/bold]  Process", classes="session-option")
            yield Static("[bold]3[/bold]  Neurofeedback", classes="session-option")
            yield Static("[bold]4[/bold]  Test", classes="session-option")

    def on_key(self, event: Key) -> None:
        """Handle single keypress to select session type."""
        key = event.key
        app = self.app

        session_map: dict[str, str] = {
            "1": "localizer",
            "2": "process",
            "3": "neurofeedback",
            "4": "test",
        }

        if key in session_map:
            event.prevent_default()
            event.stop()
            session_type = session_map[key]

            if hasattr(app, "session_type"):
                app.session_type = session_type

            test_mode = getattr(app, "test_mode", False)

            if session_type == "test" or test_mode:
                from mindfulness_nf.tui.screens.test import TestScreen
                self.app.push_screen(TestScreen())
            elif session_type == "localizer":
                from mindfulness_nf.tui.screens.localizer import LocalizerScreen
                self.app.push_screen(LocalizerScreen())
            elif session_type == "process":
                from mindfulness_nf.tui.screens.process import ProcessScreen
                self.app.push_screen(ProcessScreen())
            elif session_type == "neurofeedback":
                from mindfulness_nf.tui.screens.neurofeedback import NeurofeedbackScreen
                self.app.push_screen(NeurofeedbackScreen())
