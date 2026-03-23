"""Tests for mindfulness_nf.quality — functional core, no mocks.

Every threshold boundary is tested with parametrized cases.
"""

from __future__ import annotations

import pytest

from mindfulness_nf.models import Color
from mindfulness_nf.quality import (
    assess_data_gap,
    assess_mask,
    assess_run_selection,
    assess_volume_count,
)


# ---------------------------------------------------------------------------
# assess_volume_count — 2-volume (expected=20)
# ---------------------------------------------------------------------------


class TestAssessVolumeCount2Vol:
    """expected=20: green >= 18, yellow 1-17, red = 0."""

    @pytest.mark.parametrize("received", [18, 19, 20, 25])
    def test_green(self, received: int) -> None:
        result = assess_volume_count(received, 20)
        assert result.color is Color.GREEN

    @pytest.mark.parametrize("received", [1, 2, 10, 17])
    def test_yellow(self, received: int) -> None:
        result = assess_volume_count(received, 20)
        assert result.color is Color.YELLOW

    def test_red_zero(self) -> None:
        result = assess_volume_count(0, 20)
        assert result.color is Color.RED

    def test_boundary_green_at_18(self) -> None:
        assert assess_volume_count(18, 20).color is Color.GREEN

    def test_boundary_yellow_at_17(self) -> None:
        assert assess_volume_count(17, 20).color is Color.YELLOW

    def test_boundary_yellow_at_1(self) -> None:
        assert assess_volume_count(1, 20).color is Color.YELLOW


# ---------------------------------------------------------------------------
# assess_volume_count — resting state (expected=250)
# ---------------------------------------------------------------------------


class TestAssessVolumeCountRest:
    """expected=250: green >= 225, yellow 10-224, red < 10."""

    @pytest.mark.parametrize("received", [225, 226, 250, 300])
    def test_green(self, received: int) -> None:
        result = assess_volume_count(received, 250)
        assert result.color is Color.GREEN

    @pytest.mark.parametrize("received", [10, 11, 100, 224])
    def test_yellow(self, received: int) -> None:
        result = assess_volume_count(received, 250)
        assert result.color is Color.YELLOW

    @pytest.mark.parametrize("received", [0, 1, 5, 9])
    def test_red(self, received: int) -> None:
        result = assess_volume_count(received, 250)
        assert result.color is Color.RED

    def test_boundary_green_at_225(self) -> None:
        assert assess_volume_count(225, 250).color is Color.GREEN

    def test_boundary_yellow_at_224(self) -> None:
        assert assess_volume_count(224, 250).color is Color.YELLOW

    def test_boundary_yellow_at_10(self) -> None:
        assert assess_volume_count(10, 250).color is Color.YELLOW

    def test_boundary_red_at_9(self) -> None:
        assert assess_volume_count(9, 250).color is Color.RED

    def test_boundary_red_at_0(self) -> None:
        assert assess_volume_count(0, 250).color is Color.RED


# ---------------------------------------------------------------------------
# assess_volume_count — feedback (expected=150)
# ---------------------------------------------------------------------------


class TestAssessVolumeCountFeedback:
    """expected=150: green >= 140, yellow 1-139, red = 0."""

    @pytest.mark.parametrize("received", [140, 141, 150, 200])
    def test_green(self, received: int) -> None:
        result = assess_volume_count(received, 150)
        assert result.color is Color.GREEN

    @pytest.mark.parametrize("received", [1, 2, 50, 139])
    def test_yellow(self, received: int) -> None:
        result = assess_volume_count(received, 150)
        assert result.color is Color.YELLOW

    def test_red_zero(self) -> None:
        result = assess_volume_count(0, 150)
        assert result.color is Color.RED

    def test_boundary_green_at_140(self) -> None:
        assert assess_volume_count(140, 150).color is Color.GREEN

    def test_boundary_yellow_at_139(self) -> None:
        assert assess_volume_count(139, 150).color is Color.YELLOW

    def test_boundary_yellow_at_1(self) -> None:
        assert assess_volume_count(1, 150).color is Color.YELLOW


# ---------------------------------------------------------------------------
# assess_volume_count — red messages
# ---------------------------------------------------------------------------


class TestAssessVolumeCountMessages:
    def test_red_message_includes_expected(self) -> None:
        result = assess_volume_count(0, 250)
        assert "250" in result.message
        assert "Do not proceed" in result.message

    def test_green_message_includes_counts(self) -> None:
        result = assess_volume_count(250, 250)
        assert "250/250" in result.message

    def test_yellow_message_includes_counts(self) -> None:
        result = assess_volume_count(100, 250)
        assert "100/250" in result.message


