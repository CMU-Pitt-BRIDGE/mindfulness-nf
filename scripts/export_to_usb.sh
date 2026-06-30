#!/usr/bin/env bash
#
# GUI front-end for export_subject.sh. Double-click the "Export Subject to USB"
# desktop icon: pick a drive, a subject, and what to copy, then watch it run.
#
# Non-destructive: delegates to export_subject.sh, which never deletes anything.
set -euo pipefail

PROJECT_DIR="/home/young-lab/code/mindfulness-nf"
SUBJECTS_DIR="$PROJECT_DIR/murfi/subjects"
SCRIPT="$PROJECT_DIR/scripts/export_subject.sh"
TITLE="Export Subject to USB"

command -v zenity >/dev/null || { echo "zenity not installed" >&2; exit 1; }
fail() { zenity --error --title="$TITLE" --no-wrap --text="$1" 2>/dev/null; exit 1; }

# 1. Destination drive (mounts under /media/$USER).
mapfile -t DRIVES < <(find "/media/$USER" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)
[ "${#DRIVES[@]}" -gt 0 ] || fail "No USB drive found under /media/$USER.\nPlug one in and try again."
DEST=$(printf '%s\n' "${DRIVES[@]}" | zenity --list --title="$TITLE" \
    --width=520 --height=300 --text="1. Destination drive:" \
    --column="Drive" 2>/dev/null) || exit 0
[ -n "$DEST" ] || exit 0

# 2. Subject.
mapfile -t SUBJECTS < <(find "$SUBJECTS_DIR" -mindepth 1 -maxdepth 1 -type d -name 'sub-*' -printf '%f\n' 2>/dev/null | sort)
[ "${#SUBJECTS[@]}" -gt 0 ] || fail "No subjects in $SUBJECTS_DIR."
SUBJECT=$(printf '%s\n' "${SUBJECTS[@]}" | zenity --list --title="$TITLE" \
    --width=520 --height=420 --text="2. Subject to copy:" \
    --column="Subject" 2>/dev/null) || exit 0
[ -n "$SUBJECT" ] || exit 0

# 3. What to copy.
MODE=$(zenity --list --radiolist --title="$TITLE" --width=600 --height=300 \
    --text="3. What to copy:" --column="" --column="Mode" --column="Includes" \
    TRUE  default "Everything except raw images (recommended)" \
    FALSE lean    "Smallest: also skips FSL working dirs" \
    FALSE full    "Bit-for-bit, including raw images" 2>/dev/null) || exit 0
[ -n "$MODE" ] || MODE=default
FLAG=""; [ "$MODE" = "default" ] || FLAG="--$MODE"

# 4. Run in a terminal so the operator sees rsync progress and the result.
gnome-terminal --title="$TITLE: $SUBJECT" -- bash -c "
    bash '$SCRIPT' '$SUBJECT' '$DEST' $FLAG
    echo
    echo 'Press Enter to close.'
    read _
" 2>/dev/null || bash "$SCRIPT" "$SUBJECT" "$DEST" $FLAG
