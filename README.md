# Mindfulness Real-Time fMRI Neurofeedback

Real-time fMRI neurofeedback pipeline targeting DMN and CEN for mindfulness training
in adolescents with mood disorders.

**Clinical trial:** NCT05617495
**PI:** Dr. Kymberly Young (Pitt Psychiatry)
**Collaborators:** Dr. Danella Hafeman (Pitt), Dr. Sue Whitfield-Gabrieli & Clemens Bauer (Northeastern)

## Architecture

Single-machine setup: MURFI and PsychoPy run on the same Ubuntu workstation.
Two data paths from the scanner, matching the rt-BPD protocol:

```
Scanner (VE11C, 192.168.2.1)
    │
    ├── Vsend (TCP, port 50000)          ← 2vol, feedback, transfer runs (MoCo ON)
    │       ▼
    │   MURFI (apptainer, localhost)
    │       │ XML infoserver (TCP, port 15001)
    │       ▼
    │   PsychoPy (localhost, 127.0.0.1)
    │       │ Visual/haptic feedback
    │       ▼
    │   Participant
    │
    └── DICOM export (TCP, port 4006)    ← resting state runs (MoCo OFF)
            ▼
        dicom_receiver.py (pynetdicom, AE title: MURFI)
            ▼
        murfi/dicom_input/ → MURFI reads for offline processing
```

## Directory Structure

```
mindfulness-nf/
├── murfi/
│   ├── scripts/           # Pipeline scripts (run from here)
│   │   ├── feedback.sh    # Main orchestrator (setup, 2vol, rest, ICA, masks, register, feedback)
│   │   ├── launch_murfi.sh # GUI launcher (zenity)
│   │   ├── createxml.sh   # Subject directory setup
│   │   ├── rsn_get.py     # DMN/CEN IC selection with bilateral CEN analysis
│   │   ├── servedata.sh   # Scanner simulator
│   │   ├── masks/         # MNI templates and network masks (neurological orientation)
│   │   └── fsl_scripts/   # MELODIC ICA FSF templates
│   ├── subjects/
│   │   └── template/      # Subject directory template
│   │       ├── xml/
│   │       │   ├── xml_vsend/  # Scanner input via Vsend (default)
│   │       │   └── xml_dcm/    # Scanner input via DICOM export (alternative)
│   │       └── mask/mni/       # Template masks in MNI space
│   └── logs/              # Session logs
├── psychopy/
│   ├── balltask/          # Real-time neurofeedback task
│   │   ├── rt-network_feedback.py          # Ball feedback (PsychoPy)
│   │   ├── murfi_activation_communicator.py # MURFI socket client
│   │   └── bids_tsv_convert_balltask.py    # BIDS conversion
│   ├── self_reference/    # Self-referential encoding task
│   └── environment.yml    # Conda environment spec
└── docs/
```

## Quick Start

### 1. Create a new subject
```bash
cd murfi/scripts
source createxml.sh sub-001 setup
```

### 2. Launch the GUI
```bash
cd murfi/scripts
bash launch_murfi.sh
```

### 3. Or run steps manually
```bash
cd murfi/scripts

# Setup & connectivity check
source feedback.sh sub-001 setup

# Receive 2-volume reference scan
source feedback.sh sub-001 2vol

# Receive resting state data (use servedata.sh to simulate)
source feedback.sh sub-001 resting_state

# Extract networks via ICA (~25 min)
source feedback.sh sub-001 extract_rs_networks

# Generate personalized DMN/CEN masks
source feedback.sh sub-001 process_roi_masks

# Register masks to 2vol space for MURFI
source feedback.sh sub-001 register

# Run real-time feedback
source feedback.sh sub-001 feedback
```

### 4. Simulate scanner data (for testing)
```bash
cd murfi/scripts
source servedata.sh 250vol
```

### 5. Run PsychoPy feedback
```bash
cd psychopy/balltask
python rt-network_feedback.py
```

## Key Changes from Original Pipeline

This pipeline is based on the rt-BPD codebase (Clemens Bauer, 2025) with these adaptations:

| Change | Why |
|---|---|
| Neurological orientation throughout | Eliminates LPS/neurological confusion that caused registration errors |
| Melodic IC resampling (applywarp) | Fixes 74→68 slice dimension mismatch in multi-run ICA |
| Bilateral CEN selection | Lateralization analysis picks most bilateral CEN component |
| 4-voxel brain mask erosion | Keeps masks safely inside brain boundary |
| Robust reference selection (ls -v) | Prevents wrong-file selection vs fragile ls -t |
| Safe file operations (cp, rm -rf) | Prevents data loss from destructive mv |
| ICA overwrite protection | Zenity dialog prevents accidental 25-min re-runs |
| Apptainer container at /opt/murfi | Uses system-installed MURFI v2.1.1 |
| Single-machine (localhost) | PsychoPy connects to MURFI on 127.0.0.1 |
| BIDS subject IDs (sub-NNN) | Standard naming convention |

## Dependencies

- **MURFI v2.1.1**: `/opt/murfi/apptainer-images/murfi.sif`
- **Apptainer** (or Singularity): Container runtime
- **FSL 6+**: Registration, ICA, brain extraction
- **Python 3.9+**: rsn_get.py (pandas, numpy)
- **PsychoPy**: Neurofeedback display (see psychopy/environment.yml)
- **zenity**: GUI dialogs for launch_murfi.sh

## Scanner Input

Both paths are used per the rt-BPD protocol:

| Sequence | Input method | Scanner MoCo | MURFI config |
|---|---|---|---|
| 2vol (reference) | Vsend (port 50000) | ON | `onlyReadMoCo=true` |
| Resting state | DICOM export (port 4006) | OFF | `imageSource=DICOM` |
| Feedback/transfer | Vsend (port 50000) | ON | `onlyReadMoCo=true` |

**Vsend** requires C2P agreement (already in place). Delivers MoCo volumes in real-time.

**DICOM export** uses standard DICOM C-STORE. A Python receiver (`murfi/scripts/dicom_receiver.py`)
listens on port 4006, AE title `MURFI`. Configured on the scanner as node `MURFI_DICOM`.
After a resting state scan, send from the Patient Browser to `MURFI_DICOM`.

**Firewall:** Ports 50000 and 4006 must be open for the scanner subnet (192.168.2.0/24).
See `murfi/docs/firewall-debugging-2026-03-20.md` for the nftables/ufw gotcha.

## BIDS Subject ID Convention

Subjects are named `sub-NNN` (e.g., `sub-001`, `sub-002`).
Subject directories are created under `murfi/subjects/`.
