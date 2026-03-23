"""Tests for TUI widgets.

Uses Textual's App.run_test() pattern per Textual 8.x conventions.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Label, RichLog, Static

from mindfulness_nf.models import CheckResult, Color, RunState, TrafficLight
from mindfulness_nf.tui.widgets.log_panel import LogPanel
from mindfulness_nf.tui.widgets.preflight_checklist import PreflightChecklist
from mindfulness_nf.tui.widgets.run_progress import RunProgress
from mindfulness_nf.tui.widgets.run_table import RunInfo, RunTable
from mindfulness_nf.tui.widgets.status_light import StatusLight


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class StatusLightApp(App[None]):
    def compose(self) -> ComposeResult:
        yield StatusLight(id="sl")


class RunProgressApp(App[None]):
    def compose(self) -> ComposeResult:
        yield RunProgress(id="rp")


class LogPanelApp(App[None]):
    def compose(self) -> ComposeResult:
        yield LogPanel(id="lp")


class PreflightApp(App[None]):
    def compose(self) -> ComposeResult:
        yield PreflightChecklist(id="pc")


class RunTableApp(App[None]):
    def compose(self) -> ComposeResult:
        yield RunTable(id="rt")


@dataclass(frozen=True, slots=True)
class FakeRunInfo:
    """Test implementation of the RunInfo protocol."""

    name: str
    volumes: int
    quality: Color


# ---------------------------------------------------------------------------
# StatusLight tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_light_green() -> None:
    """StatusLight renders green indicator for Color.GREEN."""
    app = StatusLightApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#sl", StatusLight)
        widget.update(TrafficLight(Color.GREEN, "All good"))
        await pilot.pause()
        indicator = widget.query_one("#indicator", Label)
        # The content should contain "green" markup
        assert "green" in indicator.content


@pytest.mark.asyncio
async def test_status_light_yellow() -> None:
    """StatusLight renders yellow indicator for Color.YELLOW."""
    app = StatusLightApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#sl", StatusLight)
        widget.update(TrafficLight(Color.YELLOW, "Warning"))
        await pilot.pause()
        indicator = widget.query_one("#indicator", Label)
        assert "yellow" in indicator.content


@pytest.mark.asyncio
async def test_status_light_red() -> None:
    """StatusLight renders red indicator for Color.RED."""
    app = StatusLightApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#sl", StatusLight)
        widget.update(TrafficLight(Color.RED, "Critical failure"))
        await pilot.pause()
        indicator = widget.query_one("#indicator", Label)
        assert "red" in indicator.content


@pytest.mark.asyncio
async def test_status_light_message() -> None:
    """StatusLight shows the message from the TrafficLight."""
    app = StatusLightApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#sl", StatusLight)
        widget.update(TrafficLight(Color.GREEN, "Ready to advance"))
        await pilot.pause()
        msg = widget.query_one("#message", Label)
        assert "Ready to advance" in msg.content


@pytest.mark.asyncio
async def test_status_light_detail() -> None:
    """StatusLight shows detail text when present in TrafficLight."""
    app = StatusLightApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#sl", StatusLight)
        widget.update(
            TrafficLight(Color.YELLOW, "Warning", detail="Check scanner connection")
        )
        await pilot.pause()
        detail = widget.query_one("#detail", Label)
        assert "Check scanner connection" in detail.content


@pytest.mark.asyncio
async def test_status_light_detail_hidden_when_none() -> None:
    """StatusLight hides detail label when detail is None."""
    app = StatusLightApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#sl", StatusLight)
        widget.update(TrafficLight(Color.GREEN, "OK"))
        await pilot.pause()
        detail = widget.query_one("#detail", Label)
        assert not detail.display


# ---------------------------------------------------------------------------
# RunProgress tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_progress_volume_count() -> None:
    """RunProgress shows correct volume count text."""
    app = RunProgressApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#rp", RunProgress)
        run = RunState(name="Rest 1", expected_volumes=250, received_volumes=100)
        widget.update(run)
        await pilot.pause()
        vol_label = widget.query_one("#rp-volumes", Label)
        assert "100/250" in vol_label.content


@pytest.mark.asyncio
async def test_run_progress_name() -> None:
    """RunProgress shows the run name."""
    app = RunProgressApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#rp", RunProgress)
        run = RunState(name="Feedback 3", expected_volumes=150, received_volumes=75)
        widget.update(run)
        await pilot.pause()
        name_label = widget.query_one("#rp-name", Label)
        assert "Feedback 3" in name_label.content


@pytest.mark.asyncio
async def test_run_progress_checkmark_when_complete() -> None:
    """RunProgress shows checkmark when received >= expected."""
    app = RunProgressApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#rp", RunProgress)
        run = RunState(name="2vol", expected_volumes=20, received_volumes=20)
        widget.update(run)
        await pilot.pause()
        done_label = widget.query_one("#rp-done", Label)
        assert "\u2713" in done_label.content


@pytest.mark.asyncio
async def test_run_progress_no_checkmark_when_incomplete() -> None:
    """RunProgress shows no checkmark when received < expected."""
    app = RunProgressApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#rp", RunProgress)
        run = RunState(name="2vol", expected_volumes=20, received_volumes=10)
        widget.update(run)
        await pilot.pause()
        done_label = widget.query_one("#rp-done", Label)
        assert "\u2713" not in done_label.content


# ---------------------------------------------------------------------------
# LogPanel tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_panel_add_line() -> None:
    """LogPanel.add_line adds visible content."""
    app = LogPanelApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#lp", LogPanel)
        widget.add_line("Volume 1 received")
        widget.add_line("Volume 2 received")
        await pilot.pause()
        rich_log = widget.query_one("#log-output", RichLog)
        # RichLog tracks lines internally; check the line count
        assert len(rich_log.lines) >= 2


@pytest.mark.asyncio
async def test_log_panel_auto_scroll() -> None:
    """LogPanel RichLog has auto_scroll enabled."""
    app = LogPanelApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#lp", LogPanel)
        rich_log = widget.query_one("#log-output", RichLog)
        assert rich_log.auto_scroll is True


# ---------------------------------------------------------------------------
# PreflightChecklist tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_checklist_pass_indicator() -> None:
    """PreflightChecklist shows green checkmark for passed checks."""
    app = PreflightApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#pc", PreflightChecklist)
        results = (
            CheckResult(name="FSL", passed=True, message="Found at /opt/fsl"),
        )
        widget.set_results(results)
        await pilot.pause()
        static = widget.query_one("#checklist-content", Static)
        # Green checkmark character
        assert "\u2713" in static.content
        assert "FSL" in static.content


@pytest.mark.asyncio
async def test_preflight_checklist_fail_indicator() -> None:
    """PreflightChecklist shows red X for failed checks."""
    app = PreflightApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#pc", PreflightChecklist)
        results = (
            CheckResult(name="Scanner ping", passed=False, message="No response"),
        )
        widget.set_results(results)
        await pilot.pause()
        static = widget.query_one("#checklist-content", Static)
        # Red X character
        assert "\u2717" in static.content
        assert "Scanner ping" in static.content


@pytest.mark.asyncio
async def test_preflight_checklist_mixed() -> None:
    """PreflightChecklist handles a mix of passed and failed checks."""
    app = PreflightApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#pc", PreflightChecklist)
        results = (
            CheckResult(name="FSL", passed=True, message="OK"),
            CheckResult(name="Container", passed=False, message="Not found"),
            CheckResult(name="Network", passed=True, message="Connected"),
        )
        widget.set_results(results)
        await pilot.pause()
        static = widget.query_one("#checklist-content", Static)
        assert "FSL" in static.content
        assert "Container" in static.content
        assert "Network" in static.content


# ---------------------------------------------------------------------------
# RunTable tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_table_set_runs() -> None:
    """RunTable.set_runs populates the table with rows."""
    app = RunTableApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#rt", RunTable)
        runs: tuple[RunInfo, ...] = (
            FakeRunInfo(name="Rest 1", volumes=250, quality=Color.GREEN),
            FakeRunInfo(name="Rest 2", volumes=248, quality=Color.YELLOW),
        )
        widget.set_runs(runs)
        await pilot.pause()
        assert widget._run_count == 2


@pytest.mark.asyncio
async def test_run_table_selection() -> None:
    """RunTable.toggle_selection toggles and reports selected indices."""
    app = RunTableApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#rt", RunTable)
        runs: tuple[RunInfo, ...] = (
            FakeRunInfo(name="Rest 1", volumes=250, quality=Color.GREEN),
            FakeRunInfo(name="Rest 2", volumes=248, quality=Color.YELLOW),
        )
        widget.set_runs(runs)
        await pilot.pause()
        widget.toggle_selection(1)
        assert widget.selected == (1,)
        widget.toggle_selection(2)
        assert widget.selected == (1, 2)
        # Toggle off
        widget.toggle_selection(1)
        assert widget.selected == (2,)


@pytest.mark.asyncio
async def test_run_table_selection_out_of_range() -> None:
    """RunTable.toggle_selection ignores out-of-range indices."""
    app = RunTableApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#rt", RunTable)
        runs: tuple[RunInfo, ...] = (
            FakeRunInfo(name="Rest 1", volumes=250, quality=Color.GREEN),
        )
        widget.set_runs(runs)
        await pilot.pause()
        widget.toggle_selection(5)
        assert widget.selected == ()


@pytest.mark.asyncio
async def test_fake_run_info_satisfies_protocol() -> None:
    """FakeRunInfo satisfies the RunInfo protocol."""
    info = FakeRunInfo(name="Test", volumes=100, quality=Color.GREEN)
    assert isinstance(info, RunInfo)
