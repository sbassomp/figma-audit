"""Phase 1 one-shot mode: send all source code in a single Claude prompt.

The default mode. Fast (~2 min) and cheap (~$0.31) but the AI sees only what
``_build_prompt`` chose to include. For complex projects with many DTOs and
auth guards, prefer the agentic mode (see :mod:`agentic`).
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

import figma_audit.phases.analyze_code as _pkg
from figma_audit.config import Config
from figma_audit.phases.analyze_code.discovery import (
    API_PATTERNS,
    MAX_TOTAL_PROMPT_SIZE,
    PAGE_PATTERNS,
    ROUTER_PATTERNS,
    TOKEN_PATTERNS,
    _detect_framework,
    _find_files,
    _read_file_safe,
)
from figma_audit.utils.claude_client import ClaudeClient

console = Console()


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
- For **reach_paths** (new, preferred for non-trivial routes): a page may be \
reachable through SEVERAL different user journeys, and the correct capture \
sequence depends on WHICH journey you want to exercise. Instead of a flat \
`navigation_steps` list, emit a `reach_paths` array where each entry is one \
self-contained scenario. Each scenario carries: \
\
- `name`: snake_case identifier (e.g. `guest_from_shared_link`, \
  `authenticated_from_detail_button`) \
- `description`: one sentence in English explaining where in the code the \
  journey starts and under what conditions it runs \
- `required_auth`: `guest` when the journey is only reachable logged-out, \
  `authenticated` when it requires a session, `any` when it works in both \
  modes \
- `steps`: the same primitives as legacy `navigation_steps` (`navigate`, \
  `click`, `fill`, `wait`, `wait_for_url`, `bridge_push`), executed in order \
\
**How to derive reach_paths**: for each parametrised route or each route \
reachable through a `context.push(...)`, grep the project for call sites \
that reference it. For every distinct call site, read the surrounding widget \
code to capture: \
(a) the conditional branch in which the push lives (`if (!isAuthenticated)`, \
`if (user.type == X)`, wizard step N, etc.); \
(b) any `extra:` argument passed along with the route — this signals that \
the target page reads in-memory state unreachable by URL; \
(c) the UI action that triggers the push (button label, form submit). \
Each distinct call site becomes one `reach_path` with steps that reproduce \
the journey: `navigate` to the parent route, `click` the widget that \
pushes, `wait_for_url` on the target pattern. \
\
**Detail-after-list pattern (PREFERRED for ANY entity-detail page)**: when \
the parametrised route is the detail of an entity that has a corresponding \
LIST page in the same app (`/invoices/:id` <-> `/invoices`, `/orders/:id` \
<-> `/orders`, `/messages/:id` <-> `/messages`), the **best** reach_path is \
NOT to seed the entity via API and template the id, it is to navigate to \
the list page and click the first tile. This pattern: \
\
- works without knowing any backend DTO \
- works for entities figma-audit cannot create (invoices, activity history, \
  notifications, anything generated by a backend trigger) \
- mirrors how a real user reaches the page \
- requires Flutter Semantics (already a dependency) \
\
The reach_path looks like: \
```json \
{"name": "click_first_invoice_from_list", "required_auth": "authenticated", \
 "steps": [ \
   {"action": "navigate", "url": "/invoices"}, \
   {"action": "wait", "timeout": 1500}, \
   {"action": "click", "role": "button", "index": 0, "min_y": 80}, \
   {"action": "wait_for_url", "pattern": "**/invoices/*", "timeout": 5000} \
 ]} \
``` \
\
The `min_y: 80` filter excludes the app bar back button so `index: 0` lands \
on the first real list tile. Use this pattern for every detail page whose \
parent route looks like a listing. \
\
**When a page requires an `extra:` Dart object**: emit a `bridge_push` step \
instead of trying to reach it via URL. The `bridge_push` action calls the \
figma-audit JS bridge exposed by the audited app (see README, Flutter \
integration). Format: \
`{"action": "bridge_push", "url": "/target/${id}/sub", "extra": {"__type__": \
"Course", "data": {"id": "${course_id}", ...}}}`. \
\
Order the `reach_paths` from most preferred to least. The capture runner \
picks the first one whose `required_auth` fits the current browser session. \
If a page has only one trivial journey (direct URL navigation), emit the \
legacy `navigation_steps` and skip `reach_paths`.
- For legacy **navigation_steps**: describe the Playwright actions needed to \
reach each page from the app root. \
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
- For **capturable_states**: list every distinct visual state of the page \
that the report needs to capture as a separate screenshot. For each state, \
declare HOW to reach it. Two navigation styles are supported and you must \
pick the more reliable one for the case at hand: \
\
**(A) `query` — preferred for tabs, filters, sort options and any state \
encoded in the URL query string.** The runner merges these query params \
into the current URL (preserving path params) and does a fresh navigation. \
Use this when a recent stateful URLs refactor has put the tab/filter into \
the URL, OR when the app is naturally URL-driven. Format: \
`{"state_id": "taken", "description": "Tab Courses prises", \
"query": {"tab": "taken"}}`. \
For multi-key filter combinations: `{"state_id": "in_stock_on_sale", \
"query": {"in_stock": "1", "on_sale": "1"}}`. \
\
**(B) `delta_steps` — fallback for wizards and stateful interactions \
that cannot be reached by URL alone.** A list of incremental click/fill \
primitives executed from the PREVIOUS state. Use this for multi-step \
forms (wizards), for showing/hiding overlays via a button, for any \
modal that does not have its own route. Format: \
`{"state_id": "step_2_addresses", "description": "Step 2 of the wizard", \
"delta_steps": [{"action": "click", "text": "Suivant"}]}`. \
\
The FIRST capturable_state is always the page in its default rendering \
(what `navigation_steps` lands on). Its `query` and `delta_steps` are \
both empty (the runner reuses the screenshot already taken). Subsequent \
states declare query OR delta_steps. \
\
**When to emit capturable_states**: \
\
1. The page has TABS (TabBar, SegmentedButton, NavigationBar branches): \
   emit one capturable_state per tab, with `query: {"tab": "..."}` if the \
   tab is URL-encoded, else `delta_steps` clicking the tab label. \
2. The page has filters that change the displayed list (filter chips, \
   filter dropdowns, search): emit a capturable_state per "interesting" \
   filter combination (default, one filter active, another filter active). \
   Prefer URL-encoded filters via `query`. \
3. The page is a multi-step WIZARD: every step is a capturable_state \
   with delta_steps for the click sequence to the next step. \
4. The page has dark/light theme variants visible in the design: emit a \
   `dark` state with no query/delta_steps (the runner can flip the \
   browser color scheme separately). \
\
Omit capturable_states only for pages with truly one visual state. \
\
For delta_steps actions: the app may use Flutter CanvasKit which has NO \
DOM elements. Use {"action": "click", "text": "Button Label"} — the \
automation tries accessibility roles (button, link, tab) first, then \
text match, then coordinates. For form fields, use {"action": "fill", \
"label": "Field Label", "value": "..."} which uses accessibility labels. \
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
(use plausible phone numbers, addresses, and names appropriate for the app's locale).
- Extract design tokens from the theme/token files into a structured format.
- For **user roles**: this is the part most likely to go wrong, so read \
these rules carefully. \
\
A user role is an IDENTITY (who is logged in), NOT a category of a domain \
object. Roles identify WHO is acting, not WHAT the acted-upon thing is. \
\
**NEGATIVE EXAMPLES (these are NOT roles):** \
- ``ProductCategory`` / ``ListingType`` / ``PlanTier`` / ``VehicleType`` \
- Status enums (draft, published, archived, paid, shipped) \
- Any enum whose values describe the OBJECT being manipulated, not the user \
  (book/cd/dvd, basic/pro/enterprise, small/medium/large, etc.) \
\
**POSITIVE SIGNALS (these identify roles):** \
- Auth guards or route middleware that gate a route by role \
  (``@RoleGuard('driver')``, ``requireRole``, ``hasPermission``) \
- A ``UserType`` / ``UserRole`` / ``accountType`` enum stored ON THE USER \
  entity itself (not on a business object) \
- JWT claims like ``role``, ``scopes``, ``permissions`` in the token payload \
- Separate signup/registration endpoints per actor \
  (``/api/drivers/signup`` vs ``/api/clients/signup``) \
- Separate login flows or distinct redirect landing pages per role \
- Entity ownership fields like ``createdBy`` vs ``assignedTo`` that refer to \
  DIFFERENT user types \
\
**Endpoint-caller heuristic** (the most reliable signal): for each endpoint \
whose name implies an action (``/take``, ``/accept``, ``/claim``, ``/assign``, \
``/buy``, ``/order``, ``/approve``, ``/reject``, ``/fulfill``, ``/ship``, \
``/deliver``), grep the CLIENT code to find which screens call it. If the \
creation endpoint and the action endpoint are called from screens behind \
DIFFERENT auth guards or from different navigation sections, their callers \
are different roles. The create-er and the take-er are almost always \
distinct actors. \
\
**Sanity check before emitting**: every role you declare MUST have at least \
one capability the OTHER roles do not have. If two candidate roles share \
exactly the same endpoints and guards, they are not distinct roles, they \
are one role with different object categories. Collapse them. \
\
If you find ONE user role only, emit a single account named ``user``. \
If you find TWO OR MORE distinct roles, use descriptive domain names \
(``driver``/``client``, ``seller``/``buyer``, not ``user1``/``user2``, and \
ABSOLUTELY NOT the values of a ``*Type`` enum).
- For **test_setup**: analyze the API client/service files to find the EXACT \
endpoints, HTTP methods, request payloads, and authentication flow used by \
the app. The new multi-actor shape has: \
(a) ``auth_endpoint``, ``auth_payload``, ``auth_token_path`` — shared across \
all accounts (one login flow); use ``${email}`` and ``${otp}`` placeholders \
resolved per-account by the harness; \
(b) ``accounts`` — a map of role name → credentials. Emit the ROLES you \
detected; leave ``email``/``otp`` blank (the user fills them in). \
(c) ``default_viewer`` — which account loads pages that don't override it; \
this should usually be the "consumer" role (the one that VIEWS most pages — \
buyer, taker, end user). \
(d) ``steps`` — ordered list of seed calls, each tagged with ``as: <role>``. \
For a two-actor flow where a "create" action is reserved to one role and a \
"take" action to another, emit TWO steps with the correct ``as`` and a \
``depends_on`` linking them. \
(e) ``cleanup_endpoint`` — optional. \
CRITICAL: use the real endpoints and payload field names from the API client \
code — do NOT guess.
- For **routes with a user id parameter** (``/profile/:userId``, \
``/users/:id``, ``/members/:userId``): the harness exposes every \
authenticated account's stable user id as ``${<role>_user_id}`` in \
``test_data`` (decoded from the JWT ``sub`` claim after login). Use that \
template directly in the navigation URL: \
``{"action": "navigate", "url": "/profile/${driver_user_id}"}`` (replace \
``driver`` with whichever role you picked for ``default_viewer``, or the \
role declared as ``viewer`` on the page). NEVER emit a literal placeholder \
like ``/profile/test-user-id``, ``/profile/sample-user-id``, \
``/profile/example-id`` or ``/profile/demo-user-id``: those are caught by \
the placeholder guard and fail the capture.
- For **per-page viewer**: if a page is only visible to a specific role \
(detected via the route guard or the page's data dependencies), set the \
``viewer`` field on that page to the matching role. Otherwise leave it \
unset and the harness uses ``default_viewer``.
- For **per-page depends_on**: list the names of any ``steps`` whose \
``save`` values are templated into the page's route or navigation. For \
example, a detail page at ``/items/${item_id}`` depends on the step that \
creates the item.

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
      "viewer": "string (account role, optional; omit to use default_viewer)",
      "depends_on": ["string (names of steps whose save values this page needs)"],
      "description": "string (what the page does, in English)",
      "params": [{"name": "string", "type": "string", "optional": "boolean"}],
      "required_state": {
        "description": "string (what state/data is needed)",
        "data_dependencies": ["string"]
      },
      "reach_paths": [
        {
          "name": "string (snake_case, identifies the scenario)",
          "description": "string (where in the code this journey starts)",
          "required_auth": "guest|authenticated|any",
          "steps": [
            {"action": "navigate|click|fill|wait|wait_for_url|bridge_push", \
"url?": "string", "selector?": "string", "text?": "string", "label?": "string", \
"value?": "string", "pattern?": "string", "extra?": "object", "timeout?": "number"}
          ]
        }
      ],
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
          "state_id": "string (snake_case, e.g. 'taken', 'deposited', 'step_2')",
          "description": "string (what is visible in this state, in English)",
          "query": {"key": "value"},
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
    "description": "Multi-actor test setup: accounts + DAG of seed steps.",
    "auth_endpoint": "/api/auth/login",
    "auth_otp_request_endpoint": "/api/auth/otp",
    "auth_payload": {"email": "${email}", "otp": "${otp}"},
    "auth_token_path": "accessToken",
    "accounts": {
      "seller": {"email": "", "otp": "1234"},
      "buyer": {"email": "", "otp": "1234"}
    },
    "default_viewer": "buyer",
    "steps": [
      {
        "name": "create_listing",
        "as": "seller",
        "endpoint": "/api/listings",
        "method": "POST",
        "payload": {"title": "Test listing", "priceCents": 1000},
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
    "cleanup_endpoint": "/api/listings/${listing_id}/archive"
  }
}
"""


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

    client = ClaudeClient(api_key=config.anthropic_api_key)
    manifest_data = client.analyze(
        system_prompt=SYSTEM_PROMPT,
        user_prompt=user_prompt,
        max_tokens=16384,
        phase="analyze",
    )
    # Expose for cost-tracking by callers via figma_audit.phases.analyze_code._last_client
    _pkg._last_client = client
    client.print_usage()

    from figma_audit.phases.analyze_code.validator import print_issues, validate_manifest

    manifest_data, _issues = validate_manifest(manifest_data)
    print_issues(_issues, console)

    with open(manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2, ensure_ascii=False)

    pages = manifest_data.get("pages", [])
    tokens = manifest_data.get("design_tokens", {})
    console.print(f"\n[bold green]Manifest saved to {manifest_path}[/bold green]")
    console.print(f"  {len(pages)} pages identified")
    console.print(f"  {len(tokens.get('colors', {}))} color tokens")
    console.print(f"  Framework: {manifest_data.get('framework', '?')}")

    return manifest_path
