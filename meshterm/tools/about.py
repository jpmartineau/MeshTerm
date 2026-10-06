# SPDX-License-Identifier: Apache-2.0
"""The About MeshTerm tools: the four written pages in the last section of the menu.

The pages are "About MeshTerm", "About the author", "Join Discord", and "Support
MeshTerm", in that order. A new user asks the questions in the same order: what is this,
who made it, where are the other users, and how can I help. The pages share the "This
app" section with Preferences, which is first in it. The section belongs to the app
itself, and the only row that changes MeshTerm is above the four rows that describe it.
Each item is a page, not a feature. It reads nothing, it transmits nothing, and it does
not use a device. Thus it opens directly to its screen (refer to :mod:`meshterm.ui.about`),
and it does not ask for anything first.

Each page also has a CLI face (``meshterm about``, ``meshterm about-author``,
``meshterm discord``, ``meshterm support``) that prints the same page to the terminal.
Thus a user can get the answers from a shell without the full-screen session.

The pages are written in markdown in ``meshterm/assets/pages`` (refer to
:mod:`meshterm.ui.about`). Nothing in this module knows the content of a page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..context import AppContext
from .base import Tool, ToolResult, register

if TYPE_CHECKING:
    from ..ui.markdown import MarkdownDoc
    from ..ui.report import Facts


class _AboutTool(Tool):
    """The shared behaviour of the four About pages: open the screen, or print the page.

    The pages are different only in their title, icon, and content. Thus all the other
    parts (the screen that opens only in the menu, the CLI face that prints, the run row)
    are in this class, one time. A subclass gives :meth:`page`, and nothing more.
    """

    category = "This app"

    @staticmethod
    def page() -> MarkdownDoc:
        """Build the content of this page.

        Each page overrides this method. A subclass imports its builder in the override,
        not at module scope. The reason is that the registry imports each tool module at
        startup (refer to :func:`~meshterm.tools.load_all_tools`), but the content of a
        page is necessary only when a user opens it.
        """
        raise NotImplementedError

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Open the page. The return value ``None`` completes the run with no run row.

        The page transmits nothing and stores nothing. Thus, as with the Trophy case and
        the mesh walk, it opens here, not in :meth:`run`, and it logs no run of its own.

        Args:
            ctx: The shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.about import open_about_page

        await open_about_page(ctx, self.title, self.page())
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Give the page. Only the CLI comes to this method (the menu opens the screen).

        The page has no QR codes here. A QR code is a second rendering of a link that the
        page already prints. It is drawn for a phone that points at a screen. In a
        redirected file, it is only a mass of block characters, with no new information.

        Args:
            ctx: The shared application context.
            params: Not used, except for the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` that names the page that was shown.
        """
        return ToolResult(
            summary={"page": self.name},
            report=(_written_page(self.name, self.title, self.page().for_script()),),
        )


@register
class AboutMeshTermTool(_AboutTool):
    """What MeshTerm is, where it came from, and the terms under which it is distributed."""

    name = "about"
    title = "About MeshTerm"
    icon = "📖"
    help = "What this is, and the terms it ships under"
    order = 10  # the first question of a new user

    @staticmethod
    def page() -> MarkdownDoc:
        """The *About MeshTerm* page."""
        from ..ui.about import about_meshterm

        return about_meshterm()


@register
class AboutAuthorTool(_AboutTool):
    """The person who made MeshTerm, on the mesh and off the mesh."""

    name = "about-author"
    title = "About the author"
    icon = "👤"
    help = "The person behind MeshTerm, on and off the mesh"
    order = 20  # who made the app that the previous page describes

    @staticmethod
    def page() -> MarkdownDoc:
        """The *About the author* page."""
        from ..ui.about import about_author

        return about_author()


@register
class JoinDiscordTool(_AboutTool):
    """The community server: one invite link, and its QR code for a phone to read."""

    name = "discord"
    title = "Join Discord"
    icon = "🔗"
    help = "The invite link to the MeshTerm community server"
    order = 25  # where the other users are, after the user knows what this is and who made it

    @staticmethod
    def page() -> MarkdownDoc:
        """The *Join Discord* page."""
        from ..ui.about import join_discord

        return join_discord()


@register
class SupportProjectTool(_AboutTool):
    """What MeshTerm needs to continue, and the paid and unpaid ways to help it."""

    name = "support"
    title = "Support MeshTerm"
    icon = "💰"
    help = "What keeps MeshTerm going, and how to help"
    order = 30  # the request for help, only after the pages before it earned it

    @staticmethod
    def page() -> MarkdownDoc:
        """The *Support MeshTerm* page."""
        from ..ui.about import support_project

        return support_project()


def _written_page(name: str, title: str, doc: MarkdownDoc) -> Facts:
    """One prose page, given one time for both faces.

    The page is **wrapped**. This is the only exception, on purpose, to the "no line
    wrapping" rule. The rule makes sure that a record is never divided across two lines,
    with its fields under the wrong headings. A paragraph has no records and no headings.
    Without a wrap, these four pages print as lines of six hundred cells, and in a
    terminal, nobody can read such a line. Thus the page is drawn on a console that is
    :data:`~meshterm.ui.script.PAGE_WIDTH` cells wide. This is a fixed width, not the width
    of the terminal. Thus the same paragraph looks the same in a pipe, in a redirected
    file, or in the ``text`` of the document.

    ``links`` is the only part of a prose page that a program can use, and the only part
    that the menu draws as a QR code. ``text`` is the plain rendering of the page, not the
    markdown changed into nested JSON. A structured markdown tree is a second rendering,
    not data, and no program uses one.
    """
    from .. import __version__
    from ..ui import fields
    from ..ui.report import BARE, Column, Facts

    text = _rendered_page(doc)
    return Facts(
        key="page",
        fields=(
            fields.word("page", "page"),
            fields.word("title", "title"),
            fields.word("version", "version"),
            fields.word("text", "text"),
            Column(key="links"),
        ),
        values={
            "page": name,
            "title": title,
            # A copy of the package value, on purpose. The facts of a page are part of its
            # content, and this is the version that its `{version}` placeholder became.
            "version": __version__,
            "text": text,
            "links": _links(text),
        },
        shape=BARE,
        bare="text",
    )


def _rendered_page(doc: MarkdownDoc) -> str:
    """The page as plain text, wrapped at the fixed page width."""
    import io

    from rich.console import Console

    from ..ui import script

    buffer = io.StringIO()
    Console(
        file=buffer,
        width=script.PAGE_WIDTH,
        color_system=None,
        highlight=False,
        emoji=False,
        markup=False,
    ).print(doc)
    return "\n".join(line.rstrip() for line in buffer.getvalue().splitlines()).strip("\n")


def _links(text: str) -> list[str]:
    """All the URLs on the page, in document order, and each URL one time.

    The function reads the URLs from the rendered page, not from the markdown source.
    Thus a reference link and an inline link both count one time, and both are the text
    that the user sees. Punctuation at the end is not part of a URL, so the function
    removes it.
    """
    import re

    seen: list[str] = []
    for match in re.findall(r"https?://[^\s<>()\[\]]+", text):
        url = match.rstrip(".,;:*_")
        if url not in seen:
            seen.append(url)
    return seen
