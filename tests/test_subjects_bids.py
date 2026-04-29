"""Tests for the BIDS helpers in mindfulness_nf.orchestration.subjects.

Covers directory layout, session-state persistence (round-trip, schema
version, code-drift resilience, running→failed coercion), BIDS func path
naming, and per-step file cleanup.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mindfulness_nf import sessions as sessions_module
from mindfulness_nf.models import (
    SessionState,
    StepConfig,
    StepKind,
    StepState,
    StepStatus,
)
from mindfulness_nf.orchestration.subjects import (
    bids_func_path,
    clear_bids_run_files,
    create_subject_session_dir,
    load_bids_session_state,
    persist_bids_session_state,
    session_state_path,
)
from mindfulness_nf.sessions import SESSION_CONFIGS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_template_dir(tmp_path: Path) -> Path:
    """Build a minimal template dir with a couple of XMLs."""
    template_dir = tmp_path / "template"
    xml_vsend = template_dir / "xml" / "xml_vsend"
    xml_vsend.mkdir(parents=True)
    (xml_vsend / "2vol.xml").write_text("<config>vsend 2vol</config>")
    (xml_vsend / "rtdmn.xml").write_text("<config>vsend rtdmn</config>")
    (xml_vsend / "rest.xml").write_text("<config>vsend rest</config>")

    xml_dcm = template_dir / "xml" / "xml_dcm"
    xml_dcm.mkdir(parents=True)
    (xml_dcm / "2vol.xml").write_text("<config>dcm 2vol</config>")
    (xml_dcm / "rtdmn.xml").write_text("<config>dcm rtdmn</config>")
    (xml_dcm / "rest.xml").write_text(
        '<scanner>\n'
        '  <option name="imageSource"> DICOM </option>\n'
        '  <option name="inputDicomDir"> /old/hardcoded/path </option>\n'
        '</scanner>\n'
    )
    return template_dir


def _build_state(
    session_type: str,
    configs: tuple[StepConfig, ...] | None = None,
) -> SessionState:
    """Build a fresh SessionState from SESSION_CONFIGS (or override)."""
    if configs is None:
        configs = SESSION_CONFIGS[session_type]
    now = _now_iso()
    return SessionState(
        subject="sub-001",
        session_type=session_type,
        cursor=0,
        steps=tuple(StepState(config=c) for c in configs),
        created_at=now,
        updated_at=now,
    )


# ---------------------------------------------------------------------------
# create_subject_session_dir
# ---------------------------------------------------------------------------


class TestCreateSubjectSessionDir:
    def test_create_subject_session_dir_creates_bids_tree(
        self, tmp_path: Path
    ) -> None:
        """Creates the session tree and copies XML templates.

        Regression: previously we also created ``func/``,
        ``sourcedata/murfi/img/``, ``sourcedata/murfi/log/``, and
        ``derivatives/masks/`` — none of which were ever written to.
        Empty aspirational dirs misled anyone inspecting the layout; they
        have been dropped. Only directories the pipeline actually writes
        to are created here.
        """
        subjects_dir = tmp_path / "subjects"
        subjects_dir.mkdir()
        template_dir = _make_template_dir(tmp_path)

        session_dir = create_subject_session_dir(
            subjects_dir, "sub-001", "rt15", template_dir
        )

        assert session_dir == subjects_dir / "sub-001" / "ses-rt15"
        assert session_dir.is_dir()

        # Directories that are genuinely written to by the pipeline:
        for sub in (
            "log",                          # MURFI + orchestrator logs
            "rest",                         # Per-session 4D merges + preprocess
            "qc",                           # Per-session QC overlays
            "sourcedata/murfi/xml",         # Snapshot of XMLs-as-run
            "sourcedata/psychopy",          # PsychoPy writes here directly
        ):
            assert (session_dir / sub).is_dir(), f"missing {sub}"

        # Regression: these used to be created empty and confused RAs.
        for dead in (
            "func",
            "sourcedata/murfi/img",
            "sourcedata/murfi/log",
            "derivatives/masks",
        ):
            assert not (session_dir / dead).exists(), (
                f"{dead} must not be pre-created — only populated dirs "
                f"should appear in the session tree"
            )

        xml_dest = session_dir / "sourcedata" / "murfi" / "xml"
        assert (xml_dest / "2vol.xml").read_text() == "<config>vsend 2vol</config>"
        assert (xml_dest / "rtdmn.xml").read_text() == "<config>vsend rtdmn</config>"

    def test_create_subject_session_dir_idempotent_across_sessions(
        self, tmp_path: Path
    ) -> None:
        """Two distinct sessions for the same subject both succeed."""
        subjects_dir = tmp_path / "subjects"
        subjects_dir.mkdir()
        template_dir = _make_template_dir(tmp_path)

        loc3_dir = create_subject_session_dir(
            subjects_dir, "sub-001", "loc3", template_dir
        )
        rt15_dir = create_subject_session_dir(
            subjects_dir, "sub-001", "rt15", template_dir
        )

        assert loc3_dir.is_dir()
        assert rt15_dir.is_dir()
        assert loc3_dir != rt15_dir

    def test_loc3_session_seeds_from_xml_vsend(self, tmp_path: Path) -> None:
        """loc3 rest runs are real-time — scanner streams via vSend on
        port 50000. Session must be seeded from xml_vsend/, NOT xml_dcm/.
        Port 4006 / DICOM is reserved for post-hoc transfers (selfref).
        """
        subjects_dir = tmp_path / "subjects"
        subjects_dir.mkdir()
        template_dir = _make_template_dir(tmp_path)

        session_dir = create_subject_session_dir(
            subjects_dir, "sub-001", "loc3", template_dir
        )

        xml_dest = session_dir / "sourcedata" / "murfi" / "xml"
        rest_content = (xml_dest / "rest.xml").read_text()
        assert "vsend" in rest_content.lower(), (
            f"expected vsend flavor, got: {rest_content!r}"
        )

    def test_rt15_session_seeds_from_xml_vsend(self, tmp_path: Path) -> None:
        """rt15 uses vSend mode — session must be seeded from xml_vsend/."""
        subjects_dir = tmp_path / "subjects"
        subjects_dir.mkdir()
        template_dir = _make_template_dir(tmp_path)

        session_dir = create_subject_session_dir(
            subjects_dir, "sub-001", "rt15", template_dir
        )

        xml_dest = session_dir / "sourcedata" / "murfi" / "xml"
        assert "vsend" in (xml_dest / "2vol.xml").read_text()
        assert "vsend" in (xml_dest / "rtdmn.xml").read_text()

    def test_rt30_session_seeds_from_xml_vsend(self, tmp_path: Path) -> None:
        """rt30 uses vSend mode — same flavor as rt15."""
        subjects_dir = tmp_path / "subjects"
        subjects_dir.mkdir()
        template_dir = _make_template_dir(tmp_path)

        session_dir = create_subject_session_dir(
            subjects_dir, "sub-001", "rt30", template_dir
        )

        xml_dest = session_dir / "sourcedata" / "murfi" / "xml"
        assert "vsend" in (xml_dest / "rtdmn.xml").read_text()

    def test_inputDicomDir_rewrite_only_affects_xml_dcm_sessions(
        self, tmp_path: Path
    ) -> None:
        """The inputDicomDir rewriter used to run for loc3 when loc3 was
        DICOM-based. loc3 is now real-time vSend, so nothing in the seeded
        XML should contain the rewrite (no DICOM path references)."""
        subjects_dir = tmp_path / "subjects"
        subjects_dir.mkdir()
        template_dir = _make_template_dir(tmp_path)

        session_dir = create_subject_session_dir(
            subjects_dir, "sub-001", "loc3", template_dir
        )

        rest_xml = session_dir / "sourcedata" / "murfi" / "xml" / "rest.xml"
        content = rest_xml.read_text()
        assert "inputDicomDir" not in content, (
            "vsend rest.xml must not reference inputDicomDir"
        )
        assert "/old/hardcoded/path" not in content

    def test_create_subject_session_dir_raises_on_duplicate_same_session(
        self, tmp_path: Path
    ) -> None:
        """Re-creating the same (subject, session) raises FileExistsError."""
        subjects_dir = tmp_path / "subjects"
        subjects_dir.mkdir()
        template_dir = _make_template_dir(tmp_path)

        create_subject_session_dir(
            subjects_dir, "sub-001", "loc3", template_dir
        )

        with pytest.raises(FileExistsError, match="already exists"):
            create_subject_session_dir(
                subjects_dir, "sub-001", "loc3", template_dir
            )


# ---------------------------------------------------------------------------
# session_state_path
# ---------------------------------------------------------------------------


def test_session_state_path_is_under_session_dir(tmp_path: Path) -> None:
    """Returns session_dir / 'session_state.json'."""
    assert session_state_path(tmp_path) == tmp_path / "session_state.json"


# ---------------------------------------------------------------------------
# persist / load round trip + schema
# ---------------------------------------------------------------------------


class TestPersistAndLoad:
    def test_persist_and_load_round_trip(self, tmp_path: Path) -> None:
        """Round-trip: persisted rt15 state reloads with identical steps."""
        state = _build_state("rt15")

        persist_bids_session_state(tmp_path, state)
        loaded = load_bids_session_state(tmp_path)

        assert loaded is not None
        assert loaded.steps == state.steps
        assert loaded.subject == state.subject
        assert loaded.session_type == state.session_type
        assert loaded.cursor == state.cursor

    def test_persist_schema_version_is_1(self, tmp_path: Path) -> None:
        """Raw JSON records schema_version=1."""
        state = _build_state("loc3")
        persist_bids_session_state(tmp_path, state)

        raw = json.loads((tmp_path / "session_state.json").read_text())

        assert raw["schema_version"] == 1

    def test_persist_includes_full_stepconfig_per_step(
        self, tmp_path: Path
    ) -> None:
        """Each persisted step has a nested config with all 9 StepConfig fields."""
        state = _build_state("rt15")
        persist_bids_session_state(tmp_path, state)

        raw = json.loads((tmp_path / "session_state.json").read_text())

        expected_fields = {
            "name",
            "task",
            "run",
            "progress_target",
            "progress_unit",
            "xml_name",
            "kind",
            "feedback",
            "fsl_command",
        }
        for step in raw["steps"]:
            assert "config" in step
            assert set(step["config"].keys()) == expected_fields

    def test_load_returns_none_when_file_missing(
        self, tmp_path: Path
    ) -> None:
        """Loading from an empty dir returns None."""
        assert load_bids_session_state(tmp_path) is None

    def test_load_rejects_unknown_schema_version(
        self, tmp_path: Path
    ) -> None:
        """A file with an unrecognized schema_version raises ValueError."""
        payload = {
            "schema_version": 99,
            "subject": "sub-001",
            "session_type": "rt15",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "cursor": 0,
            "steps": [],
        }
        (tmp_path / "session_state.json").write_text(json.dumps(payload))

        with pytest.raises(ValueError, match="schema_version"):
            load_bids_session_state(tmp_path)


# ---------------------------------------------------------------------------
# running → failed coercion
# ---------------------------------------------------------------------------


class TestRunningCoercion:
    def test_load_coerces_running_to_failed(self, tmp_path: Path) -> None:
        """A RUNNING step becomes FAILED with error='interrupted by restart'."""
        state = _build_state("rt15")
        running_step = StepState(
            config=state.steps[1].config,
            status=StepStatus.RUNNING,
            last_started=_now_iso(),
        )
        state_with_running = SessionState(
            subject=state.subject,
            session_type=state.session_type,
            cursor=state.cursor,
            steps=state.steps[:1] + (running_step,) + state.steps[2:],
            created_at=state.created_at,
            updated_at=state.updated_at,
        )

        persist_bids_session_state(tmp_path, state_with_running)
        loaded = load_bids_session_state(tmp_path)

        assert loaded is not None
        coerced = loaded.steps[1]
        assert coerced.status is StepStatus.FAILED
        assert coerced.error == "interrupted by restart"

    def test_load_coerces_running_also_clears_phase_and_awaiting_advance(
        self, tmp_path: Path
    ) -> None:
        """Pin behavior: phase + awaiting_advance passthrough on RUNNING->FAILED.

        The implementation coerces ``status`` and ``error`` only; other
        runtime bookkeeping fields (``phase``, ``awaiting_advance``) are
        preserved verbatim from disk.  This test pins that contract.
        """
        state = _build_state("rt15")
        running_step = StepState(
            config=state.steps[2].config,
            status=StepStatus.RUNNING,
            last_started=_now_iso(),
            phase="murfi",
            awaiting_advance=True,
        )
        state_with_running = SessionState(
            subject=state.subject,
            session_type=state.session_type,
            cursor=state.cursor,
            steps=state.steps[:2] + (running_step,) + state.steps[3:],
            created_at=state.created_at,
            updated_at=state.updated_at,
        )

        persist_bids_session_state(tmp_path, state_with_running)
        loaded = load_bids_session_state(tmp_path)

        assert loaded is not None
        coerced = loaded.steps[2]
        assert coerced.status is StepStatus.FAILED
        assert coerced.error == "interrupted by restart"
        # Pinned behavior: these fields are preserved as persisted.
        assert coerced.phase == "murfi"
        assert coerced.awaiting_advance is True


# ---------------------------------------------------------------------------
# Code-drift resilience
# ---------------------------------------------------------------------------


def test_load_preserves_persisted_configs_when_SESSION_CONFIGS_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loaded steps use persisted configs, not the current SESSION_CONFIGS.

    Persist with progress_target=150 on a feedback step, then simulate code
    drift by swapping SESSION_CONFIGS["rt15"] to a tuple whose feedback steps
    have progress_target=999.  Reload.  The loaded state must reflect the
    persisted value (150), not the drifted value (999).
    """
    state = _build_state("rt15")
    persist_bids_session_state(tmp_path, state)
    original_feedback = state.steps[3].config
    assert original_feedback.progress_target == 150

    drifted: tuple[StepConfig, ...] = tuple(
        StepConfig(
            name=c.name,
            task=c.task,
            run=c.run,
            progress_target=(999 if c.kind is StepKind.NF_RUN else c.progress_target),
            progress_unit=c.progress_unit,
            xml_name=c.xml_name,
            kind=c.kind,
            feedback=c.feedback,
            fsl_command=c.fsl_command,
        )
        for c in SESSION_CONFIGS["rt15"]
    )
    monkeypatch.setitem(sessions_module.SESSION_CONFIGS, "rt15", drifted)

    loaded = load_bids_session_state(tmp_path)

    assert loaded is not None
    assert loaded.steps[3].config.progress_target == 150
    # And confirm the drift is actually in place (guards against false green).
    assert sessions_module.SESSION_CONFIGS["rt15"][3].progress_target == 999


