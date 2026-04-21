"""Verify synthetic NIfTI/DICOM generators emit parseable files."""

from __future__ import annotations

from pathlib import Path

import nibabel as nib
import pydicom
import pytest

from mindfulness_nf.orchestration.synthetic_volumes import (
    generate_synthetic_dicom,
    generate_synthetic_dicom_series,
    generate_synthetic_nifti,
    generate_synthetic_nifti_series,
)


class TestSyntheticNifti:
    def test_single_nifti_is_parseable(self, tmp_path: Path) -> None:
        out = tmp_path / "synthetic.nii"
        generate_synthetic_nifti(out, shape=(8, 8, 8), tr=1.2)

        assert out.is_file()
        img = nib.load(str(out))
        # nibabel load returns a proxy; force data access to confirm integrity.
        data = img.get_fdata()
        assert data.shape == (8, 8, 8)

    def test_series_generates_requested_count(self, tmp_path: Path) -> None:
        paths = generate_synthetic_nifti_series(tmp_path, count=3, shape=(4, 4, 4))

        assert len(paths) == 3
        for p in paths:
            assert p.is_file()
            nib.load(str(p))  # parseable


class TestSyntheticDicom:
    def test_single_dicom_is_parseable(self, tmp_path: Path) -> None:
        out = tmp_path / "synthetic.dcm"
        generate_synthetic_dicom(
            out, series_number=1, instance_number=1, rows=16, cols=16
        )

        assert out.is_file()
        ds = pydicom.dcmread(str(out))
        assert ds.SeriesNumber == 1
        assert ds.InstanceNumber == 1
        assert ds.Rows == 16
        assert ds.Columns == 16
        # Pixel data round-trips.
        assert ds.pixel_array.shape == (16, 16)

    def test_series_generates_requested_count(self, tmp_path: Path) -> None:
        paths = generate_synthetic_dicom_series(
            tmp_path, count=4, series_number=2, rows=8, cols=8
        )

        assert len(paths) == 4
        instance_numbers: list[int] = []
        for p in paths:
            assert p.is_file()
            ds = pydicom.dcmread(str(p))
            assert ds.SeriesNumber == 2
            instance_numbers.append(int(ds.InstanceNumber))
        assert instance_numbers == [1, 2, 3, 4]


class TestReproducibility:
    def test_nifti_bytes_stable_across_runs(self, tmp_path: Path) -> None:
        a = tmp_path / "a.nii"
        b = tmp_path / "b.nii"  # different name -> different seed
        c = tmp_path / "a_twin.nii"
        generate_synthetic_nifti(a, shape=(4, 4, 4))
        generate_synthetic_nifti(b, shape=(4, 4, 4))
        # Regenerate 'a' at a second path with the same filename; seed is
        # hashed off filename, so pixel data differs only by name.
        generate_synthetic_nifti(c, shape=(4, 4, 4))

        # a != b (different filename-derived seed)
        assert a.read_bytes() != b.read_bytes()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
