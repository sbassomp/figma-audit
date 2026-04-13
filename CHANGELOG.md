# Changelog

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
