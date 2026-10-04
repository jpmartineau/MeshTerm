# SPDX-License-Identifier: Apache-2.0
"""The About MeshTerm pages: the app, the person who wrote it, and how to help it along.

Four read-only pages hung under the main menu's *About MeshTerm* section — the one
section that names a subject rather than a doing, because "what is this thing, and who
made it" is a question the other five can't answer. Each opens as a full screen, scrolls
if the terminal is short, and is left with Esc; nothing here touches the radio or the
database, so a page reads the same with no device attached at all.

Unlike every other screen in the app, these are **written** rather than composed: their
content is markdown, living beside the wordmark in ``meshterm/assets/pages``, drawn
through :mod:`meshterm.ui.markdown` in the app's own visual language (see that module
for what each construct becomes). Filling a page in means editing its ``.md`` file —
no screen, menu row, or CLI face has to follow — and everything markdown offers is
available while doing it: sections, sub-headings, lists, quotes, links, tables.

All four are written. The ``##`` headings are the shape each page was written into,
and they are load-bearing beyond the eye: the screen records where each one landed while
rendering, which is what lets it pin the heading a reader is under and step the section
jump from one to the next — so a page is navigable the way a grouped list is.

What is *not* written into those files: the version, the author, and the copyright span.
Each page names them with a ``{version}`` / ``{author}`` / ``{copyright}`` placeholder
that is filled from :mod:`meshterm` itself (:func:`~meshterm.copyright_notice`) as the
page is opened, so the About page can never drift from the package it describes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .. import __author__, __version__, copyright_notice
from .markdown import MarkdownDoc, render_markdown
from .tui import ScrollScreen
from .tui.render import render_lines

if TYPE_CHECKING:
    from ..context import AppContext

#: Where the written pages live — beside the wordmark, in the package's assets, so they
#: ship with the wheel and can be edited without touching Python.
_PAGES = Path(__file__).resolve().parent.parent / "assets" / "pages"


def _page(name: str) -> MarkdownDoc:
    """Load ``assets/pages/<name>.md``, fill in the live package facts, and render it.

    Args:
        name: The page file's stem.

    Returns:
        The rendered page.
    """
    source = (_PAGES / f"{name}.md").read_text(encoding="utf-8")
    for token, value in (
        ("{version}", __version__),
        ("{author}", __author__),
        ("{copyright}", copyright_notice()),
    ):
        source = source.replace(token, value)
    return render_markdown(source)


def about_meshterm() -> MarkdownDoc:
    """The *About MeshTerm* page: what the app is, where it came from, its terms."""
    return _page("about")


def about_author() -> MarkdownDoc:
    """The *About the author* page: the person behind MeshTerm, on the mesh and off it."""
    return _page("author")


def join_discord() -> MarkdownDoc:
    """The *Join Discord* page: the invite link, and a QR of it to point a phone at.

    A page rather than a popup because there is nothing to decide — it is a link, and on
    a headless console the QR *is* how the link gets off the screen.
    """
    return _page("discord")


def support_project() -> MarkdownDoc:
    """The *Support MeshTerm* page: what keeps it going, and how to chip in.

    Deliberately two-sided — money is one way to help and not the only one, so the page
    keeps a section for the other kind rather than folding it into a donate link.
    """
    return _page("support")


class AboutPage(ScrollScreen):
    """One written page: a read-only body, drawn full-screen, left with Esc.

    Nearly all of it is the shared read-only screen — the pager, the derived footer hint
    that only names the pager when there *is* something to page, the PicoCalc F-key lane
    that dims on the same gate — with two things added, both of which come from the body
    being a *document* rather than a block:

    * It renders block by block and records where each ``##`` section starts, so a
      heading pins to the top row while its prose scrolls under it and ``^PgUp``/``^PgDn``
      step section by section (the same landmark machinery a grouped select list uses).
    * Its lane claims the left-hand ``Sect ↑`` / ``Sect ↓`` pair once the page has more
      than one section, because on the console the lane is the only place a chord can
      advertise itself.

    Only the framing differs from a result window: these are full screens rather than
    floating views, so Esc reads *back*.
    """

    def __init__(self, title: str, doc: MarkdownDoc) -> None:
        """Show ``doc`` as the page under ``title``.

        Args:
            title: The page's heading — sentence case, no icon (icons live in the menu
                rows that open these pages, never in a title).
            doc: The rendered page, from one of the builders above.
        """
        super().__init__(doc, title=title, floating=False)
        self._doc = doc

    @property
    def picocalc_lyra_lane(self):
        """The shared pager lane, plus the section step on a page that has sections.

        A left-hand pair rises toward F1, so ``Sect ↑`` sits outside ``Sect ↓`` — the
        order a grouped select list uses on the same two keys, dispatching the same two
        actions. Both are lit only while the page actually scrolls: with the whole page
        on screen there is no section to step *to*.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=self.content_overflows))
        if len(self._doc.sections) >= 2:
            lane[0] = FPair("Sect ↑", "ctrl_pageup", enabled=self.content_overflows)
            lane[1] = FPair("Sect ↓", "ctrl_pagedown", enabled=self.content_overflows)
        return lane

    def render_body(self, width: int) -> list[str]:
        """Render the page a block at a time, noting where its section headings land."""
        lines: list[str] = []
        self._sticky_headers = []
        for block in self._doc.blocks:
            # A blank separator renders to no lines at all on its own (it is one empty
            # Text, and an empty render has nothing to split); inside the document's
            # Group it would be the empty line it is meant to be, so it stays one here.
            rendered = render_lines(block.renderable, width) or [""]
            if block.heading:
                self._sticky_headers.append((len(lines), rendered))
            lines.extend(rendered)
        self._scroll_total = max(1, len(lines))
        return lines


async def open_about_page(ctx: AppContext, title: str, doc: MarkdownDoc) -> None:
    """Open one About page full-screen and hold it until the reader backs out.

    Args:
        ctx: The shared application context (must be running the interactive TUI).
        title: The page's heading.
        doc: The page's content.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the About pages are only available in the menu")
    await ctx.ui.session.run_screen(AboutPage(title, doc))


__all__ = [
    "AboutPage",
    "about_author",
    "about_meshterm",
    "join_discord",
    "open_about_page",
    "support_project",
]
