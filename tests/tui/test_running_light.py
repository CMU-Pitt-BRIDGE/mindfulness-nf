"""Operator instructions shown in the status light for a running step."""

from __future__ import annotations

import pytest

from mindfulness_nf.models import Color, StepState, StepStatus
from mindfulness_nf.sessions import LOC3, RT15
from mindfulness_nf.tui.screens.session import running_light

FEEDBACK_1 = RT15[2]
REST_1 = LOC3[1]


def _running(config, **kw) -> StepState:
    return StepState(config=config, status=StepStatus.RUNNING, **kw)


def test_gate_with_no_volumes_tells_operator_to_open_psychopy_first() -> None:
    light = running_light(_running(FEEDBACK_1, phase="murfi", awaiting_advance=True))
    assert light.color is Color.YELLOW
    assert "press D to open PsychoPy" in light.message
    assert "Do NOT start the scan yet" in (light.detail or "")


def test_volumes_at_gate_flag_scan_started_too_early() -> None:
    light = running_light(
        _running(FEEDBACK_1, phase="murfi", awaiting_advance=True, progress_current=12)
    )
    assert light.color is Color.RED
    assert "scan started before PsychoPy" in light.message
    assert "press I" in (light.detail or "")


def test_psychopy_open_without_volumes_says_start_scan() -> None:
    light = running_light(
        _running(FEEDBACK_1, phase="psychopy", detail_message="PsychoPy running")
    )
    assert light.color is Color.YELLOW
    assert "press SPACE through the instructions" in light.message


@pytest.mark.parametrize("step", [FEEDBACK_1, REST_1])
def test_receiving_volumes_is_green_with_count(step) -> None:
    light = running_light(_running(step, phase="psychopy", progress_current=40))
    assert light.color is Color.GREEN
    assert light.detail is not None and light.detail.startswith(
        f"40/{step.progress_target} volumes"
    )


def test_rest_without_volumes_points_at_realtime_export() -> None:
    light = running_light(_running(REST_1))
    assert light.color is Color.YELLOW
    assert "MURFI is listening" in light.message
    assert "vSend" in (light.detail or "")


def test_psychopy_crash_is_red() -> None:
    light = running_light(
        _running(
            FEEDBACK_1,
            phase="psychopy",
            progress_current=30,
            detail_message="PsychoPy crashed (rc=1) — press P to relaunch",
        )
    )
    assert light.color is Color.RED
    assert "press P" in (light.detail or "")


def test_psychopy_without_focus_tells_operator_to_click_it() -> None:
    from mindfulness_nf.orchestration.executors.nf_run import PSYCHOPY_NOT_FOCUSED

    light = running_light(
        _running(FEEDBACK_1, phase="psychopy", detail_message=PSYCHOPY_NOT_FOCUSED)
    )
    assert light.color is Color.YELLOW
    assert "click the participant screen" in light.message
