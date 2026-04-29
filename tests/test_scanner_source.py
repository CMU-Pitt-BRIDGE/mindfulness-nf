"""Verification suite for ScannerSource implementations.

Only external I/O is mocked (`asyncio.create_subprocess_exec`, `shutil.which`).
The Protocol + no-op implementations need no mocks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindfulness_nf.models import StepConfig, StepKind
from mindfulness_nf.orchestration.scanner_source import (
    NoOpScannerSource,
    RealScannerSource,
    ScannerSource,
    SimulatedScannerSource,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step(kind: StepKind = StepKind.VSEND_SCAN) -> StepConfig:
    return StepConfig(
        name="twovol",
        task="2vol",
        run=1,
        progress_target=2,
        progress_unit="volumes",
        xml_name="2vol.xml",
        kind=kind,
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """All three concretes conform to the ScannerSource Protocol."""

    def test_real_is_scanner_source(self) -> None:
        source: ScannerSource = RealScannerSource()
        assert source is not None

    def test_noop_is_scanner_source(self) -> None:
        source: ScannerSource = NoOpScannerSource()
        assert source is not None

    def test_simulated_is_scanner_source(self, tmp_path: Path) -> None:
        source: ScannerSource = SimulatedScannerSource(cache_dir=tmp_path)
        assert source is not None


# ---------------------------------------------------------------------------
# RealScannerSource
# ---------------------------------------------------------------------------


class TestRealScannerSource:
    """Real scanner pushes on its own; implementation is a total no-op."""

    @pytest.mark.asyncio
    async def test_real_scanner_source_is_noop(self, tmp_path: Path) -> None:
        source = RealScannerSource()
        step = _step()
        assert await source.push_vsend(tmp_path / "x.xml", tmp_path, step) is None
        assert await source.push_dicom("host", 4006, "MURFI", step) is None
        assert await source.cancel() is None


# ---------------------------------------------------------------------------
# NoOpScannerSource
# ---------------------------------------------------------------------------


class TestNoOpScannerSource:
    """Test double: every call is recorded; no side effects."""

    @pytest.mark.asyncio
    async def test_noop_scanner_source_tracks_calls(self, tmp_path: Path) -> None:
        source = NoOpScannerSource()
        step = _step()
        xml = tmp_path / "2vol.xml"
        await source.push_vsend(xml, tmp_path, step)
        await source.push_dicom("10.0.0.1", 4006, "MURFI", step)
        await source.cancel()

        assert source.push_vsend_calls == [(xml, tmp_path, step)]
        assert source.push_dicom_calls == [("10.0.0.1", 4006, "MURFI", step)]
        assert source.cancel_calls == 1


# ---------------------------------------------------------------------------
# SimulatedScannerSource
# ---------------------------------------------------------------------------


class TestSimulatedScannerSourceVsend:
    """push_vsend streams cached NIfTIs to MURFI's scanner port via the
    in-process Python sender. No external binary required.
    """

    @pytest.mark.asyncio
    async def test_push_vsend_invokes_python_sender_with_cached_volumes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nifti_dir = tmp_path / "nifti"
        nifti_dir.mkdir()
        vol_a = nifti_dir / "001.nii.gz"
        vol_b = nifti_dir / "002.nii.gz"
        vol_a.touch()
        vol_b.touch()

        captured: dict = {}

        def _fake_sender(host, port, nifti_paths, tr_seconds):
            captured["host"] = host
            captured["port"] = port
            captured["paths"] = tuple(nifti_paths)
            captured["tr"] = tr_seconds

        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source._send_nifti_via_vsend",
            _fake_sender,
        )

        source = SimulatedScannerSource(cache_dir=tmp_path, tr_seconds=1.2)
        xml = tmp_path / "2vol.xml"
        await source.push_vsend(xml, tmp_path, _step())

        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 50000
        assert captured["tr"] == 1.2
        assert vol_a in captured["paths"]
        assert vol_b in captured["paths"]


class TestSimulatedScannerSourceSynthesis:
    """Empty cache triggers synthetic volume generation; populated cache is preserved."""

    @pytest.mark.asyncio
    async def test_auto_synthesizes_when_cache_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent_paths: list[Path] = []

        def _capture_sender(host, port, nifti_paths, tr_seconds):
            sent_paths.extend(nifti_paths)

        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source._send_nifti_via_vsend",
            _capture_sender,
        )
        # Isolate from any populated real-BOLD cache on the developer's machine
        # (murfi/dry_run_cache_bold/). Point it at a non-existent dir so the
        # 3-tier lookup skips it and falls through to synthesis.
        monkeypatch.setattr(
            SimulatedScannerSource, "BOLD_CACHE_DIR", tmp_path / "no-bold-cache",
        )

        source = SimulatedScannerSource(cache_dir=tmp_path)
        await source.push_vsend(tmp_path / "x.xml", tmp_path, _step())

        nifti_dir = tmp_path / "nifti"
        generated = sorted(nifti_dir.glob("*.nii*"))
        assert len(generated) == 2
        for g in generated:
            assert g in sent_paths

    @pytest.mark.asyncio
    async def test_preserves_existing_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nifti_dir = tmp_path / "nifti"
        nifti_dir.mkdir()
        existing = nifti_dir / "prerecorded_0001.nii.gz"
        existing.write_bytes(b"REAL_DATA_MARKER")

        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source._send_nifti_via_vsend",
            lambda *_a, **_kw: None,
        )

        source = SimulatedScannerSource(cache_dir=tmp_path)
        await source.push_vsend(tmp_path / "x.xml", tmp_path, _step())

        assert existing.read_bytes() == b"REAL_DATA_MARKER"
        contents = sorted(p.name for p in nifti_dir.iterdir())
        assert contents == ["prerecorded_0001.nii.gz"]

    def test_defaults_to_tmpdir_when_no_cache_dir(self) -> None:
        source = SimulatedScannerSource()
        assert source.cache_dir.is_dir()
        # Name reflects our prefix for discoverability.
        assert "murfi_dryrun_" in source.cache_dir.name

    @pytest.mark.asyncio
    async def test_simulated_scanner_source_prefers_bold_cache_over_synthesis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty explicit cache + populated BOLD cache -> use BOLD cache.

        BOLD cache must take priority over synthesis (tier 2 > tier 3).
        """
        bold_cache = tmp_path / "dry_run_cache_bold"
        bold_nifti_dir = bold_cache / "nifti"
        bold_nifti_dir.mkdir(parents=True)
        bold_vol = bold_nifti_dir / "vol_0001.nii"
        bold_vol.write_bytes(b"REAL_BOLD_DATA")

        monkeypatch.setattr(SimulatedScannerSource, "BOLD_CACHE_DIR", bold_cache)
        explicit_cache = tmp_path / "explicit"
        explicit_cache.mkdir()

        sent_paths: list[Path] = []

        def _capture_sender(host, port, nifti_paths, tr_seconds):
            sent_paths.extend(nifti_paths)

        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source._send_nifti_via_vsend",
            _capture_sender,
        )

        source = SimulatedScannerSource(cache_dir=explicit_cache)
        await source.push_vsend(tmp_path / "x.xml", tmp_path, _step())

        assert bold_vol in sent_paths
        explicit_nifti = explicit_cache / "nifti"
        assert not explicit_nifti.exists() or not any(explicit_nifti.iterdir())
        assert bold_vol.read_bytes() == b"REAL_BOLD_DATA"


