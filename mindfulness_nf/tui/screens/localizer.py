"""Localizer session screen.

4 steps: Setup (preflight), 2vol, rest1, rest2.
Uses PreflightChecklist widget for step 1.
Uses RunProgress + StatusLight + LogPanel widgets for steps 2-4.
D keypress: validates volume count, advances if green, requires double-D on
yellow, blocks on red.
"""

from __future__ import annotations

from pathlib import Path
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Label, Static

from mindfulness_nf.models import Color, RunState, TrafficLight
from mindfulness_nf.tui.widgets.log_panel import LogPanel
from mindfulness_nf.tui.widgets.preflight_checklist import PreflightChecklist
from mindfulness_nf.tui.widgets.run_progress import RunProgress
from mindfulness_nf.tui.widgets.status_light import StatusLight

_CHECKMARK = "\u2713"
_ARROW = "\u25b6"
_CIRCLE = "\u25cb"


class LocalizerScreen(Screen[None]):
    """Screen for localizer session with 4 sequential steps.

    Step 0: Preflight checks
    Step 1: 2-volume scan (vSend, expected 20 volumes)
    Step 2: Resting state run 1 (DICOM, expected 250 volumes)
    Step 3: Resting state run 2 (DICOM, expected 250 volumes)
    """

    BINDINGS = [
        Binding("d", "advance", "Done", show=True),
        Binding("escape", "app.request_quit", "Quit", show=False),
    ]

    DEFAULT_CSS = """
    LocalizerScreen {
        layout: grid;
        grid-size: 2 2;
        grid-columns: 1fr 2fr;
        grid-rows: auto 1fr;
        padding: 1;
    }
    #loc-step-tracker {
        row-span: 2;
        border: solid $accent;
        padding: 1;
        height: 100%;
    }
    #loc-status-zone {
        border: solid $accent;
        padding: 1;
        height: auto;
    }
    #loc-log-zone {
        height: 1fr;
    }
    """

    STEP_NAMES: tuple[str, ...] = ("Setup", "2-volume", "Rest 1", "Rest 2")
    STEP_EXPECTED: tuple[int, ...] = (0, 20, 250, 250)
    STEP_XML: tuple[str, ...] = ("", "2vol.xml", "rest.xml", "rest.xml")

    def __init__(self) -> None:
        super().__init__()
        self._current_step: int = 0
        self._volumes_received: int = 0
        self._traffic_light: TrafficLight = TrafficLight(Color.GREEN, "Ready")
        self._yellow_confirmed: bool = False
        self._preflight_passed: bool = False
        self._step_completed: tuple[bool, ...] = (False, False, False, False)
        self._murfi_handle: object | None = None
        self._dicom_handle: object | None = None
        self._monitor_worker: object | None = None
        self._polling_paused: bool = False
        self._session_complete: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="loc-step-tracker"):
            yield Label("LOCALIZER", id="loc-title", classes="title-bar")
            yield Static("", id="loc-steps")
        with Vertical(id="loc-status-zone"):
            yield PreflightChecklist(id="loc-preflight")
            yield RunProgress(id="loc-run-progress")
            yield StatusLight(id="loc-status-light")
        with Vertical(id="loc-log-zone"):
            yield LogPanel(id="loc-log")

    def on_mount(self) -> None:
        """Initialize the screen: show preflight, hide scan widgets."""
        self._update_step_tracker()
        self.query_one("#loc-run-progress", RunProgress).display = False
        self.query_one("#loc-status-light", StatusLight).display = False
        self.query_one("#loc-log", LogPanel).display = False
        self._run_preflight()

    def _update_step_tracker(self) -> None:
        """Render the step tracker sidebar."""
        lines: list[str] = []
        for i, name in enumerate(self.STEP_NAMES):
            if i < self._current_step:
                lines.append(f"  [green]{_CHECKMARK}[/green]  {name}")
            elif i == self._current_step:
                if self._current_step > 0:
                    vol_text = f"  ({self._volumes_received}/{self.STEP_EXPECTED[i]})"
                else:
                    vol_text = ""
                lines.append(f"  [bold]{_ARROW}[/bold]  {name}{vol_text}")
            else:
                lines.append(f"  {_CIRCLE}  {name}")
        self.query_one("#loc-steps", Static).update("\n".join(lines))

    def _run_preflight(self) -> None:
        """Launch preflight checks as a worker."""
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

        checklist = self.query_one("#loc-preflight", PreflightChecklist)
        checklist.set_results(results)

        all_passed = all(r.passed for r in results)
        self._preflight_passed = all_passed

        status = self.query_one("#loc-status-light", StatusLight)
        status.display = True
        if all_passed:
            self._traffic_light = TrafficLight(
                Color.GREEN, "All preflight checks passed. Press D to continue."
            )
        else:
            self._traffic_light = TrafficLight(
                Color.RED,
                "Preflight checks failed. Do not proceed. Close this program and report this error.",
            )
        status.update(self._traffic_light)

    def action_advance(self) -> None:
        """Handle D keypress: validate and potentially advance."""
        if self._session_complete:
            return
        if self._current_step == 0:
            self._handle_preflight_advance()
        else:
            self._handle_scan_advance()

    def _handle_preflight_advance(self) -> None:
        """Advance from preflight step."""
        if not self._preflight_passed:
            return
        self._advance_to_next_step()

    def _handle_scan_advance(self) -> None:
        """Advance from a scan step based on traffic light state."""
        light = self._traffic_light

        if light.color == Color.RED:
            # Blocked
            return

        if light.color == Color.YELLOW:
            if not self._yellow_confirmed:
                self._yellow_confirmed = True
                self._polling_paused = True
                status = self.query_one("#loc-status-light", StatusLight)
                status.update(TrafficLight(
                    Color.YELLOW,
                    f"{light.message} Press D again to confirm.",
                ))
                return
            # Second D on yellow -> advance
            self._yellow_confirmed = False
            self._polling_paused = False

        # Green or confirmed yellow: stop current scan and advance
        self.run_worker(self._stop_current_and_advance(), exclusive=True)

    async def _stop_current_and_advance(self) -> None:
        """Stop MURFI/DICOM and advance to next step."""
        await self._stop_services()
        self._advance_to_next_step()

    async def _stop_services(self) -> None:
        """Stop any running MURFI or DICOM receiver."""
        if self._murfi_handle is not None:
            from mindfulness_nf.orchestration import murfi
            try:
                await murfi.stop(self._murfi_handle)
            except Exception:
                pass
            self._murfi_handle = None

        if self._dicom_handle is not None:
            try:
                await self._dicom_handle.stop()
            except Exception:
                pass
            self._dicom_handle = None

    def _advance_to_next_step(self) -> None:
        """Move to the next step."""
        completed = list(self._step_completed)
        completed[self._current_step] = True
        self._step_completed = tuple(completed)

        if self._current_step >= len(self.STEP_NAMES) - 1:
            self._show_complete()
            return

        self._current_step += 1
        self._volumes_received = 0
        self._yellow_confirmed = False
        self._traffic_light = TrafficLight(Color.GREEN, "Ready")
        self._update_step_tracker()
        self._start_scan_step()

    def _start_scan_step(self) -> None:
        """Start MURFI or DICOM receiver for the current scan step."""
        # Hide preflight, show scan widgets
        self.query_one("#loc-preflight", PreflightChecklist).display = False
        progress = self.query_one("#loc-run-progress", RunProgress)
        progress.display = True
        status = self.query_one("#loc-status-light", StatusLight)
        status.display = True
        log = self.query_one("#loc-log", LogPanel)
        log.display = True

        step = self._current_step
        run = RunState(
            name=self.STEP_NAMES[step],
            expected_volumes=self.STEP_EXPECTED[step],
            received_volumes=0,
        )
        progress.update(run)
        status.update(TrafficLight(Color.GREEN, "Waiting for scanner..."))

        self.run_worker(self._do_scan_step(step), exclusive=True)

    async def _do_scan_step(self, step: int) -> None:
        """Run a scan step: start services, monitor volumes."""
        app = self.app
        if not hasattr(app, "scanner_config"):
            return

        subject_dir = app.subjects_dir / app.subject_id
        xml_name = self.STEP_XML[step]
        expected = self.STEP_EXPECTED[step]

        log = self.query_one("#loc-log", LogPanel)
        log.add_line(f"Starting {self.STEP_NAMES[step]}...")

        if step == 1:
            # 2-volume: use MURFI via vSend
            from mindfulness_nf.orchestration import murfi
            murfi.configure_moco(subject_dir / "xml" / xml_name, use_moco=True)
            handle = await murfi.start(
                subject_dir, xml_name, app.pipeline_config,
                scanner_config=app.scanner_config,
            )
            self._murfi_handle = handle
            log.add_line("MURFI started for 2-volume scan")

            # Monitor volumes
            await murfi.monitor_volumes(
                handle, expected, self._on_volume_update,
            )
        else:
            # Resting state: use DICOM receiver
            from mindfulness_nf.orchestration import murfi
            from mindfulness_nf.orchestration.dicom_receiver import DicomReceiver

            # Start MURFI for processing
            murfi.configure_moco(subject_dir / "xml" / xml_name, use_moco=False)
            murfi_handle = await murfi.start(
                subject_dir, xml_name, app.pipeline_config,
                scanner_config=app.scanner_config,
            )
            self._murfi_handle = murfi_handle

            # Start DICOM receiver
            dicom_dir = subject_dir / "img"
            dicom = await DicomReceiver.start(
                dicom_dir,
                port=app.scanner_config.dicom_port,
                ae_title=app.scanner_config.dicom_ae_title,
            )
            self._dicom_handle = dicom
            log.add_line("DICOM receiver started for resting state scan")

            # Monitor via MURFI log
            await murfi.monitor_volumes(
                murfi_handle, expected, self._on_volume_update,
            )

    def _on_volume_update(self, count: int, light: TrafficLight) -> None:
        """Callback from volume monitor: update UI."""
        if self._polling_paused:
            return
        self._volumes_received = count
        if light.color == Color.GREEN and count >= self.STEP_EXPECTED[self._current_step]:
            light = TrafficLight(Color.GREEN, f"{light.message} Press D to continue.")
        self._traffic_light = light
        self._update_step_tracker()

        step = self._current_step
        run = RunState(
            name=self.STEP_NAMES[step],
            expected_volumes=self.STEP_EXPECTED[step],
            received_volumes=count,
        )
        try:
            self.query_one("#loc-run-progress", RunProgress).update(run)
            self.query_one("#loc-status-light", StatusLight).update(light)
        except Exception:
            pass  # Screen may have been dismissed

    def _show_complete(self) -> None:
        """Display completion message."""
        self._session_complete = True
        self._update_step_tracker()
        status = self.query_one("#loc-status-light", StatusLight)
        status.update(TrafficLight(
            Color.GREEN, "LOCALIZER COMPLETE. Press Escape to exit."
        ))
        log = self.query_one("#loc-log", LogPanel)
        log.add_line("LOCALIZER COMPLETE")
