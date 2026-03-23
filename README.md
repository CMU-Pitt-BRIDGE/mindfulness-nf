# Mindfulness Real-Time fMRI Neurofeedback

Real-time fMRI neurofeedback targeting the Default Mode Network (DMN) and Central Executive Network (CEN) for mindfulness training in adolescents with mood disorders.

**Clinical trial:** NCT05617495
**PI:** Dr. Kymberly Young (Pitt Psychiatry)
**Collaborators:** Dr. Danella Hafeman (Pitt), Dr. Sue Whitfield-Gabrieli and Clemens Bauer (Northeastern)

## Quick Start

### Install

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/), FSL 6+, and Apptainer.

```bash
git clone git@github.com:eduardojdiniz/mindfulness-nf.git
cd mindfulness-nf
uv venv --python 3.13
uv sync --extra dev
```

### Launch the TUI

```bash
uv run mindfulness-nf
```

Or double-click the desktop icon (installed at `~/Desktop/mindfulness-nf.desktop`).

### Test without a scanner

```bash
# Level 0: PsychoPy ball task with fake data (no MURFI)
bash murfi/scripts/test_pipeline.sh 0

# Level 1: MURFI receives 2 simulated volumes (needs 2nd terminal for: test_pipeline.sh serve 2)
bash murfi/scripts/test_pipeline.sh 1

# Level 3: Full loop, one terminal (MURFI + simulated volumes + PsychoPy)
bash murfi/scripts/test_pipeline.sh 3

# TUI dry-run mode (simulated volumes, no scanner or MURFI)
uv run mindfulness-nf --test
```

### Run the test suite

```bash
uv run pytest tests/ -x -q
```

349 tests. No scanner, MURFI, or display required.

## Architecture

Single-machine setup. MURFI (real-time analysis) and PsychoPy (participant feedback) run on the same Ubuntu workstation connected to a Siemens scanner via dedicated Ethernet.

```
Scanner (VE11C, 192.168.2.1)
    |
    +-- Vsend (TCP 50000)              2vol, feedback, transfer runs (MoCo ON)
    |       |
    |   MURFI (Apptainer container)
    |       | infoserver (TCP 15001)
    |       |
    |   PsychoPy ball task
    |       |
    |   Participant display
    |
    +-- DICOM export (TCP 4006)        Resting state runs (MoCo OFF)
            |
        dicom_receiver.py (pynetdicom, AE title: MURFI)
```

### Data transfer modes

| Scan Type | Transfer | Port | Scanner MoCo |
|-----------|----------|------|-------------|
| 2-volume reference | Vsend | 50000 | ON |
| Resting state | DICOM export | 4006 | OFF |
| Feedback/Transfer | Vsend | 50000 | ON |

Vsend delivers motion-corrected volumes in real time (requires C2P agreement). DICOM export uses standard C-STORE; the Python receiver listens on port 4006. After a resting state scan, send from the Patient Browser to the `MURFI_DICOM` node.

Ports 50000 and 4006 must be open for the scanner subnet (192.168.2.0/24).

## Operator Interface (Textual TUI)

The TUI replaces the previous zenity/bash interface. Research assistants operate the system through single-keypress interactions.

**Screen flow:**
```
Subject Entry --> Session Select --> Localizer | Process | Neurofeedback | Test
```

**Traffic light model:**

| Color | Meaning | Action |
|-------|---------|--------|
| Green | All checks pass | Press D to advance |
| Yellow | Warning, can continue | Press D twice to confirm |
| Red | Critical failure | Cannot advance; quit and report |

**Sessions:**

- **Localizer** (Session 1): Preflight checks, 2-volume reference scan, 2 resting state runs.
- **Process** (between sessions): ICA run selection with quality table, FEAT/MELODIC (~25 min), DMN/CEN mask extraction, registration.
- **Neurofeedback** (Session 2): 12 runs (Transfer Pre, 5 Feedback, Transfer Post, 5 Feedback). Each run: MURFI receives volumes, operator presses D, PsychoPy ball task runs (150s).
- **Test**: Dry-run with simulated data. Same flow as Localizer but no scanner or MURFI required.

