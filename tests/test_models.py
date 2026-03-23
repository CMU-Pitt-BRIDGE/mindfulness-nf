"""Tests for mindfulness_nf.models — functional core, no mocks."""

from __future__ import annotations

import copy

import pytest

from mindfulness_nf.models import (
    CheckResult,
    Color,
    NF_RUN_SEQUENCE,
    RunState,
    SessionState,
    TrafficLight,
)


# ---------------------------------------------------------------------------
# Color enum
# ---------------------------------------------------------------------------


class TestColor:
    def test_has_three_values(self) -> None:
        assert len(Color) == 3

    def test_values(self) -> None:
        assert Color.GREEN.value == "green"
        assert Color.YELLOW.value == "yellow"
        assert Color.RED.value == "red"


# ---------------------------------------------------------------------------
# TrafficLight
# ---------------------------------------------------------------------------


class TestTrafficLight:
    def test_frozen(self) -> None:
        tl = TrafficLight(color=Color.GREEN, message="ok")
        with pytest.raises(AttributeError, match="cannot assign to field"):
            tl.color = Color.RED  # type: ignore[misc]

    def test_default_detail_is_none(self) -> None:
        tl = TrafficLight(color=Color.GREEN, message="ok")
        assert tl.detail is None

    def test_detail_stored(self) -> None:
        tl = TrafficLight(color=Color.GREEN, message="ok", detail="extra info")
        assert tl.detail == "extra info"

    def test_blocks_advance_green(self) -> None:
        assert TrafficLight(color=Color.GREEN, message="ok").blocks_advance is False

    def test_blocks_advance_yellow(self) -> None:
        assert TrafficLight(color=Color.YELLOW, message="warn").blocks_advance is False

    def test_blocks_advance_red(self) -> None:
        assert TrafficLight(color=Color.RED, message="bad").blocks_advance is True


# ---------------------------------------------------------------------------
# RunState
# ---------------------------------------------------------------------------


class TestRunState:
    def test_frozen(self) -> None:
        rs = RunState(name="test", expected_volumes=20)
        with pytest.raises(AttributeError, match="cannot assign to field"):
            rs.received_volumes = 5  # type: ignore[misc]

    def test_defaults(self) -> None:
        rs = RunState(name="test", expected_volumes=20)
        assert rs.received_volumes == 0
        assert rs.feedback is False
        assert rs.scale_factor is None

    def test_with_volumes_returns_new_instance(self) -> None:
        original = RunState(name="test", expected_volumes=20, received_volumes=0)
        updated = original.with_volumes(15)
        assert updated.received_volumes == 15
        assert original.received_volumes == 0
        assert updated is not original

    def test_with_volumes_preserves_other_fields(self) -> None:
        original = RunState(
            name="fb", expected_volumes=150, feedback=True, scale_factor=10.0
        )
        updated = original.with_volumes(140)
        assert updated.name == "fb"
        assert updated.expected_volumes == 150
        assert updated.feedback is True
        assert updated.scale_factor == 10.0

    def test_copy_replace(self) -> None:
        rs = RunState(name="test", expected_volumes=20)
        rs2 = copy.replace(rs, received_volumes=10)
        assert rs2.received_volumes == 10
        assert rs.received_volumes == 0


# ---------------------------------------------------------------------------
# SessionState
# ---------------------------------------------------------------------------


class TestSessionState:
    @pytest.fixture()
    def three_step_session(self) -> SessionState:
        steps = (
            RunState(name="step1", expected_volumes=20),
            RunState(name="step2", expected_volumes=250),
            RunState(name="step3", expected_volumes=250),
        )
        return SessionState(
            subject="sub-001", session_type="localizer", steps=steps
        )

    def test_frozen(self, three_step_session: SessionState) -> None:
        with pytest.raises(AttributeError, match="cannot assign to field"):
            three_step_session.current_step = 1  # type: ignore[misc]

    def test_defaults(self, three_step_session: SessionState) -> None:
        assert three_step_session.current_step == 0
        assert three_step_session.completed is False

    def test_advance_increments_step(self, three_step_session: SessionState) -> None:
        advanced = three_step_session.advance()
        assert advanced.current_step == 1
        assert advanced.completed is False
        # original unchanged
        assert three_step_session.current_step == 0

    def test_advance_twice(self, three_step_session: SessionState) -> None:
        s1 = three_step_session.advance()
        s2 = s1.advance()
        assert s2.current_step == 2
        assert s2.completed is False

    def test_advance_at_last_step_completes(
        self, three_step_session: SessionState
    ) -> None:
        s = three_step_session
        s = s.advance()  # step 1
        s = s.advance()  # step 2 (last)
        s = s.advance()  # should complete
        assert s.completed is True
        assert s.current_step == 2  # stays at last step

    def test_advance_single_step_session(self) -> None:
        steps = (RunState(name="only", expected_volumes=20),)
        ss = SessionState(subject="sub-x", session_type="test", steps=steps)
        advanced = ss.advance()
        assert advanced.completed is True
        assert advanced.current_step == 0


# ---------------------------------------------------------------------------
# CheckResult
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_frozen(self) -> None:
        cr = CheckResult(name="fsl", passed=True, message="found")
        with pytest.raises(AttributeError, match="cannot assign to field"):
            cr.passed = False  # type: ignore[misc]

    def test_fields(self) -> None:
        cr = CheckResult(name="fsl", passed=False, message="not found")
        assert cr.name == "fsl"
        assert cr.passed is False
        assert cr.message == "not found"


# ---------------------------------------------------------------------------
# NF_RUN_SEQUENCE
# ---------------------------------------------------------------------------


class TestNFRunSequence:
    def test_length(self) -> None:
        assert len(NF_RUN_SEQUENCE) == 12

    def test_is_tuple_of_tuples(self) -> None:
        assert isinstance(NF_RUN_SEQUENCE, tuple)
        for entry in NF_RUN_SEQUENCE:
            assert isinstance(entry, tuple)
            assert len(entry) == 2

    def test_run_1_transfer_pre(self) -> None:
        assert NF_RUN_SEQUENCE[0] == ("Transfer Pre", False)

    def test_runs_2_through_6_feedback(self) -> None:
        for i in range(1, 6):
            name, feedback = NF_RUN_SEQUENCE[i]
            assert name == f"Feedback {i}"
            assert feedback is True

    def test_run_7_transfer_post(self) -> None:
        assert NF_RUN_SEQUENCE[6] == ("Transfer Post", False)

    def test_runs_8_through_12_feedback(self) -> None:
        for i in range(7, 12):
            name, feedback = NF_RUN_SEQUENCE[i]
            expected_num = i - 1  # index 7 -> Feedback 6, etc.
            assert name == f"Feedback {expected_num}"
            assert feedback is True

    def test_names_unique(self) -> None:
        names = [name for name, _ in NF_RUN_SEQUENCE]
        assert len(names) == len(set(names))

    def test_feedback_count(self) -> None:
        feedback_runs = [f for _, f in NF_RUN_SEQUENCE if f]
        assert len(feedback_runs) == 10

    def test_non_feedback_count(self) -> None:
        non_feedback = [f for _, f in NF_RUN_SEQUENCE if not f]
        assert len(non_feedback) == 2
