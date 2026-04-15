"""Phase 5: Compare -- Hybrid comparison (programmatic + AI Vision) of Figma vs App."""

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

VISION_SYSTEM_PROMPT = """\
You are a senior UI/UX auditor comparing a Figma design (reference) against its real implementation.

You will receive two images:
1. First image: the Figma design (the source of truth)
2. Second image: the actual deployed application screenshot

You will also receive context about the page: description, auth state, form fields, \
visual states, and structural text/font info from the Figma design.

CRITICAL - Matching verification:
BEFORE comparing details, verify that both images show the SAME TYPE of page. \
For example a splash/onboarding vs a list/dashboard, or a form vs a profile page.
If the two images show fundamentally different screens (not the same type of page), \
the screens have been incorrectly matched. In that case:
- Set overall_fidelity to "mismatch"
- Add a SINGLE discrepancy:
  category: "MATCHING_ERROR"
  description: "The two screens do not match — [explain what each image shows]"
  severity: "critical"
- Do NOT generate any other discrepancies (they would all be false)
- The summary must explain that the matching is incorrect

If both images do show the same type of page, proceed with the normal comparison below.

IMPORTANT for COLORS: Base your color comparison ONLY on the two images you see.
Do NOT rely on any hex color values mentioned in the text context — they may come from a
different variant of the design and be inaccurate. Compare what you SEE in the images.

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

IMPORTANT - Distinguish DESIGN gaps from DATA gaps:
Dynamic data (names, addresses, dates, times, prices, distances, durations, \
phone numbers, emails, profile photos) are ALWAYS different between \
the Figma (mockup) and the app (real data). NEVER report them as discrepancies. \
Completely ignore any difference that concerns the CONTENT of the data.
Focus only on:
- STRUCTURE (are the right components present?)
- STYLE (background colors, text colors, sizes, fonts, border-radius)
- ICONS (same icon? same color? same size?)
- POSITIONING (alignment, spacing, placement of badges/indicators)
- VISUAL STATES (active/inactive badge, label color based on context)

Examples of things NOT to report:
- Different price/amount (170€ vs 79€) → dynamic data
- Different address/location → dynamic data
- Different date/time → dynamic data
- Different person name → dynamic data
- Different distance/duration/quantity → dynamic data
- Version number, identifiers → ignore

Examples of things TO REPORT:
- Different badge background color (blue vs green)
- Different label text color (black vs white)
- Different icon in the navigation bar
- Shifted notification badge position
- Different border-radius on a button
- Different font size or weight

IMPORTANT - Be UNCOMPROMISING on every visual detail:

BACKGROUND COLORS — zero tolerance:
- Compare the background color of EVERY area: app bar, page body, cards, \
  bottom nav, modals, badges, buttons. The slightest tint difference is \
  a discrepancy (e.g. dark gray background #1A1A2E vs pure black #000000).
- Compare the BACKGROUND color and TEXT color of each badge/label separately.
- Check border (stroke) and shadow colors if visible.

SHAPES AND BORDERS — zero tolerance:
- Compare the border-radius of every card, button, input field, badge, \
  and image. A rounded button (radius 24px) vs a slightly rounded button \
  (radius 8px) is an important discrepancy, not minor.
- Compare rounded corners of images and avatars (circle vs rounded square vs sharp).
- Check dividers: presence, color, thickness.

POSITIONS AND ALIGNMENT — zero tolerance:
- Compare the overall placement of each section: vertical position of the \
  navigation bar, header, main content, FAB (floating action button).
- Check horizontal alignment: centered vs left-aligned vs justified.
- Compare the relative size of elements to each other (proportions).
- Check internal padding of cards and buttons.
- An element shifted by more than ~4px visually is a discrepancy.

ICONS AND NAVIGATION:
- Compare each navigation bar icon individually (shape, style, color).
- Check the exact positioning of notification badges (numeric badges).
- Compare style variants (filled vs outlined, round vs square).

SEVERITY PRINCIPLE: the Figma design is the absolute truth. Any deviation \
visible to the naked eye is at minimum "important". Reserve "minor" only \
for nearly invisible differences (1-2px spacing, slight shadow variation). \
If you hesitate between "important" and "minor", choose "important".

For each discrepancy found:
- category: one of the 8 above
- description: concise description in English
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
- summary: 1-2 sentence summary in English

Output ONLY valid JSON with this schema:
{
  "discrepancies": [
    {
      "category": "COULEURS",
      "description": "The primary button uses a darker blue",
      "severity": "important",
      "figma_value": "#3A82F7",
      "app_value": "#2563EB",
      "location": "bottom-center"
    }
  ],
  "overall_fidelity": "good",
  "summary": "Summary in English"
}
"""


