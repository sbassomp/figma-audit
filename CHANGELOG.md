# Changelog

## [0.2.1] - 2026-04-19

Maintenance release.

- API: the JSON run dispatcher now hydrates `Config` from `project.config_yaml`, so runs started via the REST API see the same configuration as runs started from the CLI or web UI.
- Docs: replaced the last `Course`/`MedCourses` reference in the README Flutter bridge example with the generic `Order` vocabulary used everywhere else.
- Tooling: `scripts/sync_to_github.sh` runs gitleaks + token-pattern grep on the commit range, tracked files and `.git/config` before fast-forwarding the public GitHub mirror.
- CI: ruff format fix on `validator.py`.

## [0.2.0] - 2026-04-16

Second public release. The headline additions are **multi-actor test setups** (one audit can log in as several accounts and have them interact via API before capture), **stateful captures** (a single page can produce one screenshot per tab/filter/wizard step), and a much tighter **Flutter Web integration** path.

### Multi-actor test setup

- New `test_setup` schema with named `accounts`, `default_viewer`, and an explicit DAG of `steps`. A seller account can create a listing, a buyer can place an order, the browser can then capture the listing from the buyer's perspective — all from one YAML block.
- Auto-migration of the legacy `seed_items` / `take_item` shape into the new schema so existing configs keep running.
- `figma-audit setup-test-data` CLI to iterate on the DAG without rerunning the full pipeline.
- Pre-authenticates every account in parallel and routes each step to the correct token via `AgentContext`.
- `http_request` tool in agentic mode takes an `as: <role>` argument.
- Each account's user id is extracted from the JWT `sub` claim and exposed as `${<role>_user_id}`, plus `${user_id}` and `${default_viewer_user_id}` aliases pointing at the default viewer.
- Phase 1 is taught to distinguish user roles from domain enums so agentic runs do not invent fake roles from `CourseType`-like values.
- Phase 4 runs the seed DAG before capture and tags every screenshot with the viewer role.

### Stateful captures

- Every page can declare multiple `capturable_states`. Two navigation styles are supported: `query` (preferred, merges params into the current URL for a fresh navigation — ideal for tabs and filters) and `delta_steps` (click/fill primitives from the previous state — for wizards and modals).
- The `state_id` is threaded end-to-end: Phase 3 matches Figma variants per state, the capture DB records one row per (page, state), and Phase 5 compares the right screenshot against the right Figma variant.
- Phase 3 runs a cross-batch disambiguation pass so two independently-matched states cannot collapse onto the same id.
- Click steps can target the Nth semantic element in the content area (`{"index": 0, "min_y": 80}`) to click the first real list tile and skip the app bar.

### Flutter Web integration

- New integration guide: `docs/integrations/flutter/INTEGRATION.md` + README section covering the two opt-in changes required for full coverage.
- `SemanticsBinding.instance.ensureSemantics()` enables Flutter's accessibility tree so Playwright can drive CanvasKit apps via `getByRole` and `getByLabel`.
- `figma_audit_bridge.dart` (≈50 lines, copy-paste) exposes `window.figmaAudit.push(route, extraJson)` so figma-audit can reach pages that receive a GoRouter `extra` object. New `bridge_push` navigation step uses it.
- Semantics-first login replaces the old coordinate-based login flow.
- Phase 1 prompts learned the `bridge_push` step, the detail-after-list reach pattern, and scenario-based `reach_paths` (one entry per user journey to a given page, the runner picks the one that matches the current auth state).
- New `docs/integrations/flutter/audits/` folder with four drop-in prompts a coding assistant can run on a Flutter project to reach audit-readiness: `context.go` vs `context.push`, stateful URLs for tabs and filters, wizard steps in URL, and Semantics on custom tappable widgets.

### Reliability and honesty

- New Phase 1 manifest validator catches literal `:id` in navigation URLs, unknown `${X_user_id}` templates, `auth_required=false` pages whose only reach_path requires a session (auto-fixed), and duplicate page / state ids — reported right after analyze instead of several phases later.
- Phase 4 refuses to screenshot when a critical navigation step fails, so no more silent captures of the wrong page under a valid URL.
- Unresolved `${...}` placeholders, literal `:param` URLs, and test-style placeholder strings (`placeholder_`, `todo_`, `sample-`, `test-`, `example-`, etc.) are detected and rejected with an actionable error.
- `_assert_url_resolved` runs before every navigation.

### Web dashboard

- Project page header redesign with clearer hierarchy and styled file inputs.
- Phase 3 reports per-screen progress for a determinate progress bar.

### Misc

- Replaced the remaining pilot-project vocabulary (`MedCorp`, `MedCourses`, `ambulance`/`taxi`/`vsl`) with generic e-commerce examples in prompts, tests and README.
- Removed em-dashes from README and prose.
- New `/examples/` demo app scaffolding (not installed via pip, source-only).
- Documentation: README grew a full Flutter integration section and an "Make your app audit-ready" subsection pointing to the four audit prompts.

