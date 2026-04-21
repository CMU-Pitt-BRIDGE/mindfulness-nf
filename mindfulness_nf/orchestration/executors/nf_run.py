"""NF_RUN executor: phase 1 MURFI volume acquisition, phase 2 PsychoPy feedback."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import StepConfig
from mindfulness_nf.orchestration import murfi as murfi_mod
from mindfulness_nf.orchestration import psychopy as psychopy_mod
from mindfulness_nf.orchestration.executor import (
    Component,
    ProgressCallback,
    StepOutcome,
    StepProgress,
)
from mindfulness_nf.orchestration.scanner_source import ScannerSource

__all__ = ["NfRunStepExecutor"]

_VOLUME_LINE_RE = re.compile(r"received image from scanner")


class NfRunStepExecutor:
    """Two-phase step: MURFI acquires N volumes, then PsychoPy renders feedback.

    Between phases the executor emits a progress snapshot with
    ``awaiting_advance=True`` and waits on an ``asyncio.Event`` until
    :meth:`advance_phase` is invoked (operator presses D).
    """

    def __init__(
        self,
        config: StepConfig,
        subject_dir: Path,
        pipeline: PipelineConfig,
        scanner_config: ScannerConfig,
        scanner_source: ScannerSource,
        *,
        psychopy_data_dir: Path | None = None,
        duration: str = "15min",
        anchor: str = "",
    ) -> None:
        if config.xml_name is None:
            msg = f"NfRunStepExecutor requires StepConfig.xml_name (step={config.name})"
            raise ValueError(msg)
        if config.run is None:
            msg = f"NfRunStepExecutor requires StepConfig.run (step={config.name})"
            raise ValueError(msg)

        self._config = config
        self._subject_dir = subject_dir
        self._pipeline = pipeline
        self._scanner_config = scanner_config
        self._scanner_source = scanner_source
        self._psychopy_data_dir = psychopy_data_dir
        self._duration = duration
        self._anchor = anchor

        self._murfi: murfi_mod.MurfiProcess | None = None
        self._psychopy: asyncio.subprocess.Process | None = None
        self._log_baseline = 0
        self._volumes = 0
        self._target = config.progress_target

        self._advance_event = asyncio.Event()
        self._phase: str = "murfi"
        self._stopped = False
        self._relaunch_lock = asyncio.Lock()

    # ---- public protocol -------------------------------------------------

    async def run(self, on_progress: ProgressCallback) -> StepOutcome:
        try:
            await self._start_murfi()
        except Exception as exc:  # noqa: BLE001
            return StepOutcome(
                succeeded=False,
                final_progress=self._snapshot_murfi(),
                error=f"MURFI start failed: {exc}",
            )

        try:
            # -------------------------- PHASE 1: MURFI ----------------------
            phase1 = await self._run_phase1_murfi(on_progress)
            if phase1 is not None:
                return phase1  # failure short-circuit

            # Phase 1 succeeded: emit gate snapshot, wait for operator D press.
            on_progress(
                StepProgress(
                    value=self._volumes,
                    target=self._target,
                    unit=self._config.progress_unit,
                    phase="murfi",
                    detail="Press D to start PsychoPy",
                    awaiting_advance=True,
                )
            )
            await self._advance_event.wait()
            if self._stopped:
                await self._shutdown()
                return StepOutcome(
                    succeeded=False,
                    final_progress=self._snapshot_murfi(),
                    error="cancelled",
                )

            # -------------------------- PHASE 2: PsychoPy -------------------
            self._phase = "psychopy"
            return await self._run_phase2_psychopy(on_progress)
        except asyncio.CancelledError:
            await self._shutdown()
            return StepOutcome(
                succeeded=False,
                final_progress=self._current_snapshot(),
                error="cancelled",
            )

    async def stop(self, timeout: float = 5.0) -> None:
        self._stopped = True
        # Unblock any phase-gate await so run() can observe the stop.
        self._advance_event.set()
        await self._shutdown(timeout=timeout)

    async def relaunch(self, component: Component) -> None:
        if component == "murfi":
            async with self._relaunch_lock:
                if self._murfi is not None:
                    try:
                        self._log_baseline = self._murfi.log_path.stat().st_size
                    except OSError:
                        self._log_baseline = 0
                    await murfi_mod.stop(self._murfi)
                    self._murfi = None
                await self._start_murfi()
        elif component == "psychopy":
            async with self._relaunch_lock:
                if self._psychopy is not None and self._psychopy.returncode is None:
                    try:
                        self._psychopy.terminate()
                        await asyncio.wait_for(self._psychopy.wait(), timeout=5.0)
                    except (asyncio.TimeoutError, ProcessLookupError):
                        try:
                            self._psychopy.kill()
                            await self._psychopy.wait()
                        except ProcessLookupError:
                            pass
                self._psychopy = await self._launch_psychopy()
        # Unknown component: no-op.

    def components(self) -> tuple[Component, ...]:
        return ("murfi", "psychopy")

    def advance_phase(self) -> None:
        self._advance_event.set()

    # ---- phase helpers ---------------------------------------------------

    async def _run_phase1_murfi(
        self, on_progress: ProgressCallback
    ) -> StepOutcome | None:
        """Drive volume acquisition until target or MURFI dies. None on success."""
        while True:
            if self._stopped:
                await self._shutdown()
                return StepOutcome(
                    succeeded=False,
                    final_progress=self._snapshot_murfi(),
                    error="cancelled",
                )
            murfi = self._murfi
            assert murfi is not None

            new_text = await asyncio.to_thread(_read_from, murfi.log_path, self._log_baseline)
            if new_text:
                self._log_baseline += len(new_text.encode())
                for line in new_text.splitlines():
                    if _VOLUME_LINE_RE.search(line):
                        self._volumes += 1
                        on_progress(self._snapshot_murfi())

            if self._volumes >= self._target:
                return None

            if murfi.process.returncode is not None:
                rc = murfi.process.returncode
                await self._shutdown()
                return StepOutcome(
                    succeeded=False,
                    final_progress=self._snapshot_murfi(),
                    error=f"MURFI exited {rc}",
                )
            await asyncio.sleep(0.25)

    async def _run_phase2_psychopy(
        self, on_progress: ProgressCallback
    ) -> StepOutcome:
        """MURFI stays alive; PsychoPy is the completion signal.

        Handles PsychoPy crash by emitting a progress update and waiting for
        operator relaunch/stop rather than failing the whole step.
        """
        # Launch first PsychoPy instance.
        try:
            self._psychopy = await self._launch_psychopy()
        except Exception as exc:  # noqa: BLE001
            await self._shutdown()
            return StepOutcome(
                succeeded=False,
                final_progress=self._phase2_snapshot("PsychoPy launch failed"),
                error=f"PsychoPy launch failed: {exc}",
            )

        on_progress(self._phase2_snapshot("PsychoPy running"))

        while True:
            if self._stopped:
                await self._shutdown()
                return StepOutcome(
                    succeeded=False,
                    final_progress=self._phase2_snapshot("stopped"),
                    error="cancelled",
                )

            murfi = self._murfi
            if murfi is None or murfi.process.returncode is not None:
                rc = murfi.process.returncode if murfi is not None else -1
                await self._shutdown()
                return StepOutcome(
                    succeeded=False,
                    final_progress=self._phase2_snapshot("MURFI died"),
                    error=f"MURFI exited {rc}",
                )

            proc = self._psychopy
            if proc is not None and proc.returncode is not None:
                rc = proc.returncode
                if rc == 0:
                    artifacts = self._collect_artifacts()
                    await self._shutdown()
                    return StepOutcome(
                        succeeded=True,
                        final_progress=self._phase2_snapshot("PsychoPy complete"),
                        artifacts=artifacts,
                    )
                # PsychoPy crashed — keep MURFI alive and wait for operator.
                on_progress(
                    self._phase2_snapshot(
                        f"PsychoPy crashed (rc={rc}) — press P to relaunch"
                    )
                )
                self._psychopy = None
                # Spin until operator relaunches (self._psychopy set again) or stops.
                while self._psychopy is None and not self._stopped:
                    if murfi.process.returncode is not None:
                        break
                    await asyncio.sleep(0.25)
                if self._psychopy is not None:
                    on_progress(self._phase2_snapshot("PsychoPy running"))
                continue

            await asyncio.sleep(0.25)

    # ---- launchers / snapshots -------------------------------------------

    async def _start_murfi(self) -> None:
        assert self._config.xml_name is not None
        self._murfi = await murfi_mod.start(
            self._subject_dir,
            xml_name=self._config.xml_name,
            config=self._pipeline,
            scanner_config=self._scanner_config,
        )

    async def _launch_psychopy(self) -> asyncio.subprocess.Process:
        assert self._config.run is not None
        return await psychopy_mod.launch(
            subject=self._subject_dir.name,
            run_number=self._config.run,
            feedback=self._config.feedback,
            duration=self._duration,
            anchor=self._anchor,
        )

    def _collect_artifacts(self) -> dict[str, object]:
        assert self._config.run is not None
        data_dir = self._psychopy_data_dir
        if data_dir is None:
            data_dir = (
                Path(__file__).resolve().parents[3] / "psychopy" / "balltask" / "data"
            )
        try:
            scale_factor = psychopy_mod.get_scale_factor(
                data_dir,
                self._subject_dir.name,
                self._config.run,
                default=self._pipeline.default_scale_factor,
                min_hits=self._pipeline.min_hits_per_tr,
                max_hits=self._pipeline.max_hits_per_tr,
                increase=self._pipeline.scale_increase,
                decrease=self._pipeline.scale_decrease,
            )
        except Exception:  # noqa: BLE001 — CSV may not exist in tests
            scale_factor = self._pipeline.default_scale_factor
        return {"scale_factor": scale_factor}

    def _snapshot_murfi(self) -> StepProgress:
        return StepProgress(
            value=self._volumes,
            target=self._target,
            unit=self._config.progress_unit,
            phase="murfi",
        )

    def _phase2_snapshot(self, detail: str) -> StepProgress:
        return StepProgress(
            value=0,
            target=1,
            unit="stages",
            phase="psychopy",
            detail=detail,
        )

    def _current_snapshot(self) -> StepProgress:
        if self._phase == "psychopy":
            return self._phase2_snapshot("cancelled")
        return self._snapshot_murfi()

    async def _shutdown(self, *, timeout: float = 5.0) -> None:
        if self._psychopy is not None and self._psychopy.returncode is None:
            try:
                self._psychopy.terminate()
                try:
                    await asyncio.wait_for(self._psychopy.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    self._psychopy.kill()
                    try:
                        await asyncio.wait_for(self._psychopy.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
            except ProcessLookupError:
                pass
            self._psychopy = None
        if self._murfi is not None:
            try:
                await murfi_mod.stop(self._murfi, timeout=timeout)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            self._murfi = None


def _read_from(path: Path, offset: int) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        return data.decode(errors="replace")
    except FileNotFoundError:
        return ""
