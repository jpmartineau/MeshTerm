# SPDX-License-Identifier: Apache-2.0
"""The admin login of the menu: one flow for each screen that logs in to a remote node.

Repeater admin, the TX sweep, and the neighbour fetch of the trace screen all log in to a
node before they send it a command. Each of them asks for a password when MeshTerm has
none, tells a refusal from a silence, and keeps the password that works. This module is
that flow, so that the screens cannot answer the same outcome in different words.

**A stock node never refuses a password.** The firmware of a repeater (and of a room
server) checks the password and, when it is wrong, sends nothing at all. The companion
sends ``LOGIN_FAILED`` only when a reply arrives that is not "OK". Thus a password that
the owner changed (for example, with a serial console) comes back as
:attr:`~meshterm.core.models.LoginResult.NO_REPLY`, the same as a node that is down. The
credential policy keeps the password after a silence, which is correct for a node that is
down (refer to :meth:`~meshterm.core.admin_store.AdminStore.record`). But before, the
flow then tried the same stale password again at each visit, with no prompt, and the user
had no way to type the new one (GitHub issue 28).

Thus, when a stored password gets no reply, the flow asks the user: ``Cancel``, or
``New password…``. The second button opens the password prompt. The new password
replaces the stored one only when the node accepts it. Thus a node that is down keeps its
password, and a node whose password changed gets the new one in one step.

The CLI cannot ask. Its error for the same silence says how to give the new password
(:func:`no_reply_error`).

A third cause of the same silence is a stale route (GitHub issue 34). The companion keeps
one route for each node and replaces it only when a path return arrives. Nothing makes it
forget a dead route, so each login on that route is lost with no error. When the node has
a route, the dialog of the silence also offers ``Try by flood``. The button forgets the
route (nothing is transmitted), and then the one next login floods. The user chooses this
step, and the flow never floods by itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from rich.text import Text

if TYPE_CHECKING:
    from ..context import AppContext
    from ..core.connection import Device
    from ..core.models import Contact

#: The title of each dialog of the login.
TITLE = "Admin login"

#: The value of the ``Try by flood`` button of :func:`offer_no_reply`.
RETRY_FLOOD = "flood"


async def ask_password(ctx: AppContext, node: Contact, *, why: str) -> str | None:
    """Ask for the admin password of ``node`` in a floating prompt.

    The prompt floats also when it is the only screen on the stack (the picker dialog
    closed before the login started). Then it is a box over a blank base, not a full-frame
    prompt.

    Args:
        ctx: The shared application context (the interactive menu must be running).
        node: The node that the password is for.
        why: One line under the title that says why the node needs a login.

    Returns:
        The typed password, or ``None`` (or ``""``) when the user cancels.
    """
    return await ctx.ui.session.text(
        f"Admin password for {node.name}",
        prompt=why,
        password=True,
        floating=True,
    )


def refused_text(node: Contact, *, saved: bool, again: str) -> str:
    """The message for a login that the node refused.

    Stock firmware never refuses (refer to the module docstring), but other firmware can.
    A refusal is the one result that proves the password wrong, and the credential policy
    forgets a stored password for it.

    Args:
        node: The node that refused.
        saved: The password came from the admin store, so the store forgot it.
        again: The words for a retry on the screen of the caller (``"try again"``,
            ``"sweep again"``).
    """
    retry = f"{again} to enter a new one."
    if saved:
        return (
            f"{node.name!r} rejected the admin login (wrong password?). "
            f"The saved password was cleared; {retry}"
        )
    return f"{node.name!r} rejected the admin login (wrong password?). {retry.capitalize()}"


def no_reply_text(node: Contact, *, again: str) -> str:
    """The message for a typed password that got no reply.

    MeshTerm stored nothing, so nothing changes in the store. The message says that the
    password can be the cause, because a stock node does not answer a wrong password.

    Args:
        node: The node that did not answer.
        again: The words for a retry on the screen of the caller.
    """
    return (
        f"No reply from {node.name} — it may be out of reach, asleep, or busy, or the "
        f"password is wrong: a node does not answer a wrong password. "
        f"{again.capitalize()} to enter it again."
    )


def no_reply_error(name: str, *, saved: bool, again: str) -> str:
    """The error of a CLI command whose admin login got no reply.

    A scripted run cannot offer a new password, so the error says how to give one. A
    password that the node accepts replaces the stored one, so ``--password`` with the new
    password repairs a stale password in one run.

    Args:
        name: The name of the node.
        saved: The password came from the admin store. Then the store kept it, and a
            changed password is a probable cause.
        again: The words for a new run (``"run again"``, ``"re-run"``).
    """
    lead = f"{name!r} did not answer the admin login — it may be out of reach, asleep, or busy"
    if saved:
        return (
            f"{lead}, or its password changed: a node does not answer a wrong password. "
            f"The saved password was kept; if the password changed, {again} with --password."
        )
    return (
        f"{lead}, or the password is wrong: a node does not answer a wrong password. "
        "Any saved password was kept."
    )


async def offer_new_password(ctx: AppContext, node: Contact) -> bool:
    """Ask what to do after a stored password got no reply: stop, or type a new password.

    The silence has two causes that MeshTerm cannot tell apart: the node is down, or its
    password changed. Only the user knows which. ``New password…`` is the default, because
    the user who opened the flow wants to log in, and the stored password stays until the
    new one works.

    Args:
        ctx: The shared application context (the interactive menu must be running).
        node: The node that did not answer.

    Returns:
        ``True`` when the user wants to type a new password.
    """
    return bool(
        await ctx.ui.dialog(
            f"No reply from {node.name} — it may be out of reach, asleep, or busy. Or its "
            "admin password changed: a node does not answer a wrong password. The saved "
            "password is kept until a new one works.",
            [("Cancel", False), ("New password…", True)],
            title=TITLE,
            default=1,
        )
    )


async def forget_route(ctx: AppContext, device: Device, node: Contact) -> None:
    """Forget the route that the radio learned to ``node``, so that its next login floods.

    This is a local action on the companion. Nothing goes on the air. The route in the
    contact cache is not valid after it, so the function also makes the cache read the
    contacts again. The login that follows is the one transmission.

    Args:
        ctx: The shared application context.
        device: The connected device.
        node: The node whose route to forget.
    """
    await device.reset_route(node)
    devstate = getattr(ctx, "devstate", None)
    if devstate is not None:
        devstate.invalidate_contacts()


async def offer_no_reply(ctx: AppContext, node: Contact, *, saved: bool) -> object:
    """Ask what to do after a login got no reply from a node that has a learned route.

    The silence has three causes that MeshTerm cannot tell apart: the node is down, its
    password changed (or the typed password is wrong), or the route is stale. A flood costs
    the least to test, because it needs no typing. Thus ``Try by flood`` is the default. It
    is the one next login, and the user chooses it. The dialog has a maximum of three
    buttons, so the row fits the dialog of the handhelds.

    Args:
        ctx: The shared application context (the interactive menu must be running).
        node: The node that did not answer. It must have a route (``route_hops`` is not
            ``None``).
        saved: The password came from the admin store. Then the dialog also offers a new
            password.

    Returns:
        :data:`RETRY_FLOOD` for ``Try by flood``, ``True`` for ``New password…``, and a
        false value to stop.
    """
    stale = (
        "The login went along a route that your radio learned, and a route goes stale "
        "when the mesh changes. A flood goes around it."
    )
    if saved:
        text = (
            f"No reply from {node.name} — it may be out of reach, asleep, or busy. Or its "
            "admin password changed: a node does not answer a wrong password. The saved "
            f"password is kept until a new one works. {stale}"
        )
        buttons: list[tuple[str, object]] = [
            ("Cancel", None),
            ("New password…", True),
            ("Try by flood", RETRY_FLOOD),
        ]
        default = 2
    else:
        text = (
            f"No reply from {node.name} — it may be out of reach, or the password is "
            f"wrong: a node does not answer a wrong password. {stale}"
        )
        buttons = [("Cancel", None), ("Try by flood", RETRY_FLOOD)]
        default = 1
    return await ctx.ui.dialog(Text(text, style="warn"), buttons, title=TITLE, default=default)


async def log_in(
    ctx: AppContext,
    device: Device,
    node: Contact,
    *,
    why: str,
    again: str,
    on_record: Callable[[], None] | None = None,
) -> bool:
    """Log in to ``node``: with the stored password silently, or else with a prompt.

    The flow:

    1. With no stored password, ask for one (:func:`ask_password`).
    2. Log in under the busy card, and give the outcome to the credential policy
       (:meth:`~meshterm.core.admin_store.AdminStore.record`). The policy stores a password
       that works, forgets a refused one, and keeps a stored one after a silence.
    3. After a refusal, or after a silence for a typed password, show why, and stop.
    4. After a silence for a stored password, offer a new password
       (:func:`offer_new_password`). If the user accepts, go back to step 1.
    5. After a silence from a node that has a learned route, the dialog of step 3 or step 4
       also offers ``Try by flood`` (:func:`offer_no_reply`). If the user accepts, the flow
       forgets the route (:func:`forget_route`) and logs in again with the same password.
       The flow offers this step one time only. A flood that also gets no reply ends in
       step 3 or step 4 as before.

    Args:
        ctx: The shared application context (the interactive menu must be running).
        device: The connected device.
        node: The node to log in to.
        why: The line of the password prompt that says why the node needs a login.
        again: The words for a retry on the screen of the caller (``"try again"``).
        on_record: Called after each change to the admin store, before a dialog opens.
            Thus a screen that shows the state of the stored password (the TX sweep) is
            correct behind the dialog.

    Returns:
        ``True`` when the node accepted the login.
    """
    from ..core.models import LoginResult

    session = ctx.ui.session
    password = ctx.admin_store.get(node)
    saved = password is not None
    flooded = False
    while True:
        if password is None:
            password = await ask_password(ctx, node, why=why)
            if not password:
                return False
        async with ctx.ui.busy_overlay():
            outcome = await device.admin_login(node, password)
        ctx.admin_store.record(node, password, outcome)
        if on_record is not None:
            on_record()
        if outcome:
            return True
        if outcome is LoginResult.REFUSED:
            await session.message_dialog(
                Text(refused_text(node, saved=saved, again=again), style="err"), title=TITLE
            )
            return False
        if node.route_hops is not None and not flooded:
            choice = await offer_no_reply(ctx, node, saved=saved)
            if choice == RETRY_FLOOD:
                await forget_route(ctx, device, node)
                flooded = True
                continue
            if not choice:
                return False
            password, saved = None, False
            continue
        if not saved:
            await session.message_dialog(
                Text(no_reply_text(node, again=again), style="warn"), title=TITLE
            )
            return False
        if not await offer_new_password(ctx, node):
            return False
        password, saved = None, False
