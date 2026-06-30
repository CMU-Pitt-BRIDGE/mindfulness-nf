"""Tests for preflight checks.

Mocks only external I/O (subprocess, network). Never mocks internal code.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mindfulness_nf.config import ScannerConfig
from mindfulness_nf.models import CheckResult
from mindfulness_nf.orchestration.preflight import (
    check_apptainer_installed,
    check_container_exists,
    check_ethernet_interface,
    check_firewall_port_4006,
    check_firewall_port_50000,
    check_fsl_on_path,
    check_port_15001_free,
    check_port_50000_can_bind,
    check_port_50000_free,
    check_scanner_reachable,
    check_stale_murfi_processes,
    check_subject_directory,
    check_vsend_on_path,
    check_wifi_off,
    run_preflight,
)


# ---------------------------------------------------------------------------
# 1. FSL on PATH
# ---------------------------------------------------------------------------


class TestCheckFslOnPath:
    def test_pass(self) -> None:
        with patch("mindfulness_nf.orchestration.preflight.shutil.which", return_value="/opt/fsl/bin/flirt"):
            result = asyncio.run(check_fsl_on_path())
        assert result == CheckResult(name="FSL on PATH", passed=True, message="flirt found")

    def test_fail(self) -> None:
        with patch("mindfulness_nf.orchestration.preflight.shutil.which", return_value=None):
            result = asyncio.run(check_fsl_on_path())
        assert result == CheckResult(name="FSL on PATH", passed=False, message="FSL not on PATH")


class TestCheckVsendOnPath:
    """vSend is now an in-process Python sender; the check succeeds as
    long as the module is importable (true by repo invariant)."""

    def test_module_importable(self) -> None:
        result = asyncio.run(check_vsend_on_path())
        assert result.passed is True
        assert "sender" in result.message.lower()


# ---------------------------------------------------------------------------
# 2. Apptainer installed
# ---------------------------------------------------------------------------


class TestCheckApptainerInstalled:
    def test_pass(self) -> None:
        with patch("mindfulness_nf.orchestration.preflight.shutil.which", return_value="/usr/bin/apptainer"):
            result = asyncio.run(check_apptainer_installed())
        assert result.passed is True

    def test_fail(self) -> None:
        with patch("mindfulness_nf.orchestration.preflight.shutil.which", return_value=None):
            result = asyncio.run(check_apptainer_installed())
        assert result.passed is False


# ---------------------------------------------------------------------------
# 3. MURFI container exists
# ---------------------------------------------------------------------------


class TestCheckContainerExists:
    def test_pass(self, tmp_path: Path) -> None:
        container = tmp_path / "murfi.sif"
        container.touch()
        result = asyncio.run(check_container_exists(str(container)))
        assert result.passed is True

    def test_fail(self, tmp_path: Path) -> None:
        result = asyncio.run(check_container_exists(str(tmp_path / "missing.sif")))
        assert result.passed is False


# ---------------------------------------------------------------------------
# 4. Subject directory exists
# ---------------------------------------------------------------------------


class TestCheckSubjectDirectory:
    def test_pass(self, tmp_path: Path) -> None:
        subject_dir = tmp_path / "sub-01"
        subject_dir.mkdir()
        result = asyncio.run(check_subject_directory(subject_dir))
        assert result.passed is True

    def test_fail(self, tmp_path: Path) -> None:
        result = asyncio.run(check_subject_directory(tmp_path / "missing"))
        assert result.passed is False

    def test_skip_localizer(self) -> None:
        result = asyncio.run(check_subject_directory(None))
        assert result.passed is True
        assert "skipped" in result.message


# ---------------------------------------------------------------------------
# 5. Ethernet interface has 192.168.2.x
# ---------------------------------------------------------------------------


def _mock_subprocess_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Default mock that returns empty output."""
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")


