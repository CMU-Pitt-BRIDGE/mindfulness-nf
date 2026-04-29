"""Lint guards for bugs in the vendored PsychoPy scripts.

These scripts run inside PsychoPy's GUI and aren't unit-testable directly.
Instead, we lint the source for known-bad patterns so regressions show up
in CI. Each guard encodes a specific bug we fixed — see the docstring.
"""

from __future__ import annotations

import re
from pathlib import Path

BALLTASK_DIR = Path(__file__).resolve().parents[1] / "psychopy" / "balltask"
SCRIPT = BALLTASK_DIR / "rt-network_feedback.py"
BIDS_TSV = BALLTASK_DIR / "bids_tsv_convert_balltask.py"


def test_no_sys_exit_with_string_literal() -> None:
    """sys.exit(<string>) forces rc=1 on *successful* completion.

    Regression: rt-network_feedback.py used `sys.exit('Done with run')` at
    end-of-experiment. Python interprets the string as an error message and
    exits with code 1, which nf_run.py classified as a PsychoPy crash — so
    MURFI was kept alive indefinitely after a successful neurofeedback run.
    """
    src = SCRIPT.read_text()
    # Match sys.exit('...') or sys.exit("..."). Allow sys.exit() or sys.exit(0).
    bad = re.findall(r"sys\.exit\(\s*['\"][^'\"]*['\"]\s*\)", src)
    assert not bad, (
        f"sys.exit(<string>) forces rc=1; use sys.exit(0). Found: {bad}"
    )


def test_overwrite_dialog_compares_dict_value_not_key() -> None:
    """Overwrite-dialog check must look at the value, not dict membership.

    Regression: `f"Overwrite Run {N}" in warning_box_data` checked if the
    string was a KEY in the dict — but the only key is 'run_choice'. So
    choosing 'Overwrite Run N' silently fell into the 'next run' branch,
    which ALSO hardcoded `_DMN_Feedback_` in the filename. Two bugs in one
    block; fixed together by reading `warning_box_data['run_choice']`.
    """
    src = SCRIPT.read_text()
    # Forbidden: `in warning_box_data` or `not in warning_box_data` where
    # the LHS is a run-label literal. Acceptable: comparing the 'run_choice'
    # value directly.
    bad_key_check = re.search(
        r"Overwrite Run[^\n]*?\s(?:not\s+)?in\s+warning_box_data\b",
        src,
    )
    assert bad_key_check is None, (
        "Compare warning_box_data['run_choice'] directly — `in warning_box_data` "
        "checks dict KEYS, not the user's selection."
    )


def test_overwrite_branch_does_not_hardcode_feedback_filename() -> None:
    """When incrementing to next run on dialog, filename must preserve feedback_on.

    Regression: line 188 unconditionally wrote `_DMN_Feedback_` on the
    'next run' branch — which meant a No-Feedback run's overflow write
    silently produced a `_DMN_Feedback_` file, mis-labelling the data.
    """
    src = SCRIPT.read_text()
    # Look for the warning-dialog block specifically; the filename rebuild
    # inside must NOT unconditionally use 'Feedback'.
    # Guard: after the `expInfo['run'] = int(expInfo['run']) +1` line, the
    # next filename assignment must branch on feedback_on just like the
    # original setup does.
    match = re.search(
        r"expInfo\['run'\]\s*=\s*int\(expInfo\['run'\]\)\s*\+\s*1\s*\n\s*"
        r"filename\s*=\s*[^\n]+DMN_Feedback_",
        src,
    )
    assert match is None, (
        "Overwrite-dialog 'next run' branch hardcodes _DMN_Feedback_ — "
        "must branch on expInfo['feedback_on'] like the original setup."
    )


