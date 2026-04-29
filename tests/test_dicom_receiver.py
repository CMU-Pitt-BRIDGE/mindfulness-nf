"""Tests for the async DICOM receiver.

Tests mock only external I/O (pynetdicom, socket). No mutable defaults.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from mindfulness_nf.orchestration.dicom_receiver import DicomReceiver, _handle_store


# ---------------------------------------------------------------------------
# volume_count — no mocks needed, just temp files
# ---------------------------------------------------------------------------

class TestVolumeCount:
    """volume_count counts .dcm files in the output directory."""

    def test_empty_directory(self, tmp_path: Path) -> None:
        receiver = DicomReceiver(
            output_dir=tmp_path, port=4006, ae_title="MURFI", server=MagicMock(),
        )
        assert receiver.volume_count() == 0

    def test_counts_dcm_files(self, tmp_path: Path) -> None:
        for i in range(5):
            (tmp_path / f"vol_{i}.dcm").touch()
        receiver = DicomReceiver(
            output_dir=tmp_path, port=4006, ae_title="MURFI", server=MagicMock(),
        )
        assert receiver.volume_count() == 5

    def test_ignores_non_dcm_files(self, tmp_path: Path) -> None:
        (tmp_path / "readme.txt").touch()
        (tmp_path / "image.nii").touch()
        (tmp_path / "actual.dcm").touch()
        receiver = DicomReceiver(
            output_dir=tmp_path, port=4006, ae_title="MURFI", server=MagicMock(),
        )
        assert receiver.volume_count() == 1


# ---------------------------------------------------------------------------
# _handle_store — saved file must be a valid DICOM (preamble + DICM magic)
# ---------------------------------------------------------------------------


class TestHandleStoreWritesCompliantDicom:
    """dcm2niix (and every DICOM parser) requires a 128-byte preamble
    followed by the ``DICM`` magic at bytes 128-132. Files saved without
    them were rejected as ``Not a DICOM image`` by the MURFI container's
    dcm2niix, so MURFI never converted any volume.
    """

    def test_saved_file_has_preamble_and_dicm_magic(
        self, tmp_path: Path
    ) -> None:
        """Reproduces what pynetdicom delivers at the handler: an
        ``event.dataset`` *without* a preamble (preamble isn't transmitted
        over the wire). Previous handler called ``save_as`` in the default
        ``write_like_original=True`` mode, which skips the preamble when
        the dataset has none — producing files that dcm2niix rejects.
        """
        from mindfulness_nf.orchestration.synthetic_volumes import (
            generate_synthetic_dicom,
        )

        src = tmp_path / "source.dcm"
        generate_synthetic_dicom(src, series_number=1, instance_number=1)
        import pydicom

        ds = pydicom.dcmread(src)
        # Simulate a wire-received dataset: no preamble.
        ds.preamble = None

        event = MagicMock()
        event.dataset = ds
        event.file_meta = ds.file_meta

        out_dir = tmp_path / "out"
        out_dir.mkdir()
        _handle_store(event, str(out_dir))

        written = next(out_dir.glob("*.dcm"))
        data = written.read_bytes()
        assert len(data) > 132, "file too short to contain preamble + DICM"
        assert data[:128] == b"\x00" * 128, (
            f"preamble missing or non-zero at bytes 0..127: first 16={data[:16]!r}"
        )
        assert data[128:132] == b"DICM", (
            f"expected DICM magic at offset 128, got {data[128:132]!r}"
        )


# ---------------------------------------------------------------------------
# start / stop lifecycle — mock pynetdicom AE
# ---------------------------------------------------------------------------

class TestStartStop:
    """start() creates a running receiver; stop() shuts it down."""

    @pytest.mark.asyncio
    async def test_start_creates_receiver(self, tmp_path: Path) -> None:
        mock_server = MagicMock()
        mock_ae_instance = MagicMock()
        mock_ae_instance.start_server.return_value = mock_server

        with patch(
            "mindfulness_nf.orchestration.dicom_receiver.AE",
            return_value=mock_ae_instance,
        ):
            receiver = await DicomReceiver.start(
                output_dir=tmp_path, port=4006, ae_title="TEST_AE",
            )

        assert receiver._port == 4006
        assert receiver._ae_title == "TEST_AE"
        assert receiver._server is mock_server
        mock_ae_instance.start_server.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_shuts_down_server(self, tmp_path: Path) -> None:
        mock_server = MagicMock()
        receiver = DicomReceiver(
            output_dir=tmp_path, port=4006, ae_title="MURFI", server=mock_server,
        )

        await receiver.stop()

        mock_server.shutdown.assert_called_once()
        assert receiver._server is None

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, tmp_path: Path) -> None:
        """Calling stop() twice does not raise."""
        mock_server = MagicMock()
        receiver = DicomReceiver(
            output_dir=tmp_path, port=4006, ae_title="MURFI", server=mock_server,
        )

        await receiver.stop()
        await receiver.stop()  # should not raise

        mock_server.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_reraises_cancelled_error(self, tmp_path: Path) -> None:
        """stop() re-raises CancelledError after cleanup."""
        mock_server = MagicMock()
        mock_server.shutdown.side_effect = asyncio.CancelledError

        receiver = DicomReceiver(
            output_dir=tmp_path, port=4006, ae_title="MURFI", server=mock_server,
        )

        with pytest.raises(asyncio.CancelledError):
            await receiver.stop()

    @pytest.mark.asyncio
    async def test_start_creates_output_dir(self, tmp_path: Path) -> None:
        """start() creates the output directory if it doesn't exist."""
        output = tmp_path / "subdir" / "dicom_output"
        mock_server = MagicMock()
        mock_ae_instance = MagicMock()
        mock_ae_instance.start_server.return_value = mock_server

        with patch(
            "mindfulness_nf.orchestration.dicom_receiver.AE",
            return_value=mock_ae_instance,
        ):
            receiver = await DicomReceiver.start(output_dir=output, port=4006)

        assert output.is_dir()
        assert receiver._output_dir == output


