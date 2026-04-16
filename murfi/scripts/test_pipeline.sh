#!/bin/bash
# =============================================================================
# Mindfulness NF Pipeline Test
# Simulates scanner data and tests the full MURFI pipeline without a real scanner
# Usage: bash test_pipeline.sh [level]
#   level 1: Smoke test (MURFI receives 2 volumes)
#   level 2: Resting state + ICA (~30 min)
#   level 3: Full feedback loop (MURFI + servenii + PsychoPy)
#   level 0: PsychoPy only (no MURFI, fake data)
# =============================================================================

set -e

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_PATH}/../.." && pwd)"
SUBJECTS_DIR="${SCRIPT_PATH}/../subjects"
CONTAINER="/opt/murfi/apptainer-images/murfi.sif"
SERVENII="/opt/murfi/util/scanner_sim/servenii"
EXAMPLE_DATA="/home/young-lab/murfi_example_data"
TEST_SUBJECT="sub-test"
TEST_SUBJECT_DIR="${SUBJECTS_DIR}/${TEST_SUBJECT}"

# FSL (via group membership) + project .venv
export FSLOUTPUTTYPE=NIFTI
export PATH="${PROJECT_ROOT}/.venv/bin:${PATH}"

# MURFI env
export MURFI_SUBJECTS_DIR="$(cd "${SUBJECTS_DIR}" && pwd)/"
export MURFI_SUBJECT_NAME="${TEST_SUBJECT}"

LEVEL="${1:-1}"

# --- Helpers ---

check_prereqs() {
    local ok=true
    command -v flirt >/dev/null 2>&1   || { echo "FAIL: FSL not on PATH (are you in the fsl group? try: newgrp fsl)"; ok=false; }
    command -v apptainer >/dev/null 2>&1 || { echo "FAIL: apptainer not found"; ok=false; }
    test -f "$CONTAINER"               || { echo "FAIL: container not found at $CONTAINER"; ok=false; }
    python -c "import pandas, numpy" 2>/dev/null || { echo "FAIL: Python deps missing (run: cd $PROJECT_ROOT && uv sync)"; ok=false; }
    test -d "$EXAMPLE_DATA/img"        || { echo "FAIL: example data not found at $EXAMPLE_DATA"; ok=false; }
    $ok || exit 1
    echo "Prerequisites OK"
}

setup_test_subject() {
    echo "Setting up test subject: ${TEST_SUBJECT}"
    cd "$SCRIPT_PATH"

    # Create subject dirs + copy XML configs
    if [ -d "$TEST_SUBJECT_DIR" ]; then
        echo "  Removing old test subject..."
        rm -rf "$TEST_SUBJECT_DIR"
    fi
    source createxml.sh "$TEST_SUBJECT" setup 2>/dev/null

    # Prepare simulated volumes from example data
    # Example data: nf-00001-00000.nii through nf-00001-00084.nii (85 volumes)
    # MURFI expects: img-SSSSS-VVVVV.nii
    echo "  Copying example data (85 volumes)..."
    mkdir -p "${TEST_SUBJECT_DIR}/img"
    local count=0
    for src in "${EXAMPLE_DATA}"/img/nf-00001-*.nii; do
        local vol=$(printf '%05d' $((count + 1)))
        cp "$src" "${TEST_SUBJECT_DIR}/img/img-00001-${vol}.nii"
        count=$((count + 1))
    done
    echo "  Prepared ${count} volumes"
}

serve_volumes() {
    local n=$1
    echo "Sending ${n} simulated volumes to MURFI on port 50000..."
    cd "$TEST_SUBJECT_DIR"
    apptainer exec \
        --bind "$(pwd):$(pwd)" \
        "$CONTAINER" \
        "$SERVENII" img/img 1 "$n" 1 68 1200 50000 127.0.0.1
}