def test_main_csv_psydat_rename_suffixed_to_canonical() -> None:
    """ExperimentHandler's ``_1`` suffix must be renamed away at end of run.

    Regression: PsychoPy's ``ExperimentHandler`` with ``savePickle=True``
    appends ``_1`` to its main csv/psydat when other files already exist
    at the same base (our ``_roi_outputs.csv``). The script renames
    ``<filename>_1.csv`` → ``<filename>.csv`` so the output is clean.
    """
    src = SCRIPT.read_text()
    # The canonical-rename block must reference both .csv and .psydat
    # and be keyed on os.path.exists + os.rename (not a different approach).
    assert "_1" in src and "os.rename" in src, (
        "End-of-script must rename PsychoPy's _1-suffixed files to canonical"
    )
    # The rename must handle BOTH .csv and .psydat (both get suffixed).
    assert re.search(r"['\"]\.csv['\"].*['\"]\.psydat['\"]", src, re.DOTALL), (
        "Rename block must cover both .csv and .psydat"
    )


def test_anchor_shown_on_both_transfer_pre_and_post() -> None:
    """Transfer Pre and Transfer Post must both render the anchor reminder.

    Scientific rationale: Transfer Pre and Post are the baseline/retest
    pair for measuring transfer-of-training. They must be the same
    instruction condition — otherwise a Pre→Post difference could reflect
    the instruction asymmetry, not the training effect. The anchor
    reminder is the subject's intended cognitive tool for the practice
    and should be symmetric across the two measurement timepoints.

    Feedback runs intentionally omit the anchor — the moving ball is the
    attentional focus there. Don't add it to the feedback text blocks.
    """
    src = SCRIPT.read_text()
    # Both text blocks that drive the No-Feedback instruction slides must
    # interpolate expInfo['anchor'].
    m_pre = re.search(
        r"no_feedback_run1_text\s*=[^\n]*(?:\n[^\n]*){0,10}?anchor",
        src,
    )
    m_post = re.search(
        r"no_feedback_later_runs_text\s*=[^\n]*(?:\n[^\n]*){0,10}?anchor",
        src,
    )
    assert m_pre is not None, "Transfer Pre text must reference expInfo['anchor']"
    assert m_post is not None, (
        "Transfer Post text must also reference expInfo['anchor'] — "
        "Pre/Post asymmetry confounds the transfer-of-training measure."
    )


def test_trigger_wait_logs_all_keys_to_file() -> None:
    """Trigger wait must persist every key seen to a debug file.

    stdout from PsychoPy is piped by the TUI, so ``print()`` is invisible
    at scanner time. When the scanner isn't triggering, the operator
    needs a way to see whether PsychoPy is receiving keyboard input at
    all. Write each key to ``trigger_debug.log`` next to the run's data.
    """
    src = SCRIPT.read_text()
    assert "trigger_debug.log" in src, (
        "Trigger wait must write a trigger_debug.log in the participant "
        "data dir so operators can diagnose 't not received' issues."
    )


def test_trigger_wait_forces_window_focus() -> None:
    """PsychoPy window must ``activate()`` before the trigger loop.

    If another app (e.g. the TUI terminal) had focus when PsychoPy
    launched, manual 't' presses and scanner triggers go there, not
    to PsychoPy. ``win.winHandle.activate()`` forces focus programmatically.
    """
    src = SCRIPT.read_text()
    assert "winHandle.activate" in src, (
        "PsychoPy window must force keyboard focus before the trigger loop"
    )


def test_trigger_wait_catches_escape_from_raw_keys() -> None:
    """Escape must be checked against the raw key stream, not filtered.

    Regression: I briefly broke this by filtering ``theseKeys`` to only
    ``['t','+','5']`` and then checking ``if 'escape' in theseKeys`` —
    which never matched. The operator couldn't abort a stuck trigger
    wait. Escape must come from the unfiltered ``event.getKeys()`` list.
    """
    src = SCRIPT.read_text()
    # There must be an ``escape`` check against ``_all_keys`` (unfiltered),
    # not just against ``theseKeys`` (filtered to trigger chars only).
    assert re.search(r'"escape"\s+in\s+_all_keys', src), (
        "Escape must be checked against the unfiltered key stream"
    )


