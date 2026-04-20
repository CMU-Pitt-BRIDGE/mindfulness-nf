# SessionRunner: Resilient session orchestration with BIDS layout, resume, and dry-run

**Date:** 2026-04-20
**Status:** Approved
**Python:** 3.13+
**Supersedes parts of:** `2026-03-20-textual-tui-design.md` (the FCIS architecture stays; per-session screen classes are replaced)

## Problem

The current TUI implements a forward-only wizard whose state lives in Python instance variables on each `Screen` subclass. This causes four failure modes the operator cannot recover from:

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
                       │ clear_current / restart_murfi / restart_psychopy
                       │ interrupt
┌──────────────────────▼──────────────────────────┐
│ SessionRunner   (orchestration/session_runner)  │
│  ─ IMPERATIVE SHELL ────────────────────────────│
│  • owns MURFI / PsychoPy / DICOM process handles│
│  • watches disk (MURFI log, func/*.nii) and     │
│    calls pure core on each event                │
│  • persists SessionState after every transition │
│  • swaps ScannerSource: Real vs Simulated       │
└──┬─────────────────────┬────────────────────┬───┘
   │                     │                    │
┌──▼──────────────┐ ┌────▼──────────┐ ┌──────▼──────────┐
│ SessionState    │ │ ProcessGroup  │ │ ScannerSource   │
│ (models.py,     │ │ (supervisor)  │ │ Protocol:       │
│  frozen, pure)  │ │ • murfi_proc  │ │  • push_vsend() │
│ • steps[]       │ │ • psypy_proc  │ │  • push_dicom() │
│ • cursor        │ │ • dicom_proc  │ │ Real | Simulated│
│ • status per    │ │ • restart_*() │ │                 │
│   step (pending │ │ • is_alive()  │ │                 │
│   running, done,│ │ • health_task │ │                 │
│   failed)       │ │               │ │                 │
└─────────────────┘ └───────────────┘ └─────────────────┘
```

### Properties

- **FCIS honored.** `SessionState` is `frozen=True, slots=True` like everything else in `models.py`; all transitions return a new instance. The TUI holds no state other than "which state did the runner notify me about."
- **Single `SessionScreen`.** Localizer, RT15, RT30 become *data* (step lists in `sessions.py`), not separate screen classes. `LocalizerScreen`, `NeurofeedbackScreen`, and `TestScreen` are deleted.
- **Resume is automatic.** On session start, `SessionRunner.__init__` checks for an existing `session_state.json` and loads it. Any step with `status=running` at load time is coerced to `failed`, because the process that was running is gone.
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

```json
{
  "subject": "sub-001",
  "session_type": "rt15",
  "created_at": "2026-04-20T14:02:00Z",
  "updated_at": "2026-04-20T14:37:00Z",
  "schema_version": 1,
  "cursor": 4,
  "steps": [
    {"name": "Setup",        "task": null,          "run": null, "expected": 0,   "status": "completed", "attempts": 1, "received": 0,   "last_started": "...", "last_finished": "..."},
    {"name": "2-volume",     "task": "2vol",        "run": 1,    "expected": 2,   "status": "completed", "attempts": 1, "received": 2},
    {"name": "Transfer Pre", "task": "transferpre", "run": 1,    "expected": 150, "status": "completed", "attempts": 2, "received": 150},
    {"name": "Feedback 1",   "task": "feedback",    "run": 1,    "expected": 150, "status": "completed", "attempts": 1, "received": 150},
    {"name": "Feedback 2",   "task": "feedback",    "run": 2,    "expected": 150, "status": "failed",    "attempts": 1, "received": 87}
  ]
}
```

- `status` ∈ `{pending, running, completed, failed}`.
- A step is only persisted as `completed` after `validate_step_data()` confirms the disk matches `expected`.
- `attempts` increments each time `clear_and_restart_current` runs.
- `cursor` is decoupled from execution: it tracks where the *operator is looking*, not what is running.

### Python models (extensions to `mindfulness_nf/models.py`)

```python
class StepStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class StepKind(enum.Enum):
    SETUP = "setup"          # preflight-only; no MURFI/DICOM
    VSEND_SCAN = "vsend"     # 2vol calibration via vSend
    DICOM_SCAN = "dicom"     # resting state via DICOM receiver
    NF_RUN = "nf_run"        # feedback/transfer runs (MURFI + PsychoPy)

@dataclass(frozen=True, slots=True)
class StepConfig:
    name: str
    task: str | None             # BIDS task label, e.g., "feedback"
    run: int | None              # BIDS run number, 1-indexed
    expected_volumes: int
    xml_name: str | None         # MURFI template, e.g., "rtdmn.xml"
    kind: StepKind
    feedback: bool = False       # only relevant for NF_RUN

@dataclass(frozen=True, slots=True)
class StepState:
    config: StepConfig
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    received_volumes: int = 0
    last_started: str | None = None   # ISO-8601 UTC
    last_finished: str | None = None

@dataclass(frozen=True, slots=True)
class SessionState:
    subject: str
    session_type: str            # "loc3" | "rt15" | "rt30"
    cursor: int
    steps: tuple[StepState, ...]
    created_at: str
    updated_at: str

    # All transitions are pure: return a new SessionState.
    def advance(self) -> SessionState
    def go_back(self) -> SessionState
    def select(self, i: int) -> SessionState
    def mark_running(self, i: int, ts: str) -> SessionState
    def mark_completed(self, i: int, ts: str) -> SessionState
    def mark_failed(self, i: int, ts: str) -> SessionState
    def clear_current(self) -> SessionState  # status=pending, received=0, attempts+=1
    def set_volumes(self, i: int, n: int) -> SessionState
    @property
    def current(self) -> StepState  # steps[cursor]
    @property
    def running_index(self) -> int | None
```

### Session configurations (new file: `mindfulness_nf/sessions.py`)

```python
def _feedback_block(start_run: int, count: int = 5) -> tuple[StepConfig, ...]:
    """Return StepConfig for `count` consecutive feedback runs, starting at
    run number `start_run`. Each is 150 volumes via rtdmn.xml with feedback=True."""
    return tuple(
        StepConfig(
            name=f"Feedback {start_run + i}",
            task="feedback",
            run=start_run + i,
            expected_volumes=150,
            xml_name="rtdmn.xml",
            kind=StepKind.NF_RUN,
            feedback=True,
        )
        for i in range(count)
    )

LOC3: tuple[StepConfig, ...] = (
    StepConfig("Setup",  None,   None, 0,   None,       StepKind.SETUP),
    StepConfig("Rest 1", "rest", 1,    250, "rest.xml", StepKind.DICOM_SCAN),
    StepConfig("Rest 2", "rest", 2,    250, "rest.xml", StepKind.DICOM_SCAN),
)

RT15: tuple[StepConfig, ...] = (
    StepConfig("Setup",          None,           None, 0,   None,        StepKind.SETUP),
    StepConfig("2-volume",       "2vol",         1,    2,   "2vol.xml",  StepKind.VSEND_SCAN),
    StepConfig("Transfer Pre",   "transferpre",  1,    150, "rtdmn.xml", StepKind.NF_RUN, feedback=False),
    *_feedback_block(start_run=1),   # Feedback 1-5
    StepConfig("Transfer Post",  "transferpost", 1,    150, "rtdmn.xml", StepKind.NF_RUN, feedback=False),
)
# RT15 has 9 steps: Setup, 2vol, TransferPre, Fb1-5, TransferPost.

RT30: tuple[StepConfig, ...] = (
    *RT15[:-1],                # Setup through Feedback 5
    StepConfig("Transfer Post 1","transferpost", 1,    150, "rtdmn.xml", StepKind.NF_RUN, feedback=False),
    *_feedback_block(start_run=6),   # Feedback 6-10
    StepConfig("Transfer Post 2","transferpost", 2,    150, "rtdmn.xml", StepKind.NF_RUN, feedback=False),
)
# RT30 has 15 steps: Setup, 2vol, TransferPre, Fb1-5, TransferPost1, Fb6-10, TransferPost2.

SESSION_CONFIGS: dict[str, tuple[StepConfig, ...]] = {
    "loc3": LOC3, "rt15": RT15, "rt30": RT30,
}
```

Exact step counts and BIDS naming derived from `materials/mri_sequences/LOC3.pdf`, `RT15.pdf`, `RT30.pdf`.

## Components

### `SessionRunner` (new file: `mindfulness_nf/orchestration/session_runner.py`)

```python
class SessionRunner:
    """Coordinates SessionState with MURFI/PsychoPy/DICOM/scanner."""

    def __init__(
        self,
        state: SessionState,
        subject_dir: Path,                    # subjects/sub-001/ses-rt15/
        pipeline: PipelineConfig,
        scanner_config: ScannerConfig,
        scanner_source: ScannerSource,        # Real | Simulated
        process_group: ProcessGroup | None = None,
    ) -> None: ...

    @classmethod
    def load_or_create(
        cls, subject_dir: Path, session_type: str, *args, **kw
    ) -> SessionRunner:
        """If session_state.json exists, load and coerce running→failed.
        Otherwise, create a fresh SessionState from SESSION_CONFIGS."""

    # --- Pure navigation (no I/O beyond persisting state) ---------------
    def advance(self) -> None
    def go_back(self) -> None
    def select(self, i: int) -> None

    # --- Step execution (I/O) -------------------------------------------
    async def start_current(self) -> None
    async def stop_current(self) -> None
    async def interrupt_current(self) -> None         # stop + clear
    async def clear_and_restart_current(self) -> None # stop + clear + start

    # --- Process-level controls -----------------------------------------
    async def restart_murfi(self) -> None
    async def restart_psychopy(self) -> None
    async def stop_all(self) -> None

    # --- Observability --------------------------------------------------
    def subscribe(self, cb: Callable[[SessionState], None]) -> None
    @property
    def state(self) -> SessionState

    # --- Internals ------------------------------------------------------
    def _apply(self, new_state: SessionState) -> None:
        """Atomic: persist JSON, update self._state, notify subscribers."""
```

### `ProcessGroup` (new file: `mindfulness_nf/orchestration/process_group.py`)

```python
class ManagedProcess:
    name: str
    process: asyncio.subprocess.Process | None
    log_path: Path
    launched_at: float | None

    async def start(self) -> None: ...
    async def stop(self, timeout: float = 5.0) -> None: ...   # SIGTERM → SIGKILL
    def is_alive(self) -> bool: ...
    def returncode(self) -> int | None: ...
    async def wait(self) -> int: ...

class ProcessGroup:
    murfi: ManagedProcess
    psychopy: ManagedProcess | None
    dicom: ManagedProcess | None

    def health_check_task(self, on_unexpected_exit: Callable[[str, int], None]) -> asyncio.Task:
        """Poll returncodes every 0.5s. On unexpected non-None returncode
        while step is expected-running, call on_unexpected_exit(name, rc)."""
```

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

    cache_dir: Path          # e.g., subjects/sub-test/sourcedata/murfi/img/
    tr_seconds: float = 1.2
    # For VSEND_SCAN: shell out to `vSend` on cached NIfTIs.
    # For DICOM_SCAN: shell out to `dcmsend` on cached DICOMs (or
    #   convert cached NIfTIs → DICOMs once and cache that too).
```

A one-time script (`scripts/populate_dry_run_cache.py`) primes the cache by copying one real session's `sourcedata/murfi/img/` to the cache directory.

## Data flow

### Operator presses **D** on a pending Feedback 2

```
SessionScreen  ──D──▶  Runner.advance()
                           │
                           ├─▶ state.advance()          (new cursor)
                           ├─▶ persist session_state.json
                           ├─▶ notify subscribers
                           │
                           └─▶ Runner.start_current()
                                    │
                                    ├─▶ state.mark_running(cursor, ts)
                                    ├─▶ persist + notify
                                    ├─▶ murfi.configure_moco(...)
                                    ├─▶ process_group.murfi.start()
                                    └─▶ scanner_source.push_dicom(...)

VolumeWatcher (bg task)  ──▶  Runner.set_volumes(i, n)  ──▶  state.set_volumes()
                                                              ├─▶ persist + notify
ProcessHealthWatcher     ──▶  Runner._on_unexpected_exit(name)
                                                              ├─▶ state.mark_failed()
                                                              └─▶ persist + notify

SessionScreen  ◀── notify(new_state) ── re-renders from new_state
```

The TUI holds no state beyond "the last SessionState I was notified about." All UI updates are a function of the received state.

## Keybindings and error handling

### Keybindings

| Key | Action | Notes |
|---|---|---|
| `d` | Done / advance | If cursor=pending → start. If cursor=running with ≥expected volumes → complete + move cursor. |
| `r` | Restart step at cursor | Stop processes if running, clear this step's files, increment attempts, mark pending, then start. |
| `i` | Interrupt running step | Stop processes cleanly, clear partial data, mark pending. No auto-restart. Cursor unchanged. |
| `b` / `←` | Back (cursor) | Pure cursor move. Does not touch running step. |
| `n` / `→` | Next (cursor) | Pure cursor move. |
| `g` | Go to step # (prompt) | Jump cursor to arbitrary step index. |
| `m` | (Re)start MURFI | Works whether MURFI is dead or alive. If alive: graceful stop → start. **Does not clear step data** — use `r` for that. On a `failed` step, pressing `m` restarts MURFI and returns the step to `running` (keeping partial volumes). |
| `p` | (Re)start PsychoPy | Same as `m` but for PsychoPy. Only valid on NF runs. Does not clear data. |
| `esc` | Quit | Confirms if any process is alive. |

### Help bar (contextual)

Bottom of screen shows only the actions that are valid for the current `(step_status, cursor_position, running_index)` triple. Example mid-Feedback 3:

```
▶ Feedback 3  (running, 87/150 vols)
  [d] Done   [i] Interrupt   [m] Restart MURFI   [b/n] Navigate
```

### Recovery scenarios (all reproduced by integration tests — see Testing)

**A. MURFI dies mid-scan at vol 50/150 of Feedback 2.**

- `ProcessHealthWatcher` sees `returncode != 0`.
- Runner: `state.mark_failed(idx)`, stops PsychoPy if running, persists, notifies.
- TUI: *"Feedback 2 FAILED — MURFI exited at vol 50/150. Press R to clear & restart, M to relaunch MURFI only, → to skip."*
- Operator presses `r` → runner clears step files, increments `attempts`, marks pending, starts fresh.

**B. PsychoPy crashes after MURFI phase.**

- Runner stops MURFI too (existing `finally` block behavior), step marked `failed`.
- TUI: *"Feedback 2 PsychoPy crashed. Press R to restart both, or P to relaunch PsychoPy only."*

**C. Subject squeezes the panic bulb mid-scan.**

- Operator presses `i`. Runner stops MURFI + DICOM + PsychoPy cleanly (SIGTERM → SIGKILL after 5s), clears step files, marks pending.
- TUI: *"Feedback 3 interrupted. Data cleared. Press R to redo, → to skip, or Escape to end session."*

**D. MURFI started but no volumes arrive.**

- No automatic detection (scanner timing is operator-driven, false positives would be worse than none).
- Traffic light stays at `0/150` yellow. Operator chooses `m` (relaunch with different XML), `i` (cancel), or waits.

**E. Operator started wrong step.**

- `i` interrupts, `b`/`n` navigates cursor, `d` or `r` starts the correct step.

### R vs M/P — the semantic distinction

- **`r` (Restart)** is destructive: stop processes, **delete this step's files**, increment attempts, mark pending, start fresh. Use when the data on disk is bad (MURFI crashed partway, subject moved, wrong sequence started).
- **`m` / `p` (Relaunch MURFI / PsychoPy)** is non-destructive: stop and restart only the named subprocess, **keep all data on disk**, keep received-volume count. Use when the process died but the already-captured volumes are fine (e.g., PsychoPy crashed after MURFI phase ended, or the MURFI container died but volumes on disk are valid).

### Invariants

1. At most one step is `running` at any time. Starting a new step when another is running forces the operator to `i` first; the TUI surfaces this as a status message.
2. `completed` requires disk validation. `validate_step_data()` confirms volume count before the transition; a mismatch forces `yellow_confirmed` or `failed`.
3. `clear_and_restart` is transactional. Files removed first, then state updated, then restart. File-removal failures don't touch state.
4. State persistence is atomic. Temp file + rename (`os.replace`) — already in `subjects.save_session_state`.
5. `m`/`p` never delete data. Only `r` and `i` touch files.

## Resume behavior

On `SessionRunner.load_or_create`:

1. Look for `<subject_dir>/<session_dir>/session_state.json`.
2. If missing, build a fresh `SessionState` from `SESSION_CONFIGS[session_type]`.
3. If present:
   - Parse JSON.
   - For each step with `status=running`, set `status=failed` (the process that was running is gone).
   - Construct `SessionState`, set `updated_at=now`, persist.
4. Render TUI from state. Operator lands on the recorded cursor.

**No confirmation prompt.** Resume is implicit when `(subject_id, session_type)` already has state; the operator can press `r` to clear any specific step, or delete the directory to start fully over. Rationale: fewer interactive prompts = fewer brittle paths.

## Dry-run mode

`uv run mindfulness-nf --dry-run [--subject <id>]`:

- `ScannerSource` → `SimulatedScannerSource` pointing at `subjects/sub-test/sourcedata/murfi/img/`.
- Subject dir defaults to `subjects/sub-dry-run/` but `--subject` can override.
- MURFI and PsychoPy launch normally (via Apptainer + python subprocess). The TUI does not know it is in dry-run mode.

This enables rehearsal of every protocol, crash recovery, resume, interrupt, navigation, and M/P restart without a scanner.

## Testing — the executable guarantee

Running `pytest -v` must prove that the system handles every scenario in the operator checklist. Three layers; all must pass before any scanner session.

### Layer 1 — Pure state machine (`tests/test_session_state.py`)

Fast, exhaustive, no I/O. These tests run in milliseconds and catch logic regressions at commit time.

```python
def test_advance_from_pending_starts_step(): ...
def test_advance_on_running_below_threshold_noop(): ...
def test_advance_on_running_at_threshold_completes(): ...
def test_go_back_does_not_change_status(): ...
def test_select_out_of_range_noop(): ...
def test_clear_current_increments_attempts(): ...
def test_clear_current_resets_received_volumes(): ...
def test_clear_current_does_not_touch_other_steps(): ...
def test_mark_failed_from_running_only(): ...
def test_cursor_and_running_can_diverge(): ...
def test_at_most_one_step_running_invariant(): ...
# Property-based: for arbitrary sequences of pure transitions,
# invariants hold. (Uses hypothesis.)
@given(ops=lists(one_of(advance_op, back_op, clear_op, mark_op)))
def test_invariants_hold_over_random_sequences(ops): ...
```

### Layer 2 — Runner with mocked processes (`tests/test_session_runner.py`)

Medium speed; uses a `FakeProcessGroup` that simulates MURFI/PsychoPy behavior without launching real subprocesses.

```python
@pytest.fixture
def runner(tmp_path):
    return SessionRunner(
        state=fresh_state("rt15", tmp_path),
        subject_dir=tmp_path,
        scanner_source=NoOpScannerSource(),
        process_group=FakeProcessGroup(),
    )

async def test_start_current_transitions_pending_to_running(runner): ...
async def test_volume_update_persists_to_json(runner): ...
async def test_completed_requires_disk_validation(runner, tmp_path): ...
async def test_murfi_crash_marks_step_failed(runner): ...
async def test_restart_murfi_keeps_step_running_if_volumes_still_come(runner): ...
async def test_interrupt_clears_partial_nii_files(runner, tmp_path): ...
async def test_clear_and_restart_increments_attempts(runner): ...
async def test_state_persisted_atomically_after_every_transition(runner): ...
async def test_load_or_create_coerces_running_to_failed(runner, tmp_path): ...
async def test_load_or_create_preserves_cursor(runner, tmp_path): ...
async def test_navigating_while_running_does_not_stop_process(runner): ...
```

### Layer 3 — End-to-end with Textual test harness and real-ish subprocesses (`tests/test_e2e_session.py`)

Slow (~seconds each), uses Textual's `App.run_test()` and the `SimulatedScannerSource`. Real MURFI and real PsychoPy are *not* required for these tests — they use a `FakeMurfiProcess` that writes a real-format MURFI log on disk and a `FakePsychoPyProcess` that writes a real-format CSV. This keeps the tests CI-runnable while still exercising the TUI + Runner + ProcessGroup + VolumeWatcher stack end-to-end.

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

async def test_manual_murfi_restart_via_m_key():
    """Start a step, press M, verify MURFI subprocess PID changes
    but step status remains running and volume count continues."""

async def test_manual_psychopy_restart_via_p_key():
    """In PsychoPy phase, press P, verify PsychoPy process restarts
    and MURFI keeps serving activations."""

async def test_m_key_does_not_delete_partial_volumes():
    """Start a step, produce 50 .nii files, press M, verify all 50
    files still on disk and received_volumes==50 in state."""

async def test_r_key_deletes_partial_volumes_and_increments_attempts():
    """Start a step, produce 50 .nii files, press R, verify files gone,
    received_volumes==0, attempts incremented."""

async def test_m_on_failed_step_returns_to_running():
    """Simulate MURFI crash at 50 volumes → status=failed. Press M;
    verify MURFI relaunches, step returns to running, 50 volumes kept."""

async def test_rt30_session_uses_all_13_feedback_phase_runs():
    """Assert RT30 config exposes exactly 15 steps (Setup, 2vol,
    TransferPre, Fb1-5, TransferPost1, Fb6-10, TransferPost2)."""

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

async def test_validate_step_data_catches_missing_volumes():
    """Complete a step with only 140/150 .nii files on disk; verify
    mark_completed either blocks (red) or requires yellow-confirm."""

async def test_migrate_or_fresh_is_chosen_by_directory_presence():
    """No session_state.json → fresh state. With session_state.json
    → load state. Never a mix."""
```

### Test doubles

- `FakeMurfiProcess`: a coroutine-backed fake that writes `log/murfi.log` with real MURFI log lines at a configurable cadence. Exposes `simulate_crash(exit_code)` and `simulate_volume(n)`.
- `FakePsychoPyProcess`: writes a real PsychoPy CSV including scale factor. Exposes `simulate_crash()`, `simulate_complete()`.
- `FakeProcessGroup`: wraps the two fakes with the same interface as `ProcessGroup`.
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

- Retro-fitting `Process` session screen (ICA/FEAT pipeline) into the SessionRunner framework. It stays as a separate screen for now. Its data outputs will land in `derivatives/masks/` per BIDS, but its state machine is simpler and not the source of the stakeholder-trust problem.
- A post-hoc BIDS validator. The layout is BIDS-shaped but we are not going to run bids-validator as part of CI in this iteration.
- Multi-subject parallelism. One subject, one session, one TUI instance.
