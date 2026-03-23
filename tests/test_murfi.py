"""Tests for mindfulness_nf.orchestration.murfi -- MURFI container lifecycle."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import Color, TrafficLight
from mindfulness_nf.orchestration.murfi import (
    MurfiProcess,
    configure_moco,
    count_volumes,
    monitor_volumes,
    start,
    stop,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_murfi_process(
    tmp_path: Path,
    *,
    log_content: str = "",
    xml_name: str = "rtdmn.xml",
    returncode: int | None = None,
) -> MurfiProcess:
    """Build a MurfiProcess with a mock subprocess and a real log file."""
    log_path = tmp_path / "log" / f"murfi_{xml_name.removesuffix('.xml')}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log_content)

    proc = AsyncMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode
    proc.pid = 12345
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()

    return MurfiProcess(process=proc, log_path=log_path, xml_name=xml_name)


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------


class TestStart:
    """Tests for the start() coroutine."""

    @pytest.mark.asyncio
    async def test_start_returns_murfi_process(self, tmp_path: Path) -> None:
        subject_dir = tmp_path / "subjects" / "sub-001"
        subject_dir.mkdir(parents=True)
        xml_dir = subject_dir / "xml"
        xml_dir.mkdir()
        (xml_dir / "rtdmn.xml").write_text("<scanner/>")

        fake_proc = AsyncMock(spec=asyncio.subprocess.Process)
        fake_proc.returncode = None
        fake_proc.pid = 99

        with patch(
            "mindfulness_nf.orchestration.murfi.asyncio.create_subprocess_exec",
            return_value=fake_proc,
        ) as mock_exec:
            result = await start(
                subject_dir, "rtdmn.xml", PipelineConfig()
            )

        assert isinstance(result, MurfiProcess)
        assert result.xml_name == "rtdmn.xml"
        assert result.log_path.exists()
        assert result.process is fake_proc

        # Verify apptainer was invoked.
        call_args = mock_exec.call_args
        assert "apptainer" in call_args.args[0]

    @pytest.mark.asyncio
    async def test_start_passes_bind_mounts(self, tmp_path: Path) -> None:
        subject_dir = tmp_path / "subjects" / "sub-002"
        subject_dir.mkdir(parents=True)
        (subject_dir / "xml").mkdir()
        (subject_dir / "xml" / "2vol.xml").write_text("<scanner/>")

        fake_proc = AsyncMock(spec=asyncio.subprocess.Process)
        fake_proc.returncode = None
        fake_proc.pid = 100

        with patch(
            "mindfulness_nf.orchestration.murfi.asyncio.create_subprocess_exec",
            return_value=fake_proc,
        ) as mock_exec:
            await start(subject_dir, "2vol.xml", PipelineConfig())

        cmd = mock_exec.call_args.args
        # Find --bind argument
        bind_idx = list(cmd).index("--bind")
        bind_val = cmd[bind_idx + 1]
        subjects_dir = str(subject_dir.parent)
        assert bind_val == f"{subjects_dir}:{subjects_dir}"

    @pytest.mark.asyncio
    async def test_start_sets_env_vars(self, tmp_path: Path) -> None:
        subject_dir = tmp_path / "subjects" / "sub-003"
        subject_dir.mkdir(parents=True)
        (subject_dir / "xml").mkdir()
        (subject_dir / "xml" / "rtdmn.xml").write_text("<scanner/>")

        fake_proc = AsyncMock(spec=asyncio.subprocess.Process)
        fake_proc.returncode = None
        fake_proc.pid = 101

        with patch(
            "mindfulness_nf.orchestration.murfi.asyncio.create_subprocess_exec",
            return_value=fake_proc,
        ) as mock_exec:
            await start(subject_dir, "rtdmn.xml", PipelineConfig())

        cmd = list(mock_exec.call_args.args)
        # Check MURFI_SUBJECT_NAME env
        env_args = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--env"]
        subject_name_envs = [e for e in env_args if e.startswith("MURFI_SUBJECT_NAME=")]
        assert len(subject_name_envs) == 1
        assert subject_name_envs[0] == "MURFI_SUBJECT_NAME=sub-003"


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


class TestStop:
    """Tests for the stop() coroutine."""

    @pytest.mark.asyncio
    async def test_stop_already_exited(self, tmp_path: Path) -> None:
        mp = _make_murfi_process(tmp_path, returncode=0)
        await stop(mp)  # should not raise

    @pytest.mark.asyncio
    async def test_stop_sends_sigterm(self, tmp_path: Path) -> None:
        mp = _make_murfi_process(tmp_path, returncode=None)
        # After stop sends SIGTERM, the process exits.
        mp.process.wait = AsyncMock(return_value=0)

        with patch("mindfulness_nf.orchestration.murfi.os.killpg") as mock_killpg, \
             patch("mindfulness_nf.orchestration.murfi.os.getpgid", return_value=12345):
            await stop(mp)

        # First call should be SIGTERM.
        import signal
        mock_killpg.assert_any_call(12345, signal.SIGTERM)

    @pytest.mark.asyncio
    async def test_stop_escalates_to_sigkill(self, tmp_path: Path) -> None:
        mp = _make_murfi_process(tmp_path, returncode=None)
        # Simulate process that doesn't respond to SIGTERM.
        mp.process.wait = AsyncMock(side_effect=asyncio.TimeoutError)

        with patch("mindfulness_nf.orchestration.murfi.os.killpg") as mock_killpg, \
             patch("mindfulness_nf.orchestration.murfi.os.getpgid", return_value=12345):
            await stop(mp, timeout=0.1)

        import signal
        calls = [c.args for c in mock_killpg.call_args_list]
        assert (12345, signal.SIGTERM) in calls
        assert (12345, signal.SIGKILL) in calls

    @pytest.mark.asyncio
    async def test_stop_reraises_cancelled(self, tmp_path: Path) -> None:
        mp = _make_murfi_process(tmp_path, returncode=None)

        with patch("mindfulness_nf.orchestration.murfi.os.killpg", side_effect=asyncio.CancelledError), \
             patch("mindfulness_nf.orchestration.murfi.os.getpgid", return_value=12345):
            with pytest.raises(asyncio.CancelledError):
                await stop(mp)


# ---------------------------------------------------------------------------
# configure_moco()
# ---------------------------------------------------------------------------


class TestConfigureMoco:
    """Tests for configure_moco() with real temp files."""

    def test_enable_moco_when_false(self, tmp_path: Path) -> None:
        xml_path = tmp_path / "2vol.xml"
        xml_path.write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <scanner>
              <option name="onlyReadMoCo">  false </option>
            </scanner>
        """))

        changed = configure_moco(xml_path, use_moco=True)

        assert changed is True
        content = xml_path.read_text()
        assert '<option name="onlyReadMoCo">  true </option>' in content

    def test_disable_moco_when_true(self, tmp_path: Path) -> None:
        xml_path = tmp_path / "rtdmn.xml"
        xml_path.write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <scanner>
              <option name="onlyReadMoCo">  true </option>
            </scanner>
        """))

        changed = configure_moco(xml_path, use_moco=False)

        assert changed is True
        content = xml_path.read_text()
        assert '<option name="onlyReadMoCo">  false </option>' in content

    def test_no_change_when_already_correct(self, tmp_path: Path) -> None:
        xml_path = tmp_path / "2vol.xml"
        xml_path.write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <scanner>
              <option name="onlyReadMoCo">  true </option>
            </scanner>
        """))

        changed = configure_moco(xml_path, use_moco=True)

        assert changed is False

    def test_skips_rest_xml(self, tmp_path: Path) -> None:
        xml_path = tmp_path / "rest.xml"
        xml_path.write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <scanner>
              <option name="onlyReadMoCo">  false </option>
            </scanner>
        """))

        changed = configure_moco(xml_path, use_moco=True)

        assert changed is False
        # Content should be untouched.
        assert "false" in xml_path.read_text()

    def test_handles_arbitrary_whitespace(self, tmp_path: Path) -> None:
        xml_path = tmp_path / "rtdmn.xml"
        xml_path.write_text(
            '<option name="onlyReadMoCo">   false   </option>'
        )

        changed = configure_moco(xml_path, use_moco=True)

        assert changed is True
        content = xml_path.read_text()
        assert '<option name="onlyReadMoCo">  true </option>' in content

    def test_real_xml_from_template(self, tmp_path: Path) -> None:
        """Test with realistic XML matching the actual template files."""
        xml_path = tmp_path / "2vol.xml"
        xml_path.write_text(textwrap.dedent("""\
            <?xml version="1.0" encoding="UTF-8"?>
            <study name="rtDMN">
                <option name="softwareDir"> /opt/murfi/ </option>
            </study>

            <scanner>
              <option name="disabled">     false </option>
              <option name="tr">             1.2 </option>
              <option name="measurements">   20 </option>
              <option name="port">           50000 </option>
              <option name="saveImages">     true </option>
              <option name="receiveImages">  true </option>
              <option name="onlyReadMoCo">  true </option>
            </scanner>
        """))

        changed = configure_moco(xml_path, use_moco=False)

        assert changed is True
        content = xml_path.read_text()
        assert '<option name="onlyReadMoCo">  false </option>' in content
        # Other options should be untouched.
        assert '<option name="tr">             1.2 </option>' in content


# ---------------------------------------------------------------------------
# count_volumes()
# ---------------------------------------------------------------------------


class TestCountVolumes:
    """Tests for count_volumes() with sample log content."""

    @pytest.mark.asyncio
    async def test_count_zero(self, tmp_path: Path) -> None:
        mp = _make_murfi_process(tmp_path, log_content="starting murfi...\nready.\n")
        assert await count_volumes(mp) == 0

    @pytest.mark.asyncio
    async def test_count_several(self, tmp_path: Path) -> None:
        log = "\n".join(
            [
                "starting murfi...",
                "received image from scanner: series 2 acquisition 1",
                "processing volume 1",
                "received image from scanner: series 2 acquisition 2",
                "received image from scanner: series 2 acquisition 3",
                "done.",
            ]
        )
        mp = _make_murfi_process(tmp_path, log_content=log)
        assert await count_volumes(mp) == 3

    @pytest.mark.asyncio
    async def test_count_with_missing_log(self, tmp_path: Path) -> None:
        mp = _make_murfi_process(tmp_path, log_content="")
        mp.log_path.unlink()
        assert await count_volumes(mp) == 0

    @pytest.mark.asyncio
    async def test_count_large(self, tmp_path: Path) -> None:
        lines = [
            f"received image from scanner: series 2 acquisition {i}"
            for i in range(1, 151)
        ]
        mp = _make_murfi_process(tmp_path, log_content="\n".join(lines))
        assert await count_volumes(mp) == 150


# ---------------------------------------------------------------------------
# monitor_volumes()
# ---------------------------------------------------------------------------


class TestMonitorVolumes:
    """Tests for monitor_volumes() polling callback."""

    @pytest.mark.asyncio
    async def test_calls_on_update_with_traffic_light(self, tmp_path: Path) -> None:
        """Monitor should call on_update and eventually stop when process exits."""
        log = "\n".join(
            [
                "received image from scanner: series 2 acquisition 1",
                "received image from scanner: series 2 acquisition 2",
            ]
        )
        mp = _make_murfi_process(tmp_path, log_content=log, returncode=None)

        updates: list[tuple[int, TrafficLight]] = []

        def on_update(count: int, tl: TrafficLight) -> None:
            updates.append((count, tl))

        # Make process exit after first sleep.
        original_sleep = asyncio.sleep
        call_count = 0

        async def fake_sleep(duration: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                mp.process.returncode = 0
            await original_sleep(0)

        with patch("mindfulness_nf.orchestration.murfi.asyncio.sleep", side_effect=fake_sleep), \
             patch("mindfulness_nf.orchestration.murfi._loop_time", return_value=0.0):
            await monitor_volumes(mp, expected=20, on_update=on_update)

        # Should have at least one update from the loop + the final update.
        assert len(updates) >= 2
        # Final update with 2 volumes.
        final_count, final_tl = updates[-1]
        assert final_count == 2
        assert isinstance(final_tl, TrafficLight)

    @pytest.mark.asyncio
    async def test_green_when_enough_volumes(self, tmp_path: Path) -> None:
        """When all expected volumes arrive, traffic light should be green."""
        lines = [
            f"received image from scanner: series 2 acquisition {i}"
            for i in range(1, 21)
        ]
        mp = _make_murfi_process(
            tmp_path, log_content="\n".join(lines), returncode=None
        )

        updates: list[tuple[int, TrafficLight]] = []

        def on_update(count: int, tl: TrafficLight) -> None:
            updates.append((count, tl))

        # Exit immediately.
        async def fake_sleep(duration: float) -> None:
            mp.process.returncode = 0

        with patch("mindfulness_nf.orchestration.murfi.asyncio.sleep", side_effect=fake_sleep), \
             patch("mindfulness_nf.orchestration.murfi._loop_time", return_value=0.0):
            await monitor_volumes(mp, expected=20, on_update=on_update)

        # Final update should be green (20/20).
        final_count, final_tl = updates[-1]
        assert final_count == 20
        assert final_tl.color == Color.GREEN

    @pytest.mark.asyncio
    async def test_red_when_zero_volumes(self, tmp_path: Path) -> None:
        """Zero volumes with long gap should produce red."""
        mp = _make_murfi_process(tmp_path, log_content="starting...\n", returncode=None)

        updates: list[tuple[int, TrafficLight]] = []

        def on_update(count: int, tl: TrafficLight) -> None:
            updates.append((count, tl))

        time_counter = [0.0]

        def mock_time() -> float:
            time_counter[0] += 20.0  # 20 seconds between polls -> red gap
            return time_counter[0]

        async def fake_sleep(duration: float) -> None:
            mp.process.returncode = 0

        with patch("mindfulness_nf.orchestration.murfi.asyncio.sleep", side_effect=fake_sleep), \
             patch("mindfulness_nf.orchestration.murfi._loop_time", side_effect=mock_time):
            await monitor_volumes(mp, expected=20, on_update=on_update)

        # At least one update should be red due to zero volumes.
        assert any(tl.color == Color.RED for _, tl in updates)

    @pytest.mark.asyncio
    async def test_gap_causes_yellow(self, tmp_path: Path) -> None:
        """Data gap >3s but <=15s should produce yellow."""
        mp = _make_murfi_process(
            tmp_path,
            log_content="received image from scanner: series 2 acquisition 1\n",
            returncode=None,
        )

        updates: list[tuple[int, TrafficLight]] = []

        def on_update(count: int, tl: TrafficLight) -> None:
            updates.append((count, tl))

        time_values = [0.0, 10.0]  # First call: 0s, second: 10s gap
        time_idx = [0]

        def mock_time() -> float:
            val = time_values[min(time_idx[0], len(time_values) - 1)]
            time_idx[0] += 1
            return val

        call_count = [0]

        async def fake_sleep(duration: float) -> None:
            call_count[0] += 1
            if call_count[0] >= 1:
                mp.process.returncode = 0

        with patch("mindfulness_nf.orchestration.murfi.asyncio.sleep", side_effect=fake_sleep), \
             patch("mindfulness_nf.orchestration.murfi._loop_time", side_effect=mock_time):
            await monitor_volumes(mp, expected=20, on_update=on_update)

        # The loop iteration with 10s gap should produce yellow.
        colors = [tl.color for _, tl in updates]
        assert Color.YELLOW in colors


# ---------------------------------------------------------------------------
# MurfiProcess dataclass
# ---------------------------------------------------------------------------


class TestMurfiProcess:
    """Tests for the MurfiProcess dataclass."""

    def test_is_mutable(self, tmp_path: Path) -> None:
        """MurfiProcess should NOT be frozen."""
        mp = _make_murfi_process(tmp_path)
        # Should be able to reassign fields.
        mp.xml_name = "2vol.xml"
        assert mp.xml_name == "2vol.xml"

    def test_fields(self, tmp_path: Path) -> None:
        mp = _make_murfi_process(tmp_path, xml_name="2vol.xml")
        assert mp.xml_name == "2vol.xml"
        assert isinstance(mp.log_path, Path)
        assert mp.process is not None
