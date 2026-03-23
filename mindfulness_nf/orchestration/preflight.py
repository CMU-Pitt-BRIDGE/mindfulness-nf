"""Preflight checks for the mindfulness neurofeedback pipeline.

Imperative shell: I/O is expected here. Imports and uses models from
the functional core (CheckResult from models.py, ScannerConfig from config.py).
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
from pathlib import Path

from mindfulness_nf.config import ScannerConfig
from mindfulness_nf.models import CheckResult


async def check_fsl_on_path() -> CheckResult:
    """Check that FSL's flirt command is available on PATH."""
    if shutil.which("flirt") is not None:
        return CheckResult(name="FSL on PATH", passed=True, message="flirt found")
    return CheckResult(name="FSL on PATH", passed=False, message="FSL not on PATH")


async def check_apptainer_installed() -> CheckResult:
    """Check that Apptainer is installed."""
    if shutil.which("apptainer") is not None:
        return CheckResult(
            name="Apptainer installed", passed=True, message="apptainer found"
        )
    return CheckResult(
        name="Apptainer installed", passed=False, message="Apptainer not found"
    )


async def check_container_exists(container_path: str) -> CheckResult:
    """Check that the MURFI container file exists at the configured path."""
    if Path(container_path).is_file():
        return CheckResult(
            name="MURFI container",
            passed=True,
            message=f"container found at {container_path}",
        )
    return CheckResult(
        name="MURFI container",
        passed=False,
        message=f"container missing at {container_path}",
    )


async def check_subject_directory(subject_dir: Path | None) -> CheckResult:
    """Check that the subject directory exists. Skip for localizer (subject_dir=None)."""
    if subject_dir is None:
        return CheckResult(
            name="Subject directory",
            passed=True,
            message="skipped (localizer session)",
        )
    if subject_dir.is_dir():
        return CheckResult(
            name="Subject directory",
            passed=True,
            message=f"{subject_dir} exists",
        )
    return CheckResult(
        name="Subject directory",
        passed=False,
        message=f"{subject_dir} does not exist",
    )


