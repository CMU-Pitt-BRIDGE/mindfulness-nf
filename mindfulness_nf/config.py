"""Frozen configuration dataclasses for scanner and pipeline constants.

No I/O imports permitted in this module (FCIS boundary).
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScannerConfig:
    """Network and transfer configuration for the MRI scanner."""

    scanner_ip: str = "192.168.2.1"
    vsend_port: int = 50000
    dicom_port: int = 4006
    dicom_ae_title: str = "MURFI"
    infoserver_port: int = 15001
    murfi_container: str = "/opt/murfi/apptainer-images/murfi.sif"


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Protocol constants for scan acquisition and neurofeedback."""

    tr: float = 1.2
    twovol_measurements: int = 20
    rest_measurements: int = 250
    feedback_measurements: int = 150
    psychopy_duration: int = 150
    ica_components: int = 128
    default_scale_factor: float = 10.0
    min_hits_per_tr: int = 3
    max_hits_per_tr: int = 5
    scale_increase: float = 1.25
    scale_decrease: float = 0.75
