"""Claude API client wrapper for structured analysis."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
from rich.console import Console

console = Console()

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
MAX_RETRIES = 3
RETRY_BACKOFF = 5

# Pricing per million tokens (Sonnet 4.5)
PRICING = {
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
}
DEFAULT_PRICING = {"input": 3.0, "output": 15.0}


class ClaudeClientError(Exception):
    pass


@dataclass
class TokenUsage:
    """Track cumulative token usage across API calls."""

    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    cache_read_tokens: int = 0
    _by_phase: dict = field(default_factory=dict)

    def add(self, response, phase: str = "") -> None:
        usage = response.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0)
        self.calls += 1
        if phase:
            if phase not in self._by_phase:
                self._by_phase[phase] = {"input": 0, "output": 0, "calls": 0}
            self._by_phase[phase]["input"] += usage.input_tokens
            self._by_phase[phase]["output"] += usage.output_tokens
            self._by_phase[phase]["calls"] += 1

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost(self, model: str = DEFAULT_MODEL) -> float:
        """Estimated cost in USD."""
        pricing = PRICING.get(model, DEFAULT_PRICING)
        return (
            self.input_tokens * pricing["input"] / 1_000_000
            + self.output_tokens * pricing["output"] / 1_000_000
        )

    def summary(self, model: str = DEFAULT_MODEL) -> str:
        """Human-readable usage summary."""
        c = self.cost(model)
        parts = [
            f"{self.calls} appels API",
            f"{self.input_tokens:,} input + {self.output_tokens:,} output"
            f" = {self.total_tokens:,} tokens",
            f"~${c:.3f}",
        ]
        return " | ".join(parts)

    def phase_breakdown(self, model: str = DEFAULT_MODEL) -> str:
        """Per-phase breakdown."""
        lines = []
        pricing = PRICING.get(model, DEFAULT_PRICING)
        for phase, data in self._by_phase.items():
            cost = (
                data["input"] * pricing["input"] / 1_000_000
                + data["output"] * pricing["output"] / 1_000_000
            )
            lines.append(
                f"  {phase:12s} {data['calls']:3d} calls  "
                f"{data['input']:>8,} in + {data['output']:>7,} out  ~${cost:.3f}"
            )
        return "\n".join(lines)


class ClaudeClient:
    """Wrapper around the Anthropic SDK for structured JSON responses."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.usage = TokenUsage()

    def analyze(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        phase: str = "",
    ) -> dict:
        """Send a prompt and expect a JSON response."""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                self.usage.add(response, phase=phase)
                text = response.content[0].text

                text = text.strip()
                if text.startswith("```"):
                    lines = text.split("\n")
                    text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else text

                return json.loads(text)

            except json.JSONDecodeError as e:
                if attempt < MAX_RETRIES:
                    console.print(
                        f"[yellow]JSON parse error (attempt {attempt}): {e}. Retrying...[/yellow]"
                    )
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                raise ClaudeClientError(f"Failed to parse JSON after {MAX_RETRIES} attempts: {e}")

            except anthropic.RateLimitError:
                wait = RETRY_BACKOFF * attempt * 4
                console.print(f"[yellow]Rate limited. Waiting {wait}s...[/yellow]")
                time.sleep(wait)
                continue

            except anthropic.APIError as e:
                if attempt < MAX_RETRIES:
                    console.print(
                        f"[yellow]API error (attempt {attempt}): {e}. Retrying...[/yellow]"
                    )
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                raise ClaudeClientError(f"Claude API error after {MAX_RETRIES} attempts: {e}")

        raise ClaudeClientError(f"Max retries ({MAX_RETRIES}) exceeded")

    def analyze_with_images(
        self,
        system_prompt: str,
        user_prompt: str,
        images: list[Path | str],
        *,
        max_tokens: int = 8192,
        temperature: float = 0.0,
        phase: str = "",
    ) -> dict:
        """Send a prompt with images and expect a JSON response."""
        content: list[dict] = []

        for img in images:
            img_path = Path(img)
            if not img_path.exists():
                console.print(f"[yellow]Image not found: {img_path}[/yellow]")
                continue

            with open(img_path, "rb") as f:
                raw = f.read()
            # Detect actual image type from magic bytes
            if raw[:8] == b"\x89PNG\r\n\x1a\n":
                media_type = "image/png"
            elif raw[:2] == b"\xff\xd8":
                media_type = "image/jpeg"
            elif raw[:4] == b"GIF8":
                media_type = "image/gif"
            else:
                media_type = "image/png"
            data = base64.standard_b64encode(raw).decode("utf-8")

            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data},
                }
            )

        content.append({"type": "text", "text": user_prompt})

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content}],
                )
                self.usage.add(response, phase=phase)
                text = response.content[0].text
                text = text.strip()
                if text.startswith("```"):
                    lines = text.split("\n")
                    text = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else text

                return json.loads(text)

            except json.JSONDecodeError as e:
                if attempt < MAX_RETRIES:
                    console.print(
                        f"[yellow]JSON parse error (attempt {attempt}): {e}. Retrying...[/yellow]"
                    )
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                raise ClaudeClientError(f"Failed to parse JSON after {MAX_RETRIES} attempts: {e}")

            except anthropic.RateLimitError:
                wait = RETRY_BACKOFF * attempt * 4
                console.print(f"[yellow]Rate limited. Waiting {wait}s...[/yellow]")
                time.sleep(wait)
                continue

            except anthropic.APIError as e:
                if attempt < MAX_RETRIES:
                    console.print(
                        f"[yellow]API error (attempt {attempt}): {e}. Retrying...[/yellow]"
                    )
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                raise ClaudeClientError(f"Claude API error after {MAX_RETRIES} attempts: {e}")

        raise ClaudeClientError(f"Max retries ({MAX_RETRIES}) exceeded")

    def messages_raw(
        self,
        *,
        system: str | list[dict],
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        phase: str = "",
        cache_system: bool = True,
    ):
        """Low-level wrapper around messages.create for the agentic loop.

        Unlike `analyze`, this does NOT parse the response as JSON — it returns
        the raw Anthropic Message object so the caller can dispatch tool_use
        blocks. Used by `agent_loop.run_agent_loop`.

        Token usage is recorded against the given phase. If `cache_system` is
        true and `system` is a string, it is wrapped with a `cache_control:
        {type: "ephemeral"}` block so the prompt prefix is cached on the
        Anthropic side, dramatically reducing per-iteration cost on long
        agentic loops where the same system prompt is sent every turn.

        Retries on rate limits and transient API errors with exponential
        backoff (same policy as `analyze`).
        """
        # Wrap a plain string system prompt in a cacheable block.
        if cache_system and isinstance(system, str):
            system_param: list[dict] | str = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_param = system

        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_param,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.client.messages.create(**kwargs)
                self.usage.add(response, phase=phase)
                return response

            except anthropic.RateLimitError:
                wait = RETRY_BACKOFF * attempt * 4
                console.print(f"[yellow]Rate limited. Waiting {wait}s...[/yellow]")
                time.sleep(wait)
                continue

            except anthropic.APIError as e:
                if attempt < MAX_RETRIES:
                    console.print(
                        f"[yellow]API error (attempt {attempt}): {e}. Retrying...[/yellow]"
                    )
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                raise ClaudeClientError(f"Claude API error after {MAX_RETRIES} attempts: {e}")

        raise ClaudeClientError(f"Max retries ({MAX_RETRIES}) exceeded")

    def print_usage(self) -> None:
        """Print token usage summary to console."""
        if self.usage.calls == 0:
            return
        console.print(f"\n[bold]Token usage:[/bold] {self.usage.summary(self.model)}")
        breakdown = self.usage.phase_breakdown(self.model)
        if breakdown:
            console.print(breakdown)
