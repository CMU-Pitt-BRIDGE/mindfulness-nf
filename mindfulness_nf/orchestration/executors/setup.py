"""SETUP step executor: runs preflight checks, no subprocesses."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import CheckResult, StepConfig
from mindfulness_nf.orchestration.executor import (
    Component,
    ProgressCallback,
    StepOutcome,
    StepProgress,
)
from mindfulness_nf.orchestration.preflight import run_preflight

# Preflight check names whose failures are ignorable in --dry-run. These are
# all the checks that depend on a real scanner / real scanner network.
_SCANNER_DEPENDENT_CHECK_KEYWORDS = ("scanner", "ethernet", "wi-fi", "firewall")


def _is_scanner_dependent(check: CheckResult) -> bool:
    lowered = check.name.lower()
    return any(kw in lowered for kw in _SCANNER_DEPENDENT_CHECK_KEYWORDS)

__all__ = ["SetupStepExecutor"]


class SetupStepExecutor:
    """Runs :func:`run_preflight` and reports per-check stage progress.

    No subprocesses, no components to relaunch. ``advance_phase`` is a no-op
    because SETUP is single-phase.
    """

    def __init__(
        self,
        config: StepConfig,
        subject_dir: Path,
        pipeline: PipelineConfig,
        scanner_config: ScannerConfig,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._subject_dir = subject_dir
        self._pipeline = pipeline
        self._scanner_config = scanner_config
        self._dry_run = dry_run
        self._stopped = False

    async def run(self, on_progress: ProgressCallback) -> StepOutcome:
        if self._stopped:
            zero = StepProgress(value=0, target=1, unit="stages")
            return StepOutcome(succeeded=False, final_progress=zero, error="cancelled")
        try:
            raw_results = await run_preflight(
                self._scanner_config, subject_dir=self._subject_dir
            )
        except asyncio.CancelledError:
            zero = StepProgress(value=0, target=1, unit="stages")
            return StepOutcome(succeeded=False, final_progress=zero, error="cancelled")

        # In dry-run, coerce failures from scanner-network-dependent checks
        # to passed; local-machine checks (FSL, Apptainer, container, subject
        # dir, ports) still count because dry-run needs real local tools.
        if self._dry_run:
            results = tuple(
                CheckResult(
                    name=r.name, passed=True, message="skipped (dry-run)"
                )
                if (not r.passed and _is_scanner_dependent(r))
                else r
                for r in raw_results
            )
        else:
            results = raw_results

        total = len(results)
        all_passed = all(r.passed for r in results)
        # Emit one progress per result so UIs can show which check is underway.
        for idx, r in enumerate(results, start=1):
            detail = f"{r.name}: {r.message}"
            on_progress(
                StepProgress(value=idx, target=total, unit="stages", detail=detail)
            )

        final = StepProgress(
            value=total,
            target=total,
            unit="stages",
            detail="all checks passed" if all_passed else "some checks failed",
        )
        failed_names = ", ".join(r.name for r in results if not r.passed)
        artifacts: dict[str, Any] = {
            "checks": tuple({"name": r.name, "passed": r.passed, "message": r.message} for r in results),
        }
        return StepOutcome(
            succeeded=all_passed,
            final_progress=final,
            error=None if all_passed else f"preflight failed: {failed_names}",
            artifacts=artifacts,
        )

    async def stop(self, timeout: float = 5.0) -> None:
        self._stopped = True

    async def relaunch(self, component: Component) -> None:
        return None

    def components(self) -> tuple[Component, ...]:
        return ()

    def advance_phase(self) -> None:
        return None
