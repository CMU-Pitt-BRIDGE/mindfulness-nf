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
import subprocess
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
from mindfulness_nf.orchestration.fsl_subprocess import run_interruptible
from mindfulness_nf.orchestration.layout import SubjectLayout

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
        # The runner passes the BIDS session dir (``sub-X/ses-Y``). Every
        # path this executor touches is exposed through SubjectLayout, which
        # tracks both subject-scoped (img/, xfm/, mask/) and session-scoped
        # (rest/, qc/) concerns. No more ``subject_dir.parent`` ladders.
        self._layout = SubjectLayout.from_session_dir(subject_dir)
        self._session_dir = subject_dir
        self._subject_dir = subject_dir.parent  # legacy; retained for dry-run stubs
        self._pipeline = pipeline
        self._scanner_config = scanner_config
        self._dry_run = dry_run
        self._stopped = False
        # Setting this event signals any in-flight subprocess started via
        # :func:`run_interruptible` to SIGTERM → SIGKILL and surface a
        # CancelledError. Set by :meth:`stop` to make operator ``i`` press
        # actually interrupt mid-stage.
        self._stop_event = asyncio.Event()

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
        # Signal any in-flight ``run_interruptible`` subprocess to terminate
        # (SIGTERM → short grace → SIGKILL to the process group). The
        # ``_stopped`` flag still gates boundary checks for helpers that
        # haven't been migrated to the interruptible path yet.
        self._stopped = True
        self._stop_event.set()

    async def relaunch(self, component: Component) -> None:
        return None

    def components(self) -> tuple[Component, ...]:
        return ()

    def advance_phase(self) -> None:
        return None

    # ---- per-command implementations -------------------------------------

    async def _run_fslmerge(self, on_progress: ProgressCallback) -> StepOutcome:
        on_progress(self._pct(5, "discovering runs"))
        runs = await ica_mod.list_runs(self._layout)
        if not runs:
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(5, "no runs found"),
                error="fslmerge: no runs in subject img/ dir",
            )
        # SessionRunner may later thread "selected_runs" via artifacts; for
        # now merge everything found.
        run_indices = tuple(int(r.run_name.split("-")[1]) for r in runs)
        on_progress(self._pct(20, f"merging {len(run_indices)} run(s)"))
        merged = await ica_mod.merge_runs(
            self._layout, run_indices, tr=self._pipeline.tr
        )
        if not merged.exists():
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(30, "merge produced no output"),
                error=f"fslmerge: expected {merged} to exist",
            )
        # Preprocess: mcflirt → Tmedian → bet. The MELODIC stage uses the
        # skull-stripped median as its regstandard/reference. Mirrors the
        # shell pipeline (feedback.sh:233-268).
        on_progress(self._pct(50, "mcflirt motion correction"))
        try:
            await self._preprocess_reference(merged)
        except subprocess.CalledProcessError as exc:
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(70, "preprocess failed"),
                error=f"preprocess: {exc.cmd[0]} failed (rc={exc.returncode})",
            )
        on_progress(self._pct(100, f"merged + preprocessed -> {merged.name}"))
        return StepOutcome(
            succeeded=True,
            final_progress=self._pct(100, "merge complete"),
            artifacts={"merged_path": str(merged)},
        )

    async def _preprocess_reference(self, merged: Path) -> None:
        """Run ``mcflirt`` → ``fslmaths -Tmedian`` → ``bet`` on the merged
        bold. Interruptible — operator ``i`` presses kill the in-flight FSL
        subprocess and surface a ``CancelledError``.
        """
        stem = merged.with_suffix("")
        mcflirt = Path(f"{stem}_mcflirt.nii")
        median = Path(f"{stem}_mcflirt_median.nii")
        bet = Path(f"{stem}_mcflirt_median_bet.nii")

        await run_interruptible(
            ["mcflirt", "-in", str(merged), "-out", str(mcflirt)],
            stop_event=self._stop_event,
        )
        await run_interruptible(
            ["fslmaths", str(mcflirt), "-Tmedian", str(median)],
            stop_event=self._stop_event,
        )
        await run_interruptible(
            ["bet", str(median), str(bet), "-R", "-f", "0.4", "-g", "0", "-m"],
            stop_event=self._stop_event,
        )

    async def _run_melodic(self, on_progress: ProgressCallback) -> StepOutcome:
        rest_dir = self._layout.rest_dir
        # Only match the raw merged BOLDs, NOT the mcflirt/median/bet
        # intermediates the preprocess step produced alongside them.
        merged = sorted(
            p for p in rest_dir.glob("*_task-rest_run-*_bold.nii")
            if "_mcflirt" not in p.name
        )
        if not merged:
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(0, "no merged inputs"),
                error="melodic: no merged bold files in rest/",
            )
        # Reference = skull-stripped median from preprocessing. Matches shell
        # feedback.sh:287 `reference_vol_for_ica=...run-01_bold_mcflirt_median_bet.nii`.
        first_stem = merged[0].with_suffix("")
        reference = Path(f"{first_stem}_mcflirt_median_bet.nii")
        if not reference.exists():
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(5, "reference missing"),
                error=f"melodic: expected reference {reference} (preprocess failed?)",
            )
        # FEAT/MELODIC template lives in murfi/scripts/fsl_scripts/. Pick the
        # single-run template when only one bold was merged; multi-run
        # template handles two BOLD inputs per the REMIND/rtNF protocol.
        fsl_scripts_dir = (
            Path(__file__).resolve().parents[3]
            / "murfi" / "scripts" / "fsl_scripts"
        )
        template_name = (
            "basic_ica_template.fsf" if len(merged) >= 2
            else "basic_ica_template_single_run.fsf"
        )
        template = fsl_scripts_dir / template_name

        on_progress(self._pct(20, "starting FEAT/MELODIC"))
        try:
            ica_dir = await ica_mod.run_ica(
                self._layout,
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
        mask_dir = self._layout.mask_dir
        rest_dir = self._layout.rest_dir
        ica_dir = rest_dir / "rs_network.gica"
        if not ica_dir.is_dir():
            ica_dir = rest_dir / "rs_network.ica"
        if not ica_dir.is_dir():
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(0, "no ICA dir"),
                error="extract: ICA output directory missing",
            )

        # Mask templates (DMNax_*, CENa_*, FSL_7networks*) live under
        # ``murfi/scripts/masks/`` per the shell pipeline (feedback.sh:34).
        template_dir = (
            Path(__file__).resolve().parents[3] / "murfi" / "scripts" / "masks"
        )
        # examplefunc = skull-stripped median produced by _preprocess_reference.
        # examplefunc_mask = the brain mask that ``bet -m`` produced alongside it.
        # Legacy code looked for ``xfm/examplefunc_brain*.nii`` — no pipeline
        # step ever writes there.
        merged = sorted(
            p for p in rest_dir.glob("*_task-rest_run-*_bold.nii")
            if "_mcflirt" not in p.name
        )
        if not merged:
            return StepOutcome(
                succeeded=False,
                final_progress=self._pct(5, "no merged bold to resolve reference"),
                error="extract: merged bold missing; preprocess step may have been skipped",
            )
        first_stem = merged[0].with_suffix("")
        examplefunc = Path(f"{first_stem}_mcflirt_median_bet.nii")
        examplefunc_mask = Path(f"{first_stem}_mcflirt_median_bet_mask.nii")
        for required in (examplefunc, examplefunc_mask):
            if not required.exists():
                return StepOutcome(
                    succeeded=False,
                    final_progress=self._pct(10, "examplefunc missing"),
                    error=(
                        f"extract: expected {required} (preprocess may not have "
                        "produced the BET brain/mask pair)"
                    ),
                )

        on_progress(self._pct(20, "extracting masks"))
        try:
            dmn, cen = await ica_mod.extract_masks(
                ica_dir,
                template_dir,
                layout=self._layout,
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
        mask_dir = self._layout.mask_dir
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
                self._layout,
                dmn_src,
                cen_src,
                on_progress=lambda msg: on_progress(self._pct(60, msg)),
                stop_event=self._stop_event,
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
        mask_dir = self._layout.mask_dir
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
        runs = await ica_mod.list_runs(self._layout)
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
        mask_dir = self._layout.mask_dir
        rest_dir = self._layout.rest_dir

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
