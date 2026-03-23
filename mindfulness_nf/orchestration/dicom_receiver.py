"""Async DICOM receiver wrapping pynetdicom for background C-STORE handling.

Imperative shell — I/O expected. Imports models from core.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path
from typing import TYPE_CHECKING

from pynetdicom import AE, StoragePresentationContexts, VerificationPresentationContexts, evt

if TYPE_CHECKING:
    from pynetdicom.transport import ThreadedAssociationServer

logger = logging.getLogger(__name__)


def _handle_store(event: evt.Event, output_dir: str) -> int:
    """Handle incoming C-STORE request — save DICOM to output directory."""
    ds = event.dataset
    ds.file_meta = event.file_meta

    filename = f"{ds.SOPInstanceUID}.dcm"
    filepath = Path(output_dir) / filename
    ds.save_as(filepath)

    series = getattr(ds, "SeriesNumber", "?")
    instance = getattr(ds, "InstanceNumber", "?")
    desc = getattr(ds, "SeriesDescription", "")
    logger.info("Received: series %s / instance %s  %s  -> %s", series, instance, desc, filename)

    return 0x0000  # Success


class DicomReceiver:
    """Async wrapper around a pynetdicom AE that accepts C-STORE requests.

    Use the ``start`` classmethod to create an instance; call ``stop`` to
    shut down.  The underlying AE runs in a background thread via
    ``asyncio.to_thread``.
    """

    def __init__(
        self,
        output_dir: Path,
        port: int,
        ae_title: str,
        server: ThreadedAssociationServer,
    ) -> None:
        self._output_dir = output_dir
        self._port = port
        self._ae_title = ae_title
        self._server: ThreadedAssociationServer | None = server

    # -- factory ----------------------------------------------------------

    @classmethod
    async def start(
        cls,
        output_dir: Path,
        port: int = 4006,
        ae_title: str = "MURFI",
    ) -> DicomReceiver:
        """Create and start a DICOM receiver listening on *port*.

        The pynetdicom server is started in a background thread (non-blocking
        mode) so the asyncio event loop is not blocked.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        ae = AE(ae_title=ae_title)
        ae.supported_contexts = (
            VerificationPresentationContexts + StoragePresentationContexts
        )

        handlers = [(evt.EVT_C_STORE, _handle_store, [str(output_dir)])]

        # start_server with block=False returns a ThreadedAssociationServer
        # that runs in its own daemon thread.
        server: ThreadedAssociationServer = await asyncio.to_thread(
            ae.start_server,
            ("0.0.0.0", port),
            evt_handlers=handlers,
            block=False,
        )

        logger.info("DICOM receiver started  AE=%s  port=%d  dir=%s", ae_title, port, output_dir)
        return cls(output_dir=output_dir, port=port, ae_title=ae_title, server=server)

    # -- public API -------------------------------------------------------

    async def stop(self) -> None:
        """Shut down the DICOM receiver.

        Re-raises ``asyncio.CancelledError`` after cleanup so callers can
        detect cancellation.
        """
        cancelled = False
        try:
            if self._server is not None:
                server = self._server
                self._server = None
                await asyncio.to_thread(server.shutdown)
                logger.info("DICOM receiver stopped")
        except asyncio.CancelledError:
            cancelled = True
        finally:
            if cancelled:
                raise asyncio.CancelledError

    def volume_count(self) -> int:
        """Return the number of ``.dcm`` files in the output directory."""
        return len(list(self._output_dir.glob("*.dcm")))

    async def wait_for_ready(self, timeout: float = 10.0) -> bool:
        """Wait until the receiver port is accepting connections.

        Returns ``True`` if the port responds within *timeout* seconds,
        ``False`` otherwise.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if self._check_port():
                return True
            await asyncio.sleep(0.1)
        return False

    # -- internal ---------------------------------------------------------

    def _check_port(self) -> bool:
        """Return True if the receiver port accepts a TCP connection."""
        try:
            with socket.create_connection(("127.0.0.1", self._port), timeout=0.5):
                return True
        except OSError:
            return False
