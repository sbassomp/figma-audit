"""figma-audit: Semantic comparison between Figma designs and deployed web applications."""

import subprocess
from pathlib import Path

__version__ = "0.2.1"


def get_build_info() -> str:
    """Return version string with build number, e.g. '0.1.0+1720'.

    Uses CI_PIPELINE_ID in CI, or queries GitLab API locally, or falls back to git commit count.
    """
    import os

    # 1. CI environment
    pipeline_id = os.environ.get("CI_PIPELINE_ID")
    if pipeline_id:
        return f"{__version__}+{pipeline_id}"

    # 2. Cached build number (written by CI or previous lookup)
    cache_file = Path.home() / ".config" / "figma-audit" / "build_number"
    if cache_file.exists():
        return f"{__version__}+{cache_file.read_text().strip()}"

    # 3. Fallback to git commit count
    try:
        count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        return f"{__version__}+{count}"
    except Exception:
        return __version__  # Not in a git repo