# ---------------------------------------------------------------------------
# assess_data_gap
# ---------------------------------------------------------------------------


class TestAssessDataGap:
    """green <= 3.0, yellow > 3.0 and <= 15.0, red > 15.0."""

    @pytest.mark.parametrize("seconds", [0.0, 1.0, 2.0, 3.0])
    def test_green(self, seconds: float) -> None:
        assert assess_data_gap(seconds).color is Color.GREEN

    @pytest.mark.parametrize("seconds", [3.1, 5.0, 10.0, 15.0])
    def test_yellow(self, seconds: float) -> None:
        assert assess_data_gap(seconds).color is Color.YELLOW

    @pytest.mark.parametrize("seconds", [15.1, 20.0, 60.0])
    def test_red(self, seconds: float) -> None:
        assert assess_data_gap(seconds).color is Color.RED

    def test_boundary_green_at_3(self) -> None:
        assert assess_data_gap(3.0).color is Color.GREEN

    def test_boundary_yellow_at_3_01(self) -> None:
        assert assess_data_gap(3.01).color is Color.YELLOW

    def test_boundary_yellow_at_15(self) -> None:
        assert assess_data_gap(15.0).color is Color.YELLOW

    def test_boundary_red_at_15_01(self) -> None:
        assert assess_data_gap(15.01).color is Color.RED

    def test_green_message(self) -> None:
        result = assess_data_gap(1.5)
        assert "1.5" in result.message

    def test_red_message(self) -> None:
        result = assess_data_gap(20.0)
        assert "Do not proceed" in result.message


# ---------------------------------------------------------------------------
# assess_mask
# ---------------------------------------------------------------------------


class TestAssessMask:
    """green >= 100, yellow 1-99, red = 0."""

    @pytest.mark.parametrize("voxels", [100, 101, 500, 10000])
    def test_green(self, voxels: int) -> None:
        assert assess_mask(voxels).color is Color.GREEN

    @pytest.mark.parametrize("voxels", [1, 2, 50, 99])
    def test_yellow(self, voxels: int) -> None:
        assert assess_mask(voxels).color is Color.YELLOW

    def test_red_zero(self) -> None:
        assert assess_mask(0).color is Color.RED

    def test_boundary_green_at_100(self) -> None:
        assert assess_mask(100).color is Color.GREEN

    def test_boundary_yellow_at_99(self) -> None:
        assert assess_mask(99).color is Color.YELLOW

    def test_boundary_yellow_at_1(self) -> None:
        assert assess_mask(1).color is Color.YELLOW

    def test_red_message(self) -> None:
        result = assess_mask(0)
        assert "0 voxels" in result.message
        assert "Do not proceed" in result.message

    def test_green_message(self) -> None:
        result = assess_mask(500)
        assert "500" in result.message


# ---------------------------------------------------------------------------
# assess_run_selection
# ---------------------------------------------------------------------------


class TestAssessRunSelection:
    """green >= min_required, yellow == 1 (when min > 1), red == 0."""

    def test_green_two_runs(self) -> None:
        assert assess_run_selection((1, 2), min_required=2).color is Color.GREEN

    def test_green_three_runs(self) -> None:
        assert assess_run_selection((1, 2, 3), min_required=2).color is Color.GREEN

    def test_yellow_one_run(self) -> None:
        assert assess_run_selection((1,), min_required=2).color is Color.YELLOW

    def test_red_no_runs(self) -> None:
        assert assess_run_selection((), min_required=2).color is Color.RED

    def test_green_one_run_min_one(self) -> None:
        assert assess_run_selection((1,), min_required=1).color is Color.GREEN

    def test_red_no_runs_min_one(self) -> None:
        assert assess_run_selection((), min_required=1).color is Color.RED

    def test_green_message(self) -> None:
        result = assess_run_selection((1, 2))
        assert "2 runs selected" in result.message

    def test_yellow_message(self) -> None:
        result = assess_run_selection((1,))
        assert "1 run selected" in result.message
        assert "2 recommended" in result.message

    def test_red_message(self) -> None:
        result = assess_run_selection(())
        assert "No runs selected" in result.message

    def test_does_not_block_on_green(self) -> None:
        result = assess_run_selection((1, 2))
        assert result.blocks_advance is False

    def test_does_not_block_on_yellow(self) -> None:
        result = assess_run_selection((1,))
        assert result.blocks_advance is False

    def test_blocks_on_red(self) -> None:
        result = assess_run_selection(())
        assert result.blocks_advance is True
