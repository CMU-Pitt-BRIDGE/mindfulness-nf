#!/bin/bash
# =============================================================================
# Mindfulness NF Session Runner
# Orchestrates MURFI + PsychoPy for a complete scanning session.
# The MR tech runs scans normally; this script handles everything else.
#
# Usage:
#   bash run_session.sh <subject_id> <session> [--dry-run]
#
# Sessions:
#   localizer   Session 1: 2vol + resting state runs
#   nf          Session 2: transfer pre + feedback runs + transfer post
#   process     Between-session processing (ICA + masks)
#
# Example:
#   bash run_session.sh sub-001 localizer
#   bash run_session.sh sub-001 process
#   bash run_session.sh sub-001 nf
# =============================================================================

set -euo pipefail

# --- Environment setup ---
SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_PATH}/../.." && pwd)"
SUBJECTS_DIR="$(cd "${SCRIPT_PATH}/../subjects" && pwd)"
CONTAINER="/opt/murfi/apptainer-images/murfi.sif"

# Source FSL if not already on PATH
if ! command -v flirt &>/dev/null; then
    source /etc/profile.d/fsl.sh 2>/dev/null || true
fi
export FSLOUTPUTTYPE=NIFTI
export PATH="${PROJECT_ROOT}/.venv/bin:${PATH}"

SUBJ="${1:-}"
SESSION="${2:-}"
DRY_RUN="${3:-}"

# MoCo is always ON: 2vol/rtdmn read MoCo volumes, rest reads raw for offline mcflirt.

if [ -z "$SUBJ" ] || [ -z "$SESSION" ]; then
    echo "Usage: bash run_session.sh <subject_id> <session> [--dry-run]"
    echo "  Sessions: localizer | process | nf"
    exit 1
fi

SUBJ_DIR="${SUBJECTS_DIR}/${SUBJ}"
export MURFI_SUBJECTS_DIR="${SUBJECTS_DIR}/"
export MURFI_SUBJECT_NAME="${SUBJ}"

# --- Colors ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

# --- Helpers ---

