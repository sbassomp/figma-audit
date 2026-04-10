# Compliance Audit - figma-audit

**Date**: 2026-04-07
**Language**: Python 3.11+
**Framework**: FastAPI + Click + Playwright + SQLModel
**Source files**: 35 files (7,300 lines)
**Test files**: 4 files (47 tests)

## Summary

| Priority | Code+Security | Accessibility | Total |
|----------|---------------|---------------|-------|
| Critical | 3 | 1 | 4 |
| Important | 5 | 2 | 7 |
| Minor | 4 | 4 | 8 |

---

## Overall Scores

| Category | Score | Status |
|----------|-------|--------|
| **Code Score** | **62/100** | Action required |
| **Security Score** | **65/100** | Action required (< 70 = deployment blocked) |
| **Accessibility Score** | **58/100** | Action required |
| **Average** | **62/100** | Action required |

### Code Sub-scores

| Code Category | Score |
|---------------|-------|
| File size | 5/10 |
| SOLID | 5/10 |
| Patterns/Naming | 8/10 |
| Dead code | 9/10 |
| Duplication | 7/10 |
| Tests | 3/10 |
| Error handling | 4/10 |
| Readability | 7/10 |

### Security Sub-scores (OWASP)

| OWASP Category | Score |
|----------------|-------|
| A01 - Broken Access Control | 20/100 |
| A02 - Cryptographic Failures | 85/100 |
| A03 - Injection | 95/100 |
| A05 - Security Misconfiguration | 60/100 |

---

## Critical violations (3)

### C1. No authentication on the REST API
**File**: `figma_audit/api/app.py`, `figma_audit/api/deps.py`
**Rule**: OWASP A01 - Broken Access Control
**Problem**: All API endpoints are accessible without authentication. Anyone with access to port 8321 can delete projects, modify discrepancies, and download files.

**Action required**: Implement Bearer token authentication (environment variable `API_TOKEN`) with a FastAPI middleware. Minimum viable: a static token verified by a `Depends(verify_token)`.

### C2. 13 silent exceptions (except: pass)
**Files**: `phases/compare.py:220`, `phases/capture_app.py:34,49,206,210`, `__main__.py:243,308,573,605`, `api/routes/web.py:198`
**Rule**: Error handling
**Problem**: 13 `except Exception: pass` blocks silently swallow errors. DB failures, Playwright failures, and subprocess failures are invisible.

```python
# Current
except Exception:
    pass

# Expected
except Exception as e:
    console.print(f"[yellow]Warning: {e}[/yellow]")
```

**Action required**: Replace each `pass` with a `console.print` or `logger.warning` call. Never swallow an exception without a trace.

### C3. Test coverage at 11% (4 modules out of 35)
**File**: `tests/`
**Rule**: Tests
**Problem**: Only config, color, models, and basic API are tested. The 6 phases (core business logic), the API clients (Claude, Figma), the htmx routes, the progress tracking -- nothing is tested.

**Action required**: Prioritize phase tests with mocks (Claude/Figma mocked), then htmx route tests (return HTML).

---

## Important violations (5)

### I1. __main__.py is 728 lines (threshold: 500)
**File**: `figma_audit/__main__.py`
**Rule**: File size, Single Responsibility
**Problem**: The file contains CLI commands, screen import logic, daemon setup, and systemd/launchd installation. 4 distinct responsibilities.

**Action required**: Extract into modules:
- `figma_audit/cli/commands.py` (phase commands)
- `figma_audit/cli/setup.py` (interactive setup + daemon)
- `figma_audit/cli/import_screens.py` (screen import)

### I2. web.py is 596 lines with mixed business logic
**File**: `figma_audit/api/routes/web.py`
**Rule**: Single Responsibility
**Problem**: Web routes contain run creation logic, file import logic, and complex DB queries. The background task `_run_pipeline_bg` (80 lines) is in the routes file.

**Action required**: Extract the background task into `figma_audit/api/tasks.py` and complex queries into a service layer.

### I3. capture_app.py is 555 lines with functions > 30 lines
**File**: `figma_audit/phases/capture_app.py`
**Rule**: Method size
**Problem**: `_run_async` is ~80 lines, `_setup_test_data` is ~70 lines, `_flutter_login` is ~50 lines.

**Action required**: Break down into smaller functions (login, navigation, screenshot, cleanup).

### I4. File endpoints without path validation
**File**: `figma_audit/api/app.py:45-58`
**Rule**: OWASP A03 - Path Traversal
**Problem**: The `/files/{slug}/{path:path}` endpoint serves files from `output_dir`. A `path` like `../../etc/passwd` could potentially be exploited.

```python
file_path = Path(project.output_dir).expanduser().resolve() / path
```

**Action required**: Verify that the resolved path stays within `output_dir`:
```python
resolved = (output_dir / path).resolve()
if not str(resolved).startswith(str(output_dir.resolve())):
    return Response(status_code=403)
```

### I5. No input validation on API endpoints
**File**: `figma_audit/api/routes/screens.py`, `discrepancies.py`
**Rule**: Input validation
**Problem**: The submitted statuses (`open`, `ignored`, `fixed`, etc.) are validated in code but the htmx endpoints (`/htmx/.../status/{new_status}`) lack Pydantic validation. An arbitrary status could be injected.

**Action required**: Use a `Literal["open", "ignored", "fixed", "wontfix"]` in the routes.

---

## Minor violations (4)

### M1. Nesting > 3 levels in export_figma.py
**File**: `figma_audit/phases/export_figma.py:93-96`
**Rule**: Nesting depth
**Problem**: Nested loops for traversing Figma elements.

**Action required**: Extract the fill/color extraction logic into a dedicated function.

