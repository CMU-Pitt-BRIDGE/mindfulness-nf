"""Layer 2 tests: SessionRunner with mocked step executors.

TDD red: the imports below (SessionRunner, NoOpScannerSource, FakeStepExecutor)
don't exist yet.  They land in later todos (runner in 13, scanner_source in 11,
fakes in 15).  Collection WILL fail with ImportError — that's the point.

These tests drive the runner's:
  * start / stop / interrupt / clear-and-restart lifecycle
  * supervisor coroutine that turns StepOutcome (or unhandled exceptions)
    into state transitions
  * relaunch_component gating (only valid while running, only for
    components exposed by the current executor)
  * advance_phase delegation to the current executor
  * atomic persistence of ``session_state.json`` after every transition
  * load_or_create's resume semantics:
      - running-at-load → coerced to failed
      - unknown schema_version → raises
      - persisted step configs take priority over current SESSION_CONFIGS
      - partial .nii files are preserved

All tests use a ``FakeStepExecutor`` that simulates subprocess behavior
synchronously.  SessionRunner._executor_for is monkey-patched in each test
that starts a step, so the fake is returned for every StepKind.  No real
subprocesses are launched.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio  # noqa: F401 — plugin registration

from mindfulness_nf.models import (
    SessionState,
    StepKind,
    StepStatus,
)
from mindfulness_nf.orchestration.executor import StepOutcome, StepProgress
from mindfulness_nf.orchestration.scanner_source import NoOpScannerSource
from mindfulness_nf.orchestration.session_runner import SessionRunner
from tests.fakes import FakeStepExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state_file(subject_dir: Path) -> Path:
    """The canonical location of session_state.json inside a session dir."""
    return subject_dir / "session_state.json"


def _read_state_json(subject_dir: Path) -> dict[str, Any]:
    """Parse the persisted state file (assumes it exists)."""
    return json.loads(_state_file(subject_dir).read_text())


def _install_fake(
    monkeypatch: pytest.MonkeyPatch,
    runner: SessionRunner,
    fake: FakeStepExecutor,
) -> None:
    """Monkey-patch the runner to return ``fake`` for every StepKind."""
    monkeypatch.setattr(runner, "_executor_for", lambda step_config: fake)


async def _wait_running(runner: SessionRunner, timeout: float = 1.0) -> None:
    """Busy-wait until the runner's current step is RUNNING (or time out)."""
    async with asyncio.timeout(timeout):
        while runner.state.current.status is not StepStatus.RUNNING:
            await asyncio.sleep(0)


async def _wait_done(runner: SessionRunner, timeout: float = 1.0) -> None:
    """Busy-wait until the supervisor coroutine has finished the current step."""
    async with asyncio.timeout(timeout):
        while runner.state.current.status is StepStatus.RUNNING:
            await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner(
    fresh_state,
    tmp_path: Path,
    pipeline_config_test,
    scanner_config_test,
) -> SessionRunner:
    """Default runner: an RT15 session in a tmp subject dir, cursor on Setup."""
    state = fresh_state("rt15")
    return SessionRunner(
        state=state,
        subject_dir=tmp_path,
        pipeline=pipeline_config_test,
        scanner_config=scanner_config_test,
        scanner_source=NoOpScannerSource(),
    )


@pytest.fixture
def nf_runner(
    fresh_state,
    tmp_path: Path,
    pipeline_config_test,
    scanner_config_test,
) -> SessionRunner:
    """Runner positioned on a feedback (NF_RUN) step with murfi+psychopy components."""
    state = fresh_state("rt15")
    # Index 3 is Feedback 1 (Setup, 2vol, TransferPre, Fb1, ...).
    state = state.select(3)
    return SessionRunner(
        state=state,
        subject_dir=tmp_path,
        pipeline=pipeline_config_test,
        scanner_config=scanner_config_test,
        scanner_source=NoOpScannerSource(),
    )