info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ready() { echo -e "${GREEN}[READY]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()  { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

wait_for_scan() {
    local label="${1:-scan}"
    local log_file="${2:-}"

    echo ""
    echo -e "  ${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "  ${GREEN}  START the scan now: ${NC}${CYAN}${label}${NC}"
    echo -e "  ${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    # Show live volume count, update every 2 seconds
    # The prompt is always the last line — MURFI output goes to the tmux
    # pane above (if using run_step), so it won't interfere here.
    local response=""
    local last_count=-1
    while [[ "$response" != "done" ]]; do
        local received=0
        if [ -n "$log_file" ] && [ -f "$log_file" ]; then
            received=$(grep -c "received image from scanner" "$log_file" 2>/dev/null || true)
        fi
        # Only reprint if count changed
        if [ "$received" != "$last_count" ]; then
            echo -e "  Volumes received: ${GREEN}${received}${NC}"
            last_count="$received"
        fi
        echo -ne "  Type ${YELLOW}done${NC} + Enter: "
        if read -t 2 -r response; then
            response=$(echo "$response" | tr '[:upper:]' '[:lower:]' | xargs)
        else
            response=""
        fi
    done
    echo ""
}

kill_stale_murfi() {
    local pid="$1"
    kill "$pid" 2>/dev/null || true
    for i in $(seq 1 5); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 1
    done
    warn "  PID ${pid} did not exit gracefully — sending SIGKILL"
    kill -9 "$pid" 2>/dev/null || true
}

stop_murfi() {
    local pid="$1"
    if kill -0 "$pid" 2>/dev/null; then
        info "Stopping MURFI (PID ${pid})..."
        kill_stale_murfi "$pid"
    fi
}

wait_for_murfi_ready() {
    # Check for port 50000 (vSend mode) or 15001 (infoserver, always present)
    for i in $(seq 1 20); do
        if ss -tlnp 2>/dev/null | grep -qE ':50000|:15001'; then
            return 0
        fi
        sleep 1
    done
    warn "MURFI may not be listening yet — check terminal output above"
}

configure_moco() {
    # 2vol and rtdmn use vSend with onlyReadMoCo=true (scanner MoCo volumes)
    # rest uses DICOM input (no MoCo setting needed)
    local xml_dir="${SUBJ_DIR}/xml"
    [ -d "$xml_dir" ] || return 0

    for xml_file in "$xml_dir"/2vol.xml "$xml_dir"/rtdmn.xml; do
        [ -f "$xml_file" ] || continue
        sed -i 's|<option name="onlyReadMoCo">[^<]*</option>|<option name="onlyReadMoCo">  true </option>|' "$xml_file"
    done
    info "MoCo: 2vol/rtdmn=ON (vSend), rest=DICOM input"
}

# Run a MURFI step with data verification and retry support.
# Uses tmux to split the terminal: MURFI output on top, operator prompt on bottom.
run_step() {
    local xml="$1"
    local label="$2"
    local min_new="${3:-1}"
    local murfi_log="${SUBJ_DIR}/log/murfi_${label// /_}.log"
    local tmux_session="murfi_scan"

    while true; do
        mkdir -p "${SUBJ_DIR}/img"

        # Kill any leftover tmux session
        tmux kill-session -t "$tmux_session" 2>/dev/null || true

        # Create a detached tmux session that will run MURFI
        # Top pane: MURFI output (operator watches volumes arrive)
        # Bottom pane: operator types "done" (this script's prompt)
        tmux new-session -d -s "$tmux_session" -x "$(tput cols)" -y "$(tput lines)" \
            "tail -f '$murfi_log' 2>/dev/null; read -p 'MURFI exited. Press Enter.'"

        # Start MURFI in background — output goes to log file, tmux tails it
        : > "$murfi_log"  # truncate log
        run_murfi "$xml" "$label" > "$murfi_log" 2>&1 &
        local murfi_pid=$!
        wait_for_murfi_ready

        if [ "$DRY_RUN" != "--dry-run" ]; then
            # Write a small prompt script for the bottom tmux pane
            local prompt_script="/tmp/.murfi_prompt_$$.sh"
            cat > "$prompt_script" <<PROMPT_EOF
#!/bin/bash
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
echo ""
echo -e "  \${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\${NC}"
echo -e "  \${GREEN}  START the scan now: \${NC}\${CYAN}${label}\${NC}"
echo -e "  \${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\${NC}"
echo ""
response=""
last_count=-1
while [ "\$response" != "done" ]; do
    received=\$(grep -c "received image from scanner" "$murfi_log" 2>/dev/null || true)
    if [ "\$received" != "\$last_count" ]; then
        echo -e "  Volumes received: \${GREEN}\${received}\${NC}"
        last_count="\$received"
    fi
    echo -ne "  Type \${YELLOW}done\${NC} + Enter: "
    if read -t 2 -r response; then
        response=\$(echo "\$response" | tr '[:upper:]' '[:lower:]' | xargs)
    else
        response=""
    fi
done
PROMPT_EOF
            chmod +x "$prompt_script"

            # Split tmux: top=MURFI log, bottom=operator prompt
            tmux split-window -t "$tmux_session" -v -l 10 "bash '$prompt_script'"
            tmux select-pane -t "$tmux_session":0.1  # focus bottom pane
            tmux attach-session -t "$tmux_session"

            rm -f "$prompt_script"
        fi

        # Clean up tmux and MURFI
        tmux kill-session -t "$tmux_session" 2>/dev/null || true
        stop_murfi "$murfi_pid"

        # Skip verification in dry-run
        if [ "$DRY_RUN" = "--dry-run" ]; then
            return 0
        fi

        # Verify data was received — use MURFI's own log as source of truth
        local received=0
        if [ -f "$murfi_log" ]; then
            received=$(grep -c "received image from scanner" "$murfi_log" 2>/dev/null || true)
        fi

        if [ "$received" -ge "$min_new" ]; then
            ready "${label}: MURFI received ${received} volume(s)"
            return 0
        fi

        # --- Data not received ---
        echo ""
        warn "No data received during: ${label} (got ${received}, expected >= ${min_new})"
        warn "Check: vSend enabled on scanner, MoCo enabled, ethernet, firewall"

        local choice=""
        while true; do
            echo ""
            echo -ne "  ${YELLOW}(r)${NC}etry  ${YELLOW}(s)${NC}kip  ${YELLOW}(q)${NC}uit: "
            read -r choice
            case "$choice" in
                r|R) break ;;
                s|S) warn "Skipping data verification"; return 0 ;;
                q|Q) fail "Session aborted." ;;
                *) warn "Enter r, s, or q." ;;
            esac
        done
    done
}

