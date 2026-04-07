"""figma-audit: Semantic comparison between Figma designs and deployed web applications."""

import subprocess
from pathlib import Path

__version__ = "0.1.0"


def get_build_info() -> str:
    """Return version string with build number, e.g. '0.1.0+1720'.

    Uses CI_PIPELINE_ID in CI, or queries GitLab API locally, or falls back to git commit count.
    """
    import os

    # 1. CI environment
    pipeline_id = os.environ.get("CI_PIPELINE_ID")
    if pipeline_id:
        return f"{__version__}+{pipeline_id}"

    # 2. Query GitLab API (cached in ~/.config/figma-audit/build_number)
    cache_file = Path.home() / ".config" / "figma-audit" / "build_number"
    token_file = Path.home() / ".config" / "gitlab-token"
    if token_file.exists():
        try:
            import requests

            token = token_file.read_text().strip()
            resp = requests.get(
                "https://git.gardendwarf.org/api/v4/projects/49/pipelines?per_page=1",
                headers={"PRIVATE-TOKEN": token},
                timeout=3,
            )
            if resp.status_code == 200:
                pipelines = resp.json()
                if pipelines:
                    build_num = str(pipelines[0]["id"])
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(build_num)
                    return f"{__version__}+{build_num}"
        except Exception:
            pass

    # 3. Cached value
    if cache_file.exists():
        return f"{__version__}+{cache_file.read_text().strip()}"

    # 4. Fallback to git commit count
    try:
        count = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
        return f"{__version__}+{count}"
    except Exception:
        return __version__
