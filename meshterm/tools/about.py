# SPDX-License-Identifier: Apache-2.0
"""The About MeshTerm tools: the four written pages in the menu's last section.

*About MeshTerm*, *About the author*, *Join Discord*, *Support MeshTerm* — in that
order, which is the order a stranger asks the questions in: what is this, who made it,
where is everyone, how do I help. They share the *This app* section with Preferences,
which leads it: the section is the app's own, and the one row that *changes*
MeshTerm sits above the four that describe it.
Each is a page rather than a feature: it reads nothing, transmits nothing, and needs no
device, so it opens straight to its screen (see :mod:`meshterm.ui.about`) instead of
prompting for anything first.

Each also keeps a CLI face — ``meshterm about``, ``meshterm about-author``,
``meshterm discord``, ``meshterm support`` — printing the same page to the terminal, so
the answers are reachable from a shell without launching the full-screen session.

The pages themselves are written in markdown under ``meshterm/assets/pages`` (see
:mod:`meshterm.ui.about`); nothing here knows what any of them say.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..context import AppContext
from .base import Tool, ToolResult, register

if TYPE_CHECKING:
    from ..ui.markdown import MarkdownDoc
    from ..ui.report import Facts


class _AboutTool(Tool):
    """Shared behaviour for the four About pages: open the screen, or print the page.

    The pages differ only in their title, icon, and content, so everything else — the
    menu-only screen open, the printing CLI face, the run row — lives here once. A
    subclass supplies :meth:`page`, and nothing more.
    """

    category = "This app"

    @staticmethod
    def page() -> MarkdownDoc:
        """Build this page's content.

        Overridden by each page. Subclasses import their builder *inside* the override
        rather than at module scope: the registry imports every tool module at startup
        (see :func:`~meshterm.tools.load_all_tools`), and a page's content is only ever
        wanted once someone actually opens it.
        """
        raise NotImplementedError

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Open the page; returning ``None`` completes the invocation with no run row.

        The page transmits nothing and stores nothing, so — like the Trophy case and the
        mesh walk — it lives here rather than in :meth:`run`, and logs no run of its own.

        Args:
            ctx: Shared application context.

        Returns:
            Always ``None``.
        """
        from ..ui.about import open_about_page

        await open_about_page(ctx, self.title, self.page())
        return None

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """State the page — only reachable from the CLI (the menu opens the screen).

        Without its scannable codes: a QR is a second rendering of a link the page already
        prints, drawn for a phone pointed at a screen, and redirected into a file it is a
        block of block characters around nothing new.

        Args:
            ctx: Shared application context.
            params: Unused beyond the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` naming the page that was shown.
        """
        return ToolResult(
            summary={"page": self.name},
            report=(_written_page(self.name, self.title, self.page().for_script()),),
        )


@register
class AboutMeshTermTool(_AboutTool):
    """What MeshTerm is, where it came from, and the terms it ships under."""

    name = "about"
    title = "About MeshTerm"
    icon = "📖"
    help = "What this is, and the terms it ships under"
    order = 10  # the question a stranger asks first

    @staticmethod
    def page() -> MarkdownDoc:
        """The *About MeshTerm* page."""
        from ..ui.about import about_meshterm

        return about_meshterm()


@register
class AboutAuthorTool(_AboutTool):
    """The person behind MeshTerm, on the mesh and off it."""

    name = "about-author"
    title = "About the author"
    icon = "👤"
    help = "The person behind MeshTerm, on and off the mesh"
    order = 20  # who made the thing you just read about

    @staticmethod
    def page() -> MarkdownDoc:
        """The *About the author* page."""
        from ..ui.about import about_author

        return about_author()


@register
class JoinDiscordTool(_AboutTool):
    """The community server: one invite link, and a QR of it for a phone to read."""

    name = "discord"
    title = "Join Discord"
    icon = "🔗"
    help = "The invite link to the MeshTerm community server"
    order = 25  # where everyone else is, once you know what this is and who made it

    @staticmethod
    def page() -> MarkdownDoc:
        """The *Join Discord* page."""
        from ..ui.about import join_discord

        return join_discord()


@register
class SupportProjectTool(_AboutTool):
    """What keeps MeshTerm going, and the ways — paid and unpaid — to help it along."""

    name = "support"
    title = "Support MeshTerm"
    icon = "💰"
    help = "What keeps MeshTerm going, and how to help"
    order = 30  # the ask, and only once the pages before it have earned it

    @staticmethod
    def page() -> MarkdownDoc:
        """The *Support MeshTerm* page."""
        from ..ui.about import support_project

        return support_project()


def _written_page(name: str, title: str, doc: MarkdownDoc) -> Facts:
    """One prose page, stated once for both faces.

    **Wrapped**, and that is the one deliberate exception to "no line wrapping". The rule
    exists so a *record* is never split across two lines with its fields landing under the
    wrong headings; a paragraph has no records and no headings, and left unwrapped these
    four pages print as single six-hundred-cell lines no terminal can read. So the page is
    drawn on a console :data:`~meshterm.ui.script.PAGE_WIDTH` cells wide — a fixed width,
    not the terminal's, so the same paragraph reads the same piped, redirected, or shipped
    as the document's ``text``.

    ``links`` is the one machine-actionable thing on a page of prose, and the one thing the
    menu bothers to draw a QR of. ``text`` is the page's plain rendering rather than a fold
    of the markdown into nested JSON: a structured markdown tree would be a second
    rendering, not data, and nothing would consume it.
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
            # Duplicated from the package on purpose: a page's own facts are part of its
            # content, and this is the version its `{version}` placeholder resolved to.
            "version": __version__,
            "text": text,
            "links": _links(text),
        },
        shape=BARE,
        bare="text",
    )


def _rendered_page(doc: MarkdownDoc) -> str:
    """The page as plain text, folded to the fixed page width."""
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
    """Every URL the page carries, in document order, de-duplicated.

    Read off the rendered page rather than the markdown source, so a link written as a
    reference and a link written inline both count once and both read as what the reader
    sees. Trailing punctuation is not part of a URL and is trimmed.
    """
    import re

    seen: list[str] = []
    for match in re.findall(r"https?://[^\s<>()\[\]]+", text):
        url = match.rstrip(".,;:*_")
        if url not in seen:
            seen.append(url)
    return seen
