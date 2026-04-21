"""Concrete :class:`StepExecutor` implementations wired to existing helpers.

SessionRunner dispatches on ``StepConfig.kind`` and constructs the matching
executor from this package. Each module wraps one family of orchestration
helpers (``murfi``, ``psychopy``, ``dicom_receiver``, ``preflight``, ``ica``)
without modifying them.
"""

from __future__ import annotations

from mindfulness_nf.orchestration.executors.dicom import DicomStepExecutor
from mindfulness_nf.orchestration.executors.fsl_stage import FslStageExecutor
from mindfulness_nf.orchestration.executors.nf_run import NfRunStepExecutor
from mindfulness_nf.orchestration.executors.setup import SetupStepExecutor
from mindfulness_nf.orchestration.executors.vsend import VsendStepExecutor

__all__ = [
    "DicomStepExecutor",
    "FslStageExecutor",
    "NfRunStepExecutor",
    "SetupStepExecutor",
    "VsendStepExecutor",
]
