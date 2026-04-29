"""Single source of truth for every filesystem path the pipeline touches.

The codebase has two path scopes that were long confused as one:

* **Subject-scoped** — shared across every session for a subject. MURFI's
  ``img/``, ``xfm/``, ``mask/``, ``xml/`` live here because MURFI itself
  writes under ``$MURFI_SUBJECTS_DIR/<subject>/`` (no session concept),
  and because DMN/CEN masks produced in a Process session are *consumed*
  by later Real-Time sessions for the same subject.
* **Session-scoped** — derived artifacts that belong to ONE session.
  ``rest/`` 4D merges, ``qc/`` overlays, the PsychoPy behavioral data,
  MURFI's per-session log, and session_state.json all live under
  ``sub-X/ses-Y/``. Two sessions cannot clobber each other's outputs.

:class:`SubjectLayout` is a frozen value object constructed once per
session at TUI startup. It exposes each concept as a typed property so
callers never have to guess which ``.parent`` ladder recovers which level.
The three-tuple ``(subjects_root, subject_id, session_type)`` is the
canonical construction seed; every other Path is derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["SubjectLayout"]


@dataclass(frozen=True, slots=True)
class SubjectLayout:
    """Typed access to every path in the pipeline for one session.

    Frozen + ``slots`` — hashable, trivially comparable, cheap to pass
    around. All properties are pure path construction (no I/O).
    """

    subjects_root: Path
    subject_id: str
    session_type: str

    def __post_init__(self) -> None:
        # Normalize via resolve() so two layouts built from the same logical
        # location compare equal regardless of how the caller spelled the
        # path. Without this, eq + hash were unreliable (from_session_dir
        # resolved; the default ctor didn't). object.__setattr__ is required
        # because the dataclass is frozen.
        object.__setattr__(self, "subjects_root", Path(self.subjects_root).resolve())
        if not self.subject_id:
            msg = "subject_id must be non-empty"
            raise ValueError(msg)
        if not self.session_type:
            msg = "session_type must be non-empty"
            raise ValueError(msg)
        if self.subject_id.startswith("/"):
            msg = f"subject_id must be a label, not a path (got {self.subject_id!r})"
            raise ValueError(msg)

    # ------------------------------------------------------------------
    # Subject-scoped (shared across every session for this subject)
    # ------------------------------------------------------------------

    @property
    def subject_root(self) -> Path:
        """``<subjects_root>/<subject_id>`` — MURFI's home for a subject."""
        return self.subjects_root / self.subject_id

    @property
    def img_dir(self) -> Path:
        """MURFI's per-volume NIfTIs (``img-<series>-<vol>.nii``).

        Subject-scoped: MURFI writes under ``$MURFI_SUBJECTS_DIR/<sub>/img/``
        regardless of session. ``rename_step_volumes`` keys filenames to
        ``step.run`` after each step, so different runs don't collide.
        """
        return self.subject_root / "img"

    @property
    def xfm_dir(self) -> Path:
        """Reference / transform volumes (``study_ref``, ``series*_ref``).

        Subject-scoped: ``study_ref`` is the subject's canonical reference
        frame used by every session's registration.
        """
        return self.subject_root / "xfm"

    @property
    def mask_dir(self) -> Path:
        """DMN/CEN masks for MURFI's ``rtdmn.xml`` mask-load module.

        Subject-scoped by design: masks are *produced* in the Process
        session and *consumed* by later Real-Time sessions for the same
        subject. If a subject has multiple Process sessions, the latest
        run's masks win (single source of truth).
        """
        return self.subject_root / "mask"

    @property
    def subject_xml_dir(self) -> Path:
        """Canonical XML templates MURFI reads at runtime.

        Subject-scoped because MURFI's launch env points at
        ``$MURFI_SUBJECTS_DIR/<sub>/xml/``. Sessions get a *snapshot* copy
        under :attr:`ses_sourcedata_xml_dir` for provenance.
        """
        return self.subject_root / "xml"

    @property
    def subject_log_dir(self) -> Path:
        """MURFI-native subject-level log directory.

        Rarely written today (MURFI's own log lives under the session),
        but :func:`create_subject` historically created it.
        """
        return self.subject_root / "log"

    @property
    def subject_scripts_dir(self) -> Path:
        """Shared FSL helper scripts copied at subject init."""
        return self.subject_root / "scripts"

    @property
    def study_ref(self) -> Path:
        """MURFI's canonical reference volume."""
        return self.xfm_dir / "study_ref.nii"

    def img_run_glob(self, run_number: int) -> str:
        """Glob pattern for one run's per-volume NIfTIs — e.g. ``img-00001-*.nii``."""
        return f"img-{run_number:05d}-*.nii"

    def series_ref_glob(self) -> str:
        """Glob for MURFI's per-series reference volumes in :attr:`xfm_dir`."""
        return "series*_ref.nii"

    # ------------------------------------------------------------------
    # Session-scoped (one session's derivatives, cannot mingle)
    # ------------------------------------------------------------------

    @property
    def session_dir(self) -> Path:
        """``<subject_root>/ses-<session_type>``."""
        return self.subject_root / f"ses-{self.session_type}"

    @property
    def session_state_json(self) -> Path:
        return self.session_dir / "session_state.json"

    @property
    def provenance_json(self) -> Path:
        """Per-session provenance: git SHA, MURFI tag, CLI args, hostname."""
        return self.session_dir / "provenance.json"

    @property
    def session_log_dir(self) -> Path:
        """Per-session orchestrator + MURFI logs (e.g. ``murfi_rtdmn.log``)."""
        return self.session_dir / "log"

    @property
    def rest_dir(self) -> Path:
        """Per-session FSL-intermediate 4D merges + preprocessing artifacts.

        Previously at the subject root — two sessions clobbered each
        other's merges. Now session-scoped: ``sub-X/ses-Y/rest/``.
        """
        return self.session_dir / "rest"

    @property
    def qc_dir(self) -> Path:
        """Per-session QC overlays (slices GIFs).

        Previously at the subject root. Now session-scoped so two
        sessions' QC can be distinguished.
        """
        return self.session_dir / "qc"

    @property
    def ses_sourcedata_xml_dir(self) -> Path:
        """Per-session snapshot of XML templates used at session start.

        Copied from :attr:`subject_xml_dir` at session creation so the
        session has a frozen record of the XMLs-as-run, even if the
        subject-level ``xml/`` is later regenerated.
        """
        return self.session_dir / "sourcedata" / "murfi" / "xml"

    @property
    def ses_sourcedata_dicom_dir(self) -> Path:
        """DicomReceiver output dir for real-time DICOM (if used)."""
        return self.session_dir / "sourcedata" / "dicom"

    @property
    def psychopy_data_dir(self) -> Path:
        """Canonical destination for PsychoPy behavioral data.

        Previously PsychoPy wrote to a sibling tree
        (``psychopy/balltask/data/<subject>/``) — invisible to anyone
        copying the subject dir. Now routes into the session tree.
        """
        return self.session_dir / "sourcedata" / "psychopy"

    # ------------------------------------------------------------------
    # BIDS filename helpers
    # ------------------------------------------------------------------

    def bold_bids_name(self, task: str, run: int, suffix: str = "bold.nii") -> str:
        """BIDS filename: ``sub-X_ses-Y_task-T_run-NN_bold.nii``.

        Uses :attr:`session_type` — the hardcoded ``ses-localizer`` that
        used to live in producers AND consumers has been removed
        everywhere in favor of this helper.
        """
        return (
            f"{self.subject_id}_ses-{self.session_type}"
            f"_task-{task}_run-{run:02d}_{suffix}"
        )

    def bold_rest_intermediate(self, task: str, run: int) -> Path:
        """Path in :attr:`rest_dir` using BIDS-style naming.

        This is the 4D merge :func:`ica.merge_runs` writes.
        """
        return self.rest_dir / self.bold_bids_name(task, run)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_session_dir(cls, session_dir: Path) -> "SubjectLayout":
        """Reconstruct a layout from a BIDS session directory.

        Back-compat seam for code paths that still carry a session ``Path``.
        Expects the dir to match ``<subjects_root>/<subject_id>/ses-<type>``.
        """
        session_dir = Path(session_dir).resolve()
        if not session_dir.name.startswith("ses-"):
            msg = (
                f"from_session_dir expects a .../ses-<type> directory, "
                f"got {session_dir!r}"
            )
            raise ValueError(msg)
        subject_root = session_dir.parent
        subjects_root = subject_root.parent
        session_type = session_dir.name.removeprefix("ses-")
        if not session_type:
            msg = f"session_dir has empty session_type: {session_dir!r}"
            raise ValueError(msg)
        return cls(
            subjects_root=subjects_root,
            subject_id=subject_root.name,
            session_type=session_type,
        )
