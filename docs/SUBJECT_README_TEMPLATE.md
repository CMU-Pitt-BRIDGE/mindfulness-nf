# Subject data layout — REMIND mindfulness neurofeedback

This directory contains all data collected for one subject across one or
more scanner sessions. It is **copy-and-go**: moving this directory to
another machine gives downstream analysts everything they need, with the
caveat that XML templates and masks reference code-defined semantics
documented here.

## Directory layout

```
sub-<ID>/
├── sub-<ID>_sessions.tsv       One row per session (session_id, acq_time).
├── sub-<ID>_participants.json  Subject-level metadata (optional).
├── img/                        MURFI per-volume NIfTIs, task+run-keyed.
├── xfm/                        study_ref + series*_ref (registration anchors).
├── mask/                       DMN/CEN masks. mask/{dmn,cen}.nii is what
│                               MURFI loaded for feedback.
├── xml/                        Subject-level XML templates MURFI reads.
├── log/                        Subject-level MURFI log (log.rtl).
└── ses-<TYPE>/                 One per session: loc3, process, rt15, rt30.
    ├── session_state.json      Authoritative step-by-step manifest.
    ├── provenance.json         git SHA, host, CLI args at session start.
    ├── log/                    Per-step MURFI logs (murfi_<xml>_<task>-<run>.log).
    ├── rest/                   4D merges + FSL preprocessing.
    ├── qc/                     QC overlays (slices GIFs).
    └── sourcedata/
        ├── murfi/xml/          Snapshot of XML templates as run.
        └── psychopy/           Behavioral data.
            └── sub-<ID>/       PsychoPy-written CSVs + BIDS TSVs (below).
```

## Sessions

| Session | Purpose | Typical steps |
|---|---|---|
| `ses-loc3` | Localizer: resting-state runs | Setup → Rest 1 → Rest 2 (each 250 TRs) |
| `ses-process` | Offline processing: ICA + mask extraction + registration | Setup → Merge → MELODIC → Extract DMN → Extract CEN → Register → QC |
| `ses-rt15` | Real-time NF, 15-min protocol | Setup → 2vol → Transfer Pre → Feedback 1-5 → Transfer Post (each 150 TRs) |
| `ses-rt30` | Real-time NF, 30-min protocol | Same as rt15 with 2 more feedback runs and 2 transfer posts |

TR is **1.2 seconds** across all sessions.

## Raw per-volume NIfTIs — `img/img-<task>-<run>-<vol>.nii`

MURFI writes the raw incoming scanner volumes here. Filename encodes:
- `<task>`: BIDS task label (`rest`, `feedback`, `transferpre`, `transferpost`, `2vol`)
- `<run>`: task-scoped 1-based run index, zero-padded to 2 digits
- `<vol>`: 1-based volume index in this run, zero-padded to 5 digits

Example: `img-feedback-03-00042.nii` = Feedback 3, volume 42.

> **Historical note:** earlier data used `img-<NNNNN>-<VVVVV>.nii` (MURFI's
> native series+volume format). Migration to task-keyed naming landed
> 2026-04-21; earlier subjects may retain the native format.

## Behavioral data — `ses-<TYPE>/sourcedata/psychopy/sub-<ID>/`

For each NF run (Transfer Pre, each Feedback, Transfer Post), PsychoPy
writes five files:

### `sub-<ID>_DMN_<task>_<run>.csv` + `.psydat`
PsychoPy's `ExperimentHandler` main experiment log. Metadata row with
participant/run/feedback_on/date/expName/TR/scale_factor/frameRate.

### `sub-<ID>_DMN_<task>_<run>.log`
Frame-by-frame text log of PsychoPy events (keypresses, stimulus onsets,
all UI interactions). Useful for timing reconstruction.

### `sub-<ID>_DMN_<task>_<run>_roi_outputs.csv`
**Per-TR streaming ROI data from MURFI — the main NF signal.** One row
per TR written by the PsychoPy feedback loop.

Columns:

