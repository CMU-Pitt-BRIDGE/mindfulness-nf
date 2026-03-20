# Mindfulness NF: Complete Setup Guide

Step-by-step instructions for configuring the MURFI workstation and Siemens scanner
for the mindfulness neurofeedback protocol. Written from the 2026-03-20 setup at
Pitt's BRIDGE Center (Siemens Prisma VE11C).

## 1. Workstation Hardware and OS

| Component | Spec |
|---|---|
| OS | Ubuntu 22.04.4 LTS |
| Kernel | 6.8.0-101-generic |
| CPU | 32 cores |
| RAM | 64 GB |
| GPU | NVIDIA (for MURFI Qt GUI, via Apptainer --nv) |
| Scanner interface | `enp3s0f1` (ethernet, 192.168.2.5/24) |

## 2. Network Configuration

### Workstation ethernet

The scanner interface is configured as a static connection named `murfi`:

```
Interface:  enp3s0f1
IP:         192.168.2.5/24
Gateway:    none (direct connection to scanner network)
```

Configured via NetworkManager:

```bash
# View current config
nmcli device show enp3s0f1

# If you need to recreate (as root):
nmcli connection add type ethernet con-name murfi ifname enp3s0f1 \
    ipv4.method manual ipv4.addresses 192.168.2.5/24
```

Wi-Fi must be OFF during scanning to avoid routing conflicts.

### Scanner network

```
Scanner console:     192.168.2.1
MARS (reconstruction): 192.168.2.x (varies, separate from console)
MURFI workstation:   192.168.2.5
```

The console and workstation are connected via a router on the 192.168.2.0/24 subnet.

### Firewall

Ubuntu uses nftables as the kernel packet filter. Docker installs iptables chains
that load nftables rules, creating a situation where `ufw status` reports "inactive"
but the kernel is actively filtering with a default INPUT DROP policy.

Required firewall rules:

```bash
# vSend (real-time volumes from scanner)
sudo ufw allow from 192.168.2.0/24 to any port 50000 proto tcp

# DICOM receiver (resting state bulk export)
sudo ufw allow from 192.168.2.0/24 to any port 4006 proto tcp

# Verify rules are in nftables (the actual enforcer):
sudo nft list chain ip filter ufw-user-input
# Should show lines with "dport 50000" and "dport 4006"
```

**Critical:** Use `192.168.2.0/24` (whole subnet), not `192.168.2.1` (console only).
The MARS reconstruction computer has a different IP than the console.
See `firewall-debugging-2026-03-20.md` for the full debugging account.

### Pre-flight network checks

The session runner performs automated network verification before any scanning session.
These checks run at the start of both localizer and NF sessions:

```bash
# 1. Ethernet interface on scanner subnet
ip addr show | grep "192.168.2"

# 2. Scanner reachable
ping -c 1 -W 2 192.168.2.1

# 3. Wi-Fi off (wireless interfaces with IPs cause routing conflicts)
ip addr show | grep -A2 'wl[a-z]' | grep 'inet '

# 4. Required ports free (kill stale MURFI if needed)
for port in 50000 15001; do
    ss -tlnp | grep ":${port}"
done

# 5. TCP bind test
timeout 2 bash -c 'echo | nc -l -p 50000'

# 6. Firewall — check nftables directly (not ufw status)
nft_input=$(sudo -n nft list chain ip filter ufw-user-input 2>/dev/null || true)
echo "$nft_input" | grep "dport 50000"
# Also checks if rule is too narrow (single IP vs subnet)
echo "$nft_input" | grep "dport 50000" | grep "saddr 192.168.2.1 "
```

If any check fails, the script reports the issue and the fix command, then exits.

## 3. Software Installation

### MURFI (Apptainer container)

MURFI v2.1.1 runs in an Apptainer container at `/opt/murfi/`.

```
/opt/murfi/
├── apptainer-images/
│   └── murfi.sif              # Main container (built from ghcr.io/gablab/murfi:v2.1.1)
├── bin/
│   ├── murfi_apptainer        # Wrapper script (handles X11 forwarding)
│   └── murfi_wrapper          # Alternative wrapper (Docker-based, not used)
├── docker-images/
│   └── murfi.Dockerfile       # Dockerfile for building the image
└── subjects/                  # System-level subjects (not used, project has its own)
```

