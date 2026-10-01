"""Monitor selection for PsychoPy (left) and MURFI (right)."""

from __future__ import annotations

import asyncio

from mindfulness_nf.orchestration import displays
from mindfulness_nf.orchestration.displays import Monitor, leftmost, rightmost

# Scanner PC order: the driver lists the right monitor (HDMI-0) first.
RIGHT = Monitor(1920, 0, 1920, 1080)
LEFT = Monitor(0, 0, 1920, 1080)


def test_leftmost_and_rightmost_ignore_driver_order() -> None:
    assert leftmost((RIGHT, LEFT)) == LEFT
    assert rightmost((RIGHT, LEFT)) == RIGHT
    assert leftmost((LEFT, RIGHT)) == LEFT


def test_single_monitor_is_both_left_and_right() -> None:
    assert leftmost((LEFT,)) == rightmost((LEFT,)) == LEFT


def test_center() -> None:
    assert RIGHT.center == (2880, 540)


def test_no_display_returns_false_without_waiting() -> None:
    assert asyncio.run(displays.focus_psychopy(timeout=5)) is False
    assert asyncio.run(displays.move_murfi_right(timeout=5)) is False


def test_covers_detects_fullscreen_window_only() -> None:
    from mindfulness_nf.orchestration.displays import covers

    assert covers(Monitor(0, 0, 1920, 1080), LEFT)
    assert not covers(Monitor(480, 376, 429, 206), LEFT)  # PsychoPy dialog
    assert not covers(Monitor(0, 0, 1920, 1080), RIGHT)


def test_inside_uses_top_left_corner() -> None:
    from mindfulness_nf.orchestration.displays import inside

    assert inside(Monitor(1960, 40, 1200, 800), RIGHT)
    assert not inside(Monitor(400, 69, 1200, 800), RIGHT)
