# SPDX-License-Identifier: Apache-2.0
"""Markdown that MeshTerm draws in its own style. This is the renderer for the About pages.

A page of prose is the one type of screen that the app cannot build from rows, lanes, and
marks, because it is paragraphs. Thus the prose is in ``.md`` files in ``meshterm/assets``,
and this module changes them into Rich renderables. The module does not print the
conventions of markdown (boxed headings, ``#`` marks left in the text, and a slab with
syntax colours for each fence). Instead it maps each construct onto the visual language
that the other screens of the app already use. Thus a written page can be next to a
screen of contacts and not look as if it came from another program:

* ``#`` is the name of the page itself, in the brand hue. It is the wordmark, the author,
  or whatever the page is about. ``##`` is a section, in the same accent that each body
  heading uses. ``###`` is a sub-heading in a section. A heading can have the standard
  muted aside if you write ``## Title · note``. Emphasis in a heading is its qualifier.
  This is how ``# MeshTerm *v0.1.0*`` prints the version in muted style beside the name.
* The prose of a page hangs at :data:`INDENT` cells under its flush heading, as a block.
  A line that wraps is under the paragraph that it belongs to. It never goes back to
  column 0, where it would look like a new thought with no heading (the hanging-indent
  rule of ``CLAUDE.md``).
* Two paragraphs are the page frame and not the body. Both are flush and muted. The
  **standfirst** is directly under the ``#`` title (the one-line description of
  MeshTerm, or the "wrote MeshTerm" of the author). The **colophon** is the last
  paragraph of the document, below a ``---`` rule. The copyright is there.
* A list hangs on a bullet in a two-column grid. Thus a wrapped item is under its own
  text, and a nested list is indented under its parent. Quotes and fenced code have a rail
  down the left side. The rail is on each line, also on the lines that wrap.
* A fence with the tag ``qr`` draws nothing of itself. Its body is a URL, and the page
  shows the scannable code of the app for that URL (:func:`~meshterm.ui.qr.qr_text`).
* Emphasis, code, links, and struck-out text use the ``md.*`` styles. These styles, the
  same as each style name here, have a meaning on both platforms (refer to
  :mod:`meshterm.ui.theme`). A link shows its target after the text, unless the text
  already shows it. Nothing here emits an OSC hyperlink, because many users of the app
  have a framebuffer console.

The parser is :mod:`markdown_it` (CommonMark, with tables and strikethrough). It is a real
parser and not a set of regular expressions, so an author can write ordinary markdown
and get the result that the author intended. All the code below walks its tree. Nothing
else in the app must know that markdown exists.

The output is a :class:`MarkdownDoc`. It is a plain Rich renderable and also a list of
top-level blocks. Thus a screen can render the page block by block and record the place
where each ``##`` section starts. This is the same landmark mechanism that a grouped
select list uses, so the headings pin to the top row while their prose scrolls under them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode
from rich import box
from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.padding import Padding
from rich.rule import Rule
from rich.segment import Segment
from rich.styled import Styled
from rich.table import Table
from rich.text import Text

#: The number of cells at which the prose of a page hangs under its flush heading. The code
#: applies it as :class:`~rich.padding.Padding` around the whole block and not as spaces
#: in the text, so a paragraph that wraps keeps its indent on each line.
INDENT = 2

#: The parser: CommonMark, with the two GFM extensions that a page needs. The typographer
#: stays off. It would change quotes and dashes to characters that the console font may
#: not have, and the app writes its own em dashes.
_MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])

#: The bullets for each nesting depth. They repeat in turn. Both glyphs are in the PicoCalc
#: console font, and the mark language does not use either of them anywhere else. ``•`` is
#: not a status. ``▸`` is the shape of the list pointer, which is correct for a sub-item.
_BULLETS = ("•", "▸")

#: The left rail that a quote or a fenced block has, and the width in cells that it uses.
_RAIL = "│ "
_RAIL_W = cell_len(_RAIL)

#: The style of the text of a heading, for each level. ``h3`` and the deeper levels use
#: :data:`_SUBHEADING_STYLE`. They are indented with the prose that they head, because a
#: sub-heading belongs to its section and does not stand beside it.
_HEADING_STYLES = {"h1": "brand", "h2": "accent"}
_SUBHEADING_STYLE = "md.strong"

#: The schemes that the code removes from the target of a link when it shows the target
#: beside the link text. On a page that is 53 cells wide, the host and the path are
#: important, and the protocol is not.
_URL_NOISE = ("https://", "http://", "mailto:")


@dataclass(frozen=True)
class Block:
    """One top-level block of a rendered page.

    Attributes:
        renderable: The Rich content of the block: a paragraph, a list, a rule, or a blank
            separator line.
        heading: Whether this block is one of the section landmarks of the page (refer to
            :attr:`MarkdownDoc.sections`).
        label: The plain text of the heading, for a landmark. Empty for other blocks.
        qr: Whether this block is the scannable code of a ``qr`` fence. The block is
            marked so that the scripted CLI, which cannot use a code, can remove it
            (refer to :meth:`MarkdownDoc.for_script`).
        rule: Whether this block is a ``---`` horizontal rule. The block is marked for the
            same reason. A rule is drawn at the full width of the console that prints it,
            and the scripted console is 16384 cells wide.
    """

    renderable: RenderableType
    heading: bool = False
    label: str = ""
    qr: bool = False
    rule: bool = False


class MarkdownDoc:
    """A rendered markdown page: one renderable, and the blocks that it has.

    If you print it (``meshterm about`` of the scripted CLI), it draws the blocks in
    order, so it behaves as any other Rich renderable. A full-screen page instead walks
    :attr:`blocks`. It renders each block and records the line where each section heading
    is. This makes a heading sticky, and it gives the ``^PgUp``/``^PgDn`` section jumps a
    meaning on a page that is long enough to scroll.
    """

    def __init__(self, blocks: Iterable[Block]) -> None:
        """Keep the ``blocks``, which are already rendered, as one page."""
        self.blocks: tuple[Block, ...] = tuple(blocks)

    @property
    def sections(self) -> tuple[str, ...]:
        """The section headings of the page, in order. A jump steps from one to the next."""
        return tuple(block.label for block in self.blocks if block.heading)

    def for_script(self) -> MarkdownDoc:
        """The same page without the parts that only a screen can use, and with no gap.

        Two blocks are for the eye. In a pipe they are worse than useless:

        * A **QR** is a second rendering of a URL that the page already prints as a link.
          It is drawn for a phone that points at a screen. If the output goes to a file,
          the QR is a block of block characters that has no new information.
        * A **rule** has the size of the console that prints it, and the scripted console
          is :data:`~meshterm.ui.script.WIDTH` cells wide. Thus the ``---`` above a
          colophon becomes one line of 16384 characters, which is tens of kilobytes of
          ``─`` in a page of prose. A rule is also a frame, and the scripted CLI does not
          draw frames. The blank line on each side of the rule already gives the same
          information.

        Returns:
            A new document. This document is not changed.
        """
        kept: list[Block] = []
        for block in self.blocks:
            if block.qr or block.rule:
                # The blank separator that was before the removed block goes with it. Thus
                # the removal does not leave two blank lines where the block was.
                if kept and isinstance(kept[-1].renderable, Text) and not kept[-1].renderable.plain:
                    kept.pop()
                continue
            kept.append(block)
        return MarkdownDoc(kept)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render each block in order. This makes the document a plain renderable."""
        for block in self.blocks:
            yield block.renderable