class TestCheckEthernetInterface:
    def test_pass(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="2: enp0s31f6: <BROADCAST> mtu 1500\n    inet 192.168.2.5/24\n",
                stderr="",
            )

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_ethernet_interface())
        assert result.passed is True

    def test_fail(self) -> None:
        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=_mock_subprocess_run):
            result = asyncio.run(check_ethernet_interface())
        assert result.passed is False


# ---------------------------------------------------------------------------
# 6. Scanner reachable via ping
# ---------------------------------------------------------------------------


class TestCheckScannerReachable:
    def test_pass(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_scanner_reachable("192.168.2.1"))
        assert result.passed is True

    def test_fail(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_scanner_reachable("192.168.2.1"))
        assert result.passed is False

    def test_timeout(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=5)

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_scanner_reachable("192.168.2.1"))
        assert result.passed is False


# ---------------------------------------------------------------------------
# 7. Wi-Fi is off
# ---------------------------------------------------------------------------


class TestCheckWifiOff:
    def test_pass_no_wireless(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="1: lo: <LOOPBACK> mtu 65536\n    inet 127.0.0.1/8\n2: enp0s31f6: <BROADCAST> mtu 1500\n    inet 192.168.2.5/24\n",
                stderr="",
            )

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_wifi_off())
        assert result.passed is True

    def test_fail_wifi_active(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="3: wlp3s0: <BROADCAST,MULTICAST,UP> mtu 1500\n    inet 10.0.0.5/24\n",
                stderr="",
            )

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_wifi_off())
        assert result.passed is False

    def test_pass_wifi_no_ip(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="3: wlp3s0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500\n    link/ether aa:bb:cc:dd:ee:ff\n",
                stderr="",
            )

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_wifi_off())
        assert result.passed is True


# ---------------------------------------------------------------------------
# 8-9. Port free checks (50000, 15001)
# ---------------------------------------------------------------------------


class TestCheckPortFree:
    def test_port_50000_free(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="LISTEN 0 128 *:22 *:*\n", stderr="")

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_port_50000_free())
        assert result.passed is True

    def test_port_50000_in_use(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="LISTEN 0 128 *:50000 *:*\n",
                stderr="",
            )

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_port_50000_free())
        assert result.passed is False

    def test_port_15001_free(self) -> None:
        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=_mock_subprocess_run):
            result = asyncio.run(check_port_15001_free())
        assert result.passed is True

    def test_port_15001_in_use(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="LISTEN 0 128 *:15001 *:*\n",
                stderr="",
            )

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_port_15001_free())
        assert result.passed is False


# ---------------------------------------------------------------------------
# 10. Port 50000 can bind
# ---------------------------------------------------------------------------


class TestCheckPort50000CanBind:
    def test_pass(self) -> None:
        with patch("mindfulness_nf.orchestration.preflight._try_bind_port", return_value=True):
            result = asyncio.run(check_port_50000_can_bind())
        assert result.passed is True

    def test_fail(self) -> None:
        with patch("mindfulness_nf.orchestration.preflight._try_bind_port", return_value=False):
            result = asyncio.run(check_port_50000_can_bind())
        assert result.passed is False


# ---------------------------------------------------------------------------
# 11-12. Firewall checks
# ---------------------------------------------------------------------------


class TestFirewallChecks:
    def test_port_50000_allowed(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="chain ip filter ufw-user-input {\n  tcp dport 50000 accept\n}\n",
                stderr="",
            )

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_firewall_port_50000())
        assert result.passed is True

    def test_port_50000_blocked(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="chain ip filter ufw-user-input {\n  tcp dport 22 accept\n}\n",
                stderr="",
            )

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_firewall_port_50000())
        assert result.passed is False

    def test_nft_not_available(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_firewall_port_50000())
        assert result.passed is True
        assert "skipping" in result.message

    def test_port_4006_allowed(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="chain ip filter ufw-user-input {\n  tcp dport 4006 accept\n}\n",
                stderr="",
            )

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_firewall_port_4006())
        assert result.passed is True

    def test_port_4006_blocked(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="chain ip filter ufw-user-input {\n  tcp dport 22 accept\n}\n",
                stderr="",
            )

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_firewall_port_4006())
        assert result.passed is False

    def test_nft_command_missing(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("nft not found")

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_firewall_port_50000())
        assert result.passed is True
        assert "skipped" in result.message


