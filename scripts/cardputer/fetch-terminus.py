#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Fetch the Terminus 12-pixel BDF fonts the Cardputer console host draws in.

Terminus is licensed under the SIL Open Font License, so MeshTerm never ships it: this
downloads the upstream release, checks it is the archive it should be, and extracts
``ter-u12n.bdf``, ``ter-u12b.bdf`` and the licence beside them into the directory the host
reads (``$MESHTERM_HOST_FONTS``, else ``fonts`` under the MeshTerm config directory).

Run it once on a machine that will run the host or its desktop simulator:

    python scripts/cardputer/fetch-terminus.py

Where Python can't fetch it (a certificate store that rejects the mirror), download the
archive any other way and hand it over; it is checked against the same digest:

    python scripts/cardputer/fetch-terminus.py --archive terminus-font-4.49.1.tar.gz
"""

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from meshterm.host.font import BOLD_BDF, REGULAR_BDF, font_dir  # noqa: E402

URL = (
    "https://sourceforge.net/projects/terminus-font/files/terminus-font-4.49/"
    "terminus-font-4.49.1.tar.gz/download"
)
#: The release archive's SHA-256, so a mirror can't hand back something else.
SHA256 = "d961c1b781627bf417f9b340693d64fc219e0113ad3a3af1a3424c7aa373ef79"
ROOT = "terminus-font-4.49.1/"
WANTED = (REGULAR_BDF, BOLD_BDF, "OFL.TXT")


def main(argv: list[str]) -> int:
    """Fetch (or take) the archive, verify it, and extract the fonts and their licence."""
    target = font_dir()
    if argv[:1] == ["--archive"] and len(argv) == 2:
        print(f"installing Terminus 4.49.1 from {argv[1]} into {target}")
        data = Path(argv[1]).read_bytes()
    elif not argv:
        print(f"fetching Terminus 4.49.1 into {target}")
        with urllib.request.urlopen(URL, timeout=60) as response:
            data = response.read()
    else:
        print(__doc__)
        return 2
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        print(f"refusing the download: sha256 {digest} is not the release's {SHA256}")
        return 1
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for name in WANTED:
            member = archive.extractfile(ROOT + name)
            if member is None:
                print(f"the archive has no {name}")
                return 1
            (target / name).write_bytes(member.read())
            print(f"  {name}")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
