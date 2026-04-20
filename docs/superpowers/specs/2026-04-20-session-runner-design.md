# SessionRunner: Resilient session orchestration with BIDS layout, resume, and dry-run

**Date:** 2026-04-20
**Status:** Approved
**Python:** 3.13+
**Supersedes parts of:** `2026-03-20-textual-tui-design.md` (the FCIS architecture stays; per-session screen classes are replaced)

## Problem

The current TUI implements a forward-only wizard whose state lives in Python instance variables on each `Screen` subclass. This causes five failure modes the operator cannot recover from:

1. **Brittle gating.** A step marked complete cannot be redone. A step in `RED` cannot be advanced. Mid-session failures force the operator to quit the TUI and lose context from every prior step.
2. **No process resilience.** MURFI or PsychoPy dying mid-run orphans the session. There is no way to restart only the dead component.
3. **No resume.** `save_session_state()` exists in `orchestration/subjects.py` but is never called. A subject walking out of the scanner mid-session means re-doing the session from step 1.
4. **Missing protocols.** The RT15 protocol has no implementation. The RT30 protocol (via `NF_RUN_SEQUENCE`) is missing the final `transferpost_run-02` that the scanner PDF (`materials/mri_sequences/RT30.pdf`) specifies.
5. **No meaningful dry-run.** `TestScreen` + `SimulatedMurfi` exercises only the state transitions of the Localizer flow, with fake volume counts on a timer. The operator cannot rehearse RT15/RT30, cannot rehearse crash recovery, and cannot exercise MURFI+PsychoPy coordination outside the scanner.

The combined effect: the operator cannot trust the system with stakeholders. A scanner session that wastes a participant's hour because MURFI died during Feedback 3 is professionally unacceptable.

## Solution

Extract orchestration into a `SessionRunner` that owns session state, drives subprocesses, and persists state to disk after every transition. Make `SessionState` a pure, immutable dataclass. Make session configuration (Localizer / RT15 / RT30) a *data table* — one row per step — rather than three screen classes. Use a BIDS-compliant directory layout so an operator reading `ls` can tell what has run, what has not, and where it stopped. Replace `TestScreen` with a dry-run mode: a simulated scanner feeds real MURFI and real PsychoPy, so rehearsal exercises the real code path.

This is a significant refactor (~800–1200 LOC changed) but the risk is bounded: the TUI becomes a thin view over a well-tested core, and the pure state machine is exhaustively unit-tested before any I/O code runs against it.

## Architecture

### Layered view

```
┌─────────────────────────────────────────────────┐
│ TUI (Textual)                                   │
│  SessionScreen   (ONE class for all session     │
│                   types; renders SessionState)  │
└──────────────────────┬──────────────────────────┘
                       │ intents
                       │ advance / go_back / select_step
                       │ clear_current / relaunch_component
                       │ interrupt
┌──────────────────────▼──────────────────────────┐
│ SessionRunner   (orchestration/session_runner)  │
│  ─ IMPERATIVE SHELL ────────────────────────────│
│  • dispatches StepKind → StepExecutor           │
│  • awaits asyncio.Task for current executor     │
│  • persists SessionState after every transition │
│  • swaps ScannerSource: Real vs Simulated       │
└──┬─────────────────────┬────────────────────┬───┘
   │                     │                    │
┌──▼──────────────┐ ┌────▼──────────────┐ ┌──▼──────────────┐
│ SessionState    │ │ StepExecutor      │ │ ScannerSource   │
│ (models.py,     │ │ (Protocol)        │ │ Protocol:       │
│  frozen, pure)  │ │ • run() → Outcome │ │  • push_vsend() │
│ • steps[]       │ │ • stop()          │ │  • push_dicom() │
│ • cursor        │ │ • relaunch(comp)  │ │ Real | Simulated│
│ • status per    │ │ • components()    │ │                 │
│   step (pending │ │                   │ │                 │
│   running, done,│ │ Concrete: Setup,  │ │                 │
│   failed)       │ │ Vsend, Dicom,     │ │                 │
│                 │ │ NfRun, FslStage   │ │                 │
└─────────────────┘ └───────────────────┘ └─────────────────┘
```

### Properties

- **FCIS honored.** `SessionState` is `frozen=True, slots=True` like everything else in `models.py`; all transitions return a new instance. The TUI holds no state other than "which state did the runner notify me about."
- **Single `SessionScreen`.** Localizer, RT15, RT30 become *data* (step lists in `sessions.py`), not separate screen classes. `LocalizerScreen`, `NeurofeedbackScreen`, and `TestScreen` are deleted.
- **Resume is automatic.** `SessionRunner.load_or_create(...)` checks for an existing `session_state.json` and loads it; otherwise builds fresh from `SESSION_CONFIGS`. Any step with `status=running` at load time is coerced to `failed`, because the process that was running is gone.
- **Dry-run is one adapter swap.** `ScannerSource` is a `Protocol`. `SimulatedScannerSource` replays cached volumes. MURFI and PsychoPy are unmodified.

## Data model

### Filesystem layout (BIDS-compliant)

```
murfi/subjects/
└── sub-001/
    ├── sub-001_sessions.tsv                       # session registry (BIDS)
    ├── ses-loc3/
    │   ├── session_state.json                     # resume source of truth
    │   ├── func/                                  # BIDS per-run outputs
    │   │   ├── sub-001_ses-loc3_task-rest_run-01_bold.nii
    │   │   ├── sub-001_ses-loc3_task-rest_run-01_bold.json
    │   │   └── sub-001_ses-loc3_task-rest_run-02_bold.nii
    │   ├── sourcedata/                            # BIDS raw provenance
    │   │   ├── murfi/
    │   │   │   ├── xml/{2vol.xml, rest.xml, rtdmn.xml}
    │   │   │   ├── img/img-00001-00001.nii …      # MURFI raw output
    │   │   │   └── log/murfi.log
    │   │   └── psychopy/
    │   │       └── sub-001_ses-loc3_run01.csv
    │   └── derivatives/
    │       └── masks/{DMN,CEN}.nii
    ├── ses-rt15/
    │   ├── session_state.json
    │   ├── func/
    │   │   ├── sub-001_ses-rt15_task-2vol_run-01_bold.nii
    │   │   ├── sub-001_ses-rt15_task-transferpre_run-01_bold.nii
    │   │   ├── sub-001_ses-rt15_task-feedback_run-01_bold.nii
    │   │   ├── … run-02 through run-05 …
    │   │   └── sub-001_ses-rt15_task-transferpost_run-01_bold.nii
    │   ├── sourcedata/
    │   └── derivatives/
    └── ses-rt30/
        └── …                                      # same shape, more runs
```

