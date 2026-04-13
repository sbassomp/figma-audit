"""File discovery + framework detection helpers shared by both Phase 1 modes.

Pure I/O over the project filesystem — no Claude calls, no network. This is
the layer the unit tests in ``test_analyze_code_helpers.py`` cover.
"""

from __future__ import annotations

from pathlib import Path

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
        # Auth guards / redirect callbacks live alongside or near the router
        "**/auth_guard*.dart",
        "**/auth_redirect*.dart",
        "**/route_guard*.dart",
        "**/router_guard*.dart",
        "**/auth_notifier*.dart",
        "**/auth_state*.dart",
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

# Patterns to find API client / service / repository files
API_PATTERNS = {
    "flutter": [
        "**/api/**/*.dart",
        "**/service/**/*.dart",
        "**/services/**/*.dart",
        "**/repository/**/*.dart",
        "**/repositories/**/*.dart",
        "**/data/**/*_repository.dart",
        "**/data/**/*_service.dart",
        "**/data/**/*_client.dart",
        "**/data/remote/**/*.dart",
        "**/data/datasource/**/*.dart",
        "**/client/**/*.dart",
    ],
    "react": [
        "**/api/**/*.{ts,tsx,js,jsx}",
        "**/services/**/*.{ts,tsx,js,jsx}",
        "**/hooks/use*Api*.{ts,tsx}",
        "**/lib/api*.{ts,js}",
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
# Max total characters to send in the one-shot prompt
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
    """Find files matching glob patterns, sorted by path.

    Excludes generated files (``.g.dart``, ``.freezed.dart``), build
    artifacts (``build/``, ``.dart_tool/``, ``node_modules/``), and tests.
    """
    files: set[Path] = set()
    for pattern in patterns:
        files.update(project_dir.glob(pattern))
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
    """Read a file, returning ``None`` if too large or unreadable."""
    try:
        content = path.read_text(encoding="utf-8")
        if len(content) > max_size:
            return content[:max_size] + f"\n\n[... truncated at {max_size} chars ...]"
        return content
    except (OSError, UnicodeDecodeError):
        return None
