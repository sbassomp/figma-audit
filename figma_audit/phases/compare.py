"""Phase 5: Compare -- Hybrid comparison (programmatic + AI Vision) of Figma vs App."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from rich.console import Console

from figma_audit.config import Config
from figma_audit.utils.claude_client import ClaudeClient

console = Console()

VISION_SYSTEM_PROMPT = """\
You are a senior UI/UX auditor comparing a Figma design (reference) against its real implementation.

You will receive two images:
1. First image: the Figma design (the source of truth)
2. Second image: the actual deployed application screenshot

You may also receive extracted design tokens and programmatic comparison results.

Compare element by element on these criteria:
1. LAYOUT: general arrangement, alignment, structure, visual hierarchy
2. COULEURS: backgrounds, text, buttons, icons (Figma hex values provided when available)
3. TYPOGRAPHIE: font family, size, weight, spacing
4. COMPOSANTS: buttons, inputs, cards, icons -- presence and style
5. TEXTES: text content, labels, placeholders
6. SPACING: margins, paddings, gaps between elements
7. ELEMENTS_MANQUANTS: present in Figma but missing in app
8. ELEMENTS_AJOUTES: present in app but not in Figma
9. DONNEES_ABSENTES: the app shows an empty/initial state (empty list, "no data" placeholder) \
while the Figma shows a populated state with data. This is NOT a design gap but a content gap.

IMPORTANT - Distinguish design gaps from data gaps:
- If the app displays an empty state (empty list, placeholder like "aucun element", "aucune course") \
while the Figma shows populated data: classify as DONNEES_ABSENTES with severity "minor".
  Still compare the structure, colors and layout of the empty state itself.
- NEVER mark as "critical" an element that is missing only because there is no data \
(course cards, activity lines, transaction rows, etc.)
- Only mark as "critical" elements that are structurally missing regardless of data state \
(missing navigation bar, wrong component type, missing buttons that should always be visible)

For each discrepancy found:
- category: one of the 8 above
- description: concise description in French
- severity: critical | important | minor
- figma_value: expected value (if quantifiable)
- app_value: observed value (if quantifiable)
- location: screen zone (top/center/bottom + left/center/right)

Severity criteria:
- critical: betrays the designer's intent (wrong component, missing element, wrong palette)
- important: visible gap degrading the experience (wrong weight, notable spacing, wrong icon)
- minor: subtle nuance, barely visible cosmetic difference

Also provide:
- overall_fidelity: excellent | good | acceptable | poor
- summary: 1-2 sentence summary in French

