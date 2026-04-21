"""Layer 1 tests: pure SessionState transitions.

TDD red: imports below don't exist yet (they land in todo-5 with the
replacement of ``mindfulness_nf/models.py``). This file is the executable
specification for the state machine described in
``docs/superpowers/specs/2026-04-20-session-runner-design.md``.

All transitions under test are pure: each returns a new ``SessionState``.
No I/O, no mocks — only assertions about inputs and outputs.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mindfulness_nf.models import (
    SessionState,
    StepConfig,
    StepKind,
    StepState,
    StepStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(name: str, *, kind: StepKind = StepKind.NF_RUN, run: int | None = 1) -> StepConfig:
    """Build a minimal, valid StepConfig for tests."""
    return StepConfig(
        name=name,
        task="feedback",
        run=run,
        progress_target=150,
        progress_unit="volumes",
        xml_name="rtdmn.xml",
        kind=kind,
        feedback=True,
        fsl_command=None,
    )


def _make_state(session_type: str = "rt15") -> SessionState:
    """Build a 4-step SessionState for tests.

    Steps: Setup (setup kind) + three NF runs. Cursor starts at 0,
    all steps pending.
    """
    steps: tuple[StepState, ...] = (
        StepState(
            config=StepConfig(
                name="Setup",
                task=None,
                run=None,
                progress_target=0,
                progress_unit="stages",
                xml_name=None,
                kind=StepKind.SETUP,
                feedback=False,
                fsl_command=None,
            ),
        ),
        StepState(config=_config("Feedback 1", run=1)),
        StepState(config=_config("Feedback 2", run=2)),
        StepState(config=_config("Feedback 3", run=3)),
    )
    return SessionState(
        subject="sub-test",
        session_type=session_type,
        cursor=0,
        steps=steps,
        created_at="2026-04-20T00:00:00Z",
        updated_at="2026-04-20T00:00:00Z",
    )


TS = "2026-04-20T12:00:00Z"
TS2 = "2026-04-20T12:05:00Z"


# ---------------------------------------------------------------------------
# Cursor navigation
# ---------------------------------------------------------------------------


def test_advance_moves_cursor_forward_by_one() -> None:
    """advance() increments cursor while leaving steps untouched."""
    state = _make_state()

    result = state.advance()

    assert result.cursor == 1
    assert result.steps == state.steps


def test_advance_at_last_step_is_clamped_noop() -> None:
    """advance() at the final index returns a state with cursor unchanged."""
    state = _make_state().select(3)  # len(steps) - 1 == 3

    result = state.advance()

    assert result.cursor == 3


def test_go_back_moves_cursor_backward_by_one() -> None:
    """go_back() decrements cursor."""
    state = _make_state().select(2)

    result = state.go_back()

    assert result.cursor == 1


def test_go_back_at_index_zero_is_clamped_noop() -> None:
    """go_back() at index 0 keeps cursor at 0."""
    state = _make_state()

    result = state.go_back()

    assert result.cursor == 0


def test_select_clamps_negative_and_out_of_range() -> None:
    """select() clamps to [0, len(steps))."""
    state = _make_state()

    low = state.select(-5)
    high = state.select(999)

    assert low.cursor == 0
    assert high.cursor == len(state.steps) - 1


def test_cursor_navigation_never_changes_step_status() -> None:
    """advance/go_back/select leave every step's status untouched."""
    state = _make_state()
    original_statuses = tuple(s.status for s in state.steps)

    for moved in (state.advance(), state.go_back(), state.select(2), state.select(-1)):
        assert tuple(s.status for s in moved.steps) == original_statuses


# ---------------------------------------------------------------------------
# mark_running / mark_completed / mark_failed
# ---------------------------------------------------------------------------


def test_mark_running_sets_status_and_last_started() -> None:
    """mark_running records status=RUNNING and the start timestamp."""
    state = _make_state()

    result = state.mark_running(1, TS)

    assert result.steps[1].status == StepStatus.RUNNING
    assert result.steps[1].last_started == TS


