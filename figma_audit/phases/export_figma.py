"""Phase 2: Export Figma — Download file tree, extract tokens, export screen PNGs."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from figma_audit.config import Config
from figma_audit.models import Bounds, FigmaElement, FigmaManifest, FigmaScreen, FileMeta
from figma_audit.utils.color import rgba_to_hex
from figma_audit.utils.figma_client import FigmaClient, load_cache, save_cache

console = Console()

# Prefixes/patterns to skip when identifying screens
SKIP_NAME_PATTERNS = re.compile(
    r"^("
    r"_|"
    r"Component/|Icon/|"
    r"icon|ico-|"
    r"bg-|"
    r"Connector|"
    r"Phosphor|"
    r"Whatsapp|"
    r"Vector|"
    r"flutter-view|"
    r"Ellipse|"
    r"Rectangle|"
    r"Line|"
    r"Image|"
    r"Group"
    r")",
    re.IGNORECASE,
)

# Only FRAME (and SECTION as container) are valid screen types.
# Everything else (COMPONENT, COMPONENT_SET, INSTANCE, CONNECTOR, etc.)
# is a design-system element, not a screen.
SCREEN_TYPES = {"FRAME", "SECTION"}

# Reasonable mobile screen dimensions
MIN_SCREEN_WIDTH = 300
MIN_SCREEN_HEIGHT = 500
MAX_SCREEN_WIDTH = 1920  # Skip overly wide desktop frames / layout grids


def _slugify(name: str) -> str:
    """Convert a Figma screen name to a filesystem-safe slug."""
    s = name.lower().strip()
    # Replace / with - before stripping other special chars
    s = s.replace("/", "-")
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    # Truncate overly long slugs (keep max 80 chars)
    if len(s) > 80:
        s = s[:80].rstrip("-")
    return s


def _is_screen_candidate(node: dict) -> bool:
    """Heuristic: is this top-level node likely a screen?"""
    node_type = node.get("type", "")
    name = node.get("name", "")

    # Only accept FRAME / SECTION types — reject components, instances, etc.
    if node_type not in SCREEN_TYPES:
        return False

    if SKIP_NAME_PATTERNS.match(name):
        return False

    # Check dimensions if available
    bbox = node.get("absoluteBoundingBox", {})
    w = bbox.get("width", 0)
    h = bbox.get("height", 0)
    if w > 0 and h > 0:
        if w < MIN_SCREEN_WIDTH or h < MIN_SCREEN_HEIGHT:
            return False
        if w > MAX_SCREEN_WIDTH:
            return False

    return True


def _extract_background_color(node: dict) -> str | None:
    """Extract background color from a node's fills."""
    bg = node.get("backgroundColor")
    if bg:
        return rgba_to_hex(bg.get("r", 0), bg.get("g", 0), bg.get("b", 0), bg.get("a", 1))

    fills = node.get("fills", [])
    for fill in fills:
        if fill.get("type") == "SOLID" and fill.get("visible", True):
            c = fill.get("color", {})
            return rgba_to_hex(c.get("r", 0), c.get("g", 0), c.get("b", 0), c.get("a", 1))
    return None


