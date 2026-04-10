# Research Assistant Procedure Manual

Step-by-step operating procedures for the mindfulness neurofeedback
sessions. All sessions use the Textual TUI (`uv run mindfulness-nf`).

---

## Before Any Session

1. Log in to the analysis workstation (Ubuntu, user `young-lab`).
2. Verify the Ethernet cable is connected to the scanner network.
3. Turn Wi-Fi OFF on the workstation.
4. Open a terminal and launch the TUI:
   ```
   cd /home/young-lab/code/mindfulness-nf
   uv run mindfulness-nf
   ```
5. Enter the participant ID (e.g., `sub-001`). The system auto-prepends
   `sub-` if you omit it.
6. Select the session type by pressing the corresponding number key:
   - `1` Localizer (first MRI visit)
   - `2` Process (between visits, ICA analysis)
   - `3` Neurofeedback (second MRI visit)
   - `4` Test (dry run, no scanner needed)

## Status Indicators

The TUI uses a traffic light model:

| Color  | Meaning           | What to Do                          |
|--------|-------------------|-------------------------------------|
| Green  | All checks pass   | Press `D` to advance                |
| Yellow | Warning           | Press `D` twice to confirm          |
| Red    | Critical failure  | Press `Q`, quit, and report error   |

---

## Session 1: Localizer

**Purpose:** Collect the reference scan and two resting-state scans for
ICA processing.

### Step 1: Preflight Checks

The TUI runs 13 automated checks. Wait for all checks to pass (green),
then press `D`.

If a check fails (red):
- Read the error message on screen.
- Report the failure to the PI or study engineer.

### Step 2: 2-Volume Reference Scan

1. The TUI starts MURFI automatically.
2. On the scanner console, start the `func-bold_task-2vol_run-01`
   sequence. Scanner motion correction (MoCo) must be ON.
3. Wait for the scan to finish (about 24 seconds; 20 volumes at
   TR=1.2s).
4. The TUI shows a live volume counter. When the count reaches 20,
   press `D`.

### Step 3: Resting-State Run 1

1. The TUI starts the DICOM receiver on port 4006.
2. On the scanner console, start `func-bold_task-rest_run-01`. MoCo
   must be OFF.
3. Wait for the scan to finish (about 5 minutes; 250 volumes).
4. On the scanner console, open Patient Browser, select the resting
   state series, right-click, and send to `MURFI_DICOM`.
5. Wait for the TUI volume counter to reach 250, then press `D`.

### Step 4: Resting-State Run 2

Repeat Step 3 with `func-bold_task-rest_run-02`.

After Step 4 completes, press `Q` to exit.

---

## Between Sessions: Process (ICA)

**Purpose:** Extract participant-specific DMN and CEN brain masks from
the resting-state data. No scanner needed.

**Duration:** Approximately 25 minutes (automated).

1. Launch the TUI and select session `2` (Process).
2. A table shows the available resting-state runs with volume counts and
   quality indicators. Toggle runs by pressing their number. Select at
   least 2 runs.
3. Press `D` to start processing.
4. The TUI shows progress through 7 automated steps:
   1. Merge selected runs
   2. Skull-strip reference image
   3. Run ICA (FEAT/MELODIC, 128 components)
   4. Register MNI templates to native space
   5. Split ICA components
   6. Select DMN and CEN components
   7. Threshold and create final masks
5. Wait for all steps to complete. The TUI advances through them automatically.

### Verification

After processing, confirm the masks exist:
```
ls murfi/subjects/sub-XXX/mask/dmn.nii
ls murfi/subjects/sub-XXX/mask/cen.nii
```

If either file is missing, report to the PI.

---

## Session 2: Neurofeedback

**Purpose:** 12 neurofeedback runs with real-time brain feedback via the
ball task.

**Duration:** 60-75 minutes of scanning.

### Preflight

The TUI runs preflight checks before Run 1. Wait for green, then press
`D`.

### Run Sequence

| Run | Type           | Ball Feedback | Scanner MoCo | What to Tell Participant |
|-----|----------------|---------------|--------------|--------------------------|
| 1   | Transfer Pre   | OFF           | ON           | "Practice Noting. The ball will not move." |
| 2-6 | Feedback 1-5   | ON            | ON           | "Practice Noting. Watch the ball occasionally." |
| 7   | Transfer Post  | OFF           | ON           | "Practice Noting. The ball will not move." |
| 8-12| Feedback 6-10  | ON            | ON           | "Practice Noting. Watch the ball occasionally." |

### For Each Run

1. The TUI starts MURFI with the `rtdmn.xml` configuration.
2. On the scanner console, start the feedback or transfer sequence.
   MoCo must be ON for all neurofeedback runs.
3. Wait for the TUI volume counter to reach 150.
4. Press `D`.
5. PsychoPy launches automatically on the participant's display
   (second monitor). The ball task runs for 2.5 minutes.
6. After PsychoPy finishes, the TUI advances to the next run.

### Adaptive Difficulty

The ball speed adjusts automatically between feedback runs:
- Fewer than 3 hits per TR: ball moves more (easier).
- More than 5 hits per TR: ball moves less (harder).
- 3-5 hits per TR: ball speed unchanged.

The TUI displays the current scale factor after each run.

### After the Last Run

Press `Q` to exit the TUI. Confirm all 12 PsychoPy CSV files exist:
```
ls psychopy/balltask/data/sub-XXX/
```

---

## Test Mode (Dry Run)

Select session `4` (Test) to run through the localizer flow with
simulated data. This mode works without a scanner or MURFI container.
Use it to train new RAs or verify the workstation setup.

```
uv run mindfulness-nf --test
```

---

## Troubleshooting

### Scanner cannot connect to port 50000

1. Is MURFI running? Check the TUI log panel for volume messages.
2. Is the firewall open? Run:
   ```
   sudo nft list chain ip filter ufw-user-input | grep 50000
   ```
3. Is Wi-Fi off? A wireless connection causes routing conflicts.

### DICOM Verification fails on scanner console

1. Is the DICOM receiver running? The TUI starts it automatically for
   resting-state steps.
2. Does the scanner know the hostname `MURFI`? It must resolve to
   `192.168.2.5`.

### MURFI receives 0 volumes

Check the MoCo setting. A mismatch between scanner MoCo and the MURFI
configuration drops volumes silently. Report the mismatch to the study
engineer.

### TUI shows red after a scan

Read the error message. Press `Q`, exit, and report the error to the
PI or study engineer.

### Stale MURFI process blocking ports

If the TUI fails to start because ports 50000 or 15001 are occupied,
a previous MURFI instance is still running. The preflight check detects
this. Report it to the study engineer.

---

## Emergency Procedures

If a participant reports distress during scanning, follow the Suicide
Risk Management Plan in `materials/suicide-risk-management-plan.md`.

Contact the PI immediately if:
- A participant reports suicidal ideation
- A scan session produces no usable data (all runs red)
- The workstation loses network connectivity mid-session

---

## Quick Reference Card

| Action                    | Key   |
|---------------------------|-------|
| Advance to next step      | `D`   |
| Confirm warning (yellow)  | `D D` |
| Quit                      | `Q`   |
| Select session 1-4        | `1`-`4` |

| Port  | Purpose                    |
|-------|----------------------------|
| 50000 | Vsend (real-time volumes)  |
| 4006  | DICOM (resting-state)      |
| 15001 | MURFI infoserver           |
