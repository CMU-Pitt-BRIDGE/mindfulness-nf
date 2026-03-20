# Textual TUI for Mindfulness Neurofeedback

**Date:** 2026-03-20
**Status:** Approved
**Python:** 3.13+

## Problem

The current operator interface combines zenity dialogs, tmux split panes, and terminal prompts across ~1000 lines of bash. Research assistants must type "done" after each scan, choose between retry/skip/quit on errors they do not understand, and monitor data arrival by reading scrolling log output. The MoCo misconfiguration incident (2026-03-19) caused silent data loss for an entire session.

## Solution

Replace all bash scripts with a Python Textual TUI application backed by an async orchestration library. The TUI provides a traffic light status model, single-keypress operation, and automatic data validation at every step.

## Architecture: Functional Core + Imperative Shell

### Functional Core (no I/O, no mocks in tests)

- `models.py`: Frozen dataclasses (`frozen=True, slots=True`) with tuple collections. `TrafficLight`, `RunState`, `SessionState`, `CheckResult`.
- `quality.py`: Pure functions that accept counts and return `TrafficLight` values. All threshold logic lives here.
- `config.py`: Frozen dataclasses for scanner, pipeline, and path configuration.

### Imperative Shell (async I/O, mocked in tests)

- `orchestration/preflight.py`: Network checks, firewall, container, stale processes. Returns `tuple[CheckResult, ...]`.
- `orchestration/murfi.py`: Apptainer container lifecycle. Start, stop, log streaming, volume counting. Calls `quality.assess_volume_count()` for traffic light decisions.
- `orchestration/dicom_receiver.py`: pynetdicom listener on port 4006 for resting state scans (MoCo OFF, no vSend).
- `orchestration/psychopy.py`: Subprocess launch and monitoring. CSV parsing for adaptive scale factors.
- `orchestration/ica.py`: FSL FEAT/MELODIC pipeline. Run listing with volume counts, merge, ICA, mask extraction. Reports progress via callback.
- `orchestration/registration.py`: flirt/applywarp mask registration to 2-volume reference space.

### TUI Layer (Textual screens and widgets)

- `tui/app.py`: Main app, screen routing.
- `tui/screens/`: `subject_entry`, `session_select`, `localizer`, `process`, `neurofeedback`, `test`.
- `tui/widgets/`: `status_light`, `run_progress`, `run_table`, `log_panel`, `preflight_checklist`.
- `tui/styles/app.tcss`: Textual CSS.

## Package Structure

```
mindfulness_nf/
├── __main__.py
├── config.py
├── models.py
├── quality.py
├── orchestration/
│   ├── preflight.py
│   ├── murfi.py
│   ├── dicom_receiver.py
│   ├── psychopy.py
│   ├── ica.py
│   └── registration.py
└── tui/
    ├── app.py
    ├── screens/
    │   ├── subject_entry.py
    │   ├── session_select.py
    │   ├── localizer.py
    │   ├── process.py
    │   ├── neurofeedback.py
    │   └── test.py
    ├── widgets/
    │   ├── status_light.py
    │   ├── run_progress.py
    │   ├── run_table.py
    │   ├── log_panel.py
    │   └── preflight_checklist.py
    └── styles/
        └── app.tcss
```

## Operator Interaction Model

### Target Users

Research assistants with no programming experience. Every screen displays a label for the current step. Error messages include the specific failure and an instruction. No screen requires typing except subject ID entry.

### Screen Flow

```
SubjectEntry → SessionSelect → [Localizer | Process | Neurofeedback | Test]
```

No back-navigation: the operator cannot return to a previous screen or re-run a completed step within a session. `Q` quits the entire application at any time (with confirmation dialog).

### Subject Entry

Free-form text input. Accepts alphanumeric characters, hyphens, and underscores. Rejects spaces, leading dots, and other special characters. Auto-prepends `sub-` if absent. Detects whether the subject directory exists and displays the result before the operator continues. If the directory does not exist, displays "New subject" and creates the directory (copying XML templates from `murfi/subjects/template/`) when the operator confirms.

### Session Select

Single keypress `1`-`4` selects localizer, process, neurofeedback, or test. No Enter required.

### Preflight Screen

Used for the setup step at the start of each session (localizer, neurofeedback). Displays the preflight checklist widget with pass/fail indicators per check. All checks must pass before the operator can press `D` to continue to the first scan step.

### During-Scan Screens (Localizer scan steps, Neurofeedback runs)

Three zones:

1. **Step tracker**: Completed steps (checkmark), current step (arrow with live volume counter and progress bar), pending steps (circle).
2. **Status panel**: Traffic light indicator with message.
3. **Log panel**: Streaming MURFI log, most recent lines first.

Pressing `D` triggers volume count validation. If green, advances to the next step. If yellow, requires a second `D` to confirm. If red, `D` is ignored and the status panel shows the stop message. The only option at red is `Q`.

Volume arrival is polled every 500ms by reading the MURFI log.

### Process Screen (ICA)

Two phases:

