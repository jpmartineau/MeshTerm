# SPDX-License-Identifier: Apache-2.0
"""The Diagnostics page: the bug-report block, drawn as one of the app's written pages.

It is prose-shaped rather than screen-shaped — a title, a standfirst, sections, and a list
under each — so it is markdown, and it goes through the renderer the About pages already
use (:mod:`meshterm.ui.markdown`). That is not a presentational choice made twice: the same
document is what gets **saved**, and an issue tracker renders markdown as written. One
source, one look, on screen and in the issue.

Being a written page buys the two things a long block most needs and neither of which a
hand-built screen had: its ``##`` headings are landmarks, so each pins to the top row while
its own section scrolls under it and ``^PgUp``/``^PgDn`` step section by section; and the
PicoCalc's F-key lane picks up ``Sect ↑``/``Sect ↓`` for the same reason a grouped list
does. :class:`~meshterm.ui.about.AboutPage` already does all of it, so this is that page
with one verb added.

The verb is **save** (:data:`SAVE_KEY`), advertised where the platform advertises verbs —
in the footer hint on a desktop, on the free F3 slot of the lane on the console — and never
in the page's own body, which belongs to the report. It answers in a popup, because the
answer is a path the reader may need to read carefully and because a write can fail; the
page underneath is unchanged and still there when the popup goes.
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

#: The key that writes the page to a file. A bare letter, which a read-only page can
#: afford because it has no filter and no field for one to mean anything else in.
SAVE_KEY = "s"

#: The action both the key and the lane's chip dispatch, so the chip is not a second
#: implementation of the key.
SAVE_ACTION = "save"


class DiagnosticsPage(AboutPage):
    """The written diagnostics page, plus the one verb that writes it to a file.

    Everything about how it *reads* is :class:`~meshterm.ui.about.AboutPage`: the pager,
    the sticky ``##`` headings, the section jumps, the lane that dims when there is nothing
    to page. What is added is the save, on the one slot the About pages leave free.
    """

    def __init__(self, source: str, *, session: TuiSession, save_path: Path) -> None:
        """Show ``source`` as the page, saving to ``save_path`` on demand.

        Args:
            source: The markdown document, from
                :func:`~meshterm.tools.diagnostics.markdown_source`.
            session: The session, for the popup the save answers in.
            save_path: Where :data:`SAVE_KEY` writes.
        """
        super().__init__("Diagnostics", render_markdown(source))
        self._source = source
        self._session = session
        self._save_path = save_path
        self._saving = False

    @property
    def footer_hint(self) -> str:  # type: ignore[override]
        """The read-only page's keys, with the save between the pager and Esc.

        Navigation first, then the action, then Esc — the standard sentence shape. The
        pager atom comes and goes with :attr:`~Screen.content_overflows`, as it does on
        every read-only page, so the line never names a key that would do nothing.
        """
        scroll = "↑↓ PgUp/PgDn scroll · " if self.content_overflows else ""
        return f"{scroll}{SAVE_KEY} save · Esc back"

    @property
    def picocalc_lyra_lane(self):
        """The About page's lane, with ``Save`` on the slot it leaves free.

        F1/F2 are the section pair and F4/F5 the pager, so F3 is the one free primary slot
        — and on this platform the lane is the *only* place a bare letter can advertise
        itself at all, which is what makes the chip load-bearing rather than decorative.
        """
        from .tui.fkeys import FPair

        lane = list(super().picocalc_lyra_lane)
        lane[2] = FPair("Save", SAVE_ACTION)
        return lane

    def handle(self, action: str, data: str = "") -> None:
        """Save on the key or the chip; scroll, jump or leave on everything else."""
        if action == SAVE_ACTION or (action == "text" and data.lower() == SAVE_KEY):
            self._begin_save()
            return
        super().handle(action, data)

    def _begin_save(self) -> None:
        """Write the file and report it in a popup, off the key handler.

        ``run_detached`` because a screen's ``handle`` is synchronous and the popup is a
        screen: the flow can only be *started* here, and the task it starts sits outside
        the navigation chain (see :meth:`~meshterm.ui.tui.session.TuiSession.run_detached`).
        One save at a time, so a held key cannot stack popups over each other.
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
        """Write the document, and say what happened as the popup's message.

        A :class:`~rich.text.Text` with real spans rather than a markup string: the dialog
        draws the message as given and reads its *spans* to pick a border tone (see
        ``session._message_border``), so markup left as text would both print its own tags
        and leave a failure framed in the success colour.

        Only the **mark** is styled — ``✓`` or ``✗``, the app's own ok/err pair — which is
        enough for the border to read the tone and leaves the path itself in body ink,
        where it stays legible. That is what the mark is for.
        """
        from ..tools.diagnostics import write_markdown

        message = Text()
        try:
            written = write_markdown(self._source, self._save_path)
        except OSError as exc:
            # A read-only home, a full disk, a path that is not there. The page itself is
            # untouched and still readable, which is the fallback that matters.
            message.append("✗ ", style="err")
            message.append(f"could not save to\n{self._save_path}\n\n")
            message.append(exc.strerror or str(exc), style="muted")
            return message
        message.append("✓ ", style="ok")
        message.append(f"saved to\n{written}")
        return message


async def open_diagnostics_page(ctx: AppContext, source: str, *, save_path: Path) -> None:
    """Open the Diagnostics page full-screen and hold it until the reader backs out.

    Args:
        ctx: The shared application context (must be running the interactive TUI).
        source: The markdown document to show and to save.
        save_path: Where the page's save key writes.

    Raises:
        RuntimeError: If called outside the interactive menu (no full-screen session).
    """
    from .surface import TuiUi

    if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - guarded by the menu-only caller
        raise RuntimeError("the Diagnostics page is only available in the menu")
    session = ctx.ui.session
    await session.run_screen(DiagnosticsPage(source, session=session, save_path=save_path))


__all__ = ["SAVE_ACTION", "SAVE_KEY", "DiagnosticsPage", "open_diagnostics_page"]