class _Railed:
    """A renderable with a rail down its left edge, on each line that it wraps to.

    Rich has no such box. A panel draws all four sides, and a table cell draws its rail one
    time against a neighbour of many lines. The rail is the purpose of a quote or a fenced
    block, so it must stay when the text wraps. If it does not, a quote of three lines
    looks like a quote of one line with two loose lines after it.
    """

    def __init__(self, body: RenderableType, *, style: str) -> None:
        """Draw ``body`` with a rail in ``style`` beside it."""
        self._body = body
        self._style = style

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        """Render the body at a width that the rail reduces, then add the rail to each line."""
        rail = Segment(_RAIL, console.get_style(self._style, default="none"))
        inner = options.update_width(max(1, options.max_width - _RAIL_W))
        for line in console.render_lines(self._body, inner, pad=True):
            yield rail
            yield from line
            yield Segment.line()


def render_markdown(source: str) -> MarkdownDoc:
    """Render a markdown document in the style that MeshTerm uses for prose.

    Args:
        source: The markdown text (CommonMark, with tables and ``~~strikethrough~~``).

    Returns:
        The page. It is ready to print, or to put in a :class:`~meshterm.ui.about.AboutPage`.
    """
    return MarkdownDoc(_document(SyntaxTreeNode(_MD.parse(source)).children))