# ---------------------------------------------------------------------------
# wait_for_ready — mock socket
# ---------------------------------------------------------------------------

class TestWaitForReady:
    """wait_for_ready polls the port until it responds or times out."""

    @pytest.mark.asyncio
    async def test_ready_when_port_open(self, tmp_path: Path) -> None:
        receiver = DicomReceiver(
            output_dir=tmp_path, port=4006, ae_title="MURFI", server=MagicMock(),
        )
        with patch.object(receiver, "_check_port", return_value=True):
            result = await receiver.wait_for_ready(timeout=1.0)
        assert result is True

    @pytest.mark.asyncio
    async def test_timeout_when_port_closed(self, tmp_path: Path) -> None:
        receiver = DicomReceiver(
            output_dir=tmp_path, port=4006, ae_title="MURFI", server=MagicMock(),
        )
        with patch.object(receiver, "_check_port", return_value=False):
            result = await receiver.wait_for_ready(timeout=0.3)
        assert result is False

    @pytest.mark.asyncio
    async def test_becomes_ready_after_retries(self, tmp_path: Path) -> None:
        """Port starts closed, then opens — should return True."""
        receiver = DicomReceiver(
            output_dir=tmp_path, port=4006, ae_title="MURFI", server=MagicMock(),
        )
        # First two checks fail, third succeeds
        with patch.object(
            receiver, "_check_port", side_effect=[False, False, True],
        ):
            result = await receiver.wait_for_ready(timeout=5.0)
        assert result is True


# ---------------------------------------------------------------------------
# _check_port — mock socket.create_connection
# ---------------------------------------------------------------------------

class TestCheckPort:
    """_check_port returns True/False based on TCP connectivity."""

    def test_returns_true_when_connected(self, tmp_path: Path) -> None:
        receiver = DicomReceiver(
            output_dir=tmp_path, port=4006, ae_title="MURFI", server=MagicMock(),
        )
        mock_conn = MagicMock()
        with patch(
            "mindfulness_nf.orchestration.dicom_receiver.socket.create_connection",
            return_value=mock_conn,
        ):
            assert receiver._check_port() is True
        mock_conn.__enter__.return_value = mock_conn
        # context manager was used

    def test_returns_false_on_oserror(self, tmp_path: Path) -> None:
        receiver = DicomReceiver(
            output_dir=tmp_path, port=4006, ae_title="MURFI", server=MagicMock(),
        )
        with patch(
            "mindfulness_nf.orchestration.dicom_receiver.socket.create_connection",
            side_effect=OSError("Connection refused"),
        ):
            assert receiver._check_port() is False
