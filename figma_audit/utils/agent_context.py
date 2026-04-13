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
        tokens: Map of role name → bearer token. Populated by the caller
            with one entry per declared account (``{"seller": "...", "buyer": "..."}``).
            :class:`http_request` picks the token based on the ``as`` param
            and falls back to :attr:`default_role` when omitted.
        default_role: Name of the role used when a tool call does not specify
            ``as``. Set automatically to the sole token's role if only one is
            registered, or to ``"default"`` when the legacy ``auth_token``
            shortcut is used.
        auth_token: **Legacy** single-token shortcut. When provided, it is
            merged into ``tokens`` under role ``"default"`` at construction
            time. New callers should populate :attr:`tokens` directly and set
            :attr:`default_role` explicitly.
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
    tokens: dict[str, str] = field(default_factory=dict)
    default_role: str | None = None
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

        # Legacy shortcut: a single ``auth_token`` becomes the ``default`` role.
        if self.auth_token and "default" not in self.tokens:
            self.tokens["default"] = self.auth_token
            if self.default_role is None:
                self.default_role = "default"

        # Convenience: if exactly one token is registered and the caller
        # did not pick a default_role, that token is the default.
        if self.default_role is None and len(self.tokens) == 1:
            self.default_role = next(iter(self.tokens))

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

    def token_for(self, role: str | None) -> tuple[str | None, str | None]:
        """Look up the bearer token for a role name.

        Returns ``(token, resolved_role)``. When ``role`` is None the
        :attr:`default_role` is used. Returns ``(None, None)`` if neither a
        role nor a default is available, and ``(None, role)`` when the role
        exists conceptually but has no registered token.
        """
        resolved = role or self.default_role
        if resolved is None:
            return None, None
        return self.tokens.get(resolved), resolved
