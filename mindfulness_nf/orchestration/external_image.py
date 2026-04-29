"""ExternalImage wire-format: MURFI's scanner input TCP protocol.

Vendored from github.com/eduardojdiniz/vsend (Eduardo Diniz), which itself
ported MURFI's original ``receive_nii.py`` / ``external_image.py`` from
Python 2 to Python 3. Kept as a single-file module because the struct
layout is an agreement with MURFI (see upstream
``murfi2/src/io/RtExternalImageInfo.h``) — changes on either side must
be mirrored on the other; vendoring lets both sender and receiver live
next to each other in this codebase.

The protocol, one volume per handshake:

    +----------------+----------------------------+
    | header (~400B) | mosaic'd or flat uint16    |
    |   struct incl. |    pixel data              |
    |   ERTI magic   |                            |
    +----------------+----------------------------+

The sender calls :meth:`ExternalImage.from_image` to produce ``(hdr, data)``
byte tuples; the receiver calls :meth:`process_header` then
:meth:`process_image` to unpack.
"""

from __future__ import annotations

import struct
from collections import namedtuple
from typing import Any

import nibabel as nb
import numpy as np

__all__ = ["ExternalImage", "demosaic", "mosaic"]


def mosaic(data: np.ndarray) -> np.ndarray:
    """Pack a 3-D volume (x, y, z) into a 2-D square grid of slices.

    Scanners often emit multi-slice EPI as a mosaic image for bandwidth.
    Slice ``k`` lands at row ``floor(k/n) * x``, column ``(k%n) * y`` of
    the output, where ``n = ceil(sqrt(z))``.
    """
    x, y, z = data.shape
    n = int(np.ceil(np.sqrt(z)))
    result = np.zeros((n * x, n * y), dtype=data.dtype)
    for idx in range(z):
        x_idx = int(np.floor(idx / n)) * x
        y_idx = (idx % n) * y
        result[x_idx : x_idx + x, y_idx : y_idx + y] = data[..., idx]
    return result


def demosaic(mosaic_data: np.ndarray, x: int, y: int, z: int) -> np.ndarray:
    """Inverse of :func:`mosaic`: reassemble a 3-D volume from a 2-D grid."""
    data = np.zeros((x, y, z), dtype=mosaic_data.dtype)
    n = int(np.ceil(np.sqrt(z)))
    dim = int(np.sqrt(np.prod(mosaic_data.shape)))
    mosaic_data = mosaic_data.reshape(dim, dim)
    for idx in range(z):
        x_idx = int(np.floor(idx / n)) * x
        y_idx = (idx % n) * y
        data[..., idx] = mosaic_data[x_idx : x_idx + x, y_idx : y_idx + y]
    return data