def _extract_elements(node: dict, depth: int = 0, max_depth: int = 5) -> list[FigmaElement]:
    """Recursively extract design elements (text, shapes) from a node tree."""
    if depth > max_depth:
        return []

    elements: list[FigmaElement] = []
    children = node.get("children", [])

    for child in children:
        child_type = child.get("type", "")
        bbox = child.get("absoluteBoundingBox", {})
        bounds = None
        if bbox:
            bounds = Bounds(
                x=bbox.get("x", 0),
                y=bbox.get("y", 0),
                w=bbox.get("width", 0),
                h=bbox.get("height", 0),
            )

        if child_type == "TEXT":
            style = child.get("style", {})
            fills = child.get("fills", [])
            color = None
            for f in fills:
                if f.get("type") == "SOLID" and f.get("visible", True):
                    c = f.get("color", {})
                    color = rgba_to_hex(c.get("r", 0), c.get("g", 0), c.get("b", 0), c.get("a", 1))
                    break

            elements.append(
                FigmaElement(
                    type="TEXT",
                    name=child.get("name"),
                    content=child.get("characters", ""),
                    font_family=style.get("fontFamily"),
                    font_size=style.get("fontSize"),
                    font_weight=style.get("fontWeight"),
                    letter_spacing=style.get("letterSpacing"),
                    line_height=style.get("lineHeightPx"),
                    color=color,
                    bounds=bounds,
                )
            )
        elif child_type in ("RECTANGLE", "ELLIPSE", "VECTOR", "LINE"):
            fills = child.get("fills", [])
            fill_color = None
            for f in fills:
                if f.get("type") == "SOLID" and f.get("visible", True):
                    c = f.get("color", {})
                    fill_color = rgba_to_hex(
                        c.get("r", 0), c.get("g", 0), c.get("b", 0), c.get("a", 1)
                    )
                    break

            elements.append(
                FigmaElement(
                    type=child_type,
                    name=child.get("name"),
                    fill=fill_color,
                    corner_radius=child.get("cornerRadius"),
                    bounds=bounds,
                )
            )

        # Recurse into children
        elements.extend(_extract_elements(child, depth + 1, max_depth))

    return elements


def _identify_screens(file_data: dict, target_page_id: str | None = None) -> list[dict]:
    """Identify screen frames from the Figma file tree.

    Returns list of {"id", "name", "page", "width", "height", "node"}.
    """
    screens = []
    pages = file_data.get("document", {}).get("children", [])

    for page in pages:
        page_name = page.get("name", "")
        page_id = page.get("id", "")

        # If a target page is specified, only process that page
        if target_page_id and page_id != target_page_id:
            continue

        for child in page.get("children", []):
            if not _is_screen_candidate(child):
                continue

            bbox = child.get("absoluteBoundingBox", {})
            w = bbox.get("width", 0)
            h = bbox.get("height", 0)

            slug = _slugify(child.get("name", "unnamed"))
            # Deduplicate filenames
            existing_names = [s["filename"] for s in screens]
            filename = f"{slug}.png"
            counter = 2
            while filename in existing_names:
                filename = f"{slug}-{counter}.png"
                counter += 1

            screens.append(
                {
                    "id": child["id"],
                    "name": child.get("name", ""),
                    "page": page_name,
                    "width": w,
                    "height": h,
                    "filename": filename,
                    "node": child,
                }
            )

    return screens


def _check_cache_valid(
    client: FigmaClient,
    file_key: str,
    cache_dir: Path,
) -> bool:
    """Check if local cache is still valid by comparing lastModified."""
    meta_path = cache_dir / "file_meta.json"
    cached_meta = load_cache(meta_path)
    if not cached_meta or not isinstance(cached_meta, dict):
        return False

    cached_modified = cached_meta.get("last_modified")
    if not cached_modified:
        return False

    current_meta = client.get_file_meta(file_key)
    current_modified = current_meta.get("last_modified")

    if cached_modified == current_modified:
        console.print("[green]Cache is valid (file not modified since last download).[/green]")
        return True

    console.print(
        f"[yellow]File modified since last cache "
        f"({cached_modified} → {current_modified}). Re-downloading.[/yellow]"
    )
    return False


