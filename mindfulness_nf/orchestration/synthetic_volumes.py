"""Generators for synthetic NIfTI and DICOM volumes used in dry-run rehearsals.

Dry-run rehearsals drive *real* MURFI and *real* PsychoPy — only the scanner
side is simulated. When no pre-recorded cache is available,
:class:`~mindfulness_nf.orchestration.scanner_source.SimulatedScannerSource`
calls into this module to fabricate just enough data for the downstream
binaries (``vSend``, ``dcmsend``) to stream something at the TR cadence.

Volumes are intentionally tiny (MNI-ish dims or 64x64 DICOM) so that even a
150-volume rest scan generates in well under a second. Pixel data is
reproducible via a fixed numpy seed so successive runs produce identical
bytes — helpful when diffing MURFI logs across rehearsals.

This file lives in the imperative shell (it writes to disk), but the
generator functions themselves are pure given their arguments.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import nibabel as nib
import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

__all__ = [
    "generate_synthetic_dicom",
    "generate_synthetic_dicom_series",
    "generate_synthetic_nifti",
    "generate_synthetic_nifti_series",
]

# MR Image Storage SOP class — matches what Siemens functional series emit.
_MR_IMAGE_STORAGE_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.4"


def generate_synthetic_nifti(
    out_path: Path,
    shape: tuple[int, int, int] = (91, 109, 91),
    tr: float = 1.2,
) -> None:
    """Write a single-volume synthetic NIfTI to ``out_path``.

    Voxel size is 2mm isotropic (MNI152 convention). Pixel data is
    reproducible random uint16 seeded off the output filename so repeat
    runs are byte-identical.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(abs(hash(out_path.name)) % (2**32))
    data = rng.integers(low=0, high=4096, size=shape, dtype=np.int16)

    affine = np.diag([2.0, 2.0, 2.0, 1.0])
    img = nib.Nifti1Image(data, affine)
    header = img.header
    header.set_zooms((2.0, 2.0, 2.0))
    header["pixdim"][4] = tr  # TR in seconds
    nib.save(img, out_path)


def generate_synthetic_nifti_series(
    out_dir: Path,
    count: int,
    shape: tuple[int, int, int] = (91, 109, 91),
    tr: float = 1.2,
) -> list[Path]:
    """Generate ``count`` single-volume NIfTIs named ``synthetic_NNNN.nii``.

    Returns the list of written paths in sequence order.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for idx in range(1, count + 1):
        p = out_dir / f"synthetic_{idx:04d}.nii"
        generate_synthetic_nifti(p, shape=shape, tr=tr)
        paths.append(p)
    return paths


def generate_synthetic_dicom(
    out_path: Path,
    series_number: int,
    instance_number: int,
    rows: int = 64,
    cols: int = 64,
) -> None:
    """Write a single minimal but valid MR-image DICOM to ``out_path``.

    Includes the tags required for ``dcmsend`` to stream the file and for
    MURFI's DICOM receiver to accept it. Pixel data is reproducible random
    uint16 seeded off ``(series_number, instance_number)``.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    file_meta = Dataset()
    file_meta.MediaStorageSOPClassUID = _MR_IMAGE_STORAGE_SOP_CLASS
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = generate_uid()

    ds = FileDataset(
        str(out_path), {}, file_meta=file_meta, preamble=b"\0" * 128
    )
    ds.SOPClassUID = _MR_IMAGE_STORAGE_SOP_CLASS
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.PatientName = "Synthetic^DryRun"
    ds.PatientID = "sub-dry-run"
    ds.Modality = "MR"
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.SeriesNumber = series_number
    ds.InstanceNumber = instance_number
    ds.SeriesDescription = "synthetic_dryrun"
    now = _dt.datetime.now()
    ds.StudyDate = now.strftime("%Y%m%d")
    ds.StudyTime = now.strftime("%H%M%S")
    ds.Rows = rows
    ds.Columns = cols
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"

    rng = np.random.default_rng((series_number << 16) ^ instance_number)
    pixels = rng.integers(low=0, high=4096, size=(rows, cols), dtype=np.uint16)
    ds.PixelData = pixels.tobytes()

    ds.save_as(out_path, enforce_file_format=True, little_endian=True, implicit_vr=False)


def generate_synthetic_dicom_series(
    out_dir: Path,
    count: int,
    series_number: int = 1,
    rows: int = 64,
    cols: int = 64,
) -> list[Path]:
    """Generate ``count`` DICOMs with sequential ``InstanceNumber`` values.

    Returns the list of written paths in instance-number order.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for instance in range(1, count + 1):
        p = out_dir / f"synthetic_{series_number:03d}_{instance:04d}.dcm"
        generate_synthetic_dicom(
            p,
            series_number=series_number,
            instance_number=instance,
            rows=rows,
            cols=cols,
        )
        paths.append(p)
    return paths
