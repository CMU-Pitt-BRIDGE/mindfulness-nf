"""Scrolling log viewer widget."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog


class LogPanel(Widget):
    """Scrolling log viewer (most recent at bottom).

    Use ``add_line()`` to append text. Auto-scrolls to bottom.
    """

    DEFAULT_CSS = """
    LogPanel {
        height: 1fr;
        border: solid $accent;
    }
    LogPanel RichLog {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield RichLog(id="log-output", wrap=True, auto_scroll=True)

    def add_line(self, text: str) -> None:
        """Append a line to the log. Auto-scrolls to bottom."""
        log = self.query_one("#log-output", RichLog)
        log.write(text)
