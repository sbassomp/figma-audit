"""Phase 1: Analyze Code — Detect framework, extract routes, produce pages manifest."""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

from figma_audit.config import Config
from figma_audit.utils.claude_client import ClaudeClient

console = Console()

# Exposed after run() for cost tracking by callers
_last_client: ClaudeClient | None = None

# Framework detection markers
FRAMEWORK_MARKERS = {
    "flutter": ["pubspec.yaml"],
    "react": ["package.json"],
    "vue": ["package.json"],
    "angular": ["package.json", "angular.json"],
    "nextjs": ["package.json", "next.config.js", "next.config.mjs", "next.config.ts"],
}

# Patterns to find router files per framework
ROUTER_PATTERNS = {
    "flutter": [
        "**/router/**/*.dart",
        "**/routing/**/*.dart",
        "**/routes/**/*.dart",
        "**/app_router.dart",
        "**/router.dart",
        "**/routes.dart",
        # Auth guards / redirect callbacks live alongside or near the router
        "**/auth_guard*.dart",
        "**/auth_redirect*.dart",
        "**/route_guard*.dart",
        "**/router_guard*.dart",
        "**/auth_notifier*.dart",
        "**/auth_state*.dart",
    ],
    "react": [
        "**/routes/**/*.{ts,tsx,js,jsx}",
        "**/router/**/*.{ts,tsx,js,jsx}",
        "**/App.{tsx,jsx}",
    ],
}

# Patterns to find page/screen files per framework
PAGE_PATTERNS = {
    "flutter": [
        "**/pages/**/*.dart",
        "**/screens/**/*.dart",
        "**/views/**/*.dart",
    ],
    "react": [
        "**/pages/**/*.{ts,tsx,js,jsx}",
        "**/views/**/*.{ts,tsx,js,jsx}",
        "**/screens/**/*.{ts,tsx,js,jsx}",
    ],
}

# Patterns to find API client / service / repository files
API_PATTERNS = {
    "flutter": [
        "**/api/**/*.dart",
        "**/service/**/*.dart",
        "**/services/**/*.dart",
        "**/repository/**/*.dart",
        "**/repositories/**/*.dart",
        "**/data/**/*_repository.dart",
        "**/data/**/*_service.dart",
        "**/data/**/*_client.dart",
        "**/data/remote/**/*.dart",
        "**/data/datasource/**/*.dart",
        "**/client/**/*.dart",
    ],
    "react": [
        "**/api/**/*.{ts,tsx,js,jsx}",
        "**/services/**/*.{ts,tsx,js,jsx}",
        "**/hooks/use*Api*.{ts,tsx}",
        "**/lib/api*.{ts,js}",
    ],
}

# Patterns to find design token files
TOKEN_PATTERNS = {
    "flutter": [
        "**/theme/**/*.dart",
        "**/tokens/**/*.dart",
        "**/design_tokens*.dart",
        "**/colors*.dart",
    ],
    "react": [
        "**/theme/**/*.{ts,js,css}",
        "**/tokens/**/*.{ts,js,css}",
        "**/tailwind.config.*",
    ],
}

# Max file size to include in the prompt (characters)
MAX_FILE_SIZE = 50_000
# Max total characters to send in the prompt
MAX_TOTAL_PROMPT_SIZE = 150_000


def _detect_framework(project_dir: Path) -> str:
    """Detect the project's framework from marker files."""
    if (project_dir / "pubspec.yaml").exists():
        return "flutter"

    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        content = pkg_json.read_text()
        if '"next"' in content or '"next/router"' in content:
            return "nextjs"
        if '"vue"' in content:
            return "vue"
        if '"@angular/core"' in content:
            return "angular"
        if '"react"' in content:
            return "react"

    return "unknown"


def _find_files(project_dir: Path, patterns: list[str]) -> list[Path]:
    """Find files matching glob patterns, sorted by path."""
    files: set[Path] = set()
    for pattern in patterns:
        files.update(project_dir.glob(pattern))
    # Exclude generated files, tests, build artifacts
    filtered = [
        f
        for f in files
        if not any(
            part in f.parts
            for part in (".dart_tool", "build", "node_modules", ".g.dart", "test", "generated")
        )
        and not f.name.endswith(".g.dart")
        and not f.name.endswith(".freezed.dart")
    ]
    return sorted(filtered)