start_murfi() {
    local xml_path="$1"
    echo "Starting MURFI with ${xml_path}..."
    cd "$SCRIPT_PATH"
    unset SESSION_MANAGER
    export NO_AT_BRIDGE=1
    mkdir -p "/tmp/runtime-$(id -u)" 2>/dev/null || true
    setsid apptainer exec \
        --nv \
        --cleanenv \
        --env DISPLAY="${DISPLAY}" \
        --env XDG_RUNTIME_DIR="/tmp/runtime-$(id -u)" \
        --env QT_QPA_PLATFORM=xcb \
        --env NO_AT_BRIDGE=1 \
        --env QT_LOGGING_RULES="*.debug=false;*.warning=false" \
        --env MURFI_SUBJECTS_DIR="${MURFI_SUBJECTS_DIR}" \
        --env MURFI_SUBJECT_NAME="${MURFI_SUBJECT_NAME}" \
        --bind "${MURFI_SUBJECTS_DIR}:${MURFI_SUBJECTS_DIR}" \
        "$CONTAINER" \
        murfi -f "$xml_path" \
        </dev/null 2>&1
}

# --- Test levels ---

level0() {
    echo ""
    echo "=== LEVEL 0: PsychoPy Only (fake MURFI data) ==="
    echo "This tests the PsychoPy task display with random data."
    echo "No MURFI or scanner simulation needed."
    echo ""

    cd "${PROJECT_ROOT}/psychopy/balltask"

    # Temporarily enable fake mode
    sed -i 's/murfi_FAKE=False/murfi_FAKE=True/' rt-network_feedback.py
    echo "Enabled fake MURFI mode. Starting PsychoPy..."
    echo "(Enter test info in the dialog, press Escape to exit early)"
    echo ""

    python rt-network_feedback.py || true

    # Restore real mode
    sed -i 's/murfi_FAKE=True/murfi_FAKE=False/' rt-network_feedback.py
    echo "Restored real MURFI mode."
}

level1() {
    echo ""
    echo "=== LEVEL 1: Smoke Test (2-volume receive) ==="
    echo "Tests: MURFI starts, receives data, saves reference image."
    echo ""
    echo "This requires TWO terminals:"
    echo "  Terminal 1 (this one): runs MURFI"
    echo "  Terminal 2: sends simulated data"
    echo ""

    setup_test_subject

    echo ""
    echo "MURFI will start and wait for data."
    echo "In another terminal, run:"
    echo "  cd $PROJECT_ROOT && bash murfi/scripts/test_pipeline.sh serve 2"
    echo ""
    echo "Press Enter to start MURFI, or Ctrl-C to cancel."
    read -r

    start_murfi "${TEST_SUBJECT_DIR}/xml/2vol.xml"

    echo ""
    if [ -f "${TEST_SUBJECT_DIR}/xfm/series1_ref.nii" ]; then
        echo "SUCCESS: reference image created at ${TEST_SUBJECT_DIR}/xfm/series1_ref.nii"
    else
        echo "CHECK: look for series*_ref.nii in ${TEST_SUBJECT_DIR}/xfm/"
        ls "${TEST_SUBJECT_DIR}/xfm/" 2>/dev/null
    fi
}

