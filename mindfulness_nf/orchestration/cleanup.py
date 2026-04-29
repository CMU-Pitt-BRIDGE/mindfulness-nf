"""Stale-process cleanup for the Setup step.

Remediation (not detection): kills processes that would block a fresh
session — orphaned DICOM receivers, stale MURFI containers, leftover
tmux sessions. Runs before :mod:`preflight`; the checks that follow
then verify the machine really is clean.

Imperative shell: this module shells out (``fuser``, ``pgrep``, ``tmux``)
and sends signals. Callers must be inside an asyncio event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess

from mindfulness_nf.config import ScannerConfig
from mindfulness_nf.models import CleanupAction

__all__ = [
    "NAME_PATTERNS",
    "cleanup_stale_processes",
]

logger = logging.getLogger(__name__)

# Process-name patterns matched via ``pgrep -f``. Deliberately narrow:
# each pattern is anchored so an unrelated shell that merely *mentions*
# these strings as arguments (e.g. ``bash -c "… murfi.sif …"``) will not
# match — we require the process to actually be *running* the target.
NAME_PATTERNS: tuple[str, ...] = (
    r"python.*murfi/scripts/dicom_receiver\.py",
    r"apptainer.*murfi\.sif",
    r"bash.*murfi/scripts/launch_murfi\.sh",
)

# SIGTERM → poll interval / attempts / final SIGKILL escalation.
_KILL_POLL_INTERVAL = 1.0
_KILL_POLL_ATTEMPTS = 5


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


async def _pids_on_port(port: int) -> tuple[int, ...]:
    """Return PIDs holding ``port/tcp`` according to ``fuser``.

    Absent / unparseable output yields an empty tuple rather than raising —
    port cleanup must be best-effort so one tool missing doesn't take down
    the whole Setup step.
    """
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["fuser", "-n", "tcp", str(port)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("fuser for port %d failed: %s", port, exc)
        return ()

    # fuser writes PIDs to stdout (the "4006/tcp:" header goes to stderr).
    # Each token is a bare PID optionally followed by an access-mode letter
    # (e.g. "1234c"). Strip trailing non-digits, ignore anything else.
    pids: list[int] = []
    for tok in result.stdout.split():
        trimmed = tok.rstrip("cefFrRsuwxymM")
        if trimmed.isdigit():
            pids.append(int(trimmed))
    return tuple(dict.fromkeys(pids))  # preserve order, dedupe


async def _pids_by_pattern(pattern: str) -> tuple[int, ...]:
    """Return PIDs whose cmdline matches ``pattern`` (``pgrep -f``)."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("pgrep for pattern %r failed: %s", pattern, exc)
        return ()

    if result.returncode not in (0, 1):  # 1 = no matches, legitimate
        logger.debug("pgrep %r returned rc=%d stderr=%r", pattern, result.returncode, result.stderr)
    pids: list[int] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return tuple(pids)


# ---------------------------------------------------------------------------
# Kill
# ---------------------------------------------------------------------------


def _ancestry_pids() -> set[int]:
    """Return current PID plus every ancestor up to init.

    Cleanup must never kill a process in its own ancestry — killing the
    shell that launched ``mindfulness-nf`` (or its ``uv`` / ``python``
    chain) would crash the TUI or the operator's terminal session.
    """
    pids: set[int] = {os.getpid()}
    cursor = os.getppid()
    safety = 0  # cycle guard — /proc shouldn't lie, but belt and suspenders
    while cursor > 1 and cursor not in pids and safety < 64:
        pids.add(cursor)
        try:
            with open(f"/proc/{cursor}/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("PPid:"):
                        cursor = int(line.split()[1])
                        break
                else:
                    break
        except (FileNotFoundError, PermissionError, ValueError, OSError):
            break
        safety += 1
    return pids


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but belongs to another user — treat as alive and
        # let SIGTERM surface the failure.
        return True


async def _kill_pid(pid: int) -> tuple[bool, str]:
    """SIGTERM then escalate to SIGKILL. Returns (killed, message)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, "already exited"
    except PermissionError as exc:
        return False, f"permission denied: {exc}"

    for _ in range(_KILL_POLL_ATTEMPTS):
        await asyncio.sleep(_KILL_POLL_INTERVAL)
        if not _alive(pid):
            return True, "SIGTERM"

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, "exited before SIGKILL"
    except PermissionError as exc:
        return False, f"SIGKILL permission denied: {exc}"

    await asyncio.sleep(_KILL_POLL_INTERVAL)
    if _alive(pid):
        return False, "alive after SIGKILL"
    return True, "SIGKILL"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _cleanup_tmux_session(name: str) -> CleanupAction | None:
    """Best-effort ``tmux kill-session -t <name>``. Returns action if killed."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["tmux", "kill-session", "-t", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    if result.returncode == 0:
        return CleanupAction(
            target=f"tmux session {name!r}",
            pid=None,
            killed=True,
            message="tmux session killed",
        )
    return None  # session didn't exist — not an action worth reporting


async def cleanup_stale_processes(
    config: ScannerConfig,
) -> tuple[CleanupAction, ...]:
    """Free ports used by MURFI and kill known orphan processes.

    Discovery runs in two passes:

    1. **Ports** — ``fuser`` on ``vsend_port``, ``infoserver_port``,
       ``dicom_port``. Any PID found is killed and attributed to the port.
    2. **Patterns** — ``pgrep -f`` on :data:`NAME_PATTERNS`. PIDs already
       killed in pass 1 are skipped; others are killed and attributed to
       the pattern that matched.

    The current process (and its parent) is always excluded — if the
    operator invoked ``mindfulness-nf`` from a shell that matches a
    pattern, we do not kill ourselves.

    Returns a tuple of :class:`CleanupAction`, one per PID touched plus
    any tmux sessions killed, in discovery order.
    """
    ports: tuple[tuple[str, int], ...] = (
        ("vsend_port", config.vsend_port),
        ("infoserver_port", config.infoserver_port),
        ("dicom_port", config.dicom_port),
    )

    skip = _ancestry_pids()
    actions: list[CleanupAction] = []
    handled: set[int] = set()

    # Pass 1: ports
    for label, port in ports:
        pids = await _pids_on_port(port)
        for pid in pids:
            if pid in skip:
                actions.append(
                    CleanupAction(
                        target=f"port {port} ({label})",
                        pid=pid,
                        killed=False,
                        message=f"skipped self/ancestor pid {pid}",
                    )
                )
                continue
            if pid in handled:
                continue
            killed, detail = await _kill_pid(pid)
            handled.add(pid)
            actions.append(
                CleanupAction(
                    target=f"port {port} ({label})",
                    pid=pid,
                    killed=killed,
                    message=detail,
                )
            )

    # Pass 2: name patterns (catches orphans that already released their port)
    for pattern in NAME_PATTERNS:
        pids = await _pids_by_pattern(pattern)
        for pid in pids:
            if pid in skip or pid in handled:
                continue
            killed, detail = await _kill_pid(pid)
            handled.add(pid)
            actions.append(
                CleanupAction(
                    target=f"process {pattern}",
                    pid=pid,
                    killed=killed,
                    message=detail,
                )
            )

    # Best-effort tmux session cleanup.
    tmux_action = await _cleanup_tmux_session("murfi_scan")
    if tmux_action is not None:
        actions.append(tmux_action)

    return tuple(actions)