def test_mark_running_refuses_when_another_step_already_running() -> None:
    """mark_running raises if a different step is already RUNNING.

    Invariant: at most one step in RUNNING status at any time. The
    state-level enforcement makes the bug loud; runner-level guard is
    where the operator-facing refusal lives.
    """
    state = _make_state().mark_running(1, TS)

    with pytest.raises(ValueError, match="running"):
        state.mark_running(2, TS2)


def test_mark_completed_sets_last_finished_and_artifacts() -> None:
    """mark_completed records finish timestamp and attaches artifacts."""
    state = _make_state().mark_running(1, TS)
    artifacts: dict[str, Any] = {"scale_factor": 1.42}

    result = state.mark_completed(1, TS2, artifacts=artifacts)

    assert result.steps[1].status == StepStatus.COMPLETED
    assert result.steps[1].last_finished == TS2
    assert result.steps[1].artifacts == artifacts


def test_mark_completed_clears_prior_error_field() -> None:
    """A successful completion wipes a stale error from a previous attempt."""
    state = (
        _make_state()
        .mark_running(1, TS)
        .mark_failed(1, TS2, error="transient")
        .clear_current()
        .select(1)
        .mark_running(1, TS)
    )

    result = state.mark_completed(1, TS2)

    assert result.steps[1].error is None


def test_mark_failed_from_running_records_error() -> None:
    """mark_failed from RUNNING stores the error string."""
    state = _make_state().mark_running(1, TS)

    result = state.mark_failed(1, TS2, error="MURFI exited 1")

    assert result.steps[1].status == StepStatus.FAILED
    assert result.steps[1].error == "MURFI exited 1"


def test_mark_failed_allowed_from_running_only() -> None:
    """Calling mark_failed on a non-running step is rejected.

    The supervisor only ever transitions RUNNING → FAILED. Any other
    origin indicates a bug in the caller.
    """
    state = _make_state()  # step 1 is PENDING

    with pytest.raises(ValueError, match="running"):
        state.mark_failed(1, TS, error="nope")


# ---------------------------------------------------------------------------
# clear_current / set_progress
# ---------------------------------------------------------------------------


def test_clear_current_resets_all_per_attempt_fields() -> None:
    """clear_current wipes per-attempt fields on the cursor step."""
    state = (
        _make_state()
        .select(1)
        .mark_running(1, TS)
        .set_progress(1, 87, detail="mid-run", phase="murfi", awaiting_advance=True)
        .mark_failed(1, TS2, error="crash")
    )

    result = state.clear_current()
    step = result.steps[1]

    assert step.status == StepStatus.PENDING
    assert step.progress_current == 0
    assert step.detail_message is None
    assert step.error is None
    assert step.phase is None
    assert step.awaiting_advance is False
    assert step.last_started is None
    assert step.last_finished is None
    assert step.artifacts is None


def test_clear_current_increments_attempts() -> None:
    """Each clear_current bumps attempts by one."""
    state = _make_state().select(1)

    once = state.clear_current()
    twice = once.clear_current()

    assert once.steps[1].attempts == state.steps[1].attempts + 1
    assert twice.steps[1].attempts == state.steps[1].attempts + 2


def test_clear_current_preserves_config() -> None:
    """StepConfig is not touched by clear_current."""
    state = _make_state().select(1)
    original_config = state.steps[1].config

    result = state.clear_current()

    assert result.steps[1].config == original_config


def test_clear_current_does_not_touch_other_steps() -> None:
    """clear_current rewrites only the cursor step."""
    state = (
        _make_state()
        .select(0)
        .mark_running(0, TS)
        .mark_completed(0, TS2)
        .select(2)
        .mark_running(2, TS)
        .mark_failed(2, TS2, error="boom")
        .select(1)
    )

    result = state.clear_current()

    assert result.steps[0] == state.steps[0]
    assert result.steps[2] == state.steps[2]
    assert result.steps[3] == state.steps[3]


