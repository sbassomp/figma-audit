"""Tests for the agentic loop runner using a scripted mock client."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from figma_audit.utils.agent_context import AgentContext
from figma_audit.utils.agent_loop import AgentLoopError, run_agent_loop
from figma_audit.utils.agent_tools import READONLY_TOOLS
from figma_audit.utils.claude_client import TokenUsage

# ─── mock SDK objects ────────────────────────────────────────────────


@dataclass
class _MockTextBlock:
    text: str
    type: str = "text"


@dataclass
class _MockToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class _MockUsage:
    input_tokens: int = 100
    output_tokens: int = 50
    cache_read_input_tokens: int = 0


@dataclass
class _MockMessage:
    content: list
    stop_reason: str = "tool_use"
    usage: _MockUsage = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.usage is None:
            self.usage = _MockUsage()


class ScriptedClient:
    """Plays back a fixed sequence of Anthropic Message objects."""

    def __init__(self, scripted_responses: list[_MockMessage]) -> None:
        self._responses = list(scripted_responses)
        self.usage = TokenUsage()
        self.model = "claude-sonnet-4-5-20250929"
        self.calls: list[dict] = []

    def messages_raw(self, **kwargs: Any) -> _MockMessage:  # noqa: D401
        if not self._responses:
            raise RuntimeError("scripted client ran out of responses")
        # Deep-copy messages so subsequent mutations by the loop don't change
        # what we recorded for assertions.
        import copy

        snapshot = dict(kwargs)
        snapshot["messages"] = copy.deepcopy(kwargs.get("messages", []))
        self.calls.append(snapshot)
        response = self._responses.pop(0)
        self.usage.add(response, phase=kwargs.get("phase", ""))
        return response


# ─── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "main.dart").write_text("void main() {}\n")
    return tmp_path


@pytest.fixture
def ctx(project: Path) -> AgentContext:
    return AgentContext(project_dir=project, interactive=False)


# ─── happy paths ─────────────────────────────────────────────────────


class TestAgentLoopHappyPath:
    def test_immediate_submit(self, ctx: AgentContext) -> None:
        """Agent submits immediately without using any other tool."""
        client = ScriptedClient(
            [
                _MockMessage(
                    content=[
                        _MockToolUseBlock(
                            id="t1",
                            name="submit_result",
                            input={"result": {"hello": "world"}},
                        ),
                    ]
                ),
            ]
        )
        result = run_agent_loop(
            client=client,  # type: ignore[arg-type]
            system_prompt="be brief",
            initial_user_message="give me hello world",
            tools=READONLY_TOOLS,
            context=ctx,
            phase="test",
        )
        assert result.data == {"hello": "world"}
        assert result.iterations == 1

    def test_read_then_submit(self, ctx: AgentContext) -> None:
        """Agent reads a file, then submits."""
        client = ScriptedClient(
            [
                _MockMessage(
                    content=[
                        _MockToolUseBlock(
                            id="t1", name="read_file", input={"path": "lib/main.dart"}
                        ),
                    ]
                ),
                _MockMessage(
                    content=[
                        _MockToolUseBlock(
                            id="t2",
                            name="submit_result",
                            input={"result": {"main_found": True}},
                        ),
                    ]
                ),
            ]
        )
        result = run_agent_loop(
            client=client,  # type: ignore[arg-type]
            system_prompt="find main",
            initial_user_message="look at lib/main.dart and tell me",
            tools=READONLY_TOOLS,
            context=ctx,
            phase="test",
        )
        assert result.data == {"main_found": True}
        assert result.iterations == 2
        # Second call should have history with the read_file tool result
        assert len(client.calls) == 2
        history = client.calls[1]["messages"]
        # user, assistant, user (with tool results)
        assert len(history) == 3
        assert history[2]["role"] == "user"
        assert history[2]["content"][0]["type"] == "tool_result"

    def test_multiple_tool_uses_in_one_turn(self, ctx: AgentContext) -> None:
        """If the model emits 2 tool_use blocks in one turn, both run."""
        client = ScriptedClient(
            [
                _MockMessage(
                    content=[
                        _MockToolUseBlock(
                            id="t1", name="read_file", input={"path": "lib/main.dart"}
                        ),
                        _MockToolUseBlock(id="t2", name="list_files", input={"directory": "lib"}),
                    ]
                ),
                _MockMessage(
                    content=[
                        _MockToolUseBlock(
                            id="t3", name="submit_result", input={"result": {"ok": 1}}
                        ),
                    ]
                ),
            ]
        )
        result = run_agent_loop(
            client=client,  # type: ignore[arg-type]
            system_prompt="x",
            initial_user_message="x",
            tools=READONLY_TOOLS,
            context=ctx,
            phase="test",
        )
        # Both results should appear in the next user message
        history = client.calls[1]["messages"]
        tool_results = history[2]["content"]
        assert len(tool_results) == 2
        assert {tr["tool_use_id"] for tr in tool_results} == {"t1", "t2"}
        assert result.data == {"ok": 1}


# ─── error paths ─────────────────────────────────────────────────────


class TestAgentLoopErrors:
    def test_iteration_cap(self, ctx: AgentContext) -> None:
        """Loop with no submit_result terminates at max_iterations."""
        # Always emit a read_file call, never submit
        looping_response = _MockMessage(
            content=[
                _MockToolUseBlock(id="t", name="read_file", input={"path": "lib/main.dart"}),
            ]
        )
        client = ScriptedClient([looping_response] * 50)
        with pytest.raises(AgentLoopError, match="iteration cap"):
            run_agent_loop(
                client=client,  # type: ignore[arg-type]
                system_prompt="x",
                initial_user_message="x",
                tools=READONLY_TOOLS,
                context=ctx,
                phase="test",
                max_iterations=5,
            )

    def test_end_turn_without_submit(self, ctx: AgentContext) -> None:
        """End_turn with no tool calls is an error."""
        client = ScriptedClient(
            [
                _MockMessage(
                    content=[_MockTextBlock(text="I give up")],
                    stop_reason="end_turn",
                ),
            ]
        )
        with pytest.raises(AgentLoopError, match="without calling submit_result"):
            run_agent_loop(
                client=client,  # type: ignore[arg-type]
                system_prompt="x",
                initial_user_message="x",
                tools=READONLY_TOOLS,
                context=ctx,
                phase="test",
            )

    def test_unknown_tool_does_not_crash(self, ctx: AgentContext) -> None:
        """Unknown tool returns an error result; loop continues."""
        client = ScriptedClient(
            [
                _MockMessage(
                    content=[
                        _MockToolUseBlock(id="t1", name="nonexistent_tool", input={}),
                    ]
                ),
                _MockMessage(
                    content=[
                        _MockToolUseBlock(
                            id="t2", name="submit_result", input={"result": {"k": "v"}}
                        ),
                    ]
                ),
            ]
        )
        result = run_agent_loop(
            client=client,  # type: ignore[arg-type]
            system_prompt="x",
            initial_user_message="x",
            tools=READONLY_TOOLS,
            context=ctx,
            phase="test",
        )
        assert result.data == {"k": "v"}
        # Next turn should have received the error tool_result
        tool_results = client.calls[1]["messages"][2]["content"]
        assert tool_results[0].get("is_error") is True
        assert "unknown tool" in tool_results[0]["content"]

    def test_tool_crash_is_caught(self, ctx: AgentContext) -> None:
        """If a tool raises, the loop must NOT crash; it returns an error result."""
        from figma_audit.utils.agent_tools import Tool

        def _crashing(_params: dict, _ctx: AgentContext) -> dict:
            raise RuntimeError("boom")

        crashing = Tool(
            name="crash",
            description="always crashes",
            input_schema={"type": "object", "properties": {}},
            run=_crashing,
        )

        client = ScriptedClient(
            [
                _MockMessage(
                    content=[
                        _MockToolUseBlock(id="t1", name="crash", input={}),
                    ]
                ),
                _MockMessage(
                    content=[
                        _MockToolUseBlock(
                            id="t2", name="submit_result", input={"result": {"x": 1}}
                        ),
                    ]
                ),
            ]
        )
        from figma_audit.utils.agent_tools import SUBMIT_RESULT

        result = run_agent_loop(
            client=client,  # type: ignore[arg-type]
            system_prompt="x",
            initial_user_message="x",
            tools=[crashing, SUBMIT_RESULT],
            context=ctx,
            phase="test",
        )
        assert result.data == {"x": 1}
        crash_result = client.calls[1]["messages"][2]["content"][0]["content"]
        assert "crashed" in crash_result

    def test_requires_submit_result_tool(self, ctx: AgentContext) -> None:
        """Loop refuses to start without a submit_result tool in the list."""
        from figma_audit.utils.agent_tools import READ_FILE

        with pytest.raises(ValueError, match="submit_result"):
            run_agent_loop(
                client=ScriptedClient([]),  # type: ignore[arg-type]
                system_prompt="x",
                initial_user_message="x",
                tools=[READ_FILE],  # missing submit_result
                context=ctx,
                phase="test",
            )
