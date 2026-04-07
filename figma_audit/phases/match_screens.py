"""Phase 3: Match Screens — Map Figma screens to application routes using AI Vision."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from rich.console import Console

from figma_audit.config import Config
from figma_audit.utils.claude_client import ClaudeClient

console = Console()

# Max images per vision API call (to stay within limits)
VISION_BATCH_SIZE = 9

SYSTEM_PROMPT = """\
You are a UI/UX expert matching Figma design screens to application routes.

You will receive:
1. A list of application routes with descriptions and metadata
2. Figma screen images with their names

Your task: for each Figma screen, find the best matching route from the application.

Rules:
- Match based on visual content, screen name, and route description
- Some Figma screens may not have a matching route (old designs, variants, components)
- Some routes may match multiple Figma screens (different states of the same page)
- Set confidence: 0.9+ = very certain, 0.7-0.9 = likely, 0.5-0.7 = uncertain, <0.5 = guess
- Set route to null if no good match exists
- Write notes in French explaining the match rationale
- Output ONLY valid JSON, no markdown, no commentary

JSON Schema:
{
  "mappings": [
    {
      "figma_screen_id": "123:456",
      "figma_screen_name": "Screen Name",
      "route": "/path or null",
      "page_id": "page_id or null",
      "confidence": 0.95,
      "notes": "Raison du matching en français"
    }
  ]
}
"""


def _build_routes_description(pages_manifest: dict) -> str:
    """Build a text description of all routes from the pages manifest."""
    lines = ["## Routes de l'application\n"]
    for page in pages_manifest.get("pages", []):
        auth = "auth requise" if page.get("auth_required") else "publique"
        fields = page.get("form_fields", [])
        states = page.get("interactive_states", [])
        desc = page.get("description", "")

        lines.append(f"- **{page['route']}** (id: `{page['id']}`, {auth})")
        lines.append(f"  Description: {desc}")
        if fields:
            field_names = [f["name"] for f in fields]
            lines.append(f"  Champs: {', '.join(field_names)}")
        if states:
            lines.append(f"  États: {', '.join(states)}")
        lines.append("")

    return "\n".join(lines)


def _build_screens_text(screens: list[dict]) -> str:
    """Build a text list of Figma screens (for screens without images)."""
    lines = ["## Écrans Figma (sans image disponible)\n"]
    for s in screens:
        lines.append(f"- **{s['name']}** (id: `{s['id']}`, {s['width']:.0f}x{s['height']:.0f})")
    return "\n".join(lines)


def run(config: Config) -> Path:
    """Run Phase 3: Match Figma screens to application routes.

    Returns:
        Path to the generated screen_mapping.yaml.
    """
    output_dir = config.output_dir
    pages_manifest_path = output_dir / "pages_manifest.json"
    figma_manifest_path = output_dir / "figma_manifest.json"
    mapping_path = output_dir / "screen_mapping.yaml"

    if not pages_manifest_path.exists():
        raise FileNotFoundError(
            f"pages_manifest.json not found at {pages_manifest_path}. Run Phase 1 first."
        )
    if not figma_manifest_path.exists():
        raise FileNotFoundError(
            f"figma_manifest.json not found at {figma_manifest_path}. Run Phase 2 first."
        )

    with open(pages_manifest_path) as f:
        pages_manifest = json.load(f)
    with open(figma_manifest_path) as f:
        figma_manifest = json.load(f)

    routes_text = _build_routes_description(pages_manifest)
    screens = figma_manifest.get("screens", [])

    n_screens = len(screens)
    n_routes = len(pages_manifest.get("pages", []))
    console.print(f"[bold]Matching {n_screens} Figma screens to {n_routes} routes[/bold]")

    # Split screens into those with images and those without
    screens_with_images = []
    screens_without_images = []
    for s in screens:
        img_path = s.get("image_path")
        if img_path and (output_dir / img_path).exists():
            screens_with_images.append(s)
        else:
            screens_without_images.append(s)

    console.print(f"  With images: {len(screens_with_images)}")
    console.print(f"  Name-only:   {len(screens_without_images)}")

    client = ClaudeClient(api_key=config.anthropic_api_key)
    all_mappings: list[dict] = []

    # ── Batch 1: Screens with images (vision) ─────────────────────
    if screens_with_images:
        batches = [
            screens_with_images[i : i + VISION_BATCH_SIZE]
            for i in range(0, len(screens_with_images), VISION_BATCH_SIZE)
        ]

        for batch_idx, batch in enumerate(batches):
            console.print(
                f"[bold]Vision batch {batch_idx + 1}/{len(batches)} "
                f"({len(batch)} screens)...[/bold]"
            )

            image_paths = []
            screen_list_text = "## Écrans Figma dans ce batch\n\n"
            for s in batch:
                img_path = output_dir / s["image_path"]
                image_paths.append(img_path)
                screen_list_text += (
                    f"- Image {len(image_paths)}: **{s['name']}** "
                    f"(id: `{s['id']}`, {s['width']:.0f}x{s['height']:.0f})\n"
                )

            user_prompt = (
                f"{routes_text}\n\n"
                f"{screen_list_text}\n\n"
                "Les images ci-dessus correspondent aux écrans Figma listés, dans l'ordre. "
                "Pour chaque écran, trouve la route correspondante dans l'application."
            )

            result = client.analyze_with_images(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                images=image_paths,
                max_tokens=8192,
                phase="match",
            )
            all_mappings.extend(result.get("mappings", []))

    # ── Batch 2: Screens without images (text-only) ───────────────
    if screens_without_images:
        console.print(f"[bold]Text-only matching ({len(screens_without_images)} screens)...[/bold]")

        screens_text = _build_screens_text(screens_without_images)
        user_prompt = (
            f"{routes_text}\n\n"
            f"{screens_text}\n\n"
            "Pour chaque écran Figma listé ci-dessus (sans image disponible), "
            "trouve la route correspondante en te basant uniquement sur le nom de l'écran. "
            "Indique une confiance plus basse car le matching est fait sans image."
        )

        result = client.analyze(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=8192,
            phase="match",
        )
        all_mappings.extend(result.get("mappings", []))

    client.print_usage()

    # ── Build YAML output ──────────────────────────────────────────
    mapping_data = {
        "verified": False,
        "mappings": all_mappings,
    }

    with open(mapping_path, "w") as f:
        yaml.dump(
            mapping_data,
            f,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
            width=120,
        )

    # Stats
    matched = sum(1 for m in all_mappings if m.get("route"))
    unmatched = sum(1 for m in all_mappings if not m.get("route"))
    high_conf = sum(1 for m in all_mappings if m.get("confidence", 0) >= 0.8)

    console.print(f"\n[bold green]Mapping saved to {mapping_path}[/bold green]")
    console.print(f"  {matched} matched, {unmatched} unmatched")
    console.print(f"  {high_conf} high confidence (>= 0.8)")
    console.print(
        "\n[yellow]⚠ Review the mapping and set 'verified: true' before running Phase 4.[/yellow]"
    )

    return mapping_path
