#!/bin/bash
# Mindfulness NF Launcher — double-click from desktop
# Shows a simple dialog, then runs the session in a terminal

PROJECT_DIR="/home/young-lab/code/mindfulness-nf"
SCRIPT="${PROJECT_DIR}/murfi/scripts/run_session.sh"

# Get subject ID
SUBJ=$(zenity --entry \
    --title="Mindfulness Neurofeedback" \
    --text="Enter subject ID (e.g., 001):" \
    --width=400 2>/dev/null)

[ -z "$SUBJ" ] && exit 0
SUBJ="sub-${SUBJ}"

# Pick session
SESSION=$(zenity --list \
    --title="Mindfulness NF — ${SUBJ}" \
    --text="Select session:" \
    --column="Session" --column="Description" \
    "localizer"  "Session 1: Collect resting state data" \
    "process"    "Between sessions: ICA + mask extraction (~30 min)" \
    "nf"         "Session 2: Neurofeedback (12 runs)" \
    "test"       "Test: Dry run (no scanner needed)" \
    --width=500 --height=300 2>/dev/null)

[ -z "$SESSION" ] && exit 0

if [ "$SESSION" = "test" ]; then
    EXTRA="--dry-run"
    SESSION="nf"
else
    EXTRA=""
fi

# Open a terminal and run the session
gnome-terminal --title="Mindfulness NF — ${SUBJ} ${SESSION}" \
    -- bash -c "
        cd ${PROJECT_DIR}/murfi/scripts
        bash run_session.sh ${SUBJ} ${SESSION} ${EXTRA}
        echo ''
        echo 'Session complete. Press Enter to close.'
        read
    "
