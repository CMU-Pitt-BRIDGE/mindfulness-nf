"""MURFI container lifecycle management.

Launches, monitors, and stops the MURFI real-time fMRI analysis container
via Apptainer.  All blocking subprocess work is run in asyncio threads;
callers should ``await`` every public coroutine.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import TrafficLight
from mindfulness_nf.quality import assess_data_gap, assess_volume_count

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_RECEIVED_RE = re.compile(r"received image from scanner")

# XML files that should have MoCo toggling applied.
_MOCO_XML_NAMES = frozenset({"2vol.xml", "rtdmn.xml"})


@dataclass
class MurfiProcess:
    """Handle for a running MURFI subprocess.

    NOT frozen -- it wraps a mutable subprocess handle.
    """

    process: asyncio.subprocess.Process
    log_path: Path
    xml_name: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def start(
    subject_dir: Path,
    xml_name: str,
    config: PipelineConfig,
    *,
    scanner_config: ScannerConfig | None = None,
) -> MurfiProcess:
    """Launch MURFI inside an Apptainer container.

    Parameters
    ----------
    subject_dir:
        Absolute path to the subject directory (e.g. ``…/subjects/sub-001``).
    xml_name:
        Filename of the XML config inside ``subject_dir/xml/`` (e.g. ``rtdmn.xml``).
    config:
        Pipeline configuration (currently unused at launch but reserved for
        future per-run overrides).
    scanner_config:
        Optional scanner configuration.  Defaults to ``ScannerConfig()``.

    Returns
    -------
    MurfiProcess
        Handle that must be passed to :func:`stop` when done.
    """
    sc = scanner_config or ScannerConfig()
    xml_path = subject_dir / "xml" / xml_name
    subjects_dir = subject_dir.parent
    subject_name = subject_dir.name

    log_dir = subject_dir / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    label = xml_name.removesuffix(".xml")
    log_path = log_dir / f"murfi_{label}.log"
    # Truncate any previous log.
    log_path.write_bytes(b"")

    # Build the Apptainer command, mirroring run_session.sh's run_murfi().
    cmd: list[str] = [
        "apptainer",
        "exec",
        "--nv",
        "--cleanenv",
        "--env", f"DISPLAY={os.environ.get('DISPLAY', ':0')}",
        "--env", f"XDG_RUNTIME_DIR=/tmp/runtime-{os.getuid()}",
        "--env", "QT_QPA_PLATFORM=xcb",
        "--env", "NO_AT_BRIDGE=1",
        "--env", "QT_LOGGING_RULES=*.debug=false;*.warning=false",
        "--env", f"MURFI_SUBJECTS_DIR={subjects_dir}/",
        "--env", f"MURFI_SUBJECT_NAME={subject_name}",
        "--bind", f"{subjects_dir}:{subjects_dir}",
        sc.murfi_container,
        "murfi",
        "-f", str(xml_path),
    ]

    log_fh = log_path.open("w")
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=log_fh,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )

    return MurfiProcess(process=process, log_path=log_path, xml_name=xml_name)


async def stop(murfi: MurfiProcess, *, timeout: float = 10.0) -> None:
    """Gracefully shut down a MURFI process (SIGTERM then SIGKILL).

    Always re-raises ``CancelledError`` after cleanup.
    """
    cancelled = False
    try:
        if murfi.process.returncode is not None:
            return  # already exited

        # Send SIGTERM to the process group.
        try:
            os.killpg(os.getpgid(murfi.process.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return

        try:
            await asyncio.wait_for(murfi.process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # Escalate to SIGKILL.
            try:
                os.killpg(os.getpgid(murfi.process.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(murfi.process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        cancelled = True
        # Best-effort kill.
        try:
            murfi.process.kill()
        except (ProcessLookupError, OSError):
            pass
    finally:
        if cancelled:
            raise asyncio.CancelledError


async def tail_log(murfi: MurfiProcess) -> AsyncIterator[str]:
    """Yield log lines as they appear, similar to ``tail -f``.

    Terminates when the process has exited **and** no new bytes remain.
    """
    path = murfi.log_path
    offset = 0
    while True:
        text = await asyncio.to_thread(_read_from, path, offset)
        if text:
            offset += len(text.encode())
            for line in text.splitlines():
                yield line
        elif murfi.process.returncode is not None:
            # Process exited -- drain any remaining bytes.
            text = await asyncio.to_thread(_read_from, path, offset)
            if text:
                for line in text.splitlines():
                    yield line
            return
        else:
            await asyncio.sleep(0.25)


async def count_volumes(murfi: MurfiProcess) -> int:
    """Return the number of ``received image from scanner`` lines in the log."""
    return await asyncio.to_thread(_count_received, murfi.log_path)


def configure_moco(xml_path: Path, use_moco: bool) -> bool:
    """Ensure the ``onlyReadMoCo`` option in *xml_path* matches *use_moco*.

    Only meaningful for ``2vol.xml`` and ``rtdmn.xml`` -- returns ``False``
    immediately for any other filename.

    Returns ``True`` if the file was modified, ``False`` otherwise.
    """
    if xml_path.name not in _MOCO_XML_NAMES:
        return False

    content = xml_path.read_text()
    desired = "true" if use_moco else "false"

    # Match the sed pattern from run_session.sh:
    #   <option name="onlyReadMoCo">  ... </option>
    pattern = r'(<option name="onlyReadMoCo">)[^<]*(</option>)'
    replacement = rf"\g<1>  {desired} \g<2>"

    new_content, subs = re.subn(pattern, replacement, content)
    if subs == 0 or new_content == content:
        return False

    xml_path.write_text(new_content)
    return True


async def monitor_volumes(
    murfi: MurfiProcess,
    expected: int,
    on_update: Callable[[int, TrafficLight], None],
    *,
    poll_interval: float = 0.5,
) -> None:
    """Poll MURFI log for volume count and call *on_update* each iteration.

    Runs until the MURFI process exits.  Each poll calls
    ``on_update(volume_count, traffic_light)`` where the traffic light is
    the *worst* (highest-severity) of the volume-count and data-gap
    assessments.

    Parameters
    ----------
    murfi:
        Running MURFI process handle.
    expected:
        Number of volumes the run should acquire.
    on_update:
        Callback invoked every *poll_interval* seconds.
    poll_interval:
        Seconds between polls (default 0.5).
    """
    last_count = 0
    last_time = _loop_time()

    while murfi.process.returncode is None:
        current_count = await count_volumes(murfi)
        now = _loop_time()

        if current_count > last_count:
            last_time = now
            last_count = current_count

        seconds_since = now - last_time
        tl_volume = assess_volume_count(current_count, expected)
        tl_gap = assess_data_gap(seconds_since)

        # Use the worst (most severe) traffic light.
        worst = _worst_light(tl_volume, tl_gap)
        on_update(current_count, worst)

        await asyncio.sleep(poll_interval)

    # Final update after exit.
    current_count = await count_volumes(murfi)
    tl_volume = assess_volume_count(current_count, expected)
    on_update(current_count, tl_volume)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_COLOR_SEVERITY = {"green": 0, "yellow": 1, "red": 2}


def _worst_light(a: TrafficLight, b: TrafficLight) -> TrafficLight:
    """Return the more severe of two traffic lights."""
    if _COLOR_SEVERITY[b.color.value] > _COLOR_SEVERITY[a.color.value]:
        return b
    return a


def _read_from(path: Path, offset: int) -> str:
    """Read *path* from byte *offset* and return the text (or ``""``).

    Runs in a thread via ``asyncio.to_thread``.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            data = fh.read()
        return data.decode(errors="replace")
    except FileNotFoundError:
        return ""


def _count_received(path: Path) -> int:
    """Count ``received image from scanner`` lines in *path*."""
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return 0
    return len(_RECEIVED_RE.findall(text))


def _loop_time() -> float:
    """Monotonic clock, mockable in tests."""
    return asyncio.get_event_loop().time()