def _read_file_safe(path: Path, max_size: int = MAX_FILE_SIZE) -> str | None:
    """Read a file, returning None if too large or unreadable."""
    try:
        content = path.read_text(encoding="utf-8")
        if len(content) > max_size:
            return content[:max_size] + f"\n\n[... truncated at {max_size} chars ...]"
        return content
    except (OSError, UnicodeDecodeError):
        return None


def _build_prompt(
    framework: str,
    router_files: dict[str, str],
    page_files: dict[str, str],
    token_files: dict[str, str],
    api_files: dict[str, str],
    project_dir: Path,
) -> str:
    """Build the user prompt with all source files."""
    sections: list[str] = []
    lang = "dart" if framework == "flutter" else "typescript"

    sections.append(f"## Framework: {framework}\n")

    # Router files (most important)
    sections.append("## Router / Navigation Files\n")
    for rel_path, content in router_files.items():
        sections.append(f"### {rel_path}\n```{lang}\n{content}\n```\n")

    # API / service files (for test_setup generation)
    if api_files:
        sections.append("## API Client / Service Files\n")
        sections.append(
            "(Use these to generate accurate test_setup with real endpoints and payloads)\n"
        )
        total_size = sum(len(s) for s in sections)
        for rel_path, content in api_files.items():
            entry = f"### {rel_path}\n```{lang}\n{content}\n```\n"
            if total_size + len(entry) > MAX_TOTAL_PROMPT_SIZE // 2:
                break
            sections.append(entry)
            total_size += len(entry)

    # Design token files
    if token_files:
        sections.append("## Design Token Files\n")
        for rel_path, content in token_files.items():
            sections.append(f"### {rel_path}\n```{lang}\n{content}\n```\n")

    # Page files — include as many as we can fit
    total_size = sum(len(s) for s in sections)
    sections.append("## Page / Screen Files\n")
    for rel_path, content in page_files.items():
        entry = f"### {rel_path}\n```dart\n{content}\n```\n"
        if total_size + len(entry) > MAX_TOTAL_PROMPT_SIZE:
            included = len([s for s in sections if s.startswith("### ")])
            remaining = len(page_files) - included
            sections.append(
                f"\n[Remaining {remaining} page files omitted for size. Listed paths only:]\n"
            )
            for rp in page_files:
                sections.append(f"- {rp}\n")
            break
        sections.append(entry)
        total_size += len(entry)

    return "\n".join(sections)