def test_filename_includes_task_when_env_set() -> None:
    """Filename must include task label when MINDFULNESS_NF_TASK is set.

    Regression: Transfer Pre and Transfer Post both have ``run=1`` in the
    BIDS session config (task-scoped run counters). Without a task label
    in the PsychoPy filename, both wrote to ``<sub>_DMN_No_Feedback_1_*``
    and collided — PsychoPy's overwrite dialog was the only safeguard,
    and choosing Overwrite destroyed the other run's data.
    """
    src = SCRIPT.read_text()
    # The filename-construction block must branch on MINDFULNESS_NF_TASK
    # and use it as a component of the filename pattern.
    assert "MINDFULNESS_NF_TASK" in src
    assert "_DMN_%s_%s" in src or "_DMN_{task}" in src, (
        "Filename must use task label when env var is set, "
        "e.g. ``%s_DMN_%s_%s`` format."
    )


def test_screen_number_is_env_configurable() -> None:
    """``screen=`` on visual.Window must be configurable via env var.

    Hardcoding ``screen=1`` meant single-monitor laptops (common for
    testing + some scanner setups) opened PsychoPy fullscreen on a
    nonexistent display, so keyboard focus couldn't land there and the
    't' trigger never registered. Honor ``MINDFULNESS_NF_SCREEN``.
    """
    src = SCRIPT.read_text()
    assert "MINDFULNESS_NF_SCREEN" in src, (
        "visual.Window screen index must honor MINDFULNESS_NF_SCREEN env var"
    )
    # screen=1 as a literal default in visual.Window should not be the only path.
    assert re.search(r"screen\s*=\s*_screen\b", src), (
        "visual.Window must pass screen=_screen (derived from env)"
    )


def test_bids_tsv_uses_env_for_session_and_task() -> None:
    """BIDS TSV filename must honor orchestrator-provided session + task.

    Regression: the writer hardcoded ``ses-nf`` (collided across rt15/rt30
    for the same subject) AND inferred ``task`` from ``run_num`` with
    legacy mappings (``run==2 → transferpost``) that are wrong for our
    rt15 protocol where ``run==2`` is Feedback 1. Both come from env
    vars the orchestrator sets: ``MINDFULNESS_NF_SESSION_TYPE`` +
    ``MINDFULNESS_NF_TASK``.
    """
    src = BIDS_TSV.read_text()
    assert "MINDFULNESS_NF_SESSION_TYPE" in src, (
        "BIDS TSV writer must read session from MINDFULNESS_NF_SESSION_TYPE env"
    )
    assert "MINDFULNESS_NF_TASK" in src, (
        "BIDS TSV writer must read task from MINDFULNESS_NF_TASK env"
    )
    # The hardcoded 'ses-nf' literal must not appear as the *only* source
    # of the session label. It's allowed as a fallback string but not as
    # a raw interpolation like ``'_ses-nf_task-'``.
    assert "'_ses-nf_task-'" not in src, (
        "Raw '_ses-nf_task-' literal must not appear — use env var"
    )


def test_psychopy_launcher_threads_session_and_task() -> None:
    """orchestration/psychopy.py launcher must pass session_type + task."""
    launcher = (
        Path(__file__).resolve().parents[1]
        / "mindfulness_nf"
        / "orchestration"
        / "psychopy.py"
    )
    src = launcher.read_text()
    assert "MINDFULNESS_NF_SESSION_TYPE" in src
    assert "MINDFULNESS_NF_TASK" in src
    # And nf_run.py must actually pass them.
    nf_run = (
        Path(__file__).resolve().parents[1]
        / "mindfulness_nf"
        / "orchestration"
        / "executors"
        / "nf_run.py"
    )
    nf_src = nf_run.read_text()
    assert "session_type=" in nf_src and "task=" in nf_src, (
        "nf_run.py must pass session_type + task to psychopy.launch"
    )


