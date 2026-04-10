"""Phase 6: Report -- Generate standalone HTML report from discrepancies."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader
from rich.console import Console

from figma_audit.config import Config
from figma_audit.utils.claude_client import ClaudeClient

console = Console()

SUMMARY_SYSTEM_PROMPT = """\
You are a UI/UX audit report writer. Generate an executive summary in English for a Figma \
design conformity audit. Be concise (3-5 sentences), highlight the most important findings, \
and give actionable prioritized recommendations.

Output plain text only, no JSON, no markdown.
"""


def _encode_image_b64(path: Path) -> str | None:
    """Encode an image file to base64 string."""
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _generate_executive_summary(
    comparisons: list[dict],
    statistics: dict,
    api_key: str | None,
) -> str:
    """Generate executive summary using AI, or a fallback text."""
    # Build a text summary of findings
    findings = []
    for comp in comparisons:
        fidelity = comp.get("overall_fidelity", "?")
        n_disc = len(comp.get("discrepancies", []))
        findings.append(
            f"- {comp['figma_screen']} ({comp['route']}): "
            f"{fidelity}, {n_disc} discrepancies"
        )

    stats = statistics
    prompt = (
        f"Here are the results of a Figma vs deployed application conformity audit:\n\n"
        f"Screens compared: {stats.get('total_screens', 0)}\n"
        f"Total discrepancies: {stats.get('total_discrepancies', 0)}\n"
        f"Critical: {stats.get('by_severity', {}).get('critical', 0)}, "
        f"Important: {stats.get('by_severity', {}).get('important', 0)}, "
        f"Minor: {stats.get('by_severity', {}).get('minor', 0)}\n\n"
        f"Detail per screen:\n" + "\n".join(findings) + "\n\n"
        "Most affected categories: "
        + ", ".join(
            f"{k} ({v})"
            for k, v in sorted(
                stats.get("by_category", {}).items(),
                key=lambda x: -x[1],
            )[:5]
        )
        + "\n\n"
        "Note: several screens protected by authentication "
        "could not be captured "
        "(redirect to welcome). Only public screens "
        "were actually compared."
    )

    if api_key:
        try:
            client = ClaudeClient(api_key=api_key)
            result = client.client.messages.create(
                model=client.model,
                max_tokens=1024,
                temperature=0.3,
                system=SUMMARY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return result.content[0].text
        except Exception as e:
            console.print(f"  [yellow]AI summary failed, using fallback: {e}[/yellow]")

    # Fallback
    total = stats.get("total_discrepancies", 0)
    critical = stats.get("by_severity", {}).get("critical", 0)
    return (
        f"Conformity audit on {stats.get('total_screens', 0)} screens. "
        f"{total} discrepancies detected including {critical} critical. "
        f"Most critical discrepancies come from authentication-protected pages "
        f"that could not be captured correctly."
    )


def run(config: Config) -> Path:
    """Run Phase 6: Generate HTML report.

    Returns:
        Path to the generated report.html.
    """
    output_dir = config.output_dir
    discrepancies_path = output_dir / "discrepancies.json"

    if not discrepancies_path.exists():
        raise FileNotFoundError("discrepancies.json not found. Run Phase 5 first.")

    with open(discrepancies_path) as f:
        data = json.load(f)

    comparisons = data.get("comparisons", [])
    statistics = data.get("statistics", {})

    console.print(f"[bold]Generating report for {len(comparisons)} comparisons[/bold]")

    # Enrich comparisons with base64 images and severity counts
    for comp in comparisons:
        figma_img = comp.get("figma_image")
        app_img = comp.get("app_image")
        comp["figma_image_b64"] = _encode_image_b64(output_dir / figma_img) if figma_img else None
        comp["app_image_b64"] = _encode_image_b64(output_dir / app_img) if app_img else None

        severity_counts = {"critical": 0, "important": 0, "minor": 0}
        for d in comp.get("discrepancies", []):
            sev = d.get("severity", "minor")
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
        comp["severity_counts"] = severity_counts

    # Max category count for bar chart scaling
    max_category = max(statistics.get("by_category", {}).values(), default=1)
    statistics["max_category"] = max_category

    # Generate executive summary
    console.print("  Generating executive summary...")
    executive_summary = _generate_executive_summary(
        comparisons, statistics, config.anthropic_api_key
    )

    # Load figma manifest for file name
    figma_manifest_path = output_dir / "figma_manifest.json"
    file_name = "Unknown"
    if figma_manifest_path.exists():
        with open(figma_manifest_path) as f:
            fm = json.load(f)
            file_name = fm.get("file_name", "Unknown")

    # Render template
    template_dir = Path(__file__).parent.parent.parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    template = env.get_template("report.html.j2")

    html = template.render(
        file_name=file_name,
        date=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        executive_summary=executive_summary,
        stats=statistics,
        comparisons=comparisons,
    )

    report_path = output_dir / "report.html"
    with open(report_path, "w") as f:
        f.write(html)

    size_mb = report_path.stat().st_size / 1024 / 1024
    console.print(f"\n[bold green]Report saved to {report_path} ({size_mb:.1f} MB)[/bold green]")
    console.print(f"  Open in browser: file://{report_path.resolve()}")

    return report_path
