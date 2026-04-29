"""Tests for :class:`DicomStepExecutor`.

Mocks MURFI/DicomReceiver start so the executor's monitor loop can be
exercised against a real-on-disk log file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.models import StepConfig, StepKind
from mindfulness_nf.orchestration.executor import StepProgress
from mindfulness_nf.orchestration.executors.dicom import DicomStepExecutor
from mindfulness_nf.orchestration.murfi import MurfiProcess
from mindfulness_nf.orchestration.scanner_source import NoOpScannerSource


def _step_config() -> StepConfig:
    return StepConfig(
        name="Rest 1",
        task="rest",
        run=1,
        progress_target=250,
        progress_unit="volumes",
        xml_name="rest.xml",
        kind=StepKind.DICOM_SCAN,
    )


def _fake_murfi_process(log_path: Path, returncode: int | None = None) -> MurfiProcess:
    """Build a MurfiProcess with a real on-disk log file."""
    proc = MagicMock(spec=asyncio.subprocess.Process)
    proc.returncode = returncode
    proc.pid = 12345
    return MurfiProcess(process=proc, log_path=log_path, xml_name="rest.xml")


class TestDicomStepExecutorFailFast:
    """Executor must abort when MURFI logs a fatal error, not wait for
    ``process.returncode`` — MURFI can print ``ERROR:`` and keep running,
    which left the step in 'running forever' state.
    """

    @pytest.mark.asyncio
    async def test_fails_when_murfi_log_reports_parse_failure(
        self, tmp_path: Path
    ) -> None:
        session_dir = tmp_path / "sub-001" / "ses-loc3"
        (session_dir / "log").mkdir(parents=True)
        log_path = session_dir / "log" / "murfi_rest.log"
        log_path.write_text(
            "intializing experiment\n"
            "parsing config file...Failed to open file\n"
            "failed\n"
            "ERROR: failed to parse config file: /nope/rest.xml\n"
        )

        # Fake MURFI: still "running" from process.returncode's perspective.
        fake_murfi = _fake_murfi_process(log_path, returncode=None)
        fake_dicom_receiver = MagicMock()
        fake_dicom_receiver.stop = AsyncMock()

        with patch(
            "mindfulness_nf.orchestration.executors.dicom.murfi_mod.start",
            AsyncMock(return_value=fake_murfi),
        ), patch(
            "mindfulness_nf.orchestration.executors.dicom.murfi_mod.stop",
            AsyncMock(),
        ), patch(
            "mindfulness_nf.orchestration.executors.dicom.DicomReceiver.start",
            AsyncMock(return_value=fake_dicom_receiver),
        ):
            executor = DicomStepExecutor(
                config=_step_config(),
                subject_dir=session_dir,
                pipeline=PipelineConfig(),
                scanner_config=ScannerConfig(),
                scanner_source=NoOpScannerSource(),
            )

            progress_updates: list[StepProgress] = []

            outcome = await asyncio.wait_for(
                executor.run(progress_updates.append),
                timeout=3.0,
            )

        assert outcome.succeeded is False
        assert outcome.error is not None
        assert "failed to parse" in outcome.error.lower() or "error" in outcome.error.lower()

    @pytest.mark.asyncio
    async def test_push_dicom_targets_local_receiver_not_scanner_ip(
        self, tmp_path: Path
    ) -> None:
        """The DICOM receiver runs in-process; the simulator must push to
        localhost. Previous code forwarded ``scanner_config.scanner_ip``
        (192.168.2.1) as the target, so push_dicom blackholed and MURFI
        waited forever for files to appear.
        """
        from mindfulness_nf.orchestration.scanner_source import NoOpScannerSource

        session_dir = tmp_path / "sub-001" / "ses-loc3"
        (session_dir / "log").mkdir(parents=True)
        log_path = session_dir / "log" / "murfi_rest.log"
        log_path.write_text("")  # benign log

        # Fake MURFI that exits immediately so the monitor loop terminates.
        fake_murfi = _fake_murfi_process(log_path, returncode=0)
        fake_receiver = MagicMock()
        fake_receiver.stop = AsyncMock()

        scanner_source = NoOpScannerSource()
        scanner_config = ScannerConfig(
            scanner_ip="192.168.2.1", dicom_port=4006
        )

        with patch(
            "mindfulness_nf.orchestration.executors.dicom.murfi_mod.start",
            AsyncMock(return_value=fake_murfi),
        ), patch(
            "mindfulness_nf.orchestration.executors.dicom.murfi_mod.stop",
            AsyncMock(),
        ), patch(
            "mindfulness_nf.orchestration.executors.dicom.DicomReceiver.start",
            AsyncMock(return_value=fake_receiver),
        ):
            executor = DicomStepExecutor(
                config=_step_config(),
                subject_dir=session_dir,
                pipeline=PipelineConfig(),
                scanner_config=scanner_config,
                scanner_source=scanner_source,
            )
            await asyncio.wait_for(
                executor.run(lambda _p: None), timeout=3.0
            )

        # NoOpScannerSource records calls. The call's target_host must be
        # localhost — the receiver is in *this* process, not the scanner.
        assert len(scanner_source.push_dicom_calls) == 1
        target_host, target_port, ae_title, _step = scanner_source.push_dicom_calls[0]
        assert target_host in ("127.0.0.1", "localhost"), (
            f"push_dicom target must be localhost, got {target_host!r}"
        )
        assert target_port == 4006

    @pytest.mark.asyncio
    async def test_succeeds_after_push_completes_if_volumes_near_target(
        self, tmp_path: Path
    ) -> None:
        """After the simulator has finished pushing every DICOM, MURFI
        sometimes ends one short (e.g. 249/250). We need the executor to
        accept this as success once the push task is done and no new
        volumes have arrived for a grace period — otherwise the step hangs
        forever even though the scan is functionally complete.
        """
        from mindfulness_nf.orchestration.scanner_source import NoOpScannerSource

        session_dir = tmp_path / "sub-001" / "ses-loc3"
        (session_dir / "log").mkdir(parents=True)
        log_path = session_dir / "log" / "murfi_rest.log"

        # Pre-populate the log with target-1 "received image from scanner" lines
        # — simulates MURFI having already processed volumes up to the off-by-one.
        target = 5  # small for the test
        log_path.write_text(
            "\n".join(
                f"received image from scanner: series 1 acquisition 1"
                for _ in range(target - 1)
            ) + "\n"
        )

        fake_murfi = _fake_murfi_process(log_path, returncode=None)  # still "running"
        fake_receiver = MagicMock()
        fake_receiver.stop = AsyncMock()

        # Scanner source's push completes immediately — simulating the case
        # where all DICOMs have been delivered by the time monitor starts.
        class _CompletedPushSource(NoOpScannerSource):
            async def push_dicom(self, *args, **kwargs):  # type: ignore[override]
                return None

        step = StepConfig(
            name="Rest 1",
            task="rest",
            run=1,
            progress_target=target,
            progress_unit="volumes",
            xml_name="rest.xml",
            kind=StepKind.DICOM_SCAN,
        )

        with patch(
            "mindfulness_nf.orchestration.executors.dicom.murfi_mod.start",
            AsyncMock(return_value=fake_murfi),
        ), patch(
            "mindfulness_nf.orchestration.executors.dicom.murfi_mod.stop",
            AsyncMock(),
        ), patch(
            "mindfulness_nf.orchestration.executors.dicom.DicomReceiver.start",
            AsyncMock(return_value=fake_receiver),
        ), patch(
            # Short grace period so the test finishes quickly.
            "mindfulness_nf.orchestration.executors.dicom._POST_PUSH_GRACE_SECONDS",
            0.5,
        ):
            executor = DicomStepExecutor(
                config=step,
                subject_dir=session_dir,
                pipeline=PipelineConfig(),
                scanner_config=ScannerConfig(),
                scanner_source=_CompletedPushSource(),
            )
            outcome = await asyncio.wait_for(
                executor.run(lambda _p: None), timeout=3.0
            )

        assert outcome.succeeded is True, (
            f"expected success after push+grace at target-1, got error={outcome.error!r}"
        )
        assert outcome.final_progress.value == target - 1

    @pytest.mark.asyncio
    async def test_does_not_fail_on_benign_lines(self, tmp_path: Path) -> None:
        """A log without ERROR markers must NOT trigger fail-fast."""
        session_dir = tmp_path / "sub-001" / "ses-loc3"
        (session_dir / "log").mkdir(parents=True)
        log_path = session_dir / "log" / "murfi_rest.log"
        log_path.write_text("starting up\nnormal info line\n")

        # MURFI will "exit cleanly" after one monitor cycle so the test
        # terminates — we just want to prove fail-fast wasn't triggered on
        # the benign lines.
        fake_murfi = _fake_murfi_process(log_path, returncode=0)
        fake_dicom_receiver = MagicMock()
        fake_dicom_receiver.stop = AsyncMock()

        with patch(
            "mindfulness_nf.orchestration.executors.dicom.murfi_mod.start",
            AsyncMock(return_value=fake_murfi),
        ), patch(
            "mindfulness_nf.orchestration.executors.dicom.murfi_mod.stop",
            AsyncMock(),
        ), patch(
            "mindfulness_nf.orchestration.executors.dicom.DicomReceiver.start",
            AsyncMock(return_value=fake_dicom_receiver),
        ):
            executor = DicomStepExecutor(
                config=_step_config(),
                subject_dir=session_dir,
                pipeline=PipelineConfig(),
                scanner_config=ScannerConfig(),
                scanner_source=NoOpScannerSource(),
            )

            outcome = await asyncio.wait_for(
                executor.run(lambda _p: None),
                timeout=3.0,
            )

        # MURFI exited rc=0 but volumes<target → step fails due to "MURFI exited"
        # but the *error string* must NOT mention parse/error — the log was clean.
        if outcome.error:
            assert "failed to parse" not in outcome.error.lower()
            assert "error:" not in outcome.error.lower()