# ---------------------------------------------------------------------------
# 13. Stale MURFI process cleanup
# ---------------------------------------------------------------------------


class TestCheckStaleMurfiProcesses:
    def test_no_stale(self) -> None:
        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=_mock_subprocess_run):
            result = asyncio.run(check_stale_murfi_processes())
        assert result.passed is True

    def test_stale_on_50000(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="LISTEN 0 128 *:50000 *:* users:((\"murfi\",pid=1234,fd=5))\n",
                stderr="",
            )

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_stale_murfi_processes())
        assert result.passed is False
        assert "50000" in result.message

    def test_stale_on_both_ports(self) -> None:
        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="LISTEN 0 128 *:50000 *:*\nLISTEN 0 128 *:15001 *:*\n",
                stderr="",
            )

        with patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run):
            result = asyncio.run(check_stale_murfi_processes())
        assert result.passed is False
        assert "50000" in result.message
        assert "15001" in result.message


# ---------------------------------------------------------------------------
# run_preflight integration
# ---------------------------------------------------------------------------


class TestRunPreflight:
    def test_returns_14_results(self, tmp_path: Path) -> None:
        """run_preflight returns exactly 14 CheckResult items (incl. vSend)."""
        container = tmp_path / "murfi.sif"
        container.touch()
        config = ScannerConfig(murfi_container=str(container))

        with (
            patch("mindfulness_nf.orchestration.preflight.shutil.which", return_value="/usr/bin/stub"),
            patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=_mock_subprocess_run),
            patch("mindfulness_nf.orchestration.preflight._try_bind_port", return_value=True),
        ):
            results = asyncio.run(run_preflight(config, subject_dir=None))

        assert len(results) == 14
        assert all(isinstance(r, CheckResult) for r in results)
        names = {r.name for r in results}
        assert "vSend on PATH" in names

    def test_all_pass_when_healthy(self, tmp_path: Path) -> None:
        """When all external checks succeed, all results pass."""
        container = tmp_path / "murfi.sif"
        container.touch()
        config = ScannerConfig(murfi_container=str(container))

        def mock_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            cmd = args[0] if args else ""
            if cmd == "ip":
                return subprocess.CompletedProcess(
                    args=args,
                    returncode=0,
                    stdout="2: enp0s31f6: <BROADCAST> mtu 1500\n    inet 192.168.2.5/24\n",
                    stderr="",
                )
            if cmd == "ping":
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if cmd == "ss":
                return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            if cmd == "sudo":
                # nft — return empty/failure so firewall check skips
                return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="")
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        with (
            patch("mindfulness_nf.orchestration.preflight.shutil.which", return_value="/usr/bin/stub"),
            patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=mock_run),
            patch("mindfulness_nf.orchestration.preflight._try_bind_port", return_value=True),
        ):
            results = asyncio.run(run_preflight(config, subject_dir=None))

        assert all(r.passed for r in results), [r for r in results if not r.passed]

    def test_subject_dir_checked_when_provided(self, tmp_path: Path) -> None:
        """When subject_dir is provided and missing, that check fails."""
        container = tmp_path / "murfi.sif"
        container.touch()
        config = ScannerConfig(murfi_container=str(container))
        missing_dir = tmp_path / "sub-99"

        with (
            patch("mindfulness_nf.orchestration.preflight.shutil.which", return_value="/usr/bin/stub"),
            patch("mindfulness_nf.orchestration.preflight.subprocess.run", side_effect=_mock_subprocess_run),
            patch("mindfulness_nf.orchestration.preflight._try_bind_port", return_value=True),
        ):
            results = asyncio.run(run_preflight(config, subject_dir=missing_dir))

        subject_check = [r for r in results if r.name == "Subject directory"][0]
        assert subject_check.passed is False
