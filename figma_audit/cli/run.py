"""``figma-audit run`` — orchestrates all 6 phases sequentially.

Mirrors the web-side ``_run_pipeline_bg`` but runs in the foreground with
rich progress output, skips Phase 1/3 if their outputs already exist, and
honors ``--from`` / ``--agentic`` / ``--offline`` flags.
"""

from __future__ import annotations

import sys

import click

from figma_audit.cli.group import _find_config, cli, console
from figma_audit.config import Config
from figma_audit.utils.checks import check_api_keys, check_playwright_browser, load_env_file

PHASE_ORDER = ["analyze", "figma", "match", "capture", "compare", "report"]
PHASE_NAMES = {
    "analyze": "Phase 1: Analyze code",
    "figma": "Phase 2: Export Figma",
    "match": "Phase 3: Match screens",
    "capture": "Phase 4: Capture app",
    "compare": "Phase 5: Compare",
    "report": "Phase 6: Report",
}


def _get_last_client(phase_name: str):
    """Retrieve the ClaudeClient exposed by a phase module after run()."""
    _phase_modules = {
        "analyze": "figma_audit.phases.analyze_code",
        "analyze_code": "figma_audit.phases.analyze_code",
        "match": "figma_audit.phases.match_screens",
        "match_screens": "figma_audit.phases.match_screens",
        "compare": "figma_audit.phases.compare",
    }
    import sys as _sys

    mod_name = _phase_modules.get(phase_name, "")
    mod = _sys.modules.get(mod_name)
    if mod:
        return getattr(mod, "_last_client", None)
    return None


def _count_pages(cfg: Config) -> int:
    import json

    path = cfg.output_dir / "pages_manifest.json"
    if path.exists():
        with open(path) as f:
            return len(json.load(f).get("pages", []))
    return 0


def _count_screens(cfg: Config) -> int:
    import json

    path = cfg.output_dir / "figma_manifest.json"
    if path.exists():
        with open(path) as f:
            return len(json.load(f).get("screens", []))
    return 0


def _count_captures(cfg: Config) -> int:
    import json

    path = cfg.output_dir / "app_captures.json"
    if path.exists():
        with open(path) as f:
            return len(json.load(f))
    return 0


def _count_discrepancies(cfg: Config) -> int:
    import json

    path = cfg.output_dir / "discrepancies.json"
    if path.exists():
        with open(path) as f:
            return json.load(f).get("statistics", {}).get("total_discrepancies", 0)
    return 0


@cli.command()
@click.option("--config", "config_path", type=click.Path(exists=True), help="Config YAML file")
@click.option("--from", "from_phase", type=click.Choice(PHASE_ORDER), help="Resume from phase")
@click.option("--figma-file", type=click.Path(exists=True), help="Local .fig file (Phase 2)")
@click.option("--target-page", help="Figma page ID (Phase 2)")
@click.option("--offline", is_flag=True, help="Figma offline mode (Phase 2)")
@click.option(
    "--agentic",
    is_flag=True,
    help="Use agentic mode for Phase 1 (Claude explores codebase with tools).",
)
def run(
    config_path: str | None,
    from_phase: str | None,
    figma_file: str | None,
    target_page: str | None,
    offline: bool,
    agentic: bool,
) -> None:
    """Run the full audit pipeline (all 6 phases)."""
    from figma_audit.utils.progress import RunProgress, set_progress

    load_env_file()

    if not check_api_keys():
        sys.exit(1)

    cfg = Config.load(config_path=_find_config(config_path), figma_file=figma_file)
    if agentic:
        cfg.analyze_mode = "agentic"

    phases = list(PHASE_ORDER)
    if from_phase:
        idx = phases.index(from_phase)
        phases = phases[idx:]

    progress = RunProgress(phases=phases)
    set_progress(progress)

    console.print(f"[bold]Running audit pipeline ({len(phases)} phases)[/bold]")

    for phase_name in phases:
        progress.start_phase(phase_name)

        if phase_name == "analyze":
            manifest_path = cfg.output_dir / "pages_manifest.json"

            # Skip if manifest already exists (stable between runs)
            if manifest_path.exists() and from_phase != "analyze":
                n_pages = _count_pages(cfg)
                console.print(
                    f"  [dim]Existing manifest ({n_pages} pages) "
                    f"— skip (use --from analyze to force)[/dim]"
                )
                progress.finish_phase(detail=f"{n_pages} pages (cached)")
            else:
                from figma_audit.phases.analyze_code import run as run_analyze

                run_analyze(cfg)
                client = _get_last_client("analyze_code")
                progress.finish_phase(
                    detail=f"{_count_pages(cfg)} pages",
                    cost=client.usage.cost(client.model) if client else 0,
                    tokens=client.usage.total_tokens if client else 0,
                )

        elif phase_name == "figma":
            from figma_audit.phases.export_figma import run as run_figma

            run_figma(cfg, offline=offline, target_page=target_page)
            screens = _count_screens(cfg)
            progress.finish_phase(detail=f"{screens} screens")

        elif phase_name == "match":
            import yaml

            mapping_path = cfg.output_dir / "screen_mapping.yaml"

            # Skip if mapping already exists and is verified (stable between runs)
            if mapping_path.exists() and from_phase != "match":
                with open(mapping_path) as f:
                    existing = yaml.safe_load(f)
                if existing and existing.get("verified"):
                    matched = sum(
                        1 for m in existing.get("mappings", []) if m.get("route")
                    )
                    console.print(
                        f"  [dim]Existing mapping ({matched} matches, verified) "
                        f"— skip (use --from match to force)[/dim]"
                    )
                    progress.finish_phase(detail=f"{matched} matches (cached)")
                    continue

            from figma_audit.phases.match_screens import run as run_match

            mapping_path = run_match(cfg)
            with open(mapping_path) as f:
                data = yaml.safe_load(f)
            if not data.get("verified"):
                data["verified"] = True
                with open(mapping_path, "w") as f:
                    yaml.dump(
                        data,
                        f,
                        default_flow_style=False,
                        allow_unicode=True,
                        sort_keys=False,
                    )
            matched = sum(1 for m in data.get("mappings", []) if m.get("route"))
            client = _get_last_client("match_screens")
            progress.finish_phase(
                detail=f"{matched} matches",
                cost=client.usage.cost(client.model) if client else 0,
                tokens=client.usage.total_tokens if client else 0,
            )

        elif phase_name == "capture":
            if not check_playwright_browser():
                sys.exit(1)
            from figma_audit.phases.capture_app import run as run_capture

            run_capture(cfg)
            captures = _count_captures(cfg)
            progress.finish_phase(detail=f"{captures} pages")

        elif phase_name == "compare":
            from figma_audit.phases.compare import run as run_compare

            run_compare(cfg)
            client = _get_last_client("compare")
            discs = _count_discrepancies(cfg)
            progress.finish_phase(
                detail=f"{discs} discrepancies",
                cost=client.usage.cost(client.model) if client else 0,
                tokens=client.usage.total_tokens if client else 0,
            )

        elif phase_name == "report":
            from figma_audit.phases.report import run as run_report

            report_path = run_report(cfg)
            size_mb = report_path.stat().st_size / 1024 / 1024
            progress.finish_phase(detail=f"{size_mb:.1f} MB")

    progress.print_summary()
    set_progress(None)
