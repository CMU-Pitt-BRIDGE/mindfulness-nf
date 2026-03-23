"""Test (dry-run) screen.

Same flow as localizer but with simulated data.
Uses SimulatedMurfi that generates fake volumes on a timer.
PsychoPy is skipped.
"""

from __future__ import annotations

import asyncio
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Label, Static

from mindfulness_nf.models import Color, RunState, TrafficLight
from mindfulness_nf.quality import assess_volume_count
from mindfulness_nf.tui.widgets.log_panel import LogPanel
from mindfulness_nf.tui.widgets.run_progress import RunProgress
from mindfulness_nf.tui.widgets.status_light import StatusLight

_CHECKMARK = "\u2713"
_ARROW = "\u25b6"
_CIRCLE = "\u25cb"


class SimulatedMurfi:
    """Generates fake volume counts on a timer (1 per TR).

    Used by the Test screen for dry-run mode.
    """

    def __init__(self, expected: int, tr: float = 1.2) -> None:
        self.expected = expected
        self.tr = tr
        self.volume_count: int = 0
        self._running: bool = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start generating fake volumes."""
        self._running = True
        self._task = asyncio.create_task(self._generate())

    async def _generate(self) -> None:
        """Generate volumes until expected count reached or stopped."""
        while self._running and self.volume_count < self.expected:
            await asyncio.sleep(self.tr)
            if self._running:
                self.volume_count += 1

    async def stop(self) -> None:
        """Stop generating volumes."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


class TestScreen(Screen[None]):
    """Dry-run test screen with simulated data.

    Same flow as localizer: 4 steps (setup/2vol/rest1/rest2) but
    uses SimulatedMurfi instead of real MURFI and skips PsychoPy.
    """

    BINDINGS = [
        Binding("d", "advance", "Done", show=True),
        Binding("escape", "app.request_quit", "Quit", show=False),
    ]

    DEFAULT_CSS = """
    TestScreen {
        layout: grid;
        grid-size: 2 2;
        grid-columns: 1fr 2fr;
        grid-rows: auto 1fr;
        padding: 1;
    }
    #test-step-tracker {
        row-span: 2;
        border: solid $accent;
        padding: 1;
        height: 100%;
    }
    #test-status-zone {
        border: solid $accent;
        padding: 1;
        height: auto;
    }
    #test-log-zone {
        height: 1fr;
    }
    """

    STEP_NAMES: tuple[str, ...] = ("Setup", "2-volume", "Rest 1", "Rest 2")
    STEP_EXPECTED: tuple[int, ...] = (0, 20, 250, 250)

    def __init__(self) -> None:
        super().__init__()
        self._current_step: int = 0
        self._volumes_received: int = 0
        self._traffic_light: TrafficLight = TrafficLight(Color.GREEN, "Ready")
        self._yellow_confirmed: bool = False
        self._step_completed: tuple[bool, ...] = (False, False, False, False)
        self._sim: SimulatedMurfi | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._polling_paused: bool = False
        self._session_complete: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="test-step-tracker"):
            yield Label("TEST (DRY RUN)", id="test-title", classes="title-bar")
            yield Static("", id="test-steps")
        with Vertical(id="test-status-zone"):
            yield RunProgress(id="test-run-progress")
            yield StatusLight(id="test-status-light")
        with Vertical(id="test-log-zone"):
            yield LogPanel(id="test-log")

    def on_mount(self) -> None:
        """Initialize: skip preflight in test mode, go straight to setup done."""
        self._update_step_tracker()
        status = self.query_one("#test-status-light", StatusLight)
        status.update(TrafficLight(
            Color.GREEN, "Test mode. Press D to start."
        ))
        self.query_one("#test-run-progress", RunProgress).display = False
        log = self.query_one("#test-log", LogPanel)
        log.add_line("Test mode: using simulated data")

    def _update_step_tracker(self) -> None:
        """Render step tracker."""
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
        self.query_one("#test-steps", Static).update("\n".join(lines))

    def action_advance(self) -> None:
        """Handle D keypress."""
        if self._session_complete:
            return
        if self._current_step == 0:
            self._advance_to_next_step()
        else:
            self._handle_scan_advance()

    def _handle_scan_advance(self) -> None:
        """Handle D during a scan step."""
        light = self._traffic_light

        if light.color == Color.RED:
            return

        if light.color == Color.YELLOW:
            if not self._yellow_confirmed:
                self._yellow_confirmed = True
                self._polling_paused = True
                status = self.query_one("#test-status-light", StatusLight)
                status.update(TrafficLight(
                    Color.YELLOW,
                    f"{light.message} Press D again to confirm.",
                ))
                return
            self._yellow_confirmed = False
            self._polling_paused = False

        self.run_worker(self._stop_and_advance(), group="advance")

    async def _stop_and_advance(self) -> None:
        """Stop simulation and advance."""
        if self._sim is not None:
            await self._sim.stop()
            self._sim = None
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        self._advance_to_next_step()

    def _advance_to_next_step(self) -> None:
        """Move to next step."""
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
        self._start_simulated_scan()

    def _start_simulated_scan(self) -> None:
        """Start simulated scan for current step."""
        progress = self.query_one("#test-run-progress", RunProgress)
        progress.display = True

        step = self._current_step
        expected = self.STEP_EXPECTED[step]
        run = RunState(
            name=self.STEP_NAMES[step],
            expected_volumes=expected,
            received_volumes=0,
        )
        progress.update(run)

        status = self.query_one("#test-status-light", StatusLight)
        status.update(TrafficLight(Color.GREEN, "Simulated scan running..."))

        log = self.query_one("#test-log", LogPanel)
        log.add_line(f"Starting simulated {self.STEP_NAMES[step]}...")

        app = self.app
        tr = 1.2
        if hasattr(app, "pipeline_config"):
            tr = app.pipeline_config.tr

        self._sim = SimulatedMurfi(expected, tr=tr)
        self.run_worker(self._run_simulation(step, expected), group="simulation", exclusive=True)

    async def _run_simulation(self, step: int, expected: int) -> None:
        """Run simulation and poll for updates."""
        sim = self._sim
        if sim is None:
            return

        await sim.start()

        while sim._running and sim.volume_count < expected:
            await asyncio.sleep(0.5)
            if self._polling_paused:
                continue
            count = sim.volume_count
            light = assess_volume_count(count, expected)
            self._on_volume_update(count, light)

        # Final update — add "Press D" prompt for the operator
        count = sim.volume_count
        light = assess_volume_count(count, expected)
        if light.color == Color.GREEN:
            light = TrafficLight(Color.GREEN, f"{light.message} Press D to continue.")
        self._on_volume_update(count, light)

    def _on_volume_update(self, count: int, light: TrafficLight) -> None:
        """Update UI with volume count."""
        self._volumes_received = count
        self._traffic_light = light
        self._update_step_tracker()

        step = self._current_step
        run = RunState(
            name=self.STEP_NAMES[step],
            expected_volumes=self.STEP_EXPECTED[step],
            received_volumes=count,
        )
        try:
            self.query_one("#test-run-progress", RunProgress).update(run)
            self.query_one("#test-status-light", StatusLight).update(light)
        except Exception:
            pass

    def _show_complete(self) -> None:
        """Display completion message."""
        self._session_complete = True
        self._update_step_tracker()
        status = self.query_one("#test-status-light", StatusLight)
        status.update(TrafficLight(Color.GREEN, "TEST COMPLETE. Press Escape to exit."))
        log = self.query_one("#test-log", LogPanel)
        log.add_line("TEST COMPLETE")