### M2. No named constants for magic strings
**Files**: Multiple
**Rule**: Named constants
**Problem**: The statuses `"open"`, `"current"`, `"obsolete"`, `"completed"`, `"running"` are repeated string literals. Not severe duplication but risk of typos.

**Action required**: Create a `figma_audit/constants.py` module with Enums or constants.

### M3. Duplicated logic in htmx and web routes
**File**: `figma_audit/api/routes/htmx.py`, `web.py`
**Rule**: DRY
**Problem**: Discrepancy card rendering is duplicated between the Jinja2 template (`run.html`) and the Python function (`_disc_card_html`).

**Action required**: Use a Jinja2 partial template included via `{% include %}` by both the template and the htmx endpoint.

### M4. Global mutable `_engine` in db/engine.py
**File**: `figma_audit/db/engine.py:9`
**Rule**: Patterns
**Problem**: Global mutable variable `_engine = None` for the DB singleton. Not thread-safe in a multi-threaded FastAPI context.

**Action required**: Use a thread-safe pattern or pass the engine via FastAPI state.

---

## Accessibility Section (WCAG 2.1 AA) - Score: 58/100

**Scope**: 9 HTML templates
**Standards**: WCAG 2.1 Level AA

### Critical accessibility violations (1)

**AC1. Action buttons without aria-label**
**Ref**: WCAG 4.1.2 Name, Role, Value
**Files**: `run.html:104-106`, `comparison.html:63-65`, `htmx.py:28-38`
**Problem**: The "Ignore", "Won't fix", "Fixed" buttons have no aria-label. Their function depends on visual context (the parent card) which is not accessible to screen readers.
**Action required**: Add `aria-label="Ignore discrepancy: {{ d.description[:50] }}"` to each button.

### Important accessibility violations (2)

**AI1. No skip navigation**
**Ref**: WCAG 2.4.1 Bypass Blocks
**File**: `base.html`
**Problem**: No "Skip to main content" link to bypass the sidebar.
**Action required**: Add `<a href="#main" class="skip-link">Skip to content</a>` with CSS to visually hide it.

**AI2. Forms without aria-required**
**Ref**: WCAG 3.3.2 Labels or Instructions
**File**: `new_project.html:13`
**Problem**: The `name` field has `required` but not `aria-required="true"`.
**Action required**: Add `aria-required="true"` to all required fields.

### Minor accessibility violations (4)

**AM1.** Dynamic alt text potentially empty (`screens.html:27`, `comparison.html:23,33`)
**AM2.** Sidebar `<nav>` without `aria-label="Main navigation"` (`base.html`)
**AM3.** No `role="main"` on `<main>` (implicit but better to be explicit)
**AM4.** Color contrast not verified (custom CSS variables)

### Accessibility good practices

- `<html lang="fr">` present on all templates
- `<meta name="viewport">` correctly configured
- Heading hierarchy h2/h3 respected
- `<label for="">` labels associated with form inputs
- Tables with `<thead>` and `<th>` for headers

---

## Positive Points

### Architecture & Code
- Clear and modular package structure (phases/, utils/, api/, db/, web/)
- Pydantic v2 for data validation
- SQLModel for ORM (zero friction with existing Pydantic models)
- Well-structured Click CLI with auto-discovery of YAML config
- Consistent dark theme between the HTML report and the web dashboard
- Resumable pipeline (`--from phase`)
- Token/cost tracking per run

### Security
- No hardcoded secrets in source code
- API keys loaded from `~/.config/figma-audit/env` with chmod 600
- SQLModel ORM = no SQL injection possible
- subprocess used with argument lists (no shell=True)
- `.gitignore` correct (excludes .db, .env, output/)

### Infrastructure
- Functional GitLab CI/CD (lint + test + build)
- Build number aligned with the GitLab instance (CI_PIPELINE_ID)
- Automated systemd/launchd daemon installation

---

## Remediation Plan

### Sprint 1 - Security (blocking)
1. **API authentication**: Bearer token on all endpoints (C1)
2. **Path traversal**: Path validation in the `/files/` endpoint (I4)
3. **Status validation**: Literal types on htmx endpoints (I5)

### Sprint 2 - Robustness (priority)
1. **Exception logging**: Replace the 13 `except: pass` blocks (C2)
2. **Phase tests**: Add tests with mocks for at least compare.py and match_screens.py (C3)
3. **htmx API tests**: Verify that HTML fragments are valid (C3)

### Sprint 3 - Refactoring (improvement)
1. **Split __main__.py**: Extract setup, import_screens, daemon (I1)
2. **Extract the background task** from web.py (I2)
3. **Split capture_app.py** into functions < 30 lines (I3)

### Sprint 4 - Accessibility (improvement)
1. **aria-label on action buttons** (AC1)
2. **Skip navigation** in base.html (AI1)
3. **aria-required** on forms (AI2)

---

## Metrics

| Metric | Value | Threshold | Status |
|--------|-------|-----------|--------|
| Files > 500 lines | 3 | 0 | :x: |
| Functions > 30 lines | ~8 | 0 | :x: |
| Tests | 47 | - | OK |
| Tests passing | 100% | 100% | :white_check_mark: |
| Module coverage | 11% | > 50% | :x: |
| Silent except pass | 13 | 0 | :x: |
| Hardcoded secrets | 0 | 0 | :white_check_mark: |
| Endpoints without auth | 15+ | 0 | :x: |
| Path traversal possible | 1 | 0 | :x: |
| CI/CD | OK | - | :white_check_mark: |
| Lint (ruff) | 0 errors | 0 | :white_check_mark: |

---

## Audit History

| Date | Code | Security | Accessibility | Evolution |
|------|------|----------|---------------|-----------|
| 2026-04-07 | 62/100 | 65/100 | 58/100 | Initial audit - 7300 lines, 47 tests, CI OK |
