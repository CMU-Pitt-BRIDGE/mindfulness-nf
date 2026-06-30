"""Test doubles for SessionRunner / end-to-end tests.

These fakes satisfy the ``StepExecutor`` Protocol (and the MURFI / PsychoPy
process surfaces) without ever launching real subprocesses.  They are the
sole mechanism by which ``tests/test_session_runner.py`` (L2) and
``tests/test_e2e_session.py`` (L3, arriving in todo-20) exercise the
runner's lifecycle, supervisor, and persistence logic deterministically.

Design notes:

* ``FakeStepExecutor`` is the critical one.  It implements the
  ``StepExecutor`` Protocol (``async run``, ``async stop``, ``async
  relaunch``, ``components()``, ``advance_phase()``) and exposes a
  ``simulate_*`` test-hook API so tests can drive the executor's lifecycle
  synchronously.  The test asserts on observable behaviour (state
  transitions, persisted JSON) — these hooks merely produce the events a
  real subprocess would emit.

* ``FakeMurfiProcess`` and ``FakePsychoPyProcess`` are stubs with enough
  surface area to let L3 tests be written in todo-20.  They do basic
  file I/O (writing MURFI-log lines / a PsychoPy CSV) but do not spawn
  subprocesses.  They will be fleshed out incrementally when the L3
  tests actually need more realism.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mindfulness_nf.orchestration.executor import (
    Component,
    ProgressCallback,
    StepOutcome,
    StepProgress,
)


# ---------------------------------------------------------------------------
# FakeStepExecutor — drives the runner's supervisor from the test side
# ---------------------------------------------------------------------------


class FakeStepExecutor:
    """A ``StepExecutor`` whose run-loop is driven entirely from test code.

    The fake's ``run()`` awaits an internal ``Future`` that the tests
    resolve via ``simulate_completion`` / ``simulate_crash`` /
    ``simulate_programming_error``.  Progress is pushed via
    ``simulate_volume`` and ``simulate_phase_gate``.

    Recorded activity (for assertions):
        * ``relaunch_calls``  — components passed to ``relaunch()``
        * ``stop_calls``      — count of ``stop()`` invocations
        * ``advance_phase_calls`` — count of ``advance_phase()`` invocations

    Constructor knobs:
        * ``components``     — what ``components()`` returns (default
                                ``("murfi",)``)
        * ``progress_target`` — target used in emitted ``StepProgress``
        * ``progress_unit``  — unit used in emitted ``StepProgress``
    """

    def __init__(
        self,
        components: tuple[Component, ...] = ("murfi",),
        progress_target: int = 150,
        progress_unit: str = "volumes",
    ) -> None:
        self._components: tuple[Component, ...] = components
        self._progress_target = progress_target
        self._progress_unit = progress_unit

        # Lifecycle state.
        self._run_future: asyncio.Future[StepOutcome] | None = None
        self._on_progress: ProgressCallback | None = None
        self._last_progress = StepProgress(
            value=0,
            target=progress_target,
            unit=progress_unit,  # type: ignore[arg-type]
        )

        # Pending test-driven state — applied when run() is next awaited
        # or when the relevant simulate_* is called after run() started.
        self._buffered_progress: list[StepProgress] = []
        self._pending_artifacts: dict[str, Any] | None = None
        self._pending_error: BaseException | None = None

        # Assertion-facing counters / logs.
        self.relaunch_calls: list[str] = []
        self.stop_calls: int = 0
        self.advance_phase_calls: int = 0

    # ------------------------------------------------------------------
    # StepExecutor Protocol
    # ------------------------------------------------------------------

    async def run(self, on_progress: ProgressCallback) -> StepOutcome:
        """Execute the fake step: wait on an internal Future driven by tests."""
        self._on_progress = on_progress
        loop = asyncio.get_running_loop()
        self._run_future = loop.create_future()

        # Drain any progress snapshots buffered before run() was awaited.
        for snapshot in self._buffered_progress:
            on_progress(snapshot)
        self._buffered_progress.clear()

        # If a programming error was pre-registered, raise now (propagates
        # to the runner's supervisor as an unhandled exception).
        if self._pending_error is not None:
            err = self._pending_error
            self._pending_error = None
            raise err

        return await self._run_future

    async def stop(self, timeout: float = 5.0) -> None:
        """Record and resolve run() with a cancelled outcome if still pending."""
        self.stop_calls += 1
        if self._run_future is not None and not self._run_future.done():
            self._run_future.set_result(
                StepOutcome(
                    succeeded=False,
                    final_progress=self._last_progress,
                    error="cancelled",
                )
            )

    async def relaunch(self, component: Component) -> None:
        """Record the relaunch request without any subprocess effect."""
        self.relaunch_calls.append(component)

    def components(self) -> tuple[Component, ...]:
        return self._components

    def advance_phase(self) -> None:
        self.advance_phase_calls += 1

    # ------------------------------------------------------------------
    # Test-hook API — called from tests to drive the fake's lifecycle.
    # ------------------------------------------------------------------

    def simulate_volume(self, n: int) -> None:
        """Fire ``on_progress`` with ``value=n``.

        Safe to call before ``run()`` has subscribed; the snapshot is
        buffered and flushed when ``run()`` is awaited.
        """
        snapshot = StepProgress(
            value=n,
            target=self._progress_target,
            unit=self._progress_unit,  # type: ignore[arg-type]
            awaiting_advance=False,
        )
        self._last_progress = snapshot
        self._emit(snapshot)

    def simulate_phase_gate(
        self,
        phase: str,
        value: int,
        target: int,
        awaiting_advance: bool = True,
        detail: str | None = None,
    ) -> None:
        """Fire an ``on_progress`` with ``phase`` set and ``awaiting_advance=True``.

        Represents a multi-phase step pausing at a gate the operator must
        acknowledge with ``advance_phase()`` (i.e. the D key on NF_RUN).
        """
        snapshot = StepProgress(
            value=value,
            target=target,
            unit=self._progress_unit,  # type: ignore[arg-type]
            phase=phase,  # type: ignore[arg-type]
            detail=detail,
            awaiting_advance=awaiting_advance,
        )
        self._last_progress = snapshot
        self._emit(snapshot)

    def simulate_phase_change(
        self,
        phase: str,
        awaiting_advance: bool = True,
    ) -> None:
        """Backwards-compat alias for ``simulate_phase_gate`` with target=progress_target."""
        self.simulate_phase_gate(
            phase=phase,
            value=0,
            target=self._progress_target,
            awaiting_advance=awaiting_advance,
        )

    def simulate_crash(self, exit_code: int, error: str = "simulated crash") -> None:
        """Resolve ``run()`` with a failure outcome (operational error)."""
        if self._run_future is not None and not self._run_future.done():
            self._run_future.set_result(
                StepOutcome(
                    succeeded=False,
                    final_progress=self._last_progress,
                    error=error,
                )
            )

    def simulate_artifacts(self, artifacts: dict[str, Any]) -> None:
        """Attach artifacts to the NEXT ``simulate_completion(succeeded=True)``."""
        self._pending_artifacts = artifacts

    def simulate_completion(
        self,
        succeeded: bool = True,
        error: str | None = None,
    ) -> None:
        """Resolve ``run()`` with a terminal outcome.

        The prompt calls this ``simulate_complete``; the live test file
        (tests/test_session_runner.py) uses ``simulate_completion`` with
        ``succeeded=``/``error=`` kwargs, which is what we implement.
        """
        if self._run_future is None or self._run_future.done():
            return
        outcome = StepOutcome(
            succeeded=succeeded,
            final_progress=self._last_progress,
            error=error if not succeeded else None,
            artifacts=self._pending_artifacts if succeeded else None,
        )
        self._pending_artifacts = None
        self._run_future.set_result(outcome)

    # Keep both spellings so nobody wastes 10 minutes hunting a typo.
    simulate_complete = simulate_completion

    def simulate_programming_error(self, exc: BaseException) -> None:
        """Cause ``run()`` to raise ``exc`` (programming-error path).

        If ``run()`` is already in flight, inject the exception by
        setting it on the pending Future so the awaiter sees it as a
        raise.  Otherwise stash it so the next ``run()`` call raises.
        """
        if self._run_future is not None and not self._run_future.done():
            self._run_future.set_exception(exc)
        else:
            self._pending_error = exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit(self, snapshot: StepProgress) -> None:
        """Fire progress callback or buffer the snapshot until run() subscribes."""
        if self._on_progress is None:
            self._buffered_progress.append(snapshot)
        else:
            self._on_progress(snapshot)


# ---------------------------------------------------------------------------
# FakeMurfiProcess — L3 stub
# ---------------------------------------------------------------------------


class FakeMurfiProcess:
    """In-memory MURFI stand-in for L3 integration tests.

    Writes real-format MURFI log lines to ``log_path`` on
    ``simulate_volume`` so code that tails the log observes realistic
    content.  No subprocess is ever spawned.

    This is a minimal functional stub — enough for L3 tests to be
    written in todo-20.  Cadence-driven log emission (TR-paced) will be
    added when the L3 suite needs it.
    """

    def __init__(self, log_path: Path, tr: float = 1.2) -> None:
        self.log_path = Path(log_path)
        self.tr = tr
        self._started = False
        self._stopped = False
        self._exit_code: int | None = None

    async def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure the log exists so tailing code has something to open.
        if not self.log_path.exists():
            self.log_path.touch()
        self._started = True

    async def stop(self) -> None:
        self._stopped = True

    def simulate_volume(self, n: int) -> None:
        """Append a 'received image from scanner N' line (real MURFI format)."""
        line = f"received image from scanner {n}\n"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as handle:
            handle.write(line)

    def simulate_crash(self, exit_code: int) -> None:
        """Record a crash.  Consumers that check exit status will observe this."""
        self._exit_code = exit_code
        self._stopped = True

    @property
    def exit_code(self) -> int | None:
        return self._exit_code


# ---------------------------------------------------------------------------
# FakePsychoPyProcess — L3 stub
# ---------------------------------------------------------------------------


class FakePsychoPyProcess:
    """In-memory PsychoPy stand-in for L3 integration tests.

    On ``simulate_complete`` writes a CSV at ``csv_path`` with a single
    scale-factor row in the real PsychoPy format, so downstream code
    that parses it (artifact extraction, TUI status display) sees
    realistic content.
    """

    def __init__(self, csv_path: Path) -> None:
        self.csv_path = Path(csv_path)
        self._started = False
        self._stopped = False
        self._crashed = False

    async def start(self) -> None:
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._started = True

    async def stop(self) -> None:
        self._stopped = True

    def simulate_complete(self, scale_factor: float = 3.5) -> None:
        """Write a minimal PsychoPy-format CSV including the scale factor."""
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_path.write_text(
            "scale_factor,feedback_mean,feedback_std\n"
            f"{scale_factor},0.0,1.0\n"
        )

    def simulate_crash(self) -> None:
        self._crashed = True
        self._stopped = True

    @property
    def crashed(self) -> bool:
        return self._crashed


__all__ = [
    "FakeMurfiProcess",
    "FakePsychoPyProcess",
    "FakeStepExecutor",
]
