# SPDX-License-Identifier: Apache-2.0
"""MeshTerm is a full-featured TUI MeshCore client for your terminal.

Connects to a companion over USB, Bluetooth, or TCP. For Windows, macOS, and Linux.
"""

from datetime import datetime

__version__ = "0.10.2"
__author__ = "Jean-Pierre Martineau"

#: The year when MeshTerm was first published. The copyright span starts in this year.
COPYRIGHT_START_YEAR = 2026


def copyright_notice() -> str:
    """The one canonical copyright line, dated from :data:`COPYRIGHT_START_YEAR` to now.

    While the year is the publication year, the line shows only that year ("Copyright ©
    2026 …"). In a later year, it shows a span ("Copyright © 2026-2027 …"). Each splash and
    each screen that shows a copyright gets it from this function. Thus the wording, the
    holder, and the span always agree.
    """
    year = datetime.now().year
    span = (
        str(COPYRIGHT_START_YEAR)
        if year <= COPYRIGHT_START_YEAR
        else f"{COPYRIGHT_START_YEAR}-{year}"
    )
    return f"Copyright © {span} {__author__}"
