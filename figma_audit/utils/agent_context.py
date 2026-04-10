"""Sandbox + state passed to every agent tool invocation.

The AgentContext is the security boundary for the agentic loop: it pins the
filesystem root the agent is allowed to read and the HTTP target it is allowed
to call. Tools must NEVER access anything outside what is configured here.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console


@dataclass
class AgentContext:
    """Per-invocation sandbox + runtime state.

    Attributes:
        project_dir: Root directory the agent's filesystem tools (read_file,
            grep_code, list_files) are allowed to access. Any path resolving
            outside this directory is rejected by the tool implementation.
        app_url: Optional base URL for http_request. None means HTTP tools
            should refuse to operate.
        auth_token: Optional bearer token automatically injected into
            http_request calls. Never logged.
        interactive: Whether ask_user can actually prompt the human via stdin.
            Defaults to True only if stdin is a TTY.
        max_file_bytes: Hard cap on a single read_file call (default 50KB).
        max_grep_hits: Hard cap on grep results (default 200).
        max_list_entries: Hard cap on list_files results (default 200).
        console: rich Console for one-line iteration logs.

    State carried across tool calls (used by anti-loop heuristics):
        _http_seen: hashes of (method, path, body) already attempted; the third
            identical call is rejected synthetically.
        _http_count: total number of http_request calls so far (capped).
        _ask_history: last few ask_user questions, to reject begging loops.
    """

    project_dir: Path
    app_url: str | None = None
    auth_token: str | None = None
    interactive: bool = field(default_factory=lambda: sys.stdin.isatty())
    max_file_bytes: int = 50_000
    max_grep_hits: int = 200
    max_list_entries: int = 200
    max_http_calls: int = 40
    console: Console = field(default_factory=Console)

    # Anti-loop state — managed by tool implementations, not user-facing.
    _http_seen: dict[str, int] = field(default_factory=dict)
    _http_count: int = 0
    _ask_history: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Resolve project_dir once so every sandbox check is symlink-safe.
        self.project_dir = Path(self.project_dir).expanduser().resolve()
        if not self.project_dir.is_dir():
            raise ValueError(f"project_dir is not a directory: {self.project_dir}")

    def is_inside_sandbox(self, candidate: Path) -> bool:
        """Return True iff candidate (after symlink resolution) lives under project_dir."""
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        try:
            return resolved.is_relative_to(self.project_dir)
        except ValueError:
            return False