Rationale:

- `ls subjects/sub-001/` instantly shows which sessions exist.
- `ls subjects/sub-001/ses-rt15/func/` instantly shows which runs completed.
- `sourcedata/murfi/img/` holds MURFI's raw `img-SSSSS-VVVVV.nii` with no renaming (lossless provenance).
- On step completion, the runner moves (or symlinks) the raw volumes into a single BIDS-named NIfTI under `func/`, and writes a JSON sidecar with task, run, expected vs received volumes, attempts, and timestamps.

### `session_state.json` schema

The JSON is **self-contained**: it persists the full `StepConfig` for each step so resume is robust to drift in `SESSION_CONFIGS`. A session started under one config version can be resumed cleanly after code changes (new fields default to None; removed fields are ignored).

```json
{
  "schema_version": 1,
  "subject": "sub-001",
  "session_type": "rt15",
  "created_at": "2026-04-20T14:02:00Z",
  "updated_at": "2026-04-20T14:37:00Z",
  "cursor": 4,
  "steps": [
    {
      "config": {
        "name": "Feedback 2", "task": "feedback", "run": 2,
        "progress_target": 150, "progress_unit": "volumes",
        "xml_name": "rtdmn.xml", "kind": "nf_run",
        "feedback": true, "fsl_command": null
      },
      "status": "failed",
      "attempts": 1,
      "progress_current": 87,
      "last_started": "2026-04-20T14:35:00Z",
      "last_finished": "2026-04-20T14:36:40Z",
      "detail_message": null,
      "error": "MURFI exited 1 at vol 87",
      "phase": "murfi",
      "awaiting_advance": false,
      "artifacts": null
    }
  ]
}
```

- `status` ∈ `{pending, running, completed, failed}`.
- A step is only persisted as `completed` after per-kind disk validation inside the executor confirms success.
- `attempts` increments each time `clear_and_restart_current` or `interrupt_current` runs.
- `cursor` is decoupled from execution: it tracks where the *operator is looking*, not what is running.
- On load: if `schema_version` is unknown, refuse to deserialize and surface a clear error. The operator can then delete the file to start fresh.

### Python models (extensions to `mindfulness_nf/models.py`)

```python
class StepStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class StepKind(enum.Enum):
    SETUP = "setup"              # preflight-only; no MURFI/DICOM
    VSEND_SCAN = "vsend"         # 2vol calibration via vSend
    DICOM_SCAN = "dicom"         # resting state via DICOM receiver
    NF_RUN = "nf_run"            # feedback/transfer runs (MURFI + PsychoPy)
    PROCESS_STAGE = "process"    # FSL pipeline stage (fslmerge, MELODIC, flirt, ...)

ProgressUnit = Literal["volumes", "percent", "stages"]   # (mirrors executor.py)
Phase = Literal["murfi", "psychopy"]                      # (mirrors executor.py)

@dataclass(frozen=True, slots=True)
class StepConfig:
    name: str
    task: str | None             # BIDS task label, e.g., "feedback"
    run: int | None              # BIDS run number, 1-indexed (None for PROCESS_STAGE)
    progress_target: int         # scans: expected volume count; compute: 100 (%) or 1 (done)
    progress_unit: ProgressUnit  # "volumes" | "percent" | "stages"
    xml_name: str | None         # MURFI template, e.g., "rtdmn.xml" (None for PROCESS_STAGE)
    kind: StepKind
    feedback: bool = False       # only relevant for NF_RUN
    fsl_command: str | None = None   # only relevant for PROCESS_STAGE

@dataclass(frozen=True, slots=True)
class StepState:
    config: StepConfig
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    progress_current: int = 0            # unit=volumes: received; percent: 0-100; stages: 0 or 1
    last_started: str | None = None      # ISO-8601 UTC
    last_finished: str | None = None
    detail_message: str | None = None    # progress narration during run, e.g. "MELODIC dim estimation"
    error: str | None = None             # set only when status=failed; short operator-readable reason
    phase: Phase | None = None           # "murfi" | "psychopy" for NF_RUN mid-step (typed alias)
    awaiting_advance: bool = False       # True iff D should call advance_phase_current()
    artifacts: dict[str, Any] | None = None   # executor-specific outputs

@dataclass(frozen=True, slots=True)
class SessionState:
    subject: str
    session_type: str                   # "loc3" | "rt15" | "rt30" | "process"
    cursor: int
    steps: tuple[StepState, ...]
    created_at: str
    updated_at: str

    # All transitions are pure: return a new SessionState.
    def advance(self) -> SessionState
    def go_back(self) -> SessionState
    def select(self, i: int) -> SessionState
    def mark_running(self, i: int, ts: str) -> SessionState
    def mark_completed(
        self, i: int, ts: str, artifacts: dict[str, Any] | None = None
    ) -> SessionState
    def mark_failed(self, i: int, ts: str, error: str | None = None) -> SessionState
    def clear_current(self) -> SessionState
        # Resets the cursor step to a clean-slate pending state:
        #   status=pending, progress_current=0, attempts+=1,
        #   detail_message=None, error=None, phase=None,
        #   awaiting_advance=False, last_started=None, last_finished=None,
        #   artifacts=None.
        # Config is preserved.
    def set_progress(                         # renamed from set_volumes; generic
        self, i: int, value: int,
        detail: str | None = None,
        phase: Phase | None = None,
        awaiting_advance: bool = False,
    ) -> SessionState
    @property
    def current(self) -> StepState            # steps[cursor]
    @property
    def running_index(self) -> int | None     # unique (invariant: at most one)
```

### Session configurations (new file: `mindfulness_nf/sessions.py`)

Configs use keyword arguments throughout to avoid positional mismatches as `StepConfig` evolves.

