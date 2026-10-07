# SPDX-License-Identifier: Apache-2.0
"""The two faces of one answer: a renderer turns a report into bytes.

A tool states its answer as a :mod:`~meshterm.ui.report`. This module changes that report
into output, and the CLI boundary picks which renderer runs (refer to
:func:`~meshterm.cli.main_callback`). The code above this module does not know what a
gutter or a comma is. The code below this module does not know what a contact is. This
separation is the purpose of the design. A third format later (YAML, CSV, or other
formats that somebody needs) is a new class in this file, and not a change to twenty tools.

**Plain** (:class:`PlainRenderer`) is for a person at a prompt. It has aligned columns,
ages, a route drawn with arrows, and a ``sysctl -a`` block for a set of facts about one
thing. It uses the vocabulary of :mod:`meshterm.ui.script`, which has the reasons for each
of its rules.

**JSON** (:class:`JsonRenderer`) is for a program, often on another machine and often
later. It prints **the answer itself**: an array for a listing, and an object for a set of
facts. The output is compact, on one line, in UTF-8, with no ASCII escaping, and with the
keys in the order that the report declares them. There is no wrapper.
``meshterm contacts --json | jq '.[].node.name'`` reads what it looks like it reads. The
report that a caller needs is still ``$?``, the same as without the flag. Thus a failure
prints **nothing at all** on stdout. The ``meshterm: what went wrong`` line stays on
stderr, where each utility puts it. When stdout has a valid document, this is a reliable
signal that the command worked.

Two rules cover a report of more than one block. Together they produce the documents that
the specifications of ``trace`` and ``tx-optimize`` have. A **single-block** report is the
document of its block. A **multi-block** report is one object. Each :class:`Listing` is
under its own key. Each :class:`Facts` is merged in at the top level, because a facts block
is the set of facts about the one thing that the command answered. Thus it is the
document, and not a compartment in it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from enum import Enum
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.text import Text

from . import script
from .report import BARE, SILENT, Block, Facts, Lane, Listing, Report

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext


class OutputFormat(str, Enum):
    """The formats in which a scripted run can print its answer.

    This is an enum and not the ``--json`` boolean that it replaces. Code asked the boolean
    questions that it could not answer (``tx-optimize`` read it as "may I prompt?"). Also,
    a third member of the enum costs one renderer and no change to a tool. ``--json`` stays
    the documented spelling, and it is a short form for this enum.
    """

    PLAIN = "plain"
    JSON = "json"


def markdown_blocks(report: Report | None) -> str:
    """Project a report as markdown sections: the third face, and the only document face.

    The plain face is a stream of records. The machine face is a document for a program.
    This face is a page that a person reads. The person reads it on a screen through
    :func:`~meshterm.ui.markdown.render_markdown`, or in an issue tracker, where no code
    processes it. In this face, :attr:`~meshterm.ui.report.Block.caption` finally has a
    meaning, because markdown has headings, and a user navigates by headings.

    The function writes each value as **inline code**. This is a matter of correctness and
    not of taste. A Windows path has many backslashes, which markdown reads as escapes. An
    error message that the firmware of another person wrote can have an asterisk or a
    bracket. Inside a code span, markdown does not interpret any of these characters. Thus
    the value that arrives is the value that left. This is the same reason that
    :func:`~meshterm.ui.script.name` quotes what it cannot vouch for. If a value already has
    a backtick, it gets a longer fence, so that the fence does not break.

    The rows are a **list**, not a table. We measured this, and did not assume it. A
    markdown table crops its cells with an ellipsis when the frame is narrow. A diagnostics
    page that has one long value cut is missing the half that was important. A list item
    wraps and hangs under its own text at each width.

    Args:
        report: The blocks to project, or ``None`` for nothing at all.

    Returns:
        Markdown source: one ``##`` section for each block that has a caption, separated by
        blank lines.
    """
    sections: list[str] = []
    for block in report or ():
        rows = _markdown_rows(block)
        head = f"## {block.caption}\n\n" if block.caption else ""
        # A section that is empty still says so. An absence is a fact here. A maintainer
        # can act on "no preferences changed". If a section is missing without a word, it
        # only raises the question whether the code ever built it.
        sections.append(head + ("\n".join(rows) if rows else "*none*"))
    return "\n\n".join(sections)


def _markdown_rows(block: Block) -> list[str]:
    """One markdown list item per row of a block."""
    if isinstance(block, Facts):
        return [f"- **{key}** — {_code(value)}" for key, value in facts_pairs(block)]
    lanes = block.lanes()
    if not lanes:
        return []
    items: list[str] = []
    for row in block.rows:
        # The first lane names the record and the other lanes describe it. A listing of two
        # lanes (a table and its count, a preference and its value) needs this. A wider
        # listing degrades to it in a good way.
        cells = [lane.render(row.get(column.key)) for column, lane in lanes]
        described = " · ".join(_code(cell) for cell in cells[1:])
        items.append(f"- **{cells[0]}** — {described}" if described else f"- **{cells[0]}**")
    return items


def _code(value: str) -> str:
    """One value as a markdown code span. The fence is long enough for the backticks in it."""
    if not value:
        return ""
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * (longest + 1)
    # A span that starts or ends with a backtick needs a space, which the parser removes.
    pad = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{fence}{pad}{value}{pad}{fence}"


def facts_pairs(block: Facts) -> list[tuple[str, str]]:
    """Project a :class:`Facts` block into its ``(key, value)`` text rows.

    This is the part of a facts block that the plain face uses. It is in a function outside
    :class:`PlainRenderer`, because one surface in the menu needs the same rows in a
    different layout. The Diagnostics page hangs a long value under its key and does not
    crop it, because a cropped path in a bug report is worse than a wrapped path. When the
    two surfaces share the projection, they cannot drift apart. If a field is added to a
    report, it appears on both surfaces, and nobody must remember to add it.

    The layout of the rows is the work of the caller. The change of a typed value into text
    is not. It stays here, in the declared :class:`~meshterm.ui.report.Lane`.

    Args:
        block: The facts block to project.

    Returns:
        One ``(header, cell)`` pair for each lane, in the order that the block declares.
    """
    rows: list[tuple[str, str]] = []
    for column in block.fields:
        value = block.values.get(column.key)
        if block.omit_absent and (value is None or value == ""):
            continue
        for lane in column.lanes:
            rows.append((lane.header, lane.render(value)))
    return rows


class Renderer:
    """Changes a report into bytes. There is one subclass for each format."""

    def render(self, report: Report | None) -> None:
        """Print a completed report, or nothing at all when there is no report."""
        raise NotImplementedError

    @contextmanager
    def stream(self, listing: Listing) -> Iterator[Any]:
        """Open a live listing whose rows arrive one at a time.

        This is the second seam, and it is narrow. It exists because a stream has no end
        from which to return. ``monitor`` and ``chat listen`` run until a window closes or
        a user interrupts them. Thus their answer cannot be a value that the command
        returns at the end. Two commands use this seam. Other commands must not use it. A
        command that can build a whole report must build one, because each format already
        knows how to read a report.

        Args:
            listing: The shape of the records that will come: its columns, its lane order,
                and (for the plain face) the width at which each lane is pinned. The
                function ignores its ``rows``. The rows arrive through the callable that
                the function yields.

        Yields:
            A callable that takes one typed row mapping.
        """
        raise NotImplementedError


class PlainRenderer(Renderer):
    """The face that a person reads: aligned text on the scripted console.

    Attributes:
        console: The plain console (refer to :func:`meshterm.ui.script.console`).
    """

    def __init__(self, console: Console) -> None:
        """Bind the renderer to the console to which its output goes."""
        self.console = console

    def render(self, report: Report | None) -> None:
        """Draw each block in order, with one blank line between the blocks.

        The code counts the separators from the blocks that it drew, and not from the block
        index. A block can render to nothing (a listing with no rows, or an acknowledgement
        whose plain answer is its exit status). A blank line above the first visible block
        would be a line of output that the command did not have.
        """
        drawn_any = False
        for block in report or ():
            drawn = self._block(block)
            if drawn is None:
                continue
            if drawn_any:
                self.console.print(script.blank())
            self.console.print(drawn)
            drawn_any = True

    def _block(self, block: Block) -> Any:
        """The renderable for one block, or ``None`` when the block prints nothing."""
        if isinstance(block, Listing):
            return self._listing(block) if block.rows else None
        if isinstance(block, Facts):
            return self._facts(block)
        raise TypeError(f"unrenderable block: {block!r}")  # pragma: no cover - closed set

    def _listing(self, block: Listing) -> Any:
        """A listing as its header line and its padded records."""
        lanes = block.lanes()
        table = script.columns(
            *(lane.header for _, lane in lanes),
            right=[lane.header for _, lane in lanes if lane.align == "right"],
        )
        # `config show` is an array to a parser and a `sysctl -a` block to a user. Thus it
        # keeps the columns and removes the heading row. The table is the same in both
        # cases. The padding, the no-wrap, and the crop make a record splittable, and the
        # header does not make any of them.
        table.show_header = block.headed
        for row in block.rows:
            table.add_row(*(lane.render(row.get(column.key)) for column, lane in lanes))
        return table

    def _facts(self, block: Facts) -> Any:
        """A facts block as a key and value listing, one bare value, or nothing."""
        if block.shape == SILENT:
            return None
        if block.shape == BARE:
            column = next(c for c in block.fields if c.key == block.bare)
            value = block.values.get(column.key)
            if value is None:
                # The bare form prints the value, and there is no value: a node that never
                # answered, or a slot that is empty. A lone `-` there would be a line of
                # output where the exit status is the whole report.
                return None
            # Use a Text and not a markup string. The value can be a private key, a share
            # URL, or the own reply of a remote node. Rich would read a bracket in these
            # values as a style tag.
            return Text(column.lanes[0].render(value))
        return script.pairs(facts_pairs(block))

    @contextmanager
    def stream(self, listing: Listing) -> Iterator[Any]:
        """Print the header line, then one record with pinned lanes for each row that arrives."""
        lanes = listing.lanes()
        pinned = script.stream(
            *((lane.header, _lane_width(lane)) for _, lane in lanes),
            right=[lane.header for _, lane in lanes if lane.align == "right"],
        )
        self.console.print(pinned.header, highlight=False)

        def row(record: Mapping[str, Any]) -> None:
            cells = [lane.render(record.get(column.key)) for column, lane in lanes]
            self.console.print(pinned.record(*cells), highlight=False)

        yield row


class JsonRenderer(Renderer):
    """The face that a program reads: one compact document, or one for each streamed record.

    Attributes:
        console: The plain console. The renderer uses only its stream. It writes the
            document through :func:`json.dumps` and not through Rich, so nothing can pad,
            wrap, highlight, or decorate the document.
    """

    def __init__(self, console: Console) -> None:
        """Bind the renderer to the console to which its output goes."""
        self.console = console

    def render(self, report: Report | None) -> None:
        """Print the report as one document, or nothing when there is no report."""
        if report is None:
            return
        blocks = [block for block in report if not block.plain_only]
        if not blocks:
            return
        self._write(self._document(blocks))

    def _document(self, blocks: Sequence[Block]) -> Any:
        """Fold the blocks into the one value with which this command answers."""
        if len(blocks) == 1:
            return self._value(blocks[0])
        document: dict[str, Any] = {}
        for block in blocks:
            if isinstance(block, Facts):
                document.update(self._facts(block))
            else:
                document[block.key] = self._value(block)
        return document

    def _value(self, block: Block) -> Any:
        """The document of one block: an array for a listing, an object for a set of facts."""
        if isinstance(block, Listing):
            return [self._row(block, row) for row in block.rows]
        if isinstance(block, Facts):
            return self._facts(block)
        raise TypeError(f"unrenderable block: {block!r}")  # pragma: no cover - closed set

    @staticmethod
    def _row(block: Listing, row: Mapping[str, Any]) -> dict[str, Any]:
        """One record, with each declared key present (``null`` where the value is absent)."""
        return {
            column.key: column.json(row.get(column.key))
            for column in block.columns
            if column.json is not None
        }

    @staticmethod
    def _facts(block: Facts) -> dict[str, Any]:
        """One facts block as an object, with each declared key kept.

        ``omit_absent`` is a concession for the plain face only. ``info`` leaves out a
        reading that the firmware does not answer for, so that a dash never says that the
        radio reported nothing. A consumer cannot act on that difference, and it must test
        the membership of each field because of it. Thus the document keeps the key and
        writes ``null``.
        """
        return {
            column.key: column.json(block.values.get(column.key))
            for column in block.fields
            if column.json is not None
        }

    def _write(self, document: Any) -> None:
        """Write one compact document and end it with a newline."""
        line = json.dumps(
            document,
            # A node name in Cyrillic is a node name, and not a run of `\uXXXX`. The
            # scripted console already sets stdout to UTF-8 for this class of character.
            ensure_ascii=False,
            # Compact and on one line, so that a program reads a stream and a one-shot
            # answer in the same way, and `jq .` can indent it for a person at no cost.
            # Nothing can change a document that is pretty-printed across four hundred
            # lines into a stream.
            separators=(",", ":"),
        )
        self.console.file.write(line + "\n")
        self.console.file.flush()

    @contextmanager
    def stream(self, listing: Listing) -> Iterator[Any]:
        """Print one document for each record, flushed, as each record arrives.

        Each line has the same shape. Thus a consumer can read the stream without a
        discriminator: it reads line by line, and it never must ask which type of record it
        holds. For this reason the closing summary for each node, which the plain face draws,
        is marked ``plain_only``. A consumer can derive it from the records above it. A
        second shape in the middle of the stream would cost each consumer a branch that it
        does not need in other cases.
        """

        def row(record: Mapping[str, Any]) -> None:
            self._write(self._row(listing, record))

        yield row


def for_format(output: OutputFormat, console: Console) -> Renderer:
    """The renderer for one output format.

    Args:
        output: The format that the caller asked for.
        console: The console on which to print.

    Returns:
        The renderer.
    """
    return JsonRenderer(console) if output is OutputFormat.JSON else PlainRenderer(console)


@contextmanager
def stream(ctx: AppContext, listing: Listing) -> Iterator[Any]:
    """Open a live listing on the renderer that this run uses.

    Args:
        ctx: The application context, for its output format and console.
        listing: The shape of the stream (refer to :meth:`Renderer.stream`).

    Yields:
        A callable that takes one typed row mapping.
    """
    with for_format(ctx.output, ctx.console).stream(listing) as row:
        yield row


def _lane_width(lane: Lane) -> int:
    """The width at which a streamed lane is pinned.

    A live capture cannot size its columns as :func:`~meshterm.ui.script.columns` does,
    because it has not seen the records yet. Thus each lane declares the width at which it
    is pinned. A value that is wider than this width overruns, and it pushes the rest of
    its row to the right. MeshTerm does not cut it. For this reason the one field that has
    no limit goes last (refer to :class:`meshterm.ui.script.Lanes`).
    """
    return max(len(lane.header), lane.width)
