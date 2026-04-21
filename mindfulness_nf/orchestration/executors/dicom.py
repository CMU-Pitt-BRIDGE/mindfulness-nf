"""DICOM_SCAN executor: MURFI + DICOM receiver + dcmsend-driven delivery."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import StepConfig
from mindfulness_nf.orchestration import murfi as murfi_mod
from mindfulness_nf.orchestration.dicom_receiver import DicomReceiver
from mindfulness_nf.orchestration.executor import (
    Component,
    ProgressCallback,
    StepOutcome,
    StepProgress,
)
from mindfulness_nf.orchestration.scanner_source import ScannerSource

__all__ = ["DicomStepExecutor"]

_VOLUME_LINE_RE = re.compile(r"received image from scanner")


class DicomStepExecutor:
    """MURFI + DICOM receiver: volumes arrive via C-STORE, MURFI does the work."""

    def __init__(
        self,
        config: StepConfig,
        subject_dir: Path,
        pipeline: PipelineConfig,
        scanner_config: ScannerConfig,
        scanner_source: ScannerSource,
    ) -> None:
        if config.xml_name is None:
            msg = f"DicomStepExecutor requires StepConfig.xml_name (step={config.name})"
            raise ValueError(msg)
        self._config = config
        self._subject_dir = subject_dir
        self._pipeline = pipeline
        self._scanner_config = scanner_config
        self._scanner_source = scanner_source

        self._murfi: murfi_mod.MurfiProcess | None = None
        self._dicom: DicomReceiver | None = None
        self._log_baseline = 0
        self._volumes = 0
        self._target = config.progress_target

        self._dicom_push_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._relaunch_lock = asyncio.Lock()

    # ---- public protocol -------------------------------------------------

    async def run(self, on_progress: ProgressCallback) -> StepOutcome:
        try:
            await self._start_dicom_receiver()
            await self._start_murfi()
        except Exception as exc:  # noqa: BLE001
            await self._shutdown()
            return StepOutcome(
                succeeded=False,
                final_progress=self._snapshot(),
                error=f"startup failed: {exc}",
            )

        # Kick off dcmsend (real scanner pushes on its own; simulator replays cache).
        sc = self._scanner_config
        self._dicom_push_task = asyncio.create_task(
            self._scanner_source.push_dicom(
                sc.scanner_ip, sc.dicom_port, sc.dicom_ae_title, self._config
            )
        )

        try:
            return await self._monitor(on_progress)
        except asyncio.CancelledError:
            await self._shutdown()
            return StepOutcome(
                succeeded=False,
                final_progress=self._snapshot(),
                error="cancelled",
            )

    async def stop(self, timeout: float = 5.0) -> None:
        self._stopped = True
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
        elif component == "dicom":
            async with self._relaunch_lock:
                if self._dicom is not None:
                    await self._dicom.stop()
                    self._dicom = None
                await self._start_dicom_receiver()
        # Unknown component: no-op.

    def components(self) -> tuple[Component, ...]:
        return ("murfi", "dicom")

    def advance_phase(self) -> None:
        return None

    # ---- internal --------------------------------------------------------

    async def _start_murfi(self) -> None:
        assert self._config.xml_name is not None
        self._murfi = await murfi_mod.start(
            self._subject_dir,
            xml_name=self._config.xml_name,
            config=self._pipeline,
            scanner_config=self._scanner_config,
        )

    async def _start_dicom_receiver(self) -> None:
        dicom_out = self._subject_dir / "sourcedata" / "dicom"
        self._dicom = await DicomReceiver.start(
            output_dir=dicom_out,
            port=self._scanner_config.dicom_port,
            ae_title=self._scanner_config.dicom_ae_title,
        )

    async def _monitor(self, on_progress: ProgressCallback) -> StepOutcome:
        while True:
            if self._stopped:
                await self._shutdown()
                return StepOutcome(
                    succeeded=False,
                    final_progress=self._snapshot(),
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
                        on_progress(self._snapshot())

            if self._volumes >= self._target:
                await self._shutdown()
                return StepOutcome(succeeded=True, final_progress=self._snapshot())

            if murfi.process.returncode is not None:
                rc = murfi.process.returncode
                await self._shutdown()
                if self._volumes >= self._target:
                    return StepOutcome(succeeded=True, final_progress=self._snapshot())
                return StepOutcome(
                    succeeded=False,
                    final_progress=self._snapshot(),
                    error=f"MURFI exited {rc}",
                )

            await asyncio.sleep(0.25)

    def _snapshot(self) -> StepProgress:
        return StepProgress(
            value=self._volumes,
            target=self._target,
            unit=self._config.progress_unit,
        )

    async def _shutdown(self, *, timeout: float = 5.0) -> None:
        if self._dicom_push_task is not None and not self._dicom_push_task.done():
            try:
                await self._scanner_source.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._dicom_push_task.cancel()
            try:
                await self._dicom_push_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._dicom_push_task = None
        if self._dicom is not None:
            try:
                await self._dicom.stop()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            self._dicom = None
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