def run(
    config: Config,
    *,
    force_refresh: bool = False,
    offline: bool = False,
    target_page: str | None = None,
) -> Path:
    """Run Phase 2: Export Figma.

    Args:
        config: Application configuration.
        force_refresh: Force re-download even if cache is valid.
        offline: Work only from local cache, no API calls.
        target_page: Figma page ID to focus on (e.g. "45:927").

    Returns:
        Path to the generated figma_manifest.json.
    """
    file_key = config.figma_file_key
    if not file_key:
        raise ValueError("No Figma file key found. Provide --figma-url or --figma-file.")

    cache_dir = config.figma_cache_dir
    screens_dir = config.figma_screens_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    screens_dir.mkdir(parents=True, exist_ok=True)

    file_json_path = cache_dir / "file.json"
    meta_path = cache_dir / "file_meta.json"
    manifest_path = config.output_dir / "figma_manifest.json"

    # ── Step 1: Get file tree ──────────────────────────────────────
    from_fig_file = bool(config.figma_file)

    if from_fig_file:
        fig_path = config.figma_file_path
        console.print(f"[bold]Parsing local .fig file: {fig_path}[/bold]")
        from figma_audit.utils.fig_parser import parse_fig_file

        file_data = parse_fig_file(fig_path)
        save_cache(file_data, file_json_path)
        console.print(f"[green]Parsed file tree saved to {file_json_path}[/green]")
    elif offline:
        console.print("[bold]Mode offline — utilisation du cache local.[/bold]")
        if not file_json_path.exists():
            raise FileNotFoundError(
                f"No cached file.json at {file_json_path}. Run without --offline first."
            )
        with open(file_json_path) as f:
            file_data = json.load(f)
    else:
        if not config.figma_token:
            raise ValueError("No Figma token. Set FIGMA_TOKEN env var or provide --figma-token.")

        client = FigmaClient(config.figma_token, config.figma)

        # Check cache validity
        use_cache = False
        if not force_refresh and file_json_path.exists():
            use_cache = _check_cache_valid(client, file_key, cache_dir)

        if use_cache:
            with open(file_json_path) as f:
                file_data = json.load(f)
        else:
            file_data = client.get_file(file_key)
            save_cache(file_data, file_json_path)
            meta = FileMeta(
                file_key=file_key,
                file_name=file_data.get("name", ""),
                last_modified=file_data.get("lastModified"),
                version=file_data.get("version"),
                downloaded_at=datetime.now(timezone.utc).isoformat(),
            )
            save_cache(meta.model_dump(), meta_path)
            console.print(f"[green]File tree saved to {file_json_path}[/green]")

    # ── Step 2: Identify screens ───────────────────────────────────
    screens = _identify_screens(file_data, target_page_id=target_page)
    console.print(f"[bold]Found {len(screens)} screen candidates.[/bold]")

    for s in screens:
        dim = f"{s['width']:.0f}x{s['height']:.0f}" if s["width"] else "?x?"
        console.print(f"  {s['id']:15s} {dim:>12s}  {s['name']}")

    # ── Step 3: Download screen PNGs ───────────────────────────────
    # Skip when using .fig file (no rendering API — user imports PNGs via import-screens)
    if not offline and not from_fig_file:
        client.download_screens(
            file_key,
            screens,
            screens_dir,
            scale=config.viewport.device_scale_factor,
        )

    # ── Step 4: Build manifest ─────────────────────────────────────
    figma_screens: list[FigmaScreen] = []
    for s in screens:
        node = s["node"]
        bg = _extract_background_color(node)
        elements = _extract_elements(node)

        image_path = f"figma_screens/{s['filename']}"
        if not (config.output_dir / image_path).exists():
            image_path = None

        figma_screens.append(
            FigmaScreen(
                id=s["id"],
                name=s["name"],
                page=s["page"],
                width=s["width"],
                height=s["height"],
                image_path=image_path,
                background_color=bg,
                elements=elements,
            )
        )

    manifest = FigmaManifest(
        file_key=file_key,
        file_name=file_data.get("name", ""),
        screens=figma_screens,
    )

    save_cache(manifest.model_dump(), manifest_path)
    console.print(f"\n[bold green]Manifest saved to {manifest_path}[/bold green]")
    console.print(
        f"  {len(figma_screens)} screens, "
        f"{sum(len(s.elements) for s in figma_screens)} elements extracted."
    )

    return manifest_path
