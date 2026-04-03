"""Claude API client wrapper for structured analysis."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import anthropic
from rich.console import Console

console = Console()

DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
MAX_RETRIES = 3
RETRY_BACKOFF = 5


class ClaudeClientError(Exception):
    pass


class ClaudeClient:
    """Wrapper around the Anthropic SDK for structured JSON responses."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def analyze(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> dict:
        """Send a prompt and expect a JSON response.

        Returns the parsed JSON dict.
        """
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                text = response.content[0].text

                # Extract JSON from response (handle markdown code blocks)
                text = text.strip()
                if text.startswith("```"):
                    # Remove ```json ... ``` wrapper
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
    ) -> dict:
        """Send a prompt with images and expect a JSON response."""
        content: list[dict] = []

        for img in images:
            img_path = Path(img)
            if not img_path.exists():
                console.print(f"[yellow]Image not found: {img_path}[/yellow]")
                continue

            media_type = "image/png" if img_path.suffix == ".png" else "image/jpeg"
            with open(img_path, "rb") as f:
                data = base64.standard_b64encode(f.read()).decode("utf-8")

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
