"""Shared test fixtures for mindfulness-nf."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import SessionState, StepState
from mindfulness_nf.sessions import SESSION_CONFIGS


# ---------------------------------------------------------------------------
# Existing fixtures (unchanged)
# ---------------------------------------------------------------------------


@pytest.fixture
def scanner_config() -> ScannerConfig:
    """Default scanner configuration."""
    return ScannerConfig()


@pytest.fixture
def pipeline_config() -> PipelineConfig:
    """Default pipeline configuration."""
    return PipelineConfig()


# ---------------------------------------------------------------------------
# Runner-level fixtures (todo-9)
# ---------------------------------------------------------------------------


@pytest.fixture
def scanner_config_test() -> ScannerConfig:
    """Sensible scanner defaults for runner/executor tests."""
    return ScannerConfig()


@pytest.fixture
def pipeline_config_test() -> PipelineConfig:
    """Sensible pipeline defaults for runner/executor tests."""
    return PipelineConfig()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def fresh_state(tmp_path: Path):
    """Factory: build a fresh SessionState for the given session_type.

    Usage in tests:
        def test_foo(fresh_state):
            state = fresh_state("rt15")
            ...
    """

    def _factory(session_type: str) -> SessionState:
        configs = SESSION_CONFIGS[session_type]
        now = _now_iso()
        return SessionState(
            subject="sub-test",
            session_type=session_type,
            cursor=0,
            steps=tuple(StepState(config=c) for c in configs),
            created_at=now,
            updated_at=now,
        )

    return _factory
