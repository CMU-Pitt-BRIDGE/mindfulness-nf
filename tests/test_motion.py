"""Tests for post-step motion-parameter extraction."""

from __future__ import annotations

from pathlib import Path

from mindfulness_nf.orchestration.motion import (
    _framewise_displacement,
    _parse_par,
    _write_motion_tsv,
)


def test_parse_par_basic(tmp_path: Path) -> None:
    par = tmp_path / "merged_mcf.par"
    par.write_text(
        "0.001 -0.002 0.003 0.4 -0.5 0.6\n"
        "0.002 -0.003 0.004 0.5 -0.6 0.7\n"
        "0.003 -0.004 0.005 0.6 -0.7 0.8\n"
    )
    rows = _parse_par(par)
    assert len(rows) == 3
    assert rows[0] == (0.001, -0.002, 0.003, 0.4, -0.5, 0.6)
    assert rows[2] == (0.003, -0.004, 0.005, 0.6, -0.7, 0.8)


def test_parse_par_skips_malformed(tmp_path: Path) -> None:
    par = tmp_path / "merged_mcf.par"
    par.write_text(
        "0.001 -0.002 0.003 0.4 -0.5 0.6\n"
        "# comment line\n"
        "garbage row only three cols\n"
        "0.002 -0.003 0.004 0.5 -0.6 0.7\n"
    )
    rows = _parse_par(par)
    assert len(rows) == 2


def test_framewise_displacement_first_row_zero() -> None:
    rows = [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0), (0.001, 0.0, 0.0, 1.0, 0.0, 0.0)]
    fd = _framewise_displacement(rows)
    assert fd[0] == 0.0
    # |drx|*50 + |dry|*50 + |drz|*50 + |dtx| + |dty| + |dtz|
    # = 0.001*50 + 0 + 0 + 1.0 + 0 + 0 = 1.05
    assert abs(fd[1] - 1.05) < 1e-9


def test_framewise_displacement_pure_translation() -> None:
    """No rotation, only translation."""
    rows = [
        (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0, 0.5, 0.5, 0.5),  # delta = 0.5+0.5+0.5
        (0.0, 0.0, 0.0, 0.5, 0.5, 0.5),  # delta = 0
    ]
    fd = _framewise_displacement(rows)
    assert fd == [0.0, 1.5, 0.0]


def test_write_motion_tsv_format(tmp_path: Path) -> None:
    out = tmp_path / "transferpre-01_motion.tsv"
    rows = [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0), (0.001, 0.0, 0.0, 0.5, 0.0, 0.0)]
    fd = [0.0, 0.55]
    _write_motion_tsv(out, rows, fd)
    text = out.read_text()
    lines = text.strip().split("\n")
    assert lines[0] == (
        "rot_x\trot_y\trot_z\ttrans_x\ttrans_y\ttrans_z\tframewise_displacement"
    )
    assert len(lines) == 3  # header + 2 data rows
    # Row 1: all zeros + 0 FD
    cols = lines[1].split("\t")
    assert len(cols) == 7
    assert all(float(c) == 0.0 for c in cols)
    # Row 2: nonzero
    cols = lines[2].split("\t")
    assert float(cols[0]) == 0.001
    assert float(cols[3]) == 0.5
    assert abs(float(cols[6]) - 0.55) < 1e-9


def test_write_motion_tsv_creates_parent_dir(tmp_path: Path) -> None:
    """Parent directory should be created if missing (BIDS derivatives/motion)."""
    out = tmp_path / "ses-rt15" / "derivatives" / "motion" / "feedback-01_motion.tsv"
    rows = [(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)]
    fd = [0.0]
    _write_motion_tsv(out, rows, fd)
    assert out.is_file()
    assert "rot_x" in out.read_text()
