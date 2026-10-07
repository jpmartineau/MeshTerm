# SPDX-License-Identifier: Apache-2.0
"""The output language of the plain CLI: aligned text without decoration, for a prompt.

MeshTerm has two front ends and a third face. A person who sits in front of the menu
reads the menu. All the other code in ``meshterm/ui/`` is made for that user: colour that
has a meaning, glyphs for concepts, and frames that make groups. A program reads
``--json``, often on a different machine and often later. This module is the third face:
**a person at a prompt who typed a command to find a fact, and who wants to read the
answer in one look.**

This is a change of premise. Before, the plain face was a serialization format that a
person could read only with effort. Several of its rules were there only to make it safe
to *split* the output into fields: quoted names, absolute timestamps, and a comma between
the hops of a path. Now a program that splits the output reads the machine face instead
(refer to :mod:`meshterm.ui.report` and :mod:`meshterm.ui.renderers`). Thus those rules
had a cost and no longer had a purpose. The rules that stay are the rules that make the
output the output of a Unix utility, and one new rule that replaces the quotes.

* **Alignment is the delimiter.** The eye already sees an aligned column as one field.
  Thus a name has no quotes (:func:`name`). The *escapes* under the quotes stay, and they
  must always stay (:func:`_escaped`). A node broadcasts its own name, and a stranger
  writes the body of a message. Neither value can end the record that contains it.
* **No colour.** ``stdout`` is more often a pipe than a terminal. A colour that goes into
  a file is noise in that file. The console that this module builds has
  ``color_system=None``, thus Rich writes no escape sequence at all: no dim and no bold.
  This is stricter than ``no_color``, which keeps the attributes and removes only the
  colours.
* **No wrapping.** A record is a line. If the console wraps a record, the record becomes
  two lines, and the fields of the second line are under the wrong headings. Thus the
  console is :data:`WIDTH` cells wide, much wider than any real line, and each column is
  ``no_wrap``. The only intentional exception is a *written page* (``about``,
  ``support``). It is drawn on a console that is :data:`PAGE_WIDTH` cells wide instead.
  A paragraph is not a record, and it has no headings to align under. If it does not
  wrap, it is one line of 600 cells, and a person cannot read that line in any terminal.
* **No frames.** No panel borders, no table boxes, no rules, and no titles. The command
  that the person typed is the title. A box around the answer is a second title.
* **Aligned columns, one header line.** The shape of ``ps`` and ``df``: a header row in
  upper case, then the records. :data:`GUTTER` spaces separate the padded fields. Refer to
  :func:`columns`.
* **A time is an age.** ``5m``, ``3h``, ``never``: the same scale that the columns of the
  menu use (:func:`age`). The reason is that "how long ago" is the question that a person
  asks. An absolute instant stays where the instant is the fact: the device clock, a
  scheduled appointment, and the clock of a live capture (:func:`stamp`). ``--absolute``
  changes all the output back to absolute times for a person who wants them. The machine
  face always uses absolute UTC times.
* **A route is drawn, a path is typed.** The lexicon already makes these two concepts
  different. A *path* is an ordered list of hops that you write or force. A *route* is
  the sequence of nodes that a walk went through. Thus a path keeps its commas
  (:func:`spec`), because you can type it back into ``--path`` (a round trip). A route
  gets arrows (:func:`route`), because it is a picture and clearly not a spec. Before,
  both had commas, and thus both seemed to make a round trip. Only one of them does.
* **One token for "nothing".** :data:`NONE` (a ``-`` alone) in each column. Thus a person
  never has to tell an absent value from an empty value, an ``—``, an ``n/a``, or a blank.
  ``never`` is a *value*, not an absence: a node that was never heard is a fact.
* **No trailing whitespace.** If the last column is padded to its width, each line ends
  with invisible spaces. The console removes them when it writes the line
  (:class:`_Trimmed`).

The menu cannot get to this module, and this module cannot get to the menu.
:class:`~meshterm.ui.surface.PlainUi` is the seam. :func:`~meshterm.cli.main_callback`
chooses it when the command line names a subcommand.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

#: The width of the scripted console. It is wide enough that Rich never wraps or cuts any
#: text that MeshTerm can make (a key of 64 hex digits, a full route, the description of a
#: preference). If a line is longer than the terminal of the person, that terminal deals
#: with it. The value is not ``sys.maxsize``, because Rich sizes some structures from the
#: width. A very large number costs nothing, but an astronomically large number has a cost.
WIDTH = 1 << 14

#: The number of spaces between columns. It is two, so that a padded column never touches
#: the next column. Also, a person never reads a single space in a field without quotes as
#: a separator.
GUTTER = 2

#: The only token for a value that is absent, unknown, or not applicable. A person (and a
#: parser) learns it one time. It is never ``—``, ``n/a``, ``never``, or an empty cell.
NONE = "-"

#: The separator between the hops of a route, with a space on each side. It is the same
#: ``→`` that the path lines of the menu use (:data:`meshterm.ui.pathline._ARROW`). The
#: PicoCalc console font has this glyph (we checked it on the handheld). The arrow shows
#: the direction in which the packet went. Also, it makes the quotes unnecessary: a person
#: can no longer read a comma in the name of a node as a boundary between hops.
ARROW = " → "

#: The width at which a *written page* wraps. It is not the width of the terminal, because
#: the same paragraph must look the same in a pipe, in a redirected file, or in the
#: ``text`` field of the machine face. Also, 72 is already the width limit of each screen
#: in this app.
PAGE_WIDTH = 72


#: The characters that have a readable escape of their own. All other characters that
#: cannot go in a record get the ``\xNN``/``\uNNNN`` escape below.
_BREAKS = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}

#: The code points that must never go to stdout without an escape. This is a *category*,
#: not a list: each C0 and C1 control character, and the two Unicode separators at which a
#: terminal also breaks a line. Before, a table of four entries caught the newline and the
#: tab, but let ESC through. An ESC in a node name starts a live colour run in the file of
#: the caller. The rule "no colour, not a single escape sequence" is there to prevent
#: exactly this. A BEL was worse: it disappeared without a warning, so the printed name was
#: not the name in the advert.
_UNPRINTABLE = frozenset([*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0), 0x2028, 0x2029])


def _escaped(text: str) -> str:
    r"""``text``, with an escape for each character that cannot go in one record.

    A record is a line, and a field is a sequence of printable cells. This function makes
    both statements true for a value that came over the radio. It uses a named escape
    where one is readable (``\n``), and a numeric escape for all other characters. It
    doubles the backslash, so that the result has only one meaning when a person reads it
    back.

    This function is the part of the quoting that stayed when we removed the quotes. The
    quotes made a name *one field* for a program that splits the line, and now the
    alignment does that. But nothing else can stop a newline in a name: without the
    escape, the newline ends the record of the name too early.
    """
    out: list[str] = []
    for ch in text:
        if ch in _BREAKS:
            out.append(_BREAKS[ch])
        elif ord(ch) in _UNPRINTABLE:
            out.append(f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return "".join(out)


def name(value: str | None) -> str:
    r"""The name of a node as a field: without quotes and with escapes, or :data:`NONE`.

    The quotes are gone. They were there so that a name with a space or a comma stayed one
    field for the program that split the line. Now that program reads JSON, and in plain
    text the alignment of the columns already shows where a field ends. The escapes stay.
    A node broadcasts its own name, so each name here is remote data, and a newline in a
    name must still not end the record that contains it.

    An empty name is still :data:`NONE`, not an empty cell. ``""`` means *called
    nothing*, and the token means *never said*. These are different facts. The machine
    face also needs this difference (an absent name is ``null``, an empty name is
    ``""``), and a field that shows both in the same way cannot give the difference back.

    Args:
        value: The name, or ``None`` or empty when the node did not give a name.

    Returns:
        The escaped name, or :data:`NONE`.
    """
    return _escaped(value) if value else NONE


def text(value: str | None) -> str:
    r"""Free text as a field that can end a record without a break in the record.

    Use it for the field that a listing puts last, because that field is the rest of the
    line: the body of a message, or the description of a preference. "The rest of the
    line" and "any text at all" are not compatible. Before, a body that contained a
    newline ended its record too early. The rest of the text was indented under the other
    columns, and it looked like a second record with an empty ``TIME``. Also, a stranger
    writes the body, so such a message is a real possibility.

    Args:
        value: The free text. ``None`` gives :data:`NONE`.

    Returns:
        The text, with escapes for its line breaks and tabs.
    """
    return NONE if value is None else _escaped(value)


def stamp(when: datetime | None) -> str:
    """Format a timestamp as local ISO-8601 to the second, or as :data:`NONE`.

    Use it where the instant is the fact, and not a way to say how long ago something
    occurred: the clock of the device, an appointment set with ``--at``, the time of each
    row in a live capture (without it, each row shows ``now``), and each column under
    ``--absolute``.

    Stored times are UTC and have a timezone. A naive time has an offset that we do not
    know. Thus it is not a fact, and the function shows it as absent.
    :func:`~meshterm.ui.widgets.age_seconds` uses the same rule for ages.

    Args:
        when: The instant to format.

    Returns:
        For example ``2026-09-07T18:22:41-04:00``, or :data:`NONE`.
    """
    if when is None or when.tzinfo is None:
        return NONE
    return when.astimezone().isoformat(timespec="seconds")


def age(when: datetime | None, *, absent: str = NONE) -> str:
    """How long ago ``when`` was: ``now``, ``5m``, ``3h``, ``2d``, ``4w``.

    This is the default form of a time, because of the change of premise. A person at a
    prompt who reads ``2026-09-07T19:58:53-04:00`` must do arithmetic to answer
    "recently?", and that is the question for which the person typed the command. The
    function calls :func:`~meshterm.ui.widgets.format_age`, so that the two faces always
    agree.

    Args:
        when: The instant to show as an age. A naive datetime has no offset that we know,
            thus it shows as absent, the same as in :func:`stamp`.
        absent: The text for an unknown time. A heard age gives ``"never"``, because a
            node that was never heard is a fact, not an absent field. All other callers
            use the default token, which says that this row has no such time at all.
            :func:`name` makes the same difference between ``-`` and an empty name, for a
            different value.

    Returns:
        The age, or ``absent``.
    """
    from .widgets import age_seconds, format_age

    seconds = age_seconds(when)
    return absent if seconds is None else format_age(seconds)


def number(value: Any, spec: str = "") -> str:
    """Format a number, or :data:`NONE` when it is absent.

    Args:
        value: The number to format. ``None`` shows as absent.
        spec: An optional :func:`format` spec, for example ``"+.1f"``.

    Returns:
        The formatted number, or :data:`NONE`.
    """
    if value is None:
        return NONE
    return format(value, spec) if spec else str(value)


#: The units that :func:`duration` uses, the largest first.
_UNITS: tuple[tuple[int, str], ...] = ((86400, "d"), (3600, "h"), (60, "m"), (1, "s"))


def duration(seconds: float | None) -> str:
    """A span of seconds in a readable form: ``93784`` → ``1d 2h``, ``360`` → ``6m``.

    It has a maximum of two units, and the two units are always next to each other. Thus
    a person sees the magnitude immediately, and the precision is never more than a person
    uses to make a decision. Use it only as a *gloss* next to the raw number
    (``uptime_s  93784  (1d 2h)``), never instead of the number. The key tells what the
    number counts, and a caller that reads the key must still find a number under it.

    Args:
        seconds: The span. ``None`` shows as absent. The function removes the sign,
            because a caller that adds a gloss to a signed value gives the direction in its
            own words.

    Returns:
        The span in one or two units, or :data:`NONE`.
    """
    if seconds is None:
        return NONE
    whole = int(abs(seconds))
    amounts: list[tuple[int, str]] = []
    for size, unit in _UNITS:
        amounts.append((whole // size, unit))
        whole %= size
    lead = next((i for i, (amount, _) in enumerate(amounts) if amount), len(amounts) - 1)
    parts = [f"{amounts[lead][0]}{amounts[lead][1]}"]
    if lead + 1 < len(amounts) and amounts[lead + 1][0]:
        parts.append(f"{amounts[lead + 1][0]}{amounts[lead + 1][1]}")
    return " ".join(parts)


def location(lat: float | None, lon: float | None) -> str:
    """A shared position as one field: ``45.50190,-73.56740``, or :data:`NONE`.

    One column, not two. For a person, a pair of coordinates is one fact, and it goes
    unchanged into the search box of a map. Also, two columns of ``-`` for each contact
    that never shared a position are worse than one column.

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.

    Returns:
        The pair, to five decimal places, or :data:`NONE` when one of the two values is
        absent.
    """
    if lat is None or lon is None:
        return NONE
    return f"{lat:.5f},{lon:.5f}"


def route(hops: Iterable[tuple[str | None, str | None]]) -> str:
    """The route of a walk, drawn: ``Yagi-Repeater (a1) → Alice (d4) → Yagi (a1)``.

    A *route* is what a walk did. Thus it is a picture and not a spec, and for this reason
    it uses :data:`ARROW` and not the comma that ``--path`` accepts. When a route had
    commas, it looked as if a person could paste it, but a person cannot. The route
    contains the names, and nothing can read it back (no round trip).

    Each hop always shows its hash. The hash is the join key to the table of hops under a
    trace, and it is the token that ``--path`` accepts. It also makes two contacts with
    the same name different. If the hash is removed "where the name is unique", the
    grammar of one line depends on the content of a different line.

    Our node is a hop like all other hops, with a name and a hash. The menu draws it as
    ``★``, because the user never has to be told which node is theirs. But here, the
    person who reads the line often reads it from a file, and was not at the prompt when
    the command ran.

    Args:
        hops: ``(label, hash)`` pairs, in the order of propagation. A hop with both values
            shows as ``Name (hash)``. A hop with one value shows only that value, never an
            empty ``()``. The menu's :class:`~meshterm.ui.pathline.PathLine` also labels an
            unresolved hop in this way.

    Returns:
        The joined route, or :data:`NONE` when there are no hops.
    """
    parts: list[str] = []
    for label, value in hops:
        if label and value:
            parts.append(f"{_escaped(label)} ({value})")
        else:
            parts.append(_escaped(label) if label else (value or NONE))
    return ARROW.join(parts) if parts else NONE


def spec(hops: Iterable[str]) -> str:
    """A forced path in the form that the device got it: ``a1,d4,a1``.

    This is the other half of the split that :func:`route` names. It is the *spec*: hashes
    with commas between them, exactly what ``--path`` accepts back. Thus it is the only
    line on the two faces that makes a round trip, and it must never get a name, an
    arrow, or a space.

    Args:
        hops: The hashes of the hops, in the order of propagation.

    Returns:
        The spec, joined with commas, or :data:`NONE` when there are no hops.
    """
    parts = [hop for hop in hops if hop]
    return ",".join(parts) if parts else NONE


def columns(*headers: str, right: Sequence[str] = ()) -> Table:
    """Build the scripted table: a header line in upper case, then padded records.

    The shape that ``ps`` and ``df`` print: no box, no title, and no padding at the edges.
    Each column has the width of its widest value, and :data:`GUTTER` spaces separate the
    columns. Nothing is wrapped, and nothing is cut with an ellipsis. Numeric lanes align
    to the right. Thus the eye can compare the magnitudes in a column, and the result of a
    split of the column does not change. An *age* lane aligns in the same way, so that the
    scale from ``now`` to ``4w`` reads down the column.

    Args:
        *headers: The column headings, already in upper case.
        right: The headings (from ``headers``) whose values align to the right.

    Returns:
        A Rich :class:`Table` that is ready for ``ctx.ui.show``.
    """
    table = Table(
        box=None,
        show_edge=False,
        pad_edge=False,
        expand=False,
        # (top, right, bottom, left): all of the gutter is on the right of each cell. Thus
        # no line starts with an indent, and the last column ends where its value ends.
        padding=(0, GUTTER, 0, 0),
    )
    wanted = set(right)
    for header in headers:
        table.add_column(
            header,
            justify="right" if header in wanted else "left",
            no_wrap=True,
            # "crop", not "ignore". Neither one cuts text with an ellipsis: neither one
            # ever writes the "…" that makes a key or a route unusable to the caller. But
            # Rich's ``Text.wrap`` returns early on "ignore" (text.py: ``if overflow ==
            # "ignore": lines.append(line); continue``), and that early return is *above*
            # the justify step. Thus "ignore" discards ``justify="right"`` without a
            # warning. You can think that the two settings are different only past the
            # 16384 cells of the console. But there, "crop" cuts and "ignore" also cuts.
            # They are different on each line before that point, where only "crop"
            # aligns.
            overflow="crop",
        )
    return table


def pairs(rows: Iterable[tuple[str, str]]) -> Table:
    """Build the scripted key/value listing: the key, then the value, aligned, no header.

    A set of facts about one thing prints in this form (``meshterm info``, ``config
    show``, ``trace``), the shape of ``sysctl -a``. The keys are the keys that the related
    ``get``/``set`` subcommand accepts. Thus a person can type a line from ``show`` back
    in.

    Args:
        rows: ``(key, value)`` pairs, in the order of display.

    Returns:
        A Rich :class:`Table` with no header line.
    """
    table = Table(
        box=None,
        show_edge=False,
        show_header=False,
        pad_edge=False,
        expand=False,
        padding=(0, GUTTER, 0, 0),
    )
    table.add_column(no_wrap=True, overflow="crop")
    table.add_column(no_wrap=True, overflow="crop")
    for key, value in rows:
        table.add_row(key, value)
    return table


@dataclass(frozen=True, slots=True)
class Lanes:
    """A column layout with fixed widths, for output whose widths come with the data.

    :func:`columns` sizes each lane to its widest value. It can do this only after it has
    all the records. A live capture never has all the records: ``monitor`` prints a row
    when a packet arrives, and the next row can be two times as wide. Before, those two
    streams joined their fields with the gutter and had no alignment at all. For this
    reason, a name in one of them still had its quotes: nothing else showed where the
    field ended.

    Lanes that are fixed at the start correct this problem fully. A value that is wider
    than its lane **goes past its lane and pushes the rest of that row to the right**. It
    is not cut with an ellipsis. One row of many is wider, and nothing gives false
    information about what the row contains. Put the one field that has no width limit
    (the name of a node, the body of a message) last. Then it pushes nothing at all.

    Attributes:
        widths: ``(header, width)`` for each lane, in order.
        right: The headers whose cells align to the right.
    """

    widths: tuple[tuple[str, int], ...]
    right: frozenset[str] = frozenset()

    @property
    def header(self) -> str:
        """The header line, printed before the first record."""
        return self.record(*(header for header, _ in self.widths))

    def record(self, *cells: str) -> str:
        """One record, padded into the lanes and joined with the gutter."""
        out: list[str] = []
        for (header, width), cell in zip(self.widths, cells, strict=True):
            out.append(cell.rjust(width) if header in self.right else cell.ljust(width))
        return (" " * GUTTER).join(out).rstrip()


def stream(*widths: tuple[str, int], right: Sequence[str] = ()) -> Lanes:
    """Build a layout with fixed lanes for a live stream (refer to :class:`Lanes`).

    Args:
        *widths: ``(header, width)`` for each lane, in order.
        right: The headings whose values align to the right.

    Returns:
        The layout, which makes its own header line and formats each record.
    """
    return Lanes(widths=widths, right=frozenset(right))


# -- the console ---------------------------------------------------------------------


class _Trimmed:
    """A text stream that removes the trailing whitespace from each line that goes through.

    Rich pads the cells of a table to the width of their column, also the cells of the
    last column. Without this stream, each record ends with invisible spaces. The stream
    is the only place that catches all of them (table padding, a justified :class:`Text`,
    a blank line that Rich padded), and no renderable has to know about it.

    The stream holds back only the *trailing run of whitespace*, never a full line. Rich
    writes a rendered row one segment at a time, and on Windows it flushes after each
    segment. Thus a wrapper that waits for the newline before it decides must decide
    dozens of times in each row. And a wrapper that trims at each flush removes the
    padding that separates two columns.

    All the text up to the last character that is not a space goes through immediately.
    The spaces after it wait until the stream knows if a newline or another column
    follows. At the end of the stream, nothing follows, and the held spaces are truly
    trailing whitespace. Thus the stream removes them.
    """

    def __init__(self, stream: Any) -> None:
        """Wrap ``stream``, and hold back the trailing whitespace that arrives."""
        self._stream = stream
        self._held = ""

    def write(self, text: str) -> int:
        """Write ``text``, but write whitespace only after something follows it on the line."""
        pending = self._held + text
        if "\n" in pending:
            *lines, pending = pending.split("\n")
            self._stream.write("".join(line.rstrip() + "\n" for line in lines))
        body = pending.rstrip()
        if body:
            self._stream.write(body)
        self._held = pending[len(body) :]
        return len(text)

    def flush(self) -> None:
        """Flush the wrapped stream, but keep the held whitespace.

        This method intentionally writes nothing. A flush comes between two columns as
        often as at the end of a line, and it does not tell which of the two it is.
        """
        self._stream.flush()

    def __getattr__(self, name: str) -> Any:
        """Delegate all other attributes (``isatty``, ``encoding``, ...) to the real stream."""
        return getattr(self._stream, name)


def console(stream: Any = None) -> Console:
    """Build the scripted console: no colour, no wrapping, no trailing whitespace.

    Args:
        stream: The file to write to. The default is ``sys.stdout``.

    Returns:
        A :class:`~rich.console.Console` that writes plain text and nothing else.
    """
    import sys

    target = sys.stdout if stream is None else stream
    reconfigure = getattr(target, "reconfigure", None)
    if reconfigure is not None:
        try:  # a name or mark that is not in cp1252 must not raise on an old Windows console
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):  # pragma: no cover - the stream cannot be reconfigured
            pass
    return Console(
        file=_Trimmed(target),
        # Not `no_color`, which keeps bold, dim, reverse, and the other attributes, and
        # removes only the colours. `color_system=None` is the only setting with which
        # Rich writes no escape sequence at all.
        color_system=None,
        width=WIDTH,
        # Without this, Rich finds a real terminal and soft-wraps to its width.
        soft_wrap=True,
        highlight=False,
        emoji=False,
        # A node broadcasts its own name, so each name on this console is remote data.
        # Rich reads `[...]` as a style tag. Before, `[bold]Loud` printed as `Loud`: the
        # name changed without a warning, and it was no longer the string that identifies
        # the node. Also, `[/]Bob` raised MarkupError, and the full command stopped. The
        # scripted CLI never prints markup, so here the parser can only cause errors.
        markup=False,
    )


def stderr_console() -> Console:
    """The console for all the output that is *about* the run, and not part of its answer.

    stdout contains only the answer to the command. Thus a progress bar, a log line, an
    acknowledgement, and an error message all go to this console instead. They are
    visible in a terminal, but they are not in ``meshterm contacts > contacts.txt``. This
    console keeps its colour, because usually only a terminal reads it.

    **When it does not write to a terminal, it does not wrap.** That is the purpose of the
    width below. When Rich cannot measure the destination, it uses 80 cells. Thus
    ``2> errors.log`` wrapped each message at 80 cells, and then
    ``grep 'not already connected'`` found nothing, because the logger broke the sentence
    across three lines. In a real terminal, the console does not change the width: a
    progress bar that is 16384 cells wide is useless as a progress bar.

    Returns:
        A :class:`~rich.console.Console` with the theme, that writes to ``sys.stderr``.
    """
    import sys

    from .theme import active_theme

    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if reconfigure is not None:
        try:  # the ✓ of an acknowledgement must not depend on the code page of the machine
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):  # pragma: no cover - the stream cannot be reconfigured
            pass
    try:
        wraps = bool(sys.stderr.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - a closed or unusual stream
        wraps = False
    return Console(
        file=sys.stderr,
        theme=active_theme(),
        markup=False,
        emoji=False,
        width=None if wraps else WIDTH,
    )


# -- the fallback --------------------------------------------------------------------


def flatten(renderable: RenderableType) -> list[RenderableType]:
    """Remove the frames from a renderable that comes to the scripted console with them.

    Each CLI surface is *written* plain: a tool gives its answer as a
    :mod:`~meshterm.ui.report`, and the plain renderer builds it through :func:`columns`
    and :func:`pairs`. This function is the fallback under that rule. It is for a
    renderable that the menu also uses and that still comes in a box. A :class:`Panel`
    loses its border and title, and the function returns its body. A :class:`Table`
    loses its box, its title, and its expansion. The function flattens a :class:`Group`
    one member at a time.

    It is intentionally not a *design*. The table of a screen has the columns of the
    menu, not the columns of the CLI. Thus this function makes such a table printable,
    not correct.

    Args:
        renderable: The renderable that a tool gave to
            :meth:`~meshterm.ui.surface.PlainUi.show`.

    Returns:
        The renderables without frames, to print in order.
    """
    if isinstance(renderable, Panel):
        return flatten(renderable.renderable)
    if isinstance(renderable, Table):
        renderable.title = None
        renderable.caption = None
        renderable.box = None
        renderable.show_edge = False
        renderable.pad_edge = False
        renderable.expand = False
        renderable.padding = (0, GUTTER, 0, 0)
        for column in renderable.columns:
            column.no_wrap = True
            column.overflow = "crop"
            column.ratio = None
        return [renderable]
    if isinstance(renderable, Group):
        out: list[RenderableType] = []
        for child in renderable.renderables:
            out.extend(flatten(child))
        return out
    return [renderable]


def blank() -> Text:
    """One empty line, to separate a listing from the block after it."""
    return Text("")
