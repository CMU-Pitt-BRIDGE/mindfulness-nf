"""Main Textual App class for the mindfulness neurofeedback pipeline.

Imperative shell: imports models from core and widgets from tui/widgets/.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.tui.screens.subject_entry import SubjectEntryScreen


class QuitConfirmScreen(ModalScreen[bool]):
    """Modal dialog confirming quit."""

    DEFAULT_CSS = """
    QuitConfirmScreen {
        align: center middle;
    }
    #quit-dialog {
        width: 50;
        height: auto;
        border: solid $accent;
        padding: 1 2;
        background: $surface;
    }
    #quit-dialog Label {
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }
    #quit-dialog Horizontal {
        align: center middle;
        height: auto;
    }
    #quit-dialog Button {
        margin: 0 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-dialog"):
            yield Label("Are you sure you want to quit?")
            with Horizontal():
                yield Button("Quit", variant="error", id="quit-yes")
                yield Button("Cancel", variant="primary", id="quit-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "quit-yes":
            self.app.exit()
        else:
            self.dismiss(False)


_CSS_PATH = Path(__file__).resolve().parent / "styles" / "app.tcss"


class MindfulnessApp(App[None]):
    """Top-level Textual application for the mindfulness neurofeedback TUI."""

    CSS_PATH = _CSS_PATH
    TITLE = "MINDFULNESS NEUROFEEDBACK"
    BINDINGS = [
        Binding("q", "request_quit", "Quit", show=True),
    ]

    def __init__(
        self,
        *,
        test_mode: bool = False,
        scanner_config: ScannerConfig | None = None,
        pipeline_config: PipelineConfig | None = None,
        subjects_dir: Path | None = None,
        template_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self.test_mode = test_mode
        self.scanner_config = scanner_config or ScannerConfig()
        self.pipeline_config = pipeline_config or PipelineConfig()
        self.subjects_dir = subjects_dir or (
            Path(__file__).resolve().parents[2] / "murfi" / "subjects"
        )
        self.template_dir = template_dir or (
            Path(__file__).resolve().parents[2] / "murfi" / "subjects" / "template"
        )
        self.subject_id: str = ""
        self.session_type: str = ""

    def on_mount(self) -> None:
        """Push the initial screen on mount."""
        self.push_screen(SubjectEntryScreen())

    def action_request_quit(self) -> None:
        """Quit with confirmation dialog."""
        self.push_screen(QuitConfirmScreen())
