"""Session step configurations.

Each session type is a tuple of StepConfig rows; SESSION_CONFIGS maps
session_type names to those tuples. No I/O imports here; data only.
"""

from __future__ import annotations

from mindfulness_nf.models import StepConfig, StepKind


def _feedback_block(start_run: int, count: int = 5) -> tuple[StepConfig, ...]:
    """Return StepConfig for ``count`` consecutive feedback runs, starting at
    run number ``start_run``. Each is 150 volumes via rtdmn.xml with feedback=True.
    """
    return tuple(
        StepConfig(
            name=f"Feedback {start_run + i}",
            task="feedback",
            run=start_run + i,
            progress_target=150,
            progress_unit="volumes",
            xml_name="rtdmn.xml",
            kind=StepKind.NF_RUN,
            feedback=True,
        )
        for i in range(count)
    )


def _fsl_stage(name: str, task: str, fsl_command: str) -> StepConfig:
    """Return a PROCESS_STAGE StepConfig with percent-based progress."""
    return StepConfig(
        name=name,
        task=task,
        run=None,
        progress_target=100,
        progress_unit="percent",
        xml_name=None,
        kind=StepKind.PROCESS_STAGE,
        fsl_command=fsl_command,
    )


LOC3: tuple[StepConfig, ...] = (
    StepConfig(
        name="Setup",
        task=None,
        run=None,
        progress_target=0,
        progress_unit="stages",
        xml_name=None,
        kind=StepKind.SETUP,
    ),
    # Rest runs are real-time — scanner pushes volumes via MURFI's vSend
    # scanner-input TCP protocol on port 50000. DICOM (port 4006) is
    # reserved for post-hoc transfers (selfref), not real-time rest.
    StepConfig(
        name="Rest 1",
        task="rest",
        run=1,
        progress_target=250,
        progress_unit="volumes",
        xml_name="rest.xml",
        kind=StepKind.VSEND_SCAN,
    ),
    StepConfig(
        name="Rest 2",
        task="rest",
        run=2,
        progress_target=250,
        progress_unit="volumes",
        xml_name="rest.xml",
        kind=StepKind.VSEND_SCAN,
    ),
)

RT15: tuple[StepConfig, ...] = (
    StepConfig(
        name="Setup",
        task=None,
        run=None,
        progress_target=0,
        progress_unit="stages",
        xml_name=None,
        kind=StepKind.SETUP,
    ),
    StepConfig(
        name="2-volume",
        task="2vol",
        run=1,
        progress_target=2,
        progress_unit="volumes",
        xml_name="2vol.xml",
        kind=StepKind.VSEND_SCAN,
    ),
    StepConfig(
        name="Transfer Pre",
        task="transferpre",
        run=1,
        progress_target=150,
        progress_unit="volumes",
        xml_name="rtdmn.xml",
        kind=StepKind.NF_RUN,
        feedback=False,
    ),
    *_feedback_block(start_run=1),  # Feedback 1-5
    StepConfig(
        name="Transfer Post",
        task="transferpost",
        run=1,
        progress_target=150,
        progress_unit="volumes",
        xml_name="rtdmn.xml",
        kind=StepKind.NF_RUN,
        feedback=False,
    ),
)
# RT15 has 9 steps: Setup, 2vol, TransferPre, Fb1-5, TransferPost.

RT30: tuple[StepConfig, ...] = (
    *RT15[:-1],  # Setup through Feedback 5
    StepConfig(
        name="Transfer Post 1",
        task="transferpost",
        run=1,
        progress_target=150,
        progress_unit="volumes",
        xml_name="rtdmn.xml",
        kind=StepKind.NF_RUN,
        feedback=False,
    ),
    *_feedback_block(start_run=6),  # Feedback 6-10
    StepConfig(
        name="Transfer Post 2",
        task="transferpost",
        run=2,
        progress_target=150,
        progress_unit="volumes",
        xml_name="rtdmn.xml",
        kind=StepKind.NF_RUN,
        feedback=False,
    ),
)
# RT30 has 15 steps: Setup, 2vol, TransferPre, Fb1-5, TransferPost1, Fb6-10, TransferPost2.

PROCESS: tuple[StepConfig, ...] = (
    StepConfig(
        name="Setup + select",
        task=None,
        run=None,
        progress_target=1,
        progress_unit="stages",
        xml_name=None,
        kind=StepKind.SETUP,
    ),
    # Interactive: preflight + operator picks which rest runs to process.
    # Selected run list stored in StepOutcome.artifacts["selected_runs"].
    _fsl_stage("Merge rests", "merge", fsl_command="fslmerge"),
    _fsl_stage("MELODIC ICA", "melodic", fsl_command="melodic"),
    _fsl_stage("Extract DMN", "dmn_mask", fsl_command="extract_dmn"),
    _fsl_stage("Extract CEN", "cen_mask", fsl_command="extract_cen"),
    _fsl_stage("Register", "register", fsl_command="flirt_applywarp"),
    StepConfig(
        name="QC",
        task="qc",
        run=None,
        progress_target=1,
        progress_unit="stages",
        xml_name=None,
        kind=StepKind.PROCESS_STAGE,
        fsl_command="qc_visualize",
    ),
)

SESSION_CONFIGS: dict[str, tuple[StepConfig, ...]] = {
    "loc3": LOC3,
    "rt15": RT15,
    "rt30": RT30,
    "process": PROCESS,
}