```python
def _feedback_block(start_run: int, count: int = 5) -> tuple[StepConfig, ...]:
    """Return StepConfig for `count` consecutive feedback runs, starting at
    run number `start_run`. Each is 150 volumes via rtdmn.xml with feedback=True."""
    return tuple(
        StepConfig(
            name=f"Feedback {start_run + i}",
            task="feedback",
            run=start_run + i,
            progress_target=150,
            progress_unit="volumes",
            xml_name="rtdmn.xml",
            kind=StepKind.NF_RUN,
            feedback=True,
        )
        for i in range(count)
    )

LOC3: tuple[StepConfig, ...] = (
    StepConfig(name="Setup",  task=None,   run=None, progress_target=0,
               progress_unit="stages",  xml_name=None,       kind=StepKind.SETUP),
    StepConfig(name="Rest 1", task="rest", run=1,    progress_target=250,
               progress_unit="volumes", xml_name="rest.xml", kind=StepKind.DICOM_SCAN),
    StepConfig(name="Rest 2", task="rest", run=2,    progress_target=250,
               progress_unit="volumes", xml_name="rest.xml", kind=StepKind.DICOM_SCAN),
)

RT15: tuple[StepConfig, ...] = (
    StepConfig(name="Setup",         task=None,          run=None, progress_target=0,
               progress_unit="stages",  xml_name=None,        kind=StepKind.SETUP),
    StepConfig(name="2-volume",      task="2vol",        run=1,    progress_target=2,
               progress_unit="volumes", xml_name="2vol.xml",  kind=StepKind.VSEND_SCAN),
    StepConfig(name="Transfer Pre",  task="transferpre", run=1,    progress_target=150,
               progress_unit="volumes", xml_name="rtdmn.xml", kind=StepKind.NF_RUN, feedback=False),
    *_feedback_block(start_run=1),   # Feedback 1-5
    StepConfig(name="Transfer Post", task="transferpost", run=1,   progress_target=150,
               progress_unit="volumes", xml_name="rtdmn.xml", kind=StepKind.NF_RUN, feedback=False),
)
# RT15 has 9 steps: Setup, 2vol, TransferPre, Fb1-5, TransferPost.

RT30: tuple[StepConfig, ...] = (
    *RT15[:-1],                # Setup through Feedback 5
    StepConfig(name="Transfer Post 1", task="transferpost", run=1, progress_target=150,
               progress_unit="volumes", xml_name="rtdmn.xml", kind=StepKind.NF_RUN, feedback=False),
    *_feedback_block(start_run=6),   # Feedback 6-10
    StepConfig(name="Transfer Post 2", task="transferpost", run=2, progress_target=150,
               progress_unit="volumes", xml_name="rtdmn.xml", kind=StepKind.NF_RUN, feedback=False),
)
# RT30 has 15 steps: Setup, 2vol, TransferPre, Fb1-5, TransferPost1, Fb6-10, TransferPost2.

def _fsl_stage(name: str, task: str, fsl_command: str) -> StepConfig:
    return StepConfig(
        name=name, task=task, run=None,
        progress_target=100, progress_unit="percent",
        xml_name=None, kind=StepKind.PROCESS_STAGE, fsl_command=fsl_command,
    )

PROCESS: tuple[StepConfig, ...] = (
    StepConfig(name="Setup + select", task=None, run=None, progress_target=1,
               progress_unit="stages", xml_name=None, kind=StepKind.SETUP),
    # ↑ Interactive: preflight + operator picks which rest runs to process.
    #   Selected run list stored in StepOutcome.artifacts["selected_runs"].
    _fsl_stage("Merge rests", "merge",    fsl_command="fslmerge"),
    _fsl_stage("MELODIC ICA", "melodic",  fsl_command="melodic"),
    _fsl_stage("Extract DMN", "dmn_mask", fsl_command="extract_dmn"),
    _fsl_stage("Extract CEN", "cen_mask", fsl_command="extract_cen"),
    _fsl_stage("Register",    "register", fsl_command="flirt_applywarp"),
    StepConfig(name="QC", task="qc", run=None, progress_target=1,
               progress_unit="stages", xml_name=None, kind=StepKind.PROCESS_STAGE,
               fsl_command="qc_visualize"),
)

SESSION_CONFIGS: dict[str, tuple[StepConfig, ...]] = {
    "loc3": LOC3, "rt15": RT15, "rt30": RT30, "process": PROCESS,
}
```

Exact step counts and BIDS naming derived from `materials/mri_sequences/LOC3.pdf`, `RT15.pdf`, `RT30.pdf`. The `PROCESS` pipeline mirrors the existing `orchestration/ica.py` stages.

## Components

### `StepExecutor` protocol (new file: `mindfulness_nf/orchestration/executor.py`)

Every step kind (VSEND_SCAN, DICOM_SCAN, NF_RUN, PROCESS_STAGE) is handled by an implementation of the same `StepExecutor` protocol. `SessionRunner` stays kind-agnostic: it dispatches to the right executor based on `StepConfig.kind`. This is what lets Process, Localizer, RT15, and RT30 share one orchestration framework.

The full Protocol definition lives in `mindfulness_nf/orchestration/executor.py`.

Concrete implementations:

- `SetupStepExecutor`: runs `orchestration.preflight.run_preflight()` and reports per-check progress. `StepOutcome.succeeded = all(r.passed for r in results)`. No subprocesses, no components. `StepConfig.kind == SETUP`.
- `VsendStepExecutor`: launches MURFI via Apptainer, then calls `ScannerSource.push_vsend()`. Progress unit = "volumes", target = 2. Components = `("murfi",)`.
- `DicomStepExecutor`: launches MURFI + DICOM receiver, then calls `ScannerSource.push_dicom()`. Progress unit = "volumes". Components = `("murfi", "dicom")`.
- `NfRunStepExecutor`: **Phase 1 (MURFI):** same as Dicom/Vsend flow to collect `progress_target` volumes (phase="murfi", unit="volumes"). When target reached, emits a progress update with `detail="Press D to start PsychoPy"` and waits on an internal asyncio.Event set by `advance_phase()`. **Phase 2 (PsychoPy):** launches PsychoPy while MURFI keeps serving activations. PsychoPy's exit is the step's completion signal; scale factor extracted from PsychoPy CSV goes into `StepOutcome.artifacts["scale_factor"]`. Components = `("murfi", "psychopy")`.
- `FslStageExecutor`: launches one FSL command via subprocess. Progress unit = "percent" (parsed from stderr for MELODIC) or "stages" (binary for simpler commands). Validates on completion that the expected output file exists in `derivatives/`. Components = `()`. The specific subcommands (`fslmerge`, `melodic`, `flirt`, `applywarp`) are dispatched inside the executor based on `StepConfig.fsl_command`.

The "Select sources" step in PROCESS is handled as a SETUP-kind step with interactive selection (not an FSL command): the operator picks which rest runs to include, results stored in `StepOutcome.artifacts["selected_runs"]` for downstream stages to consume.

### `SessionRunner` (new file: `mindfulness_nf/orchestration/session_runner.py`)

