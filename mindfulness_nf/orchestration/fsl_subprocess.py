"""Interruptible subprocess helper for FSL calls.

``subprocess.run`` inside ``asyncio.to_thread`` is uncancellable — Python
threads can't be forcibly stopped, so an in-flight ``flirt``/``bet``/
``fslmaths`` call ignores operator ``i`` presses. ``asyncio.create_subprocess_exec``
gives us a real :class:`asyncio.subprocess.Process` we can signal.

Callers pass an ``asyncio.Event`` that the executor sets on ``stop()``.
If the event fires while the subprocess is still running, we SIGTERM it,
wait a short grace, then SIGKILL. The await returns ``CancelledError`` so
the executor returns a cancelled outcome cleanly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_TERMINATE_GRACE_SECONDS = 2.0


async def run_interruptible(
    cmd: list[str],
    *,
    stop_event: asyncio.Event | None = None,
    stdout: int | Path | None = subprocess.DEVNULL,
    stderr: int | Path | None = subprocess.DEVNULL,
    cwd: str | None = None,
    check: bool = True,
) -> int:
    """Run *cmd* as an asyncio subprocess; terminate on *stop_event*.

    ``stdout``/``stderr`` accept ``subprocess.DEVNULL``/``subprocess.PIPE``
    constants or a :class:`pathlib.Path` (file is opened for binary write).

    Raises :class:`subprocess.CalledProcessError` when ``check=True`` and
    the process exits non-zero (and wasn't cancelled by stop_event).

    Raises :class:`asyncio.CancelledError` when ``stop_event`` is set and
    the subprocess is killed as a result.
    """
    stdout_fh = _open_target(stdout, "wb") if isinstance(stdout, Path) else stdout
    stderr_fh = _open_target(stderr, "wb") if isinstance(stderr, Path) else stderr
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=stdout_fh,
            stderr=stderr_fh,
            cwd=cwd,
            start_new_session=True,  # own process group for group kill
        )
        return await _await_with_stop(proc, cmd, stop_event, check)
    finally:
        _close_if_file(stdout_fh)
        _close_if_file(stderr_fh)


async def _await_with_stop(
    proc: asyncio.subprocess.Process,
    cmd: list[str],
    stop_event: asyncio.Event | None,
    check: bool,
) -> int:
    """Wait for *proc* to exit or *stop_event* to fire.

    When the event fires first, send SIGTERM to the process group, wait up
    to :data:`_TERMINATE_GRACE_SECONDS`, then SIGKILL if still alive. Raises
    ``asyncio.CancelledError`` to surface cancellation to the caller.
    """
    if stop_event is None:
        rc = await proc.wait()
        if check and rc != 0:
            raise subprocess.CalledProcessError(rc, cmd)
        return rc

    wait_task = asyncio.create_task(proc.wait())
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            {wait_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        # Our own task was cancelled — kill the subprocess too.
        _kill_group(proc)
        raise
    if wait_task in done:
        stop_task.cancel()
        rc = wait_task.result()
        if check and rc != 0:
            raise subprocess.CalledProcessError(rc, cmd)
        return rc

    # stop_event fired — terminate the subprocess.
    wait_task.cancel()
    _kill_group(proc)
    try:
        await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
    except asyncio.TimeoutError:
        _kill_group(proc, force=True)
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
    raise asyncio.CancelledError(f"FSL subprocess interrupted: {cmd[0]}")


def _kill_group(proc: asyncio.subprocess.Process, *, force: bool = False) -> None:
    """Terminate (or kill) the subprocess's process group.

    We started with ``start_new_session=True`` so ``proc.pid`` is also the
    process-group ID; signalling the group catches any children FSL has
    spawned (``fsl_sub``, ``melodic``, internal worker processes).
    """
    if proc.returncode is not None:
        return
    pgid = proc.pid
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError) as exc:
        logger.debug("killpg(%d, %s) failed: %s", pgid, sig, exc)


def _open_target(path: Path, mode: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open(mode)


def _close_if_file(handle: object) -> None:
    """Close file objects we opened; leave constants (DEVNULL/PIPE) alone."""
    if handle is None:
        return
    if isinstance(handle, int):
        return
    close = getattr(handle, "close", None)
    if close is not None:
        try:
            close()
        except OSError:
            pass
