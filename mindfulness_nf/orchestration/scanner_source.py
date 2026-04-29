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
import socket
import tempfile
import time
from pathlib import Path
from typing import Protocol

import nibabel as nb
import pydicom
from pynetdicom import AE, StoragePresentationContexts

from mindfulness_nf.models import StepConfig
from mindfulness_nf.orchestration.external_image import ExternalImage
from mindfulness_nf.orchestration.synthetic_volumes import (
    generate_synthetic_dicom_series,
    generate_synthetic_nifti_series,
)


_VSEND_CONNECT_TIMEOUT_SECONDS = 30.0
_VSEND_CONNECT_RETRY_INTERVAL = 0.25


def _connect_with_retry(host: str, port: int) -> socket.socket:
    """Open a TCP connection, retrying while the listener is not yet up.

    MURFI runs inside an apptainer container and takes ~1-2s to bind its
    scanner input port after its subprocess starts. A naive immediate
    connect from the sender returns ``ECONNREFUSED``. We retry with a
    short interval up to a bounded total, which matches scanner-operator
    timing (they start the acquisition a few seconds after pressing ``d``).
    """
    deadline = time.monotonic() + _VSEND_CONNECT_TIMEOUT_SECONDS
    last_err: OSError | None = None
    while time.monotonic() < deadline:
        try:
            return socket.create_connection((host, port), timeout=2.0)
        except (ConnectionRefusedError, OSError) as exc:
            last_err = exc
            time.sleep(_VSEND_CONNECT_RETRY_INTERVAL)
    raise ConnectionRefusedError(
        f"could not connect to {host}:{port} within "
        f"{_VSEND_CONNECT_TIMEOUT_SECONDS}s (last error: {last_err})"
    )


def _send_nifti_via_vsend(
    host: str,
    port: int,
    nifti_paths: tuple[Path, ...],
    tr_seconds: float,
) -> None:
    """Stream NIfTI volumes to MURFI's scanner input port over TCP.

    Single long-lived connection; one header+data block per volume; TR-paced.
    Matches the wire format in :class:`ExternalImage` so MURFI sees the same
    bytes a real scanner / ``servenii`` would emit. Blocking — call via
    ``asyncio.to_thread``. Retries connection for up to
    :data:`_VSEND_CONNECT_TIMEOUT_SECONDS` so MURFI has time to finish
    starting up before the first volume goes on the wire.
    """
    if not nifti_paths:
        return

    ei = ExternalImage("Sender")
    nt = len(nifti_paths)

    # MURFI's vSend receiver (per ``receive_nii.py``) spawns one handler
    # per TCP connection and reads exactly one ``(header, data)`` block
    # before closing. So the protocol is *one connection per volume*, not
    # a persistent stream. Reusing a single connection returns EPIPE on
    # the second volume.
    for i, path in enumerate(nifti_paths):
        # MURFI expects 1-indexed currentTR (``SERIES_FIRST_ACQ_NUM = 1``);
        # it floors any value less than 1 to 1 (RtInputScannerImages.cpp:443),
        # which collapses 0 and 1 onto the same filename and drops one volume.
        tr_index = i + 1
        img = nb.load(str(path))
        hdr_bytes, data_bytes = ei.from_image(
            img, idx=tr_index, nt=nt, mosaic_flag=True
        )
        with _connect_with_retry(host, port) as sock:
            sock.sendall(hdr_bytes)
            sock.sendall(data_bytes)
        if i < nt - 1 and tr_seconds > 0:
            time.sleep(tr_seconds)


def _send_dicoms_via_pynetdicom(
    target_host: str,
    target_port: int,
    ae_title: str,
    dicom_files: tuple[Path, ...],
    tr_seconds: float,
) -> None:
    """Send a list of DICOMs over C-STORE at roughly TR cadence.

    Blocking; call via ``asyncio.to_thread``. Uses StoragePresentationContexts
    so the association negotiates support for the full set of storage SOP
    classes that our synthetic (MR Image Storage) and any real recorded
    DICOMs are likely to use.
    """
    ae = AE()
    ae.requested_contexts = StoragePresentationContexts
    assoc = ae.associate(target_host, target_port, ae_title=ae_title)
    if not assoc.is_established:
        raise RuntimeError(
            f"C-STORE association to {target_host}:{target_port} AE={ae_title} failed"
        )
    try:
        for idx, path in enumerate(dicom_files):
            ds = pydicom.dcmread(path)
            status = assoc.send_c_store(ds)
            if status is None or getattr(status, "Status", 0xC000) != 0x0000:
                logger.warning(
                    "C-STORE non-success for %s: %r", path.name, status
                )
            # Pace at TR so MURFI's log tail sees volume-by-volume arrival,
            # matching scanner cadence. Skip the sleep after the final send.
            if idx < len(dicom_files) - 1 and tr_seconds > 0:
                time.sleep(tr_seconds)
    finally:
        assoc.release()

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
        """Stream cached NIfTI volumes to MURFI's scanner input port via the
        in-process Python sender.

        Replaces the former ``vSend`` binary dependency. The sender uses
        :func:`_send_nifti_via_vsend` which packs each volume with
        :class:`ExternalImage` and writes header+data blocks over TCP at
        TR cadence — same bytes on the wire that a real scanner or
        ``servenii`` would emit.
        """
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

        # If the cache (explicit or BOLD) has fewer volumes than the step
        # target, cycle through the cache to top up. Without this, a 150-vol
        # BOLD cache for a 250-vol rest run would leave MURFI waiting on 100
        # volumes that never arrive.
        target = step.progress_target
        if target > 0 and len(volumes) < target:
            logger.info(
                "SimulatedScannerSource: cache has %d volumes, step target is "
                "%d — cycling through cache to top up",
                len(volumes),
                target,
            )
            source_volumes = list(volumes)
            volumes = [source_volumes[i % len(source_volumes)] for i in range(target)]

        # MURFI listens on :50000 in this process-local rehearsal. The real
        # scanner would be the sender; we stand in for it here.
        host = "127.0.0.1"
        port = 50000
        logger.info(
            "SimulatedScannerSource: Python vSend streaming %d volumes to %s:%d for %s",
            len(volumes), host, port, step.name,
        )
        await asyncio.to_thread(
            _send_nifti_via_vsend,
            host,
            port,
            tuple(volumes),
            self.tr_seconds,
        )

    # -- push_dicom ------------------------------------------------------

    async def push_dicom(
        self, target_host: str, target_port: int, ae_title: str, step: StepConfig
    ) -> None:
        """Stream cached DICOMs to the receiver via an in-process C-STORE SCU.

        Replaces the old ``dcmsend`` binary dependency. Uses pynetdicom
        (already a project dep for the receiver side) so rehearsals work on
        any host without installing ``dcmtk``.
        """
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

        logger.info(
            "SimulatedScannerSource: C-STORE SCU sending %d files to %s:%d AE=%s for %s",
            len(dicoms), target_host, target_port, ae_title, step.name,
        )
        await asyncio.to_thread(
            _send_dicoms_via_pynetdicom,
            target_host,
            target_port,
            ae_title,
            tuple(dicoms),
            self.tr_seconds,
        )

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