| Column | Unit | Description |
|---|---|---|
| `volume` | TR index | 0-based (0 → 149 for a 150-TR run) |
| `scale_factor` | dimensionless | Ball motion gain — maps ROI signal → ball displacement. Adaptive across runs: default 10, × 1.25 if too few hits, × 0.75 if too many |
| `time` | seconds | PsychoPy clock time of this row's write, relative to scanner trigger |
| `time_plus_1.2` | seconds | `time + TR`; predicted onset of NEXT volume |
| `cen` | arbitrary MURFI units (z-scored relative to run-running-mean) | Central executive network mean activation from `mask/cen.nii` via `roi-weightedave`. Positive = above baseline, negative = below |
| `dmn` | arbitrary MURFI units (z-scored) | Default mode network mean activation from `mask/dmn.nii`. In this protocol **lower DMN = more mindful** |
| `stage` | enum | `baseline` (first ~25 TRs) or `feedback` (remaining TRs) |
| `cen_cumulative_hits` | count | Times the ball reached the CEN target circle (bottom) this run |
| `dmn_cumulative_hits` | count | Times the ball reached the DMN target circle (top) this run |
| `pda_outlier` | bool or `nan` | True if this TR's PDA (= cen − dmn) exceeds `pda_outlier_threshold` (= 2) SDs. `nan` during baseline. Feedback display damps outliers |
| `ball_y_position` | normalized [-1, 1] | Rendered ball y-coord. `nan` during baseline |
| `top_circle_y_position` | normalized [-1, 1] | DMN target circle y-coord. Fixed at 0.333 |
| `bottom_circle_y_position` | normalized [-1, 1] | CEN target circle y-coord. Fixed at −0.333 |

> **Known off-by-one:** all NF runs write **149 data rows** for a
> 150-TR scan (final TR is dropped as the PsychoPy routine timer expires
> before the last volume is fully written). MURFI's own log confirms
> 150 TRs were received. If row count matters for your analysis, use
> MURFI's `curact-*.nii` files or the BIDS TSV (same length).

### `sub-<ID>_DMN_<task>_<run>_slider_questions.csv`
Post-run self-report sliders (4 questions, 1-9 scale):
1. "How often were you using the mental noting practice?"
2. "How often did you check the position of the ball?"
3. "How difficult was it to apply mental noting?"
4. "How calm do you feel right now?"

### `sub-<ID>_ses-<TYPE>_task-<task>_run-<NN>.tsv`
BIDS-compliant events TSV. One row per TR. Duration = 1.2 s (one TR).
Columns include onset/duration/trial_type + the same cen/dmn/pda + the
4 slider responses broadcast across all rows.

## Masks — `mask/`

| File | Space | Source |
|---|---|---|
| `mask/dmn.nii` | study_ref | Final DMN mask MURFI loads for NF |
| `mask/cen.nii` | study_ref | Final CEN mask MURFI loads for NF |
| `mask/dmn_rest_original.nii` | native rest | Unregistered DMN from MELODIC |
| `mask/cen_rest_original.nii` | native rest | Unregistered CEN from MELODIC |
| `mask/dmn_studyref.nii`, `cen_studyref.nii` | study_ref | Intermediate (before erosion/masking) |
| `mask/dmn_native_rest.nii`, `cen_native_rest.nii` | native rest | Copy of the MELODIC outputs |

Only `mask/dmn.nii` + `mask/cen.nii` are consumed at scan time.

## MURFI XML templates

Each session snapshots the XMLs at `ses-<TYPE>/sourcedata/murfi/xml/`.
These are the exact configs MURFI read during the session. They define:

- `<scanner>`: TR, number of measurements, port, `save`/`saveImages`
  (both must be `true` to write raw volumes to disk)
- `<processor>`: per-TR pipeline (mosaic → mask-load → motion → GLM
  → roi-combine → current-activation → infoserver stream)

## Provenance — `ses-<TYPE>/provenance.json`

Written at session start. Fields:
- `timestamp`: ISO-8601 UTC
- `git_sha`, `git_branch`, `git_dirty`: code version
- `hostname`, `platform`, `python`: execution environment
- `cli_argv`: exact CLI invocation

## Session state — `ses-<TYPE>/session_state.json`

Authoritative record of step progression. Atomic writes; safe to read
while the session is live. Key fields:
- `subject`, `session_type`, `cursor`, `created_at`, `updated_at`
- `steps[]`: each with `config` (name, task, run, target), `status`
  (`pending`/`running`/`completed`/`failed`), `progress_current`,
  `last_started`, `last_finished`, `attempts`, `artifacts`, `error`

A `status=running` on resume is coerced to `failed` with `error =
"interrupted by restart"`.

## Copying for analysis

```bash
cp -r /path/to/sub-<ID>/ /destination/
```

Everything needed is under `sub-<ID>/`. PsychoPy behavioral data used to
live at `psychopy/balltask/data/sub-<ID>/` (sibling tree, easy to miss);
the pipeline now routes it into `ses-<TYPE>/sourcedata/psychopy/` so
nothing is outside the subject dir.
