"""Run progress widget showing name, volume counter, and progress bar."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Label, ProgressBar

from mindfulness_nf.models import RunState

_CHECKMARK = "\u2713"


class RunProgress(Widget):
    """Shows a single run's progress: name, volume counter, progress bar.

    Use ``update()`` to change the displayed state.
    """

    DEFAULT_CSS = """
    RunProgress {
        height: auto;
        padding: 0 1;
        layout: horizontal;
    }
    RunProgress #rp-name {
        width: 20;
    }
    RunProgress #rp-volumes {
        width: 16;
    }
    RunProgress #rp-bar {
        width: 1fr;
    }
    RunProgress #rp-done {
        width: 3;
        text-style: bold;
        color: green;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("", id="rp-name")
        yield Label("", id="rp-volumes")
        yield ProgressBar(total=100, show_percentage=False, show_eta=False, id="rp-bar")
        yield Label("", id="rp-done")

    def update(self, run: RunState) -> None:
        """Update the widget to reflect the given ``RunState``."""
        self.query_one("#rp-name", Label).update(run.name)
        self.query_one("#rp-volumes", Label).update(
            f"{run.received_volumes}/{run.expected_volumes}"
        )
        bar = self.query_one("#rp-bar", ProgressBar)
        bar.update(total=run.expected_volumes, progress=run.received_volumes)
        done_label = self.query_one("#rp-done", Label)
        if run.received_volumes >= run.expected_volumes:
            done_label.update(f"[green]{_CHECKMARK}[/green]")
        else:
            done_label.update("")
