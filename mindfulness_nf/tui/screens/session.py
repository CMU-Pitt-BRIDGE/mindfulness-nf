"""Unified SessionScreen — consumes a SessionRunner, renders SessionState.

Replaces LocalizerScreen / NeurofeedbackScreen / TestScreen with a single
class parameterized by session type (via the runner's state.session_type).
All keybindings, help bar, recovery flows, and screen lifecycle live here.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Label, Static

from mindfulness_nf.models import (
    Color,
    RunState,
    StepKind,
    StepState,
    StepStatus,
    TrafficLight,
)
from mindfulness_nf.orchestration.session_runner import SessionRunner
from mindfulness_nf.tui.widgets.log_panel import LogPanel
from mindfulness_nf.tui.widgets.preflight_checklist import PreflightChecklist
from mindfulness_nf.tui.widgets.run_progress import RunProgress
from mindfulness_nf.tui.widgets.status_light import StatusLight

__all__ = ["SessionScreen"]

_CHECKMARK = "\u2713"
_ARROW = "\u25b6"
_CIRCLE = "\u25cb"
_XMARK = "\u2717"


def _step_state_to_run_state(step: StepState) -> RunState:
    """Adapter: map persistent ``StepState`` to the ``RunState`` view model."""
    artifacts = step.artifacts or {}
    scale = artifacts.get("scale_factor") if isinstance(artifacts, dict) else None
    return RunState(
        name=step.config.name,
        expected_volumes=step.config.progress_target,
        received_volumes=step.progress_current,
        feedback=step.config.feedback,
        scale_factor=scale if isinstance(scale, (int, float)) else None,
    )


class _ConfirmModal(ModalScreen[bool]):
    """Simple Y/N modal.

    ``y`` dismisses with ``True``; ``n`` or ``escape`` with ``False``.
    """

    BINDINGS = [
        Binding("y", "yes", "Yes", show=True),
        Binding("n", "no", "No", show=True),
        Binding("escape", "no", show=False),
    ]

    DEFAULT_CSS = """
    _ConfirmModal {
        align: center middle;
    }
    _ConfirmModal > Vertical {
        background: $surface;
        border: solid $accent;
        padding: 2 4;
        width: auto;
        height: auto;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._message, id="confirm-message")
            yield Label("[Y]es  [N]o", id="confirm-help")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class SessionScreen(Screen[None]):
    """Single screen for every session type.

    Subscribes to a ``SessionRunner`` and repaints on every state transition.
    Keybindings dispatch to runner intents; all orchestration lives in the
    runner. See spec §Keybindings and §TUI screen lifecycle.
    """

    BINDINGS = [
        Binding("d", "dkey", "Done", show=True),
        Binding("r", "rkey", "Restart", show=True),
        Binding("i", "ikey", "Interrupt", show=True),
        Binding("b", "bkey", "Back", show=True),
        # Arrow keys need priority=True because Textual's scrollable widgets
        # (LogPanel / RichLog) consume arrow keys for scrolling by default.
        Binding("up", "bkey", show=False, priority=True),
        Binding("left", "bkey", show=False, priority=True),
        Binding("n", "nkey", "Next", show=True),
        Binding("down", "nkey", show=False, priority=True),
        Binding("right", "nkey", show=False, priority=True),
        Binding("g", "gkey", "Go to", show=False),
        Binding("m", "mkey", "Relaunch MURFI", show=True),
        Binding("p", "pkey", "Relaunch PsychoPy", show=True),
        Binding("escape", "esckey", "Quit", show=True),
    ]

    DEFAULT_CSS = """
    SessionScreen {
        layout: grid;
        grid-size: 2 2;
        grid-columns: 1fr 2fr;
        grid-rows: auto 1fr;
        padding: 1;
    }
    #session-step-tracker {
        row-span: 2;
        border: solid $accent;
        padding: 1;
        height: 100%;
    }
    #session-status-zone {
        border: solid $accent;
        padding: 1;
        height: auto;
    }
    #session-log-zone {
        height: 1fr;
    }
    #session-help-bar {
        column-span: 2;
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    .title-bar {
        text-style: bold;
    }
    """

    def __init__(self, runner: SessionRunner) -> None:
        super().__init__()
        self._runner = runner

    # ------------------------------------------------------------------
    # Composition & lifecycle
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="session-step-tracker"):
            yield Label(
                self._runner.state.session_type.upper(),
                id="session-title",
                classes="title-bar",
            )
            yield Static("", id="session-steps")
        with Vertical(id="session-status-zone"):
            yield PreflightChecklist(id="session-preflight")
            yield RunProgress(id="session-run-progress")
            yield StatusLight(id="session-status-light")
        with Vertical(id="session-log-zone"):
            yield LogPanel(id="session-log")
        yield Static("", id="session-help-bar")

    def on_mount(self) -> None:
        """Subscribe to the runner and paint initial state."""
        self._runner.subscribe(self._on_state_change)
        self._repaint(self._runner.state)

    async def on_unmount(self) -> None:
        """Critical (spec G7): await stop_current so no task is orphaned."""
        await self._runner.stop_current()

    # ------------------------------------------------------------------
    # Subscriber / render
    # ------------------------------------------------------------------

    def _on_state_change(self, state) -> None:
        """Runner subscriber callback — invoked from asyncio event loop.

        The runner fires subscribers inside ``_apply`` which is already on
        the app's event loop, so we can call ``_render`` directly. Using
        ``app.call_from_thread`` from the same loop would deadlock; using
        ``call_later`` is safe but unnecessary here — the widgets tolerate
        synchronous updates from the loop thread.
        """
        try:
            self._repaint(state)
        except Exception:  # noqa: BLE001 — subscriber bugs must not crash runner
            pass

    def _repaint(self, state) -> None:
        """Repaint all widgets from the fresh state."""
        step = state.current

        # Step tracker sidebar ----------------------------------------
        lines: list[str] = []
        for i, s in enumerate(state.steps):
            marker = self._step_marker(s, is_cursor=i == state.cursor)
            suffix = self._step_suffix(s)
            lines.append(f"  {marker}  {s.config.name}{suffix}")
        try:
            self.query_one("#session-steps", Static).update("\n".join(lines))
        except Exception:
            return  # compose not yet complete

        # Preflight visibility ----------------------------------------
        is_setup = step.config.kind is StepKind.SETUP
        try:
            preflight = self.query_one("#session-preflight", PreflightChecklist)
            preflight.display = is_setup
            progress = self.query_one("#session-run-progress", RunProgress)
            progress.display = not is_setup
            progress.update(_step_state_to_run_state(step))
        except Exception:
            pass

        # Status light -------------------------------------------------
        try:
            status = self.query_one("#session-status-light", StatusLight)
            status.update(self._traffic_light_for(step))
        except Exception:
            pass

        # Help bar -----------------------------------------------------
        try:
            help_bar = self.query_one("#session-help-bar", Static)
            help_bar.update(self._help_text(state))
        except Exception:
            pass

    def _step_marker(self, step: StepState, is_cursor: bool) -> str:
        match step.status:
            case StepStatus.COMPLETED:
                return f"[green]{_CHECKMARK}[/green]"
            case StepStatus.RUNNING:
                return f"[bold]{_ARROW}[/bold]"
            case StepStatus.FAILED:
                return f"[red]{_XMARK}[/red]"
            case StepStatus.PENDING:
                return f"[bold]{_ARROW}[/bold]" if is_cursor else _CIRCLE

    def _step_suffix(self, step: StepState) -> str:
        if step.status is StepStatus.RUNNING and step.config.progress_target:
            return f"  ({step.progress_current}/{step.config.progress_target})"
        if step.status is StepStatus.FAILED and step.error:
            return f"  [red]— {step.error}[/red]"
        return ""

    def _traffic_light_for(self, step: StepState) -> TrafficLight:
        match step.status:
            case StepStatus.COMPLETED:
                return TrafficLight(Color.GREEN, f"{step.config.name} complete")
            case StepStatus.RUNNING:
                msg = f"{step.config.name} running"
                detail = step.detail_message or (
                    f"{step.progress_current}/{step.config.progress_target}"
                    if step.config.progress_target
                    else None
                )
                return TrafficLight(Color.GREEN, msg, detail=detail)
            case StepStatus.FAILED:
                return TrafficLight(
                    Color.RED,
                    f"{step.config.name} FAILED",
                    detail=step.error,
                )
            case StepStatus.PENDING:
                return TrafficLight(
                    Color.GREEN, f"{step.config.name} — press D to start"
                )

    def _help_text(self, state) -> str:
        """Context-sensitive help bar text per spec §Help bar."""
        step = state.current
        running_idx = state.running_index
        parts: list[str] = []

        match step.status:
            case StepStatus.PENDING:
                if running_idx is None:
                    parts.append("[d] Start")
                else:
                    parts.append(
                        "Another step is running — [i] interrupt it first"
                    )
            case StepStatus.RUNNING:
                if step.awaiting_advance:
                    parts.append("[d] Advance phase")
                parts.append("[i] Interrupt")
                components = self._runner.available_components
                if "murfi" in components:
                    parts.append("[m] Relaunch MURFI")
                if "psychopy" in components:
                    parts.append("[p] Relaunch PsychoPy")
            case StepStatus.COMPLETED:
                parts.append("[d] Next")
                parts.append("[r] Re-run (confirms)")
            case StepStatus.FAILED:
                parts.append(
                    "FAILED — [r] redo, [i] clear to pending, [n]/\u2192 move on"
                )

        parts.append("[b/n] Navigate")
        parts.append("[esc] Quit")
        return "  ".join(parts)

    # ------------------------------------------------------------------
    # Keybinding handlers
    # ------------------------------------------------------------------

    async def action_dkey(self) -> None:
        """D: context-dependent per spec §Keybindings."""
        state = self._runner.state
        step = state.current
        match step.status:
            case StepStatus.PENDING:
                await self._runner.start_current()
            case StepStatus.RUNNING:
                if step.awaiting_advance:
                    await self._runner.advance_phase_current()
                else:
                    self._notify(
                        "Running — wait for completion or press I to interrupt"
                    )
            case StepStatus.COMPLETED:
                # advance() auto-chains start_current if new cursor is pending.
                self._runner.advance()
            case StepStatus.FAILED:
                self._notify(
                    "FAILED — press R to redo, I to clear, or N to move on"
                )

    async def action_rkey(self) -> None:
        """R: restart step at cursor (with confirmation on completed)."""
        state = self._runner.state
        step = state.current
        running_idx = state.running_index

        # Refuse if cursor is not the running step AND another step is running.
        if running_idx is not None and running_idx != state.cursor:
            self._notify(
                "Another step is running — interrupt it first or navigate to it"
            )
            return

        if step.status is StepStatus.COMPLETED:
            # Push a modal; when the user dismisses it, _on_restart_confirm
            # either fires the restart or logs a "cancelled" line.
            self.app.push_screen(
                _ConfirmModal("Clear and re-run this completed step? [Y/N]"),
                callback=self._on_restart_confirm,
            )
            return

        await self._runner.clear_and_restart_current()

    def _on_restart_confirm(self, confirmed: bool | None) -> None:
        """Callback for the R-on-completed confirmation modal."""
        if not confirmed:
            self._notify("Restart cancelled")
            return
        # Fire-and-forget: clear_and_restart is async and can be awaited
        # safely inside a task on the app's event loop.
        import asyncio as _asyncio

        _asyncio.create_task(self._runner.clear_and_restart_current())

    async def action_ikey(self) -> None:
        """I: interrupt the RUNNING step (regardless of cursor)."""
        state = self._runner.state
        running_idx = state.running_index
        if running_idx is not None:
            await self._runner.interrupt_current()
            return
        if state.current.status is StepStatus.FAILED:
            # interrupt_current on failed cursor: stop is a no-op, clear files,
            # reset step state to pending.
            await self._runner.interrupt_current()
            return
        self._notify("Nothing to interrupt")

    def action_bkey(self) -> None:
        """B / ↑ / ←: cursor backward, wrapping at index 0 to the last step."""
        state = self._runner.state
        n = len(state.steps)
        if n == 0:
            return
        target = (state.cursor - 1) % n
        self._runner.select(target)

    def action_nkey(self) -> None:
        """N / ↓ / →: cursor forward, wrapping at the last step to index 0."""
        state = self._runner.state
        n = len(state.steps)
        if n == 0:
            return
        target = (state.cursor + 1) % n
        self._runner.select(target)

    async def action_gkey(self) -> None:
        """G: prompt for step number; jump cursor. (Stub — not exercised.)"""
        self._notify("Go-to: not implemented (use b/n to navigate)")

    async def action_mkey(self) -> None:
        await self._relaunch_if_valid("murfi")

    async def action_pkey(self) -> None:
        await self._relaunch_if_valid("psychopy")

    async def _relaunch_if_valid(self, component: str) -> None:
        state = self._runner.state
        if state.current.status is not StepStatus.RUNNING:
            self._notify(
                f"{component.upper()} relaunch only valid during a running step"
            )
            return
        if component not in self._runner.available_components:
            self._notify(f"{component.upper()} not applicable to this step")
            return
        await self._runner.relaunch_component(component)

    async def action_esckey(self) -> None:
        """Esc: prompt when a step is running; else exit."""
        state = self._runner.state
        if state.running_index is not None:
            self.app.push_screen(
                _ConfirmModal("Stop current run and quit? [Y/N]"),
                callback=self._on_quit_confirm,
            )
            return
        self.app.exit()

    def _on_quit_confirm(self, confirmed: bool | None) -> None:
        """Callback for the escape-while-running confirmation modal."""
        if not confirmed:
            self._notify("Quit cancelled")
            return
        import asyncio as _asyncio

        async def _stop_and_exit() -> None:
            await self._runner.stop_current()
            self.app.exit()

        _asyncio.create_task(_stop_and_exit())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _notify(self, message: str) -> None:
        """Append message to the log panel (best-effort)."""
        try:
            log = self.query_one("#session-log", LogPanel)
            log.add_line(message)
        except Exception:
            pass

