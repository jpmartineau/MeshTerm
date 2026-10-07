# SPDX-License-Identifier: Apache-2.0
"""Download the Terminus 12-pixel fonts that the emulator draws in: one time, only on request.

Terminus has the SIL Open Font License. Thus the repository and the wheel never carry it.
The Debian package for the Cardputer Zero carries it next to its licence, because an app
from the launcher has no terminal in which to run this download. That build also uses
this module (refer to ``scripts/cardputer-zero/build-deb.sh``). This module
downloads the upstream release and checks that it is the correct archive. Then it
extracts ``ter-u12n.bdf``, ``ter-u12b.bdf``, and the licence next to them into the
directory that the emulator reads (:func:`~.font.font_dir`). ``meshterm emulate
--fetch-fonts`` runs it.

Python can fail to download the archive, for example when a certificate store rejects the
mirror. Then you can download the archive in a different way and give it with
``--archive``. In both cases, the module checks the archive against the same digest.
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
#: The SHA-256 of the release archive, so that a mirror cannot return a different file.
SHA256 = "d961c1b781627bf417f9b340693d64fc219e0113ad3a3af1a3424c7aa373ef79"
_ROOT = "terminus-font-4.49.1/"
_WANTED = (REGULAR_BDF, BOLD_BDF, "OFL.TXT")


def install_terminus(archive: Path | None = None, say: Callable[[str], None] = print) -> Path:
    """Download (or take) Terminus 4.49.1, check it, and install the fonts and the licence.

    Args:
        archive: The release archive, already downloaded. ``None`` downloads it.
        say: Where the progress lines go.

    Returns:
        The directory that the fonts were installed into.

    Raises:
        ValueError: The archive is not the release (its digest is different), or a file
            is missing from it.
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