level2() {
    echo ""
    echo "=== LEVEL 2: Resting State + ICA ==="
    echo "Tests: receive 85 volumes, run ICA, extract DMN/CEN masks."
    echo "ICA takes ~25 minutes."
    echo ""

    setup_test_subject

    # Adjust rest.xml to match our 85 volumes
    sed -i 's/measurements">    250/measurements">    85/' \
        "${TEST_SUBJECT_DIR}/xml/rest.xml"
    echo "Set rest.xml to 85 measurements (matching example data)"

    echo ""
    echo "MURFI will start and wait for resting state data."
    echo "In another terminal, run:"
    echo "  cd $PROJECT_ROOT && bash murfi/scripts/test_pipeline.sh serve 85"
    echo ""
    echo "Press Enter to start MURFI, or Ctrl-C to cancel."
    read -r

    start_murfi "${TEST_SUBJECT_DIR}/xml/rest.xml"

    echo ""
    echo "MURFI finished receiving. Running ICA pipeline..."
    echo "(This will open zenity dialogs — select run 1, single-run ICA)"
    echo ""

    cd "$SCRIPT_PATH"
    source feedback.sh "$TEST_SUBJECT" extract_rs_networks
    source feedback.sh "$TEST_SUBJECT" process_roi_masks
    source feedback.sh "$TEST_SUBJECT" register

    echo ""
    if [ -f "${TEST_SUBJECT_DIR}/mask/dmn.nii" ] && [ -f "${TEST_SUBJECT_DIR}/mask/cen.nii" ]; then
        dmn_vox=$(fslstats "${TEST_SUBJECT_DIR}/mask/dmn.nii" -V | awk '{print $1}')
        cen_vox=$(fslstats "${TEST_SUBJECT_DIR}/mask/cen.nii" -V | awk '{print $1}')
        echo "SUCCESS: DMN mask (${dmn_vox} voxels), CEN mask (${cen_vox} voxels)"
    else
        echo "FAIL: masks not created"
        ls "${TEST_SUBJECT_DIR}/mask/" 2>/dev/null
    fi
}

