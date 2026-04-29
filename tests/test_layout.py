"""Tests for SubjectLayout — single source of truth for pipeline paths."""

from __future__ import annotations

from pathlib import Path

import pytest

from mindfulness_nf.orchestration.layout import SubjectLayout


@pytest.fixture
def layout(tmp_path: Path) -> SubjectLayout:
    (tmp_path / "sub-001" / "ses-loc3").mkdir(parents=True)
    return SubjectLayout(
        subjects_root=tmp_path,
        subject_id="sub-001",
        session_type="loc3",
    )


class TestSubjectLevelPaths:
    """Shared across every session for a subject."""

    def test_subject_root(self, layout: SubjectLayout, tmp_path: Path) -> None:
        assert layout.subject_root == (tmp_path / "sub-001").resolve()

    def test_img_dir(self, layout: SubjectLayout, tmp_path: Path) -> None:
        assert layout.img_dir == (tmp_path / "sub-001" / "img").resolve()

    def test_xfm_dir(self, layout: SubjectLayout, tmp_path: Path) -> None:
        assert layout.xfm_dir == (tmp_path / "sub-001" / "xfm").resolve()

    def test_mask_dir(self, layout: SubjectLayout, tmp_path: Path) -> None:
        assert layout.mask_dir == (tmp_path / "sub-001" / "mask").resolve()

    def test_subject_xml_dir(self, layout: SubjectLayout, tmp_path: Path) -> None:
        assert layout.subject_xml_dir == (tmp_path / "sub-001" / "xml").resolve()

    def test_subject_log_dir(self, layout: SubjectLayout, tmp_path: Path) -> None:
        assert layout.subject_log_dir == (tmp_path / "sub-001" / "log").resolve()

    def test_study_ref(self, layout: SubjectLayout, tmp_path: Path) -> None:
        assert (
            layout.study_ref
            == (tmp_path / "sub-001" / "xfm" / "study_ref.nii").resolve()
        )

    def test_img_run_glob_pads_to_5(self, layout: SubjectLayout) -> None:
        assert layout.img_run_glob(1) == "img-00001-*.nii"
        assert layout.img_run_glob(42) == "img-00042-*.nii"

    def test_series_ref_glob(self, layout: SubjectLayout) -> None:
        assert layout.series_ref_glob() == "series*_ref.nii"


class TestSessionScopedPaths:
    """Per-session — cannot mingle across sessions."""

    def test_session_dir_uses_session_type(
        self, layout: SubjectLayout, tmp_path: Path
    ) -> None:
        assert (
            layout.session_dir == (tmp_path / "sub-001" / "ses-loc3").resolve()
        )

    def test_rt15_session_dir(self, tmp_path: Path) -> None:
        (tmp_path / "sub-X" / "ses-rt15").mkdir(parents=True)
        layout = SubjectLayout(
            subjects_root=tmp_path, subject_id="sub-X", session_type="rt15"
        )
        assert (
            layout.session_dir == (tmp_path / "sub-X" / "ses-rt15").resolve()
        )

    def test_session_state_json(self, layout: SubjectLayout, tmp_path: Path) -> None:
        assert (
            layout.session_state_json
            == (tmp_path / "sub-001" / "ses-loc3" / "session_state.json").resolve()
        )

    def test_provenance_json(self, layout: SubjectLayout, tmp_path: Path) -> None:
        assert (
            layout.provenance_json
            == (tmp_path / "sub-001" / "ses-loc3" / "provenance.json").resolve()
        )

    def test_session_log_dir(self, layout: SubjectLayout, tmp_path: Path) -> None:
        assert (
            layout.session_log_dir
            == (tmp_path / "sub-001" / "ses-loc3" / "log").resolve()
        )

    def test_rest_dir_is_session_scoped(
        self, layout: SubjectLayout, tmp_path: Path
    ) -> None:
        """Regression: rest_dir used to live at the SUBJECT root, causing
        two sessions to clobber each other's 4D merges. It now lives under
        the session dir so ses-process and ses-rt15 cannot mingle."""
        assert (
            layout.rest_dir
            == (tmp_path / "sub-001" / "ses-loc3" / "rest").resolve()
        )

    def test_qc_dir_is_session_scoped(
        self, layout: SubjectLayout, tmp_path: Path
    ) -> None:
        assert (
            layout.qc_dir == (tmp_path / "sub-001" / "ses-loc3" / "qc").resolve()
        )

    def test_ses_sourcedata_xml_dir(
        self, layout: SubjectLayout, tmp_path: Path
    ) -> None:
        assert (
            layout.ses_sourcedata_xml_dir
            == (tmp_path / "sub-001" / "ses-loc3" / "sourcedata" / "murfi" / "xml").resolve()
        )

    def test_ses_sourcedata_dicom_dir(
        self, layout: SubjectLayout, tmp_path: Path
    ) -> None:
        assert (
            layout.ses_sourcedata_dicom_dir
            == (tmp_path / "sub-001" / "ses-loc3" / "sourcedata" / "dicom").resolve()
        )

    def test_psychopy_data_dir_lives_in_session(
        self, layout: SubjectLayout, tmp_path: Path
    ) -> None:
        """PsychoPy data used to live in a sibling tree
        (``psychopy/balltask/data/``); invisible to anyone copying the
        subject dir. Now routes into the session tree."""
        assert (
            layout.psychopy_data_dir
            == (tmp_path / "sub-001" / "ses-loc3" / "sourcedata" / "psychopy").resolve()
        )


