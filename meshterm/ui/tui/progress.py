# SPDX-License-Identifier: Apache-2.0
"""A progress screen whose handle copies the part of ``rich.progress`` that the tools use.

In CLI mode, the trace sweep and the TX-optimize sweep show their progress with
``add_task`` / ``advance`` / ``update`` on a Rich ``Progress``. In the TUI, the same calls
change a progress dialog instead. The dialog has a limited size and is at the centre of
the screen. Thus the tool code is the same for the two front ends (refer to
:meth:`meshterm.ui.surface.PlainUi.progress` and
:meth:`~meshterm.ui.surface.TuiUi.progress`).
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
    """A non-interactive dialog that shows one or more progress bars of work that runs."""

    footer_hint = "working…"
    floating = True
    modal = True  # the work owns the keyboard. ^W must not unwind the stack while it runs.

    @property
    def picocalc_lyra_lane(self):
        """No lane: the dialog is non-interactive, so no keyboard key has an effect on it."""
        from .fkeys import EMPTY_LANE

        return EMPTY_LANE

    def __init__(self, title: str = "Working") -> None:
        """Start an empty progress dialog with the given heading."""
        super().__init__()
        self.title = title
        self._tasks: dict[int, _Task] = {}
        self._next = 0
        #: The working chip, one cell wide. An animation timer turns it (refer to
        #: :class:`TuiProgress`). Each row has this chip as its sign of activity. Thus a slow
        #: task (a trace that advances only one time, at the end) looks active, not frozen,
        #: while it waits. A static meter alone cannot show this.
        self.spinner = Spinner()

    def tick(self) -> None:
        """Advance the working chip by one animation step.

        The animation timer of the dialog calls this method.
        """
        self.spinner.tick()

    def add_task(self, description: str, total: float | None = None) -> int:
        """Add a task and return its id (the same as ``rich.progress.Progress.add_task``)."""
        task_id = self._next
        self._next += 1
        self._tasks[task_id] = _Task(description=description, total=total)
        return task_id

    def advance(self, task_id: int, amount: float = 1.0) -> None:
        """Increase the completed count of a task (the same as ``Progress.advance``)."""
        self._tasks[task_id].completed += amount

    def update(
        self,
        task_id: int,
        *,
        description: str | None = None,
        completed: float | None = None,
        total: float | None = None,
    ) -> None:
        """Change the fields of a task (the ``Progress.update`` arguments that the tools use)."""
        task = self._tasks[task_id]
        if description is not None:
            task.description = description
        if completed is not None:
            task.completed = completed
        if total is not None:
            task.total = total

    def render_body(self, width: int) -> list[str]:
        """Render each task as ``chip  description [meter] m/n``.

        Each row starts with the animated working chip (:attr:`spinner`), which is the
        one-cell indicator of the app. Thus the row shows that it is active, whatever the bar
        does. When the task completes, the chip changes to a green ``✓``.

        When the total is known, the bar is the braille
        :func:`~meshterm.ui.braillechart.meter` of the app, which is the only way that
        MeshTerm draws a proportion. It is a slim gauge that fills a visible ``track``, and
        it changes to the ``ok`` green when it is full.

        A task with no known total (there is nothing to divide by) shows no bar, because a
        track that never moves looks broken. Instead, the animated chip next to the current
        count shows the progress.
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
                bar = Text("")  # no total. The chip shows the activity, not a static track.
                counts = str(int(task.completed))
            chip = Text("✓", style="ok") if done else self.spinner.text("brand")
            table.add_row(
                chip, Text(task.description, style="accent"), bar, Text(counts, style="muted")
            )
        body = table if self._tasks else Text("starting…", style="muted")
        return render_lines(Group(body), width)

    def handle(self, action: str, data: str = "") -> None:
        """Ignore all keyboard keys: the progress advances with the work, not the keyboard."""
        return


@dataclass
class TuiProgress:
    """A context manager that yields a :class:`ProgressScreen` handle and keeps it pushed.

    It has the same ``with ... as progress:`` form as ``make_progress``. Thus a tool must
    change only the factory. On entry, the context manager pushes the dialog and starts an
    animation timer that keeps the working chip in motion. The ``add_task`` / ``advance`` /
    ``update`` methods change the dialog and request a paint. On exit, the context manager
    cancels the timer and pops the dialog.
    """

    session: TuiSession
    title: str = "Working"
    interval: float = 0.1
    _screen: ProgressScreen = field(init=False, default=None)  # type: ignore[assignment]
    _ticker: asyncio.Task[None] | None = field(init=False, default=None)

    def __enter__(self) -> ProgressScreen:
        """Push the progress dialog and start its animation timer."""
        self._screen = ProgressScreen(self.title)
        # Wrap add_task/advance/update, so that each call requests a paint of the session.
        for name in ("add_task", "advance", "update"):
            setattr(self._screen, name, self._wrap(getattr(self._screen, name)))
        self.session.push(self._screen)
        # The task changes the dialog only when it advances, and the advances can be seconds
        # apart (a trace advances only one time, at the end). Without a timer, the chip stays
        # frozen between the advances and looks broken. Thus a timer turns the chip for the
        # full life of the dialog, in the same way as the busy splash.
        self._ticker = asyncio.ensure_future(self._animate())
        return self._screen

    def __exit__(self, *exc: object) -> None:
        """Stop the animation timer and pop the progress dialog."""
        if self._ticker is not None:
            self._ticker.cancel()
            self._ticker = None
        self.session.pop(self._screen)

    async def _animate(self) -> None:
        """Advance the working chip and request a paint, each :attr:`interval` seconds."""
        try:
            while True:
                await asyncio.sleep(self.interval)
                self._screen.tick()
                self.session.invalidate()
        except asyncio.CancelledError:
            # Catch the cancellation and ignore it, so that the task ends cleanly on its last
            # loop cycle. If the loop destroys the task while it is pending, the loop shows a
            # warning, and the warning corrupts the screen.
            pass

    def _wrap(self, method):  # type: ignore[no-untyped-def]
        """Wrap a method that changes the dialog, so that it requests a paint after it runs."""

        def wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
            result = method(*args, **kwargs)
            self.session.invalidate()
            return result

        return wrapped
