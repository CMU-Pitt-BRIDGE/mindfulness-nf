"""Shared test fixtures for mindfulness-nf."""

import pytest

from mindfulness_nf.config import PipelineConfig, ScannerConfig


@pytest.fixture
def scanner_config() -> ScannerConfig:
    """Default scanner configuration."""
    return ScannerConfig()


@pytest.fixture
def pipeline_config() -> PipelineConfig:
    """Default pipeline configuration."""
    return PipelineConfig()