```python
class SessionRunner:
    """Coordinates SessionState with step executors and scanner source."""

    def __init__(
        self,
        state: SessionState,
        subject_dir: Path,                    # subjects/sub-001/ses-rt15/
        pipeline: PipelineConfig,
        scanner_config: ScannerConfig,
        scanner_source: ScannerSource,        # Real | Simulated
    ) -> None: ...

    @classmethod
    def load_or_create(
        cls,
        subject_dir: Path,
        session_type: str,
        pipeline: PipelineConfig,
        scanner_config: ScannerConfig,
        scanner_source: ScannerSource,
    ) -> SessionRunner:
        """If session_state.json exists, load and coerce running→failed.
        Otherwise, create a fresh SessionState from SESSION_CONFIGS."""

    # --- Cursor navigation (persists state; no subprocess interaction) ---
    def advance(self) -> None       # cursor forward; auto-chain start if new is pending
    def go_back(self) -> None       # cursor back; never touches running step
    def select(self, i: int) -> None   # jump; never touches running step

    # --- Step execution (I/O) -------------------------------------------
    async def start_current(self) -> None:
        """Construct StepExecutor for the cursor step, start its run().

        Refuses (no-op with notification) if another step is currently
        running (self._current_task exists and is not done). This is the
        single enforcement point for the "at most one running step"
        invariant.

        Wraps `executor.run(on_progress)` in asyncio.create_task, stores
        as self._current_task. A supervisor coroutine awaits the task:
            • on StepOutcome(succeeded=True) → state.mark_completed
            • on StepOutcome(succeeded=False) → state.mark_failed(error=...)
            • on unhandled exception → state.mark_failed(error=traceback),
              log full trace (programming bug, not operational failure)
        """
    async def stop_current(self) -> None                # executor.stop()
    async def interrupt_current(self) -> None           # stop + clear data
    async def clear_and_restart_current(self) -> None   # stop + clear + start

    async def advance_phase_current(self) -> None:
        """Called when operator presses D on a running multi-phase step
        to proceed past a phase gate (e.g., MURFI → PsychoPy in NF_RUN).
        Delegates to current executor's `advance_phase()`.
        No-op if no step is running or step is single-phase."""

    # --- Component-level controls (M and P keys) ------------------------
    async def relaunch_component(self, component: str) -> None:
        """Delegates to `self._current_executor.relaunch(component)`.

        Valid only while the current step's status is `running`. On any
        other status, emits a notification ("M is only valid during a
        running step") and returns.

        The TUI hides M/P keys unless:
            state.current.status == RUNNING
            and component in self.available_components.
        """

    # --- Observability --------------------------------------------------
    def subscribe(self, cb: Callable[[SessionState], None]) -> None
    @property
    def state(self) -> SessionState
    @property
    def available_components(self) -> tuple[str, ...]:
        """Pass-through to current executor's components() or ()."""

    # --- Internals ------------------------------------------------------
    # Instance attributes (set by __init__; mutated only by the methods above):
    #   self._state: SessionState
    #   self._current_task: asyncio.Task | None   # non-None iff a step is running
    #   self._current_executor: StepExecutor | None
    #   self._subscribers: list[Callable[[SessionState], None]]

    def _executor_for(self, step: StepConfig) -> StepExecutor:
        """Dispatch on StepKind to the right concrete executor,
        passing all required deps (scanner_source, configs, subject_dir)."""
    def _apply(self, new_state: SessionState) -> None:
        """Atomic: persist JSON, update self._state, notify subscribers."""
```

### TUI screen lifecycle and runner cleanup

`SessionScreen` owns exactly one `SessionRunner` instance. When the screen is dismissed (e.g., operator navigates back to `SessionSelectScreen`, or the app exits), the screen's `on_unmount` handler MUST `await runner.stop_current()` before returning. Otherwise the in-flight `asyncio.Task` gets orphaned: its subprocesses keep running, but no one is listening to their progress, and the state file no longer updates.

`esc` handling goes through this same path (prompt-then-stop-then-exit) rather than tearing the screen down abruptly.

### Subprocess ownership — per-executor, not a central `ProcessGroup`

Each concrete executor owns any subprocesses it launches. The runner never holds subprocess handles directly. This means:

- **No central health-poll task.** The executor's `run()` coroutine detects its own subprocess exits (via `await process.wait()` or similar) and returns `StepOutcome(succeeded=False, error="MURFI exited 1")` accordingly. The runner simply `await`s the `asyncio.Task` wrapping `run()` and checks `task.done()` / `task.result()` — reusing asyncio's built-in machinery rather than mirroring it.
- **M/P keys go through `relaunch(component)`.** The TUI's M key calls `runner.relaunch_component("murfi")`, which delegates to `self._current_executor.relaunch("murfi")`. The executor knows how to stop and restart only the named subprocess while keeping progress intact.
- **Shutdown is one call.** `await runner.stop_current()` calls `current_executor.stop()`, which tears down everything the executor launched.

### MURFI log handling on relaunch