## [0.1.0] - 2026-04-13

First public release.

### Pipeline

- 6-phase pipeline: **analyze → figma → match → capture → compare → report**
- `figma-audit run` command for full pipeline execution with `--from <phase>` resume
- Each phase can also run independently from its own CLI command
- `figma-audit setup` interactive first-run (API keys, Chromium, systemd/launchd daemon)
- `figma-audit serve` web dashboard on port 8321

### Phase 1 — Analyze code

- Automatic framework detection (Flutter, React, Vue, Angular, Next.js)
- Extracts routes, pages, auth guards, design tokens via Claude AI
- **Two modes**:
  - **one-shot** (default, ~$0.30): sends all source files in a single prompt
  - **agentic** (opt-in, ~$0.50-1.50): Claude iteratively explores the codebase
    via `read_file`, `grep_code`, `list_files` tools — produces more accurate
    DTO field names, auth flags, and `test_setup` payloads
- Agentic mode activation: `--agentic` flag, `analyze_mode: agentic` in YAML,
  `FIGMA_AUDIT_ANALYZE_MODE=agentic` env var, or checkbox in the web UI
- Model selection (Sonnet/Opus) for agentic mode via `analyze_model` config
  or the web UI dropdown

### Phase 2 — Figma export

- `.fig` file parsing (offline, vendored Kiwi decoder) — recommended
- ZIP import from Figma Desktop (File → Export frames to PDF)
- Figma REST API with local cache and rate limiting management
- Design token extraction (colors, fonts, spacing, border-radius)

### Phase 3 — Match screens

- Claude Vision matches Figma screens to application routes
- Output is a human-reviewable `screen_mapping.yaml` with `state_id` support
- Obsolete screen filtering

### Phase 4 — Capture app

- Playwright automation with Flutter CanvasKit support (coordinate-based
  fallback for DOM-less apps)
- Multi-strategy click/fill: CSS selector → accessibility role → text → coordinates
- Multi-state capture via `delta_steps` for wizards and tab switches
- Pre-login / post-login capture split (public pages before auth, protected
  pages after)
- API-driven test data seeding via `test_setup.seed_items` / `take_item` /
  `cleanup_endpoint` with runtime `/api` prefix fallback
- `${test_data.X}` and `${now+1d}` template tokens in navigation URLs and
  seed payloads
- **Placeholder guard**: URLs containing `placeholder_*`, `todo_*`, or
  unresolved `${...}` markers are refused with a clear error instead of
  silently navigating to nonsense routes
- **Global post-capture dedup**: hashes every screenshot and flags silent
  navigation failures (pages that redirected to the same fallback screen)
- Landed URL tracked and surfaced in the comparison view

### Phase 5 — Compare

- Claude Vision comparison across 9 categories (layout, colors, typography,
  components, texts, spacing, missing elements, added elements, data gaps)
- Zero-tolerance stance on background colors, border shapes, and global
  element positions
- Severity scale: critical / important / minor, with bias toward "important"
  on ambiguity
- State-aware lookup: wizard step N vs step N+1 can have different
  matching Figma screens

### Phase 6 — Report

- Standalone HTML report with side-by-side images, dark theme, severity
  filters, and an executive AI-generated summary

### Interactive agent — `figma-audit setup-test-data`

- Explores the audited project to find the correct request DTOs
- Builds candidate payloads and validates them against the **live backend**
  via HTTP before writing
- Iterates on 400 validation errors until every seed endpoint returns 2xx
- Writes a verified `test_setup` block to `figma-audit.yaml`
- Requires an interactive terminal

### Web dashboard

- htmx-powered UI with project tracking, run history, screen gallery
- Per-run: capture success/failure breakdown, navigation failures card,
  execution details (per-phase cost and tokens), side-by-side comparison view
- **Trend badges** on run stat cards comparing against the previous
  completed run
- Discrepancy management: ignore / won't fix / mark fixed / annotate
- Per-discrepancy fix prompt generation
- Agentic Phase 1 toggle + model selector on the project page
- Real-time progress polling during runs
- systemd (Linux) / launchd (macOS) daemon installation for a permanent
  dashboard

### REST API

- FastAPI endpoints for projects, runs, screens, discrepancies, annotations
- SQLite persistence (lightweight migrations for schema evolution)

### Configuration

- `figma-audit.yaml` with viewport, thresholds, test credentials, seed
  account, test_setup override
- Auto-load API keys from `~/.config/figma-audit/env`
- YAML overrides take priority over AI-generated manifest values

### Internal

- 224 unit tests covering agentic infrastructure, Phase 1 helpers,
  TokenUsage, progress tracker, config, checks, htmx endpoints
- ruff check + ruff format CI gates via GitHub Actions
- Pure English codebase and prompts
- Fully generic — no app-specific code
