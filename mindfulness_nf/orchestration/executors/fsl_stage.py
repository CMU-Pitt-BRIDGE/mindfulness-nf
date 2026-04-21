"""PROCESS_STAGE executor: dispatches to one FSL helper per ``fsl_command``.

Wraps existing ``orchestration.ica`` / ``orchestration.registration`` helpers
without modifying them (constraint G15). Stop is best-effort: the current
FSL helpers run via ``asyncio.to_thread`` around blocking ``subprocess.run``
calls, so cancellation cannot interrupt an in-flight ``feat``/``melodic``
process mid-stage — but we still honour the Protocol by flipping a flag and
returning a cancellation outcome at the next boundary.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import StepConfig
from mindfulness_nf.orchestration import ica as ica_mod
from mindfulness_nf.orchestration import registration as reg_mod
from mindfulness_nf.orchestration.executor import (
    Component,
    ProgressCallback,
    StepOutcome,
    StepProgress,
)

__all__ = ["FslStageExecutor"]

logger = logging.getLogger(__name__)

# Real-BOLD dry-run cache populated by scripts/fetch_dry_run_bold.py.
# When present, dry-run FSL stages run the real FSL subprocess against
# this data instead of producing empty placeholder files.
_BOLD_CACHE_DIR = Path("murfi/dry_run_cache_bold")


def _bold_cache_has_data() -> bool:
    """Return True if the real-BOLD dry-run cache contains NIfTI volumes."""
    nifti_dir = _BOLD_CACHE_DIR / "nifti"
    if not nifti_dir.is_dir():
        return False
    return any(nifti_dir.glob("*.nii*"))


class FslStageExecutor:
    """Runs one FSL pipeline stage (fslmerge, melodic, mask, register, qc, select)."""

    def __init__(
        self,
        config: StepConfig,
        subject_dir: Path,
        pipeline: PipelineConfig,
        scanner_config: ScannerConfig,
        dry_run: bool = False,
    ) -> None:
        if config.fsl_command is None:
            msg = f"FslStageExecutor requires StepConfig.fsl_command (step={config.name})"
            raise ValueError(msg)
        self._config = config
        self._subject_dir = subject_dir
        self._pipeline = pipeline
        self._scanner_config = scanner_config
        self._dry_run = dry_run
        self._stopped = False

    # ---- public protocol -------------------------------------------------

    async def run(self, on_progress: ProgressCallback) -> StepOutcome:
        if self._stopped:
            return self._cancelled_outcome()

        cmd = self._config.fsl_command
        if self._dry_run:
            # If the real-BOLD dry-run cache is populated, escalate to running
            # the actual FSL subprocess against that data — otherwise fall
            # back to placeholder-file stubs.
            if _bold_cache_has_data():
                logger.info(
                    "FslStageExecutor: dry-run with real-BOLD cache present; "
                    "running real FSL for %s",
                    cmd,
                )
            else:
                logger.info(
                    "FslStageExecutor: dry-run without real-BOLD cache; "
                    "stubbing %s",
                    cmd,
                )
                return await self._run_dry(on_progress, cmd)
        try:
            match cmd:
                case "fslmerge":
                    return await self._run_fslmerge(on_progress)
                case "melodic":
                    return await self._run_melodic(on_progress)
                case "extract_dmn" | "extract_cen":
                    return await self._run_extract(on_progress, which=cmd)
                case "flirt_applywarp":
                    return await self._run_register(on_progress)
                case "qc_visualize":
                    return await self._run_qc(on_progress)
                case "select_runs":
                    return await self._run_select(on_progress)
                case _:
                    return StepOutcome(
                        succeeded=False,
                        final_progress=self._zero_pct(),
                        error=f"unknown fsl_command: {cmd!r}",
                    )
        except asyncio.CancelledError:
            return self._cancelled_outcome()

    async def stop(self, timeout: float = 5.0) -> None:
        # FSL helpers run under asyncio.to_thread and cannot be interrupted mid-call.
        # We mark stopped so that the next boundary observes it; the outer
        # task.cancel() plus our cancelled handler covers the rest.
        self._stopped = True

    async def relaunch(self, component: Component) -> None:
        return None

    def components(self) -> tuple[Component, ...]:
        return ()

    def advance_phase(self) -> None:
        return None

    # ---- per-command implementations -------------------------------------

    async def _run_fslmerge(self, on_progress: ProgressCallback) -> StepOutcome:
        on_progress(self._pct(10, "discovering runs"))
        runs = await ica_mod.list_runs(self._subject_dir)
        if not runs:
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(10, "no runs found"),
                error="fslmerge: no runs in subject img/ dir",
            )
        # If the caller has recorded selected_runs elsewhere, FslStageExecutor
        # here simply merges everything found; SessionRunner is responsible for
        # threading artifacts ("selected_runs") in a future todo.
        run_indices = tuple(int(r.run_name.split("-")[1]) for r in runs)
        on_progress(self._pct(50, f"merging {len(run_indices)} run(s)"))
        merged = await ica_mod.merge_runs(
            self._subject_dir, run_indices, tr=self._pipeline.tr
        )
        if not merged.exists():
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(90, "merge produced no output"),
                error=f"fslmerge: expected {merged} to exist",
            )
        on_progress(self._pct(100, f"merged -> {merged.name}"))
        return StepOutcome(
            succeeded=True,
            final_progress=self._pct(100, "merge complete"),
            artifacts={"merged_path": str(merged)},
        )

    async def _run_melodic(self, on_progress: ProgressCallback) -> StepOutcome:
        rest_dir = self._subject_dir / "rest"
        merged = sorted(rest_dir.glob("*_task-rest_run-*_bold.nii"))
        if not merged:
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(0, "no merged inputs"),
                error="melodic: no merged bold files in rest/",
            )
        reference = self._subject_dir / "xfm" / "examplefunc_brain.nii"
        template = Path(__file__).resolve().parents[3] / "murfi" / "templates" / "ica.fsf"

        on_progress(self._pct(20, "starting FEAT/MELODIC"))
        try:
            ica_dir = await ica_mod.run_ica(
                self._subject_dir,
                tuple(merged),
                reference_vol=reference,
                template_path=template,
                n_volumes=self._pipeline.rest_measurements,
                on_progress=lambda msg: on_progress(self._pct(60, msg)),
            )
        except FileNotFoundError as exc:
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(60, "input missing"),
                error=f"melodic input missing: {exc}",
            )
        if not ica_dir.exists():
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(90, "ica dir missing"),
                error=f"melodic: expected {ica_dir} to exist",
            )
        on_progress(self._pct(100, f"MELODIC complete -> {ica_dir.name}"))
        return StepOutcome(
            succeeded=True,
            final_progress=self._pct(100, "melodic complete"),
            artifacts={"ica_dir": str(ica_dir)},
        )

    async def _run_extract(
        self, on_progress: ProgressCallback, *, which: str
    ) -> StepOutcome:
        mask_dir = self._subject_dir / "mask"
        rest_dir = self._subject_dir / "rest"
        ica_dir = rest_dir / "rs_network.gica"
        if not ica_dir.is_dir():
            ica_dir = rest_dir / "rs_network.ica"
        if not ica_dir.is_dir():
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(0, "no ICA dir"),
                error="extract: ICA output directory missing",
            )

        template_dir = Path(__file__).resolve().parents[3] / "murfi" / "templates"
        examplefunc = self._subject_dir / "xfm" / "examplefunc_brain.nii"
        examplefunc_mask = self._subject_dir / "xfm" / "examplefunc_brain_mask.nii"

        on_progress(self._pct(20, "extracting masks"))
        try:
            dmn, cen = await ica_mod.extract_masks(
                ica_dir,
                template_dir,
                subject_dir=self._subject_dir,
                examplefunc=examplefunc,
                examplefunc_mask=examplefunc_mask,
                on_progress=lambda msg: on_progress(self._pct(60, msg)),
            )
        except FileNotFoundError as exc:
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(60, "missing file"),
                error=f"extract: {exc}",
            )

        target = dmn if which == "extract_dmn" else cen
        if not target.exists():
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(90, "mask missing"),
                error=f"{which}: expected {target} to exist",
            )
        on_progress(self._pct(100, f"{which} -> {target.name}"))
        return StepOutcome(
            succeeded=True,
            final_progress=self._pct(100, f"{which} complete"),
            artifacts={f"{which}_path": str(target), "mask_dir": str(mask_dir)},
        )

    async def _run_register(self, on_progress: ProgressCallback) -> StepOutcome:
        mask_dir = self._subject_dir / "mask"
        dmn_src = mask_dir / "dmn_rest_original.nii"
        cen_src = mask_dir / "cen_rest_original.nii"
        if not dmn_src.exists() or not cen_src.exists():
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(0, "source masks missing"),
                error="register: dmn/cen rest-original masks missing",
            )
        on_progress(self._pct(20, "registering masks to study_ref"))
        try:
            dmn_reg, cen_reg = await reg_mod.register_masks(
                self._subject_dir,
                dmn_src,
                cen_src,
                on_progress=lambda msg: on_progress(self._pct(60, msg)),
            )
        except FileNotFoundError as exc:
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(60, "missing input"),
                error=f"register: {exc}",
            )
        if not dmn_reg.exists() or not cen_reg.exists():
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(90, "output missing"),
                error="register: registered masks were not written",
            )
        on_progress(self._pct(100, "registration complete"))
        return StepOutcome(
            succeeded=True,
            final_progress=self._pct(100, "registration complete"),
            artifacts={"dmn_registered": str(dmn_reg), "cen_registered": str(cen_reg)},
        )

    async def _run_qc(self, on_progress: ProgressCallback) -> StepOutcome:
        # Placeholder QC: verify the final masks exist under mask/.
        # A richer visualisation step can replace this without touching the runner.
        mask_dir = self._subject_dir / "mask"
        expected = (mask_dir / "dmn.nii", mask_dir / "cen.nii")
        on_progress(self._pct(50, "checking mask outputs"))
        missing = [str(p) for p in expected if not p.exists()]
        if missing:
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(100, "qc failed"),
                error=f"qc: missing {', '.join(missing)}",
            )
        on_progress(self._pct(100, "qc ok"))
        return StepOutcome(
            succeeded=True,
            final_progress=self._pct(100, "qc ok"),
            artifacts={"qc_ok": True},
        )

    async def _run_select(self, on_progress: ProgressCallback) -> StepOutcome:
        # Interactive run selection is driven by the TUI; here we emit a stub
        # artifact listing every run found on disk so downstream stages have
        # something to consume. Tests substitute a fake executor.
        runs = await ica_mod.list_runs(self._subject_dir)
        run_names = tuple(r.run_name for r in runs)
        on_progress(self._pct(100, f"selected {len(run_names)} run(s)"))
        return StepOutcome(
            succeeded=True,
            final_progress=self._pct(100, "selection complete"),
            artifacts={"selected_runs": run_names},
        )

    # ---- dry-run --------------------------------------------------------

    async def _run_dry(
        self, on_progress: ProgressCallback, cmd: str | None
    ) -> StepOutcome:
        """Simulate an FSL stage: brief progress sweep, synthesized outputs.

        Creates empty-but-present placeholder files in the paths downstream
        stages check for, so the pipeline advances without touching FSL.
        """
        on_progress(self._pct(20, f"dry-run: simulating {cmd}"))
        await asyncio.sleep(0.05)
        on_progress(self._pct(60, f"dry-run: simulating {cmd}"))
        await asyncio.sleep(0.05)

        artifacts: dict[str, Any] = {"dry_run": True}
        mask_dir = self._subject_dir / "mask"
        rest_dir = self._subject_dir / "rest"

        match cmd:
            case "fslmerge":
                rest_dir.mkdir(parents=True, exist_ok=True)
                merged = rest_dir / "sub_task-rest_run-1_bold.nii"
                merged.touch()
                artifacts["merged_path"] = str(merged)
            case "melodic":
                ica_dir = rest_dir / "rs_network.gica"
                ica_dir.mkdir(parents=True, exist_ok=True)
                artifacts["ica_dir"] = str(ica_dir)
            case "extract_dmn":
                mask_dir.mkdir(parents=True, exist_ok=True)
                dmn = mask_dir / "dmn_rest_original.nii"
                dmn.touch()
                artifacts["extract_dmn_path"] = str(dmn)
                artifacts["mask_dir"] = str(mask_dir)
            case "extract_cen":
                mask_dir.mkdir(parents=True, exist_ok=True)
                cen = mask_dir / "cen_rest_original.nii"
                cen.touch()
                artifacts["extract_cen_path"] = str(cen)
                artifacts["mask_dir"] = str(mask_dir)
            case "flirt_applywarp":
                mask_dir.mkdir(parents=True, exist_ok=True)
                dmn_reg = mask_dir / "dmn.nii"
                cen_reg = mask_dir / "cen.nii"
                dmn_reg.touch()
                cen_reg.touch()
                artifacts["dmn_registered"] = str(dmn_reg)
                artifacts["cen_registered"] = str(cen_reg)
            case "qc_visualize":
                artifacts["qc_ok"] = True
            case "select_runs":
                artifacts["selected_runs"] = ("run-1",)
            case _:
                # Unknown commands still succeed in dry-run — the operator
                # is rehearsing; they'll notice on a real run.
                pass

        on_progress(self._pct(100, f"dry-run: {cmd} complete"))
        return StepOutcome(
            succeeded=True,
            final_progress=self._pct(100, f"dry-run: {cmd} complete"),
            artifacts=artifacts,
        )

    # ---- helpers ---------------------------------------------------------

    def _pct(self, value: int, detail: str) -> StepProgress:
        return StepProgress(value=value, target=100, unit="percent", detail=detail)

    def _zero_pct(self) -> StepProgress:
        return StepProgress(value=0, target=100, unit="percent")

    def _cancelled_outcome(self) -> StepOutcome:
        return StepOutcome(
            succeeded=False,
            final_progress=self._zero_pct(),
            error="cancelled",
        )

    def _artifacts_noop(self) -> dict[str, Any]:
        return {}
