"""Layer 3 end-to-end tests: TUI + SessionRunner + StepExecutor chain.

Per spec §Testing → §Layer 3, these tests exercise every row of the
operator checklist by driving a Textual ``SessionScreen`` through its
keybindings while a ``FakeStepExecutor`` simulates subprocess behaviour
deterministically.  No real MURFI / PsychoPy / scanner processes are
spawned.

TDD red: ``mindfulness_nf.tui.screens.session.SessionScreen`` does not
exist yet (it lands in todo-21), so collection / import will fail for
the Textual-driven tests.  Config spot-check tests that do not need the
screen (``test_rt30_config_has_15_steps_...`` and
``test_bids_naming_matches_scanner_pdf``) are structured to pass even
before todo-21, because they touch only ``mindfulness_nf.sessions``.

Each async test follows the same recipe:

1. Build a ``SessionRunner`` pointing at a tmp session dir.
2. Install a ``FakeStepExecutor`` via ``monkeypatch`` of
   ``runner._executor_for`` so every step returns the controlled fake.
3. Push a ``SessionScreen`` wrapping that runner inside a minimal
   ``App`` via ``async with app.run_test() as pilot``.
4. Press the keybinding under test; drive executor lifecycle via
   ``fake.simulate_*``; assert on ``runner.state`` and on-disk files.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio  # noqa: F401 — plugin registration

from textual.app import App

from mindfulness_nf.models import (
    SessionState,
    StepKind,
    StepStatus,
)
from mindfulness_nf.orchestration.scanner_source import (
    NoOpScannerSource,
    SimulatedScannerSource,
)
from mindfulness_nf.orchestration.session_runner import SessionRunner
from mindfulness_nf.sessions import (
    PROCESS,
    RT15,
    RT30,
    SESSION_CONFIGS,
)
from tests.fakes import FakeStepExecutor

# TDD red: this import fails until todo-21 lands SessionScreen.  Tests
# that need the screen will raise ImportError on collection; tests that
# don't touch SessionScreen (config spot-checks) are unaffected because
# they don't instantiate ``_SessionScreenApp``.
try:
    from mindfulness_nf.tui.screens.session import (  # type: ignore[import-not-found]
        SessionScreen,
    )

    _SESSION_SCREEN_AVAILABLE = True
except ImportError:  # pragma: no cover — only during TDD red
    SessionScreen = None  # type: ignore[assignment,misc]
    _SESSION_SCREEN_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state_file(subject_dir: Path) -> Path:
    return subject_dir / "session_state.json"


def _read_state_json(subject_dir: Path) -> dict[str, Any]:
    return json.loads(_state_file(subject_dir).read_text())


def _install_fake(
    monkeypatch: pytest.MonkeyPatch,
    runner: SessionRunner,
    fake: FakeStepExecutor,
) -> None:
    """Route every ``_executor_for`` call through ``fake``."""
    monkeypatch.setattr(runner, "_executor_for", lambda step_config: fake)


def _install_fake_factory(
    monkeypatch: pytest.MonkeyPatch,
    runner: SessionRunner,
    factory: Any,
) -> None:
    """Route ``_executor_for`` through an arbitrary factory (step → fake)."""
    monkeypatch.setattr(runner, "_executor_for", factory)


async def _wait_running(runner: SessionRunner, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while runner.state.current.status is not StepStatus.RUNNING:
            await asyncio.sleep(0)


async def _wait_done(runner: SessionRunner, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while runner.state.current.status is StepStatus.RUNNING:
            await asyncio.sleep(0)


async def _wait_status(
    runner: SessionRunner,
    idx: int,
    status: StepStatus,
    timeout: float = 1.0,
) -> None:
    """Busy-wait until ``runner.state.steps[idx].status is status``."""
    async with asyncio.timeout(timeout):
        while runner.state.steps[idx].status is not status:
            await asyncio.sleep(0)


def _make_runner(
    tmp_path: Path,
    fresh_state_factory: Any,
    pipeline_config: Any,
    scanner_config: Any,
    session_type: str = "rt15",
    cursor: int = 0,
) -> SessionRunner:
    state = fresh_state_factory(session_type)
    if cursor != 0:
        state = state.select(cursor)
    return SessionRunner(
        state=state,
        subject_dir=tmp_path,
        pipeline=pipeline_config,
        scanner_config=scanner_config,
        scanner_source=NoOpScannerSource(),
    )


class _SessionScreenApp(App[None]):
    """Minimal Textual app that pushes a SessionScreen over a runner.

    The screen receives the runner via its constructor; we keep a handle
    on ``self._runner`` so tests can inspect state without re-resolving
    the screen.
    """

    def __init__(self, runner: SessionRunner) -> None:
        super().__init__()
        self._runner = runner

    def on_mount(self) -> None:
        # SessionScreen is imported at module top; tests skip or xfail if
        # it's missing.  The screen must accept a positional SessionRunner.
        self.push_screen(SessionScreen(self._runner))  # type: ignore[misc]


# Every Textual test skips cleanly in TDD-red mode so the collection
# phase doesn't crash the whole file.  Once todo-21 lands, the marker
# becomes a no-op and the full suite runs.
_needs_screen = pytest.mark.skipif(
    not _SESSION_SCREEN_AVAILABLE,
    reason="SessionScreen not implemented yet (TDD red — see todo-21).",
)


# ---------------------------------------------------------------------------
# TestGoldenPath
# ---------------------------------------------------------------------------


class TestGoldenPath:
    """End-to-end happy paths — the executable operator checklist."""

    @_needs_screen
    @pytest.mark.asyncio
    async def test_full_rt15_session_completes_green(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Golden path: walk RT15 (9 steps) end-to-end, all COMPLETED."""
        runner = _make_runner(
            tmp_path, fresh_state, pipeline_config_test, scanner_config_test
        )
        fakes: list[FakeStepExecutor] = []

        def factory(_step: Any) -> FakeStepExecutor:
            fake = FakeStepExecutor(components=())
            fakes.append(fake)
            return fake

        _install_fake_factory(monkeypatch, runner, factory)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            for _ in range(len(runner.state.steps)):
                await pilot.press("d")
                await pilot.pause()
                await _wait_running(runner)
                fakes[-1].simulate_completion(succeeded=True)
                await _wait_done(runner)
                await pilot.pause()

        assert all(
            s.status is StepStatus.COMPLETED for s in runner.state.steps
        ), "every RT15 step must complete in the golden path"

    @_needs_screen
    @pytest.mark.asyncio
    async def test_process_session_runs_all_fsl_stages(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PROCESS session: Setup → Merge → MELODIC → Extract DMN → CEN → Register → QC."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            session_type="process",
        )
        fakes: list[FakeStepExecutor] = []

        def factory(_step: Any) -> FakeStepExecutor:
            fake = FakeStepExecutor(components=(), progress_target=100, progress_unit="percent")
            fakes.append(fake)
            return fake

        _install_fake_factory(monkeypatch, runner, factory)

        # Create the derivatives/masks outputs the spec references.
        masks_dir = tmp_path / "derivatives" / "masks"
        masks_dir.mkdir(parents=True, exist_ok=True)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            for _ in range(len(runner.state.steps)):
                await pilot.press("d")
                await pilot.pause()
                await _wait_running(runner)
                fakes[-1].simulate_completion(succeeded=True)
                await _wait_done(runner)
                await pilot.pause()

        statuses = [s.status for s in runner.state.steps]
        assert all(st is StepStatus.COMPLETED for st in statuses)


