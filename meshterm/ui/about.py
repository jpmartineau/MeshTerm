# SPDX-License-Identifier: Apache-2.0
"""The About MeshTerm pages: the app, the person who wrote it, and how to help it.

These are four read-only pages under the *About MeshTerm* section of the main menu. This is
the one section that names a subject and not an action. The other five sections cannot
answer the question "what is this thing, and who made it". Each page opens as a full
screen, scrolls if the terminal is short, and the user leaves it with Esc. Nothing here
touches the radio or the database. Thus a page reads the same when no device is attached.

Unlike each other screen in the app, these pages are **written** and not composed. Their
content is markdown. It is in ``meshterm/assets/pages``, beside the wordmark.
:mod:`meshterm.ui.markdown` draws it in the own visual language of the app (refer to that
module for what each construct becomes). To fill in a page, edit its ``.md`` file. No
screen, menu row, or CLI face must follow. Everything that markdown offers is available:
sections, sub-headings, lists, quotes, links, and tables.

All four pages are written. The ``##`` headings are the shape into which each page was
written, and they have a function that the eye does not see. The screen records where each
heading lands while it renders. Thus it can pin the heading that the user is under, and the
section jump can step from one heading to the next. A page is navigable in the same way as
a grouped list.

Three items are *not* written into those files: the version, the author, and the copyright
span. Each page names them with a ``{version}``, ``{author}``, or ``{copyright}``
placeholder. The code fills the placeholder from :mod:`meshterm` itself
(:func:`~meshterm.copyright_notice`) when the page opens. Thus the About page can never
drift from the package that it describes.
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

#: The place of the written pages: beside the wordmark, in the assets of the package. Thus
#: they ship with the wheel, and a person can edit them without a change to Python code.
_PAGES = Path(__file__).resolve().parent.parent / "assets" / "pages"


def _page(name: str) -> MarkdownDoc:
    """Load ``assets/pages/<name>.md``, fill in the live package facts, and render it.

    Args:
        name: The stem of the page file.

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
    """The *About MeshTerm* page: what the app is, where it came from, and its terms."""
    return _page("about")


def about_author() -> MarkdownDoc:
    """The *About the author* page: the person who made MeshTerm, on the mesh and off it."""
    return _page("author")


def join_discord() -> MarkdownDoc:
    """The *Join Discord* page: the invite link, and a QR code of it for a phone camera.

    This is a page and not a dialog, because the user has nothing to decide. It is a link.
    On a headless console, the QR code is the way that the link leaves the screen.
    """
    return _page("discord")


def support_project() -> MarkdownDoc:
    """The *Support MeshTerm* page: what keeps it going, and how to help.

    The page has two sides, on purpose. Money is one way to help, but it is not the only
    way. Thus the page has a section for the other kind of help, and it does not put that
    help into a donate link.
    """
    return _page("support")


class AboutPage(ScrollScreen):
    """One written page: a read-only body, drawn full-screen, that the user leaves with Esc.

    Most of it is the shared read-only screen. This includes the pager, the footer hint
    that it derives (it names the pager only when there is something to page), and the
    PicoCalc F-key lane that dims on the same condition. Two things are added. Both come
    from the fact that the body is a *document* and not a block:

    * It renders block by block, and it records where each ``##`` section starts. Thus a
      heading pins to the top row while its prose scrolls under it, and ``^PgUp`` and
      ``^PgDn`` step from section to section (the same landmark machinery that a grouped
      select list uses).
    * When the page has more than one section, its lane claims the left-hand pair
      ``Sect ↑`` and ``Sect ↓``. On the console, the lane is the only place where a chord
      can advertise itself.

    Only the framing is different from a result window. These pages are full screens and
    not floating views, so Esc reads *back*.
    """

    def __init__(self, title: str, doc: MarkdownDoc) -> None:
        """Show ``doc`` as the page under ``title``.

        Args:
            title: The heading of the page, in sentence case, with no icon. Icons are in
                the menu rows that open these pages, and never in a title.
            doc: The rendered page, from one of the builders above.
        """
        super().__init__(doc, title=title, floating=False)
        self._doc = doc

    @property
    def picocalc_lyra_lane(self):
        """The shared pager lane, with the section step on a page that has sections.

        A left-hand pair rises toward F1, so ``Sect ↑`` is outside ``Sect ↓``. A grouped
        select list uses this order on the same two keys, and it sends the same two actions.
        Both chips are lit only while the page scrolls. When the whole page is visible, there
        is no section to which to step.
        """
        from .tui.fkeys import FPair, default_lane

        lane = list(default_lane(nav=self.content_overflows))
        if len(self._doc.sections) >= 2:
            lane[0] = FPair("Sect ↑", "ctrl_pageup", enabled=self.content_overflows)
            lane[1] = FPair("Sect ↓", "ctrl_pagedown", enabled=self.content_overflows)
        return lane

    def render_body(self, width: int) -> list[str]:
        """Render the page one block at a time, and note where its section headings land."""
        lines: list[str] = []
        self._sticky_headers = []
        for block in self._doc.blocks:
            # A blank separator by itself renders to no lines. It is one empty Text, and an
            # empty render has nothing to split. Inside the Group of the document, it is the
            # empty line that it must be. Thus it stays one empty line here.
            rendered = render_lines(block.renderable, width) or [""]
            if block.heading:
                self._sticky_headers.append((len(lines), rendered))
            lines.extend(rendered)
        self._scroll_total = max(1, len(lines))
        return lines


async def open_about_page(ctx: AppContext, title: str, doc: MarkdownDoc) -> None:
    """Open one About page full-screen and hold it until the user goes back.

    Args:
        ctx: The shared application context (the interactive TUI must run).
        title: The heading of the page.
        doc: The content of the page.

    Raises:
        RuntimeError: If the caller is outside the interactive menu (no full-screen session).
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
