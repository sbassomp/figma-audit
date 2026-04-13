"""CLI entry point for ``python -m figma_audit``.

The actual commands live in the :mod:`figma_audit.cli` sub-package, split
into focused modules per command group. This file is intentionally minimal
so it only handles the entry point convention.
"""

from __future__ import annotations

from figma_audit.cli import cli

if __name__ == "__main__":
    cli()
