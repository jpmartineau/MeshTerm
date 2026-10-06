# SPDX-License-Identifier: Apache-2.0
"""The width of a glyph: one rule in all places, and two cells where nobody can know.

The width of an emoji in cells is not standardized, and a program cannot find it. The font of a
terminal decides if the terminal draws an emoji in one cell or in two. The font decides for each
glyph, and no rule is behind the split. Thus the same terminal draws ``👋`` in one cell and
``📡`` in two,``☀️`` in one and ``🛩️`` in two. No escape
sequence reports the width of a renderer back to the program. A cursor probe does not help
either. On a split terminal (VS Code, Windows Terminal), the component that tracks the cursor
and the component that paints the glyph are different programs that do not agree with each
other, and a probe can only ask the first.

This module once answered that problem with a probe and two exception sets, curated by hand:
glyphs that somebody confirmed, one at a time, that the terminal drew narrower or wider than the
tables said. That lined up each row that somebody had already examined, and none of the rows that
nobody had examined. But the name of a contact comes over the radio, written by a stranger, and
it has whatever the stranger typed. For that reason we retired the sets, and also the probe that
decided when to apply them.

**Two halves replaced them. Neither half has to know what the font does.**

* **Placement** belongs to :mod:`~meshterm.ui.tui.colsnap`. Each glyph whose drawn width is not
  certain is written with its column pinned. Thus, whatever width the terminal gives it, the
  glyphs after it go to the columns that the app measured.
* **Measurement** belongs here. Each glyph that may be drawn as an emoji is reserved **two
  cells**, the maximum that one glyph ever draws. When the glyph is pinned into two cells, a glyph
  that the font draws in two cells fills them exactly. A glyph that the font draws in one cell
  leaves a blank next to it: never an overwrite, never a shift. This is what lines up the text
  after an emoji from row to row. The pin alone cannot do that, because it pins to the measured
  column, and the measurement was the thing that changed.

**"May be drawn as an emoji"** comes from Unicode, never from a font:

* A glyph built from several codepoints: a variation-selector sequence (``☀️``), a ZWJ sequence
  (``👨‍👩‍👧``), a keycap (``1️⃣``), a flag's pair of Regional Indicators (``🇨🇦``).
* A lone codepoint that Unicode marks as an emoji with text presentation by default (``☀``,
  ``✈``, ``❤``). This is the set that a variation selector promotes to two cells, which Rich
  ships as ``narrow_to_wide``. Also a lone Regional Indicator. A font can draw each of these two
  kinds at one cell or at two.
* An emoji whose default presentation is already emoji (``📡``, ``👋``) measures two cells in
  both width authorities without a change. This module leaves it to them.

**Two exceptions, both measured at their one cell.** A fast path measures the single-cell ranges
of Rich itself (``©``, ``▶``, box drawing), and that path never examines a patch. Thus all the
other parts must agree with it. Also, :data:`_APP_TEXT_MARKS` is the small set of text-default
emoji that MeshTerm draws itself, as one-cell marks in its own lexicon (``⚠``, ``↕``, ``🗑``).
These marks are a closed vocabulary, not content. The app draws each glyph in the list, the app
checks them (``meshterm specimen``), and a test makes sure that each such glyph in the source is
in the list.

**One rule, all authorities.** Rich measures in three places: a whole string, a single character,
and the grapheme spans by which it crops and wraps. prompt_toolkit measures in a fourth place.
:func:`install` patches all four from the same rule (:func:`_glyph_width`) at the same time, and
they must never be different. The segment splitter of Rich walks a string one codepoint at a
time, until a whole-string width meets the cut that the caller asked for. If a character measures
one cell alone but two cells in a string, the walk never meets that cut.

**Delivery.** prompt_toolkit lays out one codepoint at a time. It folds a zero-width codepoint
into the cell before it, but it keeps the place of that cell. Thus a mark measured at one cell and
followed by a selector becomes a two-cell glyph in a one-cell slot. Instead,
:class:`ClusterTextControl` gives each multi-codepoint glyph to prompt_toolkit as a single
fragment, which is laid out and written as the one glyph that it is.

**Where it applies.** :func:`install` runs one time, before the first frame, when the full-screen
app starts on a platform that draws emoji. Nothing else installs it. Tests, piped output, and the
command line keep the stock tables, because nothing pins their output. There, a glyph that is
reserved two cells but drawn in one, with no pin after it, pulls the rest of its row one cell to
the left.

Two more jobs belong here, next to the measurement, because they ask the same question of the
same glyphs:

* **Where a string can be cut** (:func:`cut_cells`, over :func:`clusters`). A lane that truncates
  a name one codepoint at a time cuts through the middle of a glyph. The pieces that this cut
  leaves (a stranded joiner, half a flag) are not things that a terminal draws at the measured
  width.
* **What can be drawn at all** (:func:`drawable`). A stranger writes the name in an advert, and
  the name can have a newline or an escape in it. No width can make these characters safe.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterator
from functools import lru_cache

from prompt_toolkit.layout.controls import FormattedTextControl
from rich.cells import cell_len

#: The zero-width joiner: the sign that the codepoints on each side of it are **one glyph**
#: (``🤷‍♂️``, ``👨‍👩‍👧``), which a terminal draws in the width of the sequence's base.
_ZWJ = "\u200d"

#: Variation selector 16, the request for emoji presentation.
_VS16 = "\ufe0f"

#: The combining enclosing keycap, which turns the digit or sign before it (and the selector
#: between them) into one keycap glyph: ``1️⃣``.
_KEYCAP = "\u20e3"

#: The five Fitzpatrick skin-tone modifiers (U+1F3FB to U+1F3FF). A modifier after an emoji
#: base changes the colour of that glyph, and does not draw a glyph of its own.
#: Thus ``👍🏽`` is a thumb, not a thumb next to a swatch, and the modifier
#: occupies no cell. Rich's table says zero, and wcwidth says two.
_MODIFIERS = frozenset(chr(cp) for cp in range(0x1F3FB, 0x1F400))

#: What can follow a base in one glyph without a joiner before it: its variation selector, its
#: skin tone, and the enclosing mark of a keycap. A joined run collects these after its base and
#: after each component.
_EXTENDERS = frozenset((_VS16, _KEYCAP, *_MODIFIERS))

#: The Unicode general categories that hold nothing that a terminal can draw: control
#: characters (``Cc``), the format and bidi-control block (``Cf``), surrogates (``Cs``), and
#: the line and paragraph separators (``Zl``/``Zp``). Refer to :func:`drawable`, which folds
#: them to a space. It keeps the one ``Cf`` codepoint that matters: the zero-width joiner.
_UNDRAWABLE = frozenset(("Cc", "Cf", "Cs", "Zl", "Zp"))

#: The text-default emoji that MeshTerm draws itself, as one-cell marks in its own lexicon.
#: Thus MeshTerm measures them at one cell, and does not reserve two. Each one is a glyph that
#: the app put on the screen on purpose and checks through ``meshterm specimen``. It is not a
#: glyph that came in the name of somebody. Thus its width is a fact about the vocabulary of the
#: app, not a guess about a font.
#:
#: A test closes the list, not care: each text-default emoji in the source of the package must
#: be named here. Thus a new mark cannot go into the reserve-two rule without notice. A glyph
#: that is not here is reserved two cells. This costs a blank next to the glyph, and never a row
#: that is not aligned. For this reason, this list can be short, but the sets that it replaced
#: had to be complete.
_APP_TEXT_MARKS = frozenset(
    (
        "⚠",  # ⚠ the warning mark
        "↔",  # ↔ a two-way link: the path composer, the key legend
        "↕",  # ↕ reorder
        "↩",  # ↩ reply, in the chat composer
        "⌨",  # ⌨ the command line
        "⚙",  # ⚙ a parameter
        "\U0001f5d1",  # 🗑 clear, delete
        "\U0001f578",  # 🕸 a Trophy case record
        "\U0001f6e3",  # 🛣 a Trophy case record
    )
)

#: Set when :func:`install` has run, so that later calls are cheap no-ops.
_INSTALLED = False

#: Whether the width authorities were redirected through this module. It gates
#: :class:`ClusterTextControl`. To give prompt_toolkit a multi-codepoint glyph as one character
#: is correct only when something measures that character as one glyph. Thus the delivery and
#: the measurement are switched on together, or not at all.
_CLUSTERS = False


def _is_regional_indicator(char: str) -> bool:
    """Whether ``char`` is a single Regional Indicator Symbol (one part of a country flag).

    The two-letter flags (``🇨🇦``, ``🇺🇸``) are a grapheme pair from this block
    (U+1F1E6 to U+1F1FF). The pair is one glyph. Each half alone is a glyph like a letter, which
    a font can draw at either width. The test is for the whole category, never for each country,
    because the flags share the indicators (``🇨🇦`` and ``🇨🇳`` both start with ``C``).
    """
    return len(char) == 1 and 0x1F1E6 <= ord(char) <= 0x1F1FF


def _joins(char: str) -> bool:
    """Whether a zero-width joiner before ``char`` truly joins it to the glyph before the joiner.

    Each component that an emoji ZWJ sequence joins is a pictograph (``♂``, ``☠``, ``❤``,
    ``💻``, all at U+2000 and above), so that is the test. The test excludes a **stranded**
    joiner. MeshCore cuts the name in an advert at a byte limit. A name that ended on a joined emoji
    (``That's So Fetch 🏴‍☠️``) arrives cut directly after the joiner, followed by whatever the row
    puts after the name: a space of lane padding, or the next lane. If that character is folded
    into the flag, it loses its cell, but the terminal still draws it. The test also
    leaves the joiners of the scripts that use them between letters (the Indic conjuncts, below
    U+2000) as the letters that they are.
    """
    return char >= "\u2000" and not char.isspace()


@lru_cache(maxsize=1)
def _reserved() -> frozenset[str]:
    """All the lone codepoints that are reserved two cells, read from Unicode one time.

    The set starts with the text-default emoji that the table of Rich itself lists
    (``narrow_to_wide``, the codepoints that a variation selector promotes). It removes the
    codepoints that the single-cell fast path of Rich measures, and the marks of the app itself
    (refer to the module docstring for both). Then it adds the Regional Indicators.
    """
    from rich._unicode_data import load
    from rich.cells import _SINGLE_CELLS

    text_default = frozenset(
        char
        for char in load("auto").narrow_to_wide
        if not char.isascii() and char not in _SINGLE_CELLS and char not in _APP_TEXT_MARKS
    )
    return text_default | frozenset(chr(cp) for cp in range(0x1F1E6, 0x1F200))


@lru_cache(maxsize=1)
def _probe() -> re.Pattern[str]:
    """Matches the first codepoint in a string that can make :func:`_glyph_width` different.

    A string with none of these codepoints measures exactly as the stock tables say. Thus the
    string goes to them with no change: one scan at C speed instead of a cluster walk. Nearly
    every string takes this path.
    """
    chars = {_ZWJ, _VS16, _KEYCAP, *_MODIFIERS, *_reserved()}
    return re.compile("[" + "".join(re.escape(char) for char in sorted(chars)) + "]")


def _override(char: str) -> int | None:
    """The width that this module gives a lone codepoint, or ``None`` for the stock answer.

    A skin tone takes no cell (wcwidth gives it two). A reserved codepoint (:func:`_reserved`)
    takes two. For all other codepoints, the stock table decides.
    """
    if char in _MODIFIERS:
        return 0
    if char in _reserved():
        return 2
    return None


def _glyph_width(glyph: str, stock: Callable[[str], int]) -> int:
    """The cells that one glyph is reserved: the one rule that patches each width authority.

    A glyph is reserved two cells if it is built from several codepoints and has a joiner, a
    variation selector, or a keycap. A glyph that is the indicator pair of a flag is also reserved
    two. All other glyphs measure as their base codepoint. A skin tone goes with its base, so an
    emoji with a tone measures the same as the emoji. A letter with a stray modifier stays a
    letter.

    Args:
        glyph: One glyph, as :func:`clusters` splits it.
        stock: The stock width of a single codepoint, in the authority that is patched.

    Returns:
        The width of the glyph in cells.
    """
    base = glyph[0]
    if len(glyph) > 1 and (
        _ZWJ in glyph or _VS16 in glyph or _KEYCAP in glyph or _is_regional_indicator(base)
    ):
        return 2
    width = _override(base)
    return max(0, stock(base)) if width is None else width


def clusters(text: str) -> Iterator[str]:
    """Split ``text`` into the glyphs that the terminal draws, never in the middle of one.

    A cluster is the same run that :func:`_join_clusters` collects from a laid-out line, but
    read from a plain string. It is a base, then what extends it (:data:`_EXTENDERS`: a
    variation selector, a skin tone, the enclosing mark of a keycap), then any number of
    *joiner + component + extenders* groups. A cluster is also the Regional Indicator **pair**
    from which a country flag is drawn. A joiner that joins nothing (:func:`_joins`) is a
    cluster of its own, because it is not part of the glyph before it.

    A lane must cut on this unit, and a width is measured by it. A cut through the middle of a
    glyph leaves pieces that a terminal does not draw at the measured width: a lone Regional
    Indicator drawn as a letter, a skin tone with no base, or a stranded joiner. Refer to
    :func:`cut_cells`.
    """
    index = 0
    count = len(text)
    while index < count:
        start = index
        index += 1
        while index < count and text[index] in _EXTENDERS:
            index += 1
        if _is_regional_indicator(text[start]) and index < count:
            if _is_regional_indicator(text[index]):
                index += 1
                while index < count and text[index] in _EXTENDERS:
                    index += 1
        while index + 1 < count and text[index] == _ZWJ and _joins(text[index + 1]):
            index += 2  # the joiner and the codepoint that it joins
            while index < count and text[index] in _EXTENDERS:
                index += 1
        yield text[start:index]


def cut_cells(text: str, width: int) -> str:
    """The longest prefix of ``text`` that fits in ``width`` cells, cut **between** glyphs.

    A fixed lane needs this prefix when a name is longer than the lane
    (:func:`~meshterm.ui.menus.fit_cells`). The lane cannot walk the string one character at a
    time, for this reason: the owner of the radio writes a MeshCore name, so the name comes
    with whatever emoji the owner typed. A cut at each codepoint lands in the middle of an
    emoji approximately as often as not. A cut of
    ``"👨‍👩‍👧"`` after its first joiner leaves a stranded joiner at the end of the lane, and
    a cut of a flag in half leaves a lone Regional Indicator, which is a letter, not half a
    flag.

    Thus the prefix is made of whole :func:`clusters`. A trailing joiner is removed, and not
    left next to the text that the caller adds after it. The cut itself can strand the
    joiner, or the joiner can be already stranded in the name.

    Args:
        text: The text to cut.
        width: The maximum number of cells that the result can occupy.

    Returns:
        A prefix that measures at most ``width`` cells, with each glyph in it whole.
    """
    kept: list[str] = []
    used = 0
    for cluster in clusters(text):
        size = cell_len(cluster)
        if used + size > width:
            break
        kept.append(cluster)
        used += size
    while kept and kept[-1] == _ZWJ:
        kept.pop()
    return "".join(kept)


def drawable(text: str) -> str:
    """``text`` with each codepoint that a terminal cannot draw folded to a space.

    A name comes over the radio from the node of a stranger, and nothing on the way in
    promises that it holds only characters that a terminal can draw. A newline in a name ends
    the row in the middle of a lane. An ``ESC`` starts an escape sequence in the middle of a
    screen that the app composed. A bidi override (``U+202E``) reverses all that is drawn
    after it. None of these is a question of width. They are text that must not get to the
    terminal, so a lane folds them to the one character that is always safe.

    Folded: control characters, the format and bidi-control block, surrogates, and the line
    and paragraph separators. **Kept:** the zero-width joiner, which is also in the format
    class and is the sign that holds an emoji sequence together. Also kept: each mark and
    modifier from which a glyph is built. Private-use codepoints are also kept, because a
    Nerd Font keeps in that block the powerline glyphs with which the path line is drawn.
    """
    return "".join(
        " " if char != _ZWJ and unicodedata.category(char) in _UNDRAWABLE else char for char in text
    )


def install() -> None:
    """Patch each width authority with the reserve-two rule, one time, before the first frame.

    The patches are: the whole-string width of Rich, its single-character width (in both
    modules that read it), its grapheme spans, and the width cache of prompt_toolkit. All of
    them use :func:`_glyph_width`. The module docstring gives the reason for all four, and for
    the patch of all four at the same time. Each patch gives a string with nothing reservable in
    it directly to the stock code that it replaced. Thus the common case pays for one scan.

    It is safe to call this function more than one time. Only the first call does something.
    The menu loop calls it on a platform that draws emoji. The module docstring gives the
    reason why nothing else calls it.
    """
    global _INSTALLED, _CLUSTERS
    if _INSTALLED:
        return
    _INSTALLED = True

    import prompt_toolkit.utils as ptu
    import rich.cells as cells
    import rich.segment as segment

    from .render import _ANSI_CACHE

    probe = _probe()
    stock_size = cells.get_character_cell_size
    stock_cell_len = cells._cell_len
    stock_split = cells.split_graphemes
    single_cells = cells._is_single_cell_widths

    @lru_cache(maxsize=4096)
    def get_character_cell_size(character: str, unicode_version: str = "auto") -> int:
        width = _override(character)
        return stock_size(character, unicode_version) if width is None else width

    def _cell_len(text: str, unicode_version: str = "auto") -> int:
        if single_cells(text):
            return len(text)
        if probe.search(text) is None:
            return stock_cell_len(text, unicode_version)
        return sum(
            _glyph_width(glyph, lambda char: stock_size(char, unicode_version))
            for glyph in clusters(text)
        )

    def split_graphemes(text: str, unicode_version: str = "auto") -> tuple[list, int]:
        if probe.search(text) is None:
            return stock_split(text, unicode_version)
        spans: list[tuple[int, int, int]] = []
        total = 0
        index = 0
        for glyph in clusters(text):
            end = index + len(glyph)
            width = _glyph_width(glyph, lambda char: stock_size(char, unicode_version))
            if width == 0 and spans:
                # Zero-width pieces belong to the glyph before them, as in the splitter of Rich
                # itself. Thus a crop never lands between a letter and its mark.
                start, _end, size = spans[-1]
                spans[-1] = (start, end, size)
            else:
                spans.append((index, end, width))
                total += width
            index = end
        return spans, total

    class _ReservingCache(type(ptu._CHAR_SIZES_CACHE)):  # type: ignore[misc]
        """The width cache of prompt_toolkit, which gets its answers from :func:`_glyph_width`."""

        def __missing__(self, string: str) -> int:
            if len(string) == 1:
                width = _override(string)
                if width is None:
                    return super().__missing__(string)
                self[string] = width
                return width
            if probe.search(string) is None:
                return super().__missing__(string)
            total = sum(_glyph_width(glyph, self.__getitem__) for glyph in clusters(string))
            # A whole line that holds an emoji is cheap to compute again. If a long line is
            # cached here, it goes past the rotation of long strings in the base class, and the
            # cache grows without limit.
            if len(string) <= self.LONG_STRING_MIN_LEN:
                self[string] = total
            return total

    cells.get_character_cell_size = get_character_cell_size
    segment.get_character_cell_size = get_character_cell_size
    cells._cell_len = _cell_len
    cells.split_graphemes = split_graphemes
    cells.cached_cell_len.cache_clear()
    ptu._CHAR_SIZES_CACHE = _ReservingCache()
    _CLUSTERS = True
    # The measurement changed under each renderable. Thus all that was rasterized before is
    # stale. (In the normal boot order, nothing was rasterized before, but never trust that.)
    _ANSI_CACHE.clear()


class _Cluster(str):
    """A multi-codepoint glyph that must reach the screen buffer as **one** character.

    prompt_toolkit builds its screen buffer one codepoint at a time (``for c in text`` in
    ``Window._copy_body``), and gives each codepoint its own ``Char``, which occupies a cell.
    Thus a glyph that is correctly measured at two cells is still laid out as its parts: a shrug
    followed by a male sign, a flag as two letters, a warning mark with a selector folded into a
    slot one cell wide. An iteration over a cluster yields the whole glyph instead, so that loop
    makes a single ``Char`` of it. The result is one width in the arithmetic of prompt_toolkit,
    each codepoint written to the terminal one after the other, and one composed glyph on the
    terminal.

    It is a ``str`` subclass instead of a wrapper, because it must stay ordinary text through
    all the other things that prompt_toolkit does with a fragment: join, slice, and compare.
    ``str`` methods return plain ``str``, so the first ``split`` loses the mark. For this
    reason, :class:`ClusterTextControl` applies the mark to the finished lines, and not earlier.
    """

    __slots__ = ()

    def __iter__(self):  # type: ignore[override]
        yield str(self)


#: If one of these codepoints is in a laid-out line, a glyph on that line is built from several.
_JOINERS = frozenset((_ZWJ, *_EXTENDERS))


def _join_clusters(line: list) -> list:
    """Merge each multi-codepoint glyph in one screen line into a single :class:`_Cluster` fragment.

    The line arrives with one codepoint in each fragment (that is what
    :class:`~prompt_toolkit.formatted_text.ANSI` produces). Thus a glyph built from several
    codepoints is a run to collect. It is the same run that :func:`clusters` reads from a plain
    string: a base and what extends it, the indicator pair of a flag, and any number of
    *joiner + component + extenders* groups.

    Each such glyph is merged, also a lone selector pair and an emoji with a tone. Without the
    merge, prompt_toolkit folds those into the cell before them itself, but while it does, it
    keeps the place of that cell. Thus a mark measured at one cell and followed by a selector
    becomes a two-cell glyph in a one-cell slot. Also,
    :class:`~meshterm.ui.tui.colsnap.PinnedOutput` pins the writes of the renderer. A pin
    between the two halves of a flag is a cursor move between them, and after that move, a
    terminal no longer joins them into one glyph.

    A joiner with no pictograph after it (refer to :func:`_joins`) is not part of a glyph, and
    keeps its fragment. When nothing on the line can start or extend such a glyph, the whole
    line is returned with no change. That is true of almost every line. The scan runs only when
    the content of a control changes.
    """
    if not any(item[1] in _JOINERS or _is_regional_indicator(item[1]) for item in line):
        return line
    merged: list = []
    index = 0
    count = len(line)
    while index < count:
        end = index + 1
        while end < count and line[end][1] in _EXTENDERS:
            end += 1
        if (
            _is_regional_indicator(line[index][1])
            and end < count
            and _is_regional_indicator(line[end][1])
        ):
            end += 1
            while end < count and line[end][1] in _EXTENDERS:
                end += 1
        while end + 1 < count and line[end][1] == _ZWJ and _joins(line[end + 1][1]):
            end += 2  # the joiner and the codepoint that it joins
            while end < count and line[end][1] in _EXTENDERS:
                end += 1
        if end - index > 1:
            text = "".join(item[1] for item in line[index:end])
            merged.append((line[index][0], _Cluster(text)))
        else:
            merged.append(line[index])
        index = end
    return merged


class ClusterTextControl(FormattedTextControl):
    """A :class:`~prompt_toolkit.layout.controls.FormattedTextControl` that keeps glyphs whole.

    The control is the last place where the text of a screen is still in lines, and where we
    can still change it. A merge of a glyph needs exactly that. ``split_lines`` runs before
    this point and returns plain ``str`` parts, so a :class:`_Cluster` marked earlier does not
    stay until the layout. All the steps after this point (the width lookup, the wrapping, the
    screen buffer) read the merged lines.

    The merge is gated on :func:`install`: it occurs only after that function has run. To give
    prompt_toolkit a glyph as one character is correct only when its cache measures that
    character as one glyph. Off that path (a test, a platform that draws no emoji), this class
    is exactly its base class.
    """

    def create_content(self, width: int, height: int | None):  # type: ignore[override]
        """The content of the base class, with the multi-codepoint glyphs of each line joined.

        The join is wrapped around the line lookup of the returned object, and cached behind
        it. prompt_toolkit returns the same content object paint after paint, and a walk over
        each line of each frame costs much more than it saves.
        """
        content = super().create_content(width, height)
        # The base class caches its ``UIContent`` for each (fragments, width, cursor), so the
        # same object comes back paint after paint. Thus wrap its line lookup one time, and
        # cache the merge behind it, instead of a new walk over each line of each frame.
        if _CLUSTERS and not getattr(content, "_clusters_merged", False):
            content._clusters_merged = True  # type: ignore[attr-defined]
            source = content.get_line
            cache: dict[int, list] = {}

            def get_line(i: int, _source=source, _cache=cache) -> list:
                line = _cache.get(i)
                if line is None:
                    line = _cache[i] = _join_clusters(_source(i))
                return line

            content.get_line = get_line
        return content


__all__ = ["ClusterTextControl", "clusters", "cut_cells", "drawable", "install"]