level3() {
    MURFI_PID=""
    SERVE_PID=""
    cleanup_level3() {
        echo ""
        echo "Cleaning up..."
        [ -n "$SERVE_PID" ] && kill $SERVE_PID 2>/dev/null
        [ -n "$MURFI_PID" ] && kill $MURFI_PID 2>/dev/null
        wait $SERVE_PID 2>/dev/null || true
        wait $MURFI_PID 2>/dev/null || true
        echo "Done."
    }
    trap cleanup_level3 EXIT INT TERM

    echo ""
    echo "=== LEVEL 3: Full Feedback Loop (automated) ==="
    echo "Tests: MURFI receives data + computes ROI activations + PsychoPy displays feedback."
    echo "Everything runs in one terminal. Press Ctrl-C to stop early."
    echo ""

    setup_test_subject

    # --- Step 1: Ensure reference image exists (run 2-vol if needed) ---
    if [ ! -f "${TEST_SUBJECT_DIR}/xfm/study_ref.nii" ]; then
        echo "[Step 1/5] Creating reference image (2-volume scan)..."

        # Start MURFI in background
        mkdir -p "/tmp/runtime-$(id -u)" 2>/dev/null || true
        setsid apptainer exec \
            --nv --cleanenv \
            --env DISPLAY="${DISPLAY}" \
            --env XDG_RUNTIME_DIR="/tmp/runtime-$(id -u)" \
            --env QT_QPA_PLATFORM=xcb \
            --env NO_AT_BRIDGE=1 \
            --env QT_LOGGING_RULES="*.debug=false;*.warning=false" \
            --env MURFI_SUBJECTS_DIR="${MURFI_SUBJECTS_DIR}" \
            --env MURFI_SUBJECT_NAME="${MURFI_SUBJECT_NAME}" \
            --bind "${MURFI_SUBJECTS_DIR}:${MURFI_SUBJECTS_DIR}" \
            "$CONTAINER" \
            murfi -f "${TEST_SUBJECT_DIR}/xml/2vol.xml" \
            </dev/null > /dev/null 2>&1 &
        MURFI_PID=$!

        # Wait for port 50000 to be listening
        for i in $(seq 1 30); do
            if ss -tln | grep -q ':50000 '; then break; fi
            sleep 1
        done

        serve_volumes 2
        sleep 2  # let MURFI finish processing

        # Kill MURFI (it doesn't exit on its own — GUI stays open)
        kill $MURFI_PID 2>/dev/null || true
        wait $MURFI_PID 2>/dev/null || true
        echo "  Reference image created."
    else
        echo "[Step 1/5] Reference image exists, skipping."
    fi

    # --- Step 2: Create test masks in native space ---
    echo "[Step 2/5] Creating test masks from reference image..."
    cd "${TEST_SUBJECT_DIR}"

    # Find the reference image
    REF=$(ls xfm/series*_ref.nii 2>/dev/null | head -1)
    if [ -z "$REF" ]; then
        REF="xfm/study_ref.nii"
    fi
    cp "$REF" xfm/study_ref.nii 2>/dev/null || true

    # MNI→native registration is unreliable from a 2-volume functional
    # reference (wrong contrast, low SNR). Instead, create non-overlapping
    # masks directly in native space by splitting the brain into posterior
    # (DMN-like) and anterior (CEN-like) halves using fslmaths -roi.
    # These are not anatomically accurate — they exist so MURFI computes
    # real weighted-average activations for the end-to-end test.
    # Real sessions use ICA-derived masks (feedback.sh process_roi_masks).
    NY=$(fslval "$REF" dim2)
    HALF_Y=$((NY / 2))

    fslmaths "$REF" -thr 1 -bin mask/brain.nii
    fslmaths mask/brain.nii -roi 0 -1 0 "$HALF_Y" 0 -1 0 1 mask/dmn.nii
    fslmaths mask/brain.nii -roi 0 -1 "$HALF_Y" "$HALF_Y" 0 -1 0 1 mask/cen.nii
    rm -f mask/brain.nii

    DMN_VOX=$(fslstats mask/dmn.nii -V | awk '{print $1}')
    CEN_VOX=$(fslstats mask/cen.nii -V | awk '{print $1}')
    echo "  DMN: ${DMN_VOX} voxels (posterior), CEN: ${CEN_VOX} voxels (anterior)"
    if [ "$DMN_VOX" -eq 0 ] || [ "$CEN_VOX" -eq 0 ]; then
        echo "ERROR: empty mask — reference image may be blank"
        exit 1
    fi

    # --- Step 3: Adjust rtdmn.xml for 85 volumes ---
    echo "[Step 3/5] Configuring feedback XML for 85 volumes..."
    sed -i 's/measurements">   150/measurements">    85/' \
        "${TEST_SUBJECT_DIR}/xml/rtdmn.xml" 2>/dev/null

    # Trim design matrix to 85 elements
    python3 -c "
import re, sys
p = '${TEST_SUBJECT_DIR}/xml/rtdmn.xml'
txt = open(p).read()
old = re.search(r'(conditionName=\"Regulation\">)\s*([\s1]+)\s*(</option>)', txt)
if old:
    ones = '\n        ' + '\n        '.join([' '.join(['1']*25) for _ in range(3)] + [' '.join(['1']*10)])
    txt = txt[:old.start(2)] + ones + '\n      ' + txt[old.start(3):]
    open(p, 'w').write(txt)
"

    # --- Step 4: Start MURFI in background, send volumes, launch PsychoPy ---
    echo "[Step 4/5] Starting MURFI..."
    cd "$SCRIPT_PATH"

    # Log MURFI output to file instead of terminal
    MURFI_LOG="${TEST_SUBJECT_DIR}/log/murfi_test.log"
    mkdir -p "/tmp/runtime-$(id -u)" 2>/dev/null || true
    setsid apptainer exec \
        --nv --cleanenv \
        --env DISPLAY="${DISPLAY}" \
        --env XDG_RUNTIME_DIR="/tmp/runtime-$(id -u)" \
        --env QT_QPA_PLATFORM=xcb \
        --env NO_AT_BRIDGE=1 \
        --env QT_LOGGING_RULES="*.debug=false;*.warning=false" \
        --env MURFI_SUBJECTS_DIR="${MURFI_SUBJECTS_DIR}" \
        --env MURFI_SUBJECT_NAME="${MURFI_SUBJECT_NAME}" \
        --bind "${MURFI_SUBJECTS_DIR}:${MURFI_SUBJECTS_DIR}" \
        "$CONTAINER" \
        murfi -f "${TEST_SUBJECT_DIR}/xml/rtdmn.xml" \
        </dev/null > "$MURFI_LOG" 2>&1 &
    MURFI_PID=$!

    # Wait for MURFI to start listening
    echo "  Waiting for MURFI to listen on port 50000..."
    for i in $(seq 1 30); do
        if ss -tln | grep -q ':50000 '; then
            echo "  MURFI ready (log: $MURFI_LOG)"
            break
        fi
        sleep 1
    done

    # --- Step 5: Match real experiment order ---
    # Real flow: MURFI listening → PsychoPy starts (shows baseline) → scanner trigger → volumes stream
    # Test flow: MURFI listening → PsychoPy starts in background → servenii streams after baseline delay

    echo "[Step 5/5] Starting PsychoPy, then simulated scanner..."
    echo ""
    echo "  PsychoPy will start first (like the real experiment)."
    echo "  Fill in the dialog, then press 't' in the PsychoPy window."
    echo "  Volumes will start streaming automatically when 't' is detected."
    echo ""

    # Clean up any old trigger file
    TRIGGER_FILE="/tmp/psychopy_trigger"
    rm -f "$TRIGGER_FILE"

    # Launch PsychoPy in background
    cd "${PROJECT_ROOT}/psychopy/balltask"
    python rt-network_feedback.py &
    PSYCHOPY_PID=$!

    # Watch for trigger file written by PsychoPy when 't' is pressed
    echo "  Waiting for 't' trigger in PsychoPy..."
    while [ ! -f "$TRIGGER_FILE" ]; do
        # Check if PsychoPy exited early (user pressed Escape)
        if ! kill -0 $PSYCHOPY_PID 2>/dev/null; then
            echo "  PsychoPy exited before trigger. Aborting."
            return 1
        fi
        sleep 0.2
    done
    rm -f "$TRIGGER_FILE"

    # Start streaming volumes
    echo "  Trigger detected! Streaming 85 volumes..."
    cd "${TEST_SUBJECT_DIR}"
    apptainer exec \
        --bind "$(pwd):$(pwd)" \
        "$CONTAINER" \
        "$SERVENII" img/img 1 85 1 68 1200 50000 127.0.0.1 &
    SERVE_PID=$!

    # Wait for PsychoPy to finish (it's the foreground task for the operator)
    wait $PSYCHOPY_PID 2>/dev/null || true

    # Cleanup handled by trap
    echo ""
    echo "Level 3 complete."
}