class TestSimulatedScannerSourcePushVsendPython:
    """push_vsend must deliver via the in-process Python sender; no
    ``vSend`` binary required. Mirror fix for the dcmsend/pynetdicom cycle.
    """

    @pytest.mark.asyncio
    async def test_push_vsend_uses_python_sender_not_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # vSend binary not present.
        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.shutil.which",
            lambda _name: None,
        )
        # Subprocess must not be launched.
        called_exec = False

        async def _fail_exec(*_a, **_kw):
            nonlocal called_exec
            called_exec = True
            raise AssertionError("subprocess should not be launched")

        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.asyncio.create_subprocess_exec",
            _fail_exec,
        )

        # Python sender receives the call.
        captured: dict = {}

        def _fake_sender(host, port, nifti_paths, tr_seconds):
            captured["host"] = host
            captured["port"] = port
            captured["paths"] = tuple(nifti_paths)
            captured["tr"] = tr_seconds

        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source._send_nifti_via_vsend",
            _fake_sender,
        )

        # Prime a cache with two NIfTI placeholders.
        cache = tmp_path / "cache"
        (cache / "nifti").mkdir(parents=True)
        (cache / "nifti" / "vol-001.nii").write_bytes(b"\x00")
        (cache / "nifti" / "vol-002.nii").write_bytes(b"\x00")

        source = SimulatedScannerSource(cache_dir=cache, tr_seconds=0.01)
        step = _step()
        xml = tmp_path / "2vol.xml"
        xml.write_text("<xml/>")

        await source.push_vsend(xml_path=xml, subject_dir=tmp_path, step=step)

        assert called_exec is False
        assert captured["host"] == "127.0.0.1"
        assert captured["port"] == 50000
        assert len(captured["paths"]) == 2