# -- the shape of the document --------------------------------------------------------


def _document(nodes: Sequence[SyntaxTreeNode]) -> list[Block]:
    """Lay out the top-level blocks, with the page frame and the blank lines between them.

    One blank line separates the blocks, but not directly after a heading. The first
    paragraph of a section is directly under its heading, as in each other body section of
    the app. This function recognizes the two frame paragraphs (the standfirst and the
    colophon), and :func:`_block` does not. A paragraph is a frame paragraph because of
    its place in the page, not because of anything in the paragraph itself.
    """
    landmark = _landmark_tag(nodes)
    blocks: list[Block] = []
    for index, node in enumerate(nodes):
        previous = nodes[index - 1] if index else None
        if blocks and (previous is None or previous.type != "heading"):
            blocks.append(Block(Text()))
        frame = _frame_paragraph(node, previous, last=index == len(nodes) - 1)
        if frame is not None:
            blocks.append(Block(frame))
            continue
        heading = node.type == "heading" and node.tag == landmark
        label = _inline(node.children[0], heading=True).plain if heading else ""
        qr = node.type == "fence" and node.info.strip() == "qr"
        rule = node.type == "hr"
        for rendered in _block(node, indent=INDENT, depth=0):
            blocks.append(Block(rendered, heading=heading, label=label, qr=qr, rule=rule))
            heading = False  # a heading is one block, so no second block gets the mark
    return blocks


def _landmark_tag(nodes: Sequence[SyntaxTreeNode]) -> str:
    """The heading level of the sections of this page: ``h2``, or ``h1`` if there is no ``h2``.

    A page normally has its title with ``#`` and its sections with ``##``. A page that has
    only ``#`` headings gets the same landmarks, and not no landmarks.
    """
    tags = {node.tag for node in nodes if node.type == "heading"}
    return "h2" if "h2" in tags else "h1"


def _frame_paragraph(
    node: SyntaxTreeNode, previous: SyntaxTreeNode | None, *, last: bool
) -> RenderableType | None:
    """The two flush muted lines of the page frame, or ``None`` for an ordinary block.

    The standfirst is the paragraph directly under the page title. The colophon is the
    last paragraph of the document, below a ``---`` rule. Both describe the page and say
    nothing in it. Thus both are flush with the title and are less prominent, in the same
    voice as an empty state.
    """
    if node.type != "paragraph" or previous is None:
        return None
    standfirst = previous.type == "heading" and previous.tag == "h1"
    colophon = last and previous.type == "hr"
    if not (standfirst or colophon):
        return None
    return _inline(node.children[0], style="muted")


# -- blocks ---------------------------------------------------------------------------


def _block(node: SyntaxTreeNode, *, indent: int, depth: int) -> list[RenderableType]:
    """Render one block node.

    Args:
        node: The syntax-tree node of the block.
        indent: The number of cells at which the prose hangs. At the page level it is
            :data:`INDENT`. It is ``0`` in a container that already gives the offset (a
            list item, a quote, or a cell).
        depth: The nesting depth of the list. It selects the bullet.

    Returns:
        The renderables of the block (usually one).
    """
    kind = node.type
    if kind == "heading":
        return [_heading(node, indent=indent)]
    if kind == "paragraph":
        return [_pad(_inline(node.children[0]), indent)]
    if kind in ("bullet_list", "ordered_list"):
        return [_pad(_list(node, depth=depth), indent)]
    if kind == "blockquote":
        quoted = Group(*_blocks(node.children, indent=0, depth=depth))
        return [_pad(_Railed(Styled(quoted, "md.quote"), style="md.bullet"), indent)]
    if kind == "fence" and node.info.strip() == "qr":
        return [_pad(_qr(node.content.strip()), indent)]
    if kind in ("fence", "code_block"):
        code = Text(node.content.rstrip("\n"), style="md.code")
        return [_pad(_Railed(code, style="md.rail"), indent)]
    if kind == "hr":
        return [Rule(style="md.rail")]
    if kind == "table":
        return [_pad(_table(node), indent)]
    if kind in ("html_block", "html_inline"):
        # A page must not have raw HTML. But it is worse to remove content with no
        # message than to show it, so print it as the muted literal text that it is.
        return [_pad(Text(node.content.rstrip("\n"), style="muted"), indent)]
    if node.children:
        return _blocks(node.children, indent=indent, depth=depth)
    return []


