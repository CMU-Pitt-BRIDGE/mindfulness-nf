"""End-to-end integration for vSend-based dry-run (2vol / rtdmn steps).

Proves that the Python-native vSend sender delivers bytes over TCP that
a receiver using the same :class:`ExternalImage` protocol can decode back
into valid NIfTI volumes — without any external binary.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from pathlib import Path

import nibabel as nb
import numpy as np
import pytest

from mindfulness_nf.models import StepConfig, StepKind
from mindfulness_nf.orchestration.external_image import ExternalImage
from mindfulness_nf.orchestration.scanner_source import SimulatedScannerSource


def _recv_exact(sock: socket.socket, nbytes: int) -> bytes:
    buf = bytearray()
    while len(buf) < nbytes:
        chunk = sock.recv(nbytes - len(buf))
        if not chunk:
            raise IOError("connection closed prematurely")
        buf.extend(chunk)
    return bytes(buf)


@pytest.mark.asyncio
async def test_vsend_end_to_end_three_volumes(tmp_path: Path) -> None:
    """Prime cache with 3 NIfTIs, start an ephemeral receiver, run
    ``SimulatedScannerSource.push_vsend``, verify the receiver decodes
    3 valid NIfTI volumes — each one round-tripping the pixel data."""
    # Cache with 3 NIfTIs of distinct pixel fill values so we can verify each.
    cache = tmp_path / "cache"
    nifti_dir = cache / "nifti"
    nifti_dir.mkdir(parents=True)
    expected_values = [42, 137, 200]
    for i, value in enumerate(expected_values):
        data = np.full((8, 8, 4), fill_value=value, dtype=np.uint16)
        img = nb.Nifti1Image(data, affine=np.eye(4))
        img.header.set_zooms((2.0, 2.0, 3.0))
        nb.save(img, str(nifti_dir / f"vol-{i:03d}.nii"))

    # Ephemeral receiver on 127.0.0.1. Accept one connection, decode 3 volumes.
    received_values: list[int] = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    _, port = server.getsockname()

    def _serve() -> None:
        # MURFI's real receiver accepts one connection per volume. Mirror
        # that here — loop accepting fresh connections until we've decoded
        # all 3 volumes.
        for _ in range(3):
            conn, _addr = server.accept()
            ei = ExternalImage("Recv")
            try:
                hdr_bytes = _recv_exact(conn, ei.get_header_size())
                ei.process_header(hdr_bytes)
                data_bytes = _recv_exact(conn, ei.get_image_size())
                img = ei.process_image(data_bytes)
                data = img.get_fdata().astype(np.uint16).flatten()
                nonzero = data[data != 0]
                if len(nonzero) > 0:
                    vals, counts = np.unique(nonzero, return_counts=True)
                    received_values.append(int(vals[counts.argmax()]))
                else:
                    received_values.append(0)
            finally:
                conn.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()

    # Monkey-patch the target so push_vsend hits our ephemeral port instead
    # of the hardcoded 50000.
    import mindfulness_nf.orchestration.scanner_source as ss_mod

    original_send = ss_mod._send_nifti_via_vsend

    def _send_to_ephemeral(host, _hardcoded_port, nifti_paths, tr_seconds):
        original_send("127.0.0.1", port, nifti_paths, tr_seconds)

    ss_mod._send_nifti_via_vsend = _send_to_ephemeral  # type: ignore[attr-defined]
    try:
        source = SimulatedScannerSource(cache_dir=cache, tr_seconds=0.01)
        step = StepConfig(
            name="2-volume",
            task="2vol",
            run=1,
            progress_target=3,
            progress_unit="volumes",
            xml_name="2vol.xml",
            kind=StepKind.VSEND_SCAN,
        )
        xml = tmp_path / "2vol.xml"
        xml.write_text("<xml/>")
        await source.push_vsend(xml_path=xml, subject_dir=tmp_path, step=step)
    finally:
        ss_mod._send_nifti_via_vsend = original_send  # type: ignore[attr-defined]
        thread.join(timeout=5.0)
        server.close()

    # Each volume was packed, sent, received, and decoded — and the pixel
    # fill value in the decoded NIfTI matches what we synthesized.
    assert received_values == expected_values
