# Dry-run rehearsal checklist

Run this checklist before every scanner day. It catches regressions between code changes and the next live session. The goal: prove that the system you are about to take to the scanner behaves the way the operator expects, including under failure.

Budget: 20-30 minutes. Do not skip steps.

Subject ID convention for rehearsals: `sub-rehearse-YYYYMMDD` (e.g., `sub-rehearse-20260420`).

---

## 0. One-time setup: real BOLD for Process-session rehearsal (optional)

Without a real-BOLD cache, dry-run falls back to random-noise volumes and the Process session stubs FSL MELODIC with placeholder files. To rehearse the Process session end-to-end with real FSL running on real public data:

- [ ] Install the `dry-run` extra: `uv sync --extra dry-run`
- [ ] Fetch once (~100 MB, cached by nilearn): `uv run python scripts/fetch_dry_run_bold.py`
- [ ] Verify: `ls murfi/dry_run_cache_bold/nifti/` lists per-volume NIfTIs

When this cache is populated, `SimulatedScannerSource` prefers it over synthetic volumes and `FslStageExecutor(dry_run=True)` runs the real FSL subprocess instead of stubbing. The cache is gitignored; a single fetch covers every subsequent rehearsal.

---

## 1. Preflight tests

- [ ] Run `bash scripts/preflight_test.sh`
- [ ] Verify the green banner prints at the end
- [ ] If any test fails, stop. Do not rehearse further. Fix the failing test before the scanner session.

## 2. Dry-run walk-through of the live session type

Pick the session type you will run live today: RT15 or RT30. Rehearse that exact type.

- [ ] Confirm the dry-run cache exists: `ls murfi/dry_run_cache/` returns volumes
- [ ] If the cache is empty, populate it: `uv run python scripts/populate_dry_run_cache.py <real_session_path>`
- [ ] Launch: `uv run mindfulness-nf --dry-run --subject sub-rehearse-YYYYMMDD`
- [ ] Press `2` for RT15 or `3` for RT30
- [ ] Setup step: press `d`; verify preflight checks pass green
- [ ] 2-volume step: press `d`; wait for 2/2 volumes; verify green
- [ ] Transfer Pre: press `d`; wait for MURFI to reach 150/150; press `d` to gate to PsychoPy; let PsychoPy run to completion
- [ ] Feedback 1-5 (RT15) or Feedback 1-5, Transfer Post 1, Feedback 6-10 (RT30): for each, press `d`, wait for MURFI, press `d` at the phase gate, let PsychoPy finish
- [ ] Final Transfer Post: complete as above
- [ ] Verify every step shows `completed` and the session summary renders a full-green list

## 3. Crash recovery

Start a fresh dry-run for each sub-scenario. Reuse the same subject ID; the runner will resume and you can press `r` on any step to re-exercise it.

### 3a. Operator interrupt (`i`)

- [ ] Start Feedback 2; wait until 50/150 volumes appear
- [ ] Press `i`
- [ ] Verify the step's `.nii` files under `func/` are deleted
- [ ] Verify status returns to `pending`, progress to `0/150`, cursor unchanged

### 3b. MURFI crash and recover (`r`)

- [ ] Start Feedback 2; wait until 50/150 volumes
- [ ] From another terminal: `killall murfi` (or the apptainer process name shown by `ps`)
- [ ] Verify the TUI marks Feedback 2 `FAILED` with error text naming MURFI
- [ ] Press `r`
- [ ] Verify files cleared, `attempts` incremented, step running again

### 3c. PsychoPy crash and relaunch (`p`)

- [ ] Advance Feedback 2 past the MURFI phase into PsychoPy
- [ ] From another terminal: `killall python` (target the PsychoPy subprocess)
- [ ] Verify status stays `RUNNING`, help bar shows `[p] Relaunch PsychoPy`
- [ ] Press `p`
- [ ] Verify PsychoPy relaunches; MURFI stays alive; volume count is unchanged

## 4. Resume

- [ ] Start any session; complete through at least Feedback 1
- [ ] In the middle of Feedback 2, force-quit the TUI with `Ctrl+\`
- [ ] Relaunch: `uv run mindfulness-nf --dry-run --subject sub-rehearse-YYYYMMDD`
- [ ] Pick the same session type
- [ ] Verify the cursor lands on Feedback 2
- [ ] Verify Feedback 2 shows status `FAILED` with error `interrupted by restart`
- [ ] Verify Feedback 1 and earlier still show `completed`
- [ ] Press `r` to clear and restart Feedback 2; verify it runs

## 5. BIDS tree inspection

- [ ] `ls subjects/sub-rehearse-YYYYMMDD/`
- [ ] Verify the session directory exists (`ses-rt15/` or `ses-rt30/`)
- [ ] `ls subjects/sub-rehearse-YYYYMMDD/ses-rt15/`
- [ ] Verify `func/`, `sourcedata/`, `derivatives/`, and `session_state.json` are all present
- [ ] `ls subjects/sub-rehearse-YYYYMMDD/ses-rt15/func/` shows one BIDS-named `.nii` per completed run
- [ ] `cat subjects/sub-rehearse-YYYYMMDD/ses-rt15/session_state.json | head` shows the expected `subject`, `session_type`, `cursor`, and step statuses

## 6. Sign-off

- [ ] All steps above checked and green
- [ ] Rehearsal subject directory cleaned up or kept for audit (operator's choice)
- [ ] Date: __________
- [ ] Operator initials: __________

If any item failed, file an issue and do not run the live session until the failure is resolved.
