# SPDX-License-Identifier: Apache-2.0
"""Fetch the Terminus 12-pixel fonts the emulator draws in — once, and only when asked.

Terminus is licensed under the SIL Open Font License, so MeshTerm never ships it: this
downloads the upstream release, checks it is the archive it should be, and extracts
``ter-u12n.bdf``, ``ter-u12b.bdf`` and the licence beside them into the directory the
emulator reads (:func:`~.font.font_dir`). ``meshterm emulate --fetch-fonts`` runs it.

Where Python can't fetch it — a certificate store that rejects the mirror — the archive can
be downloaded any other way and handed over with ``--archive``; it is checked against the
same digest either way.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .font import BOLD_BDF, REGULAR_BDF, font_dir

URL = (
    "https://sourceforge.net/projects/terminus-font/files/terminus-font-4.49/"
    "terminus-font-4.49.1.tar.gz/download"
)
#: The release archive's SHA-256, so a mirror can't hand back something else.
SHA256 = "d961c1b781627bf417f9b340693d64fc219e0113ad3a3af1a3424c7aa373ef79"
_ROOT = "terminus-font-4.49.1/"
_WANTED = (REGULAR_BDF, BOLD_BDF, "OFL.TXT")


def install_terminus(archive: Path | None = None, say: Callable[[str], None] = print) -> Path:
    """Download (or take) Terminus 4.49.1, verify it, and install the fonts and licence.

    Args:
        archive: The release archive, already downloaded; ``None`` downloads it.
        say: Where progress lines go.

    Returns:
        The directory the fonts were installed into.

    Raises:
        ValueError: The archive is not the release (its digest differs) or lacks a file.
        OSError: The download or a write failed.
    """
    target = font_dir()
    if archive is not None:
        say(f"installing Terminus 4.49.1 from {archive} into {target}")
        data = archive.read_bytes()
    else:
        say(f"fetching Terminus 4.49.1 into {target}")
        with urllib.request.urlopen(URL, timeout=60) as response:  # noqa: S310 - fixed https URL
            data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        raise ValueError(f"refusing the archive: sha256 {digest} is not the release's {SHA256}")
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for name in _WANTED:
            member = tar.extractfile(_ROOT + name)
            if member is None:
                raise ValueError(f"the archive has no {name}")
            (target / name).write_bytes(member.read())
            say(f"  {name}")
    return target
