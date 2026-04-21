"""Domain models for the mindfulness neurofeedback pipeline.

All models use frozen=True, slots=True. Collections are tuple, not list.
No I/O imports permitted in this module (FCIS boundary).
"""

from __future__ import annotations

import copy
import enum
from dataclasses import dataclass
from typing import Any, Literal, assert_never


class Color(enum.Enum):
    """Traffic light color indicating check status."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass(frozen=True, slots=True)
class TrafficLight:
    """A traffic light status indicator with color, message, and optional detail."""

    color: Color
    message: str
    detail: str | None = None

    @property
    def blocks_advance(self) -> bool:
        """Whether this status prevents the operator from advancing."""
        match self.color:
            case Color.GREEN:
                return False
            case Color.YELLOW:
                return False
            case Color.RED:
                return True
            case _ as unreachable:
                assert_never(unreachable)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of a single preflight check."""

    name: str
    passed: bool
    message: str


@dataclass(frozen=True, slots=True)
class RunState:
    """View model for a single run's progress (consumed by TUI widgets).

    Distinct from `StepState` (the engine's persistent state). The new
    `SessionScreen` constructs `RunState` snapshots from `StepState` at
    render time; widgets under `tui/widgets/` continue to consume this
    lightweight shape.
    """

    name: str
    expected_volumes: int
    received_volumes: int = 0
    feedback: bool = False
    scale_factor: float | None = None

    def with_volumes(self, count: int) -> RunState:
        """Return a new RunState with the given received volume count."""
        return copy.replace(self, received_volumes=count)


class StepStatus(enum.Enum):
    """Lifecycle status of a single session step."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StepKind(enum.Enum):
    """What kind of work a step performs; dispatches to the matching executor."""

    SETUP = "setup"
    VSEND_SCAN = "vsend"
    DICOM_SCAN = "dicom"
    NF_RUN = "nf_run"
    PROCESS_STAGE = "process"


# Typed aliases shared with executors.
ProgressUnit = Literal["volumes", "percent", "stages"]
Phase = Literal["murfi", "psychopy"]


@dataclass(frozen=True, slots=True)
class StepConfig:
    """Static configuration for one step within a session.

    Configs live in ``sessions.py`` as data tables (one row per step) and
    are embedded into ``SessionState`` so resume is robust to code drift.
    """

    name: str
    task: str | None
    run: int | None
    progress_target: int
    progress_unit: ProgressUnit
    xml_name: str | None
    kind: StepKind
    feedback: bool = False
    fsl_command: str | None = None


@dataclass(frozen=True, slots=True)
class StepState:
    """Runtime state of a single step: config + per-attempt bookkeeping."""

    config: StepConfig
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    progress_current: int = 0
    last_started: str | None = None
    last_finished: str | None = None
    detail_message: str | None = None
    error: str | None = None
    phase: Phase | None = None
    awaiting_advance: bool = False
    artifacts: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SessionState:
    """Immutable state of a full session.

    All transitions are pure: each returns a new ``SessionState`` via
    ``copy.replace``. Invariants (see spec §Invariants):

    * At most one step has status=RUNNING at any time.
    * Cursor stays within ``[0, len(steps))``.
    * Session completion is derived, not stored: all steps COMPLETED.
    """

    subject: str
    session_type: str
    cursor: int
    steps: tuple[StepState, ...]
    created_at: str
    updated_at: str

    # ------------------------------------------------------------------
    # Cursor navigation
    # ------------------------------------------------------------------

    def advance(self) -> SessionState:
        """Move cursor forward by one. No-op at the last step (clamped)."""
        last = len(self.steps) - 1
        if self.cursor >= last:
            return self
        return copy.replace(self, cursor=self.cursor + 1)

    def go_back(self) -> SessionState:
        """Move cursor backward by one. No-op at index 0 (clamped)."""
        if self.cursor <= 0:
            return self
        return copy.replace(self, cursor=self.cursor - 1)

    def select(self, i: int) -> SessionState:
        """Jump cursor to ``i``, clamped to ``[0, len(steps))``."""
        clamped = max(0, min(i, len(self.steps) - 1))
        if clamped == self.cursor:
            return self
        return copy.replace(self, cursor=clamped)

    # ------------------------------------------------------------------
    # Status transitions
    # ------------------------------------------------------------------

    def mark_running(self, i: int, ts: str) -> SessionState:
        """Transition step ``i`` to RUNNING with start timestamp ``ts``.

        Raises ``ValueError`` if any step is already RUNNING (invariant:
        at most one running step at a time).
        """
        for idx, step in enumerate(self.steps):
            if step.status is StepStatus.RUNNING and idx != i:
                raise ValueError(
                    f"cannot mark step {i} running: step {idx} is already running"
                )
        new_step = copy.replace(
            self.steps[i], status=StepStatus.RUNNING, last_started=ts
        )
        return self._with_step(i, new_step)

    def mark_completed(
        self,
        i: int,
        ts: str,
        artifacts: dict[str, Any] | None = None,
    ) -> SessionState:
        """Transition step ``i`` to COMPLETED and attach artifacts.

        Clears any stale ``error`` from a prior (cleared) attempt.
        """
        new_step = copy.replace(
            self.steps[i],
            status=StepStatus.COMPLETED,
            last_finished=ts,
            artifacts=artifacts,
            error=None,
        )
        return self._with_step(i, new_step)

    def mark_failed(
        self,
        i: int,
        ts: str,
        error: str | None = None,
    ) -> SessionState:
        """Transition step ``i`` from RUNNING to FAILED with an error message.

        Raises ``ValueError`` if the step is not currently RUNNING (the
        supervisor only ever transitions RUNNING → FAILED).
        """
        if self.steps[i].status is not StepStatus.RUNNING:
            raise ValueError(
                f"cannot mark step {i} failed: step is not running "
                f"(status={self.steps[i].status.value})"
            )
        new_step = copy.replace(
            self.steps[i],
            status=StepStatus.FAILED,
            last_finished=ts,
            error=error,
        )
        return self._with_step(i, new_step)

    # ------------------------------------------------------------------
    # Per-step mutations
    # ------------------------------------------------------------------

    def clear_current(self) -> SessionState:
        """Reset the cursor step to clean-slate PENDING, incrementing attempts.

        Preserves ``config``. Leaves every other step untouched.
        """
        i = self.cursor
        old = self.steps[i]
        new_step = StepState(config=old.config, attempts=old.attempts + 1)
        return self._with_step(i, new_step)

    def set_progress(
        self,
        i: int,
        value: int,
        detail: str | None = None,
        phase: Phase | None = None,
        awaiting_advance: bool = False,
    ) -> SessionState:
        """Update narrative progress fields for step ``i``."""
        new_step = copy.replace(
            self.steps[i],
            progress_current=value,
            detail_message=detail,
            phase=phase,
            awaiting_advance=awaiting_advance,
        )
        return self._with_step(i, new_step)

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def current(self) -> StepState:
        """The step at the current cursor position."""
        return self.steps[self.cursor]

    @property
    def running_index(self) -> int | None:
        """Index of the unique RUNNING step, or ``None`` if none is running."""
        for idx, step in enumerate(self.steps):
            if step.status is StepStatus.RUNNING:
                return idx
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _with_step(self, i: int, step: StepState) -> SessionState:
        """Return a new SessionState with ``steps[i]`` replaced by ``step``."""
        new_steps = self.steps[:i] + (step,) + self.steps[i + 1 :]
        return copy.replace(self, steps=new_steps)
