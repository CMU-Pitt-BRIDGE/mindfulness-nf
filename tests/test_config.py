"""Tests for frozen configuration dataclasses.

Core tests: NO mocks. Direct assertions on frozen dataclasses.
"""

import copy
from dataclasses import FrozenInstanceError

import pytest

from mindfulness_nf.config import PipelineConfig, ScannerConfig


class TestScannerConfigFrozen:
    """ScannerConfig is frozen and rejects mutation."""

    def test_frozen_scanner_ip(self, scanner_config: ScannerConfig) -> None:
        with pytest.raises(FrozenInstanceError):
            scanner_config.scanner_ip = "10.0.0.1"  # type: ignore[misc]

    def test_frozen_vsend_port(self, scanner_config: ScannerConfig) -> None:
        with pytest.raises(FrozenInstanceError):
            scanner_config.vsend_port = 9999  # type: ignore[misc]

    def test_frozen_dicom_port(self, scanner_config: ScannerConfig) -> None:
        with pytest.raises(FrozenInstanceError):
            scanner_config.dicom_port = 9999  # type: ignore[misc]

    def test_frozen_dicom_ae_title(self, scanner_config: ScannerConfig) -> None:
        with pytest.raises(FrozenInstanceError):
            scanner_config.dicom_ae_title = "OTHER"  # type: ignore[misc]

    def test_frozen_infoserver_port(self, scanner_config: ScannerConfig) -> None:
        with pytest.raises(FrozenInstanceError):
            scanner_config.infoserver_port = 9999  # type: ignore[misc]

    def test_frozen_murfi_container(self, scanner_config: ScannerConfig) -> None:
        with pytest.raises(FrozenInstanceError):
            scanner_config.murfi_container = "/tmp/other.sif"  # type: ignore[misc]


class TestScannerConfigDefaults:
    """ScannerConfig default values match verified constants."""

    def test_scanner_ip(self, scanner_config: ScannerConfig) -> None:
        assert scanner_config.scanner_ip == "192.168.2.1"

    def test_vsend_port(self, scanner_config: ScannerConfig) -> None:
        assert scanner_config.vsend_port == 50000

    def test_dicom_port(self, scanner_config: ScannerConfig) -> None:
        assert scanner_config.dicom_port == 4006

    def test_dicom_ae_title(self, scanner_config: ScannerConfig) -> None:
        assert scanner_config.dicom_ae_title == "MURFI"

    def test_infoserver_port(self, scanner_config: ScannerConfig) -> None:
        assert scanner_config.infoserver_port == 15001

    def test_murfi_container(self, scanner_config: ScannerConfig) -> None:
        assert scanner_config.murfi_container == "/opt/murfi/apptainer-images/murfi.sif"


class TestPipelineConfigFrozen:
    """PipelineConfig is frozen and rejects mutation."""

    def test_frozen_tr(self, pipeline_config: PipelineConfig) -> None:
        with pytest.raises(FrozenInstanceError):
            pipeline_config.tr = 2.0  # type: ignore[misc]

    def test_frozen_twovol_measurements(self, pipeline_config: PipelineConfig) -> None:
        with pytest.raises(FrozenInstanceError):
            pipeline_config.twovol_measurements = 99  # type: ignore[misc]

    def test_frozen_rest_measurements(self, pipeline_config: PipelineConfig) -> None:
        with pytest.raises(FrozenInstanceError):
            pipeline_config.rest_measurements = 99  # type: ignore[misc]

    def test_frozen_feedback_measurements(self, pipeline_config: PipelineConfig) -> None:
        with pytest.raises(FrozenInstanceError):
            pipeline_config.feedback_measurements = 99  # type: ignore[misc]

    def test_frozen_default_scale_factor(self, pipeline_config: PipelineConfig) -> None:
        with pytest.raises(FrozenInstanceError):
            pipeline_config.default_scale_factor = 99.0  # type: ignore[misc]


class TestPipelineConfigDefaults:
    """PipelineConfig default values match verified constants."""

    def test_tr(self, pipeline_config: PipelineConfig) -> None:
        assert pipeline_config.tr == 1.2

    def test_twovol_measurements(self, pipeline_config: PipelineConfig) -> None:
        assert pipeline_config.twovol_measurements == 20

    def test_rest_measurements(self, pipeline_config: PipelineConfig) -> None:
        assert pipeline_config.rest_measurements == 250

    def test_feedback_measurements(self, pipeline_config: PipelineConfig) -> None:
        assert pipeline_config.feedback_measurements == 150

    def test_psychopy_duration(self, pipeline_config: PipelineConfig) -> None:
        assert pipeline_config.psychopy_duration == 150

    def test_ica_components(self, pipeline_config: PipelineConfig) -> None:
        assert pipeline_config.ica_components == 128

    def test_default_scale_factor(self, pipeline_config: PipelineConfig) -> None:
        assert pipeline_config.default_scale_factor == 10.0

    def test_min_hits_per_tr(self, pipeline_config: PipelineConfig) -> None:
        assert pipeline_config.min_hits_per_tr == 3

    def test_max_hits_per_tr(self, pipeline_config: PipelineConfig) -> None:
        assert pipeline_config.max_hits_per_tr == 5

    def test_scale_increase(self, pipeline_config: PipelineConfig) -> None:
        assert pipeline_config.scale_increase == 1.25

    def test_scale_decrease(self, pipeline_config: PipelineConfig) -> None:
        assert pipeline_config.scale_decrease == 0.75


class TestCopyReplace:
    """Frozen configs support immutable updates via copy.replace()."""

    def test_scanner_config_replace(self, scanner_config: ScannerConfig) -> None:
        updated = copy.replace(scanner_config, scanner_ip="10.0.0.1")
        assert updated.scanner_ip == "10.0.0.1"
        assert scanner_config.scanner_ip == "192.168.2.1"

    def test_pipeline_config_replace(self, pipeline_config: PipelineConfig) -> None:
        updated = copy.replace(pipeline_config, tr=2.0)
        assert updated.tr == 2.0
        assert pipeline_config.tr == 1.2
