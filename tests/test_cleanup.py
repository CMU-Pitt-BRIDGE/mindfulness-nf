"""Tests for :mod:`mindfulness_nf.orchestration.cleanup`.

Mocks only external I/O (subprocess, os.kill). Never mocks internal code.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from mindfulness_nf.config import ScannerConfig
from mindfulness_nf.orchestration import cleanup as cleanup_mod
from mindfulness_nf.orchestration.cleanup import (
    NAME_PATTERNS,
    _kill_pid,
    _pids_by_pattern,
    _pids_on_port,
    cleanup_stale_processes,
)


# ---------------------------------------------------------------------------
# Low-level discovery
# ---------------------------------------------------------------------------


def _cp(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestPidsOnPort:
    def test_parses_pids_from_fuser(self) -> None:
        # fuser writes PIDs to stdout; the "4006/tcp:" header goes to stderr.
        with patch.object(
            cleanup_mod.subprocess,
            "run",
            return_value=_cp(stdout=" 1234 5678", stderr="4006/tcp:"),
        ):
            pids = asyncio.run(_pids_on_port(4006))
        assert pids == (1234, 5678)

    def test_strips_trailing_mode_letter(self) -> None:
        with patch.object(
            cleanup_mod.subprocess,
            "run",
            return_value=_cp(stdout=" 1234c", stderr="4006/tcp:"),
        ):
            pids = asyncio.run(_pids_on_port(4006))
        assert pids == (1234,)

    def test_does_not_confuse_port_header_for_pid(self) -> None:
        # Historic bug: parser that read stderr too would read "4006/tcp:"
        # and fabricate pid 4006. Ensure we don't.
        with patch.object(
            cleanup_mod.subprocess,
            "run",
            return_value=_cp(stdout=" 1234", stderr="4006/tcp:"),
        ):
            pids = asyncio.run(_pids_on_port(4006))
        assert pids == (1234,)
        assert 4006 not in pids

    def test_no_listeners(self) -> None:
        with patch.object(cleanup_mod.subprocess, "run", return_value=_cp(returncode=1)):
            pids = asyncio.run(_pids_on_port(4006))
        assert pids == ()

    def test_fuser_missing(self) -> None:
        with patch.object(cleanup_mod.subprocess, "run", side_effect=FileNotFoundError()):
            pids = asyncio.run(_pids_on_port(4006))
        assert pids == ()

    def test_fuser_timeout(self) -> None:
        with patch.object(
            cleanup_mod.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(cmd=[], timeout=5),
        ):
            pids = asyncio.run(_pids_on_port(4006))
        assert pids == ()

    def test_dedupes_repeated_pids(self) -> None:
        with patch.object(cleanup_mod.subprocess, "run", return_value=_cp(stdout="1234 1234 5678")):
            pids = asyncio.run(_pids_on_port(4006))
        assert pids == (1234, 5678)


class TestPidsByPattern:
    def test_parses_newline_separated_pids(self) -> None:
        with patch.object(cleanup_mod.subprocess, "run", return_value=_cp(stdout="1234\n5678\n")):
            pids = asyncio.run(_pids_by_pattern("murfi.sif"))
        assert pids == (1234, 5678)

    def test_no_matches_rc1(self) -> None:
        # pgrep returns 1 when nothing matches — that's a legitimate "empty".
        with patch.object(cleanup_mod.subprocess, "run", return_value=_cp(stdout="", returncode=1)):
            pids = asyncio.run(_pids_by_pattern("murfi.sif"))
        assert pids == ()

    def test_pgrep_missing(self) -> None:
        with patch.object(cleanup_mod.subprocess, "run", side_effect=FileNotFoundError()):
            pids = asyncio.run(_pids_by_pattern("murfi.sif"))
        assert pids == ()


# ---------------------------------------------------------------------------
# _kill_pid
# ---------------------------------------------------------------------------


class TestKillPid:
    def test_sigterm_succeeds_first_poll(self) -> None:
        # Process is alive for the initial os.kill(0), dies before first poll.
        alive_sequence = iter([False])  # after SIGTERM, dead on first _alive check

        def fake_kill(pid: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                raise AssertionError("should not escalate")
            # SIGTERM accepted silently.

        def fake_alive(pid: int) -> bool:
            return next(alive_sequence)

        with patch.object(cleanup_mod.os, "kill", side_effect=fake_kill):
            with patch.object(cleanup_mod, "_alive", side_effect=fake_alive):
                with patch.object(cleanup_mod.asyncio, "sleep", new=_no_sleep):
                    killed, msg = asyncio.run(_kill_pid(9999))
        assert killed is True
        assert msg == "SIGTERM"

    def test_sigkill_escalation(self) -> None:
        # Process survives all five SIGTERM polls, dies after SIGKILL.
        alive_calls = {"n": 0}

        def fake_alive(pid: int) -> bool:
            alive_calls["n"] += 1
            # 5 SIGTERM polls return True, post-SIGKILL poll returns False.
            return alive_calls["n"] <= 5

        sent: list[int] = []

        def fake_kill(pid: int, sig: int) -> None:
            sent.append(sig)

        with patch.object(cleanup_mod.os, "kill", side_effect=fake_kill):
            with patch.object(cleanup_mod, "_alive", side_effect=fake_alive):
                with patch.object(cleanup_mod.asyncio, "sleep", new=_no_sleep):
                    killed, msg = asyncio.run(_kill_pid(9999))
        assert killed is True
        assert msg == "SIGKILL"
        assert sent == [signal.SIGTERM, signal.SIGKILL]

    def test_already_dead_before_sigterm(self) -> None:
        def fake_kill(pid: int, sig: int) -> None:
            raise ProcessLookupError()

        with patch.object(cleanup_mod.os, "kill", side_effect=fake_kill):
            killed, msg = asyncio.run(_kill_pid(9999))
        assert killed is True
        assert "already" in msg

    def test_permission_denied(self) -> None:
        def fake_kill(pid: int, sig: int) -> None:
            raise PermissionError("Operation not permitted")

        with patch.object(cleanup_mod.os, "kill", side_effect=fake_kill):
            killed, msg = asyncio.run(_kill_pid(9999))
        assert killed is False
        assert "permission denied" in msg


# ---------------------------------------------------------------------------
# cleanup_stale_processes — orchestration
# ---------------------------------------------------------------------------


@pytest.fixture()
def config() -> ScannerConfig:
    return ScannerConfig()


class TestCleanupOrchestration:
    def test_empty_machine_no_actions(self, config: ScannerConfig) -> None:
        with patch.object(cleanup_mod, "_pids_on_port", new=AsyncMock(return_value=())):
            with patch.object(cleanup_mod, "_pids_by_pattern", new=AsyncMock(return_value=())):
                with patch.object(cleanup_mod, "_cleanup_tmux_session", new=AsyncMock(return_value=None)):
                    actions = asyncio.run(cleanup_stale_processes(config))
        assert actions == ()

    def test_port_kill_only(self, config: ScannerConfig) -> None:
        # 4006 held by pid 1234; other ports/patterns empty.
        async def pids_on_port(port: int) -> tuple[int, ...]:
            return (1234,) if port == config.dicom_port else ()

        with patch.object(cleanup_mod, "_pids_on_port", side_effect=pids_on_port):
            with patch.object(cleanup_mod, "_pids_by_pattern", new=AsyncMock(return_value=())):
                with patch.object(cleanup_mod, "_kill_pid", new=AsyncMock(return_value=(True, "SIGTERM"))):
                    with patch.object(cleanup_mod, "_cleanup_tmux_session", new=AsyncMock(return_value=None)):
                        actions = asyncio.run(cleanup_stale_processes(config))
        assert len(actions) == 1
        assert actions[0].pid == 1234
        assert actions[0].killed is True
        assert "dicom_port" in actions[0].target

    def test_name_match_after_port(self, config: ScannerConfig) -> None:
        # Port 50000 → pid 100; first NAME pattern → pids 100, 200.
        # pid 100 should be attributed to the port and not re-killed by pattern.
        first_pattern = NAME_PATTERNS[0]

        async def pids_on_port(port: int) -> tuple[int, ...]:
            return (100,) if port == config.vsend_port else ()

        async def pids_by_pattern(pattern: str) -> tuple[int, ...]:
            return (100, 200) if pattern == first_pattern else ()

        killed_pids: list[int] = []

        async def kill(pid: int) -> tuple[bool, str]:
            killed_pids.append(pid)
            return (True, "SIGTERM")

        with patch.object(cleanup_mod, "_pids_on_port", side_effect=pids_on_port):
            with patch.object(cleanup_mod, "_pids_by_pattern", side_effect=pids_by_pattern):
                with patch.object(cleanup_mod, "_kill_pid", side_effect=kill):
                    with patch.object(cleanup_mod, "_cleanup_tmux_session", new=AsyncMock(return_value=None)):
                        actions = asyncio.run(cleanup_stale_processes(config))
        assert killed_pids == [100, 200]  # each killed exactly once
        # Two actions: one for port attribution, one for pattern attribution.
        assert len(actions) == 2
        assert actions[0].target.startswith("port 50000")
        assert actions[1].target.startswith(f"process {first_pattern}")

    def test_self_pid_excluded(self, config: ScannerConfig) -> None:
        self_pid = os.getpid()

        async def pids_on_port(port: int) -> tuple[int, ...]:
            return (self_pid,) if port == config.dicom_port else ()

        async def kill(pid: int) -> tuple[bool, str]:
            raise AssertionError(f"_kill_pid should not be called for own pid {pid}")

        with patch.object(cleanup_mod, "_pids_on_port", side_effect=pids_on_port):
            with patch.object(cleanup_mod, "_pids_by_pattern", new=AsyncMock(return_value=())):
                with patch.object(cleanup_mod, "_kill_pid", side_effect=kill):
                    with patch.object(cleanup_mod, "_cleanup_tmux_session", new=AsyncMock(return_value=None)):
                        actions = asyncio.run(cleanup_stale_processes(config))
        assert len(actions) == 1
        assert actions[0].killed is False
        assert "ancestor" in actions[0].message

    def test_ancestor_pid_excluded(self, config: ScannerConfig) -> None:
        """A non-direct ancestor (e.g. the launching shell) must also be spared."""
        fake_ancestor = 999_999

        async def pids_on_port(port: int) -> tuple[int, ...]:
            return (fake_ancestor,) if port == config.dicom_port else ()

        async def kill(pid: int) -> tuple[bool, str]:
            raise AssertionError(f"_kill_pid should not be called for ancestor {pid}")

        with patch.object(cleanup_mod, "_ancestry_pids", return_value={os.getpid(), fake_ancestor}):
            with patch.object(cleanup_mod, "_pids_on_port", side_effect=pids_on_port):
                with patch.object(cleanup_mod, "_pids_by_pattern", new=AsyncMock(return_value=())):
                    with patch.object(cleanup_mod, "_kill_pid", side_effect=kill):
                        with patch.object(cleanup_mod, "_cleanup_tmux_session", new=AsyncMock(return_value=None)):
                            actions = asyncio.run(cleanup_stale_processes(config))
        assert len(actions) == 1
        assert actions[0].killed is False
        assert actions[0].pid == fake_ancestor

    def test_tmux_cleanup_reported(self, config: ScannerConfig) -> None:
        from mindfulness_nf.models import CleanupAction

        tmux_action = CleanupAction(
            target="tmux session 'murfi_scan'",
            pid=None,
            killed=True,
            message="tmux session killed",
        )

        with patch.object(cleanup_mod, "_pids_on_port", new=AsyncMock(return_value=())):
            with patch.object(cleanup_mod, "_pids_by_pattern", new=AsyncMock(return_value=())):
                with patch.object(
                    cleanup_mod,
                    "_cleanup_tmux_session",
                    new=AsyncMock(return_value=tmux_action),
                ):
                    actions = asyncio.run(cleanup_stale_processes(config))
        assert len(actions) == 1
        assert actions[0].target == "tmux session 'murfi_scan'"
        assert actions[0].pid is None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _no_sleep(_seconds: float) -> None:  # pragma: no cover — trivial
    return None
