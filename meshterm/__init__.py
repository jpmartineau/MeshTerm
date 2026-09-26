# SPDX-License-Identifier: Apache-2.0
"""MeshTerm is a full-featured TUI MeshCore client for your terminal.

Connects to a companion over USB, Bluetooth, or TCP. For Windows, macOS, and Linux.
"""

from datetime import datetime

__version__ = "0.9.0"
__author__ = "Jean-Pierre Martineau"

#: The year MeshTerm was first published; the copyright span starts here.
COPYRIGHT_START_YEAR = 2026


def copyright_notice() -> str:
    """The one canonical copyright line, dated from :data:`COPYRIGHT_START_YEAR` to now.

    A single publication year collapses to just that year ("Copyright © 2026 …"); once
    time has moved on it becomes a span ("Copyright © 2026-2027 …"). Every splash and
    screen that shows a copyright draws it from here so the wording, holder, and span can
    never drift apart.
    """
    year = datetime.now().year
    span = (
        str(COPYRIGHT_START_YEAR)
        if year <= COPYRIGHT_START_YEAR
        else f"{COPYRIGHT_START_YEAR}-{year}"
    )
    return f"Copyright © {span} {__author__}"
