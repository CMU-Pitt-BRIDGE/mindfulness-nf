"""Tests for the interruptible FSL subprocess helper."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from mindfulness_nf.orchestration.fsl_subprocess import run_interruptible


@pytest.mark.asyncio
async def test_run_to_completion_returns_zero() -> None:
    rc = await run_interruptible(["/bin/true"])
    assert rc == 0


@pytest.mark.asyncio
async def test_nonzero_exit_raises_when_check_true() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        await run_interruptible(["/bin/false"], check=True)


@pytest.mark.asyncio
async def test_nonzero_exit_no_raise_when_check_false() -> None:
    rc = await run_interruptible(["/bin/false"], check=False)
    assert rc != 0


@pytest.mark.asyncio
async def test_stop_event_kills_long_running_process() -> None:
    """Sleep 30s, trigger stop_event 100ms in, expect CancelledError fast."""
    stop_event = asyncio.Event()

    async def _fire_stop() -> None:
        await asyncio.sleep(0.1)
        stop_event.set()

    asyncio.create_task(_fire_stop())
    t0 = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await run_interruptible(["sleep", "30"], stop_event=stop_event)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"interrupt should be fast; took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_stdout_to_file(tmp_path: Path) -> None:
    log = tmp_path / "out.log"
    await run_interruptible(["echo", "hello world"], stdout=log)
    assert log.read_text().strip() == "hello world"


@pytest.mark.asyncio
async def test_completion_before_stop_returns_cleanly() -> None:
    stop_event = asyncio.Event()
    # /bin/true completes before stop_event fires.
    rc = await run_interruptible(["/bin/true"], stop_event=stop_event)
    assert rc == 0


@pytest.mark.asyncio
async def test_sigkill_escalation_for_ignore_sigterm() -> None:
    """A process that traps SIGTERM should still die via SIGKILL."""
    stop_event = asyncio.Event()
    # bash: trap '' TERM then sleep — ignores SIGTERM, must be SIGKILLed.
    script = "trap '' TERM; sleep 30"

    async def _fire() -> None:
        await asyncio.sleep(0.1)
        stop_event.set()

    asyncio.create_task(_fire())
    t0 = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await run_interruptible(
            ["bash", "-c", script], stop_event=stop_event, check=False
        )
    elapsed = time.monotonic() - t0
    # SIGTERM grace (~2s) then SIGKILL.
    assert elapsed < 5.0
