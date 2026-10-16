# SPDX-License-Identifier: Apache-2.0
"""Make sure that each grey that MeshTerm defines is neutral: red, green, and blue are equal.

A grey is chrome or an absence (``muted``, ``faint``, a keyless chip, a minor road), and
it must not have a hue. Tailwind's slate greys were cold, and a cold grey on each screen
added one more colour that had no meaning (JP, 2026-10-14). Thus a colour that the eye
reads as a grey must be ``#rrggbb`` with ``rr == gg == bb``.

The test reads each ``#rrggbb`` string literal in the package and measures its chroma in
OKLab. Below :data:`GREY_CHROMA`, the colour is a grey, and it must be neutral. The
docstrings are not read, because an example in a docstring is not a colour that MeshTerm
draws. The archived console palette (``theme._VT_SLOTS_CUSTOM``) is not read, because it
is a record of what shipped first, and MeshTerm does not install it.
"""

import ast
import math
import re
from pathlib import Path

from meshterm.ui import oklab
from tests.conftest import not_ignored

PACKAGE = Path(__file__).parent.parent / "meshterm"

#: The OKLab chroma below which the eye reads a colour as a grey. The coldest slate that
#: MeshTerm used (``#64748b``) measured 0.041. The dimmest colour that has a meaning (the
#: dark green of a park on the map, ``#173a29``) measures 0.051.
GREY_CHROMA = 0.045

_HEX = re.compile(r"#([0-9a-fA-F]{6})\b")

#: The assignments that the test does not read (refer to the module docstring).
_ARCHIVED = {"_VT_SLOTS_CUSTOM"}


def _skipped_nodes(tree: ast.AST) -> set[int]:
    """The ids of the docstring nodes and of each node in an archived assignment."""
    skipped: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            skipped.add(id(node.value))
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if any(isinstance(t, ast.Name) and t.id in _ARCHIVED for t in targets):
            skipped.update(id(n) for n in ast.walk(node))
    return skipped


def _literal_colours() -> list[tuple[str, int, str]]:
    """Each ``#rrggbb`` in a string literal of the package: (file, line, colour)."""
    found = []
    for path in not_ignored(sorted(PACKAGE.rglob("*.py"))):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skipped = _skipped_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skipped
            ):
                for match in _HEX.finditer(node.value):
                    found.append(
                        (str(path.relative_to(PACKAGE.parent)), node.lineno, match.group(1))
                    )
    return found


def test_the_scan_finds_the_greys_of_the_theme() -> None:
    """The scan reads the theme. If it does not, the main test passes with no meaning."""
    colours = {colour.lower() for _, _, colour in _literal_colours()}
    assert {"a2a2a2", "737373", "404040"} <= colours


def test_each_grey_is_neutral() -> None:
    """A colour with almost no chroma must have equal red, green, and blue."""
    tinted = []
    for file, line, colour in _literal_colours():
        _, a, b = oklab.from_hex(colour)
        r, g, b_ = (int(colour[i : i + 2], 16) for i in (0, 2, 4))
        if math.hypot(a, b) < GREY_CHROMA and not r == g == b_:
            tinted.append(f"  {file}:{line}: #{colour}")
    assert not tinted, (
        "These greys have a tint. Make each one r=g=b at the same OKLab lightness, or, if "
        "the colour has a meaning, give it more chroma:\n" + "\n".join(tinted)
    )