check_prereqs() {
    info "Running pre-flight checks..."
    local ok=true

    # Software
    command -v flirt >/dev/null 2>&1       || { warn "FSL not on PATH"; ok=false; }
    command -v apptainer >/dev/null 2>&1   || { warn "Apptainer not found"; ok=false; }
    test -f "$CONTAINER"                   || { warn "MURFI container missing"; ok=false; }
    python -c "import pandas, numpy" 2>/dev/null || { warn "Python deps missing (run: uv sync)"; ok=false; }

    # Subject
    test -d "$SUBJ_DIR" || {
        if [ "$SESSION" != "localizer" ]; then
            warn "Subject $SUBJ does not exist"
            ok=false
        fi
    }

    # Network — only check for sessions that need the scanner
    if [ "$SESSION" = "localizer" ] || [ "$SESSION" = "nf" ]; then
        check_network || ok=false
    fi

    $ok || fail "Pre-flight checks failed. Fix the issues above before continuing."
    info "All checks passed."
}

check_network() {
    local net_ok=true

    info "Checking scanner network..."

    # 1. Is the ethernet interface up with the right subnet?
    if ip addr show 2>/dev/null | grep -q "192.168.2"; then
        info "  Ethernet: 192.168.2.x interface detected"
    else
        warn "  Ethernet: No 192.168.2.x interface found — is ethernet cable plugged in?"
        net_ok=false
    fi

    # 2. Can we ping the scanner?
    if ping -c 1 -W 2 192.168.2.1 &>/dev/null; then
        info "  Scanner (192.168.2.1): reachable"
    else
        warn "  Scanner (192.168.2.1): not reachable — check cable and network config"
        net_ok=false
    fi

    # 3. Is Wi-Fi off? (any wireless interface with an IP = bad)
    if ip addr show 2>/dev/null | grep -A2 'wl[a-z]' | grep -q 'inet '; then
        warn "  Wi-Fi appears to be ON — turn it off to avoid network conflicts"
        net_ok=false
    else
        info "  Wi-Fi: off (good)"
    fi

    # 4. Are our ports free? Kill stale MURFI processes if needed.
    local stale_cleaned=false
    for port in 50000 15001; do
        if ss -tlnp 2>/dev/null | grep -q ":${port}"; then
            local stale_pid
            stale_pid=$(fuser "${port}/tcp" 2>/dev/null | xargs)
            if [ -n "$stale_pid" ]; then
                warn "  Port ${port}: in use by PID ${stale_pid} — killing stale process"
                kill_stale_murfi "$stale_pid"
                stale_cleaned=true
            fi
        fi
    done

    if $stale_cleaned; then
        sleep 2
        # Verify ports actually freed
        for port in 50000 15001; do
            if ss -tlnp 2>/dev/null | grep -q ":${port}"; then
                warn "  Port ${port}: still in use after cleanup — cannot proceed"
                net_ok=false
            else
                info "  Port ${port}: free (after cleanup)"
            fi
        done
    else
        info "  Ports 50000/15001: free"
    fi

    # 5. Can the scanner reach us? Test by briefly listening and checking the interface
    # (We can't test Vsend without actually starting MURFI, but we can verify TCP works)
    if timeout 2 bash -c 'echo | nc -l -p 50000 &>/dev/null' 2>/dev/null; then
        info "  Port 50000: can bind (TCP listener test passed)"
    else
        # nc may not be available, skip gracefully
        true
    fi

    # 6. Check firewall — inspect nftables directly (ufw status is unreliable
    #    when Docker adds its own chains; nftables is what the kernel enforces)
    local fw_ok=true
    if command -v nft &>/dev/null; then
        local nft_input
        nft_input=$(sudo -n nft list chain ip filter ufw-user-input 2>/dev/null || true)
        if [ -n "$nft_input" ]; then
            # nftables has a ufw-user-input chain — check it for port 50000
            if ! echo "$nft_input" | grep -q "dport 50000"; then
                warn "  Firewall (nftables): port 50000 NOT allowed — scanner connections will be dropped"
                warn "  → Fix: sudo ufw allow from 192.168.2.0/24 to any port 50000 proto tcp"
                fw_ok=false
            elif echo "$nft_input" | grep "dport 50000" | grep -q "saddr 192.168.2.1 "; then
                # Rule exists but only for 192.168.2.1 — the MARS reconstruction
                # computer has a different IP than the scanner console
                warn "  Firewall (nftables): port 50000 allowed only from 192.168.2.1"
                warn "  The MARS (reconstruction computer) may have a different IP"
                warn "  → Fix: sudo ufw delete allow from 192.168.2.1 to any port 50000 proto tcp"
                warn "  → Then: sudo ufw allow from 192.168.2.0/24 to any port 50000 proto tcp"
                fw_ok=false
            else
                info "  Firewall (nftables): port 50000 allowed"
            fi
        fi
    fi
    # Fallback: also check ufw status in case nft isn't available
    if $fw_ok && command -v ufw &>/dev/null; then
        if ufw status 2>/dev/null | grep -q "Status: active"; then
            if ! ufw status 2>/dev/null | grep -q "50000.*ALLOW"; then
                warn "  Firewall (ufw): port 50000 may be blocked"
                warn "  → Fix: sudo ufw allow from 192.168.2.0/24 to any port 50000 proto tcp"
                fw_ok=false
            fi
        fi
    fi
    if ! $fw_ok; then
        net_ok=false
    elif [ -n "$nft_input" ] || ufw status 2>/dev/null | grep -q "Status: active"; then
        info "  Firewall: port 50000 allowed"
    fi

    if $net_ok; then
        info "  Network: all checks passed"
    fi

    $net_ok
}

