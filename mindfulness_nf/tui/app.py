"""Main Textual App class for the mindfulness neurofeedback pipeline.

Imperative shell: selects the scanner source (real vs simulated), wires
orchestration config to the screens, and owns the screen stack lifecycle.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.orchestration.scanner_source import (
    RealScannerSource,
    ScannerSource,
    SimulatedScannerSource,
)
from mindfulness_nf.tui.screens.session_select import SessionSelectScreen
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
_DEFAULT_SUBJECTS_DIR = Path(__file__).resolve().parents[2] / "murfi" / "subjects"
_DEFAULT_DRY_RUN_CACHE = (
    Path(__file__).resolve().parents[2] / "murfi" / "dry_run_cache"
)


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
        dry_run: bool = False,
        subject_override: str | None = None,
        scanner_config: ScannerConfig | None = None,
        pipeline_config: PipelineConfig | None = None,
        subjects_dir: Path | None = None,
        template_dir: Path | None = None,
        dry_run_cache_dir: Path | None = None,
        scanner_source: ScannerSource | None = None,
        anchor: str = "",
    ) -> None:
        super().__init__()
        self.test_mode = test_mode
        self.dry_run = dry_run
        self.subject_override = subject_override
        self.anchor = anchor
        self.scanner_config = scanner_config or ScannerConfig()
        self.pipeline_config = pipeline_config or PipelineConfig()
        self.subjects_dir = subjects_dir or _DEFAULT_SUBJECTS_DIR
        self.template_dir = template_dir or (self.subjects_dir / "template")

        # Pick scanner source based on dry_run (caller may override directly).
        if scanner_source is not None:
            self.scanner_source: ScannerSource = scanner_source
        elif dry_run:
            # dry_run_cache_dir is optional: if the default cache directory
            # exists on disk we prefer it (matches prior recorded-session
            # behaviour); otherwise let SimulatedScannerSource synthesize
            # volumes under an ephemeral tmpdir.
            cache_dir = dry_run_cache_dir
            if cache_dir is None and _DEFAULT_DRY_RUN_CACHE.is_dir():
                cache_dir = _DEFAULT_DRY_RUN_CACHE
            self.scanner_source = SimulatedScannerSource(cache_dir=cache_dir)
        else:
            self.scanner_source = RealScannerSource()

        # If the caller pre-selected a subject, seed subject_id; otherwise
        # the SubjectEntryScreen will populate it.
        self.subject_id: str = subject_override or ""
        self.session_type: str = ""

    def on_mount(self) -> None:
        """Push the initial screen on mount.

        If ``subject_override`` was provided (e.g. ``--subject`` or
        ``--dry-run``), skip :class:`SubjectEntryScreen` and go straight to
        :class:`SessionSelectScreen`.
        """
        if self.subject_override:
            self.push_screen(SessionSelectScreen())
        else:
            self.push_screen(SubjectEntryScreen())

    def action_request_quit(self) -> None:
        """Quit with confirmation dialog."""
        self.push_screen(QuitConfirmScreen())
