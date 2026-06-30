"""End-to-end smoke test against the migrated layout.

Drives real orchestration code against the updated layout. Catches path
bugs the unit tests can't (unit tests mock subprocess.run; this exercises
real executor path logic, the real ``create_subject_session_dir``, and
a real ``SessionRunner``).

Two checks:

(a) **sub-process-rehearse** (migrated): verify path resolution after
    migration — ``ica.list_runs`` finds MURFI volumes at the subject
    root, ``ica.merge_runs`` writes to the session-scoped ``rest/``
    (not the old subject-root one). FSL is mocked so this is fast and
    doesn't depend on real data files.

(b) **sub-smoketest** (fresh): create a new subject/session via the
    updated ``create_subject_session_dir`` and drive a ``SessionRunner``
    end-to-end through the first few steps. Verifies the full chain:
    create_subject_session_dir → SessionRunner init → provenance +
    session_state land in the right place → no subject-root mingling.

Run: ``uv run python scripts/smoke_dry_run.py``
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import traceback
from pathlib import Path
from unittest.mock import MagicMock, patch

from mindfulness_nf.config import PipelineConfig, ScannerConfig
from mindfulness_nf.orchestration import ica
from mindfulness_nf.orchestration.layout import SubjectLayout
from mindfulness_nf.orchestration.scanner_source import SimulatedScannerSource
from mindfulness_nf.orchestration.session_runner import SessionRunner
from mindfulness_nf.orchestration.subjects import create_subject_session_dir


REPO = Path(__file__).resolve().parent.parent
SUBJECTS_DIR = REPO / "murfi" / "subjects"
TEMPLATE_DIR = REPO / "murfi"


class Check:
    """Collects pass/fail for a single smoke scenario."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.pass_count = 0
        self.fail_count = 0

    def expect(self, condition: bool, msg: str) -> None:
        if condition:
            self.pass_count += 1
            print(f"    ✓ {msg}")
        else:
            self.fail_count += 1
            print(f"    ✗ {msg}")

    def report(self) -> bool:
        ok = self.fail_count == 0
        marker = "PASS" if ok else "FAIL"
        print(f"\n  [{marker}] {self.name}: {self.pass_count} passed, {self.fail_count} failed")
        return ok


