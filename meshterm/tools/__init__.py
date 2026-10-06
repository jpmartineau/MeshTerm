# SPDX-License-Identifier: Apache-2.0
"""Tools that plug in: the menu items and the CLI subcommands.

When this package is imported, it imports each tool module immediately. Thus the
``@register`` decorator of each module adds its tools to the shared registry. A new feature
must only add a module here.
"""

from __future__ import annotations

import importlib
import pkgutil

from .base import Tool, all_tools, get_tool, register

__all__ = ["Tool", "all_tools", "get_tool", "register", "load_all_tools"]


def load_all_tools() -> None:
    """Import each sibling module, so that its tools add themselves to the registry.

    This function does not import the modules whose names start with an underscore, or
    ``base``.
    """
    package = __name__
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_") or mod.name == "base":
            continue
        importlib.import_module(f"{package}.{mod.name}")


# Fill the registry when the package is imported, so that the CLI and the menu get each tool.
load_all_tools()
