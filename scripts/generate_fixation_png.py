"""Render the Thaler ABC fixation target to a pixel-exact PNG.

This produces materials/FixationCross.png — a static 1920x1080 image that,
when shown 1:1 (no scaling) on the native-resolution BOLDscreen, is
pixel-identical to what psychopy/fixation/fixation_cross.py draws live.

Why a PNG instead of the live PsychoPy window:
    The resting-state fixation is a single, unchanging frame. The PsychoPy
    script holds it with a full OpenGL window + repaint loop whose keyboard
    teardown is window-focus dependent and unreliable on the BOLDscreen
    (the operator console never holds focus on screen 1). A static image
    shown by a tiny viewer (psychopy/fixation/fixation_image.py) removes all
    of that machinery. See that file for the close/toggle behaviour.

Geometry is duplicated from generate_fixation_cross.py and
fixation_cross.py on purpose — those two scripts already each carry their
own copy of these constants; this keeps the three renderers independent and
greppable. If the BOLDscreen calibration changes, update all three.

Reference:
    Thaler L, Schutz AC, Goodale MA, Gegenfurtner KR (2013).
    "What is the best fixation target?" Vision Research 76:31-42.

Run:
    uv run scripts/generate_fixation_png.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

# --- Display calibration (Pitt BOLDscreen) ------------------------------------
VIEWING_DISTANCE_CM = 139.0
SCREEN_W_CM, SCREEN_H_CM = 69.84, 39.29
SCREEN_W_PX, SCREEN_H_PX = 1920, 1080


def cm_to_deg(size_cm: float, dist_cm: float) -> float:
    """Visual angle subtended by `size_cm` at `dist_cm` viewing distance."""
    return math.degrees(2 * math.atan(size_cm / (2 * dist_cm)))


FOV_W_DEG = cm_to_deg(SCREEN_W_CM, VIEWING_DISTANCE_CM)  # ~28.20
PX_PER_DEG = SCREEN_W_PX / FOV_W_DEG                      # ~68.08

# --- Thaler ABC target (deg of visual angle) ----------------------------------
OUTER_DEG = 1.2   # outer disk diameter
INNER_DEG = 0.4   # inner disk diameter
CROSS_DEG = 0.2   # crosshair thickness

# --- Colors (8-bit) -----------------------------------------------------------
# Matches PsychoPy rgb space: bg rgb 0 -> 128, fg rgb -1 -> 0.
BG_GRAY = (128, 128, 128)
FG_BLACK = (0, 0, 0)

# Supersampling factor for anti-aliasing. PsychoPy smooths circle edges via
# multisampling; PIL's draw primitives are hard-edged, so we render large and
# downsample with a high-quality (Lanczos) filter to recover smooth edges.
SUPERSAMPLE = 8


def build_image(ss: int = SUPERSAMPLE) -> Image.Image:
    """Render the target at `ss`x resolution, then downsample to native."""
    w, h = SCREEN_W_PX * ss, SCREEN_H_PX * ss
    img = Image.new("RGB", (w, h), BG_GRAY)
    draw = ImageDraw.Draw(img)
    cx, cy = w / 2.0, h / 2.0

    def disk(diam_deg: float, color: tuple[int, int, int]) -> None:
        r = diam_deg * PX_PER_DEG * ss / 2.0
        draw.ellipse(
            [round(cx - r), round(cy - r), round(cx + r), round(cy + r)],
            fill=color,
        )

    def bar(w_deg: float, h_deg: float, color: tuple[int, int, int]) -> None:
        hw = w_deg * PX_PER_DEG * ss / 2.0
        hh = h_deg * PX_PER_DEG * ss / 2.0
        draw.rectangle(
            [round(cx - hw), round(cy - hh), round(cx + hw), round(cy + hh)],
            fill=color,
        )

    # Thaler ABC: outer black disk, gray crosshair cut through it, inner dot.
    disk(OUTER_DEG, FG_BLACK)
    bar(OUTER_DEG, CROSS_DEG, BG_GRAY)   # horizontal gray bar
    bar(CROSS_DEG, OUTER_DEG, BG_GRAY)   # vertical gray bar
    disk(INNER_DEG, FG_BLACK)

    return img.resize((SCREEN_W_PX, SCREEN_H_PX), Image.LANCZOS)


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "materials" / "FixationCross.png"
    out.parent.mkdir(exist_ok=True)
    img = build_image()
    img.save(out)
    print(f"Wrote {out}  ({img.width}x{img.height})")
    print(f"  {PX_PER_DEG:.2f} px/deg  |  "
          f"outer={OUTER_DEG * PX_PER_DEG:.1f}px  "
          f"inner={INNER_DEG * PX_PER_DEG:.1f}px  "
          f"cross={CROSS_DEG * PX_PER_DEG:.1f}px")


if __name__ == "__main__":
    main()
