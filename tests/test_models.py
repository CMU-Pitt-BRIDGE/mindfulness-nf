"""Tests for mindfulness_nf.models — functional core, no mocks."""

from __future__ import annotations

import copy

import pytest

from mindfulness_nf.models import (
    CheckResult,
    Color,
    RunState,
    TrafficLight,
)

# Note: new SessionState (engine) is tested in tests/test_session_state.py.
# NF_RUN_SEQUENCE moved to mindfulness_nf.sessions; configs covered there.


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


# NF_RUN_SEQUENCE test class removed — configs are now in
# mindfulness_nf.sessions and tested by tests/test_session_state.py
# (see test_rt30_has_15_steps, test_feedback_blocks_are_numbered_consecutively).
