# Subject data: REMIND mindfulness neurofeedback

Everything collected for one subject across its scanner sessions. Self-contained:
copy `sub-<ID>/` and you have all the data.

## Layout

```
sub-<ID>/
├── sub-<ID>_sessions.tsv   one row per session (session_id, acq_time)
├── img/                    raw per-volume NIfTIs (see "Raw images")
├── mask/                   DMN/CEN masks; dmn.nii and cen.nii drive feedback
├── xfm/                    registration references (series*_ref, study_ref)
├── xml/                    the MURFI configs used
├── log/                    subject-level MURFI log
└── ses-<TYPE>/             one per session: loc3, process, rt15, rt30
    ├── session_state.json  the steps and their status
    ├── provenance.json     code version, host, and command at session start
    ├── log/                per-run MURFI logs
    ├── rest/               (process only) merged rest 4D + ICA outputs
    ├── qc/                 registration QC images
    └── sourcedata/
        ├── murfi/xml/      the XMLs as run
        └── psychopy/sub-<ID>/   behavioral data (see "Behavioral data")
```

## Sessions

| Session | What it is | Steps |
|---|---|---|
| `ses-loc3` | Resting-state localizer | Setup, Rest 1, Rest 2 (250 TRs each) |
| `ses-process` | Builds the DMN/CEN masks from the rest data | Setup, Merge, MELODIC, Extract DMN, Extract CEN, Register, QC |
| `ses-rt15` | Real-time neurofeedback, 15 min | Setup, Transfer Pre, Feedback 1-5, Transfer Post (150 TRs each) |
| `ses-rt30` | Real-time neurofeedback, 30 min | rt15 plus Feedback 6-10 and a second Transfer Post |

TR is 1.2 s throughout.

## Raw images: `img/img-<task>-<run>-<vol>.nii`

One file per acquired volume. The name gives task, run, and volume:
`img-feedback-03-00042.nii` is Feedback run 3, volume 42. Tasks are `rest`,
`feedback`, `transferpre`, `transferpost`.

Rest images are kept (the masks are built from them). Feedback and transfer
images are removed after motion extraction; the scanner holds the full series,
and motion is in `ses-<TYPE>/derivatives/motion/`.

## Behavioral data: `ses-<TYPE>/sourcedata/psychopy/sub-<ID>/`

Per neurofeedback run (Transfer Pre, each Feedback, Transfer Post):

- **`..._roi_outputs.csv`**: the neurofeedback signal, one row per TR. Columns below.
- **`..._ses-<TYPE>_task-<task>_run-<NN>.tsv`**: the same data as a BIDS events file, with the slider answers repeated on every row.
- **`..._slider_questions.csv`**: the four post-run ratings (1-9).
- **`..._DMN_<task>_<run>.csv` / `.psydat`**: PsychoPy run metadata.
- **`..._DMN_<task>_<run>.log`**: PsychoPy event log, for timing.

### `roi_outputs.csv` columns

| Column | Meaning |
|---|---|
| `volume` | TR index, 0-based |
| `scale_factor` | ball gain (signal to displacement); 10 by default |
| `time` | seconds since the scanner trigger |
| `time_plus_1.2` | predicted onset of the next volume |
| `cen` | CEN activation from `mask/cen.nii`; positive above baseline, negative below |
| `dmn` | DMN activation from `mask/dmn.nii`; lower DMN means more mindful |
| `stage` | `baseline` (first ~25 TRs) or `feedback` |
| `cen_cumulative_hits` / `dmn_cumulative_hits` | times the ball reached the CEN/DMN target this run |
| `pda_outlier` | TR flagged as an outlier (cen minus dmn beyond threshold); `nan` in baseline |
| `ball_y_position` | ball y, normalized [-1, 1]; `nan` in baseline |
| `top_circle_y_position` / `bottom_circle_y_position` | DMN/CEN target y (0.333 / -0.333) |

A full run has 150 rows. A run that ended early (no activation arriving from
MURFI) has fewer.

The four slider questions:
1. How often were you using the mental noting practice?
2. How often did you check the position of the ball?
3. How difficult was it to apply mental noting?
4. How calm do you feel right now?

## Masks: `mask/`

`dmn.nii` and `cen.nii` are what MURFI loaded for feedback. The rest are
intermediates: `*_rest_original` (raw MELODIC output in rest space),
`*_studyref` (before erosion), `*_native_rest` (MELODIC copies).

## Session record: `ses-<TYPE>/session_state.json`

The steps and their status (`completed`, `failed`, ...), with start and finish
times and any error. `provenance.json` records the code version and the command
used to run the session.

## Copying

```bash
cp -r /path/to/sub-<ID>/ /destination/
```
