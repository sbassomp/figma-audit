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

IMPORTANT - Understanding pre/post-login screens:
- Pages marked "public" (auth_required: false) are screens \
visible BEFORE login (splash, login, registration, onboarding).
- Pages marked "auth required" are only visible AFTER login.
- A Figma screen of type splash/onboarding/welcome must match a \
public route, NOT an authenticated route showing data.
- If a Figma screen clearly shows list/dashboard/data content, \
it matches an authenticated route, not the splash.

IMPORTANT - Multi-state screens:
- A single Figma screen may correspond to a SPECIFIC STATE of a page \
(e.g. dark theme variant, "Courses prises" tab vs "Courses déposées" tab, \
filtered list vs unfiltered list, wizard step 2 vs wizard step 3).
- If a page declares `capturable_states` in the manifest, ALWAYS pick \
the matching `state_id` from that list and fill it in. NEVER leave \
state_id null when the page has capturable_states declared.
- If the page has capturable_states but NONE of them obviously match \
this Figma variant, choose the closest one and explain the mismatch \
in the notes.
- **WHENEVER MULTIPLE FIGMA SCREENS MAP TO THE SAME page_id**: each one \
MUST receive a distinct state_id (or be marked as obsolete by setting \
the route to null with a note "obsolete variant"). Two Figma screens \
sharing the same `(page_id, state_id)` would cascade into duplicated \
comparisons and false MATCHING_ERROR discrepancies. \
Look at what visually differs between the candidates: which tab is \
active? Is it dark mode? Are filters applied? Does the screen show \
data vs an empty state? Use the most descriptive snake_case state_id \
you can infer from the visible difference.
- If the page has NO `capturable_states` declared but you see multiple \
Figma variants for it, invent a sensible state_id (e.g. "default", \
"taken", "deposited", "with_filters", "dark_mode", "empty", "step_2") \
and document the choice in `notes`. The next analyze run can then add \
this state_id to the manifest's capturable_states.

Rules:
- Match based on visual content, screen name, route description, AND page context \
(form fields, required state, auth requirement)
- Some Figma screens may not have a matching route (old designs, variants, components)
- Some routes may match multiple Figma screens (different states of the same page)
- Set confidence: 0.9+ = very certain, 0.7-0.9 = likely, 0.5-0.7 = uncertain, <0.5 = guess
- Set route to null if no good match exists
- Write notes in English explaining the match rationale, including which visual state matches
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
      "notes": "Match rationale in English"
    }
  ]
}
"""


DISAMBIGUATE_SYSTEM_PROMPT = """\
You are a UI/UX expert. A batched matching pass has assigned the same
(page_id, state_id) key to several Figma screens of the same app page.
Your job is to disambiguate them.

You will receive:
1. A short description of the app page (route, auth, visual states)
2. N Figma screen images that were all mapped to the same page

Your task: for each screen, assign a DISTINCT ``state_id`` describing
what visually differentiates it from the others. Use snake_case
identifiers derived from what you see (NOT from the original filename
which is often wrong).

Examples of good state_ids:

- For a wizard/multi-step page: ``step_0_overview``, ``step_1_addresses``,
  ``step_2_schedule``, ``step_3_patient_info`` (use the actual visible
  content to pick names)
- For a list page with variants: ``empty``, ``with_items``, ``filtered``,
  ``dark_mode``, ``sorted_recent``
- For a detail page with lifecycle states: ``created``, ``in_progress``,
  ``completed``, ``cancelled_by_depositor``, ``cancelled_by_taker``,
  ``expired``
- For tabbed pages: use the TAB label you can read on the screen
  (``tab_taken``, ``tab_deposited``, ``tab_messages``)

Rules:

- Every screen in the group MUST receive a distinct state_id. Two
  screens sharing the same state_id is the bug this pass fixes.
- Be specific. ``default`` and ``variant_1`` are not acceptable; look
  at the images and name what you SEE.
- If two images are truly identical (same content, same layout), pick
  the clearer candidate to keep and mark the other with
  ``state_id: "duplicate_of_<kept_state_id>"`` so the human can review.
- If an image does NOT match the described app page at all (wrong
  match from the batch pass), set ``state_id`` to
  ``"wrong_match_<guess_of_actual_page>"`` and set a low confidence so
  it can be flagged for human review.
- Output ONLY valid JSON, no markdown.

