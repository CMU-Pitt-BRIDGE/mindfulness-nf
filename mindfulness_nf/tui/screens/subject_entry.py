"""Subject entry screen for the mindfulness neurofeedback pipeline.

Provides a text input for subject ID with validation and directory detection.
"""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Input, Label, Static

_VALID_SUBJECT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


class SubjectEntryScreen(Screen[str]):
    """Input screen for subject ID.

    Validates alphanumeric + hyphens + underscores.  Auto-prepends ``sub-``
    if absent.  Displays "Existing subject" or "New subject" based on
    directory existence.  On Enter, creates directory if new, then pushes
    SessionSelectScreen.
    """

    BINDINGS = [
        Binding("escape", "app.request_quit", "Quit", show=False),
    ]

    DEFAULT_CSS = """
    SubjectEntryScreen {
        align: center middle;
    }
    #subject-container {
        width: 60;
        height: auto;
        border: solid $accent;
        padding: 2 4;
        background: $surface;
    }
    #subject-title {
        text-style: bold;
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }
    #subject-input {
        margin-bottom: 1;
    }
    #subject-status {
        height: auto;
        margin-top: 1;
    }
    #subject-error {
        color: red;
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical
        with Vertical(id="subject-container"):
            yield Label("Enter Subject ID", id="subject-title")
            yield Input(placeholder="e.g. sub-001", id="subject-input")
            yield Static("", id="subject-status")
            yield Static("", id="subject-error")

    def on_input_changed(self, event: Input.Changed) -> None:
        """Validate input and show subject status."""
        raw = event.value.strip()
        error_label = self.query_one("#subject-error", Static)
        status_label = self.query_one("#subject-status", Static)

        if not raw:
            error_label.update("")
            status_label.update("")
            return

        error = validate_subject_id(raw)
        if error:
            error_label.update(f"[red]{error}[/red]")
            status_label.update("")
            return

        error_label.update("")
        subject_id = normalize_subject_id(raw)

        app = self.app
        if hasattr(app, "subjects_dir"):
            from mindfulness_nf.orchestration.subjects import subject_exists
            if subject_exists(app.subjects_dir, subject_id):
                status_label.update(f"[green]Existing subject: {subject_id}[/green]")
            else:
                status_label.update(f"[yellow]New subject: {subject_id}[/yellow]")
        else:
            status_label.update(f"Subject: {subject_id}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter key: validate, create dir if new, push next screen."""
        raw = event.value.strip()
        error_label = self.query_one("#subject-error", Static)

        if not raw:
            error_label.update("[red]Subject ID cannot be empty[/red]")
            return

        error = validate_subject_id(raw)
        if error:
            error_label.update(f"[red]{error}[/red]")
            return

        subject_id = normalize_subject_id(raw)

        app = self.app
        if hasattr(app, "subjects_dir"):
            from mindfulness_nf.orchestration.subjects import (
                create_subject,
                subject_exists,
            )
            if not subject_exists(app.subjects_dir, subject_id):
                try:
                    create_subject(
                        app.subjects_dir, subject_id, app.template_dir
                    )
                except FileExistsError:
                    pass  # Race condition; directory was just created

            app.subject_id = subject_id

            from mindfulness_nf.tui.screens.session_select import SessionSelectScreen
            self.app.push_screen(SessionSelectScreen())


def validate_subject_id(raw: str) -> str | None:
    """Validate a raw subject ID string.

    Returns an error message string if invalid, or None if valid.
    """
    if not raw:
        return "Subject ID cannot be empty"

    # Reject leading dots
    check = raw
    if check.startswith("sub-"):
        check = check[4:]

    if not check:
        return "Subject ID cannot be just 'sub-'"

    if check.startswith("."):
        return "Subject ID cannot start with a dot"

    # Reject spaces
    if " " in raw:
        return "Subject ID cannot contain spaces"

    # Validate the ID part (after potential sub- prefix)
    if not _VALID_SUBJECT_RE.match(check):
        return "Subject ID must be alphanumeric, hyphens, underscores only"

    return None


def normalize_subject_id(raw: str) -> str:
    """Normalize a subject ID: strip whitespace, prepend sub- if absent."""
    raw = raw.strip()
    if not raw.startswith("sub-"):
        raw = f"sub-{raw}"
    return raw
