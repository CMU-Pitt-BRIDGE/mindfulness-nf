"""Neurofeedback session screen.

12 runs from NF_RUN_SEQUENCE with preflight before Run 1.
Each run: MURFI phase (D to advance), then PsychoPy phase (auto-runs).
Shows scale factor per completed run.
"""

from __future__ import annotations

from pathlib import Path
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Label, Static

from mindfulness_nf.models import (
    Color,
    NF_RUN_SEQUENCE,
    RunState,
    TrafficLight,
)
from mindfulness_nf.tui.widgets.log_panel import LogPanel
from mindfulness_nf.tui.widgets.preflight_checklist import PreflightChecklist
from mindfulness_nf.tui.widgets.run_progress import RunProgress
from mindfulness_nf.tui.widgets.status_light import StatusLight

_CHECKMARK = "\u2713"
_ARROW = "\u25b6"
_CIRCLE = "\u25cb"

# Sentinel for "preflight" phase before Run 1
_PREFLIGHT_STEP = -1


class NeurofeedbackScreen(Screen[None]):
    """Screen for neurofeedback session with 12 runs.

    Preflight checks run before Run 1.  Each run has a MURFI phase
    (operator presses D) followed by a PsychoPy phase (auto-runs).
    """

    BINDINGS = [
        Binding("d", "advance", "Done", show=True),
        Binding("escape", "app.request_quit", "Quit", show=False),
    ]

    DEFAULT_CSS = """
    NeurofeedbackScreen {
        layout: grid;
        grid-size: 2 2;
        grid-columns: 1fr 2fr;
        grid-rows: auto 1fr;
        padding: 1;
    }
    #nf-step-tracker {
        row-span: 2;
        border: solid $accent;
        padding: 1;
        height: 100%;
    }
    #nf-status-zone {
        border: solid $accent;
        padding: 1;
        height: auto;
    }
    #nf-log-zone {
        height: 1fr;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._run_names: tuple[str, ...] = tuple(
            name for name, _ in NF_RUN_SEQUENCE
        )
        self._run_feedback: tuple[bool, ...] = tuple(
            fb for _, fb in NF_RUN_SEQUENCE
        )
        self._expected_volumes: int = 150  # All NF runs expect 150 volumes
        self._current_run: int = _PREFLIGHT_STEP
        self._volumes_received: int = 0
        self._traffic_light: TrafficLight = TrafficLight(Color.GREEN, "Ready")
        self._yellow_confirmed: bool = False
        self._preflight_passed: bool = False
        self._run_completed: list[bool] = [False] * len(NF_RUN_SEQUENCE)
        self._scale_factors: list[float | None] = [None] * len(NF_RUN_SEQUENCE)
        self._in_psychopy_phase: bool = False
        self._murfi_handle: object | None = None
        self._polling_paused: bool = False
        self._session_complete: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="nf-step-tracker"):
            yield Label("NEUROFEEDBACK", id="nf-title", classes="title-bar")
            yield Static("", id="nf-steps")
        with Vertical(id="nf-status-zone"):
            yield PreflightChecklist(id="nf-preflight")
            yield RunProgress(id="nf-run-progress")
            yield StatusLight(id="nf-status-light")
        with Vertical(id="nf-log-zone"):
            yield LogPanel(id="nf-log")

    def on_mount(self) -> None:
        """Initialize the screen: show preflight, hide scan widgets."""
        self._update_step_tracker()
        self.query_one("#nf-run-progress", RunProgress).display = False
        self.query_one("#nf-status-light", StatusLight).display = False
        self.query_one("#nf-log", LogPanel).display = False
        self._run_preflight()

    def _update_step_tracker(self) -> None:
        """Render the run tracker sidebar."""
        lines: list[str] = []

        # Preflight line
        if self._current_run == _PREFLIGHT_STEP:
            lines.append(f"  [bold]{_ARROW}[/bold]  Preflight")
        elif self._preflight_passed:
            lines.append(f"  [green]{_CHECKMARK}[/green]  Preflight")
        else:
            lines.append(f"  {_CIRCLE}  Preflight")

        # Run lines
        for i, name in enumerate(self._run_names):
            scale_text = ""
            if self._scale_factors[i] is not None:
                scale_text = f"  (sf={self._scale_factors[i]:.1f})"

            if self._run_completed[i]:
                lines.append(f"  [green]{_CHECKMARK}[/green]  {name}{scale_text}")
            elif i == self._current_run:
                vol_text = f"  ({self._volumes_received}/{self._expected_volumes})"
                phase = " [MURFI]" if not self._in_psychopy_phase else " [PsychoPy]"
                lines.append(f"  [bold]{_ARROW}[/bold]  {name}{vol_text}{phase}")
            else:
                lines.append(f"  {_CIRCLE}  {name}")

        self.query_one("#nf-steps", Static).update("\n".join(lines))

    def _run_preflight(self) -> None:
        """Launch preflight checks."""
        self.run_worker(self._do_preflight(), exclusive=True)

    async def _do_preflight(self) -> None:
        """Execute preflight checks and display results."""
        from mindfulness_nf.orchestration.preflight import run_preflight

        app = self.app
        if not hasattr(app, "scanner_config"):
            return

        subject_dir = app.subjects_dir / app.subject_id
        results = await run_preflight(
            app.scanner_config,
            subject_dir=subject_dir if subject_dir.is_dir() else None,
        )

        checklist = self.query_one("#nf-preflight", PreflightChecklist)
        checklist.set_results(results)

        all_passed = all(r.passed for r in results)
        self._preflight_passed = all_passed

        status = self.query_one("#nf-status-light", StatusLight)
        status.display = True
        if all_passed:
            self._traffic_light = TrafficLight(
                Color.GREEN, "All preflight checks passed. Press D to start Run 1."
            )
        else:
            self._traffic_light = TrafficLight(
                Color.RED,
                "Preflight checks failed. Do not proceed. Close this program and report this error.",
            )
        status.update(self._traffic_light)

    def action_advance(self) -> None:
        """Handle D keypress."""
        if self._session_complete or self._in_psychopy_phase:
            return

        if self._current_run == _PREFLIGHT_STEP:
            self._handle_preflight_advance()
        else:
            self._handle_scan_advance()

    def _handle_preflight_advance(self) -> None:
        """Advance from preflight."""
        if not self._preflight_passed:
            return
        self._current_run = 0
        self._volumes_received = 0
        self._yellow_confirmed = False
        self._update_step_tracker()
        self._start_murfi_phase()

    def _handle_scan_advance(self) -> None:
        """Advance from MURFI phase based on traffic light."""
        light = self._traffic_light

        if light.color == Color.RED:
            return

        if light.color == Color.YELLOW:
            if not self._yellow_confirmed:
                self._yellow_confirmed = True
                self._polling_paused = True
                status = self.query_one("#nf-status-light", StatusLight)
                status.update(TrafficLight(
                    Color.YELLOW,
                    f"{light.message} Press D again to confirm.",
                ))
                return
            self._yellow_confirmed = False
            self._polling_paused = False

        # Green or confirmed yellow: stop MURFI, start PsychoPy
        self.run_worker(self._stop_murfi_and_start_psychopy(), exclusive=True)

    def _start_murfi_phase(self) -> None:
        """Show scan widgets and start MURFI for current run."""
        self.query_one("#nf-preflight", PreflightChecklist).display = False
        progress = self.query_one("#nf-run-progress", RunProgress)
        progress.display = True
        status = self.query_one("#nf-status-light", StatusLight)
        status.display = True
        log = self.query_one("#nf-log", LogPanel)
        log.display = True

        run_idx = self._current_run
        run = RunState(
            name=self._run_names[run_idx],
            expected_volumes=self._expected_volumes,
            received_volumes=0,
        )
        progress.update(run)
        status.update(TrafficLight(Color.GREEN, "Waiting for scanner..."))

        self.run_worker(self._do_murfi_phase(run_idx), exclusive=True)

    async def _do_murfi_phase(self, run_idx: int) -> None:
        """Start MURFI and monitor volumes."""
        from mindfulness_nf.orchestration import murfi
        app = self.app
        if not hasattr(app, "scanner_config"):
            return

        subject_dir = app.subjects_dir / app.subject_id
        xml_name = "rtdmn.xml"

        log = self.query_one("#nf-log", LogPanel)
        log.add_line(f"Starting MURFI for {self._run_names[run_idx]}...")

        murfi.configure_moco(subject_dir / "xml" / xml_name, use_moco=True)
        handle = await murfi.start(
            subject_dir, xml_name, app.pipeline_config,
            scanner_config=app.scanner_config,
        )
        self._murfi_handle = handle
        log.add_line("MURFI started")

        await murfi.monitor_volumes(
            handle, self._expected_volumes, self._on_volume_update,
        )

    def _on_volume_update(self, count: int, light: TrafficLight) -> None:
        """Callback from volume monitor."""
        if self._polling_paused:
            return
        self._volumes_received = count
        if light.color == Color.GREEN and count >= self._expected_volumes:
            light = TrafficLight(Color.GREEN, f"{light.message} Press D to continue.")
        self._traffic_light = light
        self._update_step_tracker()

        run_idx = self._current_run
        run = RunState(
            name=self._run_names[run_idx],
            expected_volumes=self._expected_volumes,
            received_volumes=count,
        )
        try:
            self.query_one("#nf-run-progress", RunProgress).update(run)
            self.query_one("#nf-status-light", StatusLight).update(light)
        except Exception:
            pass

    async def _stop_murfi_and_start_psychopy(self) -> None:
        """Stop MURFI, then run PsychoPy phase."""
        if self._murfi_handle is not None:
            from mindfulness_nf.orchestration import murfi
            try:
                await murfi.stop(self._murfi_handle)
            except Exception:
                pass
            self._murfi_handle = None

        self._in_psychopy_phase = True
        self._update_step_tracker()

        run_idx = self._current_run
        log = self.query_one("#nf-log", LogPanel)
        log.add_line(f"Starting PsychoPy for {self._run_names[run_idx]}...")

        status = self.query_one("#nf-status-light", StatusLight)
        status.update(TrafficLight(Color.GREEN, "PsychoPy running... please wait."))

        from mindfulness_nf.orchestration import psychopy as psychopy_mod
        app = self.app
        if not hasattr(app, "scanner_config"):
            return

        feedback = self._run_feedback[run_idx]
        run_number = run_idx + 1

        try:
            process = await psychopy_mod.launch(
                app.subject_id,
                run_number,
                feedback,
            )
            exit_code = await psychopy_mod.wait(process)
            log.add_line(f"PsychoPy finished (exit code {exit_code})")
        except Exception as exc:
            log.add_line(f"PsychoPy error: {exc}")

        # Compute scale factor for feedback runs
        if feedback:
            try:
                data_dir = Path(__file__).resolve().parents[3] / "psychopy" / "balltask" / "data"
                scale = psychopy_mod.get_scale_factor(
                    data_dir, app.subject_id, run_number,
                    default=app.pipeline_config.default_scale_factor,
                )
                self._scale_factors[run_idx] = scale
                log.add_line(f"Scale factor after run {run_number}: {scale:.1f}")
            except Exception:
                pass

        self._in_psychopy_phase = False
        self._advance_run()

    def _advance_run(self) -> None:
        """Mark current run complete and advance to next."""
        self._run_completed[self._current_run] = True

        if self._current_run >= len(self._run_names) - 1:
            self._show_complete()
            return

        self._current_run += 1
        self._volumes_received = 0
        self._yellow_confirmed = False
        self._traffic_light = TrafficLight(Color.GREEN, "Ready")
        self._update_step_tracker()
        self._start_murfi_phase()

    def _show_complete(self) -> None:
        """Display completion message."""
        self._session_complete = True
        self._update_step_tracker()
        status = self.query_one("#nf-status-light", StatusLight)
        status.update(TrafficLight(Color.GREEN, "NEUROFEEDBACK COMPLETE. Press Escape to exit."))
        log = self.query_one("#nf-log", LogPanel)
        log.add_line("NEUROFEEDBACK COMPLETE")
