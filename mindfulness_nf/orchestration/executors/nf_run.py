"""NF_RUN executor: phase 1 MURFI volume acquisition, phase 2 PsychoPy feedback."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import StepConfig
from mindfulness_nf.orchestration import motion as motion_mod
from mindfulness_nf.orchestration import murfi as murfi_mod
from mindfulness_nf.orchestration import psychopy as psychopy_mod
from mindfulness_nf.orchestration.executor import (
    Component,
    ProgressCallback,
    StepOutcome,
    StepProgress,
)
from mindfulness_nf.orchestration.layout import SubjectLayout
from mindfulness_nf.orchestration.scanner_source import ScannerSource
from mindfulness_nf.orchestration.subjects import (
    rename_step_volumes,
    snapshot_img_dir,
)

__all__ = ["NfRunStepExecutor"]

_VOLUME_LINE_RE = re.compile(r"received image from scanner")
# MURFI prints this once it has bound its scanner input port and is ready
# to accept volume pushes. Phase 1 uses it as the "ready" signal so the
# phase gate can appear before any scanner volumes arrive — PsychoPy then
# launches *concurrently* with scanner acquisition, not after it.
_MURFI_READY_RE = re.compile(r"listening for images on port")
_MURFI_READY_TIMEOUT_SECONDS = 15.0


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
        # The runner passes the BIDS session dir (``sub-X/ses-Y``). MURFI
        # and PsychoPy both want the *subject* identifier ``sub-X``, not
        # the session name ``ses-Y``. SubjectLayout handles both cleanly
        # — no more ``.parent`` ladders.
        self._subject_dir = subject_dir
        self._layout = SubjectLayout.from_session_dir(subject_dir)
        self._subject_root = self._layout.subject_root
        self._subject_name = self._layout.subject_id
        self._pipeline = pipeline
        self._scanner_config = scanner_config
        self._scanner_source = scanner_source
        # Explicit override honoured (used in tests). In the orchestrated
        # flow we prefer the session-scoped psychopy_data_dir from layout
        # so behavioral data lands inside the subject tree.
        self._psychopy_data_dir = (
            psychopy_data_dir
            if psychopy_data_dir is not None
            else self._layout.psychopy_data_dir
        )
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
        # Snapshot img/ so new files from this MURFI run can be renamed
        # to img-<step.run>-* post-success (consecutive steps otherwise
        # collide on MURFI's per-process series counter).
        self._img_snapshot = snapshot_img_dir(self._subject_dir.parent)
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

            # Phase 1 succeeded (MURFI ready to receive): emit gate snapshot.
            # Operator presses D when subject is briefed and scanner is
            # about to start — PsychoPy launches and the scan begins
            # concurrently, so the subject sees stimuli while MURFI
            # receives real-time volumes and streams activation to PsychoPy.
            on_progress(
                StepProgress(
                    value=self._volumes,
                    target=self._target,
                    unit=self._config.progress_unit,
                    phase="murfi",
                    detail="MURFI ready — press D to launch PsychoPy + start scan",
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
            outcome = await self._run_phase2_psychopy(on_progress)
        except asyncio.CancelledError:
            await self._shutdown()
            return StepOutcome(
                succeeded=False,
                final_progress=self._current_snapshot(),
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
            if renamed == 0 or renamed < self._target:
                # Bug C guard (see vsend.py for context): log loudly when
                # MURFI produced zero or too-few saved volumes so the
                # operator can catch it before the next subject.
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
            # Best-effort post-step motion extraction. Skipped silently if
            # FSL isn't on PATH or no volumes were saved. MURFI's GLM uses
            # motion derivatives as regressors but does not write motion
            # estimates to disk; this gives the analyst per-TR motion +
            # framewise displacement.
            if renamed > 0:
                try:
                    await motion_mod.extract_motion_params(
                        img_dir=self._layout.img_dir,
                        output_dir=self._subject_dir / "derivatives" / "motion",
                        task=self._config.task,
                        run=self._config.run,
                    )
                except Exception:  # noqa: BLE001 — diagnostic, never fail the step
                    import logging as _logging
                    _logging.getLogger(__name__).exception(
                        "motion extraction raised for step %s", self._config.name
                    )
        return outcome

    async def stop(self, timeout: float = 5.0) -> None:
        self._stopped = True
        # Unblock any phase-gate await so run() can observe the stop.
        self._advance_event.set()
        await self._shutdown(timeout=timeout)

    async def relaunch(self, component: Component) -> None:
        if component == "murfi":
            async with self._relaunch_lock:
                if self._murfi is not None:
                    await murfi_mod.stop(self._murfi)
                    self._murfi = None
                await self._start_murfi()
                # ``murfi.start`` truncates the log to 0 bytes. Reset the
                # monitor's baseline so it reads from the top of the fresh
                # log; saving the old size would cause the monitor to sit
                # silent until the new log grew past the old offset (bug).
                self._log_baseline = 0
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
        """Wait until MURFI has bound its scanner input port.

        Real-time neurofeedback requires PsychoPy to run *concurrently* with
        scanner acquisition (MURFI receives each volume, infoserver streams
        activation, PsychoPy reads it and renders the feedback meter). So
        phase 1 is brief: launch MURFI, wait for it to log "listening for
        images on port", then hand off to the phase gate. Volumes are
        monitored in phase 2 alongside PsychoPy, not before it.

        Returns ``None`` on success (MURFI ready); a failure outcome
        otherwise. Previously this waited for target volumes before the
        gate, which broke the real-time protocol — PsychoPy would launch
        *after* the scan rather than concurrently with it.
        """
        deadline = asyncio.get_event_loop().time() + _MURFI_READY_TIMEOUT_SECONDS
        on_progress(self._snapshot_murfi(detail="waiting for MURFI ready"))
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

            new_text = await asyncio.to_thread(
                _read_from, murfi.log_path, self._log_baseline
            )
            if new_text:
                self._log_baseline += len(new_text.encode())
                if _MURFI_READY_RE.search(new_text):
                    on_progress(self._snapshot_murfi(detail="MURFI ready"))
                    return None

            if murfi.process.returncode is not None:
                rc = murfi.process.returncode
                await self._shutdown()
                return StepOutcome(
                    succeeded=False,
                    final_progress=self._snapshot_murfi(),
                    error=f"MURFI exited during startup (rc={rc})",
                )

            if asyncio.get_event_loop().time() > deadline:
                await self._shutdown()
                return StepOutcome(
                    succeeded=False,
                    final_progress=self._snapshot_murfi(),
                    error=(
                        f"MURFI readiness timeout: no 'listening for images' "
                        f"log line within {_MURFI_READY_TIMEOUT_SECONDS}s"
                    ),
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

            # Poll MURFI's log for "received image from scanner" lines so
            # volume progress ticks alongside PsychoPy. MURFI + PsychoPy run
            # concurrently: scanner pushes → MURFI receives → infoserver
            # streams to PsychoPy → PsychoPy renders feedback.
            new_text = await asyncio.to_thread(
                _read_from, murfi.log_path, self._log_baseline
            )
            if new_text:
                self._log_baseline += len(new_text.encode())
                for line in new_text.splitlines():
                    if _VOLUME_LINE_RE.search(line):
                        self._volumes += 1
                        on_progress(self._snapshot_murfi(
                            phase="psychopy", detail="PsychoPy running"
                        ))

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
        # Unique log name per step so Transfer Pre / Feedback 1-5 /
        # Transfer Post each get their own MURFI log instead of all
        # writing to (and truncating) murfi_rtdmn.log.
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

    async def _launch_psychopy(self) -> asyncio.subprocess.Process:
        assert self._config.run is not None
        return await psychopy_mod.launch(
            subject=self._subject_name,  # "sub-X", NOT "ses-Y"
            run_number=self._config.run,
            feedback=self._config.feedback,
            duration=self._duration,
            anchor=self._anchor,
            data_dir=self._psychopy_data_dir,
            # Thread the real session + task to override the legacy
            # ses-nf hardcode and run-number-based task inference in
            # bids_tsv_convert_balltask.py.
            session_type=self._layout.session_type,
            task=self._config.task,
        )

    def _collect_artifacts(self) -> dict[str, object]:
        assert self._config.run is not None
        data_dir = self._psychopy_data_dir
        try:
            scale_factor = psychopy_mod.get_scale_factor(
                data_dir,
                self._subject_name,  # "sub-X", NOT "ses-Y"
                self._config.run,
                default=self._pipeline.default_scale_factor,
                min_hits=self._pipeline.min_hits_per_tr,
                max_hits=self._pipeline.max_hits_per_tr,
                increase=self._pipeline.scale_increase,
                decrease=self._pipeline.scale_decrease,
                task=self._config.task,
            )
        except Exception:  # noqa: BLE001 — CSV may not exist in tests
            scale_factor = self._pipeline.default_scale_factor
        return {"scale_factor": scale_factor}

    def _snapshot_murfi(
        self, *, phase: str = "murfi", detail: str | None = None
    ) -> StepProgress:
        return StepProgress(
            value=self._volumes,
            target=self._target,
            unit=self._config.progress_unit,
            phase=phase,  # type: ignore[arg-type]
            detail=detail,
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