class TestBidsFilenames:
    def test_bold_bids_name_uses_session_type_not_hardcoded_localizer(
        self, layout: SubjectLayout
    ) -> None:
        """Historic bug: filenames hardcoded ``ses-localizer`` regardless of type."""
        name = layout.bold_bids_name(task="rest", run=1)
        assert name == "sub-001_ses-loc3_task-rest_run-01_bold.nii"
        assert "ses-localizer" not in name  # regression guard

    def test_bold_bids_name_pads_run(self, layout: SubjectLayout) -> None:
        assert (
            layout.bold_bids_name(task="rest", run=10)
            == "sub-001_ses-loc3_task-rest_run-10_bold.nii"
        )

    def test_bold_bids_name_custom_suffix(self, layout: SubjectLayout) -> None:
        assert (
            layout.bold_bids_name(task="rest", run=1, suffix="bold.json")
            == "sub-001_ses-loc3_task-rest_run-01_bold.json"
        )

    def test_bold_rest_intermediate_now_session_scoped(
        self, layout: SubjectLayout, tmp_path: Path
    ) -> None:
        """Regression: was under subject root ``rest/``; moved to session-level."""
        assert layout.bold_rest_intermediate(task="rest", run=2) == (
            (
                tmp_path
                / "sub-001"
                / "ses-loc3"
                / "rest"
                / "sub-001_ses-loc3_task-rest_run-02_bold.nii"
            ).resolve()
        )


class TestFromSessionDir:
    def test_reconstructs_layout_from_bids_session_dir(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "murfi" / "subjects" / "sub-042" / "ses-rt30"
        session_dir.mkdir(parents=True)
        layout = SubjectLayout.from_session_dir(session_dir)
        assert layout.subjects_root == (tmp_path / "murfi" / "subjects").resolve()
        assert layout.subject_id == "sub-042"
        assert layout.session_type == "rt30"

    def test_round_trips(self, tmp_path: Path) -> None:
        (tmp_path / "sub-X" / "ses-loc3").mkdir(parents=True)
        original = SubjectLayout(
            subjects_root=tmp_path, subject_id="sub-X", session_type="loc3"
        )
        reconstructed = SubjectLayout.from_session_dir(original.session_dir)
        assert reconstructed.subject_id == original.subject_id
        assert reconstructed.session_type == original.session_type

    def test_rejects_non_session_dir(self, tmp_path: Path) -> None:
        bad_dir = tmp_path / "sub-X" / "not-a-session"
        bad_dir.mkdir(parents=True)
        with pytest.raises(ValueError, match="ses-"):
            SubjectLayout.from_session_dir(bad_dir)


class TestValidation:
    def test_rejects_empty_subject_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="subject_id"):
            SubjectLayout(subjects_root=tmp_path, subject_id="", session_type="rt15")

    def test_rejects_empty_session_type(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="session_type"):
            SubjectLayout(subjects_root=tmp_path, subject_id="sub-X", session_type="")

    def test_rejects_subject_id_as_path(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="path"):
            SubjectLayout(
                subjects_root=tmp_path, subject_id="/sub-X", session_type="rt15"
            )


class TestEqualityAndHashing:
    def test_equal_layouts_built_differently_hash_equal(self, tmp_path: Path) -> None:
        """Regression: from_session_dir used to resolve but the default ctor
        didn't, so two logically-equal layouts hashed differently."""
        (tmp_path / "sub-X" / "ses-rt15").mkdir(parents=True)
        a = SubjectLayout(
            subjects_root=tmp_path, subject_id="sub-X", session_type="rt15"
        )
        b = SubjectLayout.from_session_dir(tmp_path / "sub-X" / "ses-rt15")
        assert a == b
        assert hash(a) == hash(b)


class TestFrozen:
    def test_layout_is_hashable(self, layout: SubjectLayout) -> None:
        _ = {layout}  # must not raise

    def test_layout_is_immutable(self, layout: SubjectLayout) -> None:
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            layout.subject_id = "sub-999"  # type: ignore[misc]
