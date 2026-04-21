"""ScannerSource protocol and implementations for scanner data delivery.

In real operation the MRI scanner pushes volumes to MURFI on its own via
vSend (for 2vol/rtdmn protocols) or DICOM C-STORE (for rest). In dry-run
rehearsals there is no scanner attached, so we replay cached volumes from
`murfi/dry_run_cache/` at the TR cadence. Tests use a no-op double.

This module is part of the imperative shell: it shells out to external
binaries (`vSend`, `dcmsend`) and manages subprocess lifetimes.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from mindfulness_nf.models import StepConfig
from mindfulness_nf.orchestration.synthetic_volumes import (
    generate_synthetic_dicom_series,
    generate_synthetic_nifti_series,
)

logger = logging.getLogger(__name__)

# Grace period before escalating SIGTERM to SIGKILL on cancel().
_TERMINATE_GRACE_SECONDS = 2.0


class ScannerSource(Protocol):
    """Scanner data source.

    Real scanner pushes volumes on its own — the concrete `RealScannerSource`
    is a no-op and the executor simply waits for volumes to arrive at MURFI.
    In dry-run, `SimulatedScannerSource` replays cached volumes via external
    helpers (vSend for NIfTI, dcmsend for DICOM).
    """

    async def push_vsend(
        self, xml_path: Path, subject_dir: Path, step: StepConfig
    ) -> None:
        """Deliver NIfTI volumes to MURFI via vSend at TR cadence."""
        ...

    async def push_dicom(
        self, target_host: str, target_port: int, ae_title: str, step: StepConfig
    ) -> None:
        """Deliver DICOM files to the receiver via dcmsend at TR cadence."""
        ...

    async def cancel(self) -> None:
        """Terminate any in-flight push. Idempotent; bounded (~2s)."""
        ...


class RealScannerSource:
    """No-op source. The real scanner pushes on its own; the executor just waits."""

    async def push_vsend(
        self, xml_path: Path, subject_dir: Path, step: StepConfig
    ) -> None:
        return None

    async def push_dicom(
        self, target_host: str, target_port: int, ae_title: str, step: StepConfig
    ) -> None:
        return None

    async def cancel(self) -> None:
        return None


class NoOpScannerSource:
    """Test double: records every call; performs no I/O.

    Attributes are intentionally public for direct assertion in tests.
    """

    def __init__(self) -> None:
        self.push_vsend_calls: list[tuple[Path, Path, StepConfig]] = []
        self.push_dicom_calls: list[tuple[str, int, str, StepConfig]] = []
        self.cancel_calls: int = 0

    async def push_vsend(
        self, xml_path: Path, subject_dir: Path, step: StepConfig
    ) -> None:
        self.push_vsend_calls.append((xml_path, subject_dir, step))

    async def push_dicom(
        self, target_host: str, target_port: int, ae_title: str, step: StepConfig
    ) -> None:
        self.push_dicom_calls.append((target_host, target_port, ae_title, step))

    async def cancel(self) -> None:
        self.cancel_calls += 1


class SimulatedScannerSource:
    """Replays cached volumes to MURFI via vSend / dcmsend at TR cadence.

    `cache_dir` should be `murfi/dry_run_cache/` — a sibling of
    `murfi/subjects/` primed by `scripts/populate_dry_run_cache.py`. Expected
    layout (defensive — missing files yield a warning, not an error):

        cache_dir/
            nifti/    # *.nii / *.nii.gz replayed via vSend
            dicom/    # *.dcm       replayed via dcmsend

    Discovers binaries via `shutil.which`. In CI / minimal environments the
    binaries may be absent; push methods log a warning and return rather than
    raising — dry-run rehearsals are expected to run on configured hosts.

    Cache lookup precedence (for push_vsend):
        1. Explicit ``cache_dir`` (constructor arg / tmpdir) if it contains
           NIfTIs — highest priority, lets tests and ad-hoc rehearsals pin a
           specific dataset.
        2. ``BOLD_CACHE_DIR`` (``murfi/dry_run_cache_bold/``) if populated —
           the real-BOLD cache produced by ``scripts/fetch_dry_run_bold.py``.
           Enables FSL ICA to run on real public data during rehearsal.
        3. Synthesize random-noise volumes — last resort; enables MURFI/
           PsychoPy rehearsal out of the box but not meaningful FSL ICA.
    """

    BOLD_CACHE_DIR: Path = Path("murfi/dry_run_cache_bold")

    def __init__(
        self, cache_dir: Path | None = None, tr_seconds: float = 1.2
    ) -> None:
        if cache_dir is None:
            # Ephemeral scratch dir; lives for the life of this source so
            # synthetic volumes persist across consecutive steps (letting us
            # generate once per rehearsal rather than once per step).
            cache_dir = Path(tempfile.mkdtemp(prefix="murfi_dryrun_"))
        self.cache_dir = cache_dir
        self.tr_seconds = tr_seconds
        self._procs: list[asyncio.subprocess.Process] = []
        self._lock = asyncio.Lock()

    # -- push_vsend ------------------------------------------------------

    async def push_vsend(
        self, xml_path: Path, subject_dir: Path, step: StepConfig
    ) -> None:
        """Stream cached NIfTI volumes to MURFI via the `vSend` binary."""
        binary = shutil.which("vSend")
        if binary is None:
            logger.warning("vSend binary not on PATH; SimulatedScannerSource.push_vsend is a no-op")
            return

        # 3-tier cache lookup: explicit > bold (real public BOLD) > synthetic.
        nifti_dir = self.cache_dir / "nifti"
        volumes = sorted(nifti_dir.glob("*.nii*")) if nifti_dir.is_dir() else []
        if volumes:
            logger.info(
                "SimulatedScannerSource: using explicit cache %s (%d volumes)",
                nifti_dir,
                len(volumes),
            )
        else:
            bold_nifti_dir = self.BOLD_CACHE_DIR / "nifti"
            bold_volumes = (
                sorted(bold_nifti_dir.glob("*.nii*"))
                if bold_nifti_dir.is_dir()
                else []
            )
            if bold_volumes:
                logger.info(
                    "SimulatedScannerSource: using real-BOLD cache %s (%d volumes)",
                    bold_nifti_dir,
                    len(bold_volumes),
                )
                volumes = bold_volumes
            else:
                # Fallback: no pre-recorded cache — fabricate just enough
                # volumes for this step. Keeps the cached-session rehearsal
                # path intact while making --dry-run work out of the box.
                logger.info(
                    "SimulatedScannerSource: no cached NIfTI (checked %s, %s); "
                    "synthesizing %d volume(s) for step %s",
                    nifti_dir,
                    bold_nifti_dir,
                    step.progress_target,
                    step.name,
                )
                volumes = generate_synthetic_nifti_series(
                    nifti_dir, count=step.progress_target, tr=self.tr_seconds
                )

        # vSend typically takes a config (xml_path) and a list of volumes; exact
        # flags are site-specific and wrapped here defensively. The pacing
        # argument is per-TR so vSend itself handles cadence.
        cmd = [
            binary,
            "--config", str(xml_path),
            "--tr", str(self.tr_seconds),
            *[str(v) for v in volumes],
        ]
        logger.info("SimulatedScannerSource: launching vSend (%d volumes) for %s", len(volumes), step.name)
        await self._spawn_and_wait(cmd)

    # -- push_dicom ------------------------------------------------------

    async def push_dicom(
        self, target_host: str, target_port: int, ae_title: str, step: StepConfig
    ) -> None:
        """Stream cached DICOMs to the receiver via `dcmsend`."""
        binary = shutil.which("dcmsend")
        if binary is None:
            logger.warning("dcmsend binary not on PATH; SimulatedScannerSource.push_dicom is a no-op")
            return

        dicom_dir = self.cache_dir / "dicom"
        dicoms = sorted(dicom_dir.glob("*.dcm")) if dicom_dir.is_dir() else []
        if not dicoms:
            logger.info(
                "SimulatedScannerSource: no cached DICOMs under %s; "
                "synthesizing %d file(s) for step %s",
                dicom_dir,
                step.progress_target,
                step.name,
            )
            dicoms = generate_synthetic_dicom_series(
                dicom_dir, count=step.progress_target
            )

        cmd = [
            binary,
            target_host,
            str(target_port),
            "-aec", ae_title,
            "--scan-directories",
            str(dicom_dir),
        ]
        logger.info("SimulatedScannerSource: launching dcmsend (%d files) for %s", len(dicoms), step.name)
        await self._spawn_and_wait(cmd)

    # -- cancel ----------------------------------------------------------

    async def cancel(self) -> None:
        """SIGTERM every tracked subprocess; escalate to SIGKILL after 2s."""
        async with self._lock:
            procs = list(self._procs)
            self._procs.clear()

        for proc in procs:
            if proc.returncode is not None:
                continue
            try:
                proc.terminate()
            except ProcessLookupError:
                continue

        for proc in procs:
            if proc.returncode is not None:
                continue
            try:
                await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                # Reap the zombie; don't let CancelledError swallow this.
                try:
                    await proc.wait()
                except asyncio.CancelledError:
                    raise

    # -- internal --------------------------------------------------------

    async def _spawn_and_wait(self, cmd: list[str]) -> None:
        """Launch a subprocess, track it for cancel(), await its exit."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async with self._lock:
            self._procs.append(proc)
        try:
            rc = await proc.wait()
            if rc != 0:
                logger.warning("%s exited with code %d", cmd[0], rc)
        except asyncio.CancelledError:
            # Propagate cancellation after attempting to terminate.
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            raise
        finally:
            async with self._lock:
                if proc in self._procs:
                    self._procs.remove(proc)
