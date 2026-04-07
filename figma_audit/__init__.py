"""figma-audit: Semantic comparison between Figma designs and deployed web applications."""

import subprocess

__version__ = "0.1.0"


def get_build_info() -> str:
    """Return version string with build number from git, e.g. '0.1.0+20.07bd858'."""
    try:
        count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        short_hash = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        return f"{__version__}+{count}.{short_hash}"
    except Exception:
        return __version__