run_murfi() {
    local xml="$1"
    local label="$2"

    info "Starting MURFI for: ${label}"

    # Ensure XDG_RUNTIME_DIR exists with correct permissions for Qt
    mkdir -p -m 0700 "/tmp/runtime-$(id -u)"

    # Kill any stale listeners
    fuser -k 50000/tcp 2>/dev/null || true
    fuser -k 15001/tcp 2>/dev/null || true
    sleep 1

    if [ "$DRY_RUN" = "--dry-run" ]; then
        info "[DRY RUN] Would start MURFI with: $xml"
        return 0
    fi

    # Suppress Qt/GTK warnings that flood the terminal:
    #   SESSION_MANAGER  → "Could not connect to session manager" (D-Bus)
    #   NO_AT_BRIDGE     → "Couldn't connect to accessibility bus" (AT-SPI)
    #   QT_QPA_PLATFORM  → Force X11 backend (prevents Wayland/fallback issues)
    #   QT_LOGGING_RULES → Suppress Qt debug/warning output
    unset SESSION_MANAGER
    export NO_AT_BRIDGE=1

    # setsid + </dev/null detaches MURFI from the controlling terminal.
    # MURFI's Qt GUI writes directly to /dev/tty, bypassing shell redirects.
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
        --bind "${SUBJECTS_DIR}:${SUBJECTS_DIR}" \
        "$CONTAINER" \
        murfi -f "$xml" \
        </dev/null 2>&1
}

run_psychopy() {
    local run_num="$1"
    local feedback="$2"  # "Feedback" or "No Feedback"
    local anchor="${3:-observe}"

    info "Starting PsychoPy: run=${run_num}, feedback=${feedback}"

    if [ "$DRY_RUN" = "--dry-run" ]; then
        info "[DRY RUN] Would start PsychoPy with: run=${run_num} feedback=${feedback}"
        return 0
    fi

    cd "${PROJECT_ROOT}/psychopy/balltask"

    # Launch PsychoPy with pre-filled arguments so operator only has to click OK
    # Args: participant_id, run_number, feedback_condition, session_duration, anchor
    python rt-network_feedback.py \
        "$SUBJ" "$run_num" "$feedback" "15min" "$anchor" &
    PSYCHOPY_PID=$!

    cd "${SCRIPT_PATH}"
    echo "$PSYCHOPY_PID"
}

# --- DICOM receiver for resting state scans ---
# Resting state uses DICOM export (not vSend) because the protocol has MoCo OFF.
# The receiver accepts DICOMs from the scanner and writes them to dicom_input/.

DICOM_INPUT="${SUBJECTS_DIR}/../dicom_input"
DICOM_RECEIVER="${SCRIPT_PATH}/dicom_receiver.py"
DICOM_RECEIVER_PID=""

