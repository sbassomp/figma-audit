"""Figma REST API client with rate limiting and caching."""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeRemainingColumn

from figma_audit.config import FigmaConfig

console = Console()

FIGMA_API_BASE = "https://api.figma.com/v1"


class FigmaClientError(Exception):
    pass


class FigmaClient:
    """Client REST Figma avec rate limiting, retry, et cache local."""

    def __init__(self, token: str, config: FigmaConfig | None = None):
        self.token = token
        self.config = config or FigmaConfig()
        self.session = requests.Session()
        self.session.headers.update({"X-Figma-Token": token})
        self._last_request_time: float = 0

    def _wait_rate_limit(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self.config.request_delay:
            wait = self.config.request_delay - elapsed
            time.sleep(wait)

    def _request(self, endpoint: str, params: dict | None = None) -> dict:
        url = f"{FIGMA_API_BASE}{endpoint}"
        for attempt in range(1, self.config.max_retries + 1):
            self._wait_rate_limit()
            self._last_request_time = time.time()

            resp = self.session.get(url, params=params)

            if resp.status_code == 200:
                return resp.json()

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", self.config.retry_wait_default))
                console.print(
                    f"[yellow]Rate limited (429). Attente {retry_after}s "
                    f"(tentative {attempt}/{self.config.max_retries})[/yellow]"
                )
                time.sleep(retry_after)
                continue

            if resp.status_code >= 500 and attempt < self.config.max_retries:
                wait = self.config.retry_wait_default * attempt
                console.print(
                    f"[yellow]Erreur serveur {resp.status_code}. Retry dans {wait}s[/yellow]"
                )
                time.sleep(wait)
                continue

            raise FigmaClientError(
                f"Figma API error {resp.status_code} on {endpoint}: {resp.text[:200]}"
            )

        raise FigmaClientError(f"Max retries ({self.config.max_retries}) exceeded for {endpoint}")

    def get_file(self, file_key: str) -> dict:
        """Fetch the full file tree."""
        console.print(f"[bold]Fetching file tree for {file_key}...[/bold]")
        return self._request(f"/files/{file_key}")

    def get_file_meta(self, file_key: str) -> dict:
        """Fetch file metadata only (lightweight, for cache validation)."""
        data = self._request(f"/files/{file_key}", params={"depth": "1"})
        return {
            "file_key": file_key,
            "file_name": data.get("name", ""),
            "last_modified": data.get("lastModified", ""),
            "version": data.get("version", ""),
        }

    def export_images(
        self,
        file_key: str,
        node_ids: list[str],
        scale: int = 2,
        format: str = "png",
    ) -> dict[str, str]:
        """Export nodes as images. Returns {node_id: image_url}."""
        result: dict[str, str] = {}
        batches = [
            node_ids[i : i + self.config.batch_size]
            for i in range(0, len(node_ids), self.config.batch_size)
        ]

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Export images", total=len(node_ids))
            for batch in batches:
                ids_str = ",".join(batch)
                data = self._request(
                    f"/images/{file_key}",
                    params={"ids": ids_str, "scale": str(scale), "format": format},
                )
                images = data.get("images", {})
                for node_id, url in images.items():
                    if url:
                        result[node_id] = url
                progress.update(task, advance=len(batch))

        return result

    def download_image(self, url: str, dest: Path) -> None:
        """Download an image URL to a local file."""
        resp = self.session.get(url, stream=True)
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

    def download_screens(
        self,
        file_key: str,
        screens: list[dict],
        output_dir: Path,
        scale: int = 2,
    ) -> list[dict]:
        """Download all screen PNGs. Skips already-cached files.

        Args:
            screens: list of {"id": "123:456", "name": "Screen Name", "filename": "screen-name.png"}
            output_dir: directory for PNG files

        Returns:
            Updated screens list with download status.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        # Filter out already downloaded
        to_download = []
        for screen in screens:
            dest = output_dir / screen["filename"]
            if dest.exists() and dest.stat().st_size > 0:
                screen["downloaded"] = True
                console.print(f"  [dim]Cache hit: {screen['filename']}[/dim]")
            else:
                screen["downloaded"] = False
                to_download.append(screen)

        if not to_download:
            console.print("[green]All screens already cached.[/green]")
            return screens

        console.print(
            f"[bold]Downloading {len(to_download)} screen(s) "
            f"({len(screens) - len(to_download)} cached)...[/bold]"
        )

        # Export images via Figma API
        node_ids = [s["id"] for s in to_download]
        url_map = self.export_images(file_key, node_ids, scale=scale)

        # Download PNGs
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Download PNGs", total=len(to_download))
            for screen in to_download:
                url = url_map.get(screen["id"])
                if url:
                    dest = output_dir / screen["filename"]
                    self.download_image(url, dest)
                    screen["downloaded"] = True
                else:
                    console.print(f"  [red]No URL for {screen['name']} ({screen['id']})[/red]")
                progress.update(task, advance=1)

        downloaded = sum(1 for s in screens if s.get("downloaded"))
        console.print(f"[green]Downloaded {downloaded}/{len(screens)} screens.[/green]")
        return screens


def save_cache(data: dict | list, path: Path) -> None:
    """Save data to a JSON cache file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_cache(path: Path) -> dict | list | None:
    """Load data from a JSON cache file, or None if not found."""
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)
