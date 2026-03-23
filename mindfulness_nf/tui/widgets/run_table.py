"""Run selection table widget for ICA run selection."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import DataTable

from mindfulness_nf.models import Color

_COLOR_INDICATOR: dict[Color, str] = {
    Color.GREEN: "[green]\u25cf[/green]",
    Color.YELLOW: "[yellow]\u25cf[/yellow]",
    Color.RED: "[red]\u25cf[/red]",
}


@runtime_checkable
class RunInfo(Protocol):
    """Protocol for run information passed to the RunTable."""

    @property
    def name(self) -> str: ...

    @property
    def volumes(self) -> int: ...

    @property
    def quality(self) -> Color: ...


class RunTable(Widget):
    """Table for ICA run selection.

    Columns: #, Run Name, Volumes, Quality (traffic light indicator).
    Rows are selectable by pressing number keys 1-9.
    """

    DEFAULT_CSS = """
    RunTable {
        height: auto;
    }
    RunTable DataTable {
        height: auto;
    }
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._selected: set[int] = set()
        self._run_count: int = 0

    def compose(self) -> ComposeResult:
        table = DataTable(id="run-table")
        table.cursor_type = "row"
        table.add_column("#", key="num")
        table.add_column("Run Name", key="name")
        table.add_column("Volumes", key="volumes")
        table.add_column("Quality", key="quality")
        yield table

    def set_runs(self, runs: tuple[RunInfo, ...]) -> None:
        """Set the runs displayed in the table."""
        table = self.query_one("#run-table", DataTable)
        table.clear()
        self._selected.clear()
        self._run_count = len(runs)
        for i, run in enumerate(runs, start=1):
            table.add_row(
                str(i),
                run.name,
                str(run.volumes),
                _COLOR_INDICATOR[run.quality],
                key=str(i),
            )

    def toggle_selection(self, index: int) -> None:
        """Toggle selection of a run by its 1-based index."""
        if 1 <= index <= self._run_count:
            if index in self._selected:
                self._selected.discard(index)
            else:
                self._selected.add(index)
            self._refresh_selection_display()

    def _refresh_selection_display(self) -> None:
        """Update the visual selection state in the table."""
        table = self.query_one("#run-table", DataTable)
        for i in range(1, self._run_count + 1):
            row_key = str(i)
            prefix = "\u25ba " if i in self._selected else "  "
            table.update_cell(row_key, "num", f"{prefix}{i}")

    @property
    def selected(self) -> tuple[int, ...]:
        """Return the selected 1-based indices as a sorted tuple."""
        return tuple(sorted(self._selected))

    def on_key(self, event: object) -> None:
        """Handle number key presses for selection toggling."""
        # Textual Key events have a .key attribute
        key_str = getattr(event, "key", "")
        if key_str.isdigit():
            num = int(key_str)
            if 1 <= num <= 9:
                self.toggle_selection(num)
