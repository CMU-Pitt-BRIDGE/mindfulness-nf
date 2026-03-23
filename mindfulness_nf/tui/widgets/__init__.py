"""TUI widgets for the mindfulness neurofeedback pipeline."""

from mindfulness_nf.tui.widgets.log_panel import LogPanel
from mindfulness_nf.tui.widgets.preflight_checklist import PreflightChecklist
from mindfulness_nf.tui.widgets.run_progress import RunProgress
from mindfulness_nf.tui.widgets.run_table import RunTable
from mindfulness_nf.tui.widgets.status_light import StatusLight

__all__ = [
    "LogPanel",
    "PreflightChecklist",
    "RunProgress",
    "RunTable",
    "StatusLight",
]
