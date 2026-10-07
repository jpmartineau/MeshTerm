# SPDX-License-Identifier: Apache-2.0
"""Keep the docstrings, the comments, and the documents in ASD-STE100.

MeshTerm writes its technical text in ASD-STE100 Simplified Technical English (refer to
``docs/development/writing-style.md``). Most of the rules of STE need a human reader. This
test finds the marks that a program can find: the em dash, the semicolon, the contraction,
and the Latin abbreviation. It does not examine text in code font, text in double quotes,
code blocks, or URLs, because code and quoted UX strings can contain these marks.

The test examines the docstrings and the comments of the Python files, and the prose of
the documents. It does not examine the legal text, the written pages in
``meshterm/assets/pages/``, or the code of conduct, because these texts are not in STE.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: The folders whose Python files are examined.
PYTHON_DIRS = ("meshterm", "tests", "scripts", "packaging")

#: Python files without the ``.py`` suffix.
PYTHON_EXTRA = ("scripts/uconsole/meshterm-spi-bridge", "packaging/meshterm.spec")

#: The documents that are written in STE. A folder means all its Markdown files.
DOCUMENTS = (
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "docs",
    "scripts",
)

#: Each mark that STE does not use, and the name that the failure message gives it.
MARKS = (
    ("em dash", re.compile("—")),
    ("semicolon", re.compile(";")),
    ("contraction", re.compile(r"\b[A-Za-z]+(?:n't|'re|'ve|'ll|'d|'m)\b")),
    ("contraction", re.compile(r"\b(?:[Ii]t|[Tt]hat|[Tt]here|[Ww]hat|[Hh]ere|[Ww]ho)'s\b")),
    ("Latin abbreviation", re.compile(r"\b(?:e\.g|i\.e|etc)\.", re.IGNORECASE)),
)

#: Text that is not examined in one line of a document: code in backticks, text in double
#: quotes, and URLs.
QUOTED = re.compile(r"``.*?``|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”|<?https?://[^\s)>]+>?")

#: The same, for a docstring or a block of comments. Here a code span or a quotation can
#: continue on the next line. A limit on the length keeps a stray quote mark from hiding a
#: large part of the text.
QUOTED_BLOCK = re.compile(
    r"``.{0,300}?``|`[^`\n]*`|\"[^\"]{0,600}\"|“[^”]{0,600}”|<?https?://[^\s)>]+>?",
    re.DOTALL,
)

#: A Markdown link target: ``](target)``. The target is a path or a URL, not prose.
LINK_TARGET = re.compile(r"\]\([^)\s]*\)")

#: An HTML comment on one line.
HTML_COMMENT = re.compile(r"<!--.*?-->")

#: The quote marks at the start of a line in a quote block.
QUOTE_MARK = re.compile(r"^(?:\s*>)+ ?")

#: The markers around legal text in a document, which is not in STE on purpose.
STE_OFF = "<!-- ste: off -->"
STE_ON = "<!-- ste: on -->"


def _blank_quotes(text: str) -> str:
    """``text`` with each code span and quotation changed to spaces. The newlines stay."""
    return QUOTED_BLOCK.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), text)


def _python_files() -> list[Path]:
    files = [p for d in PYTHON_DIRS for p in (ROOT / d).rglob("*.py")]
    files += [ROOT / p for p in PYTHON_EXTRA]
    return sorted(p for p in files if p.exists() and "__pycache__" not in p.parts)


def _python_prose(path: Path) -> list[tuple[int, str]]:
    """Each line of each docstring and each comment, with its line number and no quotes.

    The lines of one docstring are examined together, and so are the lines of a block of
    comments on consecutive lines. Thus a quotation that continues on the next line is
    still a quotation.
    """
    source = path.read_text(encoding="utf-8")
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            for offset, line in enumerate(_blank_quotes(first.value.value).split("\n")):
                found.append((first.lineno + offset, line))
    blocks: list[list[tuple[int, str]]] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        row = token.start[0]
        if blocks and blocks[-1][-1][0] == row - 1:
            blocks[-1].append((row, token.string))
        else:
            blocks.append([(row, token.string)])
    for block in blocks:
        text = _blank_quotes("\n".join(comment for _, comment in block))
        lines = text.split("\n")
        found.extend((row, line) for (row, _), line in zip(block, lines, strict=True))
    return found


def _documents() -> list[Path]:
    files: list[Path] = []
    for name in DOCUMENTS:
        path = ROOT / name
        files += sorted(path.rglob("*.md")) if path.is_dir() else [path]
    return files


def _markdown_prose(path: Path) -> list[tuple[int, str]]:
    """Each line of prose in a Markdown file, without the code blocks and the legal text.

    A fence can be in a quote block (``> ```bash``). The text between :data:`STE_OFF` and
    :data:`STE_ON` is legal text, which stays as it is (refer to the style guide).
    """
    found: list[tuple[int, str]] = []
    fenced = False
    off = False
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if STE_OFF in line:
            off = True
        if STE_ON in line:
            off = False
        unquoted = QUOTE_MARK.sub("", line)
        if unquoted.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if not (fenced or off) and not unquoted.startswith("    "):
            text = LINK_TARGET.sub("]", HTML_COMMENT.sub(" ", line))
            found.append((number, QUOTED.sub(" ", text)))
    return found


def _marks(path: Path, prose: list[tuple[int, str]]) -> list[str]:
    where = path.relative_to(ROOT).as_posix()
    found = []
    for number, line in prose:
        for name, pattern in MARKS:
            if pattern.search(line):
                found.append(f"{where}:{number}: {name}: {line.strip()[:100]}")
    return found


def test_docstrings_and_comments_are_in_ste():
    """No docstring or comment has an em dash, a semicolon, a contraction, or "e.g."."""
    found = [hit for path in _python_files() for hit in _marks(path, _python_prose(path))]
    assert not found, "Text that is not in STE:\n" + "\n".join(found)


def test_documents_are_in_ste():
    """No document has an em dash, a semicolon, a contraction, or "e.g." in its prose."""
    found = [hit for path in _documents() for hit in _marks(path, _markdown_prose(path))]
    assert not found, "Text that is not in STE:\n" + "\n".join(found)
