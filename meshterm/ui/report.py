# SPDX-License-Identifier: Apache-2.0
"""What a tool answers with, stated as data before anything decides how it looks.

A scripted run once said its answer when it *printed* it. ``Tool.run`` called
``ctx.ui.show(<a table built by ui/script.py>)`` and returned a :class:`ToolResult` that
had only bookkeeping. When the CLI boundary got that result, the contacts had already
gone down the pipe as a table that spaces aligned. Thus no data was left to give to a
second format. This is the reason that ``--json`` reached two commands out of twenty and
stopped. Each command that wanted it got an ``if ctx.json_output:`` branch *above* the
rendering. That branch stated the whole answer again in a second dialect, which no other
command could reuse.

``exit_code`` already had the shape that works. The tool states it, the boundary changes
it to a process status, and no tool has ever called :func:`sys.exit`. This module lets
the answer travel the same way:

    **A tool states its answer as data. A renderer turns data into bytes. The CLI
    boundary picks the renderer** (refer to :mod:`meshterm.ui.renderers`).

A report is a short sequence of blocks. There are only two types of block, because the
plain face needed only two: a :class:`Listing` (records under a header line) and
:class:`Facts` (one thing, described key by key). A :class:`Facts` block also covers the
answer of one scalar, through :data:`BARE`. ``config get`` prints its value alone
*because the caller named the key*, but the machine face still gets the object with the
key, the type, and the label in it. A third type of block for this answer would have
given a second name to the same data.

**A row holds the typed value.** Examples are ``6.0``, ``None``, and a
:class:`~datetime.datetime`. A row never holds a formatted cell. Each :class:`Column`
has both projections instead: the plain :class:`Lane` that changes the value to text, and
the JSON function that normalises it. A row is typed one time and has two projections.
If a third format is necessary later, it is one more function, not one more builder of
rows. :mod:`meshterm.ui.fields` builds the columns for the concepts that the
whole app shares (a node, a time, an SNR, a route). Thus each listing has the same shapes
because of the construction, not because of a review.

Nothing here renders. A block does not know what a gutter is, and a column does not know
if anyone will ask it for JSON.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

#: How a :class:`Facts` block reaches the *plain* face. The machine face is the same
#: object in each case. This only chooses what a person at a prompt sees.
PAIRS = "pairs"
#: The value of one named field, alone, with no key before it. This is what ``config get``
#: prints. The caller named the key, so a print of the key again is one more thing that
#: the caller must remove.
BARE = "bare"
#: Nothing at all. This is for an acknowledgement (``config advert``, ``channels join``)
#: whose plain report is its exit status. The machine face still gets a document, because
#: "prints nothing" is an answer that a person can use and a program cannot.
SILENT = "silent"


def _text(value: Any) -> str:
    """The plain projection of last resort: the value as text, or the absent token."""
    from . import script

    return script.NONE if value is None else str(value)


def normalise(value: Any) -> Any:
    """A typed value in its machine form: ready for JSON, and the same on each host.

    This is the only place where the value rules of the contract that apply to all
    commands are applied. Thus a command cannot forget one:

    * A **time** is UTC, RFC 3339, with the ``Z`` suffix, to the second. It always has
      twenty characters, so a comparison of strings is a comparison of times. A local
      offset is a fact about the machine that ran the command, not about the event. Two
      hosts that ask the same radio the same question must produce the same document. A
      *naive* time is a time with an offset that we do not know. This is not a fact, so
      it is absent, the same as on the plain face.
    * An **enum** is its value, not its Python name.
    * A **path** is its text.
    * Everything else travels as it is, typed: ``9`` and not ``"9"``, ``false`` and not
      ``"false"``, ``None`` and not the ``-`` of the plain face.

    Args:
        value: The typed value from a report row.

    Returns:
        The value in a form that :mod:`json` can write.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, Enum):
        return normalise(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [normalise(item) for item in value]
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class Lane:
    """One plain-text column, drawn from the typed value of its :class:`Column`.

    A lane is the half of a column that a person reads. Several lanes can share one column
    when the plain face uses more room for a concept than the machine face does. A node is
    one JSON object and up to four plain columns (its name, its role, its hash, and its
    key). This is the reason that a column owns a list of lanes and not one header string.

    Attributes:
        header: The heading in upper case, and the field that a caller searches for with
            grep. If a header is renamed, a script breaks with no error. Thus a header is
            a contract, the same as the exit status.
        render: Changes the typed value of the column to the cell of this lane.
        align: ``"right"`` for a lane of magnitudes. The eye can compare a column of
            numbers or ages only when the digits line up.
        width: The number of cells to which this lane is pinned in a *live* listing. In
            a live listing, MeshTerm cannot measure the widths because the records have
            not arrived. An ordinary listing ignores this value and sizes each lane to
            its widest value.
    """

    header: str
    render: Callable[[Any], str] = _text
    align: str = "left"
    width: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class Column:
    """One concept in a report, with a projection for each face.

    Attributes:
        key: The name of this concept: the JSON key, and the key at which the mapping of a
            row holds its typed value.
        lanes: The plain columns that this concept draws, usually one. **Empty means that
            the concept has no plain face.** This is how ``config show`` puts the type
            and the enum label of a setting in the document, but prints only the two
            fields that a person wants.
        json: The machine projection of the typed value. It is ``None`` when the concept
            has no machine face. This is the opposite of an empty ``lanes``. For example,
            the DESCRIPTION of a preference is prose for the person who chooses a value.
            It has nothing that a program uses to make a decision. The default is
            :func:`normalise`, which handles the scalars. A shared shape (a node, a
            position) passes its own function.
    """

    key: str
    lanes: tuple[Lane, ...] = ()
    json: Callable[[Any], Any] | None = normalise


@dataclass(frozen=True, slots=True, kw_only=True)
class Block:
    """One part of an answer.

    Attributes:
        key: The name this block goes under in a multi-block document.
        caption: The heading that a format with headings draws above this block. The plain
            face has no heading, because a heading over a block of keys and values is
            only decoration on a face that is based on lines. This is also the reason
            that ``config show`` removes even its column headers. The machine face has
            ``key`` instead. The markdown projection makes the caption a ``##`` section.
            This gives a written page the landmarks that its section jumps and its sticky
            headings step by. Empty means that the block is drawn without a heading.
        plain_only: Whether this block is a rendering for a person that the machine face
            does not use. The only case is the closing summary of a stream. ``monitor``
            prints an aggregate for each node under its live capture. A consumer that has
            each record of the capture can compute the same aggregate. A second *shape*
            of line in the middle of an NDJSON stream with one shape forces each
            consumer to have a discriminator that it does not otherwise need.
    """

    key: str
    caption: str = ""
    plain_only: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class Listing(Block):
    """Records under a header line: a listing on the plain face, an array of objects in JSON.

    Attributes:
        columns: The concepts that each record has, in the order of the machine face.
        rows: One mapping for each record. Its keys are ``Column.key`` and it holds
            **typed** values.
        order: The lane headers in the draw order of the plain face. Empty means "as
            declared", which is what most listings want. A listing states an order when
            the two orders are really different. ``contacts`` puts HEARD and PKTS between
            the TYPE and the HASH of a node. ``monitor`` puts the one field with no limit
            (the name of a node) last, so that it can never push a column.
        headed: Whether the plain face draws the header line. ``config show`` is the only
            listing that does not. To a parser it is an array of settings, and to a person
            it is a ``sysctl -a`` block. A header line over two columns, when their keys
            are the answer, would be only decoration.
    """

    columns: tuple[Column, ...]
    rows: Sequence[Mapping[str, Any]] = ()
    order: tuple[str, ...] = ()
    headed: bool = True

    def __post_init__(self) -> None:
        """Reject an ``order`` that does not name the lanes of this listing.

        A typo here draws a listing with a column missing and gives no error. A header
        line cannot show this failure. It costs less to raise an error at construction.
        """
        if not self.order:
            return
        known = {lane.header for column in self.columns for lane in column.lanes}
        if set(self.order) != known or len(self.order) != len(known):
            raise ValueError(f"{self.key}: order must name every lane exactly once")

    def lanes(self) -> list[tuple[Column, Lane]]:
        """The plain columns in draw order, each with the column that it reads from."""
        pairs = [(column, lane) for column in self.columns for lane in column.lanes]
        if not self.order:
            return pairs
        by_header = {lane.header: (column, lane) for column, lane in pairs}
        return [by_header[header] for header in self.order]


@dataclass(frozen=True, slots=True, kw_only=True)
class Facts(Block):
    """One thing, described key by key: a ``sysctl -a`` block on the plain face, an object in JSON.

    Attributes:
        fields: The concepts, in the order in which both faces show them.
        values: The typed value for each ``Column.key``.
        shape: :data:`PAIRS`, :data:`BARE`, or :data:`SILENT`. This is what the *plain*
            face prints. The machine face is the object in each case.
        bare: With :data:`BARE`, the one field whose value is printed alone.
        omit_absent: Whether the plain block leaves out a field that has no value. Only
            ``info`` needs this, and it is deliberate. Its readings are optional device
            queries, so a missing key says "this radio does not report it". A dash would
            say that the radio reported nothing. The machine face keeps each declared key
            and writes ``null``. To a caller, "does not report" and "reported nothing" are
            the same fact. A set of keys that changes would force each consumer to test
            if a key is present, for each field.
    """

    fields: tuple[Column, ...]
    values: Mapping[str, Any] = field(default_factory=dict)
    shape: str = PAIRS
    bare: str = ""
    omit_absent: bool = False


#: The whole answer of a tool. It is a plain sequence and not a class, on purpose. A
#: report does nothing. The two rules for the way in which several blocks combine belong
#: to the renderer that combines them (refer to :mod:`meshterm.ui.renderers`).
Report = tuple[Block, ...]