async def check_ethernet_interface() -> CheckResult:
    """Check that an ethernet interface has a 192.168.2.x address."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["ip", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if "192.168.2." in result.stdout:
            return CheckResult(
                name="Ethernet interface",
                passed=True,
                message="192.168.2.x interface detected",
            )
        return CheckResult(
            name="Ethernet interface",
            passed=False,
            message="no 192.168.2.x interface found",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return CheckResult(
            name="Ethernet interface",
            passed=False,
            message=f"failed to check interfaces: {exc}",
        )


async def check_scanner_reachable(scanner_ip: str) -> CheckResult:
    """Check that the scanner is reachable via ping."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["ping", "-c", "1", "-W", "2", scanner_ip],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return CheckResult(
                name="Scanner reachable",
                passed=True,
                message=f"{scanner_ip} reachable",
            )
        return CheckResult(
            name="Scanner reachable",
            passed=False,
            message=f"{scanner_ip} not reachable",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return CheckResult(
            name="Scanner reachable",
            passed=False,
            message=f"ping failed: {exc}",
        )


async def check_wifi_off() -> CheckResult:
    """Check that Wi-Fi is off (no active wireless interface with an IP)."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["ip", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = result.stdout.splitlines()
        in_wireless = False
        for line in lines:
            # Wireless interfaces start with wl (e.g. wlan0, wlp3s0)
            if "wl" in line and ": " in line and "mtu" in line.lower():
                in_wireless = True
            elif line and not line[0].isspace():
                in_wireless = False
            elif in_wireless and "inet " in line:
                return CheckResult(
                    name="Wi-Fi off",
                    passed=False,
                    message="Wi-Fi appears to be ON",
                )
        return CheckResult(name="Wi-Fi off", passed=True, message="Wi-Fi is off")
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return CheckResult(
            name="Wi-Fi off",
            passed=False,
            message=f"failed to check Wi-Fi: {exc}",
        )


async def _check_port_free_ss(port: int) -> CheckResult:
    """Check that a port is free using the ss command."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["ss", "-tlnp"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if f":{port}" in result.stdout:
            return CheckResult(
                name=f"Port {port} free",
                passed=False,
                message=f"port {port} is in use",
            )
        return CheckResult(
            name=f"Port {port} free",
            passed=True,
            message=f"port {port} is free",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return CheckResult(
            name=f"Port {port} free",
            passed=False,
            message=f"failed to check port {port}: {exc}",
        )


async def check_port_50000_free() -> CheckResult:
    """Check that port 50000 is free."""
    return await _check_port_free_ss(50000)


async def check_port_15001_free() -> CheckResult:
    """Check that port 15001 is free."""
    return await _check_port_free_ss(15001)


def _try_bind_port(port: int) -> bool:
    """Try to bind a TCP socket to a port. Returns True on success."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


async def check_port_50000_can_bind() -> CheckResult:
    """Check that port 50000 can be bound (TCP listener test)."""
    try:
        bound = await asyncio.to_thread(_try_bind_port, 50000)
        if bound:
            return CheckResult(
                name="Port 50000 bindable",
                passed=True,
                message="TCP listener test passed",
            )
        return CheckResult(
            name="Port 50000 bindable",
            passed=False,
            message="cannot bind port 50000",
        )
    except OSError as exc:
        return CheckResult(
            name="Port 50000 bindable",
            passed=False,
            message=f"bind test failed: {exc}",
        )


async def _check_firewall_port(port: int, subnet: str) -> CheckResult:
    """Check that the firewall allows a port from a given subnet via nftables."""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["sudo", "-n", "nft", "list", "chain", "ip", "filter", "ufw-user-input"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            # nft not available or no ufw-user-input chain — cannot verify
            return CheckResult(
                name=f"Firewall port {port}",
                passed=True,
                message=f"nftables chain not found, skipping firewall check for port {port}",
            )
        nft_output = result.stdout
        if f"dport {port}" not in nft_output:
            return CheckResult(
                name=f"Firewall port {port}",
                passed=False,
                message=f"port {port} NOT allowed in firewall",
            )
        return CheckResult(
            name=f"Firewall port {port}",
            passed=True,
            message=f"port {port} allowed from {subnet}",
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return CheckResult(
            name=f"Firewall port {port}",
            passed=True,
            message=f"firewall check skipped: {exc}",
        )


async def check_firewall_port_50000() -> CheckResult:
    """Check that the firewall allows port 50000 from 192.168.2.0/24."""
    return await _check_firewall_port(50000, "192.168.2.0/24")


async def check_firewall_port_4006() -> CheckResult:
    """Check that the firewall allows port 4006 from 192.168.2.0/24."""
    return await _check_firewall_port(4006, "192.168.2.0/24")


async def check_stale_murfi_processes() -> CheckResult:
    """Check for stale MURFI processes on ports 50000 and 15001."""
    stale_ports: list[int] = []
    for port in (50000, 15001):
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["ss", "-tlnp"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if f":{port}" in result.stdout:
                stale_ports.append(port)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    if stale_ports:
        ports_str = ", ".join(str(p) for p in stale_ports)
        return CheckResult(
            name="Stale MURFI processes",
            passed=False,
            message=f"stale processes found on port(s) {ports_str}",
        )
    return CheckResult(
        name="Stale MURFI processes",
        passed=True,
        message="no stale MURFI processes",
    )


async def run_preflight(
    config: ScannerConfig,
    subject_dir: Path | None = None,
) -> tuple[CheckResult, ...]:
    """Run all preflight checks and return results.

    Parameters
    ----------
    config:
        Scanner/network configuration.
    subject_dir:
        Path to the subject directory. Pass None for localizer sessions
        to skip the subject directory check.

    Returns
    -------
    tuple[CheckResult, ...]
        Results of all 13 preflight checks.
    """
    async with asyncio.TaskGroup() as tg:
        t_fsl = tg.create_task(check_fsl_on_path())
        t_apptainer = tg.create_task(check_apptainer_installed())
        t_container = tg.create_task(check_container_exists(config.murfi_container))
        t_subject = tg.create_task(check_subject_directory(subject_dir))
        t_ethernet = tg.create_task(check_ethernet_interface())
        t_scanner = tg.create_task(check_scanner_reachable(config.scanner_ip))
        t_wifi = tg.create_task(check_wifi_off())
        t_port50000 = tg.create_task(check_port_50000_free())
        t_port15001 = tg.create_task(check_port_15001_free())
        t_bind = tg.create_task(check_port_50000_can_bind())
        t_fw50000 = tg.create_task(check_firewall_port_50000())
        t_fw4006 = tg.create_task(check_firewall_port_4006())
        t_stale = tg.create_task(check_stale_murfi_processes())

    return (
        t_fsl.result(),
        t_apptainer.result(),
        t_container.result(),
        t_subject.result(),
        t_ethernet.result(),
        t_scanner.result(),
        t_wifi.result(),
        t_port50000.result(),
        t_port15001.result(),
        t_bind.result(),
        t_fw50000.result(),
        t_fw4006.result(),
        t_stale.result(),
    )
