# figma-audit

**Automated design conformity audits: compare your Figma designs against your deployed app and get a detailed discrepancy report.**

## The problem

Your designer delivers pixel-perfect Figma mockups. Your developers implement them. But between the design and the deployed app, things drift: a button color is slightly off, a border-radius changes, spacing is inconsistent, an element is missing. These gaps accumulate silently and erode the user experience.

Existing tools don't solve this:
- **Visual regression tools** (BackstopJS, Percy, Chromatic, Applitools) compare two versions of the *same app* — they catch regressions but can't tell you if the implementation matches the *original design*.
- **Manual review** works but doesn't scale: designers eyeball each screen, open Figma, compare side by side, file tickets. It takes hours and misses subtle differences.

**figma-audit** fills this gap. It takes your Figma file, takes your deployed app URL, and produces a structured report of every discrepancy — automatically.

## How it works

figma-audit runs a 6-phase pipeline:

```
                    ┌─────────────┐
  Source code ─────►│ 1. Analyze  │──► pages_manifest.json (routes, auth, params)
                    └─────────────┘
                    ┌─────────────┐
  Figma file  ─────►│ 2. Export   │──► figma_manifest.json + screen PNGs
                    └─────────────┘
                    ┌─────────────┐
  Manifests   ─────►│ 3. Match    │──► screen_mapping.yaml (Figma screen ↔ app route)
                    └─────────────┘
                    ┌─────────────┐
  Live app URL ────►│ 4. Capture  │──► app screenshots + computed styles
                    └─────────────┘
                    ┌─────────────┐
  Screenshots ─────►│ 5. Compare  │──► discrepancies.json (per-element diffs)
                    └─────────────┘
                    ┌─────────────┐
  Discrepancies ───►│ 6. Report   │──► standalone HTML report
                    └─────────────┘
```

Each phase reads from and writes to a shared output directory. You can re-run any phase independently, inspect intermediate results, and override AI decisions via YAML.

**Phase 1** uses Claude AI to analyze your source code: it detects the framework (Flutter, React, Vue, Angular, Next.js), extracts every route, identifies auth guards, and generates test data setup instructions. An optional **agentic mode** lets Claude explore the codebase iteratively with tools for higher accuracy.

**Phase 2** exports your Figma file — either from a `.fig` file (offline, recommended), a ZIP export, or the Figma REST API. Design tokens (colors, fonts, spacing, border-radius) are extracted alongside the screen images.

**Phase 3** uses Claude Vision to match each Figma screen to its corresponding app route. The result is a human-reviewable YAML mapping that you can edit before proceeding.

**Phase 4** launches a Playwright browser, authenticates, seeds test data via your app's API, and captures every mapped route. It handles Flutter CanvasKit apps, wizard multi-step flows, and detects silent navigation failures (redirects, placeholder URLs).

**Phase 5** compares each Figma screen against its app screenshot using Claude Vision. It evaluates 9 categories: layout, colors, typography, components, text content, spacing, missing elements, added elements, and data gaps. Each discrepancy gets a severity (critical / important / minor) and a location on the screen.

**Phase 6** generates a standalone HTML report with side-by-side screenshots, severity-coded discrepancy lists, and an executive summary.

## Key advantages

- **Semantic, not pixel-diff**: understands that a different person name is dynamic data (ignore it), but a different button color is a design gap (report it). No false positives from font rendering, anti-aliasing, or test data differences.
- **End-to-end automated**: from Figma file to HTML report in one command. No manual screenshot-taking, no copy-pasting between tools.
- **Works with any web framework**: Flutter (CanvasKit and HTML renderer), React, Vue, Angular, Next.js. The source code analysis adapts to each framework's routing conventions.
- **Honest about failures**: when a page can't be captured (auth redirect, missing test data, signed URLs), the tool says so clearly instead of comparing garbage screenshots. The dashboard shows exactly what was tested and what failed.
- **Human-in-the-loop where it matters**: the Figma-to-route mapping (Phase 3) is reviewable YAML. The test data setup can be overridden in a config file. The agentic mode asks clarification questions when the code is ambiguous.
- **Cost-transparent**: every API call is tracked. The dashboard shows token counts and estimated costs per phase, per run.
- **Web dashboard included**: track multiple projects, browse run history, compare Figma vs app side by side, manage discrepancies (ignore, fix, annotate), generate fix prompts for developers.

## Features