Output ONLY valid JSON with this schema:
{
  "discrepancies": [
    {
      "category": "COULEURS",
      "description": "Le bouton principal utilise un bleu plus fonce",
      "severity": "important",
      "figma_value": "#3A82F7",
      "app_value": "#2563EB",
      "location": "bottom-center"
    }
  ],
  "overall_fidelity": "good",
  "summary": "Resume en francais"
}
"""


def _build_comparison_context(
    figma_screen: dict,
    figma_manifest: dict,
    app_styles: dict | None,
    page_id: str,
    page_info: dict | None = None,
) -> str:
    """Build text context for the vision comparison."""
    parts = []

    # Figma screen info
    parts.append(f"## Ecran Figma: {figma_screen.get('name', '?')}")
    parts.append(f"Dimensions: {figma_screen.get('width', '?')}x{figma_screen.get('height', '?')}")
    bg = figma_screen.get("background_color")
    if bg:
        parts.append(f"Background: {bg}")

    # Design tokens from Figma elements
    elements = figma_screen.get("elements", [])
    if elements:
        texts = [e for e in elements if e.get("type") == "TEXT"]
        if texts:
            parts.append("\n### Textes Figma:")
            for t in texts[:20]:
                content = (t.get("content") or "")[:60]
                font = f"{t.get('font_family', '?')} {t.get('font_size', '?')}px w{t.get('font_weight', '?')}"
                color = t.get("color", "?")
                parts.append(f"  - \"{content}\" -- {font} -- {color}")

        colors_used = set()
        for e in elements:
            if e.get("color"):
                colors_used.add(e["color"])
            if e.get("fill"):
                colors_used.add(e["fill"])
        if colors_used:
            parts.append(f"\n### Couleurs utilisees: {', '.join(sorted(colors_used))}")

    # Page interactive states context
    if page_info:
        states = page_info.get("interactive_states", [])
        if states:
            parts.append(f"\n### Etats interactifs possibles: {', '.join(states)}")
            if "empty" in states and "populated" in states:
                parts.append(
                    "ATTENTION: cette page a un etat 'empty' et un etat 'populated'. "
                    "Si l'app affiche un etat vide, comparer la structure/layout de l'etat vide, "
                    "pas le contenu dynamique manquant (categorie DONNEES_ABSENTES, severity minor)."
                )

    # App computed styles (if available)
    if app_styles and page_id in app_styles:
        parts.append("\n### Styles computed de l'app (extraits du DOM):")
        styles = app_styles[page_id]
        for s in styles[:15]:
            text = (s.get("text") or "")[:40]
            if text:
                parts.append(
                    f"  - \"{text}\" -- {s.get('fontFamily', '?')} "
                    f"{s.get('fontSize', '?')} w{s.get('fontWeight', '?')} "
                    f"color={s.get('color', '?')} bg={s.get('backgroundColor', '?')}"
                )

    return "\n".join(parts)


def run(config: Config) -> Path:
    """Run Phase 5: Compare Figma designs against app screenshots.

    Returns:
        Path to the generated discrepancies.json.
    """
    output_dir = config.output_dir
    figma_manifest_path = output_dir / "figma_manifest.json"
    mapping_path = output_dir / "screen_mapping.yaml"
    captures_path = output_dir / "app_captures.json"
    styles_path = output_dir / "app_styles.json"
    discrepancies_path = output_dir / "discrepancies.json"

    pages_manifest_path = output_dir / "pages_manifest.json"

    # Load inputs
    for path, name, phase in [
        (figma_manifest_path, "figma_manifest.json", "Phase 2"),
        (mapping_path, "screen_mapping.yaml", "Phase 3"),
        (captures_path, "app_captures.json", "Phase 4"),
        (pages_manifest_path, "pages_manifest.json", "Phase 1"),
    ]:
        if not path.exists():
            raise FileNotFoundError(f"{name} not found. Run {phase} first.")

    with open(figma_manifest_path) as f:
        figma_manifest = json.load(f)
    with open(mapping_path) as f:
        mapping_data = yaml.safe_load(f)
    with open(captures_path) as f:
        captures = json.load(f)

    with open(pages_manifest_path) as f:
        pages_manifest = json.load(f)

    app_styles: dict | None = None
    if styles_path.exists():
        with open(styles_path) as f:
            app_styles = json.load(f)

    # Build lookups
    pages_by_id = {p["id"]: p for p in pages_manifest.get("pages", [])}
    figma_screens_by_id = {s["id"]: s for s in figma_manifest.get("screens", [])}
    captures_by_page_id = {c["page_id"]: c for c in captures if c.get("screenshot")}

    # Load obsolete screen IDs from DB (if available)
    obsolete_screen_ids: set[str] = set()
    db_path = Path("figma-audit.db")
    if not db_path.exists():
        db_path = Path.home() / ".config" / "figma-audit" / "figma-audit.db"
    if db_path.exists():
        try:
            from figma_audit.db.engine import get_engine, init_db
            from figma_audit.db.models import Screen
            from sqlmodel import Session, select

            init_db(str(db_path))
            engine = get_engine(str(db_path))
            with Session(engine) as session:
                screens = session.exec(
                    select(Screen).where(Screen.status == "obsolete")
                ).all()
                obsolete_screen_ids = {s.figma_node_id for s in screens}
            if obsolete_screen_ids:
                console.print(f"  [dim]{len(obsolete_screen_ids)} ecran(s) obsolete(s) exclus[/dim]")
        except Exception:
            pass

    # Build comparison pairs from mapping
    pairs = []
    for m in mapping_data.get("mappings", []):
        figma_id = m.get("figma_screen_id")
        page_id = m.get("page_id")
        route = m.get("route")
        confidence = m.get("confidence", 0)

        if not figma_id or not page_id or not route:
            continue
        if confidence < 0.7:
            continue
        if figma_id in obsolete_screen_ids:
            continue

        figma_screen = figma_screens_by_id.get(figma_id)
        capture = captures_by_page_id.get(page_id)

        if not figma_screen or not capture:
            continue

        figma_img = figma_screen.get("image_path")
        app_img = capture.get("screenshot")

        if not figma_img or not app_img:
            continue

        figma_img_full = output_dir / figma_img
        app_img_full = output_dir / app_img

        if not figma_img_full.exists() or not app_img_full.exists():
            continue

        pairs.append({
            "figma_screen_id": figma_id,
            "figma_screen_name": figma_screen.get("name", ""),
            "page_id": page_id,
            "route": route,
            "figma_image": figma_img,
            "app_image": app_img,
            "figma_screen": figma_screen,
        })

    # Deduplicate: keep only the highest-confidence mapping per (figma_screen, page_id)
    seen = set()
    unique_pairs = []
    for p in pairs:
        key = (p["figma_screen_id"], p["page_id"])
        if key not in seen:
            seen.add(key)
            unique_pairs.append(p)

    # Estimate cost (2 images ~1600 tokens each + ~500 text = ~3700 input, ~800 output per pair)
    est_input = len(unique_pairs) * 3700
    est_output = len(unique_pairs) * 800
    from figma_audit.utils.claude_client import PRICING, DEFAULT_PRICING
    pricing = PRICING.get(DEFAULT_MODEL, DEFAULT_PRICING)
    est_cost = est_input * pricing["input"] / 1_000_000 + est_output * pricing["output"] / 1_000_000
    console.print(
        f"[bold]Comparing {len(unique_pairs)} Figma/App pairs[/bold] "
        f"(estimation: ~{est_input + est_output:,} tokens, ~${est_cost:.2f})"
    )

    client = ClaudeClient(api_key=config.anthropic_api_key)
    comparisons = []
    total_discrepancies = 0

    for i, pair in enumerate(unique_pairs):
        console.print(
            f"  [{i+1}/{len(unique_pairs)}] {pair['figma_screen_name']} "
            f"vs {pair['page_id']} ({pair['route']})"
        )

        figma_img_path = output_dir / pair["figma_image"]
        app_img_path = output_dir / pair["app_image"]

        context = _build_comparison_context(
            pair["figma_screen"],
            figma_manifest,
            app_styles,
            pair["page_id"],
            page_info=pages_by_id.get(pair["page_id"]),
        )

        user_prompt = (
            f"Image 1 = design Figma (reference). Image 2 = application reelle.\n\n"
            f"{context}\n\n"
            f"Compare ces deux ecrans et liste tous les ecarts."
        )

        try:
            result = client.analyze_with_images(
                system_prompt=VISION_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                images=[figma_img_path, app_img_path],
                max_tokens=4096,
                phase="compare",
            )

            discrepancies = result.get("discrepancies", [])
            total_discrepancies += len(discrepancies)

            comparison = {
                "page_id": pair["page_id"],
                "route": pair["route"],
                "figma_screen": pair["figma_screen_name"],
                "figma_image": pair["figma_image"],
                "app_image": pair["app_image"],
                "discrepancies": discrepancies,
                "overall_fidelity": result.get("overall_fidelity", "unknown"),
                "summary": result.get("summary", ""),
            }
            comparisons.append(comparison)

            severity_counts = {}
            for d in discrepancies:
                sev = d.get("severity", "?")
                severity_counts[sev] = severity_counts.get(sev, 0) + 1
            sev_str = ", ".join(f"{v} {k}" for k, v in severity_counts.items())
            console.print(
                f"    {result.get('overall_fidelity', '?')} -- "
                f"{len(discrepancies)} ecarts ({sev_str})"
            )

        except Exception as e:
            console.print(f"    [red]Error: {e}[/red]")
            comparisons.append({
                "page_id": pair["page_id"],
                "route": pair["route"],
                "figma_screen": pair["figma_screen_name"],
                "figma_image": pair["figma_image"],
                "app_image": pair["app_image"],
                "discrepancies": [],
                "overall_fidelity": "error",
                "summary": f"Erreur: {e}",
            })

    client.print_usage()

    # Statistics
    by_severity = {"critical": 0, "important": 0, "minor": 0}
    by_category: dict[str, int] = {}
    for comp in comparisons:
        for d in comp.get("discrepancies", []):
            sev = d.get("severity", "minor")
            by_severity[sev] = by_severity.get(sev, 0) + 1
            cat = d.get("category", "OTHER")
            by_category[cat] = by_category.get(cat, 0) + 1

    output_data = {
        "comparisons": comparisons,
        "statistics": {
            "total_screens": len(comparisons),
            "total_discrepancies": total_discrepancies,
            "by_severity": by_severity,
            "by_category": by_category,
        },
    }

    with open(discrepancies_path, "w") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    console.print(f"\n[bold green]Comparisons saved to {discrepancies_path}[/bold green]")
    console.print(f"  {len(comparisons)} screens compared")
    console.print(f"  {total_discrepancies} ecarts total")
    console.print(f"  Critical: {by_severity['critical']}, Important: {by_severity['important']}, Minor: {by_severity['minor']}")

    return discrepancies_path
