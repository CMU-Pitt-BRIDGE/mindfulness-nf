#!/usr/bin/env bash
#
# Export one subject's data off the scanner workstation — e.g. to a USB drive.
#
# Usage:
#   scripts/export_subject.sh SUBJECT [DEST] [--full|--lean] [--dry-run]
#
#   SUBJECT    subject id, e.g. sub-kym260626  (the "sub-" prefix is optional)
#   DEST       destination directory, e.g. a mounted USB drive.
#              If omitted, auto-detects a single removable drive under
#              /media/$USER (errors if zero or more than one is found).
#
#   --full     include the raw per-volume images (img/img-*.nii). Default
#              EXCLUDES them: the scanner already retains the full series, and
#              everything analysis-relevant (PsychoPy, MURFI logs, masks, ICA,
#              motion, activation maps) is copied regardless.
#   --lean     also skip the regenerable FSL working dirs (*.gica/*.ica/*.feat),
#              for the smallest copy (PsychoPy + logs + masks + motion + maps).
#   --dry-run  show exactly what would be copied, copy nothing.
#
# NON-DESTRUCTIVE: never deletes anything at the source or the destination.
set -euo pipefail

PROJECT_DIR="/home/young-lab/code/mindfulness-nf"
SUBJECTS_DIR="$PROJECT_DIR/murfi/subjects"

die() { echo "ERROR: $*" >&2; exit 1; }

# --- parse args -----------------------------------------------------------
SUBJECT="" ; DEST="" ; MODE="default" ; DRYRUN=""
for a in "$@"; do
  case "$a" in
    --full)    MODE="full" ;;
    --lean)    MODE="lean" ;;
    --dry-run) DRYRUN="--dry-run" ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    -*)        die "unknown option: $a" ;;
    *) if   [ -z "$SUBJECT" ]; then SUBJECT="$a"
       elif [ -z "$DEST" ];    then DEST="$a"
       else die "too many positional args"; fi ;;
  esac
done
[ -n "$SUBJECT" ] || die "usage: export_subject.sh SUBJECT [DEST] [--full|--lean] [--dry-run]"

# normalise the sub- prefix
case "$SUBJECT" in sub-*) ;; *) SUBJECT="sub-$SUBJECT" ;; esac
SRC="$SUBJECTS_DIR/$SUBJECT"
[ -d "$SRC" ] || die "subject not found: $SRC"

# --- auto-detect a USB drive if no destination was given ------------------
if [ -z "$DEST" ]; then
  mapfile -t MOUNTS < <(find "/media/$USER" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)
  case "${#MOUNTS[@]}" in
    1) DEST="${MOUNTS[0]}" ; echo "Auto-detected drive: $DEST" ;;
    0) die "no destination given and no drive under /media/$USER. Plug in a USB, or pass a destination." ;;
    *) printf 'Multiple drives under /media/%s:\n' "$USER"; printf '  %s\n' "${MOUNTS[@]}"
       die "more than one drive found — pass the destination explicitly." ;;
  esac
fi
[ -d "$DEST" ] || die "destination not found: $DEST"
[ -w "$DEST" ] || die "destination not writable: $DEST"

DEST_SUB="$DEST/$SUBJECT"

# --- excludes by mode -----------------------------------------------------
EXCLUDES=()
case "$MODE" in
  full) ;;                                          # bit-for-bit, includes raw images
  default)
    EXCLUDES+=(--exclude='img/img-*.nii') ;;        # drop raw images, keep curact-*/design-*
  lean)
    EXCLUDES+=(--exclude='img/img-*.nii'
               --exclude='*.gica' --exclude='*.ica' --exclude='*.feat') ;;
esac

echo "Exporting $SUBJECT"
echo "  from: $SRC"
echo "  to:   $DEST_SUB"
echo "  mode: $MODE${DRYRUN:+   (dry run)}"
echo

# -ah: archive + human sizes; --info=progress2: one overall progress line.
rsync -ah --info=progress2 ${DRYRUN:+--dry-run} "${EXCLUDES[@]}" "$SRC/" "$DEST_SUB/"

if [ -z "$DRYRUN" ]; then
  sync
  echo
  echo "Done — $(du -sh "$DEST_SUB" 2>/dev/null | cut -f1) copied to $DEST_SUB"
  echo "Verify it looks right, then eject the drive from the file manager."
fi
