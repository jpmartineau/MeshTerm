# SPDX-License-Identifier: Apache-2.0
"""The Clock drift page: how far the clock of each node is from ours.

The page reads the clock samples of the last ``clock_report_days`` (refer to
:mod:`~meshterm.services.clock_offsets`) and shows them in these sections:

* **All clocks** is on top (JP, 2026-11-03): a histogram of the offsets, in log steps on
  two sides. The clocks behind ours are at the top, the clocks in sync in the centre, and
  the clocks ahead at the bottom (JP, 2026-11-02). A clock can be wrong by seconds or by
  years, so one linear axis cannot show the two. The centre row is as wide as the
  ``clock_tolerance_s`` preference. Each bar is a
  :func:`~meshterm.ui.braillechart.meter`, the only bar of the app.
* **Managed nodes** (the nodes whose admin password MeshTerm stores) are the first list,
  each one always, also when its clock is correct, because these are the clocks that the
  user can set (JP, 2026-11-03). A managed node with no sample in the window shows its
  newest offset from all the history, or ``no sample``. A repeater sends no message, so
  its adverts are its only samples, and a new install has none yet.
* **Adverts refused**: the contacts whose adverts the device drops, because their clocks
  were once ahead (refer to
  :attr:`~meshterm.core.models.Contact.adverts_refused_until`), with the date until which
  it drops them. The node page of each one has the repair.
* **Out of sync**: each other node past the tolerance, the largest offset first. Enter
  opens the node page.
* **Senders with no key**: the channel senders whose name resolves to no single key. Their
  samples count in the histogram, and their names are page text in the grey of an unknown
  node. There is no node page to open for a name.

Each section has a header line under its heading, which pins with the heading while the
section scrolls. When most clocks are off in the same direction, the fault is probably
ours. A note at the top then says that the clock of this computer can be wrong.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text

from ..services.clock_offsets import ClockOffset
from .braillechart import meter
from .menus import fit_cells, section_heading
from .theme import name_style
from .tui import Choice, Separator
from .tui.select import SelectScreen
from .widgets import DEFAULT_GLYPH, NODE_GLYPHS, age_seconds, format_age, format_offset

if TYPE_CHECKING:
    from ..core.models import Contact

#: The edges of the histogram steps past the tolerance, in seconds: an hour, a day, a year.
#: An edge at or under the tolerance is dropped, so a large tolerance gives fewer steps.
_EDGES_S = (3600, 86400, 365 * 86400)

#: The cells of the label lane of the histogram (``1d-1y behind``).
_BUCKET_LABEL_W = 14

#: The cells of the offset lane of a row (``11h 59m behind``) and of its age lane (``52w``).
_OFFSET_W = 15
_AGE_W = 4

#: The cells of the pointer column of a row, and of its type mark with its gap.
_POINTER_W = 2
_MARK_W = 2

#: The widest that a name lane gets. The filter measures a row at an unbounded width.
_NAME_MAX_W = 24

#: The smallest number of keyed nodes from which the page judges our own clock.
_OWN_CLOCK_MIN_NODES = 5

#: The share of the keyed nodes that must be off in one direction to blame our clock.
_OWN_CLOCK_SHARE = 0.6

#: The footer hint. Enter opens the node page of the highlighted row.
_HINT = "↑↓ move · Enter open · type to filter · Esc back"


@dataclass(frozen=True, slots=True)
class ClockRow:
    """One sender on the Clock drift page.

    Attributes:
        offset: Its clock offset (refer to :class:`~meshterm.services.clock_offsets.ClockOffset`),
            or ``None`` for a managed node that never sent a sample.
        name: The name to show, or ``None`` when nothing names the node.
        key: The key (or key prefix) that gives the hue of the name, or ``None``.
        node_type: The advert type of the node, if known.
        managed: Whether MeshTerm stores the admin password of the node.
        contact: What Enter opens, or ``None`` for a sender with no key.
        current: Whether the offset comes from a sample in the window. Only these count in
            the histogram and in the title. A managed node with no recent sample shows an
            older offset, which is not current.
    """

    offset: ClockOffset | None
    name: str | None
    key: str | None
    node_type: int | None
    managed: bool
    contact: Contact | None
    current: bool = True

    @property
    def label(self) -> str:
        """The name to show, or the id that stands in for it."""
        if self.name:
            return self.name
        return self.offset.who if self.offset is not None else "unknown"

    def out_of_sync(self, tolerance_s: float) -> bool:
        """Whether the clock is past the tolerance. A node with no offset is not."""
        return self.offset is not None and self.offset.out_of_sync(tolerance_s)


@dataclass(frozen=True, slots=True)
class Bucket:
    """One step of the histogram.

    Attributes:
        label: The words of the step (``1h-1d behind``, ``in sync ≤5m``).
        low: The smallest offset in the step, in seconds (``None`` for no limit).
        high: The largest offset in the step, in seconds (``None`` for no limit).
        in_sync: Whether this is the centre step.
    """

    label: str
    low: float | None
    high: float | None
    in_sync: bool = False

    def holds(self, offset_s: float) -> bool:
        """Whether ``offset_s`` falls in this step. A step includes its edge farthest from zero."""
        if self.in_sync:
            assert self.low is not None and self.high is not None
            return self.low <= offset_s <= self.high
        if offset_s < 0:
            return (self.low is None or offset_s >= self.low) and (
                self.high is None or offset_s < self.high
            )
        return (self.low is None or offset_s > self.low) and (
            self.high is None or offset_s <= self.high
        )


def _span(seconds: float) -> str:
    """A step edge in the compact age form, with years: ``5m``, ``1h``, ``1d``, ``1y``."""
    return "1y" if seconds >= 365 * 86400 else format_age(seconds)


def buckets(tolerance_s: float) -> list[Bucket]:
    """The steps of the histogram, the clocks most behind first.

    Args:
        tolerance_s: The ``clock_tolerance_s`` preference: the half width of the centre.

    Returns:
        The steps, from the farthest behind, through "in sync", to the farthest ahead.
    """
    edges = [float(tolerance_s)] + [float(e) for e in _EDGES_S if e > tolerance_s]
    ahead: list[Bucket] = []
    for low, high in zip(edges, [*edges[1:], None], strict=True):
        # A plain hyphen, not an en dash: the console font of the PicoCalc has no en dash.
        words = f"{_span(low)}-{_span(high)}" if high is not None else f"> {_span(low)}"
        ahead.append(Bucket(f"{words} ahead", low, high))
    behind = [
        Bucket(
            b.label.replace("ahead", "behind"),
            -b.high if b.high is not None else None,
            -b.low if b.low is not None else None,
        )
        for b in reversed(ahead)
    ]
    # ``≤`` and not ``±``: the console font of the PicoCalc has no ``±``, and its fold to
    # ``?`` would read as the mark of a corrected time.
    centre = Bucket(f"in sync ≤{_span(tolerance_s)}", -tolerance_s, tolerance_s, in_sync=True)
    return [*behind, centre, *ahead]


def _current(rows: Sequence[ClockRow]) -> list[ClockRow]:
    """The rows whose offset comes from a sample in the window."""
    return [r for r in rows if r.current and r.offset is not None]


def histogram(rows: Sequence[ClockRow], tolerance_s: float) -> list[tuple[Bucket, int]]:
    """Count the senders with a current offset in each step of :func:`buckets`."""
    steps = buckets(tolerance_s)
    counts = [0] * len(steps)
    for row in _current(rows):
        assert row.offset is not None
        for i, step in enumerate(steps):
            if step.holds(row.offset.offset_s):
                counts[i] += 1
                break
    return list(zip(steps, counts, strict=True))


def own_clock_note(rows: Sequence[ClockRow], tolerance_s: float) -> str | None:
    """A note when most clocks are off in one direction, so that the fault is probably ours.

    Args:
        rows: All the senders on the page.
        tolerance_s: The ``clock_tolerance_s`` preference.

    Returns:
        The note, or ``None`` when the clocks do not agree against ours.
    """
    keyed = [r.offset.offset_s for r in _current(rows) if r.offset and r.offset.keyed]
    if len(keyed) < _OWN_CLOCK_MIN_NODES:
        return None
    median = statistics.median(keyed)
    if abs(median) <= tolerance_s:
        return None
    same_way = sum(1 for o in keyed if abs(o) > tolerance_s and (o > 0) == (median > 0))
    if same_way < _OWN_CLOCK_SHARE * len(keyed):
        return None
    direction = "ahead of" if median > 0 else "behind"
    size = format_offset(median).rsplit(" ", 1)[0]
    return f"⚠ most clocks are {size} {direction} this computer — its own clock may be wrong"


def _name_w(row_width: int) -> int:
    """The width of the name lane, for a row of ``row_width`` cells (after the pointer)."""
    return max(4, min(_NAME_MAX_W, row_width - _MARK_W - 2 - _OFFSET_W - 2 - _AGE_W))


def _node_header(width: int) -> Text:
    """The header line of a list of nodes: ``NAME``, ``OFFSET``, and ``LAST``.

    A separator gets the full width, and a row gets it less the pointer column. Thus the
    header pads the pointer and the type mark itself, and each label is above its lane.
    """
    text = Text(" " * (_POINTER_W + _MARK_W), style="muted", no_wrap=True, overflow="ellipsis")
    text.append(fit_cells("NAME", _name_w(width - _POINTER_W)))
    text.append("  " + fit_cells("OFFSET", _OFFSET_W, align="right"))
    text.append("  " + fit_cells("LAST", _AGE_W, align="right"))
    return text


def _histogram_header(peak: int) -> Text:
    """The header line of the histogram: the step, and ``#`` over the counts."""
    text = Text(fit_cells("OFFSET", _BUCKET_LABEL_W), style="muted", no_wrap=True)
    text.append(" " + f"{'#':>{len(str(peak))}}")
    return text