SYSTEM_PROMPT = """\
You are a senior software engineer analyzing a codebase to produce a structured manifest \
of all pages/screens for a Figma design audit tool.

Your task: analyze the provided router, page files, and design tokens to produce a complete \
JSON manifest describing every navigable page in the application.

Rules:
- Output ONLY valid JSON, no markdown, no commentary.
- Temperature 0: be precise and factual, only include what the code explicitly shows.
- For navigation_steps: describe the Playwright actions needed to reach \
each page from the app root. \
**DEFAULT STRATEGY: direct URL navigation.** \
Modern SPA/Flutter-web apps support deep links on EVERY route — clicking through \
the UI is slower, brittle, and often fails on Flutter CanvasKit where DOM events \
are unreliable. Prefer a single `navigate` step to the full route whenever possible. \
\
Rules for picking navigation style: \
\
(1) Route has NO path parameters: generate EXACTLY ONE step: \
`[{"action": "navigate", "url": "<the_route>"}]`. \
Do NOT click tabs, do NOT click menu items, do NOT go via a list page. The app \
router resolves the deep link directly. \
\
(2) Route has ONE OR MORE path parameters (anything with `:param` in the path \
pattern from the router): substitute each `:param` with a `${test_data.<key>}` \
template that will be filled at capture time. \
Example for a route `/<entity>/:id` → \
`[{"action": "navigate", "url": "/<entity>/${test_data.<entity>_id}"}]`. \
The `test_setup.seed_items` block you generate separately must create real \
entities before capture and expose their IDs via matching `test_data_key` \
entries. Use those keys to build the URL. \
For constant-enum parameters (e.g. a `:type` param whose valid values are a \
fixed enum from the code), use one concrete enum value directly in the URL. \
\
(3) ONLY use UI-click navigation (multi-step) when the route genuinely cannot be \
reached by URL — e.g. the target is a modal/dialog with no route, or the app uses \
opaque encrypted IDs in URLs that test_setup cannot produce. In that narrow case, \
list the clicks: `[{"action": "navigate", "url": "/"}, {"action": "click", \
"text": "Button"}, {"action": "wait", "timeout": 1500}]`. \
\
(4) For capturable_states that depend on data state (e.g. detail page "available" \
vs "taken"): use `test_setup.take_item` to transition a seeded item between states \
via API, then navigate to separate IDs — NOT UI clicks.
- For form_fields: list all user-input fields visible on the page.
- For interactive_states: list distinct visual states \
(loading, empty, populated, error, wizard steps).
- For capturable_states: list ONLY the visual states that can be reached \
sequentially via Playwright browser automation (wizard steps, tab switches). \
Exclude transient states (loading, error, success). \
Each state's delta_steps are INCREMENTAL actions from the PREVIOUS state (not cumulative). \
The first state's delta_steps is empty (page already loaded after navigation_steps). \
States MUST be ordered in the sequence they can be reached. \
Omit capturable_states for pages with only one visual state. \
For wizards: EVERY step must be a capturable_state (not just 2). If a wizard has 5 steps, \
generate 5 capturable_states. \
For pages with tabs: each tab is a capturable_state. \
For registration flows with multiple pages: each page is on a separate route, \
but generate capturable_states for sub-steps within each page (e.g. form empty vs filled). \
IMPORTANT for delta_steps actions: the app may use Flutter CanvasKit which has NO DOM elements. \
Use {"action": "click", "text": "Button Label"} — the automation will try \
accessibility roles (button, link, tab) first, then text match, then coordinates. \
For form fields, use {"action": "fill", "label": "Field Label", "value": "..."} \
which uses accessibility labels. \
Never rely on CSS selectors for Flutter CanvasKit apps.
- For auth_required: this is CRITICAL — getting it wrong causes the audit \
tool to capture the wrong screen (silent redirect to login). Do NOT infer this \
from the page file alone. Determine it by inspecting the ROUTER configuration: \
look for redirect callbacks (GoRouter `redirect:`, Navigator guards, route \
observers, AuthGuard middleware, `requireAuth`, `loggedIn` checks). \
A route is `auth_required: true` if AND ONLY IF: \
(a) navigating to it while logged-out is intercepted and redirected elsewhere \
(typically to /signin, /login, /welcome), OR \
(b) the page itself reads user state and redirects on null user. \
A route is `auth_required: false` if it is reachable WITHOUT a session — \
this includes /welcome, /signin, /login, password-reset pages, AND every step \
of a registration flow (the user is still anonymous during registration). \
Conversely, list/detail pages, profile pages, settings, payment pages, and \
anything reading user-scoped data are almost always `auth_required: true`. \
When in doubt because the router code is ambiguous, prefer `auth_required: true` \
for any route that is NOT explicitly listed as a public route in the router.
- For test_data: suggest realistic test values for forms \
(French context: phone +33..., French addresses).
- Extract design tokens from the theme/token files into a structured format.
- For test_setup: analyze the API client/service files to find the EXACT endpoints, \
HTTP methods, request payloads, and authentication flow used by the app. \
Include auth_endpoint (the login/verify endpoint), auth_payload (with ${test_data.key} \
templates for credentials), auth_otp_request_endpoint (if the auth flow has a separate \
OTP request step), auth_token_path (dotted path to the token in the response), \
seed_items (API calls to create test data, with exact endpoint paths and payload structure \
from the code), take_item (to transition an item to a different state), and cleanup_endpoint. \
CRITICAL: use the real endpoints and payload field names from the API client code — do NOT guess.

JSON Schema to follow:
{
  "framework": "flutter|react|vue|angular|nextjs",
  "renderer": "canvaskit|html|dom",
  "pages": [
    {
      "id": "string (snake_case unique identifier)",
      "route": "string (URL path pattern)",
      "name": "string (class/component name)",
      "file": "string (relative file path)",
      "auth_required": "boolean",
      "description": "string (what the page does, in French)",
      "params": [{"name": "string", "type": "string", "optional": "boolean"}],
      "required_state": {
        "description": "string (what state/data is needed)",
        "data_dependencies": ["string"]
      },
      "navigation_steps": [
        {"action": "navigate|click|fill|wait|screenshot", "url?": "string", \
"selector?": "string", "value?": "string", "name?": "string", "timeout?": "number"}
      ],
      "form_fields": [
        {"name": "string", "type": "text|tel|email|address|datetime|select|checkbox|number", \
"step?": "number"}
      ],
      "interactive_states": ["string"],
      "capturable_states": [
        {
          "state_id": "string (snake_case, matches an interactive_states entry)",
          "description": "string (what is visible in this state, in French)",
          "delta_steps": [
            {"action": "navigate|click|fill|wait|wait_for_url", "url?": "string", \
"selector?": "string", "text?": "string", "value?": "string", "timeout?": "number"}
          ]
        }
      ]
    }
  ],
  "design_tokens": {
    "source_file": "string",
    "colors": {"token_name": "#hexvalue"},
    "fonts": {"family": "string", "weights": [400, 500, 600, 700]},
    "spacing_scale": [4, 8, 12, 16, ...],
    "border_radius": {"sm": 4, "md": 8, "lg": 12, "xl": 16}
  },
  "test_data": {
    "phone": "+33612345678",
    "otp": "1234",
    "email": "test@example.com"
  },
  "test_setup": {
    "description": "API calls to create test data before capture. Optional.",
    "auth_endpoint": "/api/public/auth/login",
    "auth_payload": {"email": "${test_data.email}", "code": "${test_data.otp}"},
    "auth_token_path": "accessToken",
    "seed_items": [
      {
        "endpoint": "/api/items",
        "method": "POST",
        "payload": {"name": "Test item", "status": "available"},
        "id_path": "id",
        "test_data_key": "item_id"
      }
    ],
    "take_item": {
      "endpoint": "/api/items/${item_id}/take",
      "method": "POST",
      "test_data_key": "item_taken_id"
    },
    "cleanup_endpoint": "/api/items/${item_id}/archive"
  }
}
"""


