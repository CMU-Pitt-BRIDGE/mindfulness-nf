"""Domain models for the mindfulness neurofeedback pipeline.

All models use frozen=True, slots=True. Collections are tuple, not list.
No I/O imports permitted in this module (FCIS boundary).
"""

from __future__ import annotations

import copy
import enum
from dataclasses import dataclass
from typing import assert_never


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
class RunState:
    """State of a single run within a session."""

    name: str
    expected_volumes: int
    received_volumes: int = 0
    feedback: bool = False
    scale_factor: float | None = None

    def with_volumes(self, count: int) -> RunState:
        """Return a new RunState with the given received volume count."""
        return copy.replace(self, received_volumes=count)


@dataclass(frozen=True, slots=True)
class SessionState:
    """State of a full session (localizer or neurofeedback)."""

    subject: str
    session_type: str
    steps: tuple[RunState, ...]
    current_step: int = 0
    completed: bool = False

    def advance(self) -> SessionState:
        """Advance to the next step, or mark completed if at end."""
        if self.current_step >= len(self.steps) - 1:
            return copy.replace(self, completed=True)
        return copy.replace(self, current_step=self.current_step + 1)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of a single preflight check."""

    name: str
    passed: bool
    message: str


NF_RUN_SEQUENCE: tuple[tuple[str, bool], ...] = (
    ("Transfer Pre", False),
    ("Feedback 1", True),
    ("Feedback 2", True),
    ("Feedback 3", True),
    ("Feedback 4", True),
    ("Feedback 5", True),
    ("Transfer Post", False),
    ("Feedback 6", True),
    ("Feedback 7", True),
    ("Feedback 8", True),
    ("Feedback 9", True),
    ("Feedback 10", True),
)
