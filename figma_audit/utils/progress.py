"""Run progress tracking for CLI and web UI."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from rich.console import Console

console = Console()

PHASE_LABELS = {
    "analyze": "Analyze code",
    "figma": "Export Figma",
    "match": "Match screens",
    "capture": "Capture app",
    "compare": "Compare",
    "report": "Report",
}
PHASE_ORDER = ["analyze", "figma", "match", "capture", "compare", "report"]


@dataclass
class PhaseResult:
    name: str
    duration: float = 0
    detail: str = ""
    cost: float = 0
    tokens: int = 0


@dataclass
class RunProgress:
    """Track progress across the entire pipeline."""

    phases: list[str] = field(default_factory=lambda: list(PHASE_ORDER))
    current_phase: str = ""
    current_step: str = ""
    current_progress: int = 0
    current_total: int = 0
    phase_results: list[PhaseResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    _phase_start: float = 0

    def start_phase(self, phase: str) -> None:
        self.current_phase = phase
        self.current_step = ""
        self.current_progress = 0
        self.current_total = 0
        self._phase_start = time.time()
        idx = self.phases.index(phase) + 1 if phase in self.phases else "?"
        label = PHASE_LABELS.get(phase, phase)
        console.print(f"\n[bold][{idx}/{len(self.phases)}] {label}[/bold]")

    def update(self, step: str = "", progress: int = 0, total: int = 0) -> None:
        if step:
            self.current_step = step
        if total:
            self.current_total = total
        self.current_progress = progress

    def finish_phase(self, detail: str = "", cost: float = 0, tokens: int = 0) -> None:
        duration = time.time() - self._phase_start
        result = PhaseResult(
            name=self.current_phase,
            duration=duration,
            detail=detail,
            cost=cost,
            tokens=tokens,
        )
        self.phase_results.append(result)

        duration_str = _format_duration(duration)
        parts = [f"[dim]{duration_str}[/dim]"]
        if detail:
            parts.append(f"[dim]{detail}[/dim]")
        if cost > 0:
            parts.append(f"[dim]~${cost:.3f}[/dim]")
        console.print(f"  {'  '.join(parts)}")

    def print_summary(self) -> None:
        total_duration = time.time() - self.started_at
        total_tokens = sum(r.tokens for r in self.phase_results)
        total_cost = sum(r.cost for r in self.phase_results)

        console.print(f"\n{'=' * 60}")
        console.print("[bold]Run summary[/bold]")
        console.print(f"{'=' * 60}")

        for r in self.phase_results:
            idx = self.phases.index(r.name) + 1 if r.name in self.phases else "?"
            label = PHASE_LABELS.get(r.name, r.name)
            dur = _format_duration(r.duration)
            cost_str = f"~${r.cost:.3f}" if r.cost > 0 else ""
            detail = r.detail or ""
            console.print(
                f"  [{idx}/{len(self.phases)}] {label:20s} {dur:>8s}  {detail:20s}  {cost_str}"
            )

        console.print(f"\n  [bold]Total: {_format_duration(total_duration)}[/bold]", end="")
        if total_tokens:
            console.print(f" | {total_tokens:,} tokens", end="")
        if total_cost > 0:
            console.print(f" | [bold]~${total_cost:.2f}[/bold]", end="")
        console.print()

    def to_dict(self) -> dict:
        """Serialize for web UI / API."""
        total_cost = sum(r.cost for r in self.phase_results)
        total_tokens = sum(r.tokens for r in self.phase_results)
        return {
            "current_phase": self.current_phase,
            "current_step": self.current_step,
            "current_progress": self.current_progress,
            "current_total": self.current_total,
            "elapsed": time.time() - self.started_at,
            "total_cost": total_cost,
            "total_tokens": total_tokens,
            "phases": [
                {
                    "name": p,
                    "label": PHASE_LABELS.get(p, p),
                    "status": (
                        "completed"
                        if any(r.name == p for r in self.phase_results)
                        else "running"
                        if p == self.current_phase
                        else "pending"
                    ),
                    "duration": next((r.duration for r in self.phase_results if r.name == p), None),
                    "detail": next((r.detail for r in self.phase_results if r.name == p), None),
                    "cost": next((r.cost for r in self.phase_results if r.name == p), None),
                    "tokens": next(
                        (r.tokens for r in self.phase_results if r.name == p), None
                    ),
                }
                for p in self.phases
            ],
        }


# Global progress instance (set by the run command, read by phases)
_current_progress: RunProgress | None = None


def get_progress() -> RunProgress | None:
    return _current_progress


def set_progress(p: RunProgress | None) -> None:
    global _current_progress
    _current_progress = p


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs:02d}s"