def _blocks(
    nodes: Sequence[SyntaxTreeNode], *, indent: int, depth: int, tight: bool = False
) -> list[RenderableType]:
    """Render sibling blocks with a blank line between them, unless ``tight`` (in a list item)."""
    rendered: list[RenderableType] = []
    for index, node in enumerate(nodes):
        if rendered and not tight and nodes[index - 1].type != "heading":
            rendered.append(Text())
        rendered.extend(_block(node, indent=indent, depth=depth))
    return rendered


def _qr(data: str) -> RenderableType:
    """A ``qr`` fence: the URL in it, drawn as the scannable code of the app.

    The code is generated when the page opens. It is not pasted into the ``.md`` as
    half-block art. Art does not survive markdown. A row that starts with a light module
    starts with a space, and each parser removes that space. Then the row moves one module
    to the left. A code that nobody can scan is worse than no code. If the code is drawn
    at run time, the link is written only one time, in the fence, and the code can never
    be different from it.

    Args:
        data: The body of the fence, which is the URL to encode.

    Returns:
        The code from :func:`~meshterm.ui.qr.qr_text`. It is the same white-on-black
        widget that the share screen draws. Thus a QR looks the same everywhere in
        MeshTerm, and it reads the same with any palette of the terminal. This is the only
        place where a code is inside a page and does not take the whole frame. It is an
        illustration in the prose, drawn where the fence is.
    """
    from .qr import qr_text

    return qr_text(data)


def _pad(renderable: RenderableType, indent: int) -> RenderableType:
    """Hang ``renderable`` at ``indent`` cells. If it is flush, return it as it is."""
    if not indent:
        return renderable
    return Padding(renderable, (0, 0, 0, indent))


def _heading(node: SyntaxTreeNode, *, indent: int) -> RenderableType:
    """A heading: the page title, a section, or a sub-heading in a section."""
    style = _HEADING_STYLES.get(node.tag, _SUBHEADING_STYLE)
    text = _inline(node.children[0], style=style, heading=True)
    return text if node.tag in _HEADING_STYLES else _pad(text, indent)


def _list(node: SyntaxTreeNode, *, depth: int) -> RenderableType:
    """A bullet list or a numbered list, with one hanging-indent grid row for each item.

    Each item is a grid of two columns: its marker, then its content. Thus a wrapped item
    is under its own first line, and a nested list is indented under the text that it
    belongs to. A loose list is a list that the author spaced out in the source. It keeps
    that spacing.
    """
    ordered = node.type == "ordered_list"
    start = int(node.attrs.get("start", 1)) if ordered else 1
    items = node.children
    markers = [
        f"{start + index}." if ordered else _BULLETS[depth % len(_BULLETS)]
        for index in range(len(items))
    ]
    width = max(cell_len(marker) for marker in markers) + 1 if markers else 0
    loose = any(
        child.type == "paragraph" and not child.hidden for item in items for child in item.children
    )

    rows: list[RenderableType] = []
    for marker, item in zip(markers, items, strict=True):
        if rows and loose:
            rows.append(Text())
        grid = Table.grid(padding=0)
        grid.add_column(width=width, no_wrap=True)
        grid.add_column(overflow="fold")
        body = _blocks(item.children, indent=0, depth=depth + 1, tight=not loose)
        grid.add_row(
            Text(marker.ljust(width), style="md.bullet"),
            Group(*body) if len(body) != 1 else body[0],
        )
        rows.append(grid)
    return Group(*rows)


