"""Tests for mindfulness_nf.orchestration._process.ManagedProcess.

Uses ``sleep 10`` as a dummy long-running subprocess so we can exercise
SIGTERM -> SIGKILL semantics, idempotent stop, and concurrent-call
coalescing without depending on application logic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mindfulness_nf.orchestration._process import ManagedProcess


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sleep_cmd(seconds: int = 10) -> list[str]:
    return ["sleep", str(seconds)]


# ---------------------------------------------------------------------------
# Happy-path lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_launches_process_and_is_alive() -> None:
    mp = ManagedProcess("sleeper", _sleep_cmd(10))
    await mp.start()
    try:
        assert mp.is_alive() is True
        assert mp.pid is not None
        assert mp.returncode() is None
    finally:
        await mp.stop()


@pytest.mark.asyncio
async def test_start_twice_raises() -> None:
    mp = ManagedProcess("sleeper", _sleep_cmd(10))
    await mp.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            await mp.start()
    finally:
        await mp.stop()


# ---------------------------------------------------------------------------
# SIGTERM -> SIGKILL semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_sigterm_then_sigkill() -> None:
    """SIGTERM succeeds on ``sleep``; verify the process ends and state updates."""
    mp = ManagedProcess("sleeper", _sleep_cmd(30))
    await mp.start()
    pid = mp.pid
    assert pid is not None

    await mp.stop(timeout=2.0)

    assert mp.is_alive() is False
    assert mp.returncode() is not None
    # SIGTERM on `sleep` produces returncode = -SIGTERM (negative) or 143.
    # We only care that it exited, not the specific encoding.


@pytest.mark.asyncio
async def test_stop_escalates_to_sigkill_when_sigterm_ignored(
    tmp_path: Path,
) -> None:
    """A shell script that traps SIGTERM forces the SIGKILL path."""
    script = tmp_path / "ignore_term.sh"
    script.write_text("#!/bin/sh\ntrap '' TERM\nwhile true; do sleep 1; done\n")
    script.chmod(0o755)

    mp = ManagedProcess("stubborn", [str(script)])
    await mp.start()

    # With timeout=0.3, SIGTERM will be ignored; SIGKILL must follow.
    await mp.stop(timeout=0.3)

    assert mp.is_alive() is False
    assert mp.returncode() is not None


# ---------------------------------------------------------------------------
# Idempotence / coalescing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    mp = ManagedProcess("sleeper", _sleep_cmd(10))
    await mp.start()

    await mp.stop(timeout=2.0)
    rc_first = mp.returncode()

    # Second call must be a no-op (no error, returncode unchanged).
    await mp.stop(timeout=2.0)
    assert mp.returncode() == rc_first
    assert mp.is_alive() is False


@pytest.mark.asyncio
async def test_stop_concurrent_calls_coalesce() -> None:
    """Three concurrent stop()s must all resolve; only one terminate cycle."""
    mp = ManagedProcess("sleeper", _sleep_cmd(30))
    await mp.start()
    assert mp._process is not None

    # Spy on wait() so we can observe how many times the stop path was driven.
    real_wait = mp._process.wait
    wait_spy = AsyncMock(side_effect=real_wait)
    mp._process.wait = wait_spy  # type: ignore[method-assign]

    results = await asyncio.gather(
        mp.stop(timeout=2.0),
        mp.stop(timeout=2.0),
        mp.stop(timeout=2.0),
    )

    assert results == [None, None, None]
    assert mp.is_alive() is False
    # Coalesced: the underlying stop logic ran once, so wait() (the "await exit"
    # call) was invoked exactly once even though three callers awaited stop().
    assert wait_spy.await_count == 1


@pytest.mark.asyncio
async def test_stop_before_start_is_noop() -> None:
    mp = ManagedProcess("never-started", _sleep_cmd(10))

    # Must not raise; must not touch any process.
    await mp.stop(timeout=2.0)
    await mp.stop(timeout=2.0)  # and still idempotent

    assert mp.is_alive() is False
    assert mp.returncode() is None
    assert mp.pid is None


@pytest.mark.asyncio
async def test_stop_on_already_exited_process_is_noop() -> None:
    mp = ManagedProcess("quick", ["true"])
    await mp.start()
    # Let the process exit on its own.
    await mp.wait()
    assert mp.is_alive() is False

    # stop() after natural exit must be a no-op.
    await mp.stop(timeout=2.0)
    assert mp.returncode() == 0


# ---------------------------------------------------------------------------
# wait()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_returns_returncode() -> None:
    mp = ManagedProcess("quick", ["true"])
    await mp.start()
    rc = await mp.wait()
    assert rc == 0
    assert mp.returncode() == 0
    assert mp.is_alive() is False


@pytest.mark.asyncio
async def test_wait_before_start_raises() -> None:
    mp = ManagedProcess("never-started", _sleep_cmd(10))
    with pytest.raises(RuntimeError, match="not started"):
        await mp.wait()


# ---------------------------------------------------------------------------
# Log redirection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_redirects_stdout_to_log_path(tmp_path: Path) -> None:
    log_path = tmp_path / "subdir" / "out.log"
    mp = ManagedProcess(
        "echoer", ["sh", "-c", "echo hello-from-subprocess"], log_path=log_path
    )
    await mp.start()
    rc = await mp.wait()
    assert rc == 0

    assert log_path.exists()
    assert "hello-from-subprocess" in log_path.read_text()
