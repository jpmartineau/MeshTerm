# SPDX-License-Identifier: Apache-2.0
"""The only way that MeshTerm replaces a file that it owns: all the new contents, or none.

Each store in ``core/`` keeps its state in one file, and it writes that file again in full.
If a store writes in place, and a crash (or a laptop lid that closes) occurs during the
write, a truncated file is left where a contact list was. Thus each write goes to a
temporary file next to the real file first. Then one rename puts it in position. The
filesystem does the rename completely or not at all.

The name of the temporary file contains the id of the process that writes it. This detail
is necessary. All the copies of MeshTerm on the machine share its data directory: a
checkout, a downloaded build, or a second instance for a second companion. With a fixed
``.tmp`` name, when two copies write at the same time, each copy overwrites the same
temporary file in turn. Then each copy renames the contents of that file over the real
file. The rename is atomic, but the file that it renamed was not. With a name for each
process, the two writes are independent again, and the last rename wins. The result is a
lost change, not a corrupted file.

Use ``MESHTERM_HOME`` if you do not want copies to share the directory at all (refer to
:func:`meshterm.core.config.default_config_dir`).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

__all__ = ["write_atomically"]


def write_atomically(path: Path, text: str, *, owner_only: bool = False) -> None:
    """Replace ``path`` with ``text`` in one step, and make parent directories if necessary.

    Args:
        path: The file to replace. Its directory is created if it does not exist.
        text: The complete new contents, written as UTF-8.
        owner_only: Limit the file to the owner (``0600``) before it goes into position.
            Use it for each file that holds a credential. The function applies it to the
            temporary file, not to the destination. Thus the real file never exists with
            wider permissions. This limit is best-effort: not all platforms or filesystems
            obey it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        if owner_only:
            try:
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:  # pragma: no cover - platform-dependent
                pass
        tmp.replace(path)
    except BaseException:
        # A failed write leaves no temporary file behind. `missing_ok` is necessary,
        # because the failure can be in the write that makes the temporary file.
        tmp.unlink(missing_ok=True)
        raise