- **Source code analysis**: automatic framework detection, route extraction, design token discovery. Optional **agentic mode** (Claude explores the codebase with read/grep/list tools) for higher accuracy on complex projects
- **Figma export**: `.fig` file parsing (offline), ZIP import, or REST API with local cache and rate limiting
- **Intelligent matching**: Figma screens matched to app routes using Claude Vision, with human-reviewable YAML output
- **Application capture**: Playwright automation with Flutter CanvasKit support, multi-strategy click/fill, test data seeding via API, silent redirect detection
- **Interactive `setup-test-data` agent**: explores your codebase, builds API payloads, validates them against the live backend, writes verified config
- **Hybrid comparison**: per-element analysis across 9 categories with zero-tolerance on colors, borders, and positions
- **Standalone HTML report**: self-contained file with embedded images, dark theme, side-by-side view
- **Web dashboard**: htmx interface with project tracking, run history, screen gallery, discrepancy management
- **Daemon service**: systemd (Linux) or launchd (macOS) for a permanent dashboard

## Installation

```bash
pip install -e .
figma-audit setup
```

The `setup` command guides you through the installation step by step:
1. API key configuration (Anthropic, Figma)
2. SQLite database initialization
3. Chromium browser installation (Playwright)
4. Optional system daemon installation

### Prerequisites

