# Contributing to figma-audit

Thanks for your interest in contributing! Here's how to get started.

## Development setup

```bash
git clone https://github.com/<your-username>/figma-audit.git
cd figma-audit
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

## Running tests

```bash
# Full test suite
pytest tests/ -v

# Stop on first failure
pytest tests/ -x

# Single test file
pytest tests/test_agent_tools.py -v
```

## Code style

We use [ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
# Check for issues
ruff check figma_audit/

# Auto-fix what can be fixed
ruff check --fix figma_audit/

# Check formatting
ruff format --check figma_audit/

# Auto-format
ruff format figma_audit/
```

All code must pass `ruff check` and `ruff format --check` before merging.

## Code conventions

- Python 3.11+, type hints on all public functions
- Pydantic for data models, dataclasses for internal state
- `async` for I/O-bound operations (Playwright, HTTP)
- `rich` for console output
- Tests use `pytest` with `tmp_path` fixtures for filesystem isolation

## Project structure

```
figma_audit/
├── phases/          # The 6 pipeline phases + setup-test-data agent
├── utils/           # Shared utilities (Claude client, agent loop, tools)
├── api/routes/      # FastAPI routes (REST API + web UI)
├── web/templates/   # Jinja2 HTML templates
├── web/static/      # CSS + htmx
├── db/              # SQLModel models + engine
└── __main__.py      # CLI entry point (click)
```

## Submitting changes

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-change`)
3. Make your changes
4. Run tests and lint (`pytest tests/ && ruff check figma_audit/`)
5. Commit with a clear message describing the **why**, not just the what
6. Open a pull request against `master`

## Reporting issues

Open an issue on GitHub with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- The run number and any relevant logs from the dashboard

## Architecture decisions

If your change touches the pipeline architecture, the agentic loop, or the
comparison prompt, please open an issue first to discuss the approach. These
areas have subtle interactions that benefit from upfront discussion.

## License

By contributing, you agree that your contributions will be licensed under the
[MIT License](LICENSE).
