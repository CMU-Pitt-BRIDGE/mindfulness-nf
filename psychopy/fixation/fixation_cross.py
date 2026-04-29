"""Display a Thaler ABC optimal fixation target on the BOLDscreen.

Reference:
    Thaler L, Schütz AC, Goodale MA, Gegenfurtner KR (2013).
    "What is the best fixation target?" Vision Research 76:31-42.
    https://doi.org/10.1016/j.visres.2012.10.012

Calibration is for the Pitt BOLDscreen setup:
    screen = 69.84 x 39.29 cm, 1920 x 1080 px, viewed at 139 cm.
Change `BOLDscreen_139cm` parameters below if the hardware or viewing
distance changes.

Usage:
    # Double-click FixationCross.desktop (preferred), or:
    .venv/bin/python psychopy/fixation/fixation_cross.py           # on screen 1, indefinite
    .venv/bin/python psychopy/fixation/fixation_cross.py -d 600    # run for 10 min
    .venv/bin/python psychopy/fixation/fixation_cross.py --windowed  # for debugging

Press ESC or Q to exit.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# --- Error logging (captures crashes when launched from .desktop icon) --------
_LOG_FILE = Path.home() / ".fixation_cross.log"
logging.basicConfig(
    filename=str(_LOG_FILE),
    filemode="a",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

# --- Display calibration -------------------------------------------------------
MONITOR_NAME = "BOLDscreen_139cm"
SCREEN_WIDTH_CM = 69.84
SCREEN_HEIGHT_CM = 39.29
SCREEN_WIDTH_PX = 1920
SCREEN_HEIGHT_PX = 1080
VIEWING_DISTANCE_CM = 139.0

# --- Thaler ABC target (deg of visual angle) ----------------------------------
# Thaler et al. 2013 specify outer RADIUS = 0.6°, so outer DIAMETER = 1.2°.
# Inner disk and crosshair preserve Thaler's 3:1 and 6:1 ratios.
OUTER_DEG = 1.2   # outer disk diameter
INNER_DEG = 0.4   # inner disk diameter
CROSS_DEG = 0.2   # crosshair thickness

# --- Colors in PsychoPy rgb space (-1..1) -------------------------------------
# Using rgb (not rgb255) because shape stims with colorSpace kwarg via **dict
# can silently fall back to default rgb interpretation, rendering (0,0,0)
# rgb255 as mid-gray. The rgb space has unambiguous three-valued semantics:
#   -1 = black, 0 = mid-gray (same as PPT 128,128,128), +1 = white.
BG_GRAY = (0, 0, 0)       # mid-gray
FG_BLACK = (-1, -1, -1)   # black


def ensure_monitor():
    """Register/update the BOLDscreen monitor in PsychoPy's monitor store.

    Writes ~/.psychopy3/monitors/BOLDscreen_139cm.json so every subsequent
    launch (and any other script that names this monitor) gets the same
    geometry. Running this on every launch keeps the JSON in sync with
    the constants above — the code is the source of truth.
    """
    from psychopy import monitors
    mon = monitors.Monitor(MONITOR_NAME)
    mon.setWidth(SCREEN_WIDTH_CM)
    mon.setDistance(VIEWING_DISTANCE_CM)
    mon.setSizePix([SCREEN_WIDTH_PX, SCREEN_HEIGHT_PX])
    mon.saveMon()
    return mon


def assert_screen_available(screen_index: int) -> None:
    """Abort loudly if the requested display doesn't exist."""
    import pyglet
    n = len(pyglet.canvas.get_display().get_screens())
    if screen_index >= n:
        sys.stderr.write(
            f"ERROR: requested screen {screen_index} but only {n} display(s) "
            f"are connected. Fixation would render on the wrong monitor.\n"
            f"Check cable/arrangement, or pass --screen 0 for testing.\n"
        )
        sys.exit(2)


TEST_SCALE = 6.0  # only applied with --test; multiplies every dimension