def _bar_row(step: Bucket, count: int, peak: int, width: int) -> Text:
    """One line of the histogram: the label, the count, and the bar.

    The count is between the label and the bar, in a lane as wide as the largest count.
    Thus each count is next to its label, also when its bar is short or absent.
    """
    count_w = len(str(peak))
    bar_w = max(1, width - _BUCKET_LABEL_W - 1 - count_w - 1)
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(fit_cells(step.label, _BUCKET_LABEL_W), style="muted")
    text.append(" ")
    text.append(f"{count:>{count_w}}", style="muted" if count else "faint")
    text.append(" ")
    style = "ok" if step.in_sync else "warn"
    text.append_text(meter(count / peak if count else None, bar_w, style=style))
    return text


def _offset_cells(row: ClockRow, tolerance_s: float, text: Text) -> None:
    """Append the offset lane and the age lane of a row to ``text``."""
    text.append("  ")
    if row.offset is None:
        text.append(fit_cells("no sample", _OFFSET_W, align="right"), style="muted")
        text.append("  " + fit_cells("—", _AGE_W, align="right"), style="faint")
        return
    if row.out_of_sync(tolerance_s):
        words = format_offset(row.offset.offset_s)
        text.append(fit_cells(words, _OFFSET_W, align="right"), style="warn")
    else:
        text.append(fit_cells("in sync", _OFFSET_W, align="right"))
    age = format_age(age_seconds(row.offset.last_at))
    text.append("  " + fit_cells(age, _AGE_W, align="right"), style="muted")


