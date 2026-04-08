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
    project_dir: Path,
) -> str:
    """Build the user prompt with all source files."""
    sections: list[str] = []

    sections.append(f"## Framework: {framework}\n")

    # Router files (most important)
    sections.append("## Router / Navigation Files\n")
    for rel_path, content in router_files.items():
        sections.append(f"### {rel_path}\n```dart\n{content}\n```\n")

    # Design token files
    if token_files:
        sections.append("## Design Token Files\n")
        for rel_path, content in token_files.items():
            sections.append(f"### {rel_path}\n```dart\n{content}\n```\n")

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
For pages that require dynamic IDs (e.g. /courses/:id), use ${test_data.key} templates \
in URLs (e.g. {"action": "navigate", "url": "/courses/${test_data.course_id}"}). \
Available test_data keys include: course_id (first available), \
course_available_id, course_taken_id (taken by main user), course_ids (all). \
For capturable_states that depend on data state (e.g. course detail "available" vs "taken"), \
use different test_data keys in the delta_steps navigation URL \
(e.g. state "taken" navigates to /courses/${test_data.course_taken_id}).
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
For wizards: each step is a capturable_state. \
For pages with tabs: each tab is a capturable_state. \
Use text-based selectors (e.g. {"action": "click", "text": "Suivant"}) rather than \
CSS selectors when possible, as Flutter CanvasKit apps may not have DOM elements.
- For auth_required: check if the route is behind an auth guard/redirect.
- For test_data: suggest realistic test values for forms \
(French context: phone +33..., French addresses).
- Extract design tokens from the theme/token files into a structured format.

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
    "email": "test@example.com",
    "addresses": {"pickup": "...", "destination": "..."},
    "patient_name": "Jean Dupont"
  }
}
"""


def run(config: Config) -> Path:
    """Run Phase 1: Analyze the project codebase and produce pages_manifest.json.

    Returns:
        Path to the generated pages_manifest.json.
    """
    project_dir = Path(config.project).expanduser().resolve()
    if not project_dir.exists():
        raise FileNotFoundError(f"Project directory not found: {project_dir}")

    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "pages_manifest.json"

    # ── Step 1: Detect framework ───────────────────────────────────
    framework = _detect_framework(project_dir)
    console.print(f"[bold]Framework detected: {framework}[/bold]")

    if framework == "unknown":
        raise ValueError(
            f"Could not detect framework in {project_dir}. "
            "Supported: flutter, react, vue, angular, nextjs."
        )

    # ── Step 2: Find relevant files ────────────────────────────────
    router_paths = _find_files(project_dir, ROUTER_PATTERNS.get(framework, []))
    page_paths = _find_files(project_dir, PAGE_PATTERNS.get(framework, []))
    token_paths = _find_files(project_dir, TOKEN_PATTERNS.get(framework, []))

    console.print(f"  Router files: {len(router_paths)}")
    console.print(f"  Page files:   {len(page_paths)}")
    console.print(f"  Token files:  {len(token_paths)}")

    if not router_paths:
        raise FileNotFoundError(
            f"No router files found in {project_dir}. "
            f"Searched patterns: {ROUTER_PATTERNS.get(framework, [])}"
        )

    # ── Step 3: Read files ─────────────────────────────────────────
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

    total_chars = (
        sum(len(v) for v in router_files.values())
        + sum(len(v) for v in page_files.values())
        + sum(len(v) for v in token_files.values())
    )
    console.print(f"  Total source: {total_chars:,} chars")

    # ── Step 4: Build prompt and call Claude ───────────────────────
    user_prompt = _build_prompt(framework, router_files, page_files, token_files, project_dir)
    console.print(f"  Prompt size: {len(user_prompt):,} chars")
    console.print("[bold]Sending to Claude for analysis...[/bold]")

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

    # ── Step 5: Save manifest ──────────────────────────────────────
    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2, ensure_ascii=False)

    pages = manifest_data.get("pages", [])
    tokens = manifest_data.get("design_tokens", {})
    console.print(f"\n[bold green]Manifest saved to {manifest_path}[/bold green]")
    console.print(f"  {len(pages)} pages identified")
    console.print(f"  {len(tokens.get('colors', {}))} color tokens")
    console.print(f"  Framework: {manifest_data.get('framework', '?')}")

    return manifest_path