# ---------------------------------------------------------------------------
# TestResume
# ---------------------------------------------------------------------------


class TestResume:
    """Resume from ``session_state.json`` across process boundaries."""

    @_needs_screen
    @pytest.mark.asyncio
    async def test_resume_after_force_quit_lands_on_same_cursor(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Half-complete state.json → new runner keeps cursor, coerces running→failed."""
        runner_a = _make_runner(
            tmp_path, fresh_state, pipeline_config_test, scanner_config_test
        )
        fake = FakeStepExecutor(components=())
        _install_fake(monkeypatch, runner_a, fake)

        # Complete step 0.
        await runner_a.start_current()
        await _wait_running(runner_a)
        fake.simulate_completion(succeeded=True)
        await _wait_done(runner_a)
        runner_a.advance()  # cursor moves to step 1
        await _wait_running(runner_a)  # auto-start kicks in
        # Do NOT complete step 1 — this simulates a force-quit mid-run.

        persisted = _read_state_json(tmp_path)
        assert persisted["cursor"] == 1
        assert persisted["steps"][0]["status"] == "completed"
        assert persisted["steps"][1]["status"] == "running"

        runner_b = SessionRunner.load_or_create(
            subject_dir=tmp_path,
            session_type="rt15",
            pipeline=pipeline_config_test,
            scanner_config=scanner_config_test,
            scanner_source=NoOpScannerSource(),
        )

        assert runner_b.state.cursor == 1
        assert runner_b.state.steps[0].status is StepStatus.COMPLETED
        assert runner_b.state.steps[1].status is StepStatus.FAILED
        assert "interrupted" in (runner_b.state.steps[1].error or "").lower()

    def test_fresh_vs_load_chosen_by_json_presence(
        self,
        tmp_path: Path,
        pipeline_config_test: Any,
        scanner_config_test: Any,
    ) -> None:
        """No json → fresh from SESSION_CONFIGS; with json → reconstruct from persisted."""
        # Fresh: no session_state.json exists yet in an empty tmp_path.
        fresh_dir = tmp_path / "fresh"
        fresh_dir.mkdir()
        runner_fresh = SessionRunner.load_or_create(
            subject_dir=fresh_dir,
            session_type="rt15",
            pipeline=pipeline_config_test,
            scanner_config=scanner_config_test,
            scanner_source=NoOpScannerSource(),
        )
        assert len(runner_fresh.state.steps) == len(SESSION_CONFIGS["rt15"])
        assert runner_fresh.state.cursor == 0
        assert all(
            s.status is StepStatus.PENDING for s in runner_fresh.state.steps
        )

        # Load: a prior persist has created the json file.
        load_dir = tmp_path / "load"
        load_dir.mkdir()
        _ = SessionRunner(
            state=SessionState(
                subject="sub-test",
                session_type="rt15",
                cursor=3,
                steps=tuple(
                    # Use persist-source-of-truth: constructing from SESSION_CONFIGS.
                    SessionRunner.load_or_create(
                        subject_dir=tmp_path / "seed",
                        session_type="rt15",
                        pipeline=pipeline_config_test,
                        scanner_config=scanner_config_test,
                        scanner_source=NoOpScannerSource(),
                    ).state.steps
                ),
                created_at="2026-04-20T00:00:00+00:00",
                updated_at="2026-04-20T00:00:00+00:00",
            ),
            subject_dir=load_dir,
            pipeline=pipeline_config_test,
            scanner_config=scanner_config_test,
            scanner_source=NoOpScannerSource(),
        )
        assert _state_file(load_dir).exists()

        runner_loaded = SessionRunner.load_or_create(
            subject_dir=load_dir,
            session_type="rt15",
            pipeline=pipeline_config_test,
            scanner_config=scanner_config_test,
            scanner_source=NoOpScannerSource(),
        )
        assert runner_loaded.state.cursor == 3

    @_needs_screen
    @pytest.mark.asyncio
    async def test_process_resume_skips_completed_stages(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Start PROCESS, complete Merge + MELODIC, force-quit.  Resume skips them."""
        runner_a = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            session_type="process",
        )

        # Complete Setup, Merge, MELODIC (indices 0, 1, 2).
        for _ in range(3):
            fake = FakeStepExecutor(components=())
            _install_fake(monkeypatch, runner_a, fake)
            await runner_a.start_current()
            await _wait_running(runner_a)
            fake.simulate_completion(succeeded=True)
            await _wait_done(runner_a)
            runner_a.advance()

        # Cursor should now point at Extract DMN (index 3).
        assert runner_a.state.cursor == 3

        # Force-quit: drop runner_a, resume via load_or_create.
        runner_b = SessionRunner.load_or_create(
            subject_dir=tmp_path,
            session_type="process",
            pipeline=pipeline_config_test,
            scanner_config=scanner_config_test,
            scanner_source=NoOpScannerSource(),
        )
        assert runner_b.state.cursor == 3
        for i in range(3):
            assert runner_b.state.steps[i].status is StepStatus.COMPLETED
        assert runner_b.state.steps[3].status is StepStatus.PENDING


