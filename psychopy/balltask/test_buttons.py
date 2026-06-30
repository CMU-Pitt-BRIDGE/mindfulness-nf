"""Press any button on the button box to see its key name.
Press Escape to quit. Run from the project root:
    .venv/bin/python psychopy/balltask/test_buttons.py
"""
from psychopy import visual, event, core

win = visual.Window(
    size=(800, 400), fullscr=False, screen=0,
    color=[-1, -1, -1], allowGUI=True,
)

msg = visual.TextStim(
    win, text="Press any button on the box.\nKey names appear here.\nEscape to quit.",
    height=0.1, wrapWidth=1.8, color="white",
)
history = []

while True:
    keys = event.getKeys()
    if "escape" in keys:
        break
    if keys:
        for k in keys:
            history.append(k)
            print(f"Key pressed: '{k}'")
        # Show last 8 key presses
        recent = history[-8:]
        msg.text = "Recent keys:\n" + "  ".join(f"'{k}'" for k in recent) + "\n\nEscape to quit."
    msg.draw()
    win.flip()

win.close()
core.quit()
