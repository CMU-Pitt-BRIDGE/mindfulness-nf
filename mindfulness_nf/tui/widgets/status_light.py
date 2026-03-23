"""Traffic light status indicator widget."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label

from mindfulness_nf.models import Color, TrafficLight

_COLOR_MAP: dict[Color, str] = {
    Color.GREEN: "green",
    Color.YELLOW: "yellow",
    Color.RED: "red",
}

_CIRCLE = "\u25cf"  # filled circle


class StatusLight(Widget):
    """Displays a traffic light: colored circle indicator + message text.

    Use ``update()`` to change the displayed state.
    """

    DEFAULT_CSS = """
    StatusLight {
        height: auto;
        padding: 1 2;
    }
    StatusLight .status-light--indicator {
        text-style: bold;
    }
    StatusLight .status-light--message {
        text-style: bold;
        margin-left: 1;
    }
    StatusLight .status-light--detail {
        color: $text-muted;
        margin-left: 3;
    }
    """

    _color: reactive[str] = reactive("green")
    _message: reactive[str] = reactive("")
    _detail: reactive[str] = reactive("")

    def compose(self) -> ComposeResult:
        yield Label(_CIRCLE, id="indicator", classes="status-light--indicator")
        yield Label("", id="message", classes="status-light--message")
        yield Label("", id="detail", classes="status-light--detail")

    def update(self, light: TrafficLight) -> None:
        """Update the status light to reflect the given ``TrafficLight``."""
        self._color = _COLOR_MAP[light.color]
        self._message = light.message
        self._detail = light.detail or ""

    def watch__color(self, value: str) -> None:
        indicator = self.query_one("#indicator", Label)
        indicator.update(f"[{value}]{_CIRCLE}[/{value}]")

    def watch__message(self, value: str) -> None:
        msg_label = self.query_one("#message", Label)
        color = self._color
        msg_label.update(f"[{color}]{value}[/{color}]")

    def watch__detail(self, value: str) -> None:
        detail_label = self.query_one("#detail", Label)
        detail_label.update(value)
        detail_label.display = bool(value)
