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
    """push_vsend shells out to the vSend binary with cached volumes."""

    @pytest.mark.asyncio
    async def test_launches_vsend_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Prime cache with two fake NIfTI files.
        nifti_dir = tmp_path / "nifti"
        nifti_dir.mkdir()
        vol_a = nifti_dir / "001.nii.gz"
        vol_b = nifti_dir / "002.nii.gz"
        vol_a.touch()
        vol_b.touch()

        # Pretend vSend is on PATH.
        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.shutil.which",
            lambda _name: "/usr/local/bin/vSend",
        )

        # Mock the subprocess: exits cleanly.
        proc = MagicMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()

        create = AsyncMock(return_value=proc)
        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.asyncio.create_subprocess_exec",
            create,
        )

        source = SimulatedScannerSource(cache_dir=tmp_path, tr_seconds=1.2)
        xml = tmp_path / "2vol.xml"
        await source.push_vsend(xml, tmp_path, _step())

        assert create.await_count == 1
        called_args = create.await_args.args
        assert called_args[0] == "/usr/local/bin/vSend"
        # The two cache volumes appear in the command.
        assert str(vol_a) in called_args
        assert str(vol_b) in called_args
        # The xml path is forwarded.
        assert str(xml) in called_args

    @pytest.mark.asyncio
    async def test_push_vsend_noop_when_binary_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.shutil.which",
            lambda _name: None,
        )
        create = AsyncMock()
        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.asyncio.create_subprocess_exec",
            create,
        )

        source = SimulatedScannerSource(cache_dir=tmp_path)
        await source.push_vsend(tmp_path / "x.xml", tmp_path, _step())

        create.assert_not_called()


class TestSimulatedScannerSourceSynthesis:
    """Empty cache triggers synthetic volume generation; populated cache is preserved."""

    @pytest.mark.asyncio
    async def test_auto_synthesizes_when_cache_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.shutil.which",
            lambda _name: "/usr/local/bin/vSend",
        )
        proc = MagicMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()
        create = AsyncMock(return_value=proc)
        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.asyncio.create_subprocess_exec",
            create,
        )

        source = SimulatedScannerSource(cache_dir=tmp_path)
        # _step() above sets progress_target=2 so we expect 2 synthesized files.
        await source.push_vsend(tmp_path / "x.xml", tmp_path, _step())

        nifti_dir = tmp_path / "nifti"
        generated = sorted(nifti_dir.glob("*.nii*"))
        assert len(generated) == 2
        # The generated files made it into the vSend command.
        called_args = create.await_args.args
        for g in generated:
            assert str(g) in called_args

    @pytest.mark.asyncio
    async def test_preserves_existing_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pre-populate cache with a single pretend-real volume.
        nifti_dir = tmp_path / "nifti"
        nifti_dir.mkdir()
        existing = nifti_dir / "prerecorded_0001.nii.gz"
        existing.write_bytes(b"REAL_DATA_MARKER")

        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.shutil.which",
            lambda _name: "/usr/local/bin/vSend",
        )
        proc = MagicMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()
        create = AsyncMock(return_value=proc)
        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.asyncio.create_subprocess_exec",
            create,
        )

        source = SimulatedScannerSource(cache_dir=tmp_path)
        await source.push_vsend(tmp_path / "x.xml", tmp_path, _step())

        # Existing file is still there, unchanged.
        assert existing.read_bytes() == b"REAL_DATA_MARKER"
        # No synthetic file was generated.
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
        # Build the real-BOLD cache location inside tmp_path and monkeypatch
        # the class attribute to point at it.
        bold_cache = tmp_path / "dry_run_cache_bold"
        bold_nifti_dir = bold_cache / "nifti"
        bold_nifti_dir.mkdir(parents=True)
        bold_vol = bold_nifti_dir / "vol_0001.nii"
        bold_vol.write_bytes(b"REAL_BOLD_DATA")

        monkeypatch.setattr(SimulatedScannerSource, "BOLD_CACHE_DIR", bold_cache)

        # The explicit cache_dir is intentionally empty -> fallback kicks in.
        explicit_cache = tmp_path / "explicit"
        explicit_cache.mkdir()

        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.shutil.which",
            lambda _name: "/usr/local/bin/vSend",
        )
        proc = MagicMock()
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = MagicMock()
        create = AsyncMock(return_value=proc)
        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.asyncio.create_subprocess_exec",
            create,
        )

        source = SimulatedScannerSource(cache_dir=explicit_cache)
        await source.push_vsend(tmp_path / "x.xml", tmp_path, _step())

        # The BOLD volume is in the vSend command.
        called_args = create.await_args.args
        assert str(bold_vol) in called_args

        # No synthetic volume was written to the explicit cache's nifti dir.
        explicit_nifti = explicit_cache / "nifti"
        assert not explicit_nifti.exists() or not any(explicit_nifti.iterdir())

        # BOLD file is intact.
        assert bold_vol.read_bytes() == b"REAL_BOLD_DATA"


class TestSimulatedScannerSourceCancel:
    """cancel() terminates every tracked subprocess and reaps it."""

    @pytest.mark.asyncio
    async def test_cancel_terminates_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Prime cache.
        nifti_dir = tmp_path / "nifti"
        nifti_dir.mkdir()
        (nifti_dir / "001.nii.gz").touch()

        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.shutil.which",
            lambda _name: "/usr/local/bin/vSend",
        )

        # Build a proc that "runs forever" until terminate() is called.
        done = asyncio.Event()

        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock(side_effect=lambda: done.set())
        proc.kill = MagicMock()

        async def _wait() -> int:
            await done.wait()
            proc.returncode = 0
            return 0

        proc.wait = AsyncMock(side_effect=_wait)

        create = AsyncMock(return_value=proc)
        monkeypatch.setattr(
            "mindfulness_nf.orchestration.scanner_source.asyncio.create_subprocess_exec",
            create,
        )

        source = SimulatedScannerSource(cache_dir=tmp_path)

        # Start the push in the background; it will block on proc.wait().
        push_task = asyncio.create_task(
            source.push_vsend(tmp_path / "x.xml", tmp_path, _step())
        )
        # Yield control so the push registers its subprocess.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        await source.cancel()

        proc.terminate.assert_called_once()

        # The push task should now complete (wait() returns after terminate).
        await asyncio.wait_for(push_task, timeout=1.0)
