"""SETUP step executor: clean-slate remediation, then preflight verification."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import CheckResult, CleanupAction, StepConfig
from mindfulness_nf.orchestration.cleanup import cleanup_stale_processes
from mindfulness_nf.orchestration.executor import (
    Component,
    ProgressCallback,
    StepOutcome,
    StepProgress,
)
from mindfulness_nf.orchestration.preflight import run_preflight

# Preflight check names whose failures are ignorable in --dry-run. These
# are all the checks that depend on a real scanner / scanner network.
_SCANNER_DEPENDENT_CHECK_KEYWORDS = ("scanner", "ethernet", "wi-fi", "firewall")


def _is_scanner_dependent(check: CheckResult) -> bool:
    lowered = check.name.lower()
    return any(kw in lowered for kw in _SCANNER_DEPENDENT_CHECK_KEYWORDS)


__all__ = ["SetupStepExecutor"]


class SetupStepExecutor:
    """Clean-slate Setup: kill stale processes, then run the 13 checks.

    Two phases, both emitted as ``StepProgress`` so the TUI can show the
    operator what is happening:

    1. **Cleanup** — :func:`cleanup_stale_processes` kills any process on
       our configured ports (``vsend``, ``infoserver``, ``dicom``) plus
       known orphan patterns (``dicom_receiver.py``, ``murfi.sif``, the
       ``launch_murfi.sh`` wrapper) and any leftover ``murfi_scan`` tmux
       session. Runs unconditionally — dry-run still needs a clean machine.
    2. **Verification** — :func:`run_preflight` runs the 13 detection
       checks. After cleanup they should all pass locally; dry-run still
       coerces scanner-network-dependent failures to pass.

    No subprocesses to manage mid-step, no components to relaunch.
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

        # -- Phase 1: cleanup -------------------------------------------
        try:
            actions = await cleanup_stale_processes(self._scanner_config)
        except asyncio.CancelledError:
            zero = StepProgress(value=0, target=1, unit="stages")
            return StepOutcome(succeeded=False, final_progress=zero, error="cancelled")

        cleanup_failed = tuple(a for a in actions if not a.killed)
        # Emit one progress per cleanup action so the TUI sees a live trail.
        for idx, action in enumerate(actions, start=1):
            verb = "killed" if action.killed else "FAILED"
            pid_str = f" pid={action.pid}" if action.pid is not None else ""
            detail = f"cleanup: {verb} {action.target}{pid_str} — {action.message}"
            on_progress(
                StepProgress(
                    value=idx,
                    target=max(len(actions), 1),
                    unit="stages",
                    detail=detail,
                )
            )

        # -- Phase 2: verification --------------------------------------
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
        checks_passed = all(r.passed for r in results)
        # Emit one progress per result so UIs can show which check is underway.
        for idx, r in enumerate(results, start=1):
            detail = f"{r.name}: {r.message}"
            on_progress(
                StepProgress(value=idx, target=total, unit="stages", detail=detail)
            )

        all_passed = checks_passed and not cleanup_failed
        final = StepProgress(
            value=total,
            target=total,
            unit="stages",
            detail="all checks passed" if all_passed else "some checks failed",
        )
        failed_names = ", ".join(r.name for r in results if not r.passed)
        artifacts: dict[str, Any] = {
            "cleanup_actions": tuple(
                _action_to_dict(a) for a in actions
            ),
            "checks": tuple(
                {"name": r.name, "passed": r.passed, "message": r.message}
                for r in results
            ),
        }
        if all_passed:
            error = None
        elif cleanup_failed and failed_names:
            error = f"cleanup incomplete; preflight failed: {failed_names}"
        elif cleanup_failed:
            pids = ", ".join(
                f"pid {a.pid}" if a.pid is not None else a.target
                for a in cleanup_failed
            )
            error = f"cleanup incomplete: could not kill {pids}"
        else:
            error = f"preflight failed: {failed_names}"
        return StepOutcome(
            succeeded=all_passed,
            final_progress=final,
            error=error,
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


def _action_to_dict(action: CleanupAction) -> dict[str, Any]:
    return {
        "target": action.target,
        "pid": action.pid,
        "killed": action.killed,
        "message": action.message,
    }
