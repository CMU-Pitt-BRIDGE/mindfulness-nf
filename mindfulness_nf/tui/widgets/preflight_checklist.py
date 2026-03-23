"""Preflight checklist widget showing pass/fail indicators."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from mindfulness_nf.models import CheckResult

_PASS = "[green]\u2713[/green]"
_FAIL = "[red]\u2717[/red]"


class PreflightChecklist(Widget):
    """List of check results with pass/fail indicators.

    Use ``set_results()`` to populate the checklist.
    """

    DEFAULT_CSS = """
    PreflightChecklist {
        height: auto;
        padding: 1 2;
    }
    PreflightChecklist .checklist-item {
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("", id="checklist-content")

    def set_results(self, results: tuple[CheckResult, ...]) -> None:
        """Set the preflight check results to display."""
        lines: list[str] = []
        for result in results:
            icon = _PASS if result.passed else _FAIL
            lines.append(f"{icon}  {result.name}: {result.message}")
        content = self.query_one("#checklist-content", Static)
        content.update("\n".join(lines))