# ---------------------------------------------------------------------------
# bids_func_path
# ---------------------------------------------------------------------------


def test_bids_func_path_matches_scanner_pdf_naming(tmp_path: Path) -> None:
    """Filename matches BIDS scanner-PDF naming with zero-padded run."""
    result = bids_func_path(tmp_path, "sub-001", "rt15", "feedback", 3)

    assert result == (
        tmp_path / "func" / "sub-001_ses-rt15_task-feedback_run-03_bold.nii"
    )


# ---------------------------------------------------------------------------
# clear_bids_run_files
# ---------------------------------------------------------------------------


def _write(path: Path, data: bytes = b"data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _feedback_step(run: int, series: int) -> StepConfig:
    """Build a feedback step where MURFI series index == ``series``."""
    return StepConfig(
        name=f"Feedback {run}",
        task="feedback",
        run=series,  # clear_bids_run_files uses step.run as the series number
        progress_target=150,
        progress_unit="volumes",
        xml_name="rtdmn.xml",
        kind=StepKind.NF_RUN,
        feedback=True,
    )


class TestClearBidsRunFiles:
    def test_clear_bids_run_files_removes_rest_files(
        self, tmp_path: Path
    ) -> None:
        """Removes step's 4D merge + sidecars from session-scoped ``rest/``.

        Regression: this test used to assert deletion under ``func/``. The
        pipeline never wrote 4D BOLDs to ``func/`` — it writes to
        ``<session_dir>/rest/`` (session-scoped post-migration) and
        ``func/`` is reserved for a future BIDSify symlink step.
        """
        step = StepConfig(
            name="Feedback 2",
            task="feedback",
            run=2,
            progress_target=150,
            progress_unit="volumes",
            xml_name="rtdmn.xml",
            kind=StepKind.NF_RUN,
            feedback=True,
        )
        nii = (
            tmp_path
            / "rest"
            / "sub-001_ses-rt15_task-feedback_run-02_bold.nii"
        )
        sidecar = (
            tmp_path
            / "rest"
            / "sub-001_ses-rt15_task-feedback_run-02_bold.json"
        )
        _write(nii)
        _write(sidecar, b"{}")

        clear_bids_run_files(tmp_path, "sub-001", "rt15", step)

        assert not nii.exists()
        assert not sidecar.exists()

    def test_clear_bids_run_files_removes_task_keyed_img_files(
        self, tmp_path: Path
    ) -> None:
        """Removes raw MURFI volumes for the step's (task, run) from subject-root ``img/``.

        Regression: ``clear_bids_run_files`` used to glob ``img-<run:05d>-*.nii``
        (run-only keying). That matched Rest 1 (ses-loc3, run=1) when the
        operator restarted Transfer Pre (ses-rt15, run=1) — sub-morgan's
        Rest 1 data was wiped as a side-effect, 2026-04-21. The glob now
        keys on (task, run) so only this step's files get deleted.
        """
        step = _feedback_step(run=3, series=3)
        session_dir = tmp_path / "sub-001" / "ses-rt15"
        session_dir.mkdir(parents=True)
        img_dir = tmp_path / "sub-001" / "img"
        # This step's own files (task=feedback, run=3):
        for i in (1, 2, 3):
            _write(img_dir / f"img-feedback-03-{i:05d}.nii")
        # Another task's files that must survive:
        _write(img_dir / "img-rest-01-00001.nii")
        _write(img_dir / "img-rest-01-00002.nii")

        clear_bids_run_files(session_dir, "sub-001", "rt15", step)

        assert list(img_dir.glob("img-feedback-03-*.nii")) == []
        # Other task's files untouched — this is the anti-regression.
        assert len(list(img_dir.glob("img-rest-01-*.nii"))) == 2

    def test_clear_bids_run_files_does_not_touch_other_steps_data(
        self, tmp_path: Path
    ) -> None:
        """Clearing step 2 leaves step 3's files intact."""
        step2 = _feedback_step(run=2, series=2)
        session_dir = tmp_path / "sub-001" / "ses-rt15"
        session_dir.mkdir(parents=True)
        img_dir = tmp_path / "sub-001" / "img"
        _write(img_dir / "img-feedback-02-00001.nii")
        _write(img_dir / "img-feedback-02-00002.nii")
        _write(img_dir / "img-feedback-03-00001.nii")
        _write(img_dir / "img-feedback-03-00002.nii")

        rest_dir = session_dir / "rest"
        step2_nii = rest_dir / "sub-001_ses-rt15_task-feedback_run-02_bold.nii"
        step3_nii = rest_dir / "sub-001_ses-rt15_task-feedback_run-03_bold.nii"
        _write(step2_nii)
        _write(step3_nii)

        clear_bids_run_files(session_dir, "sub-001", "rt15", step2)

        # Step 2 wiped.
        assert list(img_dir.glob("img-feedback-02-*.nii")) == []
        assert not step2_nii.exists()
        # Step 3 untouched.
        assert len(list(img_dir.glob("img-feedback-03-*.nii"))) == 2
        assert step3_nii.exists()