def test_set_progress_updates_value_detail_phase_awaiting() -> None:
    """set_progress writes all four narrative fields at once."""
    state = _make_state().select(1).mark_running(1, TS)

    result = state.set_progress(
        1, 87, detail="vol 87/150", phase="murfi", awaiting_advance=True
    )
    step = result.steps[1]

    assert step.progress_current == 87
    assert step.detail_message == "vol 87/150"
    assert step.phase == "murfi"
    assert step.awaiting_advance is True


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def test_cursor_and_running_index_can_diverge() -> None:
    """The operator can navigate away from the running step."""
    state = _make_state().mark_running(1, TS).select(3)

    assert state.cursor == 3
    assert state.running_index == 1


def test_running_index_is_unique() -> None:
    """running_index returns the one RUNNING step, or None."""
    pending = _make_state()
    running = pending.mark_running(2, TS)
    finished = running.mark_completed(2, TS2)

    assert pending.running_index is None
    assert running.running_index == 2
    assert finished.running_index is None


def test_session_complete_derived_from_all_completed() -> None:
    """Session completion is derived from statuses, not stored."""
    state = _make_state()
    all_done = state
    for i in range(len(state.steps)):
        all_done = all_done.mark_running(i, TS).mark_completed(i, TS2)

    none_done = state
    almost = state
    for i in range(len(state.steps) - 1):
        almost = almost.mark_running(i, TS).mark_completed(i, TS2)

    assert all(s.status == StepStatus.COMPLETED for s in all_done.steps)
    assert not all(s.status == StepStatus.COMPLETED for s in none_done.steps)
    assert not all(s.status == StepStatus.COMPLETED for s in almost.steps)


# ---------------------------------------------------------------------------
# Property-based sweep
# ---------------------------------------------------------------------------


NUM_STEPS = 4  # matches _make_state()

# One tagged-union strategy per operation. Each "op" is a tuple whose
# first element is a discriminator; the remaining fields are args.
_advance_op = st.tuples(st.just("advance"))
_back_op = st.tuples(st.just("back"))
_select_op = st.tuples(st.just("select"), st.integers(min_value=-3, max_value=NUM_STEPS + 3))
_clear_op = st.tuples(st.just("clear"))
_mark_op = st.tuples(
    st.just("mark"),
    st.integers(min_value=0, max_value=NUM_STEPS - 1),
    st.sampled_from(("running", "completed", "failed")),
)
_progress_op = st.tuples(
    st.just("progress"),
    st.integers(min_value=0, max_value=NUM_STEPS - 1),
    st.integers(min_value=0, max_value=200),
    st.sampled_from((None, "murfi", "psychopy")),
    st.booleans(),
)

_op_strategy = st.one_of(
    _advance_op, _back_op, _select_op, _clear_op, _mark_op, _progress_op
)


def _apply_op(state: SessionState, op: tuple[Any, ...]) -> SessionState:
    """Best-effort dispatcher. Invalid transitions are swallowed: we want
    the invariant check to observe whatever the state machine allowed, and
    raising from transitions is a valid behavior that we simply skip over.
    """
    try:
        kind = op[0]
        if kind == "advance":
            return state.advance()
        if kind == "back":
            return state.go_back()
        if kind == "select":
            return state.select(op[1])
        if kind == "clear":
            return state.clear_current()
        if kind == "mark":
            i, verb = op[1], op[2]
            if verb == "running":
                return state.mark_running(i, TS)
            if verb == "completed":
                return state.mark_completed(i, TS2)
            if verb == "failed":
                return state.mark_failed(i, TS2, error="simulated")
        if kind == "progress":
            i, value, phase, awaiting = op[1], op[2], op[3], op[4]
            return state.set_progress(i, value, phase=phase, awaiting_advance=awaiting)
    except (ValueError, IndexError):
        return state
    return state