**Preflight checks** (13 total): FSL, Apptainer, MURFI container, subject directory, Ethernet interface, scanner ping, Wi-Fi off, ports 50000/15001 free, port binding, firewall rules for ports 50000 and 4006, stale process detection.

## Codebase Structure

```
mindfulness-nf/
+-- mindfulness_nf/                Python package (Textual TUI + orchestration)
|   +-- config.py                  Frozen scanner and pipeline configuration
|   +-- models.py                  Frozen dataclasses: TrafficLight, RunState, SessionState
|   +-- quality.py                 Pure threshold functions (green/yellow/red)
|   +-- orchestration/
|   |   +-- preflight.py           13 async preflight checks
|   |   +-- murfi.py               Apptainer container lifecycle, volume monitoring
|   |   +-- dicom_receiver.py      pynetdicom async wrapper (port 4006)
|   |   +-- psychopy.py            Subprocess launch, adaptive scale factor
|   |   +-- ica.py                 FSL FEAT/MELODIC pipeline, mask extraction
|   |   +-- registration.py        flirt/applywarp mask registration
|   |   +-- subjects.py            Subject creation, session state, crash recovery
|   +-- tui/
|       +-- app.py                 Main Textual App
|       +-- screens/               SubjectEntry, SessionSelect, Localizer, Process, NF, Test
|       +-- widgets/               StatusLight, RunProgress, RunTable, LogPanel, PreflightChecklist
|       +-- styles/app.tcss        Textual CSS
+-- murfi/
|   +-- scripts/                   Shell scripts (reference implementation, gradually deprecated)
|   |   +-- run_session.sh         Session orchestrator (696 lines)
|   |   +-- feedback.sh            MURFI step executor (730 lines)
|   |   +-- test_pipeline.sh       Test levels 0-3
|   |   +-- dicom_receiver.py      Standalone DICOM receiver
|   |   +-- rsn_get.py             DMN/CEN IC selection
|   |   +-- masks/                 MNI templates and network masks
|   |   +-- fsl_scripts/           MELODIC ICA FSF templates
|   +-- subjects/
|   |   +-- template/              Subject directory template (XML configs, mask dirs)
|   +-- dicom_input/               DICOM receiver output directory
+-- psychopy/
|   +-- balltask/                  Real-time neurofeedback ball task
|       +-- rt-network_feedback.py PsychoPy feedback display (1080 lines)
|       +-- murfi_activation_communicator.py  MURFI infoserver client
|       +-- data/                  Per-subject CSV output
+-- tests/                         349 tests
+-- assets/                        Desktop icon
+-- docs/superpowers/specs/        Design spec
```

## Internal Design

The Python package follows Functional Core + Imperative Shell (FCIS) architecture.

**Functional core** (`models.py`, `quality.py`, `config.py`): Frozen dataclasses with `slots=True`, tuple collections, pure functions. Zero I/O imports. Tested without mocks.

**Imperative shell** (`orchestration/`): Async I/O for subprocesses, network, filesystem. Uses `asyncio.TaskGroup` for parallel preflight checks, `asyncio.to_thread()` for blocking FSL calls. Tests mock only external I/O.

**TUI layer** (`tui/`): Textual 8.x screens and widgets. Calls orchestration functions via Textual workers.

### Neurofeedback session sequence

| Run | Type | Feedback | Scale Factor |
|-----|------|----------|-------------|
| 1 | Transfer Pre | No | n/a |
| 2-6 | Feedback 1-5 | Yes | Default 10.0, then adaptive |
| 7 | Transfer Post | No | n/a |
| 8-12 | Feedback 6-10 | Yes | Carries over from Run 6 |

