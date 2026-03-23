"""Process (ICA) screen.

Phase 1: RunTable widget for ICA run selection.
Phase 2: Step-by-step ICA progress.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Key
from textual.screen import Screen
from textual.widgets import Label, Static

from mindfulness_nf.models import Color, TrafficLight
from mindfulness_nf.quality import assess_run_selection
from mindfulness_nf.tui.widgets.log_panel import LogPanel
from mindfulness_nf.tui.widgets.run_table import RunTable
from mindfulness_nf.tui.widgets.status_light import StatusLight

@dataclass(frozen=True, slots=True)
class _TableRunInfo:
    """Adapts ICA RunInfo to the RunTable protocol."""

    name: str
    volumes: int
    quality: Color


class ProcessScreen(Screen[None]):
    """ICA processing screen with two phases:

    Phase 1: Run selection (operator toggles runs by number, D to confirm).
    Phase 2: ICA pipeline progress (auto-advances).
    """

    BINDINGS = [
        Binding("d", "advance", "Done/Confirm", show=True),
        Binding("escape", "app.request_quit", "Quit", show=False),
    ]

    DEFAULT_CSS = """
    ProcessScreen {
        layout: vertical;
        padding: 1;
    }
    #proc-header {
        text-style: bold;
        text-align: center;
        width: 100%;
        margin-bottom: 1;
    }
    #proc-run-table {
        height: auto;
    }
    #proc-status-zone {
        height: auto;
        border: solid $accent;
        padding: 1;
    }
    #proc-log-zone {
        height: 1fr;
    }
    #proc-elapsed {
        height: auto;
        text-align: right;
        padding: 0 2;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._phase: int = 1  # 1 = selection, 2 = processing
        self._ica_runs: tuple[object, ...] = ()
        self._start_time: float | None = None

    def compose(self) -> ComposeResult:
        yield Label("PROCESS (ICA)", id="proc-header", classes="title-bar")
        with Vertical(id="proc-selection-zone"):
            yield RunTable(id="proc-run-table")
        with Vertical(id="proc-status-zone"):
            yield StatusLight(id="proc-status-light")
            yield Static("", id="proc-elapsed")
        with Vertical(id="proc-log-zone"):
            yield LogPanel(id="proc-log")

    def on_mount(self) -> None:
        """Load available runs into the table."""
        self.query_one("#proc-elapsed", Static).display = False
        self.query_one("#proc-log", LogPanel).display = False
        status = self.query_one("#proc-status-light", StatusLight)
        status.update(TrafficLight(
            Color.GREEN, "Select runs for ICA processing, then press D."
        ))
        self.run_worker(self._load_runs(), exclusive=True)

    async def _load_runs(self) -> None:
        """Load available resting state runs."""
        from mindfulness_nf.orchestration.ica import list_runs

        app = self.app
        if not hasattr(app, "scanner_config"):
            return

        subject_dir = app.subjects_dir / app.subject_id
        runs = await list_runs(subject_dir)
        self._ica_runs = runs

        # Convert to RunTable protocol
        table_runs: list[_TableRunInfo] = []
        for run in runs:
            if run.volume_count >= 225:
                quality = Color.GREEN
            elif run.volume_count >= 10:
                quality = Color.YELLOW
            else:
                quality = Color.RED
            table_runs.append(_TableRunInfo(
                name=run.run_name,
                volumes=run.volume_count,
                quality=quality,
            ))

        table = self.query_one("#proc-run-table", RunTable)
        table.set_runs(tuple(table_runs))

        if not runs:
            status = self.query_one("#proc-status-light", StatusLight)
            status.update(TrafficLight(
                Color.RED, "No resting state runs found. Cannot proceed."
            ))

    def on_key(self, event: Key) -> None:
        """Forward number keys to RunTable in phase 1."""
        if self._phase == 1 and event.key.isdigit():
            table = self.query_one("#proc-run-table", RunTable)
            num = int(event.key)
            if 1 <= num <= 9:
                table.toggle_selection(num)
                self._update_selection_status()

    def _update_selection_status(self) -> None:
        """Update status light based on current selection."""
        table = self.query_one("#proc-run-table", RunTable)
        selected = table.selected
        light = assess_run_selection(selected)
        status = self.query_one("#proc-status-light", StatusLight)
        if light.color != Color.RED:
            status.update(TrafficLight(
                light.color,
                f"{light.message} Press D to confirm.",
            ))
        else:
            status.update(light)

    def action_advance(self) -> None:
        """Handle D keypress."""
        if self._phase == 1:
            self._confirm_selection()
        # Phase 2: no operator input needed

    def _confirm_selection(self) -> None:
        """Confirm run selection and start processing."""
        table = self.query_one("#proc-run-table", RunTable)
        selected = table.selected
        light = assess_run_selection(selected)

        if light.color == Color.RED:
            return  # Can't proceed with no runs

        self._phase = 2
        self.query_one("#proc-run-table", RunTable).display = False
        self.query_one("#proc-elapsed", Static).display = True
        self.query_one("#proc-log", LogPanel).display = True

        status = self.query_one("#proc-status-light", StatusLight)
        status.update(TrafficLight(Color.GREEN, "Processing..."))

        self._start_time = time.monotonic()
        self.run_worker(self._do_processing(selected), exclusive=True)

    async def _do_processing(self, selected_indices: tuple[int, ...]) -> None:
        """Run the ICA pipeline."""
        from mindfulness_nf.orchestration import ica
        from mindfulness_nf.orchestration.registration import register_masks

        app = self.app
        if not hasattr(app, "scanner_config"):
            return

        subject_dir = app.subjects_dir / app.subject_id
        log = self.query_one("#proc-log", LogPanel)

        def on_progress(msg: str) -> None:
            log.add_line(msg)
            self._update_elapsed()

        try:
            # Step 1: Merge selected runs
            on_progress("Merging selected runs...")
            runs = await ica.list_runs(subject_dir)
            run_indices = tuple(
                int(runs[i - 1].run_name.split("-")[1])
                for i in selected_indices
                if i <= len(runs)
            )
            merged_path = await ica.merge_runs(
                subject_dir, run_indices, tr=app.pipeline_config.tr,
            )
            on_progress(f"Merged to {merged_path.name}")

            # Step 2: Run ICA
            template_path = (
                app.template_dir.parent.parent
                / "scripts"
                / "fsl_scripts"
                / "basic_ica_template.fsf"
            )
            rest_dir = subject_dir / "rest"
            subject_name = subject_dir.name
            examplefunc = (
                rest_dir
                / f"{subject_name}_ses-localizer_task-rest_run-01_bold_mcflirt_median_bet.nii"
            )

            merged_paths = (merged_path,)
            if len(selected_indices) == 2 and len(runs) >= 2:
                second_idx = int(runs[selected_indices[1] - 1].run_name.split("-")[1])
                second_merged = rest_dir / f"{subject_name}_ses-localizer_task-rest_run-02_bold.nii"
                if second_merged.exists():
                    merged_paths = (merged_path, second_merged)

            ica_dir = await ica.run_ica(
                subject_dir,
                merged_paths,
                examplefunc,
                template_path=template_path,
                on_progress=on_progress,
            )

            # Step 3: Extract masks
            template_dir_fsl = app.template_dir.parent.parent / "scripts" / "templates"
            examplefunc_mask = (
                rest_dir
                / f"{subject_name}_ses-localizer_task-rest_run-01_bold_mcflirt_median_bet_mask.nii"
            )

            dmn_mask, cen_mask = await ica.extract_masks(
                ica_dir,
                template_dir_fsl,
                subject_dir=subject_dir,
                examplefunc=examplefunc,
                examplefunc_mask=examplefunc_mask,
                on_progress=on_progress,
            )

            # Step 4: Register masks
            dmn_reg, cen_reg = await register_masks(
                subject_dir,
                dmn_mask,
                cen_mask,
                on_progress=on_progress,
            )

            on_progress("Processing complete!")

            # Show mask quality results
            from mindfulness_nf.quality import assess_mask
            import subprocess
            import asyncio

            for name, mask_path in [("DMN", dmn_reg), ("CEN", cen_reg)]:
                try:
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["fslstats", str(mask_path), "-V"],
                        capture_output=True, text=True,
                    )
                    voxels = int(result.stdout.strip().split()[0])
                    mask_light = assess_mask(voxels)
                    on_progress(f"{name} mask: {mask_light.message}")
                except Exception:
                    on_progress(f"{name} mask: unable to assess quality")

            status = self.query_one("#proc-status-light", StatusLight)
            status.update(TrafficLight(Color.GREEN, "PROCESSING COMPLETE"))

        except Exception as exc:
            log.add_line(f"Error: {exc}")
            status = self.query_one("#proc-status-light", StatusLight)
            status.update(TrafficLight(
                Color.RED,
                f"Processing failed: {exc}",
            ))

    def _update_elapsed(self) -> None:
        """Update the elapsed time display."""
        if self._start_time is not None:
            elapsed = time.monotonic() - self._start_time
            minutes = int(elapsed // 60)
            seconds = int(elapsed % 60)
            self.query_one("#proc-elapsed", Static).update(
                f"Elapsed: {minutes:02d}:{seconds:02d}"
            )
