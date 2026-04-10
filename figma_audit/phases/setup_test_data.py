"""Interactive agent that builds a verified test_setup config block.

This phase reads the existing pages_manifest.json to understand what endpoints
and entities need to be seeded, then uses an agentic loop with Claude to:
1. Explore the audited project's codebase for request DTOs
2. Build candidate payloads
3. Validate them against the live backend via http_request
4. Iterate on 400 responses until each seed returns 2xx
5. Produce a test_setup block written to figma-audit.yaml

The result is a human-reviewed, backend-validated test_setup config that
replaces the AI-guessed one from Phase 1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from rich.console import Console

from figma_audit.config import Config
from figma_audit.utils.agent_context import AgentContext
from figma_audit.utils.agent_loop import run_agent_loop
from figma_audit.utils.agent_tools import LIVE_BACKEND_TOOLS
from figma_audit.utils.claude_client import ClaudeClient

console = Console()

SYSTEM_PROMPT = """\
You are an API integration specialist helping configure a UI audit tool \
(figma-audit). Your job is to produce a validated `test_setup` YAML block \
that the tool uses to seed test data before capturing screenshots.

## Context

figma-audit captures pages of a web app via Playwright. For pages whose \
URL has path parameters (like /courses/:id or /invoices/:id), the tool \
needs to create real entities via the app's API so it can navigate to \
/courses/<real-id>. The `test_setup` config block describes the API \
endpoints, payloads, and auth flow needed to do this.

The user has already run Phase 1 (code analysis) which produced a \
pages_manifest.json with an AI-guessed test_setup. That guess often \
has wrong field names. Your job is to find the correct fields by \
reading the actual source code and validating against the live backend.

## Your tools

- `read_file(path)`: read files in the project being audited
- `grep_code(pattern, file_glob)`: search the project codebase
- `list_files(directory)`: explore directory structure
- `http_request(method, path, body)`: call the live backend (base URL + \
  bearer token are injected automatically). The token belongs to a \
  seed account that creates entities visible to the main test user.
- `ask_user(question)`: ask the human when genuinely ambiguous
- `submit_result(result)`: call once when you have a complete, \
  backend-validated test_setup JSON object

## Process

1. Read the initial context below (pages_manifest skeleton) to understand \
   what endpoints the tool needs.