1. **Run selection**: Table of available resting state runs with volume counts and quality indicators. Operator toggles selection by pressing run number (1-9), confirms with `D`. The current protocol collects 2 resting state runs; the table supports up to 9.
2. **Processing**: Step-by-step progress display. No operator input required. Advances automatically. Shows elapsed time.

## Traffic Light Model

### Colors

| Color | Meaning | Operator Action |
|-------|---------|-----------------|
| Green | All checks pass | Press `D` to advance |
| Yellow | Warning; can continue | Press `D` twice to confirm |
| Red | Critical failure | Cannot advance. Quit and report error. |

### Expected Volume Counts

| Scan Type | XML | Expected Volumes |
|-----------|-----|-----------------|
| 2-volume | `2vol.xml` | 20 |
| Resting state | `rest.xml` | 250 |
| Feedback/Transfer | `rtdmn.xml` | 150 |

### Thresholds

| Context | Green | Yellow | Red |
|---------|-------|--------|-----|
| 2-volume scan (expected 20) | >= 18 | < 18 | 0 |
| Resting state (expected 250) | >= 225 | < 225 | < 10 |
| Feedback run (expected 150) | >= 140 | < 140 | 0 |
| Data gap (during scan) | <= 3s | > 3s since last volume | > 15s since last volume |
| ICA mask voxels | >= 100 | < 100 | 0 |

The data gap timer starts when MURFI's vSend port (50000) begins accepting connections, as detected by a TCP connect probe.

### Red State Messaging

Red messages state the problem and instruct the operator to close the program and report the error. They do not name individuals or suggest troubleshooting steps.

Example: "0 volumes received. Expected 250. Do not proceed. Close this program and report this error."

## MoCo Safety

`configure_moco()` applies only to vSend XML files (`2vol.xml`, `rtdmn.xml`). It reads the target XML, verifies `onlyReadMoCo` matches the expected value, and corrects it if wrong. If correction is needed, the log panel displays: "MoCo setting corrected in [xml_name]. Was [old], set to [new]."

Resting state scans use a separate input mechanism (`imageSource=DICOM` in `rest.xml` within the `xml_dcm/` template directory) and do not have an `onlyReadMoCo` field.

- vSend scans (2-volume, feedback, transfer): `onlyReadMoCo=true`
- DICOM scans (resting state): no MoCo field; data arrives via pynetdicom receiver

## Data Transfer Modes

| Scan Type | Transfer | Port | MoCo |
|-----------|----------|------|------|
| 2-volume | vSend | 50000 | ON |
| Resting state | DICOM export | 4006 | OFF |
| Feedback/Transfer | vSend | 50000 | ON |

The DICOM receiver (`pynetdicom`, AE title `MURFI`) starts before resting state scans and stops after them.

### Constants

All scanner and protocol constants live in `config.py`:

| Constant | Value | Used By |
|----------|-------|---------|
| `SCANNER_IP` | `192.168.2.1` | `preflight.py` |
| `VSEND_PORT` | `50000` | `murfi.py` |
| `DICOM_PORT` | `4006` | `dicom_receiver.py` |
| `DICOM_AE_TITLE` | `MURFI` | `dicom_receiver.py` |
| `INFOSERVER_PORT` | `15001` | `psychopy.py` |
| `MURFI_CONTAINER` | `/opt/murfi/apptainer-images/murfi.sif` | `murfi.py` |

XML template directories:

- vSend scans: `murfi/subjects/template/xml/xml_vsend/` (`2vol.xml`, `rtdmn.xml`)
- DICOM scans: `murfi/subjects/template/xml/xml_dcm/` (`rest.xml`)

## Localizer Session

4 steps in fixed sequence:

| Step | Scan Type | Expected Volumes | Transfer |
|------|-----------|-----------------|----------|
| 1 | Setup | (pre-flight checks) | n/a |
| 2 | 2-volume | 20 | vSend |
| 3 | Resting state run 1 | 250 | DICOM |
| 4 | Resting state run 2 | 250 | DICOM |

Step 1 (Setup) uses the preflight screen. Steps 2-4 use the during-scan screen. For each scan step: MURFI starts (or DICOM receiver for resting state), operator waits for scanner acquisition to finish, operator presses `D`, system validates volume count, advances if green.

## Crash Recovery

An `atexit` handler and signal traps clean up MURFI containers, DICOM receivers, and PsychoPy subprocesses on exit.

A session state file (`.session_state.json`) records subject, session type, last completed step index (integer, 0-based), and timestamp. Example:

```json
{
  "subject": "sub-001",
  "session": "localizer",
  "last_completed_step": 1,
  "timestamp": "2026-03-20T14:32:01"
}
```

On next launch for the same subject and session, the app offers to resume from step 2 (the next step after last_completed_step). Resumption discards partial data from the interrupted step. MURFI re-receives all volumes for that step from scratch. Partial volume files from the crashed run are deleted before restarting.

## Data Validation

Before advancing to each next step, the system verifies:

- Volume files exist on disk (not just MURFI log count)
- File sizes are non-zero
- For NF runs: PsychoPy CSV exists with expected row count
- For ICA: output directories contain expected files

Validation failures produce yellow or red traffic lights.

## Neurofeedback Session

