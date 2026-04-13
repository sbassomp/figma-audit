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

from figma_audit.config import Account, Config, TestSetup
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

figma-audit captures pages of a web app via Playwright. Many pages only \
make sense when real backend state exists:
- A `/courses/:id` page needs a real course in the database
- An empty-vs-populated list page needs an item seeded first
- A two-sided flow (seller ↔ buyer, requester ↔ taker, admin ↔ user) \
  requires setup steps performed by DIFFERENT accounts, because each \
  action is authorized for a specific role only

The `test_setup` block is a declarative script: a list of accounts and \
a DAG of HTTP calls, each tagged with which account performs it. The \
harness runs the steps in order before every capture, replaying the \
data each run needs.

The user has already run Phase 1 (code analysis) which produced a \
pages_manifest.json with an AI-guessed test_setup. That guess often \
has wrong field names. Your job is to find the correct fields by \
reading the actual source code and validating against the live backend.

## Your tools

- `read_file(path)`: read files in the project being audited
- `grep_code(pattern, file_glob)`: search the project codebase
- `list_files(directory)`: explore directory structure
- `http_request(method, path, body, as)`: call the live backend. Base URL \
  is injected automatically. Use `as` to pick which account performs the \
  call — it must be one of the roles the harness has pre-authenticated \
  (listed below). When omitted, the default role is used. **Match the \
  role to the action**: if "only drivers can take a course", call the take \
  endpoint `as: driver`. If "only clients can create a course", call the \
  create endpoint `as: client`.
- `ask_user(question)`: ask the human when genuinely ambiguous
- `submit_result(result)`: call once when you have a complete, \
  backend-validated test_setup JSON object

## Process

