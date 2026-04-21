"""SessionRunner — the central orchestrator the TUI consumes.

Coordinates :class:`SessionState` transitions with concrete step executors and
the scanner source.  Owns the asyncio supervisor coroutine that awaits each
step's task and folds its :class:`StepOutcome` (or unhandled exception) into
state transitions.  Persists ``session_state.json`` atomically after every
transition.

This module is part of the imperative shell: it performs file I/O (atomic
writes of the state JSON) and orchestrates asyncio tasks.  All domain logic
(transitions, invariants) lives in :mod:`mindfulness_nf.models`.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import assert_never

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import (
    SessionState,
    StepConfig,
    StepKind,
    StepState,
    StepStatus,
)
from mindfulness_nf.orchestration.executor import (
    StepExecutor,
    StepOutcome,
    StepProgress,
)
from mindfulness_nf.orchestration.executors import (
    DicomStepExecutor,
    FslStageExecutor,
    NfRunStepExecutor,
    SetupStepExecutor,
    VsendStepExecutor,
)
from mindfulness_nf.orchestration.scanner_source import ScannerSource
from mindfulness_nf.orchestration.subjects import (
    clear_bids_run_files,
    load_bids_session_state,
    persist_bids_session_state,
)
from mindfulness_nf.sessions import SESSION_CONFIGS

__all__ = ["SessionRunner"]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    """UTC wall-clock timestamp in ISO-8601 with timezone suffix."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# SessionRunner
# ---------------------------------------------------------------------------


