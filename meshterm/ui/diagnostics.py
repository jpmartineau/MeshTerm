# SPDX-License-Identifier: Apache-2.0
"""The Diagnostics page: the bug-report block, drawn as one of the app's written pages.

The page has the shape of prose, not of a screen: a title, a standfirst, sections, and a
list under each section. Thus it is markdown, and it goes through the renderer that the
About pages use (:mod:`meshterm.ui.markdown`). This is not a choice of presentation that
we make two times: the same document is the one that MeshTerm **saves**, and an issue
tracker renders markdown as it is written. Thus one source gives one appearance, on the
screen and in the issue.

Two things are most necessary for a long block, and a hand-built screen had neither of
them. A written page gets both:

- Its ``##`` headings are landmarks. Each heading pins to the top row while its section
  scrolls under it, and ``^PgUp``/``^PgDn`` step from one section to the next.
- The F-key lane of the PicoCalc gets ``Sect ↑``/``Sect ↓``, for the same reason that a
  grouped list gets them.

:class:`~meshterm.ui.about.AboutPage` already does all of this. Thus this page is that
page with one verb added.

The verb is **save** (:data:`SAVE_KEY`). The page shows it where the platform shows verbs:
in the footer hint on a desktop, and on the free F3 slot of the lane on the console. The
page never shows it in its own body, because the body belongs to the report. The save
answers in a dialog, because the answer is a path that the user may have to read
carefully, and because a write can fail. The page under the dialog does not change, and
it is still there when the dialog closes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from .about import AboutPage
from .markdown import render_markdown

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext
    from .tui.session import TuiSession

#: The keyboard key that writes the page to a file. It is a bare letter. A read-only page
#: can use a bare letter, because it has no filter and no field in which the letter can
#: have a different meaning.
SAVE_KEY = "s"

#: The action that the keyboard key and the chip on the lane both dispatch. Thus the chip
#: is not a second implementation of the keyboard key.
SAVE_ACTION = "save"


class DiagnosticsPage(AboutPage):
    """The written diagnostics page, plus the one verb that writes it to a file.

    All of the behaviour for reading comes from :class:`~meshterm.ui.about.AboutPage`: the
    pager, the sticky ``##`` headings, the section jumps, and the lane that dims when there
    is nothing to page. This class adds only the save, on the one slot that the About pages
    leave free.
    """

    def __init__(self, source: str, *, session: TuiSession, save_path: Path) -> None:
        """Show ``source`` as the page, and save it to ``save_path`` when the user asks.

        Args:
            source: The markdown document, from
                :func:`~meshterm.tools.diagnostics.markdown_source`.
            session: The session, for the dialog in which the save answers.
            save_path: The file to which :data:`SAVE_KEY` writes.
        """
        super().__init__("Diagnostics", render_markdown(source))
        self._source = source
        self._session = session
        self._save_path = save_path
        self._saving = False

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The keys of the read-only page, with the save between the pager and Esc.

        The hint has the standard sentence shape: navigation first, then the action, then
        Esc. The pager atom is present only while :attr:`~Screen.content_overflows` is
        true, as on each read-only page. Thus the line never names a keyboard key that does
        nothing.
        """
        scroll = "↑↓ PgUp/PgDn scroll · " if self.content_overflows else ""
        return f"{scroll}{SAVE_KEY} save · Esc back"

    @property
    def picocalc_lyra_lane(self):
        """The lane of the About page, with ``Save`` on the slot that it leaves free.

        F1/F2 are the section pair and F4/F5 are the pager. Thus F3 is the one free primary
        slot. On this platform, the lane is the only place that can show a bare-letter
        keyboard key. Thus the chip is necessary, and not only a decoration.
        """
        from .tui.fkeys import FPair

        lane = list(super().picocalc_lyra_lane)
        lane[2] = FPair("Save", SAVE_ACTION)
        return lane

    def handle(self, action: str, data: str = "") -> None:
        """Save on the keyboard key or the chip. Scroll, jump, or leave on all other actions."""
        if action == SAVE_ACTION or (action == "text" and data.lower() == SAVE_KEY):
            self._begin_save()
            return
        super().handle(action, data)

    def _begin_save(self) -> None:
        """Write the file and report the result in a dialog, outside the key handler.

        This method uses ``run_detached``, because the ``handle`` of a screen is
        synchronous and the dialog is a screen. Thus this method can only start the flow,
        and the task that it starts is outside the navigation chain (refer to
        :meth:`~meshterm.ui.tui.session.TuiSession.run_detached`). Only one save runs at a
        time, so a held keyboard key cannot put dialogs on top of each other.
        """
        if self._saving:
            return
        self._saving = True

        async def run() -> None:
            try:
                await self._session.message_dialog(self._write(), title="Diagnostics")
            finally:
                self._saving = False

        self._session.run_detached(run())

    def _write(self) -> Text:
        """Write the document, and return the result as the message of the dialog.

        The message is a :class:`~rich.text.Text` with real spans, not a markup string.
        The dialog draws the message as it is, and reads its spans to find the border tone
        (refer to ``session._message_border``). If markup stays as text, the dialog shows
        its tags, and the border of a failure has the colour of a success.

        Only the **mark** has a style: ``✓`` or ``✗``, the ok/err pair of the app. This
        mark is sufficient for the border to read the tone. The path itself stays in the
        body colour, where it is easy to read. That is the purpose of the mark.
        """
        from ..tools.diagnostics import write_markdown

        message = Text()
        try:
            written = write_markdown(self._source, self._save_path)
        except OSError as exc:
            # For example: a read-only home, a full disk, or a path that does not exist.
            # The page itself does not change, and the user can still read it. That is
            # the important fallback.
            message.append("✗ ", style="err")
            message.append(f"could not save to\n{self._save_path}\n\n")
            message.append(exc.strerror or str(exc), style="muted")
            return message
        message.append("✓ ", style="ok")
        message.append(f"saved to\n{written}")
        return message


async def open_diagnostics_page(ctx: AppContext, source: str, *, save_path: Path) -> None:
    """Open the Diagnostics page full-screen, and keep it until the user leaves it.

    Args:
        ctx: The shared application context. The interactive TUI must be in use.
        source: The markdown document to show and to save.
        save_path: The file to which the save key of the page writes.

    Raises:
        RuntimeError: If the call is outside the interactive menu (there is no
            full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - the menu-only caller guards this
        raise RuntimeError("the Diagnostics page is only available in the menu")
    session = ctx.ui.session
    await session.run_screen(DiagnosticsPage(source, session=session, save_path=save_path))


__all__ = ["SAVE_ACTION", "SAVE_KEY", "DiagnosticsPage", "open_diagnostics_page"]