def _table(node: SyntaxTreeNode) -> Table:
    """A markdown table as a Rich table: accent headers, with one thin line under them.

    The alignment of a column follows the ``---:`` and ``:---:`` markers of the markdown.
    The parser gives them as a CSS ``text-align`` on each cell.
    """
    table = Table(
        box=box.SIMPLE_HEAD,
        show_edge=False,
        pad_edge=False,
        header_style="accent",
        border_style="md.rail",
    )
    head = next((child for child in node.children if child.type == "thead"), None)
    body = next((child for child in node.children if child.type == "tbody"), None)
    header_cells = head.children[0].children if head and head.children else []
    for cell in header_cells:
        table.add_column(
            _inline(cell.children[0]) if cell.children else Text(), justify=_justify(cell)
        )
    for row in body.children if body else []:
        table.add_row(
            *(_inline(cell.children[0]) if cell.children else Text() for cell in row.children)
        )
    return table


def _justify(cell: SyntaxTreeNode) -> str:
    """The alignment of a table cell, from the ``text-align`` attribute of the parser."""
    align = str(cell.attrs.get("style", ""))
    if "right" in align:
        return "right"
    if "center" in align:
        return "center"
    return "left"


# -- inline runs ----------------------------------------------------------------------


def _inline(node: SyntaxTreeNode, *, style: str = "", heading: bool = False) -> Text:
    """Render the children of an inline node into one styled :class:`~rich.text.Text`.

    Args:
        node: The ``inline`` node (the single child of a paragraph, a heading, or a cell).
        style: The base style of the plain text of the run.
        heading: Whether this run is a heading. In a heading the marks have a different
            meaning: emphasis becomes the muted qualifier, and a ``·`` starts the muted
            aside.

    Returns:
        The rendered run.
    """
    text = Text(style=style or "")
    _append(text, node.children, style=style, heading=heading)
    return text


def _append(text: Text, nodes: Sequence[SyntaxTreeNode], *, style: str, heading: bool) -> None:
    """Append each inline node to ``text``, in the style that its mark needs."""
    for node in nodes:
        kind = node.type
        if kind == "text":
            _append_text(text, node.content, style=style, heading=heading)
        elif kind == "strong":
            _append(text, node.children, style="md.strong", heading=heading)
        elif kind == "em":
            _append(text, node.children, style="muted" if heading else "md.em", heading=heading)
        elif kind == "s":
            _append(text, node.children, style="md.strike", heading=heading)
        elif kind == "code_inline":
            text.append(node.content, style="md.code")
        elif kind == "link":
            _append_link(text, node, style=style, heading=heading)
        elif kind == "image":
            _append(text, node.children, style="md.em", heading=heading)
        elif kind == "softbreak":
            text.append(" ", style=style or None)
        elif kind == "hardbreak":
            text.append("\n", style=style or None)
        elif kind == "html_inline":
            text.append(node.content, style=style or None)
        elif node.children:
            _append(text, node.children, style=style, heading=heading)
        elif node.content:
            text.append(node.content, style=style or None)


def _append_text(text: Text, content: str, *, style: str, heading: bool) -> None:
    """Append plain text. In a heading, the ``·`` aside is split off and made muted."""
    if heading and " · " in content:
        title, _, note = content.partition(" · ")
        text.append(title, style=style or None)
        text.append(f"  ·  {note}", style="muted")
        return
    if content:
        text.append(content, style=style or None)


def _append_link(text: Text, node: SyntaxTreeNode, *, style: str, heading: bool) -> None:
    """Append a link: its text, then its target unless the text already shows it.

    Nothing here is clickable, because one of the platforms is a framebuffer console. A
    link with the text "the repo" must show where the repo is. If it does not, it gives
    no information. If the text of a link already has the address (a bare URL, or an
    autolink), the address is shown one time.
    """
    label = Text()
    _append(label, node.children, style="md.link", heading=heading)
    text.append_text(label)
    target = _display_url(str(node.attrs.get("href", "")))
    if target and target.lower() not in label.plain.lower():
        text.append(" ", style=style or None)
        text.append(target, style="muted")


def _display_url(href: str) -> str:
    """A link target as a page shows it: no scheme, no ``www.``, and no trailing slash.

    An anchor in the page shows nothing, because it names a place in the page that the user
    is already on.
    """
    if not href or href.startswith("#"):
        return ""
    for noise in _URL_NOISE:
        if href.lower().startswith(noise):
            href = href[len(noise) :]
            break
    if href.lower().startswith("www."):
        href = href[4:]
    return href.rstrip("/")


__all__ = ["INDENT", "Block", "MarkdownDoc", "render_markdown"]