class TestSimulatedScannerSourcePushDicom:
    """push_dicom must deliver DICOMs without requiring an external binary.

    The old implementation shelled out to ``dcmsend`` from ``dcmtk``; if
    the binary was absent on PATH, the simulator was a silent no-op and
    MURFI saw no volumes — which is exactly the bug that landed the user
    stuck on "Rest 1 running" with zero progress.
    """

    @pytest.mark.asyncio
    async def test_push_dicom_delivers_to_receiver_without_external_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mindfulness_nf.orchestration.dicom_receiver import DicomReceiver

        # dcmsend MUST NOT be required — the new impl is pure-Python.
        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.shutil.which",
            lambda _name: None,
        )

        # Receiver on an ephemeral port, writing to tmp_path/out/.
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        receiver = await DicomReceiver.start(
            output_dir=out_dir, port=0, ae_title="MURFITEST"
        )
        # pynetdicom exposes the bound port via the server's socket.
        assert receiver._server is not None  # sanity for the test
        port = receiver._server.socket.getsockname()[1]
        assert port > 0

        # Prime the cache with three synthetic DICOMs.
        cache = tmp_path / "cache"
        source = SimulatedScannerSource(cache_dir=cache, tr_seconds=0.01)
        step = StepConfig(
            name="rest",
            task="rest",
            run=1,
            progress_target=3,
            progress_unit="volumes",
            xml_name="rest.xml",
            kind=StepKind.DICOM_SCAN,
        )

        try:
            await source.push_dicom("127.0.0.1", port, "MURFITEST", step)
            # Allow the server a beat to flush writes.
            for _ in range(50):
                if len(list(out_dir.glob("*.dcm"))) >= 3:
                    break
                await asyncio.sleep(0.05)
        finally:
            await receiver.stop()

        delivered = sorted(out_dir.glob("*.dcm"))
        assert len(delivered) == 3, (
            f"expected 3 DICOMs delivered, got {len(delivered)}"
        )


class TestSimulatedScannerSourceCancel:
    """cancel() is still used to terminate any remaining subprocess
    launched by the DICOM-flavor path (historical ``_spawn_and_wait``)
    and is a safe no-op when no subprocess is outstanding.

    The vSend path now uses an in-process Python sender (no subprocess);
    cancel()'s effect there is indirect (the caller cancels the wrapping
    task, which raises CancelledError inside ``asyncio.to_thread``).
    """

    @pytest.mark.asyncio
    async def test_cancel_noop_when_nothing_to_terminate(
        self, tmp_path: Path
    ) -> None:
        source = SimulatedScannerSource(cache_dir=tmp_path)
        # Should complete without raising even with no active subprocess.
        await source.cancel()