def _build_comparison_context(
    figma_screen: dict,
    app_styles: dict | None,
    page_id: str,
    page_info: dict | None = None,
    state_id: str | None = None,
) -> str:
    """Build text context for the vision comparison.

    NOTE: We intentionally omit extracted Figma color values from the context.
    These colors are parsed from the Figma JSON tree and can come from a different
    variant of the screen (e.g. light-mode colors for a dark-mode screen), leading
    to false-positive "critical" color discrepancies. The AI should compare colors
    based solely on the two images it receives.
    """
    parts = []

    # Figma screen info
    parts.append(f"## Figma Screen: {figma_screen.get('name', '?')}")
    parts.append(f"Dimensions: {figma_screen.get('width', '?')}x{figma_screen.get('height', '?')}")

    # Page context from manifest (description, auth, form fields, etc.)
    if page_info:
        header = f"\n## Application Page: {page_id} ({page_info.get('route', '?')})"
        if state_id:
            header += f" — state: {state_id}"
        parts.append(header)
        desc = page_info.get("description", "")
        if desc:
            parts.append(f"Description: {desc}")

        auth = page_info.get("auth_required", False)
        if auth:
            parts.append("Auth required: Yes (page visible after login)")
        else:
            parts.append("Auth required: No (public page, visible before login)")

        req_state = page_info.get("required_state", {})
        if req_state:
            state_desc = req_state.get("description", "")
            if state_desc:
                parts.append(f"Prerequisites: {state_desc}")
            deps = req_state.get("data_dependencies", [])
            if deps:
                parts.append(f"Required data: {', '.join(deps)}")

        fields = page_info.get("form_fields", [])
        if fields:
            field_descs = [f"{f['name']} ({f.get('type', '?')})" for f in fields]
            parts.append(f"Expected form fields: {', '.join(field_descs)}")

        states = page_info.get("interactive_states", [])
        if states:
            parts.append(f"Possible visual states: {', '.join(states)}")
            if "empty" in states and "populated" in states:
                parts.append(
                    "WARNING: this page has an 'empty' state "
                    "and a 'populated' state. "
                    "If the app shows an empty state, compare "
                    "the structure/layout of the empty state, "
                    "not the missing dynamic content "
                    "(category DONNEES_ABSENTES, severity minor)."
                )

    # Text elements — content and font info only, NO color values
    elements = figma_screen.get("elements", [])
    if elements:
        texts = [e for e in elements if e.get("type") == "TEXT"]
        if texts:
            parts.append("\n### Figma Texts:")
            for t in texts[:20]:
                content = (t.get("content") or "")[:60]
                font = (
                    f"{t.get('font_family', '?')} "
                    f"{t.get('font_size', '?')}px "
                    f"w{t.get('font_weight', '?')}"
                )
                parts.append(f'  - "{content}" -- {font}')

    # App computed styles (if available)
    if app_styles and page_id in app_styles:
        parts.append("\n### App computed styles (extracted from DOM):")
        styles = app_styles[page_id]
        for s in styles[:15]:
            text = (s.get("text") or "")[:40]
            if text:
                parts.append(
                    f'  - "{text}" -- {s.get("fontFamily", "?")} '
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
    # Build capture lookup: (page_id, state_id) → capture dict with screenshot
    captures_by_key: dict[tuple[str, str | None], dict] = {}
    for c in captures:
        pid = c.get("page_id")
        if not pid:
            continue
        if c.get("screenshot"):
            captures_by_key[(pid, None)] = c
        for state in c.get("states", []):
            if state.get("screenshot"):
                captures_by_key[(pid, state["state_id"])] = {
                    **c,
                    "screenshot": state["screenshot"],
                    "state_id": state["state_id"],
                }

    # Load obsolete screen IDs from DB (if available)
    obsolete_screen_ids: set[str] = set()
    try:
        from sqlmodel import Session, select

        from figma_audit.db.engine import get_engine
        from figma_audit.db.models import Screen

        engine = get_engine()
        with Session(engine) as session:
            screens = session.exec(select(Screen).where(Screen.status == "obsolete")).all()
            obsolete_screen_ids = {s.figma_node_id for s in screens}
        if obsolete_screen_ids:
            n_obs = len(obsolete_screen_ids)
            console.print(f"  [dim]{n_obs} obsolete screen(s) excluded[/dim]")
    except Exception as e:
        console.print(f"  [dim]DB obsolete check skipped: {e}[/dim]")

    # Build comparison pairs from mapping
    pairs = []
    for m in mapping_data.get("mappings", []):
        figma_id = m.get("figma_screen_id")
        page_id = m.get("page_id")
        state_id = m.get("state_id")  # may be None for single-state pages
        route = m.get("route")
        confidence = m.get("confidence", 0)

        if not figma_id or not page_id or not route:
            continue
        if confidence < 0.7:
            continue
        if figma_id in obsolete_screen_ids:
            continue

        figma_screen = figma_screens_by_id.get(figma_id)
        if not figma_screen:
            continue

        # Try state-specific capture first, fall back to base capture
        capture = captures_by_key.get((page_id, state_id))
        if not capture and state_id:
            capture = captures_by_key.get((page_id, None))
        if not capture:
            continue

        figma_img = figma_screen.get("image_path")
        app_img = capture.get("screenshot")

        if not figma_img or not app_img:
            continue

        figma_img_full = output_dir / figma_img
        app_img_full = output_dir / app_img

        if not figma_img_full.exists() or not app_img_full.exists():
            continue

        pairs.append(
            {
                "figma_screen_id": figma_id,
                "figma_screen_name": figma_screen.get("name", ""),
                "page_id": page_id,
                "state_id": state_id,
                "route": route,
                "figma_image": figma_img,
                "app_image": app_img,
                "figma_screen": figma_screen,
            }
        )

    # Deduplicate: keep only the highest-confidence mapping per (figma_screen, page_id, state_id)
    seen = set()
    unique_pairs = []
    for p in pairs:
        key = (p["figma_screen_id"], p["page_id"], p.get("state_id"))
        if key not in seen:
            seen.add(key)
            unique_pairs.append(p)

    # Estimate cost (2 images ~1600 tokens each + ~500 text = ~3700 input, ~800 output per pair)
    est_input = len(unique_pairs) * 3700
    est_output = len(unique_pairs) * 800
    from figma_audit.utils.claude_client import DEFAULT_MODEL, DEFAULT_PRICING, PRICING

    pricing = PRICING.get(DEFAULT_MODEL, DEFAULT_PRICING)
    est_cost = est_input * pricing["input"] / 1_000_000 + est_output * pricing["output"] / 1_000_000
    console.print(
        f"[bold]Comparing {len(unique_pairs)} Figma/App pairs[/bold] "
        f"(estimation: ~{est_input + est_output:,} tokens, ~${est_cost:.2f})"
    )

    global _last_client
    client = ClaudeClient(api_key=config.anthropic_api_key)
    comparisons = []
    total_discrepancies = 0

    # Get global progress for web UI updates
    from figma_audit.utils.progress import get_progress

    run_progress = get_progress()

    for i, pair in enumerate(unique_pairs):
        console.print(
            f"  [{i + 1}/{len(unique_pairs)}] {pair['figma_screen_name']} "
            f"vs {pair['page_id']} ({pair['route']})"
        )

        # Update web UI progress
        if run_progress:
            run_progress.update(
                step=f"{pair['figma_screen_name']} vs {pair['page_id']}",
                progress=i + 1,
                total=len(unique_pairs),
            )
            # Save to DB for htmx polling
            import json as _json

            from figma_audit.db.engine import get_engine
            from figma_audit.db.models import Run

            try:
                engine = get_engine()
                with Session(engine) as _s:
                    _run = _s.exec(select(Run).where(Run.status == "running")).first()
                    if _run:
                        _run.progress_json = _json.dumps(run_progress.to_dict())
                        _s.add(_run)
                        _s.commit()
            except Exception:
                pass  # Non-blocking: progress update failure is OK

        figma_img_path = output_dir / pair["figma_image"]
        app_img_path = output_dir / pair["app_image"]

        context = _build_comparison_context(
            pair["figma_screen"],
            app_styles,
            pair["page_id"],
            page_info=pages_by_id.get(pair["page_id"]),
            state_id=pair.get("state_id"),
        )

        user_prompt = (
            f"Image 1 = Figma design (reference). Image 2 = actual application.\n\n"
            f"{context}\n\n"
            f"Compare these two screens and list all discrepancies."
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
                "state_id": pair.get("state_id"),
                "figma_screen": pair["figma_screen_name"],
                "figma_screen_id": pair["figma_screen_id"],
                "figma_image": pair["figma_image"],
                "app_image": pair["app_image"],
                "discrepancies": discrepancies,
                "overall_fidelity": result.get("overall_fidelity", "unknown"),
                "summary": result.get("summary", ""),
            }
            comparisons.append(comparison)

            fidelity = result.get("overall_fidelity", "unknown")

            if fidelity == "mismatch":
                console.print(
                    "    [bold yellow]MISMATCH[/bold yellow] -- "
                    "screens incorrectly matched, comparison skipped"
                )
            else:
                severity_counts = {}
                for d in discrepancies:
                    sev = d.get("severity", "?")
                    severity_counts[sev] = severity_counts.get(sev, 0) + 1
                sev_str = ", ".join(f"{v} {k}" for k, v in severity_counts.items())
                console.print(f"    {fidelity} -- {len(discrepancies)} discrepancies ({sev_str})")

        except Exception as e:
            console.print(f"    [red]Error: {e}[/red]")
            comparisons.append(
                {
                    "page_id": pair["page_id"],
                    "route": pair["route"],
                    "state_id": pair.get("state_id"),
                    "figma_screen": pair["figma_screen_name"],
                    "figma_image": pair["figma_image"],
                    "app_image": pair["app_image"],
                    "discrepancies": [],
                    "overall_fidelity": "error",
                    "summary": f"Error: {e}",
                }
            )

    _last_client = client
    client.print_usage()

    # Statistics (exclude mismatches — their discrepancies are not real design gaps)
    by_severity = {"critical": 0, "important": 0, "minor": 0}
    by_category: dict[str, int] = {}
    n_mismatches = 0
    for comp in comparisons:
        if comp.get("overall_fidelity") == "mismatch":
            n_mismatches += 1
            continue
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
            "mismatches": n_mismatches,
            "by_severity": by_severity,
            "by_category": by_category,
        },
    }

    with open(discrepancies_path, "w") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    console.print(f"\n[bold green]Comparisons saved to {discrepancies_path}[/bold green]")
    console.print(f"  {len(comparisons)} screens compared")
    if n_mismatches:
        console.print(f"  [bold yellow]{n_mismatches} mismatch(es) detected[/bold yellow]")
    console.print(f"  {total_discrepancies} total discrepancies")
    console.print(
        f"  Critical: {by_severity['critical']}, "
        f"Important: {by_severity['important']}, "
        f"Minor: {by_severity['minor']}"
    )

    return discrepancies_path