class SessionRunner:
    """Coordinates :class:`SessionState` with step executors and scanner source.

    Invariants enforced here (see spec §Invariants):

    #. At most one step is RUNNING — single enforcement point in
       :meth:`start_current`.
    #. State persistence is atomic (temp file + ``os.replace``).
    #. Programming errors become failed steps with traces, not crashes.
    """

    def __init__(
        self,
        state: SessionState,
        subject_dir: Path,
        pipeline: PipelineConfig,
        scanner_config: ScannerConfig,
        scanner_source: ScannerSource,
    ) -> None:
        self._state = state
        self._subject_dir = subject_dir
        self._pipeline = pipeline
        self._scanner_config = scanner_config
        self._scanner_source = scanner_source

        self._current_task: asyncio.Task[StepOutcome] | None = None
        self._current_executor: StepExecutor | None = None
        self._subscribers: list[Callable[[SessionState], None]] = []

        # Persist the initial state so callers observing the file on disk
        # see a coherent snapshot from the first moment.
        self._persist(self._state)

    # ------------------------------------------------------------------
    # Classmethod constructor
    # ------------------------------------------------------------------

    @classmethod
    def load_or_create(
        cls,
        subject_dir: Path,
        session_type: str,
        pipeline: PipelineConfig,
        scanner_config: ScannerConfig,
        scanner_source: ScannerSource,
    ) -> SessionRunner:
        """Resume from ``session_state.json`` if present; else build fresh.

        Resume behaviour (per spec §Resume):
          * Unknown ``schema_version`` → ``ValueError``.
          * ``status=running`` steps are coerced to ``failed`` with
            ``error="interrupted by restart"``.
          * Persisted step configs take priority over the current
            ``SESSION_CONFIGS`` (self-contained resume, robust to drift).
          * Partial NIfTI files on disk are left alone — operator decides.
        """
        loaded = load_bids_session_state(subject_dir)
        if loaded is not None:
            state = loaded
        else:
            configs = SESSION_CONFIGS[session_type]
            now = _iso_now()
            # Default subject id from the directory name; BIDS layout uses
            # ``sub-<id>/ses-<type>/``.  Good enough for the runner; the TUI
            # passes the intended subject via the preceding SessionState.
            subject = subject_dir.name or "sub-unknown"
            state = SessionState(
                subject=subject,
                session_type=session_type,
                cursor=0,
                steps=tuple(StepState(config=c) for c in configs),
                created_at=now,
                updated_at=now,
            )

        return cls(
            state=state,
            subject_dir=subject_dir,
            pipeline=pipeline,
            scanner_config=scanner_config,
            scanner_source=scanner_source,
        )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    @property
    def state(self) -> SessionState:
        """Current session state.  Immutable — do not mutate in place."""
        return self._state

    @property
    def available_components(self) -> tuple[str, ...]:
        """Components the current executor can individually relaunch (M/P keys)."""
        if self._current_executor is None:
            return ()
        return tuple(self._current_executor.components())

    def subscribe(self, cb: Callable[[SessionState], None]) -> None:
        """Register a callback invoked on every state transition."""
        self._subscribers.append(cb)

    # ------------------------------------------------------------------
    # Cursor navigation (pure; no subprocess interaction)
    # ------------------------------------------------------------------

    def advance(self) -> None:
        """Move cursor forward.  Auto-starts the new cursor step if pending.

        Navigation never touches the running step (invariant: one running
        step at a time, cursor and running index may diverge).
        """
        new_state = self._state.advance()
        self._apply(new_state)
        # Auto-chain: if the new cursor step is pending and nothing is
        # running, start it — preserves the "one D press = move on" UX.
        if (
            new_state.current.status is StepStatus.PENDING
            and (self._current_task is None or self._current_task.done())
        ):
            asyncio.create_task(self.start_current())

    def go_back(self) -> None:
        """Move cursor backward.  Never touches the running step."""
        self._apply(self._state.go_back())

    def select(self, i: int) -> None:
        """Jump cursor to ``i`` (clamped).  Never touches the running step."""
        self._apply(self._state.select(i))

    # ------------------------------------------------------------------
    # Step execution (I/O)
    # ------------------------------------------------------------------

    async def start_current(self) -> None:
        """Launch the cursor step's executor.

        Single enforcement point for the "at most one running step"
        invariant: refuses (no-op with log) if another step is in flight.
        """
        if self._current_task is not None and not self._current_task.done():
            # Another step is running; refuse without raising (per spec).
            logger.info(
                "start_current refused: another step is running (index=%s)",
                self._state.running_index,
            )
            return

        cursor = self._state.cursor
        step = self._state.current.config
        self._current_executor = self._executor_for(step)
        self._apply(self._state.mark_running(cursor, _iso_now()))

        def _on_progress(p: StepProgress) -> None:
            # Guard: progress may arrive after a navigation; clamp to the
            # index that was running when the executor was launched.
            self._apply(
                self._state.set_progress(
                    cursor,
                    p.value,
                    detail=p.detail,
                    phase=p.phase,
                    awaiting_advance=p.awaiting_advance,
                )
            )

        executor = self._current_executor
        self._current_task = asyncio.create_task(executor.run(_on_progress))
        asyncio.create_task(self._supervise(cursor, self._current_task))
        # Yield once so the executor's run() coroutine gets to set up its
        # internal Future / subscribe progress callback before the caller
        # drives it (e.g. FakeStepExecutor.simulate_completion).
        await asyncio.sleep(0)

    async def stop_current(self) -> None:
        """Stop the current step's executor.  Idempotent."""
        executor = self._current_executor
        if executor is None:
            return
        await executor.stop()
        task = self._current_task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception:
                # Supervisor already converted it; avoid double-log noise.
                pass

    async def interrupt_current(self) -> None:
        """Stop the running step, clear its partial data, mark pending.

        ``i`` targets the *running* step, not the cursor — so we operate on
        ``running_index`` when available, otherwise on the cursor if it is
        pointing at a failed step.
        """
        running_idx = self._state.running_index
        target = running_idx if running_idx is not None else self._state.cursor

        # Stop first (so no new progress lands after we clear).
        await self.stop_current()

        # Clear partial NIfTI files for this step's (task, run).
        step_config = self._state.steps[target].config
        clear_bids_run_files(
            self._subject_dir,
            self._state.subject,
            self._state.session_type,
            step_config,
        )

        # Clear per-attempt state (increments attempts, resets progress).
        # clear_current operates on the *cursor*; point the cursor at the
        # targeted step transiently so the invariant is upheld.
        selected = self._state.select(target)
        cleared = selected.clear_current()
        # Restore caller's cursor unless it was the target we just cleared.
        if running_idx is not None and self._state.cursor != target:
            cleared = cleared.select(self._state.cursor)
        self._apply(cleared)

    async def clear_and_restart_current(self) -> None:
        """Stop the current step, clear its data, and start fresh."""
        await self.interrupt_current()
        await self.start_current()

    async def advance_phase_current(self) -> None:
        """Signal the current executor to advance past a phase gate (D key)."""
        if self._current_executor is None:
            return
        self._current_executor.advance_phase()

    # ------------------------------------------------------------------
    # Component controls (M / P)
    # ------------------------------------------------------------------

    async def relaunch_component(self, component: str) -> None:
        """Relaunch a single component (MURFI / PsychoPy) mid-step.

        Valid only while the current step is RUNNING *and* the component is
        exposed by the executor.  On any other state this is a no-op with a
        log line (the TUI hides the key, but misuse is still safe).
        """
        if self._state.current.status is not StepStatus.RUNNING:
            logger.info(
                "relaunch_component(%s) refused: current step is %s, not running",
                component,
                self._state.current.status.value,
            )
            return
        if self._current_executor is None:
            return
        if component not in self._current_executor.components():
            logger.info(
                "relaunch_component(%s) refused: not in executor.components()=%r",
                component,
                self._current_executor.components(),
            )
            return
        await self._current_executor.relaunch(component)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _executor_for(self, step: StepConfig) -> StepExecutor:
        """Construct the right concrete executor for this step's ``StepKind``."""
        match step.kind:
            case StepKind.SETUP:
                return SetupStepExecutor(
                    config=step,
                    subject_dir=self._subject_dir,
                    pipeline=self._pipeline,
                    scanner_config=self._scanner_config,
                )
            case StepKind.VSEND_SCAN:
                return VsendStepExecutor(
                    config=step,
                    subject_dir=self._subject_dir,
                    pipeline=self._pipeline,
                    scanner_config=self._scanner_config,
                    scanner_source=self._scanner_source,
                )
            case StepKind.DICOM_SCAN:
                return DicomStepExecutor(
                    config=step,
                    subject_dir=self._subject_dir,
                    pipeline=self._pipeline,
                    scanner_config=self._scanner_config,
                    scanner_source=self._scanner_source,
                )
            case StepKind.NF_RUN:
                return NfRunStepExecutor(
                    config=step,
                    subject_dir=self._subject_dir,
                    pipeline=self._pipeline,
                    scanner_config=self._scanner_config,
                    scanner_source=self._scanner_source,
                )
            case StepKind.PROCESS_STAGE:
                return FslStageExecutor(
                    config=step,
                    subject_dir=self._subject_dir,
                    pipeline=self._pipeline,
                    scanner_config=self._scanner_config,
                )
            case _ as unreachable:
                assert_never(unreachable)

    async def _supervise(
        self, cursor: int, task: asyncio.Task[StepOutcome]
    ) -> None:
        """Await the run task and fold its outcome into state.

        Supervisor always transitions RUNNING → (COMPLETED | FAILED):
          * ``StepOutcome(succeeded=True)``   → ``mark_completed``
          * ``StepOutcome(succeeded=False)``  → ``mark_failed``
          * unhandled exception               → ``mark_failed`` with
            ``internal error:`` prefix (programming bug, not operational).

        ``CancelledError`` propagates — it means the supervisor's own task
        was cancelled, not that the step was cancelled (that returns a
        ``StepOutcome(succeeded=False, error="cancelled")``).
        """
        try:
            outcome = await task
        except asyncio.CancelledError:
            raise
        except Exception as bug:  # noqa: BLE001 — deliberately catch-all
            tb = traceback.format_exc()
            logger.error("unhandled exception in executor.run():\n%s", tb)
            err_msg = f"internal error: {type(bug).__name__}: {bug}"
            # Only mark failed if still running — a concurrent interrupt
            # may have already transitioned us out of RUNNING.
            if self._state.steps[cursor].status is StepStatus.RUNNING:
                self._apply(
                    self._state.mark_failed(cursor, _iso_now(), error=err_msg)
                )
        else:
            ts = _iso_now()
            if self._state.steps[cursor].status is not StepStatus.RUNNING:
                # Someone interrupted / cleared us — don't overwrite.
                pass
            elif outcome.succeeded:
                self._apply(
                    self._state.mark_completed(
                        cursor, ts, artifacts=outcome.artifacts
                    )
                )
            else:
                self._apply(
                    self._state.mark_failed(cursor, ts, error=outcome.error)
                )
        finally:
            # Clear executor handles regardless of outcome so the runner
            # is ready to start the next step.
            if self._current_task is task:
                self._current_task = None
                self._current_executor = None

    def _apply(self, new_state: SessionState) -> None:
        """Atomic: bump ``updated_at``, persist to JSON, swap in-memory, notify.

        Persist BEFORE swapping so that if the write fails, in-memory state
        stays consistent with disk.  Subscribers see the post-swap state.
        """
        new_state = copy.replace(new_state, updated_at=_iso_now())
        self._persist(new_state)
        self._state = new_state
        for cb in self._subscribers:
            try:
                cb(new_state)
            except Exception:  # noqa: BLE001 — subscriber bugs must not crash the runner
                logger.exception("subscriber callback failed")

    def _persist(self, state: SessionState) -> None:
        """Write ``session_state.json`` atomically (temp file + ``os.replace``).

        Delegates to :func:`subjects.persist_bids_session_state`.
        """
        persist_bids_session_state(self._subject_dir, state)
