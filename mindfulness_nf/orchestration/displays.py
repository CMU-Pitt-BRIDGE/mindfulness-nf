"""Put PsychoPy on the left monitor with keyboard focus, and MURFI on the right.

The scanner trigger arrives as keystrokes from a USB trigger box, so the
PsychoPy window must hold keyboard focus when the scan starts. Every X11
call here is best-effort: on failure (no DISPLAY, no python-xlib, window
never appears) the functions return ``False`` and the run continues.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

__all__ = [
    "Monitor",
    "focus_psychopy",
    "leftmost",
    "covers",
    "inside",
    "move_murfi_right",
    "rightmost",
]

_POLL_SECONDS = 0.25
_MURFI_WM_CLASS = "murfi"
# pyglet sets WM_CLASS but not _NET_WM_PID on PsychoPy windows.
_PSYCHOPY_WM_CLASS = "psychopy"


@dataclass(frozen=True, slots=True)
class Monitor:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2


def leftmost(monitors: tuple[Monitor, ...]) -> Monitor:
    return min(monitors, key=lambda m: (m.x, m.y))


def rightmost(monitors: tuple[Monitor, ...]) -> Monitor:
    return max(monitors, key=lambda m: (m.x, -m.y))


def covers(window: Monitor, monitor: Monitor) -> bool:
    """True when *window* (as a rectangle) fills *monitor*: a fullscreen window."""
    return (
        window.x <= monitor.x
        and window.y <= monitor.y
        and window.x + window.width >= monitor.x + monitor.width
        and window.y + window.height >= monitor.y + monitor.height
    )


def inside(window: Monitor, monitor: Monitor) -> bool:
    """True when the window's top-left corner lies on *monitor*."""
    return (
        monitor.x <= window.x < monitor.x + monitor.width
        and monitor.y <= window.y < monitor.y + monitor.height
    )


# ---------------------------------------------------------------------------
# X11 shell
# ---------------------------------------------------------------------------


def _open_display() -> Any | None:
    try:
        from Xlib import display

        return display.Display()
    except Exception:  # noqa: BLE001 — no X server / no python-xlib
        return None


def _monitors(d: Any) -> tuple[Monitor, ...]:
    screens = d.xinerama_query_screens().screens
    return tuple(Monitor(s.x, s.y, s.width, s.height) for s in screens)


def _client_windows(d: Any) -> list[Any]:
    from Xlib import X

    root = d.screen().root
    prop = root.get_full_property(
        d.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType
    )
    if prop is not None:
        return [d.create_resource_object("window", wid) for wid in prop.value]
    return list(root.query_tree().children)


def _find(d: Any, *, wm_class: str) -> list[Any]:
    found = []
    for win in _client_windows(d):
        try:
            if wm_class in (win.get_wm_class() or ()):
                found.append(win)
        except Exception:  # noqa: BLE001 — window vanished mid-scan
            continue
    return found


def _rect(d: Any, win: Any) -> Monitor:
    g = win.get_geometry()
    origin = win.translate_coords(d.screen().root, 0, 0)
    return Monitor(-origin.x, -origin.y, g.width, g.height)


def _send_root_message(d: Any, win: Any, atom_name: str, data: list[int]) -> None:
    from Xlib import X
    from Xlib.protocol import event

    root = d.screen().root
    msg = event.ClientMessage(
        window=win,
        client_type=d.intern_atom(atom_name),
        data=(32, (data + [0] * 5)[:5]),
    )
    root.send_event(
        msg, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask
    )


def _move(d: Any, win: Any, target: Monitor) -> None:
    # _NET_MOVERESIZE_WINDOW: gravity=static(10), x+y flags (bits 8,9),
    # source=pager (bit 13) so the window manager applies it.
    flags = 10 | (1 << 8) | (1 << 9) | (2 << 12)
    _send_root_message(
        d, win, "_NET_MOVERESIZE_WINDOW", [flags, target.x + 40, target.y + 40, 0, 0]
    )
    win.configure(x=target.x + 40, y=target.y + 40)
    d.sync()


def _active_window(d: Any) -> int | None:
    from Xlib import X

    prop = d.screen().root.get_full_property(
        d.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType
    )
    return int(prop.value[0]) if prop is not None and len(prop.value) else None


def _focus(d: Any, win: Any, monitor: Monitor) -> None:
    from Xlib import X
    from Xlib.ext import xtest

    # Ask the window manager to activate it, as a pager would (source=2).
    _send_root_message(d, win, "_NET_ACTIVE_WINDOW", [2, X.CurrentTime, 0])
    # Then move the pointer onto it and click, which every window manager
    # treats as a focus change.
    x, y = monitor.center
    d.screen().root.warp_pointer(x, y)
    d.sync()
    xtest.fake_input(d, X.ButtonPress, 1)
    xtest.fake_input(d, X.ButtonRelease, 1)
    d.sync()


def _focus_psychopy_blocking(timeout: float) -> bool:
    d = _open_display()
    if d is None:
        return False
    try:
        target = leftmost(_monitors(d))
        deadline = time.monotonic() + timeout
        # Wait for the fullscreen task window; skip any dialog PsychoPy shows
        # first (e.g. "already have data for this run").
        while time.monotonic() < deadline:
            task_windows = [
                w
                for w in _find(d, wm_class=_PSYCHOPY_WM_CLASS)
                if covers(_rect(d, w), target)
            ]
            if task_windows:
                win = task_windows[0]
                _focus(d, win, target)
                time.sleep(_POLL_SECONDS)
                if _active_window(d) == win.id:
                    return True
            time.sleep(_POLL_SECONDS)
        return False
    except Exception:  # noqa: BLE001 — best effort
        return False
    finally:
        d.close()


def _move_murfi_right_blocking(timeout: float) -> bool:
    d = _open_display()
    if d is None:
        return False
    try:
        deadline = time.monotonic() + timeout
        target = rightmost(_monitors(d))
        while time.monotonic() < deadline:
            wins = _find(d, wm_class=_MURFI_WM_CLASS)
            for win in wins:
                _move(d, win, target)
            time.sleep(_POLL_SECONDS)
            if wins and all(inside(_rect(d, w), target) for w in wins):
                return True
        return False
    except Exception:  # noqa: BLE001 — best effort
        return False
    finally:
        d.close()


async def focus_psychopy(timeout: float = 120.0) -> bool:
    """Wait for PsychoPy's fullscreen window on the left monitor, then focus it.

    Returns ``True`` once the window manager reports it as the active window.
    """
    return await asyncio.to_thread(_focus_psychopy_blocking, timeout)


async def move_murfi_right(timeout: float = 30.0) -> bool:
    """Wait for MURFI's window and move it to the rightmost monitor."""
    return await asyncio.to_thread(_move_murfi_right_blocking, timeout)
