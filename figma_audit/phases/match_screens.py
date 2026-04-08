"""Phase 3: Match Screens — Map Figma screens to application routes using AI Vision."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from rich.console import Console

from figma_audit.config import Config
from figma_audit.utils.claude_client import ClaudeClient

console = Console()

# Exposed after run() for cost tracking by callers
_last_client: ClaudeClient | None = None

# Max images per vision API call (to stay within limits)
VISION_BATCH_SIZE = 9

SYSTEM_PROMPT = """\
You are a UI/UX expert matching Figma design screens to application routes.

You will receive:
1. A list of application routes with descriptions, metadata, and context
2. Figma screen images with their names

Your task: for each Figma screen, find the best matching route from the application.

IMPORTANT - Comprendre les ecrans pre/post-login:
- Les pages marquees "publique" (auth_required: false) sont des ecrans \
visibles AVANT la connexion (splash, login, inscription, onboarding).
- Les pages marquees "auth requise" ne sont visibles qu'APRES connexion.
- Un ecran Figma de type splash/onboarding/welcome doit correspondre a une \
route publique, PAS a une route authentifiee montrant des donnees.
- Si un ecran Figma montre clairement un contenu de type liste/dashboard/donnees, \
il correspond a une route authentifiee, pas au splash.

IMPORTANT - Ecrans a plusieurs etats:
- Un meme ecran Figma peut correspondre a un ETAT SPECIFIQUE d'une page \
(ex: un ecran avec un theme sombre = variante visuelle de la meme page).
- Si une page a des "etats capturables" (capturable_states), identifie a quel \
etat specifique correspond chaque ecran Figma et renseigne le champ state_id \
avec l'identifiant exact (ex: "step_2_addresses"). \
Si l'ecran correspond a l'etat initial (premiere etape), met state_id au premier etat capturable. \
Si aucun etat capturable ne correspond, met state_id a null.

Rules:
- Match based on visual content, screen name, route description, AND page context \
(form fields, required state, auth requirement)
- Some Figma screens may not have a matching route (old designs, variants, components)
- Some routes may match multiple Figma screens (different states of the same page)
- Set confidence: 0.9+ = very certain, 0.7-0.9 = likely, 0.5-0.7 = uncertain, <0.5 = guess
- Set route to null if no good match exists
- Write notes in French explaining the match rationale, including which visual state matches
- Output ONLY valid JSON, no markdown, no commentary

JSON Schema:
{
  "mappings": [
    {
      "figma_screen_id": "123:456",
      "figma_screen_name": "Screen Name",
      "route": "/path or null",
      "page_id": "page_id or null",
      "state_id": "capturable_state_id or null",
      "confidence": 0.95,
      "notes": "Raison du matching en français"
    }
  ]
}
"""


def _build_routes_description(pages_manifest: dict) -> str:
    """Build a rich text description of all routes from the pages manifest."""
    lines = ["## Routes de l'application\n"]
    for page in pages_manifest.get("pages", []):
        auth_required = page.get("auth_required", False)
        if auth_required:
            auth = "auth requise — visible apres connexion"
        else:
            auth = "publique — visible AVANT login"

        desc = page.get("description", "")
        fields = page.get("form_fields", [])
        states = page.get("interactive_states", [])
        params = page.get("params", [])
        req_state = page.get("required_state", {})

        lines.append(f"- **{page['route']}** (id: `{page['id']}`, {auth})")
        lines.append(f"  Description: {desc}")

        if req_state:
            state_desc = req_state.get("description", "")
            deps = req_state.get("data_dependencies", [])
            if state_desc:
                lines.append(f"  Prerequis: {state_desc}")
            if deps:
                lines.append(f"  Donnees requises: {', '.join(deps)}")

        if fields:
            field_descs = [
                f"{f['name']} ({f.get('type', '?')})"
                + (f" etape {f['step']}" if f.get("step") else "")
                for f in fields
            ]
            lines.append(f"  Champs de formulaire: {', '.join(field_descs)}")

        if params:
            param_descs = [
                f":{p['name']} ({p.get('type', 'string')}"
                + (", optionnel" if p.get("optional") else "")
                + ")"
                for p in params
            ]
            lines.append(f"  Parametres URL: {', '.join(param_descs)}")

        if states:
            lines.append(f"  Etats visuels: {', '.join(states)}")

        capturable = page.get("capturable_states", [])
        if capturable:
            cap_descs = [
                f"{cs['state_id']}: {cs.get('description', '')}" for cs in capturable
            ]
            lines.append(f"  Etats capturables (dans l'ordre): {'; '.join(cap_descs)}")

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

    # Exclude obsolete screens from matching
    obsolete_ids: set[str] = set()
    try:
        from sqlmodel import Session, select

        from figma_audit.db.engine import get_engine
        from figma_audit.db.models import Screen as DBScreen

        engine = get_engine()
        with Session(engine) as session:
            obs = session.exec(select(DBScreen).where(DBScreen.status == "obsolete")).all()
            obsolete_ids = {s.figma_node_id for s in obs}
    except Exception:
        pass
    if obsolete_ids:
        before = len(screens)
        screens = [s for s in screens if s["id"] not in obsolete_ids]
        n_excluded = before - len(screens)
        if n_excluded:
            console.print(f"  [dim]{n_excluded} ecran(s) obsolete(s) exclus du matching[/dim]")

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

    global _last_client
    client = ClaudeClient(api_key=config.anthropic_api_key)
    all_mappings: list[dict] = []

    # ── Batch 1: Screens with images (vision) ─────────────────────
    if screens_with_images:
        batches = [
            screens_with_images[i : i + VISION_BATCH_SIZE]
            for i in range(0, len(screens_with_images), VISION_BATCH_SIZE)
        ]

        from figma_audit.utils.progress import get_progress

        run_progress = get_progress()

        for batch_idx, batch in enumerate(batches):
            console.print(
                f"[bold]Vision batch {batch_idx + 1}/{len(batches)} "
                f"({len(batch)} screens)...[/bold]"
            )
            if run_progress:
                run_progress.update(
                    step=f"Vision batch {batch_idx + 1}/{len(batches)}",
                    progress=batch_idx + 1,
                    total=len(batches),
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

    _last_client = client
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