The adaptive scale factor reads the prior run's CSV. If mean hits per TR fall below 3, it multiplies by 1.25. If above 5, it multiplies by 0.75. If between 3 and 5, it stays unchanged.

### MoCo safety

`configure_moco()` verifies the `onlyReadMoCo` XML field before every MURFI launch. If the value is wrong, it corrects it and logs a warning. Only `2vol.xml` and `rtdmn.xml` have this field; `rest.xml` uses DICOM input.

If scanner MoCo is ON but `onlyReadMoCo` is false (or vice versa), MURFI silently drops all incoming data.

### Crash recovery

A session state file (`.session_state.json`) records subject, session type, last completed step, and timestamp. On relaunch, the app offers to resume. Partial data from the interrupted step is discarded.

## Protocol Constants

| Constant | Value |
|----------|-------|
| TR | 1.2 s |
| 2-volume measurements | 20 |
| Resting state measurements | 250 |
| Feedback measurements | 150 |
| PsychoPy run duration | 150 s |
| ICA components | 128 |
| Default scale factor | 10.0 |
| Hit target range | 3-5 per TR |
| Scanner IP | 192.168.2.1 |
| Vsend port | 50000 |
| DICOM port | 4006 |
| Infoserver port | 15001 |
| MURFI container | /opt/murfi/apptainer-images/murfi.sif |

## System Requirements

- **OS:** Ubuntu 22.04+
- **Python:** 3.13+ (managed by uv at `/opt/uv/python/`)
- **FSL:** 6+ at `/opt/fsl`
- **Apptainer:** 1.3+
- **MURFI:** v2.1.1 container at `/opt/murfi/apptainer-images/murfi.sif`
- **Network:** Dedicated Ethernet to scanner on 192.168.2.x subnet; Wi-Fi must be off during sessions
- **Display:** Second monitor for PsychoPy (screen=1, 1920x1080)
- **Firewall:** Ports 50000 and 4006 open from 192.168.2.0/24

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run tests
uv run pytest tests/ -x -q

# Run TUI in test mode
uv run mindfulness-nf --test

# Run a specific test file
uv run pytest tests/test_quality.py -v

# Run TUI screen tests
uv run pytest tests/tui/ -v
```

### Test structure

| Layer | Tests | Mocks |
|-------|-------|-------|
| Functional core (`test_quality.py`, `test_models.py`, `test_config.py`) | Parametrized threshold and immutability tests | None |
| Orchestration (`test_preflight.py`, `test_murfi.py`, etc.) | Subprocess and network behavior | External I/O only |
| TUI (`tests/tui/`) | Screen transitions, keybindings, widget rendering | Orchestration calls |
| Integration (`test_integration.py`) | Full app flow in dry-run mode | Preflight results |

## Provenance

Based on the rt-BPD codebase (Clemens Bauer, 2025) with these changes:

| Change | Reason |
|--------|--------|
| Neurological orientation throughout | Eliminates LPS/neurological confusion in registration |
| Melodic IC resampling (applywarp) | Fixes 74 to 68 slice dimension mismatch in multi-run ICA |
| Bilateral CEN selection | Lateralization analysis picks most bilateral CEN component |
| 4-voxel brain mask erosion | Keeps masks inside brain boundary |
| Robust reference selection (ls -v) | Prevents wrong-file selection vs fragile ls -t |
| Safe file operations (cp, rm -rf) | Prevents data loss from destructive mv |
| ICA overwrite protection | Prevents accidental 25-minute re-runs |
| Apptainer container at /opt/murfi | System-installed MURFI v2.1.1 |
| Single-machine (localhost) | PsychoPy connects to MURFI on 127.0.0.1 |
| BIDS subject IDs (sub-NNN) | Standard naming convention |
| Textual TUI | Replaces zenity/bash/tmux interface |
| Python 3.13, unified .venv | Single environment for TUI, orchestration, and PsychoPy |