def run(duration: float | None, screen: int, fullscreen: bool,
        test_mode: bool) -> None:
    from psychopy import visual, core, event

    ensure_monitor()
    if fullscreen:
        assert_screen_available(screen)

    scale = TEST_SCALE if test_mode else 1.0

    win = visual.Window(
        size=(SCREEN_WIDTH_PX, SCREEN_HEIGHT_PX),
        fullscr=fullscreen,
        screen=screen,
        monitor=MONITOR_NAME,
        units="deg",
        color=BG_GRAY,
        colorSpace="rgb",
        allowGUI=False,
        waitBlanking=True,
        winType="pyglet",
    )
    win.mouseVisible = False

    # Use a shared flag instead of core.quit() so exit is deterministic and
    # loggable; globalKeys fire regardless of window focus (robust on Wayland).
    quit_flag = {"requested_by": None}

    def _quit_via(source: str):
        def _fn():
            if quit_flag["requested_by"] is None:
                quit_flag["requested_by"] = source
                logging.info("quit requested via %s", source)
        return _fn

    event.globalKeys.add(key="escape", func=_quit_via("globalKey:escape"),
                         name="esc_quit")
    event.globalKeys.add(key="q", func=_quit_via("globalKey:q"),
                        name="q_quit")

    shape_kwargs = dict(units="deg", colorSpace="rgb",
                        fillColor=FG_BLACK, lineColor=FG_BLACK)
    bg_kwargs = dict(units="deg", colorSpace="rgb",
                     fillColor=BG_GRAY, lineColor=BG_GRAY)

    outer = visual.Circle(win, radius=(OUTER_DEG * scale) / 2, **shape_kwargs)
    h_bar = visual.Rect(win, width=OUTER_DEG * scale,
                        height=CROSS_DEG * scale, **bg_kwargs)
    v_bar = visual.Rect(win, width=CROSS_DEG * scale,
                        height=OUTER_DEG * scale, **bg_kwargs)
    inner = visual.Circle(win, radius=(INNER_DEG * scale) / 2, **shape_kwargs)

    # Loud banner so test-sized target is never mistaken for a real session.
    # All colors in rgb space: red ~ (1, -0.8, -0.8); black (-1, -1, -1).
    test_banner = None
    test_backing = None
    if test_mode:
        test_backing = visual.Rect(
            win, width=18.0, height=2.0, pos=(0, -6.5), units="deg",
            colorSpace="rgb",
            fillColor=(1.0, -0.8, -0.8), lineColor=(1.0, -0.8, -0.8),
        )
        test_banner = visual.TextStim(
            win, text="TEST MODE - NOT FOR SCAN USE",
            units="deg", height=1.0, pos=(0, -6.5),
            colorSpace="rgb", color=(-1, -1, -1), bold=True,
        )

    def _render_frame():
        outer.draw()
        h_bar.draw()
        v_bar.draw()
        inner.draw()
        if test_backing is not None:
            test_backing.draw()
        if test_banner is not None:
            test_banner.draw()

    # Draw once and flip. The stimulus now sits in the framebuffer and stays
    # visible until we change it — no per-frame clear-redraw flicker.
    clock = core.Clock()
    event.clearEvents()
    _render_frame()
    win.flip()
    logging.info("initial frame displayed; entering persistent wait loop")

    next_log_tick = 2.0
    next_refresh = 1.0            # slow refresh: 1 Hz, preserves buffer
    POLL_INTERVAL = 0.05          # 20 Hz key polling
    try:
        while quit_flag["requested_by"] is None:
            # Fallback key polling (in case globalKeys fails on this system).
            if event.getKeys(keyList=["escape", "q"]):
                quit_flag["requested_by"] = "getKeys_fallback"
                logging.info("quit via getKeys fallback")
                break

            t = clock.getTime()

            if duration is not None and t >= duration:
                quit_flag["requested_by"] = "duration"
                logging.info("quit via duration (%.2fs)", duration)
                break

            # Slow refresh — redraws the same shapes and flips without
            # clearing. Keeps pyglet event loop alive for keyboard input
            # and repaints the window if the compositor invalidated it,
            # without introducing per-frame gray flashes.
            if t >= next_refresh:
                _render_frame()
                win.flip(clearBuffer=False)
                next_refresh = t + 1.0

            if t >= next_log_tick:
                logging.info("alive at t=%.2fs", t)
                next_log_tick = t + 10.0

            core.wait(POLL_INTERVAL)
    finally:
        logging.info("loop exit: reason=%s elapsed=%.2fs",
                     quit_flag["requested_by"], clock.getTime())
        win.close()
        core.quit()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("-d", "--duration", type=float, default=None,
                   help="seconds to display; omit to run until ESC/Q")
    p.add_argument("-s", "--screen", type=int, default=1,
                   help="display index (default: 1 = BOLDscreen)")
    p.add_argument("--windowed", action="store_true",
                   help="run in a window instead of fullscreen (for debugging)")
    p.add_argument("--test", action="store_true",
                   help="scale target 6x with a 'TEST MODE' banner, for "
                        "operator visibility checks from the control room. "
                        "Never use for actual scans.")
    args = p.parse_args()
    logging.info("launch: duration=%s screen=%s fullscreen=%s test=%s",
                 args.duration, args.screen, not args.windowed, args.test)
    try:
        run(duration=args.duration, screen=args.screen,
            fullscreen=not args.windowed, test_mode=args.test)
    except Exception:
        logging.exception("fixation_cross crashed")
        raise
    logging.info("exit: clean")


if __name__ == "__main__":
    main()