1. Read the initial context below to understand which pages need what state.
2. For each step you need to script:
   a. Work out which ROLE performs it. Read auth guards, permission \
      decorators, role enums in the source to decide. If two roles are \
      plausible, prefer the one whose name matches the action (an action \
      called `acceptCourse` belongs to the party that accepts).
   b. Find the exact request DTO in the source code (Dart freezed, Kotlin \
      data class, TS Zod/interface). Note SERIALIZED field names — they \
      may differ from property names via @JsonValue, @JsonProperty, etc.
   c. Build a realistic payload using the correct field names and types.
   d. For dates that must be in the future, use `${now+1d}` (resolves to \
      tomorrow's ISO-8601 UTC timestamp at run time). Other supported \
      suffixes: `${now}`, `${now-30m}`, `${now+2h}`.
   e. For values produced by an EARLIER step, use `${key_name}` where \
      `key_name` matches the `save` field of that step.
   f. Call `http_request(POST, path, payload, as: <role>)` to validate.
   g. On 400: READ THE ERROR BODY — it lists missing/invalid fields. Fix \
      and retry. Never retry the exact same payload twice.
   h. On 2xx: record the working payload and the id_path for `save`.
3. When every step is green, call `submit_result` with the full block.

## Output schema (the argument to submit_result)

```json
{
  "auth_endpoint": "/api/auth/login",
  "auth_otp_request_endpoint": "/api/auth/otp",
  "auth_payload": {"email": "${email}", "otp": "${otp}"},
  "auth_token_path": "accessToken",

  "default_viewer": "buyer",

  "steps": [
    {
      "name": "create_listing",
      "as": "seller",
      "endpoint": "/api/listings",
      "method": "POST",
      "payload": {"title": "Test Item", "priceCents": 1000},
      "save": {"listing_id": "id"}
    },
    {
      "name": "place_order",
      "as": "buyer",
      "endpoint": "/api/listings/${listing_id}/orders",
      "method": "POST",
      "payload": {"quantity": 1},
      "save": {"order_id": "id"},
      "depends_on": ["create_listing"]
    }
  ],

  "cleanup_endpoint": "/api/listings/${item_id}/archive"
}
```

**Do NOT emit an `accounts` field** — the harness injects the credentials \
map from `figma-audit.yaml` itself. You only describe the STEPS and which \
role performs each.

## Rules

- ALWAYS read the actual DTO source code before building a payload.
- For enum fields, find the exact serialized string values in the source.
- Dates that must be in the future: use `${now+1d}`, never hard-code.
- `auth_payload` is shared across all accounts (one login flow). Use \
  `${email}` and `${otp}` — those placeholders are resolved per-account \
  by the harness.
- Keep payloads minimal: required fields plus a few useful optionals.
- Each step must reference a role (`as`) that exists in the registered \
  accounts list below. Using an unknown role will be rejected.
- `depends_on` must list the `name` of any step whose `save` values are \
  templated into this step's URL or payload. The harness enforces the \
  order.
- If you get 3 consecutive failures on the same endpoint, ask_user.
"""


def _derive_accounts(config: Config, test_setup: TestSetup) -> dict[str, Account]:
    """Figure out which accounts to pre-authenticate.

    Priority order:

    1. ``test_setup.accounts`` (new-shape YAML) — used verbatim.
    2. Legacy fallback: ``config.seed_account`` becomes role ``seed`` and
       ``config.test_credentials`` becomes role ``main``. This lets users
       who haven't migrated their YAML still benefit from Phase C by
       getting two roles pre-authed automatically.
    """
    if test_setup.accounts:
        return dict(test_setup.accounts)

    accounts: dict[str, Account] = {}
    if config.seed_account.email:
        accounts["seed"] = Account(email=config.seed_account.email, otp=config.seed_account.otp)
    if config.test_credentials.email:
        accounts["main"] = Account(
            email=config.test_credentials.email, otp=config.test_credentials.otp
        )
    return accounts


def _login_accounts(
    app_url: str,
    test_setup_dict: dict,
    accounts: dict[str, Account],
    console: Console,
) -> dict[str, str]:
    """Pre-authenticate every account and return a map of role → bearer token.

    Failures are reported but not fatal for individual accounts — the
    caller decides whether to abort. The shared ``test_setup_dict`` is
    mutated in place so ``_api_prefix_hint`` is discovered once and
    reused across every login.
    """
    from figma_audit.phases.capture_app import _api_login

    tokens: dict[str, str] = {}
    for role, account in accounts.items():
        if not account.email:
            console.print(f"  [yellow]Role '{role}' has no email, skipping[/yellow]")
            continue
        creds = {"email": account.email, "otp": account.otp}
        token = _api_login(app_url, test_setup_dict, creds)
        if token:
            tokens[role] = token
            console.print(f"  [green]{role}: authenticated[/green] ({account.email})")
        else:
            console.print(f"  [red]{role}: login failed[/red] ({account.email})")
    return tokens


def _normalize_agent_output(raw: dict, accounts: dict[str, Account]) -> dict:
    """Turn the agent's ``submit_result`` payload into a valid TestSetup dict.

    The agent is instructed to emit the new multi-actor shape (``steps``
    + ``default_viewer``) and is told NOT to output ``accounts`` (the
    harness injects them from config). Older prompts or confused models
    occasionally return a legacy ``seed_items`` / ``take_item`` block —
    we handle both.

    We inject ``accounts`` before validation so that step refs like
    ``as: seller`` resolve against the real registered accounts rather
    than failing on an empty account map.
    """
    clean = {k: v for k, v in raw.items() if not k.startswith("_")}

    is_new_shape = bool(clean.get("steps") or clean.get("accounts"))

    if is_new_shape:
        # Agent emitted the new shape: drop any accounts it may have
        # included (we don't trust agent-supplied credentials) and inject
        # the real ones from config before validation.
        clean.pop("accounts", None)
        clean["accounts"] = {
            role: acct.model_dump(exclude_none=True) for role, acct in accounts.items()
        }
        parsed = TestSetup.model_validate(clean)
    else:
        # Legacy shape — route through from_raw with credentials hints so
        # seed_items/take_item migrate to the right role names, then
        # overwrite the parsed accounts with the real ones from config.
        main_creds = None
        seed_creds = None
        if "main" in accounts and accounts["main"].email:
            main_creds = {
                "email": accounts["main"].email,
                "otp": accounts["main"].otp,
            }
        if "seed" in accounts and accounts["seed"].email:
            seed_creds = {
                "email": accounts["seed"].email,
                "otp": accounts["seed"].otp,
            }
        parsed = TestSetup.from_raw(
            clean,
            main_credentials=main_creds,
            seed_credentials=seed_creds,
        )
        # Only replace roles that actually exist in our registered map —
        # legacy migration may have produced "seed"/"main" even when the
        # caller uses different role names. Re-validate after replacement
        # in case the replacement changed the role set.
        parsed = parsed.model_copy(update={"accounts": accounts})
        TestSetup.model_validate(parsed.model_dump(by_alias=True))

    return parsed.model_dump(by_alias=True, exclude_none=True)


def _build_initial_message(
    manifest: dict,
    config: Config,
    accounts: dict[str, Account],
    default_role: str | None,
) -> str:
    """Build the first user message that bootstraps the agent."""
    parts: list[str] = []

    parts.append("## Project info\n")
    parts.append(f"Framework: {manifest.get('framework', 'unknown')}")
    parts.append(f"Renderer: {manifest.get('renderer', 'unknown')}")
    parts.append(f"Project directory: {config.project}")
    parts.append(f"App URL: {config.app_url}")
    parts.append("")

    # Registered accounts the agent may use with ``http_request(as=...)``.
    parts.append("## Registered accounts (use these in `as`)")
    for role, account in accounts.items():
        marker = " (default)" if role == default_role else ""
        parts.append(f"- `{role}`{marker}: {account.email or '(no email)'}")
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
        parts.append("## test_data (available for ${X} templates in URLs/payloads)")
        parts.append("```json")
        parts.append(json.dumps(td, indent=2, ensure_ascii=False))
        parts.append("```")
        parts.append("")

    # List pages with :param routes to show what needs seeding
    param_pages = [p for p in manifest.get("pages", []) if ":" in (p.get("route") or "")]
    if param_pages:
        parts.append("## Pages needing seeded entity IDs")
        for p in param_pages:
            td_ref = ""
            for step in p.get("navigation_steps", []):
                url = step.get("url", "")
                if "${" in url:
                    td_ref = url
            parts.append(f"- {p['id']} route={p['route']} → nav URL: {td_ref or '(direct)'}")
        parts.append("")

    parts.append(
        "## Instructions\n"
        "Start by exploring the project code to find the request DTOs for the "
        "seed endpoints above. Decide which role performs each step based on "
        "the auth guards in the code. Validate every call against the live "
        "backend with `http_request(..., as: <role>)`. When every step "
        "returns 2xx, call `submit_result` with the multi-actor block."
    )

    return "\n".join(parts)


def _write_test_setup_to_yaml(test_setup: dict, yaml_path: Path, console: Console) -> None:
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
        raise FileNotFoundError("pages_manifest.json not found. Run Phase 1 (analyze) first.")

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

    app_url = config.app_url
    if not app_url:
        raise ValueError("No app URL configured. Set app_url in figma-audit.yaml.")

    test_data = manifest.get("test_data", {}).copy()
    if config.test_credentials.email:
        test_data["email"] = config.test_credentials.email
        test_data["otp"] = config.test_credentials.otp

    # Parse the (possibly legacy) test_setup and figure out which accounts
    # to pre-authenticate. We build a mutable dict copy of the test_setup
    # because `_api_login` records the API prefix hint as a side effect
    # we want to share across all account logins.
    parsed_setup = config.test_setup_model()
    accounts = _derive_accounts(config, parsed_setup)
    if not accounts:
        console.print(
            "[red]No accounts to authenticate.[/red] Configure "
            "`test_setup.accounts` (or `seed_account` / `test_credentials`) "
            "in figma-audit.yaml first."
        )
        raise SystemExit(1)

    console.print(f"\n[bold]Pre-authenticating {len(accounts)} account(s)...[/bold]")
    login_setup_dict = dict(config.test_setup or manifest.get("test_setup", {}) or {})
    tokens = _login_accounts(app_url.rstrip("/"), login_setup_dict, accounts, console)

    if not tokens:
        console.print(
            "[red]None of the accounts could log in.[/red] Check credentials, "
            "the auth_endpoint in test_setup, and the backend reachability."
        )
        raise SystemExit(1)

    # Drop accounts whose login failed — the agent should not reference
    # them in `as` parameters.
    accounts = {role: acct for role, acct in accounts.items() if role in tokens}

    default_role = parsed_setup.default_viewer if parsed_setup.default_viewer in tokens else None
    if default_role is None:
        # Prefer "main" → "seed" → first; stable order for determinism.
        for preferred in ("main", "seed"):
            if preferred in tokens:
                default_role = preferred
                break
        else:
            default_role = next(iter(tokens))

    # Build agent context with the multi-token map
    project_dir = Path(config.project).expanduser().resolve()
    ctx = AgentContext(
        project_dir=project_dir,
        app_url=app_url.rstrip("/"),
        tokens=tokens,
        default_role=default_role,
        interactive=True,
    )

    # Build initial user message from manifest
    initial_message = _build_initial_message(manifest, config, accounts, default_role)

    # Launch the agentic loop
    console.print("\n[bold]Starting setup-test-data agent...[/bold]")
    console.print("[dim]Budget: max 25 iterations, ~$0.40-1.50 expected[/dim]\n")

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

    # Extract and normalize the test_setup
    test_setup_data = result.data
    if not isinstance(test_setup_data, dict):
        console.print(
            "[red]Agent returned an unexpected result type. Expected a test_setup dict.[/red]"
        )
        console.print(f"Got: {test_setup_data}")
        raise SystemExit(1)

    try:
        normalized = _normalize_agent_output(test_setup_data, accounts)
    except Exception as e:
        console.print(f"[red]Agent output failed validation: {e}[/red]")
        console.print("\n[bold]Raw agent output:[/bold]")
        console.print(yaml.dump(test_setup_data, default_flow_style=False, allow_unicode=True))
        raise SystemExit(1) from e

    normalized["default_viewer"] = normalized.get("default_viewer") or default_role

    console.print(
        f"\n[bold]Agent completed in {result.iterations} iterations "
        f"({result.elapsed_seconds:.1f}s)[/bold]"
    )
    client.print_usage()

    # Show the result for user review
    console.print("\n[bold]Proposed test_setup:[/bold]")
    console.print(yaml.dump(normalized, default_flow_style=False, allow_unicode=True))

    # Write to YAML
    _write_test_setup_to_yaml(normalized, yaml_path, console)

    return yaml_path