When `m` relaunches MURFI mid-step, partial volumes are already on disk (good — that's the whole point of M vs R) but the old MURFI's log file lines remain. Without explicit handling, the volume watcher would either double-count (if it re-scans the whole log) or reset to zero (if it starts fresh). Neither is acceptable.

The executor's relaunch policy:
1. Before stopping old MURFI, record the current log byte offset as `log_baseline`.
2. Stop old MURFI (graceful, SIGTERM → SIGKILL).
3. Start new MURFI (appending to the same log).
4. Volume watcher resumes reading from `log_baseline`. It counts only lines after that offset. The executor keeps `progress_current` continuous — new volumes from the relaunched MURFI continue from the previous count.

This is internal to the executor; neither the Protocol nor SessionRunner need to know.

A small `ManagedProcess` helper (a thin wrapper around `asyncio.subprocess.Process` with SIGTERM→SIGKILL semantics) lives in `orchestration/_process.py` and is used inside each executor. It is NOT part of the SessionRunner's public surface.

### `ScannerSource` (new file: `mindfulness_nf/orchestration/scanner_source.py`)

```python
class ScannerSource(Protocol):
    async def push_vsend(
        self, xml_path: Path, subject_dir: Path, step: StepConfig
    ) -> None: ...
    async def push_dicom(
        self, target_host: str, target_port: int, ae_title: str, step: StepConfig
    ) -> None: ...
    async def cancel(self) -> None: ...

class RealScannerSource:
    """No-op: real scanner pushes on its own; we just wait."""

class SimulatedScannerSource:
    """Replays cached volumes at configured TR."""

    cache_dir: Path          # murfi/dry_run_cache/ — sibling of murfi/subjects/, gitignored
    tr_seconds: float = 1.2
    # For VSEND_SCAN: shell out to `vSend` on cached NIfTIs.
    # For DICOM_SCAN: shell out to `dcmsend` on cached DICOMs.
```

`ScannerSource.cancel()` is invoked by the executor when `stop()` is called mid-push — it terminates any in-flight `vSend` or `dcmsend` subprocess the source has spawned, so the overall step-stop completes within its 5-second budget.

A one-time script (`scripts/populate_dry_run_cache.py`) primes the cache by copying one real session's `sourcedata/murfi/img/` to the cache directory.

## Data flow

### D key dispatch — three cases, dispatched by the TUI

The TUI reads `state.current.status` and `state.current.awaiting_advance` to pick which runner method to call.

**Case 1 — D on `pending`** (operator starts the step):

```
SessionScreen  ──D──▶  Runner.start_current()
                                    │
                                    ├─▶ if self._current_task and not done: refuse + notify
                                    ├─▶ executor = Runner._executor_for(step)
                                    ├─▶ self._current_executor = executor
                                    ├─▶ state.mark_running(cursor, ts) + persist + notify
                                    ├─▶ task = asyncio.create_task(
                                    │         executor.run(on_progress=Runner._on_progress))
                                    ├─▶ self._current_task = task
                                    └─▶ supervisor coroutine awaits task:
                                            try:
                                                outcome = await task
                                            except Exception as bug:
                                                # Programming error, not operational:
                                                log_trace(bug)
                                                state.mark_failed(cursor, ts,
                                                    error=f"internal error: {bug!r}")
                                            else:
                                                if outcome.succeeded:
                                                    state.mark_completed(cursor, ts,
                                                        artifacts=outcome.artifacts)
                                                else:
                                                    state.mark_failed(cursor, ts,
                                                        error=outcome.error)
                                            persist + notify
                                            self._current_task = None
                                            self._current_executor = None
```

**Case 2 — D on `running` with `awaiting_advance=True`** (operator gates MURFI→PsychoPy):

```
SessionScreen  ──D──▶  Runner.advance_phase_current()
                           └─▶ self._current_executor.advance_phase()
                               (executor's internal Event gets set; Phase 2 begins;
                                on_progress resumes firing with phase="psychopy",
                                awaiting_advance=False)
```

**Case 3 — D on `completed`** (operator moves to next step):

```
SessionScreen  ──D──▶  Runner.advance()              (pure cursor move + chain start)
                           ├─▶ state.advance()       (cursor+=1; clamped at last step)
                           ├─▶ persist + notify
                           │
                           └─▶ if new cursor step is pending AND no other step running:
                                  auto-call Runner.start_current()
                                  (preserves the current "one D press = move on" UX)
```

To inspect a pending step without starting it, the operator uses `n`/`→` or `b`/`←` — these are pure cursor navigation and never invoke `start_current`.

**Progress updates during any running step:**

```
on_progress(p: StepProgress)  ──▶  state.set_progress(
                                        cursor, p.value,
                                        detail=p.detail,
                                        phase=p.phase,
                                        awaiting_advance=p.awaiting_advance,
                                    )
                                        ├─▶ persist + notify

SessionScreen  ◀── notify(new_state) ── re-renders from new_state
```

The runner does not run a separate health-poll task. The executor's `run()` coroutine detects subprocess exits internally and returns `StepOutcome(succeeded=False, error=...)`, which the supervisor coroutine converts to `mark_failed`.

The TUI holds no state beyond "the last SessionState I was notified about." All UI updates are a function of the received state.

## Keybindings and error handling

### Keybindings

Behavior depends on the current step's status.  The TUI help bar shows only keys that are valid right now.

| Key | When valid | Action |
|---|---|---|
| `d` | status=pending | Start the step (`runner.start_current()`). |
| `d` | status=running, `awaiting_advance=True` | Advance phase (`runner.advance_phase_current()`). Cursor unchanged. |
| `d` | status=running, `awaiting_advance=False` | No-op — completion is automatic when `run()` returns `StepOutcome`. |
| `d` | status=completed | `runner.advance()` — move cursor forward. If the new cursor step is `pending` AND no other step is running, auto-call `start_current()` so one D press moves the session on. |
| `d` | status=failed | No-op. Help bar reads "FAILED — press R to redo, I to clear to pending, or N/→ to move cursor away (step stays failed; session cannot complete until resolved)". |
| `r` | cursor step is not running AND no other step is running | Restart step at cursor: clear this step's files, reset per-attempt state, increment attempts, start fresh. **On status=completed, prompts for confirmation ("Clear and re-run this completed step?").** |
| `r` | cursor step is running | Clear + restart in place (stop → clear → start). |
| `r` | cursor ≠ running step AND another step is running | Refused with notification "another step is running — interrupt it first or navigate to it." |
| `i` | any step is running (anywhere) | Interrupt: stop the running step's processes, clear its partial data, mark it pending. Cursor unchanged. (Note: `i` targets the running step, not the cursor step.) |
| `i` | no step running AND cursor step is failed | Clear cursor step's partial data, mark pending (no restart). Lets operator tidy a failed step without retrying. |
| `i` | no step running AND cursor is pending or completed | No-op with notification "nothing to interrupt". |
| `b` / `←` | any | Pure cursor move backward. Never touches the running step. |
| `n` / `→` | any | Pure cursor move forward. |
| `g` | any | Prompt for step number; jump cursor. |
| `m` | status=running AND "murfi" in `executor.components()` | Relaunch MURFI: stop and restart that subprocess only, keep all data, keep progress. |
| `p` | status=running AND "psychopy" in `executor.components()` | Relaunch PsychoPy: same as `m` but for PsychoPy. |
| `esc` | any | Quit. If a step is running, first prompts "Stop current run and quit? (Y/N)" — on Y, calls `stop_current()` (marks step failed with error="cancelled"), then exits. |

### Help bar (contextual)

Bottom of screen shows only the actions that are valid for the current `(step_status, cursor_position, running_index)` triple. Example mid-Feedback 3:

```
▶ Feedback 3  (running, 87/150 vols)
  [i] Interrupt   [m] Relaunch MURFI   [b/n] Navigate
```

(Note: no `[d]` while `awaiting_advance=False` on a running step — completion is automatic; D becomes valid again when the step completes or reaches a phase gate.)

### Recovery scenarios (all reproduced by integration tests — see Testing)

**A. MURFI dies mid-scan at vol 50/150 of Feedback 2.**

- Executor's `run()` sees the MURFI subprocess exit with non-zero returncode, returns `StepOutcome(succeeded=False, error="MURFI exited 1")`.
- Runner's supervisor coroutine awaits the task, receives the outcome, calls `state.mark_failed(idx, ts=now_iso(), error=outcome.error)`, persists, notifies.
- TUI: *"Feedback 2 FAILED — MURFI exited at vol 50/150. Press R to clear & restart, M to relaunch MURFI only, → to skip."*
- Operator presses `r` → runner clears step files, increments `attempts`, marks pending, starts fresh.

**B. PsychoPy crashes after MURFI phase.**

- `NfRunStepExecutor.run()` detects PsychoPy subprocess exit (non-zero code). MURFI is left alive — it was not the cause of the failure, and keeping it serving activations means the operator can relaunch PsychoPy without losing the collected scan.
- Executor emits a progress update: `detail="PsychoPy crashed — press P to relaunch, R to restart the full run"`, `awaiting_advance=False`. **Step status stays `running`** (MURFI still healthy).
- TUI: help bar shows `[p] Relaunch PsychoPy`, `[r] Restart step`.
- Operator presses `p` → `executor.relaunch("psychopy")` → PsychoPy restarts; MURFI unchanged. When PsychoPy completes, the executor stops MURFI, extracts scale factor, returns `StepOutcome(succeeded=True, artifacts={"scale_factor": ...})`.
- If MURFI also dies while waiting, the executor returns `StepOutcome(succeeded=False, error="MURFI died while awaiting PsychoPy relaunch")`.

**C. Subject squeezes the panic bulb mid-scan.**

- Operator presses `i`. Runner calls `executor.stop()`, which (inside the executor) SIGTERMs every owned subprocess, then SIGKILLs after 5s. Runner then clears step files, marks pending.
- TUI: *"Feedback 3 interrupted. Data cleared. Press R to redo, → to skip, or Escape to end session."*

**D. MURFI started but no volumes arrive.**

- No automatic detection (scanner timing is operator-driven; false positives would be worse than none).
- Traffic light stays at `0/150` yellow. Operator chooses `m` (relaunch MURFI — useful if the container is in a bad state) or `i` (cancel), or waits.

**E. Operator started wrong step.**

- `i` interrupts, `b`/`n` navigates cursor, `d` or `r` starts the correct step.

### R vs M/P — the semantic distinction

- **`r` (Restart)** is destructive: stop processes, **delete this step's files**, increment attempts, mark pending, start fresh. Use when the data on disk is bad (MURFI crashed partway, subject moved, wrong sequence started).
- **`m` / `p` (Relaunch MURFI / PsychoPy)** is non-destructive: stop and restart only the named subprocess, **keep all data on disk**, keep received-volume count. Use when the process died but the already-captured volumes are fine (e.g., PsychoPy crashed after MURFI phase ended, or the MURFI container died but volumes on disk are valid).

### Invariants

1. **At most one step is `running` at any time.**  Enforced by `SessionRunner.start_current()`: if `self._current_task` exists and is not done, start_current refuses (notification-only, no exception).  This is the single enforcement point.
2. **`completed` requires per-kind validation.**  Validation happens inside each executor's `run()`.  Scan executors assert volume count matches `progress_target`.  FslStageExecutor asserts the expected output file exists in `derivatives/`.  SetupStepExecutor asserts all preflight checks passed.  The runner trusts `StepOutcome.succeeded` — it does not re-validate.
3. **`clear_and_restart` is transactional.**  Files removed first, then state updated, then restart.  File-removal failures don't touch state.
4. **State persistence is atomic.**  Temp file + rename (`os.replace`) — already implemented in `subjects.save_session_state`.
5. **`m`/`p` never delete data.**  Only `r` and `i` touch files.  `m`/`p` are valid only while status=`running`.
6. **Programming errors become failed steps with traces.**  The supervisor coroutine catches unhandled exceptions from `executor.run()`, logs full trace to MURFI-log-style file, and calls `state.mark_failed(error="internal error: ...")`. The runner itself does not crash; the operator sees a failed step and can press R.
7. **Cursor cannot point outside `steps`.**  `select(i)` clamps to `[0, len(steps))`.  `advance()` at the last step is a no-op (cursor stays).  `go_back()` at index 0 is a no-op.  Session completion is **derived**, not stored: a session is complete iff `all(s.status == COMPLETED for s in steps)`.  The TUI renders a completion view based on this property; no persistent `session_complete` flag exists.

## Resume behavior

On `SessionRunner.load_or_create`:

1. Look for `<subject_dir>/<session_dir>/session_state.json`.
2. If missing, build a fresh `SessionState` from `SESSION_CONFIGS[session_type]` and the current code's step configs.
3. If present:
   - Parse JSON. If `schema_version` is unknown, surface a clear error and refuse to load (operator can delete the file to start fresh).
   - For each step with `status=running`, set `status=failed` with `error="interrupted by restart"` (the process that was running is gone; we don't know if partial data on disk is valid). The `error` field is the right slot — `detail_message` is for progress narration during run, not terminal reasons.
   - Partial `.nii` files from the interrupted step are **left on disk**. The operator can inspect them; pressing `r` clears them. Automatic clearing on resume would be surprising.
   - Construct `SessionState` from the JSON's **persisted step configs** (not from `SESSION_CONFIGS` — self-contained resume, robust to code drift). Set `updated_at=now`, persist.
4. Render TUI from state. Operator lands on the recorded cursor.

**No confirmation prompt.** Resume is implicit when `(subject_id, session_type)` already has state; the operator can press `r` to clear any specific step, or delete the directory to start fully over. Rationale: fewer interactive prompts = fewer brittle paths.

## Dry-run mode

`uv run mindfulness-nf --dry-run [--subject <id>]`:

- `ScannerSource` → `SimulatedScannerSource` pointing at `murfi/dry_run_cache/` (**new dedicated directory, sibling of `murfi/subjects/`, gitignored**). The cache is populated once from a real session's `sourcedata/murfi/img/` via `scripts/populate_dry_run_cache.py <source_session_dir>`. If the cache is missing, the TUI refuses to start dry-run with a clear error telling the operator to run the populate script first.
- Subject dir defaults to `subjects/sub-dry-run/` but `--subject` can override.
- MURFI and PsychoPy launch normally (via Apptainer + python subprocess). The TUI does not know it is in dry-run mode.

This enables rehearsal of every protocol, crash recovery, resume, interrupt, navigation, and M/P relaunch without a scanner. Note: the cache location is deliberately NOT inside `subjects/`, so a blanket `rm -rf subjects/sub-*/` doesn't destroy dry-run capability.

## Testing — the executable guarantee

Running `pytest -v` must prove that the system handles every scenario in the operator checklist. Three layers; all must pass before any scanner session.

### Layer 1 — Pure state machine (`tests/test_session_state.py`)

Fast, exhaustive, no I/O. These tests run in milliseconds and catch logic regressions at commit time.

```python
# SessionState transitions (pure: each returns a new SessionState)
def test_advance_moves_cursor_forward_by_one(): ...
def test_advance_at_last_step_is_clamped_noop(): ...
def test_go_back_moves_cursor_backward_by_one(): ...
def test_go_back_at_index_zero_is_clamped_noop(): ...
def test_select_clamps_negative_and_out_of_range(): ...
def test_cursor_navigation_never_changes_step_status(): ...

# mark_* transitions
def test_mark_running_sets_status_and_last_started(): ...
def test_mark_running_refuses_when_another_step_already_running(): ...
def test_mark_completed_sets_last_finished_and_artifacts(): ...
def test_mark_completed_clears_prior_error_field(): ...
def test_mark_failed_from_running_records_error(): ...
def test_mark_failed_allowed_from_running_only(): ...

# clear_current / set_progress
def test_clear_current_resets_all_per_attempt_fields(): ...
def test_clear_current_increments_attempts(): ...
def test_clear_current_preserves_config(): ...
def test_clear_current_does_not_touch_other_steps(): ...
def test_set_progress_updates_value_detail_phase_awaiting(): ...

# Invariants as properties
def test_cursor_and_running_index_can_diverge(): ...
def test_running_index_is_unique(): ...            # property: ≤ 1 running at a time
def test_session_complete_derived_from_all_completed(): ...

# Property-based sweeps
@given(ops=lists(one_of(advance_op, back_op, select_op, clear_op, mark_op, set_progress_op)))
def test_invariants_hold_over_random_sequences(ops): ...
# Asserts, after any operation sequence:
#   - cursor ∈ [0, len(steps))
#   - at most one step has status=RUNNING
#   - every field's type matches its declaration (no str slipping into Phase)
```

### Layer 2 — Runner with mocked processes (`tests/test_session_runner.py`)

Medium speed; injects a `FakeStepExecutor` (or uses a real concrete executor with a `NoOpScannerSource`) that simulates subprocess behavior without launching real processes. `SessionRunner._executor_for` is monkey-patched in tests to return the fake.

```python
@pytest.fixture
def runner(tmp_path):
    return SessionRunner(
        state=fresh_state("rt15", tmp_path),
        subject_dir=tmp_path,
        pipeline=PipelineConfig.test_fixture(),
        scanner_config=ScannerConfig.test_fixture(),
        scanner_source=NoOpScannerSource(),
    )  # _executor_for monkey-patched in tests to return FakeStepExecutor

async def test_start_current_transitions_pending_to_running(runner): ...
async def test_start_current_refuses_when_another_step_running(runner): ...
async def test_progress_update_persists_to_json(runner): ...
async def test_completed_requires_per_kind_validation(runner, tmp_path): ...
async def test_murfi_crash_marks_step_failed_with_error_message(runner): ...
async def test_programming_error_in_run_becomes_failed_step_with_trace(runner): ...
async def test_relaunch_murfi_keeps_progress_if_running(runner): ...
async def test_relaunch_murfi_rejected_on_failed_step(runner): ...
async def test_relaunch_psychopy_rejected_on_vsend_step(runner): ...
async def test_advance_phase_signals_executor(runner): ...
async def test_advance_phase_is_noop_on_single_phase_executor(runner): ...
async def test_interrupt_clears_partial_nii_files(runner, tmp_path): ...
async def test_clear_and_restart_increments_attempts(runner): ...
async def test_state_persisted_atomically_after_every_transition(runner): ...
async def test_load_or_create_coerces_running_to_failed(runner, tmp_path): ...
async def test_load_or_create_preserves_cursor(runner, tmp_path): ...
async def test_load_or_create_rejects_unknown_schema_version(runner, tmp_path): ...
async def test_resume_uses_persisted_step_configs_not_current(runner, tmp_path): ...
async def test_resume_leaves_partial_data_on_disk(runner, tmp_path): ...
async def test_navigating_while_running_does_not_stop_process(runner): ...
```

### Layer 3 — End-to-end with Textual test harness and real-ish subprocesses (`tests/test_e2e_session.py`)

Slow (~seconds each), uses Textual's `App.run_test()` and the `SimulatedScannerSource`. Real MURFI and real PsychoPy are *not* required for these tests — they use a `FakeMurfiProcess` that writes a real-format MURFI log on disk and a `FakePsychoPyProcess` that writes a real-format CSV. This keeps the tests CI-runnable while still exercising the TUI + Runner + StepExecutor chain end-to-end.

Every item from the operator checklist becomes a test. Naming convention: one test per row of the checklist.

```python
async def test_full_rt15_session_completes_green():
    """Golden path: dry-run RT15 from Setup through Transfer Post,
    all steps green, session_state.json shows all completed."""

async def test_resume_after_force_quit_lands_on_same_cursor():
    """Write a half-completed session_state.json; launch TUI with
    matching subject/session; verify cursor and statuses match;
    any 'running' status becomes 'failed'."""

async def test_interrupt_mid_feedback_clears_data_and_keeps_cursor():
    """Start Feedback 2, wait until 50 volumes, press I, verify
    func/*.nii for this run is deleted, state=pending, cursor
    unchanged, other steps untouched."""

async def test_murfi_crash_mid_feedback_marks_step_failed_and_r_restarts():
    """Start Feedback 2, simulate MURFI crash at vol 50, verify
    state=failed, press R, verify processes restart and new volumes
    are received."""

async def test_psychopy_crash_after_murfi_phase_marks_step_failed():
    """Start Feedback 2, let MURFI phase complete, simulate PsychoPy
    crash, verify state=failed and MURFI stopped."""

async def test_back_and_rerun_feedback_1_leaves_feedback_2_3_untouched():
    """Complete Feedback 1-3, press B twice to go to Feedback 1,
    press R, verify Feedback 1 data cleared and re-running, and
    Feedback 2 & 3 statuses still 'completed'."""

async def test_manual_murfi_relaunch_via_m_key():
    """Start a step, press M, verify MURFI subprocess PID changes
    but step status remains running and volume count continues via
    log_baseline offset tracking."""

async def test_manual_psychopy_relaunch_via_p_key():
    """In PsychoPy phase, press P, verify PsychoPy process restarts
    and MURFI keeps serving activations."""

async def test_m_key_does_not_delete_partial_volumes():
    """Start a step, produce 50 .nii files, press M, verify all 50
    files still on disk and progress_current==50 in state."""

async def test_r_key_deletes_partial_volumes_and_increments_attempts():
    """Start a step, produce 50 .nii files, press R, verify files gone,
    progress_current==0, attempts incremented."""

async def test_m_on_failed_step_is_rejected_with_notification():
    """Simulate MURFI crash at 50 volumes → status=failed. Press M;
    verify M is rejected with 'only valid during running step'; use R instead."""

async def test_r_on_completed_step_prompts_confirmation():
    """Complete Feedback 1. Navigate back to it. Press R. Verify a
    confirmation dialog appears. On Yes: data cleared, status=pending, restart.
    On No: no changes."""

async def test_escape_mid_run_prompts_and_marks_cancelled():
    """Start Feedback 2 (running). Press Escape. Verify prompt 'Stop and quit?'
    On Yes: executor.stop() called, step marked failed with error='cancelled',
    state persisted, TUI exits."""

async def test_advance_phase_triggers_psychopy_launch_in_nf_run():
    """Start Feedback 2. Let MURFI phase reach 150/150 volumes. Verify
    on_progress reports detail='Press D to start PsychoPy'. Press D.
    Verify PsychoPy subprocess launches and MURFI keeps running."""

async def test_advance_phase_before_murfi_complete_is_ignored():
    """Start Feedback 2. At 50/150 volumes, press D. Verify advance_phase
    was called but executor's internal guard ignores it (MURFI phase not
    yet done). Progress continues as normal."""

async def test_rt30_config_has_15_steps_with_13_feedback_phase_runs():
    """Assert RT30 config exposes exactly 15 StepConfig entries:
    Setup + 2vol + 13 feedback-phase runs (TransferPre, Fb1-5,
    TransferPost1, Fb6-10, TransferPost2)."""

async def test_bids_naming_matches_scanner_pdf():
    """For each session type, assert that on completion each run's
    BIDS filename matches the scanner PDF task/run labels."""

async def test_scanner_simulator_pushes_vsend_and_dicom():
    """Spin up the simulator against a FakeMurfi that logs every
    received volume; verify vSend path writes 2 volumes and DICOM
    path writes the expected count at TR cadence."""

async def test_concurrent_running_is_prevented():
    """While Feedback 2 is running, navigate to Feedback 3, press D;
    verify an 'interrupt first' message appears and no second process
    launches."""

async def test_partial_scan_fails_without_force_complete():
    """Complete a step with only 140/150 .nii files on disk; verify
    the executor returns StepOutcome(succeeded=False, error=...)
    and the step is marked failed. Operator must press R to retry."""

async def test_fresh_vs_load_chosen_by_json_presence():
    """No session_state.json → fresh SessionState from SESSION_CONFIGS.
    With session_state.json → load and reconstruct from persisted configs."""

async def test_d_on_completed_advances_and_auto_starts_next_pending():
    """Feedback 1 completes cleanly. Press D. Verify cursor moves to
    Feedback 2 AND Feedback 2 transitions to running (one D press,
    not two)."""

async def test_d_on_completed_last_step_is_noop():
    """Final step of RT15 (Transfer Post) completes. Press D. Verify
    cursor stays at last step, no new step starts (clamped)."""

async def test_r_refused_when_cursor_not_running_step():
    """Feedback 2 is running. Navigate cursor to completed Feedback 1.
    Press R. Verify a notification 'another step is running' appears
    and Feedback 1 data is NOT cleared."""

async def test_interrupt_targets_running_step_regardless_of_cursor():
    """Feedback 2 is running. Navigate cursor to pending Feedback 5.
    Press I. Verify Feedback 2 (the running step) is stopped and
    cleared — not Feedback 5."""

async def test_screen_unmount_calls_stop_current():
    """Start a step, dismiss SessionScreen programmatically. Verify
    runner.stop_current() was awaited before the screen tore down,
    and the subprocess is no longer alive."""

async def test_process_session_runs_all_fsl_stages():
    """Golden path: dry-run PROCESS session from Setup through QC,
    all stages green, derivatives/masks/{DMN,CEN}.nii created."""

async def test_process_melodic_failure_marks_stage_failed():
    """Simulate FSL MELODIC exiting with non-zero return code.
    Verify stage=failed, R clears intermediate outputs and restarts."""

async def test_process_resume_skips_completed_stages():
    """Start PROCESS, complete Merge + MELODIC, force-quit. Resume;
    verify cursor lands at Extract DMN, prior stages stay completed,
    no re-run of MELODIC."""
```

### Test doubles

- `FakeMurfiProcess`: a coroutine-backed fake that writes `log/murfi.log` with real MURFI log lines at a configurable cadence. Exposes `simulate_crash(exit_code)` and `simulate_volume(n)`.
- `FakePsychoPyProcess`: writes a real PsychoPy CSV including scale factor. Exposes `simulate_crash()`, `simulate_complete()`.
- `FakeStepExecutor`: implements the `StepExecutor` Protocol. Drives fake `run()` lifecycle for tests: `simulate_volume(n)`, `simulate_crash(exit_code)`, `simulate_phase_change("psychopy")`. Supports `relaunch(component)` tracking.
- `NoOpScannerSource`: does nothing (used when the fakes simulate volumes directly).
- `SimulatedScannerSource` (the real dry-run implementation): pushes from a cache directory; also used in L3 tests.

### CI integration

- Layer 1 + Layer 2 run on every push (fast, deterministic).
- Layer 3 runs before any scanner session via `scripts/preflight_test.sh`. The script fails loudly if any L3 test fails, with a big red banner: *"DO NOT PROCEED TO SCANNER — integration tests failed."*
- The L3 suite completes in under 2 minutes on the operator's workstation.

### Manual smoke test (not a replacement for automated tests, a complement)

Before scanner session:
```bash
uv run pytest -v                    # all 3 layers
uv run mindfulness-nf --dry-run     # walk through one RT15 by hand
```

## Migration

Existing test subjects (`sub-002`, `sub-3421`, `sub-kym`, `sub-test`) contain no meaningful data. No migration required; they can be deleted or ignored. New subjects are created directly in BIDS layout by the updated `subjects.create_subject()`.

## Out of scope

- A post-hoc BIDS validator. The layout is BIDS-shaped but we are not going to run bids-validator as part of CI in this iteration.
- Multi-subject parallelism. One subject, one session, one TUI instance.
- Retro-fitting the subject-entry and session-select screens into a single unified routing model. They stay as they are (`subject_entry.py`, `session_select.py`), though the session-select list gains a `process` option.
