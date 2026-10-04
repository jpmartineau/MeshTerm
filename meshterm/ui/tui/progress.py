# SPDX-License-Identifier: Apache-2.0
"""A progress screen whose handle mimics the slice of ``rich.progress`` the tools use.

Trace and TX-optimize sweeps drive progress with ``add_task`` / ``advance`` / ``update`` on a
Rich ``Progress`` in CLI mode. In the TUI the same calls update a bounded, centered progress
dialog instead, so tool code stays identical across both front-ends (see
:meth:`meshterm.ui.surface.PlainUi.progress` vs :meth:`~meshterm.ui.surface.TuiUi.progress`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rich.console import Group
from rich.table import Table
from rich.text import Text

from ..braillechart import meter
from .render import render_lines
from .screen import Screen
from .spinner import Spinner

if TYPE_CHECKING:
    from .session import TuiSession


@dataclass
class _Task:
    """One tracked progress task."""

    description: str
    total: float | None
    completed: float = 0.0


class ProgressScreen(Screen):
    """A non-interactive dialog showing one or more running progress bars."""

    footer_hint = "working…"
    floating = True
    modal = True  # work in flight owns the keyboard; ^W must not unwind out from under it

    @property
    def picocalc_lane(self):
        """No lane: the dialog is non-interactive, so no key does anything to it."""
        from .fkeys import EMPTY_LANE

        return EMPTY_LANE

    def __init__(self, title: str = "Working") -> None:
        """Start an empty progress dialog with the given heading."""
        super().__init__()
        self.title = title
        self._tasks: dict[int, _Task] = {}
        self._next = 0
        #: The one-cell working chip, spun by an animation timer (see :class:`TuiProgress`).
        #: It is the liveness signal every row carries so a slow task — a trace that only
        #: advances once, at the very end — reads as *working* rather than frozen while it
        #: waits, which a static meter alone cannot show.
        self.spinner = Spinner()

    def tick(self) -> None:
        """Advance the working chip one frame (called from the dialog's animation timer)."""
        self.spinner.tick()

    def add_task(self, description: str, total: float | None = None) -> int:
        """Add a task and return its id (mirrors ``rich.progress.Progress.add_task``)."""
        task_id = self._next
        self._next += 1
        self._tasks[task_id] = _Task(description=description, total=total)
        return task_id

    def advance(self, task_id: int, amount: float = 1.0) -> None:
        """Advance a task's completed count (mirrors ``Progress.advance``)."""
        self._tasks[task_id].completed += amount

    def update(
        self,
        task_id: int,
        *,
        description: str | None = None,
        completed: float | None = None,
        total: float | None = None,
    ) -> None:
        """Update a task's fields (mirrors the ``Progress.update`` kwargs used)."""
        task = self._tasks[task_id]
        if description is not None:
            task.description = description
        if completed is not None:
            task.completed = completed
        if total is not None:
            task.total = total

    def render_body(self, width: int) -> list[str]:
        """Render each task as ``chip  description [meter] m/n``.

        Every row leads with the animated working :attr:`spinner` chip — the app's one-cell
        indicator — so the row is visibly alive whatever the bar is doing; it flips to a
        green ``✓`` once the task completes. When a total is known, the bar is the app's
        braille :func:`~meshterm.ui.braillechart.meter` — the one way MeshTerm draws a
        proportion — a slim gauge filling a visible ``track`` and turning ``ok`` green at
        full. A task with no known total (nothing to divide by) shows no bar at all — a dead,
        never-moving track reads as broken — and leans on the spinning chip beside its running
        count to signal progress.
        """
        bar_width = max(10, min(40, width - 26))
        table = Table.grid(padding=(0, 1))
        table.add_column()  # working chip
        table.add_column()  # description
        table.add_column()  # meter
        table.add_column(justify="right")  # counts
        for task in self._tasks.values():
            if task.total:
                done = task.completed >= task.total
                bar = meter(
                    task.completed / task.total,
                    bar_width,
                    style="ok" if done else "brand",
                    slim=True,
                    track="track",
                )
                counts = f"{int(task.completed)}/{int(task.total)}"
            else:
                done = False
                bar = Text("")  # indeterminate: the chip carries the liveness, not a dead track
                counts = str(int(task.completed))
            chip = Text("✓", style="ok") if done else self.spinner.text("brand")
            table.add_row(
                chip, Text(task.description, style="accent"), bar, Text(counts, style="muted")
            )
        body = table if self._tasks else Text("starting…", style="muted")
        return render_lines(Group(body), width)

    def handle(self, action: str, data: str = "") -> None:
        """Swallow all keys: progress advances with the work, not the keyboard."""
        return


@dataclass
class TuiProgress:
    """Context manager yielding a :class:`ProgressScreen` handle, pushed for its lifetime.

    Presents the same ``with ... as progress:`` shape as ``make_progress`` so tools need
    only swap the factory. Entering pushes the dialog and starts an animation timer that
    keeps its working chip spinning; the ``add_task`` / ``advance`` / ``update`` methods
    mutate it and repaint; exiting cancels the timer and pops it.
    """

    session: TuiSession
    title: str = "Working"
    interval: float = 0.1
    _screen: ProgressScreen = field(init=False, default=None)  # type: ignore[assignment]
    _ticker: asyncio.Task[None] | None = field(init=False, default=None)

    def __enter__(self) -> ProgressScreen:
        """Push the progress dialog and start its animation timer."""
        self._screen = ProgressScreen(self.title)
        # Wrap add_task/advance/update so each repaints the session automatically.
        for name in ("add_task", "advance", "update"):
            setattr(self._screen, name, self._wrap(getattr(self._screen, name)))
        self.session.push(self._screen)
        # The task mutates the dialog only when it advances, which can be seconds apart (a
        # trace advances just once, at the end). Without a heartbeat the chip would sit frozen
        # between those beats and read as broken, so spin it on a timer, exactly as the busy
        # splash does, for the dialog's whole lifetime.
        self._ticker = asyncio.ensure_future(self._animate())
        return self._screen

    def __exit__(self, *exc: object) -> None:
        """Stop the animation timer and pop the progress dialog."""
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        self.session.pop(self._screen)

    async def _animate(self) -> None:
        """Advance the dialog's working chip and repaint every :attr:`interval` seconds."""
        try:
            while True:
                await asyncio.sleep(self.interval)
                self._screen.tick()
                self.session.invalidate()
        except asyncio.CancelledError:
            # Swallow the cancellation so the task finishes cleanly on its final loop cycle
            # rather than being torn down while pending (a screen-corrupting loop warning).
            pass

    def _wrap(self, method):  # type: ignore[no-untyped-def]
        """Wrap a mutating method so it repaints the session after running."""

        def wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
            result = method(*args, **kwargs)
            self.session.invalidate()
            return result

        return wrapped
