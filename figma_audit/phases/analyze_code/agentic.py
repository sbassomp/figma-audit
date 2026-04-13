"""Phase 1 agentic mode: Claude explores the codebase via tools.

Opt-in via ``--agentic`` flag, ``analyze_mode: agentic`` in the YAML, or
``FIGMA_AUDIT_ANALYZE_MODE=agentic``. More expensive (~$0.55-1.50) but
produces correct DTOs, auth_required flags, and ``test_setup`` payloads
because the agent reads exactly what it needs instead of guessing from
a 150KB dump.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

import figma_audit.phases.analyze_code as _pkg
from figma_audit.config import Config
from figma_audit.phases.analyze_code.discovery import (
    API_PATTERNS,
    PAGE_PATTERNS,
    ROUTER_PATTERNS,
    TOKEN_PATTERNS,
    _detect_framework,
    _find_files,
)
from figma_audit.phases.analyze_code.one_shot import SYSTEM_PROMPT
from figma_audit.utils.claude_client import ClaudeClient

console = Console()


# ── Agentic mode system prompt ─────────────────────────────────────
# The agent explores the codebase with tools instead of receiving a
# 150KB dump, which avoids hallucinated field names and wrong auth guards.

AGENTIC_SYSTEM_PROMPT = """\
You are a senior software engineer analyzing a codebase to produce a structured manifest \
of all pages/screens for a Figma design audit tool.

You have tools to explore the project directory: read_file, grep_code, list_files. \
Use them to navigate the codebase iteratively — do NOT ask for files to be provided.

## CRITICAL — Token budget management

You have a finite token budget (~800K input tokens). Every file you read and every \
grep you run adds to the cumulative context. Be STRATEGIC:
- Do NOT read page files one by one — read the ROUTER first, it lists ALL routes \
  in a single file. Extract page names, routes, auth guards from it.
- For page files, only read the ones you CANNOT infer from the router (e.g. when \
  you need form_fields or complex capturable_states).
- Use grep_code with NARROW globs (e.g. '**/*_repository.dart') instead of broad \
  searches. Prefer targeted file reads over exploratory greps.
- For design tokens, read ONLY the main theme file, not every variant.
- Call submit_result as soon as you have enough information. Do NOT read every file \
  in the project — aim for 10-15 file reads total, not 30+.

## Process

1. Start by reading the router file(s) listed in the initial message. They define \
   every route in the application and the auth guards. Extract ALL routes and their \
   auth status from this single read — this is your most important file.
2. Only read individual page/screen files when you need details the router doesn't \
   provide (form fields, complex states). Skip pages that are straightforward \
   from the router definition alone.
3. For auth_required: read the router redirect logic (GoRouter `redirect:`, AuthGuard, \
   route observers). A route is auth_required: true if navigating to it while logged-out \
   redirects elsewhere. When in doubt, prefer true.
4. For test_setup: this has TWO SEPARATE parts — do NOT mix them up: \
   \
   (a) AUTH FIELDS (how to log in via API): \
       - auth_endpoint: the endpoint that VERIFIES credentials and returns a token \
         (e.g. /api/auth/verify-otp). This is NOT the OTP request endpoint. \
       - auth_otp_request_endpoint: OPTIONAL, the endpoint that SENDS the OTP \
         (e.g. /api/auth/request-otp). Only include if the auth flow has a separate \
         step for requesting the code before verifying it. \
       - auth_payload: the body sent to auth_endpoint. Use ${test_data.X} templates. \
       - auth_token_path: dotted path to the bearer token in the response. \
   \
   (b) SEED ITEMS (entities to create for capturing parameterized routes): \
       - seed_items is a list of API calls that CREATE test data (e.g. POST /api/items). \
       - Each seed_item has: endpoint, method, payload, id_path, test_data_key. \
       - Do NOT put auth/login endpoints in seed_items — those belong in (a). \
       - seed_items should ONLY contain entity-creation endpoints. \
   \
   For both: grep for the API client / repository classes to find the exact \
   request DTOs. Read the DTO source file to get the SERIALIZED field names \
   (@JsonValue, @JsonProperty, @SerializedName). Use ${now+1d} for future dates. \
   Use ${test_data.X} for credential templates. NEVER guess field names. \
   \
   IMPORTANT: include the full API prefix in endpoints (e.g. /api/exchange/courses, \
   not just /exchange/courses). Read the API client's baseUrl/baseOptions to find \
   the prefix.
5. For navigation_steps: prefer direct URL navigation. Only use click-based steps for \
   modals without a route. For parameterized routes (/:id), use ${test_data.<key>}.
6. For design tokens: read the MAIN theme/token file only. Extract colors, fonts, \
   spacing, radii.
7. When done, call submit_result with the complete manifest JSON. Do not \
   over-explore — completeness matters less than correctness.

## Output schema (the argument to submit_result)

