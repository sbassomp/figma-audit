"""figma-audit CLI command registry.

Each command sub-module imports the shared ``cli`` Click group from
:mod:`group` and registers its commands via ``@cli.command()`` decorators.
Importing this package triggers all sub-modules and makes every command
discoverable.

The ``__main__.py`` entry point is a slim 2-liner that imports ``cli``
from here and invokes it.
"""

from __future__ import annotations

# Import sub-modules so their @cli.command() decorators register against
# the shared cli group. The order does not matter functionally.
from figma_audit.cli import (  # noqa: F401 — registration side effects
    agents,
    imports,
    phases,
    serve,
    setup,
)
from figma_audit.cli import run as run_cmd  # noqa: F401 — registration side effects
from figma_audit.cli.group import cli

__all__ = ["cli"]
