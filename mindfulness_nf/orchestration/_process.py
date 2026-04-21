"""Thin asyncio.subprocess wrapper with SIGTERM -> SIGKILL semantics.

Executors own their subprocesses via :class:`ManagedProcess`.  This keeps the
lifecycle details (process group signalling, graceful-then-forceful kill,
idempotent ``stop``, log redirection) in one place so each executor does not
re-implement them.

Not part of :class:`SessionRunner`'s public surface; executors are the only
intended callers.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import IO

__all__ = ["ManagedProcess"]


class ManagedProcess:
    """Thin wrapper around ``asyncio.subprocess.Process`` with SIGTERM -> SIGKILL.

    Parameters
    ----------
    name:
        Display name for logs (``"murfi"``, ``"psychopy"``, ``"dicom_receiver"``).
    cmd:
        Argv list (first element is the executable).
    log_path:
        Optional file to redirect stdout+stderr to (opened in append mode).
        When ``None`` the subprocess inherits the parent's stdout/stderr.
    env:
        Optional environment overrides; merged on top of :data:`os.environ`.
    """

    def __init__(
        self,
        name: str,
        cmd: list[str],
        log_path: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self._cmd = list(cmd)
        self._log_path = log_path
        self._env = env
        self._process: asyncio.subprocess.Process | None = None
        self._log_fh: IO[bytes] | None = None
        self._stop_lock = asyncio.Lock()
        # When non-None, a stop() is either in progress or has completed.
        # Concurrent callers coalesce by awaiting the same task.
        self._stop_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ start
    async def start(self) -> None:
        """Launch the subprocess.

        Raises
        ------
        RuntimeError
            If ``start`` has already been called (whether the process is
            still alive or has exited).
        """
        if self._process is not None:
            raise RuntimeError(
                f"ManagedProcess[{self.name}] already started (pid={self._process.pid})"
            )

        stdout: IO[bytes] | int | None
        stderr: int | None
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = self._log_path.open("ab")
            stdout = self._log_fh
            stderr = asyncio.subprocess.STDOUT
        else:
            # Inherit: pass None so child uses parent's stdout/stderr.
            stdout = None
            stderr = None

        env: dict[str, str] | None
        if self._env is not None:
            env = {**os.environ, **self._env}
        else:
            env = None

        self._process = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdout=stdout,
            stderr=stderr,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )

    # ------------------------------------------------------------------- stop
    async def stop(self, timeout: float = 5.0) -> None:
        """Terminate the subprocess: SIGTERM, wait ``timeout`` s, then SIGKILL.

        Idempotent: safe if ``start`` was never called and safe to call
        concurrently.  Concurrent callers coalesce via an :class:`asyncio.Lock`:
        the first caller performs the work, subsequent callers await the
        completed stop.
        """
        async with self._stop_lock:
            if self._stop_task is None:
                self._stop_task = asyncio.create_task(self._do_stop(timeout))
            task = self._stop_task

        # Await outside the lock so concurrent callers share the same task.
        await task

    async def _do_stop(self, timeout: float) -> None:
        proc = self._process
        try:
            if proc is None:
                return  # never started
            if proc.returncode is not None:
                return  # already exited

            # SIGTERM the process group (executors launch with start_new_session=True).
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                # Fall back to direct signal if pgid lookup fails.
                try:
                    proc.terminate()
                except (ProcessLookupError, OSError):
                    return

            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
                return
            except asyncio.TimeoutError:
                pass

            # Escalate to SIGKILL.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass

            # Always drain the process so returncode is set.
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
        finally:
            if self._log_fh is not None:
                try:
                    self._log_fh.close()
                except OSError:
                    pass
                self._log_fh = None

    # ------------------------------------------------------------------ state
    def is_alive(self) -> bool:
        """True iff the subprocess is running (spawned and no returncode)."""
        return self._process is not None and self._process.returncode is None

    def returncode(self) -> int | None:
        """Subprocess exit code, or ``None`` if not yet exited / never started."""
        return self._process.returncode if self._process is not None else None

    async def wait(self) -> int:
        """Wait for the subprocess to exit and return its returncode.

        Raises
        ------
        RuntimeError
            If ``start`` was never called.
        """
        if self._process is None:
            raise RuntimeError(f"ManagedProcess[{self.name}] not started")
        return await self._process.wait()

    @property
    def pid(self) -> int | None:
        """OS pid of the subprocess, or ``None`` if not started."""
        return self._process.pid if self._process is not None else None