def _check_invariants(state: SessionState) -> None:
    """Invariants that must hold after every transition."""
    # Cursor in range.
    assert 0 <= state.cursor < len(state.steps)

    # At most one RUNNING step.
    running = [i for i, s in enumerate(state.steps) if s.status == StepStatus.RUNNING]
    assert len(running) <= 1

    # Field types match declarations on every StepState.
    for step in state.steps:
        assert isinstance(step.status, StepStatus)
        assert isinstance(step.attempts, int)
        assert isinstance(step.progress_current, int)
        assert step.last_started is None or isinstance(step.last_started, str)
        assert step.last_finished is None or isinstance(step.last_finished, str)
        assert step.detail_message is None or isinstance(step.detail_message, str)
        assert step.error is None or isinstance(step.error, str)
        assert step.phase is None or step.phase in ("murfi", "psychopy")
        assert isinstance(step.awaiting_advance, bool)
        assert step.artifacts is None or isinstance(step.artifacts, dict)


@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
@given(ops=st.lists(_op_strategy, min_size=0, max_size=20))
def test_invariants_hold_over_random_sequences(ops: list[tuple[Any, ...]]) -> None:
    """Arbitrary sequences of transitions never violate state invariants."""
    state = _make_state()
    _check_invariants(state)

    for op in ops:
        state = _apply_op(state, op)
        _check_invariants(state)


# ---------------------------------------------------------------------------
# Session config spot-checks (todo-8)
# ---------------------------------------------------------------------------


def test_loc3_has_3_steps() -> None:
    from mindfulness_nf.sessions import LOC3

    assert len(LOC3) == 3
    assert [s.name for s in LOC3] == ["Setup", "Rest 1", "Rest 2"]


def test_rt15_has_9_steps() -> None:
    from mindfulness_nf.sessions import RT15

    assert len(RT15) == 9
    assert RT15[0].name == "Setup"
    assert RT15[1].name == "2-volume"
    assert RT15[2].name == "Transfer Pre"
    assert [s.name for s in RT15[3:8]] == [f"Feedback {i}" for i in range(1, 6)]
    assert RT15[-1].name == "Transfer Post"


def test_rt30_has_15_steps() -> None:
    from mindfulness_nf.sessions import RT30

    assert len(RT30) == 15
    names = [s.name for s in RT30]
    assert names[0:3] == ["Setup", "2-volume", "Transfer Pre"]
    assert names[3:8] == [f"Feedback {i}" for i in range(1, 6)]
    assert names[8] == "Transfer Post 1"
    assert names[9:14] == [f"Feedback {i}" for i in range(6, 11)]
    assert names[14] == "Transfer Post 2"


def test_process_has_7_steps() -> None:
    from mindfulness_nf.sessions import PROCESS

    assert len(PROCESS) == 7
    fsl_commands = [s.fsl_command for s in PROCESS if s.fsl_command]
    assert "melodic" in fsl_commands
    assert PROCESS[-1].name == "QC"


def test_feedback_blocks_are_numbered_consecutively() -> None:
    from mindfulness_nf.sessions import RT30

    feedback_runs = [s.run for s in RT30 if s.task == "feedback"]
    assert feedback_runs == list(range(1, 11))


def test_bids_task_names_match_scanner_pdfs() -> None:
    """Expected task labels from materials/mri_sequences/{LOC3,RT15,RT30}.pdf."""
    from mindfulness_nf.sessions import LOC3, RT15, RT30

    assert {s.task for s in LOC3 if s.task} == {"rest"}
    assert {s.task for s in RT15 if s.task} == {
        "2vol", "transferpre", "feedback", "transferpost",
    }
    assert {s.task for s in RT30 if s.task} == {
        "2vol", "transferpre", "feedback", "transferpost",
    }


def test_all_feedback_runs_have_feedback_true() -> None:
    from mindfulness_nf.sessions import RT15, RT30

    for cfg in list(RT15) + list(RT30):
        if cfg.task == "feedback":
            assert cfg.feedback is True, f"{cfg.name} should be feedback=True"
        elif cfg.task in ("transferpre", "transferpost"):
            assert cfg.feedback is False, f"{cfg.name} should be feedback=False"
