# Mindfulness Real-Time fMRI Neurofeedback

Real-time fMRI neurofeedback targeting the Default Mode Network (DMN) and Central Executive Network (CEN) for mindfulness training in adolescents with mood disorders.

**Clinical trial:** NCT05617495
**PI:** Dr. Kymberly Young (Pitt Psychiatry)
**Collaborators:** Dr. Danella Hafeman (Pitt), Dr. Sue Whitfield-Gabrieli and Clemens Bauer (Northeastern)

## Why the operator interface exists

A scanner session costs the participant an hour and the lab a slot. Mid-session failures that forced the operator to quit and restart were professionally unacceptable. The current TUI drives sessions through a resilient `SessionRunner`: every transition persists to disk, any step can be redone, MURFI or PsychoPy can be relaunched without discarding collected volumes, and the whole session can be rehearsed with a simulated scanner.

## Install

Requires Python 3.13+, [uv](https://docs.astral.sh/uv/), FSL 6+, and Apptainer.

```bash
git clone git@github.com:CMU-Pitt-BRIDGE/mindfulness-nf.git
cd mindfulness-nf
uv venv --python 3.13
uv sync                  # runtime + dev tools (dev is a default PEP 735 group)
```

On the **scanner PC**, also install the operator extra — PsychoPy plus the
pygame/psychtoolbox hardware-timing deps the ball task needs. These are kept
out of the base install because their wxpython build broke CI:

```bash
uv sync --extra operator
```

## Run a session

Launch the TUI from the **desktop icon** (how operators normally start it) or
from a terminal. It prompts for a subject ID, then for a session type.

```bash
uv run mindfulness-nf
```

> **The desktop launcher must use a login shell** (`bash -lc`, set in
> `~/Desktop/mindfulness-nf.desktop`). The GNOME session does not source
> `/etc/profile.d`, so a plain `bash -c` would leave `fslmerge`/`mcflirt` off
> `PATH` and Process-session FSL steps would fail with `FileNotFoundError`. The
> `-l` flag sources `/etc/profile.d/fsl.sh` (you must be in the `fsl` group) so
> FSL and the MURFI tools are available. If you rebuild the launcher, keep
> `-l`; verify with `command -v fslmerge`.

Pass `--subject` to skip the subject prompt.

```bash
uv run mindfulness-nf --subject sub-001
```

### Session types

| Key | Type | Purpose |
|-----|------|---------|
| 1 | Localizer (loc3) | Setup/preflight + 2 resting-state runs (250 vols each) |
| 2 | RT15 | 9 steps: Setup, 2vol, Transfer Pre, Feedback 1-5, Transfer Post |
| 3 | RT30 | 15 steps: RT15 plus Transfer Post 1, Feedback 6-10, Transfer Post 2 |
| 4 | Process | FSL pipeline: merge, MELODIC, DMN/CEN extraction, registration, QC |

### Session order

Run the three session types in this order, using the **same `--subject` ID**
throughout. Each step consumes what the previous one produced:

1. **Localizer (`1`)** — collects two resting-state runs into
   `murfi/subjects/sub-XXX/img/` as `img-rest-<run>-*.nii`.
2. **Process (`4`)** — reads those rest volumes, runs the FSL pipeline
   (merge → MELODIC → DMN/CEN extraction → registration → QC), and writes the
   masks to `murfi/subjects/sub-XXX/mask/`.
3. **RT15 (`2`) or RT30 (`3`)** — real-time neurofeedback; MURFI's `rtdmn.xml`
   loads the DMN/CEN masks that Process produced.

Process and the neurofeedback sessions cannot run until the Localizer has
populated `img/`.

> The Process **Merge rests** step ending with `no runs in subject img/ dir`
> is **not a crash** — it means that subject's `img/` holds no `img-rest-*.nii`
> volumes yet. Run the Localizer (`1`) for that subject first.

### Dry-run rehearsal

Rehearse any session type end-to-end against a simulated scanner. MURFI and PsychoPy launch for real; only the scanner is simulated.

```bash
uv run mindfulness-nf --dry-run --subject sub-rehearse
```

Populate the dry-run cache once from a real session's raw volumes.

```bash
uv run python scripts/populate_dry_run_cache.py <real_session_path>
```

The cache lives at `murfi/dry_run_cache/` (gitignored). If the cache is missing, `--dry-run` refuses to start and prints the populate command.

## Keybindings

The help bar shows only keys valid for the current step status. Cursor navigation never interrupts a running step.

| Key | When valid | Action |
|-----|------------|--------|
| `d` | status=pending | Start the step |
| `d` | status=running, `awaiting_advance=True` | Advance phase (MURFI to PsychoPy) |
| `d` | status=completed | Move cursor forward; auto-start next pending step |
| `d` | status=failed | No-op; press `r` or `i` |
| `r` | cursor step not running, no other step running | Clear files and restart step (confirms on completed) |
| `r` | cursor step running | Stop, clear, restart |
| `i` | any step running | Interrupt running step; clear its partial data; mark pending |
| `i` | cursor step failed, nothing running | Clear cursor step's partial data; mark pending |
| `b` / `left` | any | Move cursor backward |
| `n` / `right` | any | Move cursor forward |
| `g` | any | Prompt for step number; jump cursor |
| `m` | status=running, `murfi` in components | Relaunch MURFI; keep data and progress |
| `p` | status=running, `psychopy` in components | Relaunch PsychoPy; keep data and progress |
| `esc` | any | Quit; prompts before stopping a running step |

`r` destroys on-disk data for the step. `m` and `p` keep data; they restart one subprocess.

## Resume

Force-quitting a session leaves `session_state.json` on disk under
`murfi/subjects/sub-XXX/ses-<type>/`. Relaunch with the same `--subject` and
the same session type; the cursor lands where it was. Any step marked
`running` at the time of the crash is coerced to `failed` on load (with
`error="interrupted by restart"`). Partial `.nii` files stay on disk; press
`r` on the failed step to clear them and re-run it.

A fresh subject ID always starts clean. Reusing an ID that already has a
`session_state.json` resumes that session instead of starting over — use a new
ID for each new participant.

## Before a scanner session

Run the preflight test suite. It must print the green banner before you touch the scanner.

```bash
bash scripts/preflight_test.sh
```

Then walk through the dry-run checklist.

See `docs/operator/rehearsal.md`.

## Running tests

```bash
uv run pytest tests/
```

Three layers: pure state machine (milliseconds), runner with mocked processes (seconds), end-to-end with the Textual harness and fake MURFI/PsychoPy subprocesses (~2 minutes total). No scanner, MURFI container, or display required.

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
    +-- DICOM export (TCP 4006)        Resting-state runs (MoCo OFF)
            |
        dicom_receiver.py (pynetdicom, AE title: MURFI)
```

Ports 50000 and 4006 must be open for the scanner subnet (192.168.2.0/24). Wi-Fi must be off during sessions.

## BIDS layout

Each session writes to `murfi/subjects/sub-XXX/ses-<type>/` with `func/`, `sourcedata/`, `derivatives/`, and `session_state.json`. Raw MURFI output lives in `sourcedata/murfi/img/`; on step completion the runner publishes a BIDS-named NIfTI under `func/` with a JSON sidecar.

```
murfi/subjects/sub-001/ses-rt15/
  session_state.json
  func/sub-001_ses-rt15_task-feedback_run-01_bold.nii
  sourcedata/murfi/{xml,img,log}/
  sourcedata/psychopy/sub-001_ses-rt15_run01.csv
  derivatives/masks/{DMN,CEN}.nii
```

## Protocol constants

| Constant | Value |
|----------|-------|
| TR | 1.2 s |
| 2-volume measurements | 2 |
| Resting-state measurements | 250 |
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

## System requirements

- **OS:** Ubuntu 22.04+
- **Python:** 3.13+ (managed by uv at `/opt/uv/python/`)
- **FSL:** 6+ at `/opt/fsl`
- **Apptainer:** 1.3+
- **MURFI:** v2.1.1 container at `/opt/murfi/apptainer-images/murfi.sif`
- **Network:** Dedicated Ethernet on 192.168.2.x subnet; Wi-Fi off during sessions
- **Display:** Second monitor for PsychoPy (screen=1, 1920x1080)
- **Firewall:** Ports 50000 and 4006 open from 192.168.2.0/24

## Provenance

Based on the rt-BPD codebase (Clemens Bauer, 2025) with these changes:

| Change | Reason |
|--------|--------|
| Neurological orientation throughout | Eliminates LPS/neurological confusion in registration |
| Melodic IC resampling (applywarp) | Fixes 74-to-68 slice dimension mismatch in multi-run ICA |
| Bilateral CEN selection | Lateralization analysis picks most bilateral CEN component |
| 4-voxel brain mask erosion | Keeps masks inside brain boundary |
| Robust reference selection (`ls -v`) | Prevents wrong-file selection vs fragile `ls -t` |
| Safe file operations (`cp`, `rm -rf`) | Prevents data loss from destructive `mv` |
| ICA overwrite protection | Prevents accidental 25-minute re-runs |
| Apptainer container at /opt/murfi | System-installed MURFI v2.1.1 |
| Single-machine (localhost) | PsychoPy connects to MURFI on 127.0.0.1 |
| BIDS subject IDs and session layout | Standard naming; `ls` reveals what ran |
| Textual TUI with `SessionRunner` | Replaces zenity/bash/tmux; persists every transition; supports resume and dry-run |
| Python 3.13, unified .venv | Single environment for TUI, orchestration, and PsychoPy |
