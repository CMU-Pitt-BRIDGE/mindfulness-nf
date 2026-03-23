"""Tests for mindfulness_nf.orchestration.subjects."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mindfulness_nf.orchestration.subjects import (
    clear_partial_data,
    create_subject,
    load_session_state,
    save_session_state,
    subject_exists,
    validate_step_data,
)


# ---------------------------------------------------------------------------
# create_subject
# ---------------------------------------------------------------------------


class TestCreateSubject:
    """Tests for create_subject."""

    def test_creates_correct_directory_structure(self, tmp_path: Path) -> None:
        subjects_dir = tmp_path / "subjects"
        subjects_dir.mkdir()

        # Set up a template directory with XML files.
        template_dir = tmp_path / "template"
        xml_vsend = template_dir / "xml" / "xml_vsend"
        xml_vsend.mkdir(parents=True)
        (xml_vsend / "2vol.xml").write_text("<config>2vol</config>")
        (xml_vsend / "rtdmn.xml").write_text("<config>rtdmn</config>")
        (xml_vsend / "rest.xml").write_text("<config>rest</config>")

        result = create_subject(subjects_dir, "sub-001", template_dir)

        assert result == subjects_dir / "sub-001"
        assert result.is_dir()

        # Check standard subdirectories.
        for subdir in ("xml", "mask", "mask/qc", "img", "log", "xfm", "rest"):
            assert (result / subdir).is_dir(), f"Missing subdir: {subdir}"

        # Check XML templates were copied.
        assert (result / "xml" / "2vol.xml").read_text() == "<config>2vol</config>"
        assert (result / "xml" / "rtdmn.xml").read_text() == "<config>rtdmn</config>"
        assert (result / "xml" / "rest.xml").read_text() == "<config>rest</config>"

    def test_raises_on_existing_directory(self, tmp_path: Path) -> None:
        subjects_dir = tmp_path / "subjects"
        subjects_dir.mkdir()

        template_dir = tmp_path / "template"
        xml_vsend = template_dir / "xml" / "xml_vsend"
        xml_vsend.mkdir(parents=True)

        create_subject(subjects_dir, "sub-001", template_dir)

        with pytest.raises(FileExistsError, match="already exists"):
            create_subject(subjects_dir, "sub-001", template_dir)

    def test_works_without_xml_template_dir(self, tmp_path: Path) -> None:
        """Template dir missing xml/xml_vsend is tolerated (no files copied)."""
        subjects_dir = tmp_path / "subjects"
        subjects_dir.mkdir()
        template_dir = tmp_path / "empty_template"
        template_dir.mkdir()

        result = create_subject(subjects_dir, "sub-002", template_dir)
        assert result.is_dir()
        # xml/ exists but is empty.
        assert list((result / "xml").iterdir()) == []


# ---------------------------------------------------------------------------
# subject_exists
# ---------------------------------------------------------------------------


class TestSubjectExists:
    def test_true_when_exists(self, tmp_path: Path) -> None:
        (tmp_path / "sub-001").mkdir()
        assert subject_exists(tmp_path, "sub-001") is True

    def test_false_when_missing(self, tmp_path: Path) -> None:
        assert subject_exists(tmp_path, "sub-999") is False

    def test_false_for_file_not_dir(self, tmp_path: Path) -> None:
        (tmp_path / "sub-001").write_text("not a directory")
        assert subject_exists(tmp_path, "sub-001") is False


# ---------------------------------------------------------------------------
# save_session_state / load_session_state
# ---------------------------------------------------------------------------


class TestSessionState:
    def test_round_trip(self, tmp_path: Path) -> None:
        state_file = tmp_path / "session_state.json"

        save_session_state(state_file, "sub-001", "localizer", 3)
        loaded = load_session_state(state_file)

        assert loaded is not None
        assert loaded["subject"] == "sub-001"
        assert loaded["session"] == "localizer"
        assert loaded["last_completed_step"] == 3
        assert "timestamp" in loaded
        # Timestamp should be valid ISO 8601.
        assert "T" in loaded["timestamp"]

    def test_load_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        state_file = tmp_path / "nonexistent.json"
        assert load_session_state(state_file) is None

    def test_atomic_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        state_file = tmp_path / "deep" / "nested" / "state.json"
        save_session_state(state_file, "sub-002", "nf", 0)

        loaded = load_session_state(state_file)
        assert loaded is not None
        assert loaded["subject"] == "sub-002"

    def test_overwrite_existing_state(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"

        save_session_state(state_file, "sub-001", "localizer", 1)
        save_session_state(state_file, "sub-001", "localizer", 5)

        loaded = load_session_state(state_file)
        assert loaded is not None
        assert loaded["last_completed_step"] == 5

    def test_state_is_valid_json(self, tmp_path: Path) -> None:
        state_file = tmp_path / "state.json"
        save_session_state(state_file, "sub-001", "nf", 2)

        raw = state_file.read_text()
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# validate_step_data
# ---------------------------------------------------------------------------


def _create_volumes(
    subject_dir: Path, step_index: int, count: int, *, size: int = 100
) -> None:
    """Helper to create fake volume files."""
    img_dir = subject_dir / "img"
    img_dir.mkdir(parents=True, exist_ok=True)
    series = f"{step_index + 1:05d}"
    for i in range(1, count + 1):
        vol = img_dir / f"img-{series}-{i:05d}.nii"
        vol.write_bytes(b"\x00" * size)


class TestValidateStepData:
    def test_valid_with_correct_count(self, tmp_path: Path) -> None:
        _create_volumes(tmp_path, step_index=0, count=20)
        assert validate_step_data(tmp_path, step_index=0, expected_volumes=20) is True

    def test_invalid_with_fewer_volumes(self, tmp_path: Path) -> None:
        _create_volumes(tmp_path, step_index=0, count=15)
        assert validate_step_data(tmp_path, step_index=0, expected_volumes=20) is False

    def test_invalid_with_more_volumes(self, tmp_path: Path) -> None:
        _create_volumes(tmp_path, step_index=0, count=25)
        assert validate_step_data(tmp_path, step_index=0, expected_volumes=20) is False

    def test_invalid_with_empty_file(self, tmp_path: Path) -> None:
        _create_volumes(tmp_path, step_index=0, count=20, size=100)
        # Make one file empty.
        series = "00001"
        empty_file = tmp_path / "img" / f"img-{series}-00010.nii"
        empty_file.write_bytes(b"")
        assert validate_step_data(tmp_path, step_index=0, expected_volumes=20) is False

    def test_invalid_with_no_img_dir(self, tmp_path: Path) -> None:
        assert validate_step_data(tmp_path, step_index=0, expected_volumes=20) is False

    def test_different_step_indices(self, tmp_path: Path) -> None:
        """Step index 2 should look for series 00003."""
        _create_volumes(tmp_path, step_index=2, count=150)
        assert validate_step_data(tmp_path, step_index=2, expected_volumes=150) is True
        # Step 0 has no volumes.
        assert validate_step_data(tmp_path, step_index=0, expected_volumes=150) is False


# ---------------------------------------------------------------------------
# clear_partial_data
# ---------------------------------------------------------------------------


class TestClearPartialData:
    def test_removes_volumes_for_step(self, tmp_path: Path) -> None:
        _create_volumes(tmp_path, step_index=0, count=10)
        _create_volumes(tmp_path, step_index=1, count=5)

        clear_partial_data(tmp_path, step_index=0)

        # Step 0 volumes should be gone.
        series0 = list((tmp_path / "img").glob("img-00001-*.nii"))
        assert series0 == []

        # Step 1 volumes should remain.
        series1 = list((tmp_path / "img").glob("img-00002-*.nii"))
        assert len(series1) == 5

    def test_noop_when_no_img_dir(self, tmp_path: Path) -> None:
        # Should not raise.
        clear_partial_data(tmp_path, step_index=0)

    def test_noop_when_no_matching_files(self, tmp_path: Path) -> None:
        _create_volumes(tmp_path, step_index=1, count=5)
        clear_partial_data(tmp_path, step_index=0)
        # Step 1 files still there.
        series1 = list((tmp_path / "img").glob("img-00002-*.nii"))
        assert len(series1) == 5