12 runs in fixed sequence:

| Run | Type | Feedback |
|-----|------|----------|
| 1 | Transfer Pre | No |
| 2-6 | Feedback 1-5 | Yes |
| 7 | Transfer Post | No |
| 8-12 | Feedback 6-10 | Yes |

Preflight checks run once before Run 1 as part of session initialization (using the preflight screen), not as a numbered run. Each run then has three phases in order:

1. **MURFI phase**: MURFI container starts, receives volumes from scanner via vSend. Operator waits for scanner acquisition to finish, then presses `D`.
2. **Validation**: System checks volume count against thresholds. Green advances; yellow requires second `D`; red blocks.
3. **PsychoPy phase**: PsychoPy launches as subprocess (15 min). Ball task runs. No operator input. PsychoPy exits and writes CSV.

### Adaptive Scale Factor

The scale factor controls ball movement sensitivity during feedback runs.

- **Default**: 10.0. Used for Run 2 (first feedback run). The scale factor resets to 10.0 at Run 8 (first feedback run after Transfer Post).
- **Adjustment**: After each feedback run, the system computes the mean hit rate (hits per TR) from that run's CSV. If the mean is below 3 hits per TR, the scale factor is multiplied by 1.25. If above 5 hits per TR, multiplied by 0.75. If between 3 and 5 (inclusive), the scale factor is unchanged.
- **No clamp**: The scale factor itself is not clamped. The per-TR hit target range of 3-5 is the control mechanism.

The run tracker displays scale factor per completed run.

## Dry-Run / Test Mode

`python -m mindfulness_nf --test` or select "Test" from session menu.

- `SimulatedMurfi` generates fake volume counts on a timer (1 per TR)
- PsychoPy runs in fake mode or is skipped
- All orchestration functions accept `dry_run: bool`
- Traffic lights work normally; low volume counts can be simulated

## Python 3.13+ Patterns

| Pattern | Usage |
|---------|-------|
| `frozen=True, slots=True` | All domain models |
| `tuple` not `list` | Immutable collections in frozen models |
| `match`/`case` + `assert_never` | Exhaustive `Color` handling |
| `copy.replace()` | Immutable model updates |
| `X \| None` | Union syntax throughout |
| `type` aliases | Complex callback signatures |
| `asyncio.TaskGroup` | Parallel pre-flight checks |
| `asyncio.to_thread()` | FSL subprocess calls |
| `@deprecated` | Old bash-wrapper functions during transition |
| `TypeIs` | Type narrowing in quality checks |

`pyproject.toml` must set `requires-python = ">=3.13"`.

## Testing Strategy

### Functional Core (no mocks)

Pure functions and frozen dataclasses tested with direct assertions. Every threshold in the traffic light table has green, yellow, and red test cases:

- `test_quality.py`: Parametrized threshold tests. `(received, expected) -> Color`.
- `test_models.py`: Immutable update tests. Original unchanged after `copy.replace()`.
- `test_config.py`: Frozen config validation.

### Imperative Shell (mocked I/O)

External subprocess and network calls mocked with `pytest-mock`:

- `test_murfi.py`: Container launch, log parsing, volume counting.
- `test_dicom_receiver.py`: pynetdicom lifecycle.
- `test_psychopy.py`: Subprocess management, CSV parsing.
- `test_ica.py`: FSL call sequencing, progress callbacks.
- `test_preflight.py`: Network and firewall checks.

### TUI (Textual `run_test()`)

Screen transitions and keybindings:

- `D` keypress advances on green, requires confirmation on yellow, blocks on red.
- `Q` triggers quit confirmation.
- Session select routes to correct screen.
- Run selection toggle works.

### Integration

1 end-to-end test. Full localizer session in dry-run mode:

1. Enter subject ID
2. Select localizer
3. Pre-flight passes
4. Simulated 2-vol -> press D
5. Simulated rest run 1 -> press D
6. Simulated rest run 2 -> press D
7. Verify session state shows completion

### Test Principles

- Test behavior, not implementation.
- Mock external systems only (subprocesses, network). Never mock internal code.
- `pytest.raises(match=...)` for all error assertions.
- `@pytest.mark.asyncio` on all async tests.
- AAA pattern (Arrange/Act/Assert) with one behavior per test.
- Factory fixtures for customizable test data.

## Dependencies

Add to `pyproject.toml`:

```toml
requires-python = ">=3.13"
dependencies = [
    "pandas>=2.0",
    "numpy>=1.26",
    "pynetdicom>=3.0.4",
    "textual>=8.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=1.0",
    "pytest-mock>=3.15",
    "textual-dev>=1.0",
]
```

## Entry Point

```toml
[project.scripts]
mindfulness-nf = "mindfulness_nf.__main__:main"
```

After installation, `mindfulness-nf` launches the TUI. The old `mindfulness-nf.sh` is retired.

## Existing Code

- `murfi/scripts/` stays as reference during transition.
- `psychopy/balltask/` stays as-is. Launched as subprocess by `orchestration/psychopy.py`.
- `murfi/subjects/` stays as-is. Subject directories and XML templates are read by the orchestration library.
