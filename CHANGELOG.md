# Changelog

## [0.1.0] - 2026-04-07

### Added
- CLI pipeline with 6 phases: analyze, figma, match, capture, compare, report
- `figma-audit run` command for full pipeline execution with `--from` resume
- `figma-audit setup` interactive setup (API keys, Chromium, systemd/launchd daemon)
- `figma-audit serve` web dashboard on port 8321
- `figma-audit import-screens` to import Figma Desktop exports (ZIP with PDFs)
- FastAPI REST API with CRUD for projects, runs, screens, discrepancies
- Web UI with htmx: dashboard, project timeline, screen gallery, side-by-side comparison
- SQLite persistence (Project, Run, Screen, Capture, Discrepancy, Annotation)
- htmx inline actions: ignore/wontfix/fix discrepancies, mark screens obsolete
- Token usage and cost tracking per run
- Progress tracking for CLI and web UI
- Test data seeding via API with separate seed account
- Flutter CanvasKit authentication (coordinate-based input focus)
- DONNEES_ABSENTES category to reduce false criticals on empty states
- Obsolete screen exclusion from future comparisons
- Status filters (open/ignored/fixed) on discrepancy list
- Path traversal protection on file serving endpoint
- Build number from GitLab CI_PIPELINE_ID
- Auto-load API keys from ~/.config/figma-audit/env
- Pre-flight checks (Chromium installed, API keys configured)
- Dark theme consistent across report and dashboard
- 62 unit tests (color, config, models, API, htmx, path traversal, filters)
- GitLab CI pipeline (lint + test + build)
