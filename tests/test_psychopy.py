"""Tests for PsychoPy orchestration module.

Shell tests: mocks only external I/O (subprocess creation).
Scale-factor tests use tmp_path with real CSV files — no mocks.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mindfulness_nf.orchestration.psychopy import (
    get_previous_scale_factor,
    get_scale_factor,
    launch,
    wait,
)

# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

CSV_HEADER = "volume,scale_factor,time,time_plus_1.2,cen,dmn,stage,cen_cumulative_hits,dmn_cumulative_hits,pda_outlier,ball_y_position,top_circle_y_position,bottom_circle_y_position"


def _write_csv(
    path: Path,
    scale_factor: float = 10.0,
    cen_hits: int = 4,
    dmn_hits: int = 4,
    num_rows: int = 150,
) -> None:
    """Write a minimal PsychoPy-style CSV for testing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [CSV_HEADER]
    for i in range(num_rows):
        # Cumulative hits ramp from 0 to the final value
        cen_cum = min(i * cen_hits // max(num_rows - 1, 1), cen_hits)
        dmn_cum = min(i * dmn_hits // max(num_rows - 1, 1), dmn_hits)
        # On the last row, make sure cumulative hits match the target
        if i == num_rows - 1:
            cen_cum = cen_hits
            dmn_cum = dmn_hits
        lines.append(
            f"{i},{scale_factor},{i * 1.2},{(i + 1) * 1.2},0.5,0.3,feedback,{cen_cum},{dmn_cum},0,100,200,-200"
        )
    path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# launch() tests
# ---------------------------------------------------------------------------


class TestLaunch:
    """Test that launch() constructs correct command args."""

    @pytest.mark.asyncio
    async def test_launch_feedback(self, tmp_path: Path) -> None:
        """launch with feedback=True passes 'Feedback' and correct args."""
        mock_process = AsyncMock()
        mock_process.returncode = None

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            proc = await launch(
                subject="sub-001",
                run_number=2,
                feedback=True,
                duration="15min",
                anchor="peace calm",
                psychopy_dir=tmp_path,
            )

            mock_exec.assert_called_once_with(
                sys.executable,
                "rt-network_feedback.py",
                "sub-001",
                "2",
                "Feedback",
                "15min",
                "peace",
                "calm",
                cwd=str(tmp_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc is mock_process

    @pytest.mark.asyncio
    async def test_launch_no_feedback(self, tmp_path: Path) -> None:
        """launch with feedback=False passes 'No Feedback'."""
        mock_process = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await launch(
                subject="sub-002",
                run_number=1,
                feedback=False,
                duration="30min",
                psychopy_dir=tmp_path,
            )

            args, kwargs = mock_exec.call_args
            assert args[4] == "No Feedback"
            assert args[5] == "30min"
            # No anchor words appended
            assert len(args) == 6

    @pytest.mark.asyncio
    async def test_launch_empty_anchor_excluded(self, tmp_path: Path) -> None:
        """Empty anchor string does not add extra args."""
        mock_process = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await launch(
                subject="sub-003",
                run_number=3,
                feedback=True,
                anchor="",
                psychopy_dir=tmp_path,
            )

            args, _kwargs = mock_exec.call_args
            # Should be: python, script, subject, run, Feedback, 15min
            assert len(args) == 6

    @pytest.mark.asyncio
    async def test_launch_default_psychopy_dir(self) -> None:
        """When psychopy_dir is None, cwd points to project's psychopy/balltask/."""
        mock_process = AsyncMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_process) as mock_exec:
            await launch(subject="sub-001", run_number=1, feedback=True)

            _args, kwargs = mock_exec.call_args
            cwd = Path(kwargs["cwd"])
            assert cwd.name == "balltask"
            assert cwd.parent.name == "psychopy"


# ---------------------------------------------------------------------------
# wait() tests
# ---------------------------------------------------------------------------


class TestWait:
    """Test that wait() returns exit code and handles cancellation."""

    @pytest.mark.asyncio
    async def test_wait_returns_exit_code(self) -> None:
        mock_process = AsyncMock()
        mock_process.returncode = 0
        mock_process.wait = AsyncMock(return_value=0)

        code = await wait(mock_process)
        assert code == 0
        mock_process.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wait_nonzero_exit_code(self) -> None:
        mock_process = AsyncMock()
        mock_process.returncode = 1
        mock_process.wait = AsyncMock(return_value=1)

        code = await wait(mock_process)
        assert code == 1

    @pytest.mark.asyncio
    async def test_wait_cancellation_terminates_process(self) -> None:
        """CancelledError is re-raised after terminating the process."""
        mock_process = AsyncMock()
        mock_process.returncode = -15
        # terminate() is synchronous on a real Process, so use a regular Mock
        mock_process.terminate = Mock()

        call_count = 0

        async def _wait_side_effect() -> int:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise asyncio.CancelledError()
            return -15

        mock_process.wait = AsyncMock(side_effect=_wait_side_effect)

        with pytest.raises(asyncio.CancelledError):
            await wait(mock_process)

        mock_process.terminate.assert_called_once()


# ---------------------------------------------------------------------------
# get_scale_factor() tests — pure function with real CSV files
# ---------------------------------------------------------------------------


class TestGetScaleFactor:
    """Test adaptive scale-factor computation from CSV data."""

    def test_missing_csv_returns_default(self, tmp_path: Path) -> None:
        """When CSV does not exist, return the default scale factor."""
        result = get_scale_factor(tmp_path, "sub-001", previous_run=1)
        assert result == 10.0

    def test_empty_csv_returns_default(self, tmp_path: Path) -> None:
        """When CSV has only a header, return the default."""
        csv_path = tmp_path / "sub-001" / "run1.csv"
        csv_path.parent.mkdir(parents=True)
        csv_path.write_text(CSV_HEADER + "\n")

        result = get_scale_factor(tmp_path, "sub-001", previous_run=1)
        assert result == 10.0

    def test_hits_in_range_keeps_scale_factor(self, tmp_path: Path) -> None:
        """When hits are within [min_hits, max_hits], scale factor is unchanged."""
        csv_path = tmp_path / "sub-001" / "run1.csv"
        _write_csv(csv_path, scale_factor=10.0, cen_hits=2, dmn_hits=2)

        result = get_scale_factor(
            tmp_path, "sub-001", previous_run=1,
            min_hits=3, max_hits=5,
        )
        # cen_hits + dmn_hits = 4 >= min_hits=3, and neither exceeds max_hits=5
        assert result == 10.0

    def test_below_min_hits_increases_scale(self, tmp_path: Path) -> None:
        """When total hits < min_hits, scale factor is increased."""
        csv_path = tmp_path / "sub-001" / "run1.csv"
        _write_csv(csv_path, scale_factor=10.0, cen_hits=1, dmn_hits=0)

        result = get_scale_factor(
            tmp_path, "sub-001", previous_run=1,
            min_hits=3, max_hits=5,
            increase=1.25,
        )
        # total = 1 < min_hits=3 → increase
        assert result == 10.0 * 1.25

    def test_above_max_hits_decreases_scale(self, tmp_path: Path) -> None:
        """When either hit count > max_hits, scale factor is decreased."""
        csv_path = tmp_path / "sub-001" / "run1.csv"
        _write_csv(csv_path, scale_factor=10.0, cen_hits=6, dmn_hits=2)

        result = get_scale_factor(
            tmp_path, "sub-001", previous_run=1,
            min_hits=3, max_hits=5,
            decrease=0.75,
        )
        # cen_hits=6 > max_hits=5 → decrease
        assert result == 10.0 * 0.75

    def test_dmn_above_max_hits_decreases_scale(self, tmp_path: Path) -> None:
        """DMN hits exceeding max also triggers decrease."""
        csv_path = tmp_path / "sub-001" / "run1.csv"
        _write_csv(csv_path, scale_factor=8.0, cen_hits=2, dmn_hits=7)

        result = get_scale_factor(
            tmp_path, "sub-001", previous_run=1,
            min_hits=3, max_hits=5,
            decrease=0.75,
        )
        assert result == 8.0 * 0.75

    def test_custom_default(self, tmp_path: Path) -> None:
        """Custom default is returned when CSV is missing."""
        result = get_scale_factor(tmp_path, "sub-001", previous_run=1, default=20.0)
        assert result == 20.0

    def test_uses_previous_scale_factor_from_csv(self, tmp_path: Path) -> None:
        """The adjustment is applied to the scale_factor from the CSV, not the default."""
        csv_path = tmp_path / "sub-001" / "run2.csv"
        _write_csv(csv_path, scale_factor=5.0, cen_hits=0, dmn_hits=0)

        result = get_scale_factor(
            tmp_path, "sub-001", previous_run=2,
            default=10.0, min_hits=3, increase=1.25,
        )
        # total = 0 < min_hits=3 → increase from 5.0, not default 10.0
        assert result == 5.0 * 1.25

    def test_malformed_csv_returns_default(self, tmp_path: Path) -> None:
        """CSV with missing columns returns default."""
        csv_path = tmp_path / "sub-001" / "run1.csv"
        csv_path.parent.mkdir(parents=True)
        csv_path.write_text("volume,time\n1,0.0\n")

        result = get_scale_factor(tmp_path, "sub-001", previous_run=1)
        assert result == 10.0


# ---------------------------------------------------------------------------
# get_previous_scale_factor() tests
# ---------------------------------------------------------------------------


class TestGetPreviousScaleFactor:
    """Test reading the scale_factor column from a previous run."""

    def test_returns_scale_factor_from_csv(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "sub-001" / "run1.csv"
        _write_csv(csv_path, scale_factor=7.5)

        result = get_previous_scale_factor(tmp_path, "sub-001", previous_run=1)
        assert result == 7.5

    def test_missing_csv_returns_default(self, tmp_path: Path) -> None:
        result = get_previous_scale_factor(tmp_path, "sub-001", previous_run=1)
        assert result == 10.0

    def test_custom_default(self, tmp_path: Path) -> None:
        result = get_previous_scale_factor(
            tmp_path, "sub-001", previous_run=1, default=20.0
        )
        assert result == 20.0

    def test_empty_csv_returns_default(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "sub-001" / "run1.csv"
        csv_path.parent.mkdir(parents=True)
        csv_path.write_text(CSV_HEADER + "\n")

        result = get_previous_scale_factor(tmp_path, "sub-001", previous_run=1)
        assert result == 10.0

    def test_malformed_csv_returns_default(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "sub-001" / "run1.csv"
        csv_path.parent.mkdir(parents=True)
        csv_path.write_text("volume,time\n1,0.0\n")

        result = get_previous_scale_factor(tmp_path, "sub-001", previous_run=1)
        assert result == 10.0