async def scenario_a_migrated_subject_paths() -> bool:
    """Exercise path resolution on the migrated sub-process-rehearse.

    Calls the real ``ica.list_runs`` and ``ica.merge_runs`` helpers with
    fslmerge mocked — verifies they find data and write to the correct
    (session-scoped) locations. This is the regression guard for the
    migration: before the layout change, ``rest_dir`` was subject-scoped
    and filenames hardcoded ``ses-localizer``.
    """
    print("\n=== (a) Path resolution on migrated sub-process-rehearse ===")
    subject_dir = SUBJECTS_DIR / "sub-process-rehearse"
    session_dir = subject_dir / "ses-process"
    if not session_dir.is_dir():
        print(f"  ! skipping: {session_dir} missing")
        return True

    check = Check("Migrated subject path resolution")

    # Construct layout the way SessionRunner would.
    layout = SubjectLayout(
        subjects_root=SUBJECTS_DIR,
        subject_id="sub-process-rehearse",
        session_type="process",
    )

    # 1. list_runs must find MURFI volumes at layout.img_dir (subject root).
    runs = await ica.list_runs(layout)
    check.expect(len(runs) >= 1, f"list_runs found {len(runs)} run(s) under {layout.img_dir}")
    if runs:
        check.expect(
            runs[0].path == layout.img_dir,
            f"run.path = layout.img_dir ({layout.img_dir})",
        )

    # 2. merge_runs writes to layout.rest_dir (session-scoped). Mock fslmerge
    #    so this is fast + doesn't rewrite the user's 589MB _bold.nii.
    if runs:
        run_idx = int(runs[0].run_name.split("-")[1])
        with patch("mindfulness_nf.orchestration.ica.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            merged = await ica.merge_runs(layout, (run_idx,), tr=1.2)

        check.expect(
            merged.parent == layout.rest_dir,
            f"merge output in session-scoped rest_dir (got {merged.parent})",
        )
        check.expect(
            merged.parent != subject_dir / "rest",
            "merge output is NOT at subject-root rest/ (pre-migration path)",
        )
        check.expect(
            f"ses-process" in merged.name,
            f"filename uses actual session_type ses-process (got {merged.name})",
        )
        check.expect(
            "ses-localizer" not in merged.name,
            f"filename does NOT hardcode ses-localizer (got {merged.name})",
        )

    # 3. Layout-level invariants.
    check.expect(
        not (subject_dir / "rest").exists(),
        "subject_root/rest/ is gone (migration removed it)",
    )
    check.expect(
        (subject_dir / "mask").is_dir(),
        "subject_root/mask/ still exists (cross-session)",
    )
    check.expect(
        (session_dir / "rest").is_dir(),
        "session_dir/rest/ exists (session-scoped)",
    )

    return check.report()


async def scenario_b_fresh_subject_end_to_end() -> bool:
    """Create sub-smoketest fresh and exercise SessionRunner end-to-end.

    The runner goes through start_current()/supervise/completion for the
    non-scanner steps (Setup) and stops cleanly at the scanner step
    (2-volume) rather than trying to actually push vSend data. The point
    is to catch any path/state error in the first real executor hand-off.
    """
    print("\n=== (b) Fresh sub-smoketest end-to-end ===")
    subject_dir = SUBJECTS_DIR / "sub-smoketest"
    if subject_dir.exists():
        shutil.rmtree(subject_dir)

    check = Check("Fresh subject end-to-end")
    try:
        session_dir = create_subject_session_dir(
            SUBJECTS_DIR, "sub-smoketest", "rt15", TEMPLATE_DIR
        )
    except Exception as exc:
        print(f"  ! create_subject_session_dir failed: {exc}")
        traceback.print_exc()
        return False

    # New layout: only populated dirs created.
    for sub in ("log", "rest", "qc", "sourcedata/murfi/xml", "sourcedata/psychopy"):
        check.expect((session_dir / sub).is_dir(), f"created {sub}")
    for dead in ("func", "derivatives/masks", "sourcedata/murfi/img", "sourcedata/murfi/log"):
        check.expect(not (session_dir / dead).exists(), f"did NOT create {dead}")

    # Pre-populate mask/ + xfm/ so rt15 has prerequisites.
    (subject_dir / "mask").mkdir(parents=True, exist_ok=True)
    (subject_dir / "mask" / "dmn.nii").write_bytes(b"FAKE_DMN")
    (subject_dir / "mask" / "cen.nii").write_bytes(b"FAKE_CEN")
    (subject_dir / "xfm").mkdir(parents=True, exist_ok=True)
    (subject_dir / "xfm" / "study_ref.nii").write_bytes(b"FAKE_REF")

    # Boot the runner. provenance.json should be written before we do anything.
    try:
        runner = SessionRunner.load_or_create(
            subject_dir=session_dir,
            session_type="rt15",
            pipeline=PipelineConfig(),
            scanner_config=ScannerConfig(),
            scanner_source=SimulatedScannerSource(),
            dry_run=True,
        )
    except Exception as exc:
        print(f"  ! SessionRunner.load_or_create failed: {exc}")
        traceback.print_exc()
        return False

    check.expect(
        (session_dir / "session_state.json").is_file(),
        "session_state.json written at startup",
    )
    check.expect(
        (session_dir / "provenance.json").is_file(),
        "provenance.json written at session init",
    )
    check.expect(
        runner.state.subject == "sub-smoketest",
        f"runner.state.subject = sub-smoketest (got {runner.state.subject!r})",
    )
    check.expect(
        runner.state.session_type == "rt15",
        f"runner.state.session_type = rt15 (got {runner.state.session_type!r})",
    )

    # Verify provenance fields are sane.
    import json
    prov = json.loads((session_dir / "provenance.json").read_text())
    check.expect(
        "timestamp" in prov and "hostname" in prov,
        "provenance has timestamp + hostname",
    )

    # Executor construction (start_current) exercises the layout chain.
    # Setup step is non-scanner; completes without needing a real scanner.
    try:
        await runner.start_current()
        # Wait for Setup to complete (dry-run, should be fast).
        for _ in range(40):
            await asyncio.sleep(0.1)
            if runner.state.steps[0].status.value == "completed":
                break
    except Exception as exc:
        print(f"  ! start_current(Setup) raised: {exc}")
        traceback.print_exc()

    setup_status = runner.state.steps[0].status.value
    check.expect(
        setup_status == "completed",
        f"Setup step reached completed (got {setup_status})",
    )

    # After Setup, ensure no subject-root rest/ or qc/ leaked.
    check.expect(
        not (subject_dir / "rest").exists(),
        "subject_root/rest/ NOT created by Setup",
    )
    check.expect(
        not (subject_dir / "qc").exists(),
        "subject_root/qc/ NOT created by Setup",
    )

    # Stop the runner so nothing is left running.
    try:
        await runner.stop_current()
    except Exception:
        pass

    return check.report()


async def main() -> int:
    print("Smoke test: dry-run validation of migrated layout")
    results = [
        await scenario_a_migrated_subject_paths(),
        await scenario_b_fresh_subject_end_to_end(),
    ]
    ok = all(results)
    print("\n" + "=" * 60)
    print("RESULT: " + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    # Cleanup: sub-smoketest is ephemeral.
    smoke = SUBJECTS_DIR / "sub-smoketest"
    if smoke.exists():
        shutil.rmtree(smoke)
        print("(cleaned up sub-smoketest)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