def test_rtdmn_xml_has_both_save_and_saveImages() -> None:
    """rtdmn.xml + 2vol.xml must have BOTH scanner options.

    Root cause of Bug C (sub-morgan's rt15 runs wrote 0 raw NIfTIs):
    rtdmn.xml had ``saveImages=true`` but was missing
    ``<option name="save">true</option>``. MURFI's convention — ``save``
    is the disk-write toggle, ``saveImages`` is the in-memory accept
    toggle — means both must be true for files to land on disk. rest.xml
    had both and worked; rtdmn/2vol were missing ``save`` and silently
    discarded all incoming volumes after processing them.
    """
    template_dir = (
        Path(__file__).resolve().parents[1]
        / "murfi" / "subjects" / "template" / "xml" / "xml_vsend"
    )
    for xml_name in ("rtdmn.xml", "2vol.xml"):
        src = (template_dir / xml_name).read_text()
        # Extract scanner block and check both options are present inside.
        scanner_match = re.search(r"<scanner>(.*?)</scanner>", src, re.DOTALL)
        assert scanner_match is not None, f"{xml_name} has no <scanner> block"
        scanner = scanner_match.group(1)
        assert re.search(r'name="save"[^>]*>\s*true', scanner), (
            f"{xml_name} must have <option name=\"save\">true</option> "
            "inside <scanner> — otherwise MURFI won't write raw volumes."
        )
        assert re.search(r'name="saveImages"[^>]*>\s*true', scanner), (
            f"{xml_name} must also have saveImages=true"
        )


def test_bids_tsv_participant_column_no_double_prefix() -> None:
    """BIDS TSV ``participant`` column must not double-prefix ``sub-``.

    Regression on sub-morgan (2026-04-21): every rt15 TSV row had
    ``participant = sub-sub-morgan`` because the writer did
    ``"sub-" + id`` on an already-prefixed id. The filename generator
    handled this correctly but the column did not — breaks group-level
    joins on participant.
    """
    src = BIDS_TSV.read_text()
    # Must not unconditionally concatenate "sub-" + the raw id into the column.
    bad = re.search(
        r'df\.participant\s*=\s*["\']sub-["\']\s*\+\s*df\.participant',
        src,
    )
    assert bad is None, (
        "Unconditional ``df.participant = 'sub-' + df.participant`` "
        "produces 'sub-sub-<name>' for ids that already carry 'sub-'."
    )
    # Positive signal: there should be a branch that checks the prefix.
    assert re.search(r"startswith\(['\"]sub-['\"]\)", src), (
        "participant column assignment must guard on existing 'sub-' prefix"
    )


def test_bids_tsv_duration_is_nonzero() -> None:
    """BIDS events duration must not be hardcoded 0.

    BIDS spec: zero-duration events are only valid for strictly
    instantaneous markers. Each row here represents a per-TR sample of
    streamed cen/dmn values, so duration = one TR = 1.2s.
    """
    src = BIDS_TSV.read_text()
    # Forbidden: exact ``df['duration']=0`` hardcode.
    assert "df['duration']=0" not in src and "df['duration'] = 0" not in src, (
        "duration must be the TR (1.2s), not 0"
    )


def test_bids_tsv_no_double_sub_prefix() -> None:
    """BIDS TSV filename must not produce `sub-sub-<name>` when id is already `sub-X`.

    Regression: bids_tsv_convert_balltask.py line 66 prepended `'sub-'`
    unconditionally. When participant id was already `sub-process-rehearse`
    the output was `sub-sub-process-rehearse_ses-nf_task-...tsv`.
    """
    src = BIDS_TSV.read_text()
    # Forbidden: `'sub-' + ... participant/id` concatenation without guard.
    # Acceptable: using the id as-is when it already starts with 'sub-',
    # or using lstrip / a helper.
    bad = re.search(
        r"['\"][^'\"]*sub-['\"]\s*\+\s*str\(slider_outputs\[['\"]id['\"]\]\[0\]\)",
        src,
    )
    assert bad is None, (
        "Unconditional 'sub-' + id concatenation will produce sub-sub-<name> "
        "when id already has the prefix. Strip it first."
    )