To build/update the container:

```bash
# Pull the Docker image and convert to Apptainer SIF
# (requires Docker to be installed for the initial pull)
docker pull ghcr.io/gablab/murfi:v2.1.1
apptainer build /opt/murfi/apptainer-images/murfi.sif docker-daemon://ghcr.io/gablab/murfi:v2.1.1
```

The container needs X11 libraries for MURFI's Qt GUI. The Dockerfile adds them
on top of the base MURFI image:

```dockerfile
FROM ohinds/murfi:framewise-displacement

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
    '^libxcb.*-dev' libx11-xcb-dev libglu1-mesa-dev libxrender-dev \
    libxi-dev libxkbcommon-dev libxkbcommon-x11-dev && \
    apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

ENTRYPOINT ["murfi"]
```

### How the session runner launches MURFI

MURFI is launched inside the Apptainer container with GPU passthrough and the
subject data directory bind-mounted. Several environment variables suppress
Qt/GTK warnings that would otherwise flood the terminal:

```bash
# Host-side: suppress D-Bus and accessibility warnings before launching
unset SESSION_MANAGER   # Prevents "Could not connect to session manager"
export NO_AT_BRIDGE=1   # Prevents "Couldn't connect to accessibility bus"

# Ensure Qt runtime directory exists
mkdir -p -m 0700 "/tmp/runtime-$(id -u)"

setsid apptainer exec \
    --nv \                                          # NVIDIA GPU passthrough
    --cleanenv \                                    # Clean environment
    --env DISPLAY="${DISPLAY}" \                     # X11 display
    --env XDG_RUNTIME_DIR="/tmp/runtime-$(id -u)" \ # Qt runtime dir
    --env QT_QPA_PLATFORM=xcb \                     # Force X11 backend (not Wayland)
    --env NO_AT_BRIDGE=1 \                          # Suppress AT-SPI inside container
    --env QT_LOGGING_RULES="*.debug=false;*.warning=false" \  # Suppress Qt warnings
    --env MURFI_SUBJECTS_DIR="${SUBJECTS_DIR}/" \    # Where subjects live
    --env MURFI_SUBJECT_NAME="${SUBJ}" \             # Current subject
    --bind "${SUBJECTS_DIR}:${SUBJECTS_DIR}" \       # Mount subjects into container
    /opt/murfi/apptainer-images/murfi.sif \
    murfi -f "$xml" \                                # MURFI with XML config
    </dev/null 2>&1                                  # Detach from terminal
```

