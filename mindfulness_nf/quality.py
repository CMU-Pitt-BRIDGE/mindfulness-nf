"""Pure quality-assessment functions returning TrafficLight values.

All functions are pure: data in, TrafficLight out. No I/O.
No I/O imports permitted in this module (FCIS boundary).
"""

from __future__ import annotations

from mindfulness_nf.models import Color, TrafficLight


def assess_volume_count(received: int, expected: int) -> TrafficLight:
    """Assess volume count quality against expected thresholds.

    Thresholds by expected volume count:
      - expected=20  (2vol):     green >= 18, yellow 1-17, red = 0
      - expected=250 (rest):     green >= 225, yellow 10-224, red < 10
      - expected=150 (feedback): green >= 140, yellow 1-139, red = 0
    """
    if expected == 20:
        if received >= 18:
            return TrafficLight(
                color=Color.GREEN,
                message=f"{received}/{expected} volumes received.",
            )
        if received >= 1:
            return TrafficLight(
                color=Color.YELLOW,
                message=f"{received}/{expected} volumes received. Below expected.",
            )
        return TrafficLight(
            color=Color.RED,
            message=f"0 volumes received. Expected {expected}. Do not proceed. Close this program and report this error.",
        )

    if expected == 250:
        if received >= 225:
            return TrafficLight(
                color=Color.GREEN,
                message=f"{received}/{expected} volumes received.",
            )
        if received >= 10:
            return TrafficLight(
                color=Color.YELLOW,
                message=f"{received}/{expected} volumes received. Below expected.",
            )
        return TrafficLight(
            color=Color.RED,
            message=f"{received} volumes received. Expected {expected}. Do not proceed. Close this program and report this error.",
        )

    if expected == 150:
        if received >= 140:
            return TrafficLight(
                color=Color.GREEN,
                message=f"{received}/{expected} volumes received.",
            )
        if received >= 1:
            return TrafficLight(
                color=Color.YELLOW,
                message=f"{received}/{expected} volumes received. Below expected.",
            )
        return TrafficLight(
            color=Color.RED,
            message=f"0 volumes received. Expected {expected}. Do not proceed. Close this program and report this error.",
        )

    # Fallback for unexpected expected values: use 90% green threshold
    green_threshold = int(expected * 0.9)
    if received >= green_threshold:
        return TrafficLight(
            color=Color.GREEN,
            message=f"{received}/{expected} volumes received.",
        )
    if received >= 1:
        return TrafficLight(
            color=Color.YELLOW,
            message=f"{received}/{expected} volumes received. Below expected.",
        )
    return TrafficLight(
        color=Color.RED,
        message=f"0 volumes received. Expected {expected}. Do not proceed. Close this program and report this error.",
    )


def assess_data_gap(seconds_since_last: float) -> TrafficLight:
    """Assess data gap quality based on seconds since last volume.

    Thresholds:
      - green:  <= 3.0s
      - yellow: > 3.0s and <= 15.0s
      - red:    > 15.0s
    """
    if seconds_since_last <= 3.0:
        return TrafficLight(
            color=Color.GREEN,
            message=f"Last volume {seconds_since_last:.1f}s ago.",
        )
    if seconds_since_last <= 15.0:
        return TrafficLight(
            color=Color.YELLOW,
            message=f"No volume for {seconds_since_last:.1f}s.",
        )
    return TrafficLight(
        color=Color.RED,
        message=f"No volume for {seconds_since_last:.1f}s. Do not proceed. Close this program and report this error.",
    )


def assess_mask(voxel_count: int) -> TrafficLight:
    """Assess ICA mask quality based on voxel count.

    Thresholds:
      - green:  >= 100
      - yellow: 1-99
      - red:    0
    """
    if voxel_count >= 100:
        return TrafficLight(
            color=Color.GREEN,
            message=f"Mask contains {voxel_count} voxels.",
        )
    if voxel_count >= 1:
        return TrafficLight(
            color=Color.YELLOW,
            message=f"Mask contains only {voxel_count} voxels.",
        )
    return TrafficLight(
        color=Color.RED,
        message="Mask contains 0 voxels. Do not proceed. Close this program and report this error.",
    )


def assess_run_selection(
    runs: tuple[int, ...], min_required: int = 2
) -> TrafficLight:
    """Assess whether enough runs are selected for ICA processing.

    Thresholds:
      - green:  len(runs) >= min_required
      - yellow: len(runs) == 1 and min_required > 1
      - red:    len(runs) == 0
    """
    count = len(runs)
    if count >= min_required:
        return TrafficLight(
            color=Color.GREEN,
            message=f"{count} runs selected.",
        )
    if count == 1 and min_required > 1:
        return TrafficLight(
            color=Color.YELLOW,
            message=f"Only 1 run selected. {min_required} recommended.",
        )
    return TrafficLight(
        color=Color.RED,
        message="No runs selected. Select at least 1 run.",
    )
