"""Tests for the RunProgress tracker and its helpers."""

from __future__ import annotations

import time

import pytest

from figma_audit.utils.progress import (
    PHASE_LABELS,
    PHASE_ORDER,
    PhaseResult,
    RunProgress,
    _format_duration,
    get_progress,
    set_progress,
)


class TestFormatDuration:
    def test_seconds_under_minute(self) -> None:
        assert _format_duration(0) == "0.0s"
        assert _format_duration(0.5) == "0.5s"
        assert _format_duration(12.34) == "12.3s"
        assert _format_duration(59.9) == "59.9s"

    def test_minutes_and_seconds(self) -> None:
        assert _format_duration(60) == "1m00s"
        assert _format_duration(61) == "1m01s"
        assert _format_duration(125.7) == "2m05s"
        assert _format_duration(3600) == "60m00s"


class TestPhaseResult:
    def test_defaults(self) -> None:
        r = PhaseResult(name="analyze")
        assert r.name == "analyze"
        assert r.duration == 0
        assert r.detail == ""
        assert r.cost == 0
        assert r.tokens == 0

    def test_with_values(self) -> None:
        r = PhaseResult(name="compare", duration=12.5, detail="50 pages", cost=1.23, tokens=5000)
        assert r.duration == 12.5
        assert r.cost == 1.23
        assert r.tokens == 5000


class TestRunProgress:
    def test_initial_state(self) -> None:
        p = RunProgress()
        assert p.current_phase == ""
        assert p.current_step == ""
        assert p.current_progress == 0
        assert p.current_total == 0
        assert p.phase_results == []
        assert p.phases == PHASE_ORDER

    def test_start_phase_resets_step_state(self) -> None:
        p = RunProgress()
        p.update(step="something", progress=5, total=10)
        p.start_phase("analyze")
        assert p.current_phase == "analyze"
        assert p.current_step == ""
        assert p.current_progress == 0
        assert p.current_total == 0

    def test_update_partial(self) -> None:
        p = RunProgress()
        p.start_phase("capture")
        p.update(step="page 3/10", progress=3, total=10)
        assert p.current_step == "page 3/10"
        assert p.current_progress == 3
        assert p.current_total == 10

    def test_update_step_only_keeps_other_fields(self) -> None:
        """Calling update(step=...) without total preserves the prior total."""
        p = RunProgress()
        p.update(step="initial", progress=2, total=20)
        p.update(step="next")
        assert p.current_step == "next"
        # total preserved because we passed 0 (falsy)
        assert p.current_total == 20

    def test_finish_phase_records_result(self) -> None:
        p = RunProgress()
        p.start_phase("analyze")
        time.sleep(0.01)
        p.finish_phase(detail="35 pages", cost=0.31, tokens=12000)
        assert len(p.phase_results) == 1
        result = p.phase_results[0]
        assert result.name == "analyze"
        assert result.detail == "35 pages"
        assert result.cost == 0.31
        assert result.tokens == 12000
        assert result.duration > 0

    def test_to_dict_pending_running_completed(self) -> None:
        p = RunProgress()
        p.start_phase("analyze")
        p.finish_phase(detail="35 pages", cost=0.31, tokens=12000)
        p.start_phase("figma")  # running, not finished
        d = p.to_dict()

        statuses = {phase["name"]: phase["status"] for phase in d["phases"]}
        assert statuses["analyze"] == "completed"
        assert statuses["figma"] == "running"
        assert statuses["match"] == "pending"
        assert statuses["report"] == "pending"

    def test_to_dict_totals(self) -> None:
        p = RunProgress()
        p.start_phase("analyze")
        p.finish_phase(detail="x", cost=0.5, tokens=1000)
        p.start_phase("compare")
        p.finish_phase(detail="y", cost=1.5, tokens=4000)
        d = p.to_dict()
        assert d["total_cost"] == 2.0
        assert d["total_tokens"] == 5000
        assert d["elapsed"] >= 0

    def test_to_dict_includes_all_phases(self) -> None:
        p = RunProgress()
        d = p.to_dict()
        names = {phase["name"] for phase in d["phases"]}
        assert names == set(PHASE_ORDER)

    def test_to_dict_phase_metadata_for_completed(self) -> None:
        p = RunProgress()
        p.start_phase("compare")
        p.finish_phase(detail="42 ecarts", cost=1.20, tokens=8000)
        d = p.to_dict()
        compare_entry = next(ph for ph in d["phases"] if ph["name"] == "compare")
        assert compare_entry["detail"] == "42 ecarts"
        assert compare_entry["cost"] == 1.20
        assert compare_entry["tokens"] == 8000
        assert compare_entry["duration"] is not None

    def test_phase_labels_complete(self) -> None:
        """Every phase in PHASE_ORDER must have a human label."""
        for phase in PHASE_ORDER:
            assert phase in PHASE_LABELS
            assert PHASE_LABELS[phase]


class TestGlobalProgress:
    def test_set_and_get(self) -> None:
        p = RunProgress()
        set_progress(p)
        assert get_progress() is p
        set_progress(None)
        assert get_progress() is None

    @pytest.fixture(autouse=True)
    def _cleanup(self):
        yield
        set_progress(None)
