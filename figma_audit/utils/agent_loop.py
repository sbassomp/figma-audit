"""Generic agentic loop runner.

The runner is provider-aware (uses ClaudeClient.messages_raw) but tool-agnostic:
it dispatches whatever Tool objects you pass in. Termination is explicit via
the special `submit_result` tool — there is no implicit "stop on first
end_turn", because in tool-use mode the model often emits an end_turn after
calling a tool to wait for the result and we must keep going.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console

from figma_audit.utils.agent_context import AgentContext
from figma_audit.utils.agent_tools import (
    Tool,
    find_tool,
    format_tool_result,
    serialize_tools,
)
from figma_audit.utils.claude_client import ClaudeClient

console = Console()


class AgentLoopError(RuntimeError):
    """Raised when the agentic loop terminates without a successful result."""


@dataclass
class AgentResult:
    """The outcome of a successful agent run."""

    data: Any
    """The payload submitted via the `submit_result` tool."""

    iterations: int
    """How many model turns the loop took."""

    elapsed_seconds: float
    """Total wall time the loop took."""

    usage_snapshot: dict = field(default_factory=dict)
    """Snapshot of TokenUsage totals for this loop, for cost reporting."""


def _content_blocks_to_dicts(content) -> list[dict]:
    """Convert SDK content blocks to plain dicts the next request can echo back."""
    out: list[dict] = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text":
            out.append({"type": "text", "text": block.text})
        elif btype == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
        # Ignore other block types (e.g. images) — agent shouldn't emit them
    return out


def run_agent_loop(
    *,
    client: ClaudeClient,
    system_prompt: str,
    initial_user_message: str,
    tools: list[Tool],
    context: AgentContext,
    phase: str,
    max_iterations: int = 25,
    max_wall_seconds: float = 600.0,
    max_tokens_per_turn: int = 4096,
    max_total_input_tokens: int = 400_000,
    on_iteration: Callable[[int, str, str], None] | None = None,
) -> AgentResult:
    """Run an agentic conversation until the model calls submit_result.

    Args:
        client: ClaudeClient instance (token usage accumulates on it)
        system_prompt: The system prompt for the agent. Cached on Anthropic
            side via messages_raw(cache_system=True).
        initial_user_message: First user message kicking off the loop.
        tools: Tools the agent may call. Must include a `submit_result` tool
            (otherwise the loop has no termination signal and will hit max_iterations).
        context: AgentContext with sandbox roots, used by every tool invocation.
        phase: phase label for token accounting.
        max_iterations: Hard cap on model turns.
        max_wall_seconds: Hard cap on wall time.
        max_tokens_per_turn: max_tokens passed to messages.create per call.
        max_total_input_tokens: Cumulative input-token budget for this loop.

    Returns:
        AgentResult with the submitted payload.

    Raises:
        AgentLoopError: on iteration cap, wall timeout, token budget, or
            unrecoverable model behavior (end_turn without submit_result).
    """
    if not any(t.name == "submit_result" for t in tools):
        raise ValueError("tools list must include a submit_result tool")

    serialized_tools = serialize_tools(tools)
    messages: list[dict] = [
        {"role": "user", "content": initial_user_message},
    ]

    start_input_tokens = client.usage.input_tokens
    start = time.monotonic()

    for iteration in range(1, max_iterations + 1):
        # Wall budget
        elapsed = time.monotonic() - start
        if elapsed > max_wall_seconds:
            raise AgentLoopError(
                f"agent loop wall timeout after {elapsed:.0f}s "
                f"(limit {max_wall_seconds:.0f}s)"
            )
        # Token budget
        loop_input_so_far = client.usage.input_tokens - start_input_tokens
        if loop_input_so_far > max_total_input_tokens:
            raise AgentLoopError(
                f"agent loop token budget exceeded: {loop_input_so_far:,} "
                f"input tokens (limit {max_total_input_tokens:,})"
            )

        response = client.messages_raw(
            system=system_prompt,
            messages=messages,
            tools=serialized_tools,
            max_tokens=max_tokens_per_turn,
            temperature=0.0,
            phase=phase,
        )

        # Append the assistant turn to the conversation history
        assistant_content = _content_blocks_to_dicts(response.content)
        messages.append({"role": "assistant", "content": assistant_content})

        # Collect tool_use blocks; if any is submit_result, that's our exit.
        tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]

        if not tool_uses:
            # Model emitted no tool calls — likely end_turn with text only.
            stop_reason = getattr(response, "stop_reason", "?")
            raise AgentLoopError(
                f"agent stopped without calling submit_result "
                f"(iteration {iteration}, stop_reason={stop_reason})"
            )

        tool_results: list[dict] = []
        submitted_payload: Any = None
        for use in tool_uses:
            tool_name = use.name
            tool_input = use.input or {}
            tool_id = use.id

            if tool_name == "submit_result":
                submitted_payload = tool_input.get("result")
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": "received",
                    }
                )
                continue

            tool = find_tool(tools, tool_name)
            if tool is None:
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": json.dumps({"error": f"unknown tool: {tool_name}"}),
                        "is_error": True,
                    }
                )
                continue

            try:
                value = tool.run(tool_input, context)
            except Exception as e:  # noqa: BLE001 - tools must never crash the loop
                value = {"error": f"tool {tool_name} crashed: {type(e).__name__}: {e}"}

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": format_tool_result(value),
                }
            )

            # One-line iteration log + optional progress callback
            short_input = json.dumps(tool_input, ensure_ascii=False)[:80]
            step_label = f"iter {iteration} · {tool_name}({short_input})"
            console.print(f"  [dim]{step_label}[/dim]")
            if on_iteration:
                on_iteration(iteration, tool_name, step_label)

        # If submit_result was called this turn, we're done.
        if submitted_payload is not None:
            return AgentResult(
                data=submitted_payload,
                iterations=iteration,
                elapsed_seconds=time.monotonic() - start,
                usage_snapshot={
                    "input_tokens": client.usage.input_tokens - start_input_tokens,
                    "calls_in_loop": iteration,
                },
            )

        messages.append({"role": "user", "content": tool_results})

    raise AgentLoopError(
        f"agent loop hit iteration cap ({max_iterations}) without submit_result"
    )
