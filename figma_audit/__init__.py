"""figma-audit: Semantic comparison between Figma designs and deployed web applications."""

import subprocess

__version__ = "0.1.0"


def get_build_info() -> str:
    """Return version string with build number from git, e.g. '0.1.0+21'."""
    try:
        count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        return f"{__version__}+{count}"
    except Exception:
        return __version__