- Python 3.11+
- An [Anthropic](https://console.anthropic.com/) account with an API key
- Figma screens provided via **one** of the following (see [Providing Figma screens](#providing-figma-screens)):
  - A `.fig` file (exported from Figma Desktop or downloaded from the Figma UI)
  - A ZIP export from Figma Desktop (File > Export frames to PDF)
  - A [Figma Personal Access Token](https://www.figma.com/developers/api#access-tokens) (online API — subject to rate limits)
- `pdftoppm` (`poppler-utils` package) — only needed for ZIP imports

## Quick start

### 1. Project configuration

Create a `figma-audit.yaml` file at the project root:

```yaml
project: ~/dev/mon-projet
figma_url: "https://www.figma.com/design/XXXXXX/Mon-Projet"
app_url: "https://mon-app.example.com"
output: ./output

viewport:
  width: 390
  height: 844
  device_scale_factor: 1

# Secondary account for creating test data (optional)
seed_account:
  email: "test@example.com"
  otp: "1234"
```

API keys are automatically loaded from `~/.config/figma-audit/env` (created by `figma-audit setup`).

### 2. Run a full audit

```bash
figma-audit run
```

The pipeline executes the 6 phases and displays progress:

```
[1/6] Analyze code
  12.3s  35 pages  ~$0.167
[2/6] Export Figma
  0.2s  77 screens
[3/6] Match screens
  45.2s  70 matches  ~$0.187
[4/6] Capture app
  92.1s  19 pages
[5/6] Compare (estimate: ~279,100 tokens, ~$1.38)
  8m12s  673 discrepancies  ~$1.426
[6/6] Report
  3.1s  32.4 MB

Run summary
  Total: 10m44s | 282,700 tokens | ~$1.78
```

The report is generated in `output/report.html`.

### 3. Open the dashboard

```bash
figma-audit serve
```

Open http://localhost:8321 in a browser.

## CLI commands

### Pipeline

| Command | Description |
|---------|-------------|
| `figma-audit run` | Execute the full pipeline (6 phases) |
| `figma-audit run --from compare` | Resume from a phase (analyze, figma, match, capture, compare, report) |
| `figma-audit setup` | Interactive configuration (keys, DB, browser, daemon) |
| `figma-audit serve` | Start the web dashboard on port 8321 |

### Individual phases

Each phase can be executed independently. Phases read from and write to the `--output` directory (default: `./audit-results` or the YAML value).

| Command | Phase | Input | Output |
|---------|-------|-------|--------|
| `figma-audit analyze -p ~/dev/project` | 1 | Source code | `pages_manifest.json` |
| `figma-audit figma` | 2 | Figma URL + token | `figma_manifest.json` + PNGs |
| `figma-audit match` | 3 | Phase 1+2 manifests | `screen_mapping.yaml` |
| `figma-audit capture --app-url https://...` | 4 | Mapping + manifest | App screenshots |
| `figma-audit compare` | 5 | Screenshots + manifests | `discrepancies.json` |
| `figma-audit report` | 6 | Discrepancies | `report.html` |
| `figma-audit setup-test-data` | — | Manifest + live backend | `figma-audit.yaml` |

The `setup-test-data` command is an interactive agent (not part of the 6-phase pipeline). See [Agentic mode](#agentic-mode) and [Test setup configuration](#test-setup-configuration) below.

### Providing Figma screens

There are three ways to feed Figma screens into figma-audit. The `.fig` file and ZIP import are **recommended** — they are faster, work offline, and avoid the Figma API rate limits entirely.

| Method | Command | Figma token needed? | Notes |
|--------|---------|---------------------|-------|
| **.fig file** (recommended) | `figma-audit figma --figma-file design.fig` | No | Fastest. Parses the binary file locally. Also uploadable from the dashboard. |
| **ZIP export** | `figma-audit import-screens export.zip` | No | Export from Figma Desktop (File > Export). Contains PDFs converted to PNGs. Requires `pdftoppm`. |
| **Figma API** | `figma-audit figma` | Yes | Downloads screens via REST API. Subject to strict rate limits (~30 req/min, cooldown up to 48h). |

#### Using a .fig file

Download the `.fig` from Figma (File > Save local copy), then either:

```bash
# CLI
figma-audit figma --figma-file design.fig --output ./output

# Or via the dashboard: open the project page and use the "Upload .fig" button
```

#### Using a ZIP export

In Figma Desktop, select the frames you want, then File > Export. This produces a ZIP containing one PDF per frame. Import it with:

```bash
figma-audit import-screens export.zip --output ./output
```

#### Using the Figma API

Set `figma_url` and `figma_token` in your `figma-audit.yaml` or environment variables, then:

```bash
figma-audit figma --output ./output

# Options
figma-audit figma --offline          # Work only from local cache
figma-audit figma --force-refresh    # Force re-download from API
figma-audit figma --target-page "45:927"  # Limit to a specific Figma page
```

See [Figma rate limiting](#figma-rate-limiting) for details on caching and rate limit management.

## Web dashboard

The dashboard is accessible via `figma-audit serve` or by installing the daemon with `figma-audit setup`.

### Pages

- **Dashboard** (`/`): project overview with global statistics
- **Project** (`/projects/{slug}`): run timeline, statistics, button to launch a new run
- **Screen gallery** (`/projects/{slug}/screens`): all Figma screens with thumbnails, filtering by status (current/obsolete)
- **Run detail** (`/projects/{slug}/runs/{id}`): statistics, comparison table per screen, discrepancy list with filters
- **Comparison** (`/projects/{slug}/runs/{id}/compare/{page_id}`): side-by-side Figma vs Application with discrepancy list

### Interactive actions

All actions are executed in-place via htmx (no page reload):

- **Mark a discrepancy**: Ignore / Won't fix / Fixed
- **Mark a screen**: Current / Obsolete
- **Launch a run**: from the project page
- **Filter discrepancies**: by severity (Critical / Important / All)

### REST API

Full API documentation is available at `http://localhost:8321/docs` (auto-generated OpenAPI).

Main endpoints:

```
GET    /api/projects                              # List projects
POST   /api/projects                              # Create a project
GET    /api/projects/{slug}/runs                   # Run history
POST   /api/projects/{slug}/runs                   # Launch a run
GET    /api/projects/{slug}/screens                # Figma screens
PATCH  /api/projects/{slug}/discrepancies/{id}     # Update discrepancy status
POST   /api/projects/{slug}/discrepancies/{id}/annotate  # Annotate a discrepancy
```

## Project structure

```
figma_audit/
  __main__.py          # CLI (Click)
  config.py            # Pydantic configuration + YAML loading
  models.py            # Data models (FigmaScreen, FigmaManifest, etc.)
  phases/
    analyze_code.py    # Phase 1: source code analysis via Claude
    export_figma.py    # Phase 2: Figma export via REST API
    match_screens.py   # Phase 3: Figma/routes matching via Claude Vision
    capture_app.py     # Phase 4: Playwright capture + API seeding
    compare.py         # Phase 5: hybrid comparison
    report.py          # Phase 6: HTML report generation
  utils/
    claude_client.py   # Claude API client with token/cost tracking
    figma_client.py    # Figma REST client with rate limiting and cache
    color.py           # Color conversions and deltaE CIE2000
    progress.py        # CLI and web progress tracking
    checks.py          # Pre-execution checks
  db/
    models.py          # SQLModel tables (Project, Run, Screen, etc.)
    engine.py          # SQLite engine and session management
  api/
    app.py             # FastAPI factory
    deps.py            # Dependency injection
    routes/
      projects.py      # Projects CRUD
      runs.py          # Run management
      screens.py       # Screen management
      discrepancies.py # Discrepancy management
      htmx.py          # HTML fragments for htmx
      web.py           # Web page routes (Jinja2)
  web/
    static/            # htmx.min.js + style.css (dark theme)
    templates/         # Jinja2 templates (dashboard, project, run, etc.)
```

## Intermediate files

A run produces the following files in the output directory:

```
output/
  pages_manifest.json     # Phase 1: routes, pages, design tokens
  figma_raw/
    file.json             # Full Figma file tree (cache)
    file_meta.json        # Cache metadata
  figma_manifest.json     # Phase 2: screens, elements, tokens
  figma_screens/*.png     # Phase 2: Figma screen screenshots
  screen_mapping.yaml     # Phase 3: Figma <-> routes mapping (editable)
  app_screenshots/*.png   # Phase 4: application screenshots
  app_captures.json       # Phase 4: capture metadata
  discrepancies.json      # Phase 5: detected discrepancies
  report.html             # Phase 6: standalone HTML report
```

## Discrepancy management

Each detected discrepancy is classified by:

- **Severity**: `critical` (betrays the designer's intent), `important` (visible discrepancy), `minor` (subtle nuance)
- **Category**: LAYOUT, COLORS, TYPOGRAPHY, COMPONENTS, TEXT, SPACING, MISSING_ELEMENTS, ADDED_ELEMENTS, MISSING_DATA
- **Status**: `open`, `ignored`, `acknowledged`, `fixed`, `wontfix`

The `MISSING_DATA` category distinguishes discrepancies related to an empty application state (no test data) from actual design discrepancies.

## Advanced configuration

### Full figma-audit.yaml

```yaml
project: ~/dev/mon-projet
figma_url: "https://www.figma.com/design/XXXXXX/Mon-Projet"
app_url: "https://mon-app.example.com"
output: ./output

viewport:
  width: 390
  height: 844
  device_scale_factor: 1

figma:
  cache_dir: figma_raw
  request_delay: 3.0       # seconds between each API request
  batch_size: 8             # screens per export batch
  retry_wait_default: 60    # default wait on 429
  max_retries: 5

thresholds:
  color_delta_e: 5.0        # deltaE threshold for "same color"
  font_size_tolerance: 2    # pixel tolerance
  spacing_tolerance: 4      # pixel tolerance

seed_account:
  email: "test@example.com" # secondary account for creating data
  otp: "1234"
```

### Environment variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key (required) |
| `FIGMA_TOKEN` | Figma Personal Access token (required for online Phase 2) |
| `FIGMA_URL` | Figma file URL (alternative to YAML) |
| `APP_URL` | Application URL (alternative to YAML) |

Variables are automatically loaded from `~/.config/figma-audit/env`.

### Agentic mode

Phase 1 (code analysis) supports two modes:

| Mode | Cost | Speed | Accuracy | Activation |
|------|------|-------|----------|------------|
| **one-shot** (default) | ~$0.30 | ~2 min | Good for simple routers; can hallucinate DTO field names on complex apps | Default — no flag needed |
| **agentic** (opt-in) | ~$0.50-1.50 | ~5-10 min | Reads DTOs directly via `grep_code` + `read_file`; verifies auth guards in the router; produces correct `test_setup` payloads | See below |

In agentic mode, Claude iteratively explores the codebase using tools (`read_file`, `grep_code`, `list_files`, `ask_user`) instead of receiving a single 150KB prompt dump. This avoids the main failure mode of one-shot analysis: hallucinated field names and wrong `auth_required` flags.

#### Activating agentic mode

Three ways (CLI flag takes precedence over YAML, which takes precedence over env var):

```bash
# CLI flag (per-invocation)
figma-audit analyze --agentic
figma-audit run --agentic

# YAML config (persistent)
# In figma-audit.yaml:
analyze_mode: agentic

# Environment variable
export FIGMA_AUDIT_ANALYZE_MODE=agentic
```

**From the web dashboard**: check the **Agentic** checkbox next to the "Lancer un run" button on the project page. The agent's tool calls appear in real time in the run progress bar (e.g. `iter 3 · grep_code(AuthGuard)`).

#### The `setup-test-data` interactive agent

For cases where Phase 1's `test_setup` output is wrong (the most common symptom: `Item 1 failed (400)` in Phase 4 logs, followed by `Unresolved placeholder` errors), you can run the interactive `setup-test-data` agent instead of manually editing the YAML:

```bash
figma-audit setup-test-data
```

This agent:
1. Reads the existing `pages_manifest.json` to understand what endpoints need seeding
2. Explores the project codebase to find the correct request DTO field names
3. Builds candidate payloads and **tests them against the live backend** via HTTP
4. Iterates on 400 validation errors (reading the error body to fix fields)
5. Once every seed endpoint returns 2xx, writes the validated `test_setup` block to `figma-audit.yaml`

The command requires an interactive terminal (it may ask clarification questions via `ask_user`). Typical cost: ~$0.40. It replaces 30-60 minutes of manual YAML editing with a 2-minute automated conversation.

### Test setup configuration

For most pages, figma-audit just authenticates and navigates. But pages with path parameters (`/products/:id`, `/orders/:id`, `/profile/:userId`) cannot be reached without a valid entity ID, and pages whose state depends on backend data (a product in the "published" state, a paid order, etc.) need that data to exist before the screenshot is taken. The `test_setup` block tells Phase 4 how to log in via the API and seed those entities so the tool can build real URLs and capture real content.

#### How `test_setup` is normally generated

Phase 1 reads the project's API client / repository / service files (`*_repository.dart`, `*Service.kt`, `services/api*.ts`, etc.) and asks Claude to infer the auth flow and the seed payload. The result lands in `output/pages_manifest.json` under `test_setup`. Most of the time it works on the first try.

#### When Phase 1 gets it wrong

The AI can hallucinate field names that look plausible but do not match the real DTO. Symptoms in the logs:

```
[4/6] Capture app
  Setting up test data via API...
    API login OK (seed)
    Item 1 failed (400): {"type":"about:blank","title":"Validation Error",
       "detail": "category: Category is required, ..."}
  0 test item(s) created
```

…followed in the run page by captures marked **`Unresolved placeholder: product_id not in test_data`**. This is the placeholder guard refusing to navigate to a nonsense URL like `/products/placeholder_product_id`. It is the tool telling you the seed step did not produce a real ID.

When that happens, override `test_setup` in your `figma-audit.yaml`. The YAML override fully replaces the manifest version — it is not merged, so write the whole block.

#### Finding the right field names

Open the request DTO of the endpoint you want to seed (in the project being audited, not in figma-audit). For Flutter / Dart this is typically a `freezed` class:

```dart
// lib/features/catalog/data/models/product.dart
class CreateProductRequest with _$CreateProductRequest {
  const factory CreateProductRequest({
    required String title,
    required String description,
    required double priceCents,
    required String sku,
    required Category category,             // enum: ELECTRONICS, BOOKS, ...
    required DateTime publishAt,             // must be in the future
    @Default(0) int stockQuantity,
    ProductVisibility? visibility,
    String? notes,
  }) = _CreateProductRequest;
}
```

For other stacks: look for `RequestBody` classes in Spring/Kotlin, Pydantic models in FastAPI, Zod schemas in tRPC, etc. The serialized field names (often controlled by `@JsonValue` / `@JsonProperty` / `alias`) are what you must use in `payload`, not the language-side property names.

#### Full `test_setup` reference

```yaml
test_setup:
  # API login flow used both for seed_items and for the main browser session.
  auth_endpoint: /api/auth/login                # POST to verify credentials
  auth_otp_request_endpoint: /api/auth/request-otp  # OPTIONAL: called first if the
                                                    # backend uses passwordless OTP
  auth_payload:                                 # Body sent to auth_endpoint.
    phone: "${test_data.phone}"                 # ${test_data.X} is filled from the
    code: "${test_data.otp}"                    # test_data dict at runtime.
  auth_token_path: accessToken                  # Dotted path to the bearer token in
                                                # the response (e.g. "data.token").

  # Each entry creates one entity via API before the browser starts capturing.
  # The returned ID is injected into test_data under test_data_key, so any
  # ${test_data.<key>} placeholder in navigation_steps gets a real value.
  seed_items:
    - endpoint: /api/catalog/products           # The path to call (with /api prefix
                                                # if your backend uses one).
      method: POST
      payload:                                  # Must EXACTLY match the request DTO
        title: "figma-audit seed product"       # of your backend. Hard-coded values
        description: "Seed entity for audit"    # are fine for fixed fields.
        priceCents: 1999
        sku: "SEED-001"
        category: ELECTRONICS                   # Use the JSON value from the enum,
                                                # not the language-side name.
        publishAt: "${now+1d}"                  # Magic token: ISO-8601 UTC, 1 day
                                                # in the future. See "Template tokens"
                                                # below.
        stockQuantity: 10
        visibility: PUBLIC
      id_path: id                               # Where to find the ID in the
                                                # response (dotted path: "data.id").
      test_data_key: product_id                 # Key under which to store the ID
                                                # so navigation_steps can use
                                                # ${test_data.product_id}.

  # OPTIONAL: transition the seeded item into a different state for pages that
  # need a "published / paid / archived" variant.
  take_item:
    endpoint: /api/catalog/products/${product_id}/publish
    method: POST
    test_data_key: product_published_id

  # OPTIONAL: cleanup endpoint called once captures are done. Without this,
  # every run leaks a test entity into your backend.
  cleanup_endpoint: /api/catalog/products/${item_id}/archive
```

#### Template tokens

Two kinds of `${...}` placeholders are supported in `payload`, `endpoint`, and navigation URLs:

| Token | Resolves to | Use case |
|-------|-------------|----------|
| `${test_data.<key>}` | The string value at `test_data[<key>]` | Inject seeded IDs, credentials, etc. |
| `${<key>}` | Same as above (the `test_data.` prefix is optional) | Shorter form |
| `${now}` | ISO-8601 UTC timestamp at substitution time | Creation timestamps |
| `${now+1d}` | Now + 1 day | Future-dated fields like `desiredArrivalTime` |
| `${now-30m}` | Now - 30 minutes | Backdating |
| `${now+2h}` | Now + 2 hours | Short-term scheduling |

Suffixes for `${now±N<unit>}`: `s` seconds, `m` minutes, `h` hours, `d` days.

Hard-coding a date (`"2025-01-15T14:00:00Z"`) is the most common cause of intermittent test_setup failures — it works for a while, then the date drifts into the past and the backend starts rejecting it. Always use `${now+1d}` (or longer) for future-required fields.

#### Two test accounts: `test_credentials` vs `seed_account`

```yaml
test_credentials:                    # The MAIN user the browser logs in as.
  email: "tester@example.com"        # This is who sees the captured screens.
  otp: "1234"

seed_account:                        # A SECOND user used only by _setup_test_data
  email: "seeder@example.com"        # to create entities. Required when items
  otp: "1234"                        # created by user X are visible to user Y as
                                     # "available" — typical for marketplace apps.
```

If your app does not have this depositor/consumer split, omit `seed_account` and the seed step will use `test_credentials`.

#### The placeholder guard

If after `_setup_test_data` runs, any `test_data` value still contains a marker like `placeholder_*`, `todo_*`, `<TODO>`, `<REPLACE>`, or `xxxxxx`, the tool purges it and prints a warning. Any subsequent capture whose URL would have used that key fails with a clear `Unresolved placeholder: ...` error instead of silently navigating to a garbage URL. You will see those failures in the run page's **Navigation failures** card with the exact unresolved template.

This is by design: **the tool refuses to lie about what it captured**. If you see a placeholder error, fix the matching `seed_items` entry — do not chase the symptom in the comparison view.

## Figma rate limiting

The Figma API enforces strict limits on image exports (~30 req/min, with a cooldown that can reach 48h if exceeded). **This is why `.fig` file and ZIP imports are recommended** — they bypass the API entirely.

If you do use the API:

1. **Local cache**: the Figma file tree is downloaded once and cached in `figma_raw/file.json` (69 MB for a 77-screen file). Subsequent runs use the cache automatically.
2. **Offline mode**: `figma-audit figma --offline` works only from the cache — zero API calls.
3. **Force refresh**: `figma-audit figma --force-refresh` re-downloads everything (use sparingly).

If you hit the rate limit and get a 48h cooldown, switch to `.fig` file or ZIP import to keep working.

## Audit cost

The cost depends on the number of screens and the model used (Sonnet 4.5 by default):

| Phase | API calls | Typical cost |
|-------|-----------|--------------|
| Analyze (35 pages) | 1 | ~$0.17 |
| Match (70 screens) | 9 | ~$0.19 |
| Compare (62 pairs) | 62 | ~$1.43 |
| Report (summary) | 1 | ~$0.01 |
| **Total** | **73** | **~$1.80** |

The cost is displayed in real-time during execution and in the final summary.

## Development

```bash
git clone <your-repo-url>/figma-audit.git
cd figma-audit
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

# Tests
pytest tests/ -v

# Lint
ruff check figma_audit/
ruff format figma_audit/
```

## License

MIT
