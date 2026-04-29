"""VSEND_SCAN executor: MURFI + vSend-driven volume delivery."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import StepConfig
from mindfulness_nf.orchestration import murfi as murfi_mod
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

__all__ = ["VsendStepExecutor"]

# Matches one "received image from scanner" log line -> one acquired volume.
_VOLUME_LINE_RE = re.compile(r"received image from scanner")


class VsendStepExecutor:
    """Drive a vSend scan: launch MURFI, push volumes, monitor log."""

    def __init__(
        self,
        config: StepConfig,
        subject_dir: Path,
        pipeline: PipelineConfig,
        scanner_config: ScannerConfig,
        scanner_source: ScannerSource,
    ) -> None:
        if config.xml_name is None:
            msg = f"VsendStepExecutor requires StepConfig.xml_name (step={config.name})"
            raise ValueError(msg)
        self._config = config
        self._subject_dir = subject_dir
        self._pipeline = pipeline
        self._scanner_config = scanner_config
        self._scanner_source = scanner_source

        self._murfi: murfi_mod.MurfiProcess | None = None
        self._log_baseline = 0  # bytes to skip when reading log (MURFI relaunches)
        self._volumes = 0
        self._target = config.progress_target

        self._vsend_task: asyncio.Task[None] | None = None
        self._run_task: asyncio.Task[StepOutcome] | None = None
        self._stopped = False
        self._relaunch_lock = asyncio.Lock()

    # ---- public protocol -------------------------------------------------

    async def run(self, on_progress: ProgressCallback) -> StepOutcome:
        self._run_task = asyncio.current_task()
        # Snapshot img/ so we can rename only files produced by *this*
        # MURFI invocation after it exits. MURFI reuses its per-process
        # series counter across step runs, so without rename consecutive
        # steps (e.g. Rest 1 → Rest 2) would overwrite each other.
        self._img_snapshot = snapshot_img_dir(self._subject_dir.parent)
        try:
            await self._start_murfi()
        except Exception as exc:  # noqa: BLE001 — operational error to outcome
            return StepOutcome(
                succeeded=False,
                final_progress=self._snapshot(),
                error=f"MURFI start failed: {exc}",
            )

        # Kick off the vSend push as a background task; Real source is a no-op.
        xml_path = self._subject_dir / "xml" / self._config.xml_name  # type: ignore[operator]
        self._vsend_task = asyncio.create_task(
            self._scanner_source.push_vsend(xml_path, self._subject_dir, self._config)
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
            renamed = rename_step_volumes(
                self._subject_dir.parent,
                self._config.task,
                self._config.run,
                self._img_snapshot,
            )
            # Best-effort motion extraction (see nf_run for rationale).
            if renamed > 0:
                try:
                    from mindfulness_nf.orchestration import motion as motion_mod
                    from mindfulness_nf.orchestration.layout import SubjectLayout

                    _layout = SubjectLayout.from_session_dir(self._subject_dir)
                    await motion_mod.extract_motion_params(
                        img_dir=_layout.img_dir,
                        output_dir=self._subject_dir / "derivatives" / "motion",
                        task=self._config.task,
                        run=self._config.run,
                    )
                except Exception:  # noqa: BLE001 — diagnostic only
                    import logging as _logging
                    _logging.getLogger(__name__).exception(
                        "motion extraction raised for step %s", self._config.name
                    )
            if renamed == 0 or renamed < self._target:
                # Bug C guard: MURFI said the scan completed but saveImages
                # produced no (or too few) on-disk volumes. This happened
                # with sub-morgan's rt15 runs (2026-04-21) — lost all raw
                # NIfTIs despite saveImages=true in XML, because rtdmn.xml
                # was missing the separate <option name="save"> flag.
                # Don't fail the step (volumes may have been processed in
                # memory and the NF run still completed), but log prominently.
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "MURFI saved %d raw img files for step %s "
                    "(task=%s run=%s, target=%d) — raw volumes may be "
                    "incomplete. Check MURFI log + XML `save`/`saveImages`.",
                    renamed,
                    self._config.name,
                    self._config.task,
                    self._config.run,
                    self._target,
                )
        return outcome

    async def stop(self, timeout: float = 5.0) -> None:
        self._stopped = True
        await self._shutdown(timeout=timeout)

    async def relaunch(self, component: Component) -> None:
        if component != "murfi":
            return
        async with self._relaunch_lock:
            if self._murfi is not None:
                await murfi_mod.stop(self._murfi)
                self._murfi = None
            await self._start_murfi()
            # ``murfi.start`` truncates the log; reset baseline so the
            # monitor reads the fresh log from byte 0. Saving the old
            # file size would starve the monitor until the new log grew
            # past it.
            self._log_baseline = 0

    def components(self) -> tuple[Component, ...]:
        return ("murfi",)

    def advance_phase(self) -> None:
        return None

    # ---- internal --------------------------------------------------------

    async def _start_murfi(self) -> None:
        assert self._config.xml_name is not None  # validated in __init__
        # Unique log per (task, run) so repeated vsend steps sharing the
        # same XML don't clobber each other's logs.
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

    async def _monitor(self, on_progress: ProgressCallback) -> StepOutcome:
        """Read MURFI log from the current baseline; resolve on target hit or crash."""
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

            # Read any new bytes from the log past our current baseline.
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
                # If target was reached exactly as MURFI exited, accept success.
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
        # Cancel any in-flight vSend push first so MURFI doesn't see stragglers.
        if self._vsend_task is not None and not self._vsend_task.done():
            try:
                await self._scanner_source.cancel()
            except Exception:  # noqa: BLE001 — best effort
                pass
            self._vsend_task.cancel()
            try:
                await self._vsend_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._vsend_task = None
        if self._murfi is not None:
            try:
                await murfi_mod.stop(self._murfi, timeout=timeout)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — best effort cleanup
                pass
            self._murfi = None


def _read_from(path: Path, offset: int) -> str:
    """Read *path* starting at byte *offset*. Shared tail-reader semantics."""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        return data.decode(errors="replace")
    except FileNotFoundError:
        return ""