JSON schema:
{
  "mappings": [
    {
      "figma_screen_id": "123:456",
      "state_id": "step_2_addresses",
      "confidence": 0.92,
      "notes": "Shows the 'Adresses' step with departure and destination fields visible"
    }
  ]
}
"""


def _disambiguate_states(
    all_mappings: list[dict],
    *,
    screens_by_id: dict[str, dict],
    output_dir: Path,
    pages_manifest: dict,
    client: ClaudeClient,
) -> None:
    """Re-disambiguate colliding ``(page_id, state_id)`` groups in place.

    Walks ``all_mappings``, finds every ``page_id`` that has several
    mappings, and within each page detects groups that collide on
    ``state_id``. For each colliding group, makes a single vision call
    with every screen image in the group plus the page description so
    the model can assign fresh, distinct state_ids.

    Mutates ``all_mappings`` in place: on success the ``state_id``,
    ``confidence`` and ``notes`` fields of the affected entries are
    overwritten. The ``route`` and ``page_id`` fields are preserved.
    """
    # Group by page_id
    by_page: dict[str, list[dict]] = {}
    for m in all_mappings:
        page_id = m.get("page_id")
        if not page_id or not m.get("route"):
            continue
        by_page.setdefault(page_id, []).append(m)

    # Find groups with collisions
    colliding_groups: list[tuple[str, list[dict]]] = []
    for page_id, mappings in by_page.items():
        if len(mappings) < 2:
            continue
        state_counts: dict[str | None, int] = {}
        for m in mappings:
            sid = m.get("state_id")
            state_counts[sid] = state_counts.get(sid, 0) + 1
        if any(count >= 2 for count in state_counts.values()):
            colliding_groups.append((page_id, mappings))

    if not colliding_groups:
        return

    console.print(
        f"\n[bold]Phase 3 disambiguation: {len(colliding_groups)} page(s) "
        f"with colliding state_ids[/bold]"
    )

    pages_by_id = {p["id"]: p for p in pages_manifest.get("pages", [])}

    for page_id, mappings in colliding_groups:
        # Build the image list for this page
        image_paths: list[Path] = []
        screens_in_batch: list[dict] = []
        for m in mappings:
            fid = m.get("figma_screen_id")
            screen = screens_by_id.get(fid) if fid else None
            if not screen:
                continue
            img_rel = screen.get("image_path")
            if not img_rel:
                continue
            abs_img = output_dir / img_rel
            if not abs_img.exists():
                continue
            image_paths.append(abs_img)
            screens_in_batch.append(screen)

        if len(image_paths) < 2:
            continue

        # Build the page description
        page_info = pages_by_id.get(page_id) or {}
        desc_lines = [
            f"## App page to disambiguate: `{page_id}`",
            f"Route: **{page_info.get('route', 'unknown')}**",
            f"Auth: {'required' if page_info.get('auth_required') else 'public'}",
            f"Description: {page_info.get('description', '')}",
        ]
        capturable = page_info.get("capturable_states") or []
        if capturable:
            desc_lines.append("Known capturable states (pick matching identifiers when obvious):")
            for cs in capturable:
                desc_lines.append(f"  - `{cs.get('state_id')}`: {cs.get('description', '')}")
        desc_lines.append("")
        desc_lines.append(f"## {len(screens_in_batch)} Figma screens currently mapped to this page")
        for i, s in enumerate(screens_in_batch, start=1):
            desc_lines.append(f"- Image {i}: **{s['name']}** (id: `{s['id']}`)")

        user_prompt = "\n".join(desc_lines) + (
            "\n\nAssign a DISTINCT `state_id` to each screen based on what you see in the images."
        )

        console.print(f"  [dim]{page_id}: disambiguating {len(screens_in_batch)} screens...[/dim]")

        try:
            result = client.analyze_with_images(
                system_prompt=DISAMBIGUATE_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                images=image_paths,
                max_tokens=4096,
                phase="match",
            )
        except Exception as e:
            console.print(f"    [yellow]disambiguation failed for {page_id}: {e}[/yellow]")
            continue

        # Apply the new state_ids. Match by figma_screen_id.
        by_fid = {m.get("figma_screen_id"): m for m in mappings}
        for new in result.get("mappings", []):
            fid = new.get("figma_screen_id")
            existing = by_fid.get(fid)
            if not existing:
                continue
            new_state = new.get("state_id")
            if not new_state:
                continue
            existing["state_id"] = new_state
            if new.get("confidence") is not None:
                existing["confidence"] = new["confidence"]
            if new.get("notes"):
                prev_notes = existing.get("notes", "")
                existing["notes"] = f"{prev_notes} | disambiguated: {new['notes']}".strip(" |")

        # Sanity: count collisions remaining
        remaining = {}
        for m in mappings:
            sid = m.get("state_id")
            remaining[sid] = remaining.get(sid, 0) + 1
        still_colliding = sum(1 for c in remaining.values() if c >= 2)
        if still_colliding:
            console.print(
                f"    [yellow]{page_id}: {still_colliding} state_id(s) still colliding "
                "after disambiguation[/yellow]"
            )
        else:
            console.print(f"    [green]{page_id}: {len(remaining)} distinct state_ids[/green]")


def _build_routes_description(pages_manifest: dict) -> str:
    """Build a rich text description of all routes from the pages manifest."""
    lines = ["## Application Routes\n"]
    for page in pages_manifest.get("pages", []):
        auth_required = page.get("auth_required", False)
        if auth_required:
            auth = "auth required — visible after login"
        else:
            auth = "public — visible BEFORE login"

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
                lines.append(f"  Prerequisites: {state_desc}")
            if deps:
                lines.append(f"  Required data: {', '.join(deps)}")

        if fields:
            field_descs = [
                f"{f['name']} ({f.get('type', '?')})"
                + (f" step {f['step']}" if f.get("step") else "")
                for f in fields
            ]
            lines.append(f"  Form fields: {', '.join(field_descs)}")

        if params:
            param_descs = [
                f":{p['name']} ({p.get('type', 'string')}"
                + (", optional" if p.get("optional") else "")
                + ")"
                for p in params
            ]
            lines.append(f"  URL parameters: {', '.join(param_descs)}")

        if states:
            lines.append(f"  Visual states: {', '.join(states)}")

        capturable = page.get("capturable_states", [])
        if capturable:
            cap_descs = [f"{cs['state_id']}: {cs.get('description', '')}" for cs in capturable]
            lines.append(f"  Capturable states (in order): {'; '.join(cap_descs)}")

        lines.append("")

    return "\n".join(lines)


def _build_screens_text(screens: list[dict]) -> str:
    """Build a text list of Figma screens (for screens without images)."""
    lines = ["## Figma Screens (no image available)\n"]
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
            console.print(f"  [dim]{n_excluded} obsolete screen(s) excluded from matching[/dim]")

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

    from figma_audit.utils.progress import get_progress

    run_progress = get_progress()
    n_total_screens = len(screens_with_images) + len(screens_without_images)
    processed_screens = 0
    if run_progress and n_total_screens > 0:
        run_progress.update(
            step=f"Matching 0/{n_total_screens} screens",
            progress=0,
            total=n_total_screens,
        )

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
            if run_progress:
                run_progress.update(
                    step=(
                        f"Matching {processed_screens}/{n_total_screens} screens "
                        f"(vision batch {batch_idx + 1}/{len(batches)})"
                    ),
                    progress=processed_screens,
                    total=n_total_screens,
                )

            image_paths = []
            screen_list_text = "## Figma Screens in this batch\n\n"
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
                "The images above correspond to the listed Figma screens, in order. "
                "For each screen, find the matching route in the application."
            )

            result = client.analyze_with_images(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=user_prompt,
                images=image_paths,
                max_tokens=8192,
                phase="match",
            )
            all_mappings.extend(result.get("mappings", []))
            processed_screens += len(batch)
            if run_progress:
                run_progress.update(
                    step=f"Matching {processed_screens}/{n_total_screens} screens",
                    progress=processed_screens,
                    total=n_total_screens,
                )

    # ── Batch 2: Screens without images (text-only) ───────────────
    if screens_without_images:
        console.print(f"[bold]Text-only matching ({len(screens_without_images)} screens)...[/bold]")
        if run_progress:
            run_progress.update(
                step=(
                    f"Matching {processed_screens}/{n_total_screens} screens "
                    f"(text-only, {len(screens_without_images)} remaining)"
                ),
                progress=processed_screens,
                total=n_total_screens,
            )

        screens_text = _build_screens_text(screens_without_images)
        user_prompt = (
            f"{routes_text}\n\n"
            f"{screens_text}\n\n"
            "For each Figma screen listed above (no image available), "
            "find the matching route based solely on the screen name. "
            "Indicate a lower confidence since matching is done without an image."
        )

        result = client.analyze(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=8192,
            phase="match",
        )
        all_mappings.extend(result.get("mappings", []))
        processed_screens += len(screens_without_images)
        if run_progress:
            run_progress.update(
                step=f"Matching {processed_screens}/{n_total_screens} screens",
                progress=processed_screens,
                total=n_total_screens,
            )

    # ── Post-pass: cross-batch state disambiguation ──────────────
    # Each vision batch processes its subset independently and cannot see
    # what other batches assigned. When several Figma screens end up
    # mapped to the same (page_id, state_id), compare in Phase 5
    # collapses them all against the single app capture and produces
    # cascading MATCHING_ERROR discrepancies. This pass detects those
    # collisions and asks the model to re-disambiguate the group in a
    # single dedicated vision call, where it CAN see every candidate at
    # once.
    _disambiguate_states(
        all_mappings,
        screens_by_id={s["id"]: s for s in screens},
        output_dir=output_dir,
        pages_manifest=pages_manifest,
        client=client,
    )

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