# ---------------------------------------------------------------------------
# Lifecycle: start / progress / completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_current_transitions_pending_to_running(
    runner: SessionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start_current moves the cursor step from PENDING to RUNNING."""
    fake = FakeStepExecutor(components=())

    _install_fake(monkeypatch, runner, fake)
    assert runner.state.current.status is StepStatus.PENDING

    await runner.start_current()
    await _wait_running(runner)

    assert runner.state.current.status is StepStatus.RUNNING
    assert runner.state.current.last_started is not None

    fake.simulate_completion(succeeded=True)
    await _wait_done(runner)


@pytest.mark.asyncio
async def test_start_current_refuses_when_another_step_running(
    runner: SessionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start_current is a no-op (with notification) if another step is running."""
    fake = FakeStepExecutor(components=())
    _install_fake(monkeypatch, runner, fake)

    await runner.start_current()
    await _wait_running(runner)
    running_idx = runner.state.running_index

    # Navigate the cursor elsewhere and try to start again.
    runner.select(2)
    await runner.start_current()

    # No second run: still exactly the one running step, cursor step untouched.
    assert runner.state.running_index == running_idx
    assert runner.state.steps[2].status is StepStatus.PENDING

    fake.simulate_completion(succeeded=True)
    await _wait_done(runner)


@pytest.mark.asyncio
async def test_progress_update_persists_to_json(
    runner: SessionRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each progress tick is reflected in state and written to disk."""
    fake = FakeStepExecutor(components=())
    _install_fake(monkeypatch, runner, fake)

    await runner.start_current()
    await _wait_running(runner)

    fake.simulate_volume(87)
    await asyncio.sleep(0)  # let the progress propagate

    persisted = _read_state_json(tmp_path)
    cursor = persisted["cursor"]
    assert persisted["steps"][cursor]["progress_current"] == 87

    fake.simulate_completion(succeeded=True)
    await _wait_done(runner)


@pytest.mark.asyncio
async def test_completed_requires_per_kind_validation(
    runner: SessionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner trusts StepOutcome.succeeded; a False outcome → failed, not completed."""
    fake = FakeStepExecutor(components=())
    _install_fake(monkeypatch, runner, fake)

    await runner.start_current()
    await _wait_running(runner)

    # Executor says "I ran, but validation failed" — runner must mark_failed,
    # not mark_completed.
    fake.simulate_completion(succeeded=False, error="expected 150 volumes, got 87")
    await _wait_done(runner)

    assert runner.state.current.status is StepStatus.FAILED
    assert runner.state.current.error is not None
    assert "150" in runner.state.current.error


# ---------------------------------------------------------------------------
# Failure paths: subprocess crash and programming errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_murfi_crash_marks_step_failed_with_error_message(
    runner: SessionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subprocess crash comes back as StepOutcome(succeeded=False); runner → FAILED."""
    fake = FakeStepExecutor(components=("murfi",))
    _install_fake(monkeypatch, runner, fake)

    await runner.start_current()
    await _wait_running(runner)

    fake.simulate_crash(exit_code=1, error="MURFI exited 1")
    await _wait_done(runner)

    assert runner.state.current.status is StepStatus.FAILED
    assert "MURFI" in (runner.state.current.error or "")


@pytest.mark.asyncio
async def test_programming_error_in_run_becomes_failed_step_with_trace(
    runner: SessionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unhandled exception from run() → supervisor catches, marks step failed."""
    fake = FakeStepExecutor(components=())
    _install_fake(monkeypatch, runner, fake)

    await runner.start_current()
    await _wait_running(runner)

    fake.simulate_programming_error(KeyError("components_selected"))
    await _wait_done(runner)

    assert runner.state.current.status is StepStatus.FAILED
    assert runner.state.current.error is not None
    assert "internal error" in runner.state.current.error.lower()


# ---------------------------------------------------------------------------
# Relaunch gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relaunch_murfi_keeps_progress_if_running(
    nf_runner: SessionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M on a running step delegates to executor.relaunch and preserves progress."""
    fake = FakeStepExecutor(components=("murfi", "psychopy"))
    _install_fake(monkeypatch, nf_runner, fake)

    await nf_runner.start_current()
    await _wait_running(nf_runner)
    fake.simulate_volume(50)
    await asyncio.sleep(0)

    await nf_runner.relaunch_component("murfi")

    assert "murfi" in fake.relaunch_calls
    # Progress is preserved; relaunch did not reset it or fail the step.
    assert nf_runner.state.current.progress_current == 50
    assert nf_runner.state.current.status is StepStatus.RUNNING

    fake.simulate_completion(succeeded=True)
    await _wait_done(nf_runner)


@pytest.mark.asyncio
async def test_relaunch_murfi_rejected_on_failed_step(
    nf_runner: SessionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M is only valid while RUNNING.  On a failed step, no-op (no executor call)."""
    fake = FakeStepExecutor(components=("murfi", "psychopy"))
    _install_fake(monkeypatch, nf_runner, fake)

    await nf_runner.start_current()
    await _wait_running(nf_runner)
    fake.simulate_crash(exit_code=1, error="MURFI exited 1")
    await _wait_done(nf_runner)
    assert nf_runner.state.current.status is StepStatus.FAILED

    await nf_runner.relaunch_component("murfi")

    assert fake.relaunch_calls == []


@pytest.mark.asyncio
async def test_relaunch_psychopy_rejected_on_vsend_step(
    runner: SessionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P is rejected when psychopy is not among the executor's components()."""
    # Move cursor to the 2-volume step (VSEND_SCAN: components = ("murfi",)).
    runner.select(1)
    fake = FakeStepExecutor(components=("murfi",))
    _install_fake(monkeypatch, runner, fake)

    await runner.start_current()
    await _wait_running(runner)

    await runner.relaunch_component("psychopy")

    assert "psychopy" not in fake.relaunch_calls

    fake.simulate_completion(succeeded=True)
    await _wait_done(runner)


# ---------------------------------------------------------------------------
# Phase gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_phase_signals_executor(
    nf_runner: SessionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """advance_phase_current delegates to the current executor's advance_phase."""
    fake = FakeStepExecutor(components=("murfi", "psychopy"))
    _install_fake(monkeypatch, nf_runner, fake)

    await nf_runner.start_current()
    await _wait_running(nf_runner)
    fake.simulate_phase_gate(phase="murfi", value=150, target=150)
    await asyncio.sleep(0)
    assert nf_runner.state.current.awaiting_advance is True

    await nf_runner.advance_phase_current()

    assert fake.advance_phase_calls == 1

    fake.simulate_completion(succeeded=True)
    await _wait_done(nf_runner)


@pytest.mark.asyncio
async def test_advance_phase_is_noop_on_single_phase_executor(
    runner: SessionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For single-phase steps, advance_phase is a safe no-op (still delegated)."""
    fake = FakeStepExecutor(components=())  # single-phase
    _install_fake(monkeypatch, runner, fake)

    await runner.start_current()
    await _wait_running(runner)

    # Runner still forwards the call; the fake records it but takes no effect.
    await runner.advance_phase_current()
    assert runner.state.current.status is StepStatus.RUNNING

    fake.simulate_completion(succeeded=True)
    await _wait_done(runner)


# ---------------------------------------------------------------------------
# Interrupt / clear-and-restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_clears_partial_nii_files(
    nf_runner: SessionRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """interrupt stops the executor, deletes partial .nii files for this run."""
    fake = FakeStepExecutor(components=("murfi",))
    _install_fake(monkeypatch, nf_runner, fake)

    func_dir = tmp_path / "rest"
    func_dir.mkdir()
    run_file = func_dir / "sub-test_ses-rt15_task-feedback_run-01_bold.nii"
    run_file.write_bytes(b"partial")
    untouched = func_dir / "sub-test_ses-rt15_task-transferpre_run-01_bold.nii"
    untouched.write_bytes(b"valid")

    await nf_runner.start_current()
    await _wait_running(nf_runner)
    fake.simulate_volume(50)
    await asyncio.sleep(0)

    await nf_runner.interrupt_current()

    assert not run_file.exists(), "interrupt must delete this run's partial NIfTI"
    assert untouched.exists(), "interrupt must not touch other runs' data"
    assert nf_runner.state.current.status is StepStatus.PENDING
    assert nf_runner.state.current.attempts == 1


@pytest.mark.asyncio
async def test_clear_and_restart_increments_attempts(
    runner: SessionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clear_and_restart_current stops, clears, bumps attempts, starts fresh."""
    fake = FakeStepExecutor(components=())
    _install_fake(monkeypatch, runner, fake)

    await runner.start_current()
    await _wait_running(runner)
    fake.simulate_volume(30)
    await asyncio.sleep(0)

    # Swap in a second fake for the restart so the first one's state stays clean.
    second = FakeStepExecutor(components=())
    _install_fake(monkeypatch, runner, second)

    await runner.clear_and_restart_current()
    await _wait_running(runner)

    assert runner.state.current.attempts == 1
    assert runner.state.current.progress_current == 0
    assert runner.state.current.status is StepStatus.RUNNING

    second.simulate_completion(succeeded=True)
    await _wait_done(runner)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_persisted_atomically_after_every_transition(
    runner: SessionRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After each transition, session_state.json is valid parseable JSON."""
    fake = FakeStepExecutor(components=())
    _install_fake(monkeypatch, runner, fake)

    def assert_json_valid() -> dict[str, Any]:
        path = _state_file(tmp_path)
        assert path.exists(), "state file must exist after any transition"
        return json.loads(path.read_text())

    runner.advance()
    assert_json_valid()

    runner.go_back()
    assert_json_valid()

    runner.select(2)
    parsed = assert_json_valid()
    assert parsed["cursor"] == 2

    runner.select(0)
    await runner.start_current()
    await _wait_running(runner)
    assert_json_valid()

    fake.simulate_volume(10)
    await asyncio.sleep(0)
    parsed = assert_json_valid()
    assert parsed["steps"][0]["progress_current"] == 10

    fake.simulate_completion(succeeded=True)
    await _wait_done(runner)
    parsed = assert_json_valid()
    assert parsed["steps"][0]["status"] == "completed"


# ---------------------------------------------------------------------------
# load_or_create: resume semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_or_create_coerces_running_to_failed(
    fresh_state,
    tmp_path: Path,
    pipeline_config_test,
    scanner_config_test,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step persisted as running is coerced to failed with a clear error on load."""
    # Run one step to RUNNING, then persist, simulating a crash before completion.
    state = fresh_state("rt15")
    runner_a = SessionRunner(
        state=state,
        subject_dir=tmp_path,
        pipeline=pipeline_config_test,
        scanner_config=scanner_config_test,
        scanner_source=NoOpScannerSource(),
    )
    fake = FakeStepExecutor(components=())
    _install_fake(monkeypatch, runner_a, fake)
    await runner_a.start_current()
    await _wait_running(runner_a)
    # Do NOT complete — leave status=running in the persisted JSON.

    # Hand-edit the persisted state to reflect the running-at-crash scenario.
    persisted = _read_state_json(tmp_path)
    assert persisted["steps"][persisted["cursor"]]["status"] == "running"

    runner_b = SessionRunner.load_or_create(
        subject_dir=tmp_path,
        session_type="rt15",
        pipeline=pipeline_config_test,
        scanner_config=scanner_config_test,
        scanner_source=NoOpScannerSource(),
    )
    loaded = runner_b.state
    coerced = loaded.steps[loaded.cursor]
    assert coerced.status is StepStatus.FAILED
    assert coerced.error is not None
    assert "interrupted" in coerced.error.lower()


@pytest.mark.asyncio
async def test_load_or_create_preserves_cursor(
    fresh_state,
    tmp_path: Path,
    pipeline_config_test,
    scanner_config_test,
) -> None:
    """load_or_create round-trips the cursor index."""
    state = fresh_state("rt15").select(4)
    runner_a = SessionRunner(
        state=state,
        subject_dir=tmp_path,
        pipeline=pipeline_config_test,
        scanner_config=scanner_config_test,
        scanner_source=NoOpScannerSource(),
    )
    # Trigger a persist via any transition.
    runner_a.select(4)

    runner_b = SessionRunner.load_or_create(
        subject_dir=tmp_path,
        session_type="rt15",
        pipeline=pipeline_config_test,
        scanner_config=scanner_config_test,
        scanner_source=NoOpScannerSource(),
    )

    assert runner_b.state.cursor == 4


def test_load_or_create_rejects_unknown_schema_version(
    tmp_path: Path,
    pipeline_config_test,
    scanner_config_test,
) -> None:
    """An unknown schema_version must surface a clear error, not silent misinterpretation."""
    state_file = _state_file(tmp_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({
        "schema_version": 999,
        "subject": "sub-test",
        "session_type": "rt15",
        "created_at": "2026-04-20T00:00:00Z",
        "updated_at": "2026-04-20T00:00:00Z",
        "cursor": 0,
        "steps": [],
    }))

    with pytest.raises((ValueError, RuntimeError), match="schema_version"):
        SessionRunner.load_or_create(
            subject_dir=tmp_path,
            session_type="rt15",
            pipeline=pipeline_config_test,
            scanner_config=scanner_config_test,
            scanner_source=NoOpScannerSource(),
        )


@pytest.mark.asyncio
async def test_resume_uses_persisted_step_configs_not_current(
    fresh_state,
    tmp_path: Path,
    pipeline_config_test,
    scanner_config_test,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persisted step configs take priority; SESSION_CONFIGS drift is ignored on resume."""
    state = fresh_state("rt15")
    runner_a = SessionRunner(
        state=state,
        subject_dir=tmp_path,
        pipeline=pipeline_config_test,
        scanner_config=scanner_config_test,
        scanner_source=NoOpScannerSource(),
    )
    # Force a persist so the on-disk file has the ORIGINAL configs.
    runner_a.select(1)
    original_target = runner_a.state.steps[1].config.progress_target

    # Simulate code drift: SESSION_CONFIGS["rt15"] now differs.
    from mindfulness_nf import sessions as sessions_module
    from mindfulness_nf.models import StepConfig as _StepConfig
    drifted = tuple(
        (
            _StepConfig(
                name=c.name,
                task=c.task,
                run=c.run,
                progress_target=9999,  # drift!
                progress_unit=c.progress_unit,
                xml_name=c.xml_name,
                kind=c.kind,
                feedback=c.feedback,
                fsl_command=c.fsl_command,
            )
            if i == 1
            else c
        )
        for i, c in enumerate(sessions_module.SESSION_CONFIGS["rt15"])
    )
    monkeypatch.setitem(
        sessions_module.SESSION_CONFIGS, "rt15", drifted
    )

    runner_b = SessionRunner.load_or_create(
        subject_dir=tmp_path,
        session_type="rt15",
        pipeline=pipeline_config_test,
        scanner_config=scanner_config_test,
        scanner_source=NoOpScannerSource(),
    )

    # Resume must use the persisted config (original target), not the drifted one.
    assert runner_b.state.steps[1].config.progress_target == original_target
    assert runner_b.state.steps[1].config.progress_target != 9999


@pytest.mark.asyncio
async def test_resume_leaves_partial_data_on_disk(
    fresh_state,
    tmp_path: Path,
    pipeline_config_test,
    scanner_config_test,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial .nii files from an interrupted run are preserved across resume."""
    state = fresh_state("rt15").select(3)
    runner_a = SessionRunner(
        state=state,
        subject_dir=tmp_path,
        pipeline=pipeline_config_test,
        scanner_config=scanner_config_test,
        scanner_source=NoOpScannerSource(),
    )
    fake = FakeStepExecutor(components=("murfi",))
    _install_fake(monkeypatch, runner_a, fake)

    func_dir = tmp_path / "rest"
    func_dir.mkdir()
    partial = func_dir / "sub-test_ses-rt15_task-feedback_run-01_bold.nii"
    partial.write_bytes(b"partial")

    await runner_a.start_current()
    await _wait_running(runner_a)

    # Do not call interrupt; simulate a crash by just dropping the runner.
    # The on-disk JSON still says status=running; load_or_create will coerce it.

    SessionRunner.load_or_create(
        subject_dir=tmp_path,
        session_type="rt15",
        pipeline=pipeline_config_test,
        scanner_config=scanner_config_test,
        scanner_source=NoOpScannerSource(),
    )

    assert partial.exists(), "resume must not delete partial data — operator decides"


# ---------------------------------------------------------------------------
# Navigation invariants
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_navigating_while_running_does_not_stop_process(
    runner: SessionRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving the cursor while a step is running never touches the executor."""
    fake = FakeStepExecutor(components=())
    _install_fake(monkeypatch, runner, fake)

    await runner.start_current()
    await _wait_running(runner)
    running_idx = runner.state.running_index

    runner.advance()
    runner.select(3)
    runner.go_back()

    # Cursor moved but the running step is untouched.
    assert runner.state.running_index == running_idx
    assert runner.state.steps[running_idx].status is StepStatus.RUNNING
    assert fake.stop_calls == 0

    fake.simulate_completion(succeeded=True)
    await _wait_done(runner)