# --- Subcommands for Terminal 2/3 ---

cmd_serve() {
    local n="${1:-2}"
    setup_test_subject 2>/dev/null || true  # ensure data exists
    echo "Serving ${n} volumes..."
    serve_volumes "$n"
    echo "Done sending ${n} volumes."
}

cmd_psychopy() {
    echo "Starting PsychoPy (real MURFI connection on 127.0.0.1:15001)..."
    cd "${PROJECT_ROOT}/psychopy/balltask"
    python rt-network_feedback.py
}

# --- Main ---

check_prereqs

case "$LEVEL" in
    0)       level0 ;;
    1)       level1 ;;
    2)       level2 ;;
    3)       level3 ;;
    serve)   cmd_serve "$2" ;;
    psychopy) cmd_psychopy ;;
    *)
        echo "Usage: bash test_pipeline.sh [0|1|2|3|serve N|psychopy]"
        echo "  0        PsychoPy only (fake data, no MURFI)"
        echo "  1        Smoke test (2 volumes, needs 2 terminals)"
        echo "  2        Resting state + ICA (~30 min, needs 2 terminals)"
        echo "  3        Full feedback loop (needs 3 terminals)"
        echo "  serve N  Helper: send N simulated volumes (for Terminal 2)"
        echo "  psychopy Helper: start PsychoPy (for Terminal 3)"
        ;;
esac