2. For each seed endpoint:
   a. grep/read the project code to find the exact request DTO class \
      (Dart: freezed/JsonSerializable, Kotlin: data class, TS: Zod/interface)
   b. Note the SERIALIZED field names (check @JsonValue, @JsonProperty, \
      @SerializedName annotations — the JSON field name may differ from \
      the property name)
   c. Build a realistic payload using the correct field names and types
   d. For date/time fields that must be in the future, use the magic \
      template "${now+1d}" (resolves to tomorrow's ISO-8601 UTC timestamp \
      at capture time)
   e. For fields that reference other test_data values, use "${test_data.X}"
   f. Call http_request(POST, path, payload) to test it
   g. If you get 400: READ THE ERROR BODY — it lists missing/invalid fields. \
      Adjust and retry. Do NOT guess randomly. Do NOT retry the same payload.
   h. If you get 2xx: record the working payload.
3. Also verify the auth endpoint works (it has already been tested by the \
   harness; you can call it to confirm).
4. When ALL seed endpoints return 2xx, call submit_result with the full \
   test_setup object.

## Output schema (the argument to submit_result)

```json
{
  "auth_endpoint": "/api/...",
  "auth_otp_request_endpoint": "/api/...",  // optional
  "auth_payload": {"field": "${test_data.key}", ...},
  "auth_token_path": "accessToken",  // dotted path in login response
  "seed_items": [
    {
      "endpoint": "/api/...",
      "method": "POST",
      "payload": {/* exact fields, validated */},
      "id_path": "id",
      "test_data_key": "course_id"  // injected into test_data
    }
  ],
  "take_item": {  // optional
    "endpoint": "/api/items/${course_id}/take",
    "method": "POST",
    "test_data_key": "course_taken_id"
  },
  "cleanup_endpoint": "/api/items/${item_id}/archive"  // optional
}
```

## Rules

- ALWAYS read the actual DTO source code before building a payload. \
  Never guess field names from the manifest skeleton alone.
- For enum fields, find the @JsonValue or serialized string values in the \
  code. Use those exact strings in the payload.
- Dates that must be in the future: use "${now+1d}", never hard-code dates.
- Credentials in auth_payload: use "${test_data.email}", "${test_data.otp}", \
  "${test_data.phone}" — not literal values.
- If a field is truly optional and you are unsure of its format, omit it.
- Keep the payload minimal: only required fields + a few useful optionals.
- If you get 3 consecutive failures on the same endpoint, ask_user.
"""


def _build_initial_message(manifest: dict, config: Config) -> str:
    """Build the first user message that bootstraps the agent."""
    parts: list[str] = []

    parts.append("## Project info\n")
    parts.append(f"Framework: {manifest.get('framework', 'unknown')}")
    parts.append(f"Renderer: {manifest.get('renderer', 'unknown')}")
    parts.append(f"Project directory: {config.project}")
    parts.append(f"App URL: {config.app_url}")
    parts.append("")

    # Include the existing (possibly broken) test_setup skeleton
    existing_ts = manifest.get("test_setup")
    if existing_ts:
        parts.append("## Existing test_setup from Phase 1 (AI-guessed, may be wrong)")
        parts.append("```json")
        parts.append(json.dumps(existing_ts, indent=2, ensure_ascii=False))
        parts.append("```")
        parts.append("")

    # Include test_data for credential templates
    td = manifest.get("test_data", {})
    if td:
        parts.append("## test_data (available for ${test_data.X} templates)")
        parts.append("```json")
        parts.append(json.dumps(td, indent=2, ensure_ascii=False))
        parts.append("```")
        parts.append("")

    # List pages with :param routes to show what needs seeding
    param_pages = [
        p for p in manifest.get("pages", [])
        if ":" in (p.get("route") or "")
    ]
    if param_pages:
        parts.append("## Pages needing seeded entity IDs")
        for p in param_pages:
            td_ref = ""
            for step in p.get("navigation_steps", []):
                url = step.get("url", "")
                if "${" in url:
                    td_ref = url
            parts.append(
                f"- {p['id']} route={p['route']} → nav URL: {td_ref or '(direct)'}"
            )
        parts.append("")

    parts.append(
        "## Instructions\n"
        "Start by exploring the project code to find the request DTOs for the "
        "seed endpoints above. Then build and validate payloads against the "
        "live backend. When every seed returns 2xx, call submit_result."
    )

    return "\n".join(parts)


def _write_test_setup_to_yaml(
    test_setup: dict, yaml_path: Path, console: Console
) -> None:
    """Write the validated test_setup to figma-audit.yaml (preserving other keys)."""
    existing: dict = {}
    if yaml_path.exists():
        with open(yaml_path) as f:
            existing = yaml.safe_load(f) or {}

    existing["test_setup"] = test_setup
    with open(yaml_path, "w") as f:
        yaml.dump(existing, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    console.print(f"\n[bold green]test_setup written to {yaml_path}[/bold green]")


def run(config: Config) -> Path:
    """Run the setup-test-data agent interactively.

    Returns the path to the updated figma-audit.yaml.
    """
    if not sys.stdin.isatty():
        console.print("[red]setup-test-data requires an interactive terminal.[/red]")
        console.print("Run it from a shell, not from a daemon or CI pipeline.")
        raise SystemExit(1)

    output_dir = config.output_dir
    manifest_path = output_dir / "pages_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            "pages_manifest.json not found. Run Phase 1 (analyze) first."
        )

    with open(manifest_path) as f:
        manifest = json.load(f)

    # Find the YAML config file to write to
    yaml_candidates = [
        Path("figma-audit.yaml"),
        Path("figma-audit.yml"),
        output_dir / "figma-audit.yaml",
    ]
    yaml_path = yaml_candidates[0]  # default: CWD
    for p in yaml_candidates:
        if p.exists():
            yaml_path = p
            break

    # Pre-flight: login the seed account to get a bearer token for http_request
    from figma_audit.phases.capture_app import _api_login

    app_url = config.app_url
    if not app_url:
        raise ValueError("No app URL configured. Set app_url in figma-audit.yaml.")

    test_data = manifest.get("test_data", {}).copy()
    if config.test_credentials.email:
        test_data["email"] = config.test_credentials.email
        test_data["otp"] = config.test_credentials.otp

    test_setup = config.test_setup or manifest.get("test_setup", {})
    seed_account = config.seed_account.model_dump() if config.seed_account.email else None
    seed_creds = seed_account or test_data
    token = _api_login(app_url.rstrip("/"), dict(test_setup), seed_creds)

    if not token:
        console.print("[red]Could not authenticate the seed account.[/red]")
        console.print(
            "Check that seed_account (or test_credentials) are correctly "
            "configured in figma-audit.yaml and that the backend is reachable."
        )
        raise SystemExit(1)

    console.print("[green]Seed account authenticated.[/green]")

    # Build agent context
    project_dir = Path(config.project).expanduser().resolve()
    ctx = AgentContext(
        project_dir=project_dir,
        app_url=app_url.rstrip("/"),
        auth_token=token,
        interactive=True,
    )

    # Build initial user message from manifest
    initial_message = _build_initial_message(manifest, config)

    # Launch the agentic loop
    console.print("\n[bold]Starting setup-test-data agent...[/bold]")
    console.print(
        "[dim]Budget: max 25 iterations, ~$0.40-1.50 expected[/dim]\n"
    )

    client = ClaudeClient(api_key=config.anthropic_api_key)
    result = run_agent_loop(
        client=client,
        system_prompt=SYSTEM_PROMPT,
        initial_user_message=initial_message,
        tools=LIVE_BACKEND_TOOLS,
        context=ctx,
        phase="setup_test_data",
        max_iterations=25,
        max_wall_seconds=600.0,
    )

    # Extract and write the test_setup
    test_setup_data = result.data
    if not isinstance(test_setup_data, dict):
        console.print(
            "[red]Agent returned an unexpected result type. "
            "Expected a test_setup dict.[/red]"
        )
        console.print(f"Got: {test_setup_data}")
        raise SystemExit(1)

    console.print(f"\n[bold]Agent completed in {result.iterations} iterations "
                  f"({result.elapsed_seconds:.1f}s)[/bold]")
    client.print_usage()

    # Show the result for user review
    console.print("\n[bold]Proposed test_setup:[/bold]")
    console.print(yaml.dump(test_setup_data, default_flow_style=False, allow_unicode=True))

    # Write to YAML
    _write_test_setup_to_yaml(test_setup_data, yaml_path, console)

    return yaml_path
