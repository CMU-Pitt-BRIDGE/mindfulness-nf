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
from mindfulness_nf.orchestration.subjects import (
    rename_step_volumes,
    snapshot_img_dir,
)

__all__ = ["DicomStepExecutor"]

_VOLUME_LINE_RE = re.compile(r"received image from scanner")

# Markers that indicate MURFI (or its apptainer wrapper) hit a fatal condition
# but kept the process alive — e.g. failed XML parse, missing DICOM dir, or
# apptainer bind failures. Detecting these lets the executor fail fast instead
# of waiting forever for ``process.returncode`` to flip.
_FATAL_LOG_MARKERS: tuple[str, ...] = ("ERROR:", "FATAL:")

# Grace period after the scanner-source push task completes. MURFI's DICOM
# watcher sometimes finalizes one short of the requested `measurements`
# count (e.g. 249/250 for a 250-volume scan); once we've pushed everything
# the scan is logically done, so we wait this long for MURFI to catch up
# and accept ``received >= target-1`` as success.
_POST_PUSH_GRACE_SECONDS = 10.0


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
        # Snapshot img/ to key on-disk filenames to step.run post-success.
        self._img_snapshot = snapshot_img_dir(self._subject_dir.parent)
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

        # Kick off the simulated push (RealScannerSource is a no-op — a real
        # scanner pushes on its own). The receiver lives in *this* process,
        # bound to 0.0.0.0:<dicom_port>, so the simulator must target
        # localhost — passing ``scanner_ip`` (the MRI console's IP) would
        # send the C-STORE to the scanner, not to our receiver.
        sc = self._scanner_config
        self._dicom_push_task = asyncio.create_task(
            self._scanner_source.push_dicom(
                "127.0.0.1", sc.dicom_port, sc.dicom_ae_title, self._config
            )
        )

        try:
            outcome = await self._monitor(on_progress)
        except asyncio.CancelledError:
            await self._shutdown()
            return StepOutcome(
                succeeded=False,
                final_progress=self._snapshot(),
                error="cancelled",
            )
        if (
            outcome.succeeded
            and self._config.run is not None
            and self._config.task is not None
        ):
            rename_step_volumes(
                self._subject_dir.parent,
                self._config.task,
                self._config.run,
                self._img_snapshot,
            )
        return outcome

    async def stop(self, timeout: float = 5.0) -> None:
        self._stopped = True
        await self._shutdown(timeout=timeout)

    async def relaunch(self, component: Component) -> None:
        if component == "murfi":
            async with self._relaunch_lock:
                if self._murfi is not None:
                    await murfi_mod.stop(self._murfi)
                    self._murfi = None
                await self._start_murfi()
                # ``murfi.start`` truncates the log; reset baseline so the
                # monitor reads the fresh log from byte 0.
                self._log_baseline = 0
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
        xml_label = self._config.xml_name.removesuffix(".xml")
        if self._config.task is not None and self._config.run is not None:
            log_name = f"{xml_label}_{self._config.task}-{self._config.run:02d}"
        else:
            log_name = xml_label
        self._murfi = await murfi_mod.start(
            self._subject_dir,
            xml_name=self._config.xml_name,
            config=self._pipeline,
            scanner_config=self._scanner_config,
            log_name=log_name,
        )

    async def _start_dicom_receiver(self) -> None:
        dicom_out = self._subject_dir / "sourcedata" / "dicom"
        self._dicom = await DicomReceiver.start(
            output_dir=dicom_out,
            port=self._scanner_config.dicom_port,
            ae_title=self._scanner_config.dicom_ae_title,
        )

    async def _monitor(self, on_progress: ProgressCallback) -> StepOutcome:
        push_done_at: float | None = None
        loop = asyncio.get_event_loop()
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
                fatal_line: str | None = None
                for line in new_text.splitlines():
                    if _VOLUME_LINE_RE.search(line):
                        self._volumes += 1
                        on_progress(self._snapshot())
                    elif fatal_line is None and any(
                        line.lstrip().startswith(m) for m in _FATAL_LOG_MARKERS
                    ):
                        fatal_line = line.strip()
                if fatal_line is not None:
                    await self._shutdown()
                    return StepOutcome(
                        succeeded=False,
                        final_progress=self._snapshot(),
                        error=f"MURFI fatal: {fatal_line}",
                    )

            if self._volumes >= self._target:
                await self._shutdown()
                return StepOutcome(succeeded=True, final_progress=self._snapshot())

            # Post-push grace: once the scanner-source has delivered every
            # DICOM and MURFI is within 1 of target, consider the scan done
            # after a short quiescence window. Closes the MURFI-side
            # off-by-one that used to hang at N-1/N.
            push_task = self._dicom_push_task
            if push_task is not None and push_task.done():
                if push_done_at is None:
                    push_done_at = loop.time()
                elif (
                    self._volumes >= self._target - 1
                    and loop.time() - push_done_at >= _POST_PUSH_GRACE_SECONDS
                ):
                    await self._shutdown()
                    return StepOutcome(
                        succeeded=True, final_progress=self._snapshot()
                    )

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