""" + SYSTEM_PROMPT.split("JSON Schema to follow:\n")[1]  # Reuse the JSON schema from one-shot


def _build_agentic_seed_message(
    framework: str,
    project_dir: Path,
    router_paths: list[Path],
    page_paths: list[Path],
    token_paths: list[Path],
    api_paths: list[Path],
) -> str:
    """Build the initial user message for the agentic loop.

    Only sends file PATHS (not contents) so the agent reads them on demand.
    This keeps the initial message small (~2-4KB) vs the one-shot's ~150KB.
    """
    parts: list[str] = []

    parts.append(f"## Framework: {framework}\n")
    parts.append("## Files discovered in the project\n")

    def _list_paths(label: str, paths: list[Path]) -> None:
        parts.append(f"### {label} ({len(paths)} files)")
        for p in paths:
            try:
                rel = p.relative_to(project_dir)
            except ValueError:
                rel = p
            parts.append(f"- {rel}")
        parts.append("")

    _list_paths("Router / Navigation", router_paths)
    _list_paths("Pages / Screens", page_paths)
    _list_paths("Design Tokens / Theme", token_paths)
    _list_paths("API Clients / Services / Repositories", api_paths)

    parts.append(
        "## Instructions\n"
        "Start by reading the router file(s) to discover all routes and auth guards. "
        "Then read each page file to understand what it renders. "
        "For test_setup, find the request DTOs via grep_code and read their source. "
        "When you have the complete manifest, call submit_result."
    )

    return "\n".join(parts)


def _run_agentic(config: Config) -> Path:
    """Run Phase 1 in agentic mode: Claude explores the codebase with tools."""
    from figma_audit.utils.agent_context import AgentContext
    from figma_audit.utils.agent_loop import run_agent_loop
    from figma_audit.utils.agent_tools import READONLY_TOOLS

    project_dir = Path(config.project).expanduser().resolve()
    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "pages_manifest.json"

    framework = _detect_framework(project_dir)
    console.print(f"[bold]Framework detected: {framework}[/bold]")
    if framework == "unknown":
        raise ValueError(
            f"Could not detect framework in {project_dir}. "
            "Supported: flutter, react, vue, angular, nextjs."
        )

    router_paths = _find_files(project_dir, ROUTER_PATTERNS.get(framework, []))
    page_paths = _find_files(project_dir, PAGE_PATTERNS.get(framework, []))
    token_paths = _find_files(project_dir, TOKEN_PATTERNS.get(framework, []))
    api_paths = _find_files(project_dir, API_PATTERNS.get(framework, []))

    console.print(f"  Router files: {len(router_paths)}")
    console.print(f"  Page files:   {len(page_paths)}")
    console.print(f"  Token files:  {len(token_paths)}")
    console.print(f"  API files:    {len(api_paths)}")

    if not router_paths:
        raise FileNotFoundError(
            f"No router files found in {project_dir}. "
            f"Searched patterns: {ROUTER_PATTERNS.get(framework, [])}"
        )

    ctx = AgentContext(project_dir=project_dir, interactive=True)
    initial_msg = _build_agentic_seed_message(
        framework, project_dir, router_paths, page_paths, token_paths, api_paths
    )

    # Model selection: use analyze_model override if set, otherwise default
    model = config.analyze_model or None  # None = ClaudeClient default (Sonnet)
    model_label = model or "sonnet (default)"
    console.print(f"\n[bold]Starting agentic analysis with {model_label}...[/bold]")
    console.print("[dim]Budget: max 30 iterations[/dim]\n")

    # Wire progress updates so the web UI polling shows agent iterations
    from figma_audit.utils.progress import get_progress

    run_progress = get_progress()

    def _on_iteration(iteration: int, tool_name: str, step_label: str) -> None:
        if run_progress:
            run_progress.update(step=step_label, progress=iteration, total=30)

    client = (
        ClaudeClient(api_key=config.anthropic_api_key, model=model)
        if model
        else ClaudeClient(api_key=config.anthropic_api_key)
    )
    result = run_agent_loop(
        client=client,
        system_prompt=AGENTIC_SYSTEM_PROMPT,
        initial_user_message=initial_msg,
        tools=READONLY_TOOLS,
        context=ctx,
        phase="analyze",
        max_iterations=30,
        max_wall_seconds=900.0,
        max_tokens_per_turn=16384,
        max_total_input_tokens=800_000,
        on_iteration=_on_iteration,
    )
    # Expose for cost-tracking by callers via figma_audit.phases.analyze_code._last_client
    _pkg._last_client = client
    client.print_usage()

    manifest_data = result.data
    if not isinstance(manifest_data, dict):
        raise ValueError(f"Agent returned non-dict result: {type(manifest_data)}")

    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2, ensure_ascii=False)

    pages = manifest_data.get("pages", [])
    tokens = manifest_data.get("design_tokens", {})
    console.print(
        f"\n[bold green]Manifest saved to {manifest_path} "
        f"(agentic, {result.iterations} iterations, "
        f"{result.elapsed_seconds:.0f}s)[/bold green]"
    )
    console.print(f"  {len(pages)} pages identified")
    console.print(f"  {len(tokens.get('colors', {}))} color tokens")
    console.print(f"  Framework: {manifest_data.get('framework', '?')}")

    return manifest_path