Key details:
- `--nv` enables NVIDIA GPU passthrough (required for MURFI's Qt GUI)
- `--cleanenv` strips the environment; required vars are passed explicitly via `--env`
- `setsid` and `</dev/null` detach MURFI from the controlling terminal (MURFI's Qt
  GUI writes directly to `/dev/tty`, bypassing shell redirects)
- The XML file path must be an absolute path inside the bind mount
- MURFI's environment variables (`MURFI_SUBJECTS_DIR`, `MURFI_SUBJECT_NAME`) tell it
  where to find the subject directory, images, masks, and transforms

### Suppressing Qt/GTK warnings

MURFI uses a Qt GUI that produces several harmless but noisy warnings when run
inside an Apptainer container on a GNOME desktop. Without suppression, the
terminal fills with messages like:

```
(murfi:1234): Gtk-WARNING **: cannot open display
Gdk-Message: Unable to connect to the accessibility bus
Qt: Session management error: Could not open network socket
```

The environment variables above suppress these. Here is what each one does:

| Variable | Warning it suppresses | Where to set |
|---|---|---|
| `unset SESSION_MANAGER` | "Could not open network socket" / D-Bus session errors | Host shell, before `apptainer exec` |
| `NO_AT_BRIDGE=1` | "Couldn't connect to accessibility bus" (AT-SPI / dbus) | Both host (`export`) and container (`--env`) |
| `QT_QPA_PLATFORM=xcb` | Qt platform plugin errors, Wayland fallback warnings | Container (`--env`) |
| `QT_LOGGING_RULES=*.debug=false;*.warning=false` | All remaining Qt debug/warning messages | Container (`--env`) |
| `XDG_RUNTIME_DIR=/tmp/runtime-$(id -u)` | "XDG_RUNTIME_DIR not set" / Qt socket errors | Container (`--env`), must `mkdir -p` first |

The `--cleanenv` flag is essential: without it, the host's full environment leaks
into the container, causing additional conflicts (wrong library paths, locale errors,
D-Bus socket mismatches). With `--cleanenv`, only the explicitly passed `--env`
variables are visible inside the container.

Note: even with all suppressions, MURFI's Qt GUI may still write some output
directly to `/dev/tty` (the controlling terminal). The `setsid` and `</dev/null`
combination detaches MURFI from the terminal to prevent this from interfering
with the operator's `read` prompt.

### Group membership

The operator user must be in the `murfi` and `fsl` groups:

```bash
sudo usermod -aG murfi,fsl young-lab
# Log out and back in for group changes to take effect
```

### Apptainer

```bash
# Check version
apptainer --version   # 1.3.4

# Install if needed (Ubuntu 22.04):
sudo add-apt-repository -y ppa:apptainer/ppa
sudo apt update
sudo apt install -y apptainer
```

### FSL

FSL 6.0.7 is installed at `/opt/fsl` and loaded via `/etc/profile.d/fsl.sh`
for users in the `fsl` group. The session runner sources it automatically:

```bash
if ! command -v flirt &>/dev/null; then
    source /etc/profile.d/fsl.sh 2>/dev/null || true
fi
export FSLOUTPUTTYPE=NIFTI  # MURFI needs uncompressed NIfTI
```

### Python dependencies

The project uses `uv` for Python dependency management:

```bash
cd /home/young-lab/code/mindfulness-nf
uv sync   # Creates .venv/ and installs all dependencies
```

The session runner adds the venv to PATH automatically:

```bash
export PATH="${PROJECT_ROOT}/.venv/bin:${PATH}"
```

Key Python packages: `pynetdicom` (DICOM receiver), `pydicom` (DICOM parsing),
`pandas`, `numpy` (ICA component selection).

## 4. Scanner Configuration (Siemens VE11C)

### 4a. vSend (C2P agreement required)

vSend pushes reconstructed volumes to the workstation in real-time via TCP.
Used for: 2vol, feedback, and transfer runs (all have Motion correction ON).

The C2P agreement allows access to the Siemens ICE functor `SendExternalFunctor`.
Configuration is done in the Siemens service software or via the sequence protocol.

vSend destination: `192.168.2.5:50000`

The scanner sends both raw and MoCo volumes. MURFI's `onlyReadMoCo=true` filters
to keep only MoCo volumes.

### 4b. DICOM export

Standard DICOM C-STORE for bulk export after scan completion. Used for resting
state runs (Motion correction OFF, no vSend).

#### Add the DICOM node on the scanner

Open **Siemens Med Service Software** (Internet Explorer on scanner console):

1. Go to **DICOM > Network Nodes**
2. Fill in:
   - **edit Name:** `MURFI_DICOM`
   - **Host:** `MURFI` (must be in the scanner's hosts/routing table, mapped to 192.168.2.5)
   - **edit AE Title:** `MURFI` → click **Add**
   - **Port Number:** `4006`
   - **Connection mode:** select **Unsecure**
   - Check **Storage** under Supported DICOM services
3. Click **Verification** to test (requires the DICOM receiver to be running)
4. Click **Save**

#### Add the hostname mapping

If the Verification fails with "AE title did not respond", the hostname `MURFI`
needs to be resolved to `192.168.2.5`. On the Siemens service software:

1. Go to **TCP/IP LAN** or **Routing**
2. Add the hostname mapping: `MURFI` → `192.168.2.5`
3. Save and restart the application if prompted

Note: "restart application pending" appears at the bottom of the screen after
changes. Save and click Finish to apply.

#### AutoTransfers (optional)

Under **Service > AutoTransfers**, you can set up automatic DICOM export rules.
This requires a service software license. If you see "the selected function is
not available for your Service Software License", use manual transfer instead.

#### Manual transfer (Patient Browser)

After each resting state scan:

1. Open **Patient Browser** on the scanner console
2. Select the resting state series
3. Right-click → **Send** (or Transfer menu)
4. Choose **MURFI_DICOM** as destination
5. DICOMs are sent to the workstation

### 4c. Scanner sequences (from rt-BPD protocol PDFs)

The protocol uses these sequences under `\\USER\Auerbach\REMIND`:

| Protocol | Sequence | TR | Meas | MoCo | Data path | vSend needed |
|---|---|---|---|---|---|---|
| LOC3 / RT15 | `func-bold_task-2vol_run-01` | 1200ms | 2 | ON | vSend | Yes |
| LOC3 | `func-bold_task-rest_run-01` | 1200ms | 250 | OFF | DICOM | No |
| LOC3 | `func-bold_task-rest_run-02` | 1200ms | 250 | OFF | DICOM | No |
| RT15 | `func-bold_task-restpre_run-01` | 1200ms | 250 | OFF | DICOM | No |
| RT15 | `func-bold_task-restpre_run-02` | 1200ms | 250 | OFF | DICOM | No |
| RT15 | `func-bold_task-transferpre_run-01` | 1200ms | 150 | ON | vSend | Yes |
| RT15 | `func-bold_task-feedback_run-01..10` | 1200ms | 150 | ON | vSend | Yes |
| RT15 | `func-bold_task-transferpost_run-01` | 1200ms | 150 | ON | vSend | Yes |

All EPI sequences: 2mm iso, 72 slices, 128x128, GRAPPA 2.

vSend must be enabled on the scanner for sequences with MoCo ON. Resting state
sequences do not need vSend — they use DICOM export after scan completion.

## 5. DICOM Receiver

A Python DICOM receiver accepts C-STORE requests from the scanner and writes
DICOM files to `murfi/dicom_input/`. It supports both C-ECHO (verification)
and C-STORE (image storage):

```python
from pynetdicom import AE, evt, StoragePresentationContexts, VerificationPresentationContexts

ae = AE(ae_title="MURFI")
ae.supported_contexts = VerificationPresentationContexts + StoragePresentationContexts

def handle_store(event, output_dir):
    ds = event.dataset
    ds.file_meta = event.file_meta
    filename = f"{ds.SOPInstanceUID}.dcm"
    ds.save_as(Path(output_dir) / filename)
    return 0x0000  # Success

handlers = [(evt.EVT_C_STORE, handle_store, [output_dir])]
ae.start_server(("0.0.0.0", 4006), evt_handlers=handlers, block=True)
```

Configuration:
- **AE Title:** `MURFI` (must match scanner Network Nodes config)
- **Port:** `4006` (unprivileged, no root needed)
- **Output:** `murfi/dicom_input/` (MURFI polls this directory when `imageSource=DICOM`)

For manual testing:

```bash
cd /home/young-lab/code/mindfulness-nf
.venv/bin/python murfi/scripts/dicom_receiver.py --port 4006
```

### How the session runner manages the receiver

The DICOM receiver is started/stopped automatically around resting state steps:

```bash
start_dicom_receiver() {
    mkdir -p "$DICOM_INPUT"
    rm -f "$DICOM_INPUT"/*.dcm    # Clear old DICOMs

    python dicom_receiver.py --port 4006 --output "$DICOM_INPUT" \
        > "${SUBJ_DIR}/log/dicom_receiver.log" 2>&1 &
    DICOM_RECEIVER_PID=$!

    # Wait for it to start listening
    for i in $(seq 1 10); do
        ss -tlnp | grep -q ':4006' && return 0
        sleep 1
    done
}

stop_dicom_receiver() {
    kill "$DICOM_RECEIVER_PID" 2>/dev/null || true
}
```

Between resting state runs, old DICOMs are cleared so MURFI starts fresh:

```bash
rm -f "$DICOM_INPUT"/*.dcm    # Clear between runs
```

## 6. Running the Pipeline

### Session 1: Localizer

```bash
cd /home/young-lab/code/mindfulness-nf/murfi/scripts
bash run_session.sh sub-001 localizer
```

The localizer session orchestrates four steps. Here is what happens internally:

#### Step 1: System setup and pre-flight checks

```bash
# Source the setup step from feedback.sh (pings scanner, shows connectivity info)
source feedback.sh "$SUBJ" setup

# Automated checks:
# - Ethernet 192.168.2.x interface exists
# - Scanner (192.168.2.1) responds to ping
# - Wi-Fi is off
# - Ports 50000 and 15001 are free
# - Firewall allows port 50000 (nftables check)
```

#### Step 2: 2-volume reference scan (vSend)

The script ensures MoCo settings are correct, then starts MURFI:

```bash
# Enforce MoCo settings in subject XML files
# 2vol.xml and rtdmn.xml: onlyReadMoCo=true (use scanner MoCo volumes)
# rest.xml: DICOM input (no MoCo setting needed)
for xml_file in "$xml_dir"/2vol.xml "$xml_dir"/rtdmn.xml; do
    sed -i 's|<option name="onlyReadMoCo">[^<]*</option>|<option name="onlyReadMoCo">  true </option>|' "$xml_file"
done
```

MURFI is launched in the background. The script waits for it to bind to a port,
then prompts the operator:

```bash
# Start MURFI with the 2vol XML config
run_murfi "${SUBJ_DIR}/xml/2vol.xml" "2-volume scan" > "$murfi_log" 2>&1 &
murfi_pid=$!

# Wait until MURFI is listening (checks port 50000 or 15001)
for i in $(seq 1 20); do
    ss -tlnp | grep -qE ':50000|:15001' && break
    sleep 1
done

# Operator starts the scan on the scanner console, then types "done"
wait_for_scan "2-volume scan" "$murfi_log"

# Stop MURFI
kill "$murfi_pid"
```

After the scan, the script verifies data was received by checking MURFI's log:

```bash
received=$(grep -c "received image from scanner" "$murfi_log" || true)
if [ "$received" -ge 2 ]; then
    echo "2-volume scan: MURFI received ${received} volume(s)"
else
    echo "No data received — retry, skip, or quit?"
fi
```

#### Step 3-4: Resting state runs (DICOM)

The DICOM receiver is started before the first resting state run and stopped after
the last:

```bash
# Start receiver — listens on port 4006, writes to dicom_input/
start_dicom_receiver

# Run 1: clear old DICOMs, start MURFI in DICOM mode, wait for operator
rm -f "$DICOM_INPUT"/*.dcm
run_step "${SUBJ_DIR}/xml/rest.xml" "resting state run 1" 10

# Run 2: clear, repeat
rm -f "$DICOM_INPUT"/*.dcm
run_step "${SUBJ_DIR}/xml/rest.xml" "resting state run 2" 10

# Stop receiver
stop_dicom_receiver
```

For each resting state run, the operator workflow is:
1. Script starts MURFI (reads from `dicom_input/` directory)
2. Operator starts the scan on the scanner console
3. Scan completes (~5 minutes)
4. Operator sends DICOMs from Patient Browser to MURFI_DICOM
5. MURFI picks up the DICOMs and saves them as NIfTI to `img/`
6. Operator types `done`

### Between sessions: Processing

```bash
bash run_session.sh sub-001 process
```

The processing session runs three pipeline steps (~25 minutes total):

```bash
# Step 1: ICA — extracts resting state networks using MELODIC
# A dialog appears for the operator to select which resting state runs to use.
# Runs mcflirt (motion correction), skull stripping (BET), and multi-run ICA.
source feedback.sh "$SUBJ" extract_rs_networks

# Step 2: Mask generation — selects DMN and CEN components from ICA output.
# Uses rsn_get.py which correlates ICA components with template networks
# and selects the most bilateral CEN component.
# Masks are thresholded to ~2000 voxels each.
source feedback.sh "$SUBJ" process_roi_masks

# Step 3: Registration — transforms masks from resting state space to
# study_ref (2vol) space using FLIRT, with 4-voxel brain mask erosion.
# Creates the final dmn.nii and cen.nii that MURFI uses during feedback.
source feedback.sh "$SUBJ" register
```

Verification after processing:

```bash
if [ -f "${SUBJ_DIR}/mask/dmn.nii" ] && [ -f "${SUBJ_DIR}/mask/cen.nii" ]; then
    dmn=$(fslstats "${SUBJ_DIR}/mask/dmn.nii" -V | awk '{print $1}')
    cen=$(fslstats "${SUBJ_DIR}/mask/cen.nii" -V | awk '{print $1}')
    echo "DMN mask: ${dmn} voxels"
    echo "CEN mask: ${cen} voxels"
fi
```

### Session 2: Neurofeedback

```bash
bash run_session.sh sub-001 nf
```

The NF session runs 12 runs total (transfer pre, 10 feedback, transfer post).
Before starting, it verifies masks exist and switches PsychoPy to real scanning mode:

```bash
# Verify masks are ready
test -f "${SUBJ_DIR}/mask/dmn.nii" || fail "dmn.nii missing"
test -f "${SUBJ_DIR}/mask/cen.nii" || fail "cen.nii missing"

# Switch PsychoPy from fake mode to real scanning
sed -i 's/murfi_FAKE=True/murfi_FAKE=False/' rt-network_feedback.py
```

The run sequence is defined as an array:

```bash
runs=(
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
```

For each run, the script:

```bash
for run_spec in "${runs[@]}"; do
    IFS=':' read -r run_num feedback label <<< "$run_spec"

    # 1. Start MURFI in background with rtdmn.xml (vSend, port 50000)
    run_murfi "${SUBJ_DIR}/xml/rtdmn.xml" "$label" > "$murfi_log" 2>&1 &
    MURFI_PID=$!
    wait_for_murfi_ready

    # 2. Operator starts scan on console, types "done" when ready
    wait_for_scan "$label" "$murfi_log"

    # 3. Launch PsychoPy feedback task (blocks until task completes)
    python rt-network_feedback.py "$SUBJ" "$run_num" "$feedback" "15min" "observe"

    # 4. Stop MURFI
    stop_murfi $MURFI_PID
done
```

PsychoPy connects to MURFI's infoserver (port 15001) to read real-time activation
values. MURFI computes weighted-average activation in DMN and CEN ROIs using an
incremental GLM, and sends the values to PsychoPy which drives the ball feedback display.

After all runs, PsychoPy is switched back to fake mode for safety:

```bash
sed -i 's/murfi_FAKE=False/murfi_FAKE=True/' rt-network_feedback.py
```

## 7. MURFI XML Configuration

Each subject has three XML configs in `subjects/sub-XXX/xml/`:

| File | Input | Port | MoCo | Measurements | Processing |
|---|---|---|---|---|---|
| `2vol.xml` | vSend | 50000 | `onlyReadMoCo=true` | 20 | mosaic only |
| `rest.xml` | DICOM | — | — | 250 | mosaic only |
| `rtdmn.xml` | vSend | 50000 | `onlyReadMoCo=true` | 150 | full pipeline |

### 2vol.xml (reference scan)

Minimal processing — just receives 2 volumes for registration reference:

```xml
<scanner>
  <option name="port">           50000 </option>
  <option name="receiveImages">  true </option>
  <option name="onlyReadMoCo">   true </option>
  <option name="measurements">   20 </option>
  <option name="saveImages">     true </option>
</scanner>
```

### rest.xml (resting state)

DICOM input — MURFI polls a directory for DICOM files:

```xml
<scanner>
  <option name="imageSource">    DICOM </option>
  <option name="inputDicomDir">  /home/young-lab/code/mindfulness-nf/murfi/dicom_input </option>
  <option name="tr">             1.2 </option>
  <option name="measurements">   250 </option>
  <option name="saveImages">     true </option>
</scanner>
```

### rtdmn.xml (real-time feedback)

Full processing pipeline: mask loading, incremental GLM, current activation,
ROI combination for DMN and CEN feedback:

```xml
<scanner>
  <option name="port">           50000 </option>
  <option name="receiveImages">  true </option>
  <option name="onlyReadMoCo">   true </option>
  <option name="measurements">   150 </option>
</scanner>

<processor>
  <module name="mosaic"> ... </module>
  <module name="mask-gen">
    <option name="roiID"> brain </option>
    <option name="threshold"> 0.5 </option>
  </module>
  <module name="mask-load">
    <option name="roiID"> dmn </option>
    <option name="filename"> dmn </option>
    <option name="align"> true </option>
  </module>
  <module name="mask-load">
    <option name="roiID"> cen </option>
    <option name="filename"> cen </option>
    <option name="align"> true </option>
  </module>
  <module name="incremental-glm">
    <option name="maskRoiID"> brain </option>
    <design>
      <option name="modelMotionDerivatives"> true </option>
      <option name="maxTrendOrder"> 1 </option>
      <option name="conditionShift"> 25 </option>
    </design>
  </module>
  <module name="current-activation">
    <option name="modelFitModuleID"> incremental-glm </option>
    <option name="numDataPointsForErrEst"> 25 </option>
  </module>
  <module name="roi-combine">
    <output> infoserver </output>
    <option name="maskRoiID"> dmn </option>
    <option name="method"> weighted-ave </option>
  </module>
  <module name="roi-combine">
    <output> infoserver </output>
    <option name="maskRoiID"> cen </option>
    <option name="method"> weighted-ave </option>
  </module>
</processor>
```

The `infoserver` output sends ROI values to PsychoPy on port 15001.

### Creating a new subject

Templates are in `subjects/template/xml/xml_vsend/`. New subjects are created
from these templates:

```bash
cd murfi/scripts
source createxml.sh sub-002 setup
```

This creates the directory structure and copies XML templates:

```bash
mkdir ${subject_dir}/{img,log,mask,mask/mni,xfm,xml,rest,fsfs,qc}
cp -r ${subject_dir}template/xml/xml_vsend/* ${subject_dir}$subject/xml/
```

## 8. Troubleshooting

### "SendExternalFunctor::sendData() — Cannot connect to 192.168.2.5:50000"

Scanner can't reach MURFI. Check in order:
1. Is MURFI running? (`ss -tlnp | grep 50000`)
2. Firewall? (`sudo nft list chain ip filter ufw-user-input | grep 50000`)
3. Is the firewall rule for the whole subnet, not just 192.168.2.1?
4. Is Wi-Fi off? (`ip addr show | grep 'wl[a-z]'`)

### DICOM Verification fails on scanner

1. Is the receiver running? (`ss -tlnp | grep 4006`)
2. Firewall rule for port 4006? (`sudo nft list chain ip filter ufw-user-input | grep 4006`)
3. Hostname `MURFI` resolves to 192.168.2.5 on the scanner?
4. Did you restart the application after adding the hostname mapping?

### MURFI starts but receives 0 volumes

- Check `onlyReadMoCo` setting matches the sequence. MoCo ON sequences need `true`,
  resting state (MoCo OFF) needs DICOM input instead.
- Check the MURFI log: `subjects/sub-XXX/log/log.rtl`
- "ignoring non-MoCo image" without "got MoCo image" = scanner MoCo is OFF but MURFI
  expects it ON. Either enable MoCo on the sequence or set `onlyReadMoCo=false`.

### ufw says "inactive" but ports are blocked

Docker installs iptables chains that load nftables rules. The kernel enforces nftables
regardless of ufw daemon status. Always check:
```bash
sudo nft list chain ip filter ufw-user-input
```
See `firewall-debugging-2026-03-20.md` for the full analysis.

### MURFI output floods the terminal

MURFI's Qt GUI writes directly to `/dev/tty`, bypassing shell stdout redirects.
This is a known issue. The status line shows the volume count but MURFI output
may interleave. Type `done` when the scan finishes — `read` captures stdin
regardless of terminal output.

### Stale MURFI process blocking ports

The session runner automatically kills stale processes on ports 50000 and 15001:

```bash
fuser -k 50000/tcp 2>/dev/null || true
fuser -k 15001/tcp 2>/dev/null || true
sleep 1
```

If ports are still blocked after cleanup, find and kill the process manually:

```bash
fuser 50000/tcp    # Shows PID
kill <PID>
```

### DICOM files received but MURFI doesn't process them

Verify the `inputDicomDir` path in `rest.xml` matches the actual DICOM receiver
output directory. The path must be absolute and accessible inside the Apptainer
container (bind-mounted via `--bind`).

```bash
grep inputDicomDir subjects/sub-XXX/xml/rest.xml
ls murfi/dicom_input/*.dcm | wc -l
```
