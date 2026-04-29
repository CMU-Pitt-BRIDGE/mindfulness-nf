"""Tests for the vendored ExternalImage protocol (vsend wire format).

Contract for the protocol helpers we use to stream NIfTI volumes over TCP
to MURFI's scanner input port. The round-trip test locks in the wire
format: what the sender packs, the receiver must unpack identically.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("nibabel")


class TestExternalImageRoundTrip:
    """hdr_to_bytes → hdr_from_bytes must preserve every field exactly."""

    def test_header_round_trips_losslessly(self) -> None:
        from mindfulness_nf.orchestration.external_image import ExternalImage

        ei = ExternalImage("Test")
        original = ei.named_tuple_class(
            magic=b"ERTI",
            headerVersion=1,
            seriesUID=b"abc123",
            scanType=b"EPI",
            imageType=b"3D",
            note=b"hello world",
            dataType=b"int16_t",
            isLittleEndian=True,
            isMosaic=True,
            pixelSpacingReadMM=2.0,
            pixelSpacingPhaseMM=2.0,
            pixelSpacingSliceMM=3.0,
            sliceGapMM=0.0,
            numPixelsRead=64,
            numPixelsPhase=64,
            numSlices=32,
            voxelToWorldMatrix=[1.0, 0.0, 0.0, 0.0,
                                0.0, 1.0, 0.0, 0.0,
                                0.0, 0.0, 1.0, 0.0,
                                0.0, 0.0, 0.0, 1.0],
            repetitionTimeMS=1200,
            repetitionDelayMS=0,
            currentTR=42,
            totalTR=250,
            isMotionCorrected=True,
            mcOrder=b"XYZT",
            mcTranslationXMM=0.1,
            mcTranslationYMM=0.2,
            mcTranslationZMM=0.01,
            mcRotationXRAD=0.001,
            mcRotationYRAD=0.002,
            mcRotationZRAD=0.0001,
        )
        packed = ei.hdr_to_bytes(original)
        assert len(packed) == ei.get_header_size()

        unpacked = ei.hdr_from_bytes(packed)
        assert unpacked.magic == "ERTI"  # bytes → str on unpack
        assert unpacked.headerVersion == 1
        assert unpacked.seriesUID == "abc123"
        assert unpacked.scanType == "EPI"
        assert unpacked.imageType == "3D"
        assert unpacked.note == "hello world"
        assert unpacked.dataType == "int16_t"
        assert unpacked.isLittleEndian is True
        assert unpacked.isMosaic is True
        assert unpacked.numPixelsRead == 64
        assert unpacked.numPixelsPhase == 64
        assert unpacked.numSlices == 32
        assert unpacked.repetitionTimeMS == 1200
        assert unpacked.currentTR == 42
        assert unpacked.totalTR == 250
        assert unpacked.voxelToWorldMatrix == list(original.voxelToWorldMatrix)

    def test_magic_must_be_ERTI_or_SIMU(self) -> None:
        """process_header rejects unrecognized magic values — guards against
        wire-format drift between sender and receiver."""
        from mindfulness_nf.orchestration.external_image import ExternalImage
        import struct

        ei = ExternalImage("Test")
        bad = struct.pack("4s", b"NOPE") + b"\x00" * (ei.get_header_size() - 4)
        with pytest.raises(ValueError, match="Unknown magic"):
            ei.process_header(bad)

    def test_send_nifti_retries_connection_until_receiver_ready(self, tmp_path) -> None:
        """Real MURFI takes ~1s to bind its scanner port after its
        subprocess starts, so a naive immediate connect fails with
        ECONNREFUSED. The sender must retry within a bounded window.
        """
        import socket
        import threading
        import time as _time
        from pathlib import Path
        import nibabel as nb
        from mindfulness_nf.orchestration.external_image import ExternalImage
        from mindfulness_nf.orchestration.scanner_source import (
            _send_nifti_via_vsend,
        )

        # Build one synthetic 3D NIfTI.
        data = np.zeros((8, 8, 4), dtype=np.uint16)
        img = nb.Nifti1Image(data, affine=np.eye(4))
        img.header.set_zooms((2.0, 2.0, 3.0))
        p = tmp_path / "vol-000.nii"
        nb.save(img, str(p))

        # Reserve a port with a temp socket, hold it closed until the
        # sender has started retrying, then open the real listener.
        temp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        temp.bind(("127.0.0.1", 0))
        _, port = temp.getsockname()
        temp.close()  # free the port; nothing listens yet

        def _delayed_server() -> None:
            _time.sleep(0.5)  # simulate MURFI startup delay
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.bind(("127.0.0.1", port))
            srv.listen(1)
            conn, _ = srv.accept()
            ei = ExternalImage("Recv")
            try:
                hdr_bytes = _recv_exact(conn, ei.get_header_size())
                ei.process_header(hdr_bytes)
                data_bytes = _recv_exact(conn, ei.get_image_size())
                ei.process_image(data_bytes)
            finally:
                conn.close()
                srv.close()

        threading.Thread(target=_delayed_server, daemon=True).start()

        # If _send_nifti_via_vsend lacks retry, this raises ConnectionRefusedError.
        _send_nifti_via_vsend(
            host="127.0.0.1",
            port=port,
            nifti_paths=(p,),
            tr_seconds=0.01,
        )


    def test_send_nifti_via_vsend_delivers_every_volume(self, tmp_path) -> None:
        """The sender opens a TCP connection, packs+sends each volume as
        (header + mosaic data), and the receiver can decode every one of
        them. This is the end-to-end protocol contract on the sender side.
        """
        import socket
        import threading
        from pathlib import Path
        import nibabel as nb
        from mindfulness_nf.orchestration.external_image import ExternalImage
        from mindfulness_nf.orchestration.scanner_source import (
            _send_nifti_via_vsend,
        )

        # Build 3 synthetic 3D NIfTIs on disk.
        paths: list[Path] = []
        for i in range(3):
            data = np.full((8, 8, 4), fill_value=i * 10, dtype=np.uint16)
            img = nb.Nifti1Image(data, affine=np.eye(4))
            img.header.set_zooms((2.0, 2.0, 3.0))
            p = tmp_path / f"vol-{i:03d}.nii"
            nb.save(img, str(p))
            paths.append(p)

        # Ephemeral receiver on 127.0.0.1.
        received_headers: list = []
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        host, port = server.getsockname()

        def _serve() -> None:
            # MURFI's real receiver accepts one connection per volume.
            for _ in range(3):
                conn, _addr = server.accept()
                ei = ExternalImage("Recv")
                try:
                    hdr_bytes = _recv_exact(conn, ei.get_header_size())
                    hdr = ei.process_header(hdr_bytes)
                    received_headers.append(hdr)
                    data_bytes = _recv_exact(conn, ei.get_image_size())
                    ei.process_image(data_bytes)
                finally:
                    conn.close()

        server_thread = threading.Thread(target=_serve, daemon=True)
        server_thread.start()

        _send_nifti_via_vsend(
            host=host,
            port=port,
            nifti_paths=tuple(paths),
            tr_seconds=0.01,
        )

        server_thread.join(timeout=5.0)
        server.close()

        assert len(received_headers) == 3
        # MURFI clamps currentTR to >= 1 (RtInputScannerImages.cpp:443):
        # sending 0-indexed TRs makes the first two volumes collide at
        # acquisition 1 — N-1 files saved. 1-indexed TRs avoid the collision.
        assert [h.currentTR for h in received_headers] == [1, 2, 3]
        assert all(h.totalTR == 3 for h in received_headers)


def _recv_exact(sock, nbytes: int) -> bytes:
    buf = bytearray()
    while len(buf) < nbytes:
        chunk = sock.recv(nbytes - len(buf))
        if not chunk:
            raise IOError("connection closed prematurely")
        buf.extend(chunk)
    return bytes(buf)


class TestFromImage:
    def test_from_image_returns_header_plus_mosaic_data(self) -> None:
        """from_image packs one volume into (header_bytes, mosaic_data_bytes)."""
        from mindfulness_nf.orchestration.external_image import ExternalImage
        import nibabel as nb

        ei = ExternalImage("Test")
        # Build a small 4D NIfTI (x,y,z,t) = (8,8,4,3)
        data = np.arange(8 * 8 * 4 * 3, dtype=np.uint16).reshape(8, 8, 4, 3)
        img = nb.Nifti1Image(data, affine=np.eye(4))
        img.header.set_zooms((2.0, 2.0, 3.0, 1.2))

        hdr_bytes, data_bytes = ei.from_image(img, idx=0, nt=3, mosaic_flag=True)
        assert len(hdr_bytes) == ei.get_header_size()
        # Mosaic packs the 4 slices into a 2x2 grid of 8x8 → 16x16 total uint16 values.
        # Total bytes = 16 * 16 * 2 = 512.
        assert len(data_bytes) == 16 * 16 * 2