# ---------------------------------------------------------------------------
# TestCrashRecovery
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    """Fault-injection paths: MURFI crash, PsychoPy crash, FSL failure."""

    @_needs_screen
    @pytest.mark.asyncio
    async def test_murfi_crash_mid_feedback_marks_step_failed_and_r_restarts(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MURFI crash at vol 50/150 → FAILED; R clears & restarts."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,  # Feedback 1 (NF_RUN with murfi component)
        )
        first = FakeStepExecutor(components=("murfi", "psychopy"))
        _install_fake(monkeypatch, runner, first)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)

            first.simulate_volume(50)
            await asyncio.sleep(0)
            first.simulate_crash(exit_code=1, error="MURFI exited 1")
            await _wait_done(runner)

            assert runner.state.current.status is StepStatus.FAILED
            assert "MURFI" in (runner.state.current.error or "")

            # Swap in a second fake for the restart.
            second = FakeStepExecutor(components=("murfi", "psychopy"))
            _install_fake(monkeypatch, runner, second)

            await pilot.press("r")
            await pilot.pause()
            await _wait_running(runner)

            assert runner.state.current.status is StepStatus.RUNNING
            assert runner.state.current.attempts >= 1

            second.simulate_completion(succeeded=True)
            await _wait_done(runner)

    @_needs_screen
    @pytest.mark.asyncio
    async def test_psychopy_crash_after_murfi_phase_marks_step_failed(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """MURFI phase completes, then PsychoPy crash → step FAILED."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        fake = FakeStepExecutor(components=("murfi", "psychopy"))
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fake.simulate_phase_gate(
                phase="psychopy", value=150, target=150, awaiting_advance=False
            )
            await asyncio.sleep(0)

            fake.simulate_crash(exit_code=1, error="PsychoPy exited 1")
            await _wait_done(runner)

        assert runner.state.current.status is StepStatus.FAILED
        assert "PsychoPy" in (runner.state.current.error or "")

    @_needs_screen
    @pytest.mark.asyncio
    async def test_process_melodic_failure_marks_stage_failed(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FSL MELODIC exits non-zero → stage FAILED; R can restart."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            session_type="process",
            cursor=2,  # MELODIC
        )
        fake = FakeStepExecutor(components=(), progress_target=100, progress_unit="percent")
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fake.simulate_crash(exit_code=1, error="MELODIC exited 1")
            await _wait_done(runner)

            assert runner.state.current.status is StepStatus.FAILED

            # R should clear and restart.
            second = FakeStepExecutor(
                components=(), progress_target=100, progress_unit="percent"
            )
            _install_fake(monkeypatch, runner, second)
            await pilot.press("r")
            await pilot.pause()
            await _wait_running(runner)
            assert runner.state.current.status is StepStatus.RUNNING
            second.simulate_completion(succeeded=True)
            await _wait_done(runner)


# ---------------------------------------------------------------------------
# TestKeybindings
# ---------------------------------------------------------------------------


class TestKeybindings:
    """Per-key semantics from spec §Keybindings."""

    @_needs_screen
    @pytest.mark.asyncio
    async def test_interrupt_mid_feedback_clears_data_and_keeps_cursor(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Press I mid-run → executor stopped, func/*.nii cleared, cursor unchanged."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        fake = FakeStepExecutor(components=("murfi",))
        _install_fake(monkeypatch, runner, fake)

        func_dir = tmp_path / "func"
        func_dir.mkdir()
        this_run = func_dir / "sub-test_ses-rt15_task-feedback_run-01_bold.nii"
        this_run.write_bytes(b"partial")
        other_run = (
            func_dir / "sub-test_ses-rt15_task-transferpre_run-01_bold.nii"
        )
        other_run.write_bytes(b"valid")

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fake.simulate_volume(50)
            await asyncio.sleep(0)
            cursor_before = runner.state.cursor

            await pilot.press("i")
            await pilot.pause()
            await _wait_done(runner)

        assert not this_run.exists(), "I must delete this run's partial data"
        assert other_run.exists(), "I must not touch other runs' data"
        assert runner.state.cursor == cursor_before
        assert runner.state.current.status is StepStatus.PENDING

    @_needs_screen
    @pytest.mark.asyncio
    async def test_manual_murfi_relaunch_via_m_key(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M delegates to executor.relaunch('murfi'); status stays RUNNING."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        fake = FakeStepExecutor(components=("murfi", "psychopy"))
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fake.simulate_volume(50)
            await asyncio.sleep(0)

            await pilot.press("m")
            await pilot.pause()

            assert "murfi" in fake.relaunch_calls
            assert runner.state.current.status is StepStatus.RUNNING
            fake.simulate_completion(succeeded=True)
            await _wait_done(runner)

    @_needs_screen
    @pytest.mark.asyncio
    async def test_manual_psychopy_relaunch_via_p_key(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """P delegates to executor.relaunch('psychopy'); MURFI keeps running."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        fake = FakeStepExecutor(components=("murfi", "psychopy"))
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)

            await pilot.press("p")
            await pilot.pause()

            assert "psychopy" in fake.relaunch_calls
            assert runner.state.current.status is StepStatus.RUNNING
            fake.simulate_completion(succeeded=True)
            await _wait_done(runner)

    @_needs_screen
    @pytest.mark.asyncio
    async def test_m_key_does_not_delete_partial_volumes(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M leaves partial .nii files intact and keeps progress_current."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        fake = FakeStepExecutor(components=("murfi", "psychopy"))
        _install_fake(monkeypatch, runner, fake)

        func_dir = tmp_path / "func"
        func_dir.mkdir()
        partial = func_dir / "sub-test_ses-rt15_task-feedback_run-01_bold.nii"
        partial.write_bytes(b"partial")

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fake.simulate_volume(50)
            await asyncio.sleep(0)

            await pilot.press("m")
            await pilot.pause()

            assert partial.exists(), "M must not delete partial volumes"
            assert runner.state.current.progress_current == 50
            fake.simulate_completion(succeeded=True)
            await _wait_done(runner)

    @_needs_screen
    @pytest.mark.asyncio
    async def test_r_key_deletes_partial_volumes_and_increments_attempts(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R stops, clears files, resets progress, bumps attempts, restarts."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        first = FakeStepExecutor(components=("murfi", "psychopy"))
        _install_fake(monkeypatch, runner, first)

        func_dir = tmp_path / "func"
        func_dir.mkdir()
        partial = func_dir / "sub-test_ses-rt15_task-feedback_run-01_bold.nii"
        partial.write_bytes(b"partial")

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            first.simulate_volume(50)
            await asyncio.sleep(0)

            second = FakeStepExecutor(components=("murfi", "psychopy"))
            _install_fake(monkeypatch, runner, second)

            await pilot.press("r")
            await pilot.pause()
            await _wait_running(runner)

            assert not partial.exists(), "R must delete partial volumes"
            assert runner.state.current.progress_current == 0
            assert runner.state.current.attempts == 1
            second.simulate_completion(succeeded=True)
            await _wait_done(runner)

    @_needs_screen
    @pytest.mark.asyncio
    async def test_m_on_failed_step_is_rejected_with_notification(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """M is only valid on RUNNING steps; FAILED rejects (no relaunch call)."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        fake = FakeStepExecutor(components=("murfi", "psychopy"))
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fake.simulate_crash(exit_code=1, error="MURFI exited 1")
            await _wait_done(runner)
            assert runner.state.current.status is StepStatus.FAILED

            relaunch_count_before = len(fake.relaunch_calls)
            await pilot.press("m")
            await pilot.pause()

            assert len(fake.relaunch_calls) == relaunch_count_before

    @_needs_screen
    @pytest.mark.asyncio
    async def test_r_on_completed_step_prompts_confirmation(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R on COMPLETED prompts; Y clears + restarts, N is a no-op."""
        runner = _make_runner(
            tmp_path, fresh_state, pipeline_config_test, scanner_config_test
        )
        fake = FakeStepExecutor(components=())
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            # Complete step 0.
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fake.simulate_completion(succeeded=True)
            await _wait_done(runner)
            assert runner.state.current.status is StepStatus.COMPLETED

            # R should prompt; N leaves state alone.
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert runner.state.current.status is StepStatus.COMPLETED

            # R then Y clears + restarts.
            second = FakeStepExecutor(components=())
            _install_fake(monkeypatch, runner, second)
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            await _wait_running(runner)
            assert runner.state.current.status is StepStatus.RUNNING
            second.simulate_completion(succeeded=True)
            await _wait_done(runner)

    @_needs_screen
    @pytest.mark.asyncio
    async def test_escape_mid_run_prompts_and_marks_cancelled(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Escape mid-run prompts; Y stops the step, marks failed with 'cancelled'."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        fake = FakeStepExecutor(components=("murfi",))
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)

            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            await _wait_done(runner)

        assert fake.stop_calls >= 1
        # The outcome of stop() is succeeded=False with error="cancelled".
        assert runner.state.steps[3].status is StepStatus.FAILED
        assert (runner.state.steps[3].error or "").lower() == "cancelled"


# ---------------------------------------------------------------------------
# TestStateInvariants
# ---------------------------------------------------------------------------


class TestStateInvariants:
    """Invariants that must hold at all times (spec §Invariants)."""

    @_needs_screen
    @pytest.mark.asyncio
    async def test_concurrent_running_is_prevented(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Trying to start a second step while one runs is refused."""
        runner = _make_runner(
            tmp_path, fresh_state, pipeline_config_test, scanner_config_test
        )
        fake = FakeStepExecutor(components=())
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            running_idx = runner.state.running_index

            # Navigate to a pending step and try to start it.
            await pilot.press("n")
            await pilot.press("n")
            await pilot.press("d")
            await pilot.pause()

            # Only one RUNNING step exists: the original one.
            running = [
                i
                for i, s in enumerate(runner.state.steps)
                if s.status is StepStatus.RUNNING
            ]
            assert running == [running_idx]

            fake.simulate_completion(succeeded=True)
            await _wait_done(runner)

    @_needs_screen
    @pytest.mark.asyncio
    async def test_r_refused_when_cursor_not_running_step(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """R on cursor≠running refuses (no file deletion, no restart)."""
        runner = _make_runner(
            tmp_path, fresh_state, pipeline_config_test, scanner_config_test
        )
        fake = FakeStepExecutor(components=())
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            # Complete step 0, start step 1 (auto-chain).
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fake.simulate_completion(succeeded=True)
            await _wait_done(runner)
            second = FakeStepExecutor(components=())
            _install_fake(monkeypatch, runner, second)
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            running_idx = runner.state.running_index

            # Seed a sentinel file for the already-completed step 0.
            func_dir = tmp_path / "func"
            func_dir.mkdir(exist_ok=True)
            sentinel = (
                func_dir / "sub-test_ses-rt15_task-2vol_run-01_bold.nii"
            )
            sentinel.write_bytes(b"do not delete")

            # Cursor at step 0 (completed), running step is elsewhere.
            await pilot.press("b")
            await pilot.pause()
            assert runner.state.cursor != running_idx

            await pilot.press("r")
            await pilot.pause()

            # Completed step's file must still exist; running step untouched.
            assert sentinel.exists()
            assert runner.state.running_index == running_idx

            second.simulate_completion(succeeded=True)
            await _wait_done(runner)

    @_needs_screen
    @pytest.mark.asyncio
    async def test_interrupt_targets_running_step_regardless_of_cursor(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """I stops the RUNNING step even when cursor points elsewhere."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        fake = FakeStepExecutor(components=("murfi",))
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            running_idx = runner.state.running_index

            # Move cursor away from running step.
            await pilot.press("n")
            await pilot.press("n")
            await pilot.pause()
            cursor = runner.state.cursor
            assert cursor != running_idx

            await pilot.press("i")
            await pilot.pause()
            await _wait_done(runner)

        # The *running* step is now pending; the cursor-target is unchanged.
        assert runner.state.steps[running_idx].status is StepStatus.PENDING
        assert fake.stop_calls >= 1

    @_needs_screen
    @pytest.mark.asyncio
    async def test_screen_unmount_calls_stop_current(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dismissing the screen awaits runner.stop_current() before teardown."""
        runner = _make_runner(
            tmp_path, fresh_state, pipeline_config_test, scanner_config_test
        )
        fake = FakeStepExecutor(components=())
        _install_fake(monkeypatch, runner, fake)

        stop_mock = AsyncMock(wraps=runner.stop_current)
        monkeypatch.setattr(runner, "stop_current", stop_mock)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)

            # Resolve the running task cleanly first so teardown isn't blocked.
            fake.simulate_completion(succeeded=True)
            await _wait_done(runner)

            app.pop_screen()
            await pilot.pause()

        stop_mock.assert_awaited()

    @_needs_screen
    @pytest.mark.asyncio
    async def test_partial_scan_fails_without_force_complete(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Executor returning succeeded=False with short-volume error → FAILED."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        fake = FakeStepExecutor(components=("murfi",))
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fake.simulate_volume(140)
            await asyncio.sleep(0)
            fake.simulate_completion(
                succeeded=False, error="expected 150 volumes, got 140"
            )
            await _wait_done(runner)

        assert runner.state.current.status is StepStatus.FAILED
        assert "150" in (runner.state.current.error or "")


# ---------------------------------------------------------------------------
# TestNavigation
# ---------------------------------------------------------------------------


class TestNavigation:
    """Cursor motion + phase gating dispatch."""

    @_needs_screen
    @pytest.mark.asyncio
    async def test_back_and_rerun_feedback_1_leaves_feedback_2_3_untouched(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Complete Fb1-3, go back to Fb1, R → Fb2/Fb3 still COMPLETED."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        fakes: list[FakeStepExecutor] = []

        def factory(_step: Any) -> FakeStepExecutor:
            fake = FakeStepExecutor(components=())
            fakes.append(fake)
            return fake

        _install_fake_factory(monkeypatch, runner, factory)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            # Complete Feedback 1, 2, 3 (indices 3, 4, 5).
            for _ in range(3):
                await pilot.press("d")
                await pilot.pause()
                await _wait_running(runner)
                fakes[-1].simulate_completion(succeeded=True)
                await _wait_done(runner)

            assert runner.state.steps[3].status is StepStatus.COMPLETED
            assert runner.state.steps[4].status is StepStatus.COMPLETED
            assert runner.state.steps[5].status is StepStatus.COMPLETED

            # Go back to Feedback 1. After the loop cursor==5 (Feedback 3);
            # pressing B twice moves 5 -> 4 -> 3 (Feedback 1).
            await pilot.press("b")
            await pilot.press("b")
            await pilot.pause()
            assert runner.state.cursor == 3

            await pilot.press("r")
            await pilot.pause()
            await pilot.press("y")  # confirm the re-run
            await pilot.pause()
            await _wait_running(runner)

            # Feedback 2 and 3 are untouched.
            assert runner.state.steps[4].status is StepStatus.COMPLETED
            assert runner.state.steps[5].status is StepStatus.COMPLETED

            fakes[-1].simulate_completion(succeeded=True)
            await _wait_done(runner)

    @_needs_screen
    @pytest.mark.asyncio
    async def test_d_on_completed_advances_and_auto_starts_next_pending(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One D press on a COMPLETED step both advances AND auto-starts the next."""
        runner = _make_runner(
            tmp_path, fresh_state, pipeline_config_test, scanner_config_test
        )
        fakes: list[FakeStepExecutor] = []

        def factory(_step: Any) -> FakeStepExecutor:
            fake = FakeStepExecutor(components=())
            fakes.append(fake)
            return fake

        _install_fake_factory(monkeypatch, runner, factory)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            # Complete step 0.
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fakes[-1].simulate_completion(succeeded=True)
            await _wait_done(runner)

            # Single D press → cursor advances AND step 1 starts.
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)

            assert runner.state.cursor == 1
            assert runner.state.steps[1].status is StepStatus.RUNNING

            fakes[-1].simulate_completion(succeeded=True)
            await _wait_done(runner)

    @_needs_screen
    @pytest.mark.asyncio
    async def test_d_on_completed_last_step_is_noop(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D on final COMPLETED step stays clamped (no new step starts)."""
        runner = _make_runner(
            tmp_path, fresh_state, pipeline_config_test, scanner_config_test
        )
        last_idx = len(runner.state.steps) - 1
        runner.select(last_idx)

        fake = FakeStepExecutor(components=())
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fake.simulate_completion(succeeded=True)
            await _wait_done(runner)
            assert runner.state.cursor == last_idx
            assert runner.state.current.status is StepStatus.COMPLETED

            await pilot.press("d")
            await pilot.pause()

            # Cursor clamped at last step; no new task launched.
            assert runner.state.cursor == last_idx

    @_needs_screen
    @pytest.mark.asyncio
    async def test_advance_phase_triggers_psychopy_launch_in_nf_run(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """At MURFI→PsychoPy gate, D signals advance_phase on the executor."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        fake = FakeStepExecutor(components=("murfi", "psychopy"))
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fake.simulate_phase_gate(
                phase="murfi",
                value=150,
                target=150,
                awaiting_advance=True,
                detail="Press D to start PsychoPy",
            )
            await asyncio.sleep(0)
            assert runner.state.current.awaiting_advance is True

            await pilot.press("d")
            await pilot.pause()

            assert fake.advance_phase_calls == 1
            fake.simulate_completion(succeeded=True)
            await _wait_done(runner)

    @_needs_screen
    @pytest.mark.asyncio
    async def test_advance_phase_before_murfi_complete_is_ignored(
        self,
        tmp_path: Path,
        fresh_state: Any,
        pipeline_config_test: Any,
        scanner_config_test: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D before the phase gate does not early-start PsychoPy (runner-side no-op)."""
        runner = _make_runner(
            tmp_path,
            fresh_state,
            pipeline_config_test,
            scanner_config_test,
            cursor=3,
        )
        fake = FakeStepExecutor(components=("murfi", "psychopy"))
        _install_fake(monkeypatch, runner, fake)

        app = _SessionScreenApp(runner)
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            await _wait_running(runner)
            fake.simulate_volume(50)
            await asyncio.sleep(0)
            assert runner.state.current.awaiting_advance is False

            await pilot.press("d")
            await pilot.pause()

            # Status stays RUNNING; progress not reset.  Executor-internal guard
            # may silently ignore the advance_phase call — either 0 calls (TUI
            # suppressed) or calls handled as no-op is acceptable.
            assert runner.state.current.status is StepStatus.RUNNING
            assert runner.state.current.progress_current == 50
            fake.simulate_completion(succeeded=True)
            await _wait_done(runner)


# ---------------------------------------------------------------------------
# TestConfigsAndBids
# ---------------------------------------------------------------------------


class TestConfigsAndBids:
    """Static spot-checks: no pilot needed."""

    def test_rt30_config_has_15_steps_with_13_feedback_phase_runs(self) -> None:
        """RT30 = Setup + 2vol + 13 feedback-phase runs (TransferPre, Fb1-5, TP1, Fb6-10, TP2)."""
        assert len(RT30) == 15

        # Expected step shape.
        kinds = [c.kind for c in RT30]
        assert kinds[0] is StepKind.SETUP
        assert kinds[1] is StepKind.VSEND_SCAN
        # The remaining 13 are NF_RUN.
        assert all(k is StepKind.NF_RUN for k in kinds[2:])
        assert len([k for k in kinds if k is StepKind.NF_RUN]) == 13

        # Name / run sanity.
        names = [c.name for c in RT30]
        assert names[0] == "Setup"
        assert names[1] == "2-volume"
        assert names[2] == "Transfer Pre"
        assert "Feedback 1" in names
        assert "Feedback 10" in names
        assert "Transfer Post 1" in names
        assert "Transfer Post 2" in names

    def test_bids_naming_matches_scanner_pdf(self) -> None:
        """Task/run labels match the expected scanner PDF listing."""
        # RT15 expected (subject, session) labels.
        expected_rt15 = [
            (None, None),  # Setup
            ("2vol", 1),
            ("transferpre", 1),
            ("feedback", 1),
            ("feedback", 2),
            ("feedback", 3),
            ("feedback", 4),
            ("feedback", 5),
            ("transferpost", 1),
        ]
        actual_rt15 = [(c.task, c.run) for c in RT15]
        assert actual_rt15 == expected_rt15

        # RT30 expected.
        expected_rt30 = [
            (None, None),  # Setup
            ("2vol", 1),
            ("transferpre", 1),
            ("feedback", 1),
            ("feedback", 2),
            ("feedback", 3),
            ("feedback", 4),
            ("feedback", 5),
            ("transferpost", 1),
            ("feedback", 6),
            ("feedback", 7),
            ("feedback", 8),
            ("feedback", 9),
            ("feedback", 10),
            ("transferpost", 2),
        ]
        actual_rt30 = [(c.task, c.run) for c in RT30]
        assert actual_rt30 == expected_rt30

        # PROCESS stages have task labels, no run number.
        process_tasks = [c.task for c in PROCESS]
        assert "merge" in process_tasks
        assert "melodic" in process_tasks
        assert "dmn_mask" in process_tasks
        assert "cen_mask" in process_tasks

    @pytest.mark.asyncio
    async def test_scanner_simulator_pushes_vsend_and_dicom(
        self,
        tmp_path: Path,
        mocker: Any,
    ) -> None:
        """SimulatedScannerSource shells out to vSend / dcmsend with cached files."""
        # Seed a cache with 2 NIfTI and 3 DICOM placeholders.
        cache = tmp_path / "cache"
        (cache / "nifti").mkdir(parents=True)
        (cache / "dicom").mkdir(parents=True)
        (cache / "nifti" / "vol-001.nii").write_bytes(b"\x00")
        (cache / "nifti" / "vol-002.nii").write_bytes(b"\x00")
        for i in range(3):
            (cache / "dicom" / f"slice-{i:03d}.dcm").write_bytes(b"\x00")

        # Mock shutil.which so the simulator finds the binaries on any host.
        mocker.patch(
            "mindfulness_nf.orchestration.scanner_source.shutil.which",
            return_value="/usr/bin/true",
        )

        # Mock the subprocess launcher to record invocations without exec-ing.
        class _FakeProc:
            returncode: int | None = 0

            async def wait(self) -> int:
                return 0

            def terminate(self) -> None:
                return None

        async def _fake_exec(*cmd: str, **_kw: Any) -> _FakeProc:
            _fake_exec.calls.append(cmd)  # type: ignore[attr-defined]
            return _FakeProc()

        _fake_exec.calls = []  # type: ignore[attr-defined]
        mocker.patch(
            "mindfulness_nf.orchestration.scanner_source.asyncio.create_subprocess_exec",
            side_effect=_fake_exec,
        )

        source = SimulatedScannerSource(cache_dir=cache, tr_seconds=0.1)

        step = RT15[1]  # 2-volume (VSEND_SCAN)
        xml = tmp_path / "2vol.xml"
        xml.write_text("<xml/>")

        await source.push_vsend(xml_path=xml, subject_dir=tmp_path, step=step)
        await source.push_dicom(
            target_host="localhost", target_port=4006, ae_title="MURFI", step=step
        )

        calls = _fake_exec.calls  # type: ignore[attr-defined]
        assert len(calls) == 2
        assert any("vSend" in arg or arg.endswith("true") for arg in calls[0])
        # Second call is dcmsend.
        assert "localhost" in calls[1] or any(
            "4006" == a for a in calls[1]
        )