def _row_text(row: ClockRow, tolerance_s: float, width: int) -> Text:
    """One node: its type mark, its name in its hue, its offset, and the age of its sample."""
    glyph, glyph_style = NODE_GLYPHS.get(row.node_type, DEFAULT_GLYPH)
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{glyph} ", style=glyph_style)
    # A node that nothing names shows its id in the grey of an unknown node: a bare hash
    # that stands in for a name never takes a hue.
    hue = name_style(row.name, row.key) if row.name else "node.unknown"
    text.append(fit_cells(row.label, _name_w(width)), style=hue)
    _offset_cells(row, tolerance_s, text)
    return text


def _refused_header(width: int) -> Text:
    """The header line of the refused adverts: ``NAME`` and ``UNTIL``."""
    text = Text(" " * (_POINTER_W + _MARK_W), style="muted", no_wrap=True, overflow="ellipsis")
    text.append(fit_cells("NAME", _name_w(width - _POINTER_W)))
    text.append("  " + fit_cells("UNTIL", _OFFSET_W + 2 + _AGE_W, align="right"))
    return text


def _refused_text(row: ClockRow, width: int) -> Text:
    """One contact whose adverts the device refuses: its name, and the date in warn."""
    assert row.contact is not None and row.contact.adverts_refused_until is not None
    glyph, glyph_style = NODE_GLYPHS.get(row.node_type, DEFAULT_GLYPH)
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{glyph} ", style=glyph_style)
    hue = name_style(row.name, row.key) if row.name else "node.unknown"
    text.append(fit_cells(row.label, _name_w(width)), style=hue)
    until = f"{row.contact.adverts_refused_until.astimezone():%Y-%m-%d}"
    text.append("  " + fit_cells(until, _OFFSET_W + 2 + _AGE_W, align="right"), style="warn")
    return text


