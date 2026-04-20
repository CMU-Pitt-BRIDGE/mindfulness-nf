"""StepExecutor protocol — unified interface for running any step kind.

SessionRunner stays kind-agnostic: it builds the right concrete executor
for each step (dispatched on `StepConfig.kind`) and calls `run()`.  Every
step type — vSend scans, DICOM scans, NF runs, FSL pipeline stages —
conforms to this Protocol.

This file is currently a DESIGN SKETCH.  The four concrete executors
(Vsend, Dicom, NfRun, FslStage) will be implemented during plan-execute
in sibling modules, each wrapping the existing `orchestration/{murfi,
dicom_receiver, psychopy, ica}.py` helpers.

────────────────────────────────────────────────────────────────────────
Design decisions locked in
────────────────────────────────────────────────────────────────────────

1. **Executors are per-step, one-shot, self-encapsulating.**  SessionRunner
   constructs a fresh executor for each step, passing all config to
   `__init__` (NOT to `run`).  The executor owns any subprocesses it
   launches; the runner never reaches through to them.  `relaunch()` is
   how the M/P keys restart a single subprocess while keeping the rest
   of the step's state intact.  When the step ends, the instance is
   discarded.

2. **`run()` is a single awaitable that returns a terminal `StepOutcome`.**
   No separate on_complete / on_error callbacks.  The caller wraps
   `run()` in `asyncio.create_task()`; `task.done()` tells whether the
   step is still in flight, `task.result()` yields the outcome.  This
   collapses three-way completion logic (success / clean-cancel / error)
   into one return site, and reuses asyncio.Task's existing API instead
   of mirroring it.

3. **Progress is push via sync callback; terminal state is pull via return.**
   Progress updates fire many times per second → push is correct.
   Terminal state fires once → awaiting a task is correct.  The callback
   is synchronous (not async) so it can't accidentally yield control in
   the middle of an update — JSON persistence in the runner takes ~10ms
   vs. TR ~1200ms, so there's no starvation risk.

4. **`run()` converts OPERATIONAL failures to StepOutcome, not programming
   errors.**  Expected operational errors (subprocess crash, I/O error,
   `asyncio.CancelledError`) return `StepOutcome(succeeded=False,
   error="...")`.  Programming errors (AttributeError, TypeError, KeyError
   in the executor's own logic) propagate as exceptions — the runner
   catches them at the outer level, logs with full trace, and marks the
   step failed.  Swallowing all exceptions would hide real bugs.

5. **`stop()` is idempotent and bounded.**  SIGTERM → `timeout` seconds
   grace → SIGKILL.  Safe to call before `run()` starts, during `run()`,
   or after it finishes.  Concurrent calls coalesce.  After `stop()`
   returns, any in-flight `run()` resolves with
   `StepOutcome(succeeded=False, error="cancelled")`.

6. **No `is_alive()` method.**  The runner uses `asyncio.Task.done()` on
   the run() task.  Adding a separate liveness method would create two
   sources of truth that can briefly disagree — e.g., subprocess died but
   run() hasn't returned `StepOutcome` yet.  One truth, not two.

7. **`StepProgress` uses typed Literals for unit and phase.**  `unit ∈
   {"volumes", "percent", "stages"}` covers all four step kinds.
   `phase ∈ {"murfi", "psychopy"}` or None handles the NF_RUN two-phase
   case.  The UI can render phase labels conditionally and resets the
   progress bar on phase change.

8. **Executor exposes `relaunch(component)` and `components()`.**  This
   is how the M/P keys work without the runner reaching into executor
   internals.  `components()` returns e.g. `("murfi", "psychopy")` for
   NF_RUN, `("murfi",)` for Vsend/Dicom, `()` for FslStage.  The TUI
   consults this to decide which keys are valid for the current step.
   `relaunch("murfi")` ensures MURFI is running fresh: if alive, stop
   then start; if dead, just start.  Safe concurrent with `run()`.

9. **`StepOutcome.artifacts` carries executor-specific metadata.**  A
   `dict[str, Any]` escape hatch for things the Protocol shouldn't
   enumerate: PsychoPy's scale factor, FSL mask paths, MELODIC component
   indices, etc.  The runner stores these in `StepState` for display and
   for downstream steps to read.

10. **Multi-phase steps use `advance_phase()`.**  Some steps have internal
   phases where the operator must acknowledge completion of one phase
   before the next begins — notably NF_RUN, where MURFI finishes collecting
   150 volumes and the operator presses D to launch PsychoPy.  The runner
   calls `advance_phase()` on that D press; the executor transitions
   internally.  For single-phase executors this is a no-op.  Distinct
   from `SessionRunner.advance()`, which moves the *cursor* between steps.

11. **M/P (relaunch) is restricted to `running` state.**  Relaunching a
   component on a `failed` step would require reconstructing executor
   state over partial data — complex and error-prone.  Instead: on
   `failed`, only R (clear + restart) is offered.  TUI gates M/P by
   checking both `components()` and the current step's status.

12. **No yellow-confirm force-complete.**  The old TUI let the operator
   force-advance a partial scan (e.g., 148/150 volumes) by double-pressing
   D.  The new design removes this: if the scanner delivers fewer volumes
   than expected, the step fails and the operator presses R to retry.
   This keeps the state machine simpler and tests deterministic.  If
   operations prove this is too strict, a follow-up can add
   `force_complete()` as a Protocol method.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol


# ---------------------------------------------------------------------------
# Typed enums and callbacks
# ---------------------------------------------------------------------------

ProgressUnit = Literal["volumes", "percent", "stages"]
Phase = Literal["murfi", "psychopy"]
Component = Literal["murfi", "psychopy", "dicom"]


# ---------------------------------------------------------------------------
# StepProgress + StepOutcome — the two value types the runner consumes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StepProgress:
    """A snapshot of a step's progress, pushed to the runner on every update.

    Shape examples:
        Scanner-paced:  value=87, target=150, unit="volumes"
        FSL stage:      value=63, target=100, unit="percent", detail="MELODIC: dim estimation"
        Binary stage:   value=0|1, target=1, unit="stages"
        NF_RUN at gate: phase="murfi", value=150, target=150, unit="volumes",
                        awaiting_advance=True, detail="Press D to start PsychoPy"
        NF_RUN phase 2: phase="psychopy", value=0, target=1, unit="stages",
                        detail="PsychoPy running", awaiting_advance=False

    The UI resets its progress bar when `phase` changes between updates.
    """

    value: int
    target: int
    unit: ProgressUnit
    phase: Phase | None = None
    detail: str | None = None
    awaiting_advance: bool = False  # True iff next phase requires `advance_phase()`


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """Terminal result of a step's execution. Returned by `run()` exactly once.

    `succeeded`        True iff the step completed cleanly AND data on disk
                       is valid.
    `final_progress`   Last progress snapshot (e.g. "147/150 volumes received
                       before MURFI died" forensics).
    `error`            None when succeeded.  Otherwise a short operator-readable
                       reason: "cancelled", "MURFI exited 1", "dcmsend failed",
                       "MELODIC non-zero exit", etc.  Never a stack trace.
    `artifacts`        Executor-specific metadata surfaced to the runner and UI.
                       e.g. {"scale_factor": 3.7}, {"dmn_mask": "path/to.nii"},
                       {"components_selected": [3, 7, 11]}.
    """

    succeeded: bool
    final_progress: StepProgress
    error: str | None = None
    artifacts: dict[str, Any] | None = None


ProgressCallback = Callable[[StepProgress], None]


# ---------------------------------------------------------------------------
# StepExecutor — the Protocol every step kind implements
# ---------------------------------------------------------------------------


class StepExecutor(Protocol):
    """Runs one step to completion. Instances are per-step, single-use.

    Construction:  the concrete class's `__init__` takes all step-specific
    config (StepConfig, subject_dir, scanner_config, pipeline_config,
    scanner_source, etc.). This Protocol does not constrain the constructor.

    Lifecycle:  exactly one call to `run()` per instance. Calls to `stop()`
    and `relaunch()` may arrive at any time — before, during, or after
    `run()`. All must be safe in all orderings.
    """

    async def run(self, on_progress: ProgressCallback) -> StepOutcome:
        """Execute the step. Returns when the step ends (cleanly or not).

        Invokes `on_progress(snapshot)` on every meaningful state change
        (new volume received, FSL stage transition, phase change). The
        callback is synchronous — keep it fast, no awaits.

        Converts operational failures (subprocess crash, I/O error,
        `asyncio.CancelledError`) into `StepOutcome(succeeded=False, ...)`.
        Programming errors propagate as exceptions.
        """

    async def stop(self, timeout: float = 5.0) -> None:
        """Cancel any in-flight work. SIGTERM → `timeout`s → SIGKILL.

        Idempotent. Safe to call before run() starts, during run(), or
        after it finishes. Concurrent calls coalesce. After stop() returns,
        an in-flight run() resolves with succeeded=False, error="cancelled".
        """

    async def relaunch(self, component: Component) -> None:
        """Ensure `component` is running fresh for the remainder of this step.

        If currently alive: graceful stop, then start.  If dead: start.
        No-op if `component` is not applicable to this executor (caller
        should gate on `components()`, but misuse is safe).  Keeps all
        other subprocesses and progress intact — does NOT clear data on
        disk (that is `r`, not `m`/`p`).
        """

    def components(self) -> tuple[Component, ...]:
        """Which components this executor can individually relaunch.

        Used by the TUI to decide which of `m` / `p` keybindings to show.
        Examples:
            NfRunStepExecutor  → ("murfi", "psychopy")
            VsendStepExecutor  → ("murfi",)
            DicomStepExecutor  → ("murfi", "dicom")
            FslStageExecutor   → ()
            SetupStepExecutor  → ()

        Non-async, zero-side-effect — safe to call any time.
        """

    def advance_phase(self) -> None:
        """Signal this executor to advance past a phase gate.

        For multi-phase executors (NF_RUN: MURFI → PsychoPy), this is how
        the runner tells the executor "operator pressed D, proceed to the
        next phase".  For single-phase executors (Vsend, Dicom, FslStage,
        Setup), this is a no-op.

        Synchronous (sets an internal flag or event).  Safe to call
        multiple times; only the first call in each phase takes effect.
        Does NOT complete the step — the step completes only when `run()`
        returns its `StepOutcome`.
        """


# ---------------------------------------------------------------------------
# Concrete executors (implemented during plan-execute, not here)
# ---------------------------------------------------------------------------
#
# class SetupStepExecutor:     # SETUP         — run preflight checks only
# class VsendStepExecutor:     # VSEND_SCAN    — MURFI via vSend for 2vol
# class DicomStepExecutor:     # DICOM_SCAN    — MURFI + DICOM receiver for rest
# class NfRunStepExecutor:     # NF_RUN        — MURFI (phase=murfi) then
#                              #                 PsychoPy (phase=psychopy)
# class FslStageExecutor:      # PROCESS_STAGE — fslmerge / MELODIC / flirt / ...
#
# All are one-shot: constructor takes config + deps, `run()` is called once,
# instance is discarded on completion.