start_dicom_receiver() {
    mkdir -p "$DICOM_INPUT"
    # Clear old DICOMs from previous runs
    rm -f "$DICOM_INPUT"/*.dcm 2>/dev/null || true

    info "Starting DICOM receiver on port 4006..."
    python "$DICOM_RECEIVER" --port 4006 --output "$DICOM_INPUT" > "${SUBJ_DIR}/log/dicom_receiver.log" 2>&1 &
    DICOM_RECEIVER_PID=$!

    # Wait for it to start listening
    for i in $(seq 1 10); do
        if ss -tlnp 2>/dev/null | grep -q ':4006'; then
            info "DICOM receiver ready (PID ${DICOM_RECEIVER_PID})"
            return 0
        fi
        sleep 1
    done
    warn "DICOM receiver may not be listening — check log"
}

stop_dicom_receiver() {
    if [ -n "$DICOM_RECEIVER_PID" ] && kill -0 "$DICOM_RECEIVER_PID" 2>/dev/null; then
        info "Stopping DICOM receiver (PID ${DICOM_RECEIVER_PID})..."
        kill "$DICOM_RECEIVER_PID" 2>/dev/null || true
        DICOM_RECEIVER_PID=""
    fi
}

count_dicom_files() {
    find "$DICOM_INPUT" -maxdepth 1 -name '*.dcm' -type f 2>/dev/null | wc -l | tr -d '[:space:]'
}

# --- Session: Localizer ---

session_localizer() {
    echo ""
    echo "============================================"
    echo "  SESSION 1: LOCALIZER for ${SUBJ}"
    echo "============================================"
    echo ""

    # Create subject if needed
    if [ ! -d "$SUBJ_DIR" ]; then
        info "Creating subject: ${SUBJ}"
        cd "${SCRIPT_PATH}"
        source createxml.sh "$SUBJ" setup 2>/dev/null
    fi

    # Configure MoCo in subject's XML files
    configure_moco

    # Step 1: Setup
    info "Step 1/4: System setup"
    cd "${SCRIPT_PATH}"
    source feedback.sh "$SUBJ" setup 2>/dev/null || true

    # Step 2: 2vol via vSend (expect at least 2 volumes)
    info "Step 2/4: 2-VOLUME scan (vSend)"
    run_step "${SUBJ_DIR}/xml/2vol.xml" "2-volume scan" 2

    # Start DICOM receiver for resting state scans
    start_dicom_receiver

    # Step 3: Resting state run 1 via DICOM export
    # Scanner runs the sequence, then sends DICOMs (auto-transfer or manual send).
    # MURFI reads from dicom_input/ directory.
    info "Step 3/4: RESTING STATE run 1 (DICOM)"
    rm -f "$DICOM_INPUT"/*.dcm 2>/dev/null || true  # clear between runs
    run_step "${SUBJ_DIR}/xml/rest.xml" "resting state run 1" 10

    # Step 4: Resting state run 2 via DICOM export
    info "Step 4/4: RESTING STATE run 2 (DICOM)"
    rm -f "$DICOM_INPUT"/*.dcm 2>/dev/null || true  # clear between runs
    run_step "${SUBJ_DIR}/xml/rest.xml" "resting state run 2" 10

    # Stop DICOM receiver
    stop_dicom_receiver

    echo ""
    echo "============================================"
    echo "  LOCALIZER SESSION COMPLETE for ${SUBJ}"
    echo "  Next: bash run_session.sh ${SUBJ} process"
    echo "============================================"
}

# --- Session: Between-session processing ---

session_process() {
    echo ""
    echo "============================================"
    echo "  BETWEEN-SESSION PROCESSING for ${SUBJ}"
    echo "  This takes ~25-30 minutes"
    echo "============================================"
    echo ""

    cd "${SCRIPT_PATH}"

    if [ "$DRY_RUN" = "--dry-run" ]; then
        info "[DRY RUN] Would run: extract_rs_networks, process_roi_masks, register"
        return 0
    fi

    info "Step 1/3: Extracting resting state networks (ICA)..."
    info "A dialog will appear — select your resting state runs."
    source feedback.sh "$SUBJ" extract_rs_networks

    info "Step 2/3: Processing ROI masks (DMN & CEN)..."
    source feedback.sh "$SUBJ" process_roi_masks

    info "Step 3/3: Registering masks to study_ref space..."
    source feedback.sh "$SUBJ" register

    # Verify
    echo ""
    if [ -f "${SUBJ_DIR}/mask/dmn.nii" ] && [ -f "${SUBJ_DIR}/mask/cen.nii" ]; then
        dmn=$(fslstats "${SUBJ_DIR}/mask/dmn.nii" -V 2>/dev/null | awk '{print $1}')
        cen=$(fslstats "${SUBJ_DIR}/mask/cen.nii" -V 2>/dev/null | awk '{print $1}')
        echo "============================================"
        echo "  PROCESSING COMPLETE for ${SUBJ}"
        echo "  DMN mask: ${dmn} voxels"
        echo "  CEN mask: ${cen} voxels"
        echo "  Next: bash run_session.sh ${SUBJ} nf"
        echo "============================================"
    else
        fail "Masks not created. Check the ICA output."
    fi
}

# --- Session: Neurofeedback ---

session_nf() {
    echo ""
    echo "============================================"
    echo "  SESSION 2: NEUROFEEDBACK for ${SUBJ}"
    echo "============================================"
    echo ""

    # Verify masks exist
    test -f "${SUBJ_DIR}/mask/dmn.nii" || fail "dmn.nii missing. Run: bash run_session.sh ${SUBJ} process"
    test -f "${SUBJ_DIR}/mask/cen.nii" || fail "cen.nii missing. Run: bash run_session.sh ${SUBJ} process"

    cd "${SCRIPT_PATH}"

    # Configure MoCo in subject's XML files
    configure_moco

    # Ensure murfi_FAKE is False for real scanning
    local psychopy_script="${PROJECT_ROOT}/psychopy/balltask/rt-network_feedback.py"
    if grep -q "murfi_FAKE=True" "$psychopy_script"; then
        warn "murfi_FAKE is True — switching to False for real scanning"
        sed -i 's/murfi_FAKE=True/murfi_FAKE=False/' "$psychopy_script"
    fi

    # Step 1: Setup
    info "System setup..."
    source feedback.sh "$SUBJ" setup 2>/dev/null || true

    # Define the run sequence
    # Format: "run_number:feedback_condition:label"
    local runs=(
        "1:No Feedback:Transfer Pre"
        "1:Feedback:Feedback Run 1"
        "2:Feedback:Feedback Run 2"
        "3:Feedback:Feedback Run 3"
        "4:Feedback:Feedback Run 4"
        "5:Feedback:Feedback Run 5"
        "1:No Feedback:Transfer Post"
        "6:Feedback:Feedback Run 6"
        "7:Feedback:Feedback Run 7"
        "8:Feedback:Feedback Run 8"
        "9:Feedback:Feedback Run 9"
        "10:Feedback:Feedback Run 10"
    )

    local total=${#runs[@]}
    local current=0

    for run_spec in "${runs[@]}"; do
        current=$((current + 1))
        IFS=':' read -r run_num feedback label <<< "$run_spec"

        echo ""
        echo "────────────────────────────────────────────"
        echo "  Run ${current}/${total}: ${label}"
        echo "  Feedback: ${feedback} | Run #: ${run_num}"
        echo "────────────────────────────────────────────"

        # Start MURFI in background, redirect output to log to keep prompt clean
        local murfi_log="${SUBJ_DIR}/log/murfi_${label// /_}.log"
        info "Starting MURFI for: ${label}..."
        run_murfi "${SUBJ_DIR}/xml/rtdmn.xml" "$label" > "$murfi_log" 2>&1 &
        MURFI_PID=$!
        wait_for_murfi_ready

        # Now prompt — MURFI is listening, PsychoPy will launch after confirmation
        if [ "$DRY_RUN" != "--dry-run" ]; then
            wait_for_scan "$label" "$murfi_log"
        fi

        # Start PsychoPy (blocks until task completes)
        cd "${PROJECT_ROOT}/psychopy/balltask"
        if [ "$DRY_RUN" = "--dry-run" ]; then
            info "[DRY RUN] PsychoPy: run=${run_num}, feedback=${feedback}"
        else
            python rt-network_feedback.py "$SUBJ" "$run_num" "$feedback" "15min" "observe" || true
        fi
        cd "${SCRIPT_PATH}"

        # Wait for MURFI to finish
        stop_murfi $MURFI_PID

        info "${label} complete."
    done

    # Restore fake mode for safety after session
    sed -i 's/murfi_FAKE=False/murfi_FAKE=True/' "$psychopy_script"

    echo ""
    echo "============================================"
    echo "  NEUROFEEDBACK SESSION COMPLETE for ${SUBJ}"
    echo "  ${total} runs completed."
    echo "  murfi_FAKE restored to True (safety)"
    echo "============================================"
}

# --- Main ---

check_prereqs

case "$SESSION" in
    localizer) session_localizer ;;
    process)   session_process ;;
    nf)        session_nf ;;
    *)
        echo "Unknown session: $SESSION"
        echo "Usage: bash run_session.sh <subject_id> <session>"
        echo "  Sessions: localizer | process | nf"
        exit 1
        ;;
esac