class ExternalImage:
    """Pack / unpack the ~400-byte per-volume header MURFI expects.

    Mirror of MURFI's C++ ``RtExternalImageInfo`` struct. See module
    docstring for the wire layout.
    """

    struct_def = (
        ("magic", "5s"),
        ("headerVersion", "i"),
        ("seriesUID", "64s"),
        ("scanType", "64s"),
        ("imageType", "16s"),
        ("note", "256s"),
        ("dataType", "16s"),
        ("isLittleEndian", "?"),
        ("isMosaic", "?"),
        ("pixelSpacingReadMM", "d"),
        ("pixelSpacingPhaseMM", "d"),
        ("pixelSpacingSliceMM", "d"),
        ("sliceGapMM", "d"),
        ("numPixelsRead", "i"),
        ("numPixelsPhase", "i"),
        ("numSlices", "i"),
        ("voxelToWorldMatrix", "16f"),
        ("repetitionTimeMS", "i"),
        ("repetitionDelayMS", "i"),
        ("currentTR", "i"),
        ("totalTR", "i"),
        ("isMotionCorrected", "?"),
        ("mcOrder", "5s"),
        ("mcTranslationXMM", "d"),
        ("mcTranslationYMM", "d"),
        ("mcTranslationZMM", "d"),
        ("mcRotationXRAD", "d"),
        ("mcRotationYRAD", "d"),
        ("mcRotationZRAD", "d"),
    )

    def __init__(
        self,
        typename: str,
        format_def: tuple[tuple[str, str], ...] = struct_def,
    ) -> None:
        self.names = [name for name, _ in format_def]
        self.formatstr = "".join(fmt for _, fmt in format_def)
        self.header_fmt = struct.Struct(self.formatstr)
        self.named_tuple_class = namedtuple(typename, self.names)
        self.hdr: Any = None
        self.img: nb.Nifti1Image | None = None
        self.num_bytes: int | None = None

    # ---- header (de)serialization --------------------------------------

    def hdr_from_bytes(self, byte_str: bytes) -> Any:
        """Unpack header bytes into a namedtuple; decode strings from UTF-8."""
        alist = list(self.header_fmt.unpack(byte_str))
        values: list[Any] = []
        for key in self.names:
            if key != "voxelToWorldMatrix":
                val = alist.pop(0)
                if isinstance(val, bytes):
                    values.append(val.split(b"\0", 1)[0].decode("utf-8"))
                else:
                    values.append(val)
            else:
                values.append([alist.pop(0) for _ in range(16)])
        return self.named_tuple_class._make(tuple(values))

    def hdr_to_bytes(self, hdr_info: Any) -> bytes:
        """Pack a namedtuple (or compatible object) into header bytes."""
        values: list[Any] = []
        for val in hdr_info._asdict().values():
            if isinstance(val, list):
                values.extend(val)
            else:
                if isinstance(val, str):
                    val = val.encode("utf-8")
                values.append(val)
        return self.header_fmt.pack(*values)

    def get_header_size(self) -> int:
        return self.header_fmt.size

    def get_image_size(self) -> int | None:
        return self.num_bytes

    # ---- header construction -------------------------------------------

    def create_header(
        self, img: nb.Nifti1Image, idx: int, nt: int, mosaic_flag: bool
    ) -> Any:
        """Build a header namedtuple describing one volume of *img*.

        Accepts both 3-D (single volume) and 4-D (time series) NIfTIs.
        """
        shape = img.shape
        if len(shape) == 4:
            x, y, z, _t = shape
        elif len(shape) == 3:
            x, y, z = shape
        else:
            raise ValueError(
                f"expected 3-D or 4-D NIfTI, got shape {shape}"
            )
        zooms = img.header.get_zooms()
        sx, sy, sz = zooms[0], zooms[1], zooms[2]
        tr = zooms[3] if len(zooms) >= 4 else 0.0
        affine = img.affine.flatten().tolist()

        return self.named_tuple_class(
            magic=b"ERTI",
            headerVersion=1,
            seriesUID=b"someuid",
            scanType=b"EPI",
            imageType=b"3D",
            note=b"mindfulness-nf dry-run",
            dataType=b"int16_t",
            isLittleEndian=True,
            isMosaic=mosaic_flag,
            pixelSpacingReadMM=sx,
            pixelSpacingPhaseMM=sy,
            pixelSpacingSliceMM=sz,
            sliceGapMM=0.0,
            numPixelsRead=x,
            numPixelsPhase=y,
            numSlices=z,
            voxelToWorldMatrix=affine,
            repetitionTimeMS=int(tr * 1000),
            repetitionDelayMS=0,
            currentTR=idx,
            totalTR=nt,
            isMotionCorrected=True,
            mcOrder=b"XYZT",
            mcTranslationXMM=0.1,
            mcTranslationYMM=0.2,
            mcTranslationZMM=0.01,
            mcRotationXRAD=0.001,
            mcRotationYRAD=0.002,
            mcRotationZRAD=0.0001,
        )

    def from_image(
        self,
        img: nb.Nifti1Image,
        idx: int,
        nt: int,
        mosaic_flag: bool = True,
    ) -> tuple[bytes, bytes]:
        """Pack one volume ``img[..., idx]`` into ``(hdr_bytes, data_bytes)``.

        If *img* is 3-D, passes through; if 4-D, selects time index *idx*.
        """
        hdrinfo = self.create_header(img, idx, nt, mosaic_flag)
        fdata = img.get_fdata()
        if fdata.ndim == 4:
            data = fdata[..., idx]
        else:
            data = fdata
        if mosaic_flag:
            data = mosaic(data)
        flat = data.astype(np.uint16).flatten().tolist()
        num_elem = len(flat)
        return self.hdr_to_bytes(hdrinfo), struct.pack(f"{num_elem}H", *flat)

    # ---- receiver-side hooks -------------------------------------------

    def make_img(self, in_bytes: bytes) -> nb.Nifti1Image:
        h = self.hdr
        if h is None:
            raise RuntimeError("process_header must be called before make_img")
        if h.dataType != "int16_t":
            raise ValueError(f"Unsupported data type: {h.dataType}")
        assert self.num_bytes is not None

        raw = struct.unpack(f"{self.num_bytes // 2}H", in_bytes)
        arr = np.array(raw, dtype=np.uint16)
        if h.isMosaic:
            data = demosaic(arr, h.numPixelsRead, h.numPixelsPhase, h.numSlices)
        else:
            data = arr.reshape((h.numPixelsRead, h.numPixelsPhase, h.numSlices))
        affine = np.array(h.voxelToWorldMatrix).reshape((4, 4))
        img = nb.Nifti1Image(data, affine)
        img.header.set_zooms(
            (h.pixelSpacingReadMM, h.pixelSpacingPhaseMM, h.pixelSpacingSliceMM)
        )
        img.header.set_xyzt_units("mm", "msec")
        return img

    def process_header(self, in_bytes: bytes) -> Any:
        """Inspect the 4-byte magic, unpack the header if recognized."""
        magic = struct.unpack("4s", in_bytes[:4])[0]
        if magic in (b"ERTI", b"SIMU"):
            self.hdr = self.hdr_from_bytes(in_bytes)
            if self.hdr.isMosaic:
                nrows = int(np.ceil(np.sqrt(self.hdr.numSlices)))
                self.num_bytes = (
                    2 * self.hdr.numPixelsRead * self.hdr.numPixelsPhase * nrows * nrows
                )
            else:
                self.num_bytes = (
                    2
                    * self.hdr.numPixelsRead
                    * self.hdr.numPixelsPhase
                    * self.hdr.numSlices
                )
            return self.hdr
        raise ValueError(f"Unknown magic number {magic!r}")

    def process_image(self, in_bytes: bytes) -> nb.Nifti1Image:
        self.img = self.make_img(in_bytes)
        return self.img