def _name_line(row: ClockRow, tolerance_s: float, width: int) -> Text:
    """One sender with no key: page text under the row column, in the unknown grey."""
    text = Text(" " * (_POINTER_W + _MARK_W), no_wrap=True, overflow="ellipsis")
    text.append(fit_cells(row.label, _name_w(width - _POINTER_W)), style="node.unknown")
    _offset_cells(row, tolerance_s, text)
    return text


def _by_size(rows: Sequence[ClockRow]) -> list[ClockRow]:
    """The largest offset first, then the nodes with no offset, by name."""
    return sorted(
        rows,
        key=lambda r: (r.offset is None, -abs(r.offset.offset_s) if r.offset else 0, r.label),
    )


def clock_drift_screen(rows: Sequence[ClockRow], *, tolerance_s: float) -> SelectScreen:
    """Build the Clock drift page from its rows.

    Args:
        rows: Each sender with a clock sample in the window, keyed or not, and each managed
            node.
        tolerance_s: The ``clock_tolerance_s`` preference.

    Returns:
        The screen. It resolves with the :class:`~meshterm.core.models.Contact` of the
        chosen row.
    """
    current = _current(rows)
    off = [r for r in current if r.out_of_sync(tolerance_s)]
    title = f"Clock drift · {len(current)} nodes · {len(off)} out of sync"
    items: list = []
    note = own_clock_note(rows, tolerance_s)
    if note is not None:
        items.append(Separator(note, style="warn"))

    def node_rows(group: Sequence[ClockRow]) -> list:
        return [Choice(lambda w, r=r: _row_text(r, tolerance_s, w), r.contact) for r in group]

    # The histogram is on top: the shape of all the clocks, before the lists of single
    # nodes (JP, 2026-11-03).
    counted = histogram(rows, tolerance_s)
    peak = max((count for _step, count in counted), default=0) or 1
    items.append(section_heading("All clocks"))
    items.append(Separator(lambda _w: _histogram_header(peak)))
    for step, count in counted:
        items.append(Separator(lambda w, s=step, c=count: _bar_row(s, c, peak, w)))

    managed = _by_size([r for r in rows if r.managed and r.contact is not None])
    if managed:
        items.append(section_heading("Managed nodes"))
        items.append(Separator(_node_header))
        items += node_rows(managed)

    # The contacts whose adverts the device refuses, because their clocks were once ahead
    # (refer to Contact.adverts_refused_until). Their node page has the repair.
    refused = sorted(
        (r for r in rows if r.contact is not None and r.contact.adverts_refused_until),
        key=lambda r: r.label.casefold(),
    )
    if refused:
        items.append(section_heading("Adverts refused"))
        items.append(Separator(_refused_header))
        items += [Choice(lambda w, r=r: _refused_text(r, w), r.contact) for r in refused]

    others = _by_size([r for r in off if not r.managed and r.contact is not None])
    items.append(section_heading("Out of sync"))
    if others:
        items.append(Separator(_node_header))
        items += node_rows(others)
    else:
        items.append(Separator("no other clock is out of sync", style="muted"))

    nameless = _by_size([r for r in off if r.offset is not None and not r.offset.keyed])
    if nameless:
        items.append(section_heading("Senders with no key"))
        items.append(Separator(_node_header))
        items += [Separator(lambda w, r=r: _name_line(r, tolerance_s, w)) for r in nameless]
    return SelectScreen(title, items, footer_hint=_HINT)