# ── Agentic mode system prompt ─────────────────────────────────────
# Used when analyze_mode == "agentic" (opt-in via --agentic or YAML).
# The agent explores the codebase with tools instead of receiving a
# 150KB dump, which avoids hallucinated field names and wrong auth guards.

AGENTIC_SYSTEM_PROMPT = """\
You are a senior software engineer analyzing a codebase to produce a structured manifest \
of all pages/screens for a Figma design audit tool.

You have tools to explore the project directory: read_file, grep_code, list_files. \
Use them to navigate the codebase iteratively — do NOT ask for files to be provided.

## Process

1. Start by reading the router file(s) listed in the initial message. They define every \
   route in the application and the auth guards.
2. For each route you discover, read the corresponding page/screen file to understand: \
   what it renders, what parameters it takes, what state it needs.
3. For auth_required: read the router redirect logic (GoRouter `redirect:`, AuthGuard, \
   route observers). A route is auth_required: true if navigating to it while logged-out \
   redirects elsewhere. When in doubt, prefer true.
4. For test_setup: grep for the API client / repository classes to find the exact \
   request DTOs. Read the DTO source file to get the SERIALIZED field names \
   (@JsonValue, @JsonProperty, @SerializedName). Use ${now+1d} for future dates. \
   Use ${test_data.X} for credential templates. NEVER guess field names.
5. For navigation_steps: prefer direct URL navigation. Only use click-based steps for \
   modals without a route. For parameterized routes (/:id), use ${test_data.<key>}.
6. For design tokens: read theme/token files to extract colors, fonts, spacing, radii.
7. When done, call submit_result with the complete manifest JSON.

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

    console.print("\n[bold]Starting agentic analysis...[/bold]")
    console.print("[dim]Budget: max 30 iterations, ~$0.50-1.50 expected[/dim]\n")

    # Wire progress updates so the web UI polling shows agent iterations
    from figma_audit.utils.progress import get_progress

    run_progress = get_progress()

    def _on_iteration(iteration: int, tool_name: str, step_label: str) -> None:
        if run_progress:
            run_progress.update(step=step_label, progress=iteration, total=30)

    global _last_client
    client = ClaudeClient(api_key=config.anthropic_api_key)
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
        on_iteration=_on_iteration,
    )
    _last_client = client
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


def _run_one_shot(config: Config) -> Path:
    """Run Phase 1 in one-shot mode (original behavior)."""
    project_dir = Path(config.project).expanduser().resolve()
    if not project_dir.exists():
        raise FileNotFoundError(f"Project directory not found: {project_dir}")

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

    def _read_files(paths: list[Path]) -> dict[str, str]:
        result = {}
        for p in paths:
            content = _read_file_safe(p)
            if content:
                rel = str(p.relative_to(project_dir))
                result[rel] = content
        return result

    router_files = _read_files(router_paths)
    page_files = _read_files(page_paths)
    token_files = _read_files(token_paths)
    api_files = _read_files(api_paths)

    total_chars = (
        sum(len(v) for v in router_files.values())
        + sum(len(v) for v in page_files.values())
        + sum(len(v) for v in token_files.values())
        + sum(len(v) for v in api_files.values())
    )
    console.print(f"  Total source: {total_chars:,} chars")

    user_prompt = _build_prompt(
        framework, router_files, page_files, token_files, api_files, project_dir
    )
    console.print(f"  Prompt size: {len(user_prompt):,} chars")
    console.print("[bold]Sending to Claude for analysis (one-shot)...[/bold]")

    global _last_client
    client = ClaudeClient(api_key=config.anthropic_api_key)
    manifest_data = client.analyze(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=16384,
        phase="analyze",
    )
    _last_client = client
    client.print_usage()

    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2, ensure_ascii=False)

    pages = manifest_data.get("pages", [])
    tokens = manifest_data.get("design_tokens", {})
    console.print(f"\n[bold green]Manifest saved to {manifest_path}[/bold green]")
    console.print(f"  {len(pages)} pages identified")
    console.print(f"  {len(tokens.get('colors', {}))} color tokens")
    console.print(f"  Framework: {manifest_data.get('framework', '?')}")

    return manifest_path


def run(config: Config) -> Path:
    """Run Phase 1: Analyze the project codebase and produce pages_manifest.json.

    Two modes:
    - one-shot (default): sends all source code in a single prompt. Fast, cheap (~$0.31).
    - agentic (opt-in): Claude explores the codebase with read_file/grep_code tools.
      More expensive (~$0.55-1.50) but finds correct DTOs, auth guards, and endpoints
      because it can read exactly what it needs instead of guessing from a dump.

    Activate agentic mode via:
    - CLI: ``figma-audit analyze --agentic`` or ``figma-audit run --agentic``
    - YAML: ``analyze_mode: agentic``
    - Env: ``FIGMA_AUDIT_ANALYZE_MODE=agentic``

    Returns:
        Path to the generated pages_manifest.json.
    """
    import os

    mode = (
        os.environ.get("FIGMA_AUDIT_ANALYZE_MODE")
        or getattr(config, "analyze_mode", "one-shot")
        or "one-shot"
    )
    if mode == "agentic":
        console.print("[bold cyan]Mode: agentic (agent explores codebase with tools)[/bold cyan]")
        return _run_agentic(config)
    else:
        return _run_one_shot(config)
