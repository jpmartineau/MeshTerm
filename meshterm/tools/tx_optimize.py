# SPDX-License-Identifier: Apache-2.0
"""The ``tx-optimize`` tool: tune the TX power of a remote node for the best signal at a target.

You force a path (the same as for ``trace``) that ends at the **target** node, where
MeshTerm measures the SNR. The node one hop *before* the target is the node whose transmit
power MeshTerm sweeps and tunes. You must have admin rights on that node. MeshTerm keeps
the admin passwords from one run to the next.

In the interactive menu, one dialog with two steps selects the link. First, it selects
the node to tune (the repeaters for which you have credentials come first in the list).
Then it selects the target whose reception MeshTerm optimizes. Then the live sweep screen
(:mod:`meshterm.ui.tx_screen`) continues. It is ready, but it does nothing yet:

- You adjust the route, the range, the step, and the samples on the screen.
- Nothing transmits until you start Sweep.
- The levels go into a bar chart as MeshTerm measures them.
- You decide whether to apply the result *after* the sweep, when you can see the
  evidence.

On the CLI, the tool stays a scriptable single run with the full set of flags
(``--path``, range, step, samples, ``--apply``), and it streams its progress as a trace
does.
"""

from __future__ import annotations

from typing import Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.connection import DeviceCommandError
from ..core.models import NODE_TYPE_LABELS, Contact, LoginResult, TxOptResult
from ..services import trace_runner, tx_optimizer
from ..ui.widgets import tx_opt_summary, tx_opt_table
from .base import Tool, ToolResult, register

#: The number of traces for each TX level. It stays below 10, so that the duty cycle of the
#: radio stays in safe limits and the sweep of each level ends in a reasonable time.
MAX_SAMPLES = 9


@register
class TxOptimizeTool(Tool):
    """Tune the TX power of a remote node for the strongest, most reliable signal at a target."""

    name = "tx-optimize"
    title = "TX optimize"
    icon = "📶"
    help = "Tune a remote node's TX power for a target"
    category = "Other nodes"
    order = 20  # the same as Repeater admin: a remote radio, changed over the mesh

    async def prompt_params(self, ctx: AppContext) -> dict[str, Any] | None:
        """Nothing to collect here. The user selects the link in :meth:`run`.

        The two selections are one dialog with steps, and the sweep screen opens
        immediately when that dialog closes. :meth:`_run_live` keeps them together. Thus the
        dialog rows and the resolver of the sweep share one read of the contacts (the only
        slow step before the screen opens).

        Args:
            ctx: The shared application context.

        Returns:
            ``{"live": True}``: the marker of the menu for the interactive path.
        """
        return {"live": True}

    async def execute(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Run, with no outer run row on the live path.

        The live screen opens its own ``runs`` row for the sweep (the same row that a
        scripted run stores). If another row goes around the screen session, the log has
        the sweep two times. Scripted runs keep the logging of the base class.

        Args:
            ctx: The shared application context.
            params: The parameters for this run.

        Returns:
            The :class:`ToolResult` from :meth:`run`.
        """
        if params.get("live"):
            return await self.run(ctx, params)
        return await super().execute(ctx, params)

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Open the live sweep (menu), or run the scripted optimization (CLI).

        Args:
            ctx: The shared application context.
            params: ``live`` from the menu (the user selects the two nodes in one dialog).
                Or, from the CLI: ``path``, ``samples``, ``tx_min``, ``tx_max``, ``step``,
                ``apply``, the optional ``password`` and ``viz``, and the injected
                ``_run_id``.

        Returns:
            A :class:`ToolResult` with the optimum and the chart path, if there is one.
        """
        if params.get("live"):
            return await self._run_live(ctx)
        return await self._run_cli(ctx, params)

    # -- interactive (menu) ---------------------------------------------------------

    async def _run_live(self, ctx: AppContext) -> ToolResult:
        """Select the link in one dialog with steps, then run the sweep screen over the menu.

        The two selections (first the node to tune, then the node at which to measure) are
        one floating list that turns its page (:func:`~meshterm.ui.menus.run_wizard`).
        Esc on the second step turns back to the first step, and the selected node stays
        highlighted. Esc on the first step leaves. The dialog closes before the sweep screen
        opens, so Esc from the sweep goes to the main menu. The link was a question before
        the screen, not a place to come back to. Before, there were two pickers, and each
        stayed pushed under the sweep as a hub. That put the sweep three frames deep, and
        put two dialogs on top of each other before it. They looked like two places, not
        one question in two parts.

        There is no login here. The screen logs in when the user starts the first Sweep.
        Thus, if the user leaves an idle screen, MeshTerm did nothing with the radio but
        read the contacts.

        Args:
            ctx: The shared application context.

        Returns:
            A :class:`ToolResult` that repeats the stored summary of the sweep.
        """
        from ..ui.admin_picker import admin_picker_rows
        from ..ui.menus import WizardPage, run_wizard
        from ..ui.surface import TuiUi
        from ..ui.tx_screen import open_tx_optimize

        if not isinstance(ctx.ui, TuiUi):  # pragma: no cover - only the menu calls this
            raise RuntimeError("the live TX sweep is only available in the menu")

        # Read through the session cache. This entry flow runs each time the tool opens, and
        # the contacts table is a slow read on a busy node (refer to
        # :class:`~meshterm.services.device_state.DeviceState`).
        contacts = await ctx.devstate.contacts()
        items, candidates = admin_picker_rows(ctx, contacts)
        if not candidates:
            ctx.ui.note("[err]no contacts with a key — receive an advert first[/err]")
            await ctx.ui.present(title=self.title)
            return ToolResult(summary={})

        def admin_of(values: list) -> Contact:
            return next(c for c in candidates if c.name == values[0])

        def node_page(values: list) -> WizardPage:
            return WizardPage(
                title="TX optimize — node to tune · step 1 of 2",
                items=items,
                prompt="Whose transmit power gets tuned (you need its admin password):",
                default=values[0],
            )

        def target_page(values: list) -> WizardPage | Any:
            admin = admin_of(values)
            targets = _target_items(contacts, admin)
            if not targets:
                # The tuned node is the only contact. Thus a typed hex key prefix is the only
                # way to name the target. Its prompt floats over the first page of the dialog.
                return ctx.ui.session.text(
                    "TX optimize — measure at · step 2 of 2",
                    prompt=f"Hex key prefix of the node that hears {admin.name}:",
                    default=values[1] or "",
                    floating=True,
                )
            return WizardPage(
                title="TX optimize — measure at · step 2 of 2",
                items=targets,
                prompt=f"The node whose reception of {admin.name} gets optimized:",
                default=values[1],
            )

        answers = await run_wizard(ctx.ui.session, [node_page, target_page])
        if answers is None:  # Esc on the first step: back to the menu
            return ToolResult(summary={})
        admin_node = admin_of(answers)
        target = str(answers[1]).strip()
        if not target:
            return ToolResult(summary={})

        target_contact = next((c for c in contacts if c.name == target), None)
        if target_contact is not None:
            target_label = target_contact.name
            target_hash = target_contact.public_key or target_contact.key_prefix
        else:
            target_label = target
            target_hash = target  # a typed hex prefix is its own hash

        summary = await open_tx_optimize(
            ctx,
            admin_node=admin_node,
            target_label=target_label,
            target_hash=target_hash,
        )
        return ToolResult(summary=summary)

    async def _login(self, ctx: AppContext, admin_node: Contact, params: dict[str, Any]) -> None:
        """Log in to the admin node, and keep a password that works.

        Args:
            ctx: The shared application context.
            admin_node: The node that we will tune.
            params: The tool parameters (on the CLI, they can hold an explicit
                ``password``).

        Raises:
            DeviceCommandError: If the node refused the login. MeshTerm then forgets the
                stored password, because it is known to be bad, and the next run asks
                again. Also if the node never answered. Then the password is not tested,
                and MeshTerm keeps it.
        """
        device = await ctx.device()
        password = await self._resolve_password(ctx, admin_node, params)
        outcome = await device.admin_login(admin_node, password)
        ctx.admin_store.record(admin_node, password, outcome)
        if outcome is LoginResult.REFUSED:
            raise DeviceCommandError(
                f"admin login to {admin_node.name!r} failed (wrong password?). "
                "The saved password was cleared; re-run to enter a new one."
            )
        if not outcome:
            raise DeviceCommandError(
                f"{admin_node.name!r} did not answer the admin login — it may be out of "
                "reach or asleep. The saved password was kept; re-run when it answers."
            )

    # -- scripted (CLI) ---------------------------------------------------------------

    async def _run_cli(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Resolve the link, log in, sweep the TX power, render the results, and apply the best.

        Args:
            ctx: The shared application context.
            params: ``path``, ``samples``, ``tx_min``, ``tx_max``, ``step``, ``apply``, the
                optional ``password`` and ``viz``, and the injected ``_run_id``.

        Returns:
            A :class:`ToolResult` with the optimum and the chart path, if there is one.
        """
        from ..ui.surface import TuiUi

        run_id = params["_run_id"]
        device = await ctx.device()
        contacts = await device.get_contacts()

        # Resolve the forced path to hashes. Then get the target (the last hop) and the admin
        # node that we tune (the hop before the target).
        path = trace_runner.parse_trace_path(params["path"], contacts)
        admin_node, target_label = _resolve_link(path, contacts)

        samples = max(1, min(MAX_SAMPLES, int(params.get("samples", 3))))
        if int(params.get("samples", 3)) > MAX_SAMPLES:
            ctx.ui.ack(f"[warn]capping at {MAX_SAMPLES} traces per level[/warn]")

        ctx.ui.ack(
            f"[muted]logging in to[/muted] [brand]{admin_node.name}[/brand] [muted]…[/muted]"
        )
        await self._login(ctx, admin_node, params)

        ctx.ui.ack(
            f"[muted]tuning[/muted] [brand]{admin_node.name}[/brand] "
            f"[muted]→ target[/muted] [brand]{target_label}[/brand]  "
            f"[muted]via {path}[/muted]"
        )

        with ctx.ui.progress("tx-optimize") as progress:
            task = progress.add_task(f"optimizing TX -> {target_label}", total=None)

            def on_level(done: int, total: int, level) -> None:  # noqa: ANN001
                progress.update(
                    task,
                    total=total,
                    completed=done,
                    description=f"TX {level.tx_power:>2}  "
                    f"SNR {_fmt_snr(level.target_snr)}  {level.success_rate:.0%}",
                )

            result = await tx_optimizer.optimize_tx_power(
                device,
                target_label,
                admin_node,
                path,
                tx_min=int(params.get("tx_min", ctx.preferences.tx_opt_min)),
                tx_max=int(params.get("tx_max", ctx.preferences.tx_opt_max)),
                coarse_step=int(params.get("step", 3)),
                samples_per_level=samples,
                apply=bool(params.get("apply", True)),
                cooldown_s=ctx.preferences.trace_cooldown_s,
                snr_tolerance=ctx.preferences.tx_snr_tolerance_db,
                on_level=on_level,
                persist_level=lambda lv: ctx.repo.record_tx_sample(run_id, lv),
                persist_trace=lambda t: ctx.repo.record_trace(run_id, t),
            )

        # No trace got through at any TX level. Thus nothing was tuned, and the node stays
        # at its original power. Usually the cause is a path that is wrong or that cannot be
        # reached, not a weak link.
        no_result = result.best_snr is None
        if result.applied:
            ctx.log.info("set TX power %s on %s", result.best_tx, admin_node.name)

        report = None
        if isinstance(ctx.ui, TuiUi):
            ctx.ui.show(tx_opt_table(result))
            ctx.ui.show(tx_opt_summary(result))
        else:
            report = _sweep_report(result, path, admin_node, contacts)

        if no_result:
            restored = (
                f" Restored TX to {result.original_tx}." if result.original_tx is not None else ""
            )
            message = (
                f"[warn]![/warn] no traces reached [brand]{result.target}[/brand] at any TX "
                f"level — check the path ends at the target and is reachable.{restored}"
            )
        else:
            applied_note = f"  [ok](set on {admin_node.name})[/ok]" if result.applied else ""
            message = (
                f"[ok]✓[/ok] optimal TX for [brand]{admin_node.name}[/brand] → "
                f"[brand]{result.target}[/brand] is [brand]{result.best_tx}[/brand]" + applied_note
            )

        return ToolResult(
            report=report,
            # No level got a trace through. Thus nothing was measured and nothing was tuned.
            # The sweep ran and has nothing to report.
            exit_code=exitcodes.NO_RESULT if no_result else exitcodes.OK,
            summary={
                "target": result.target,
                "admin_node": result.admin_node,
                "path": result.path,
                "best_tx": result.best_tx,
                "best_snr": result.best_snr,
                "best_success_rate": round(result.best_success_rate, 3),
                "original_tx": result.original_tx,
                "applied": result.applied,
                "levels_measured": len(result.levels),
            },
            message=message,
        )

    async def _resolve_password(
        self, ctx: AppContext, admin_node: Contact, params: dict[str, Any]
    ) -> str:
        """Find the admin password: from the flag, from the store, or from a prompt.

        The question that this method asks is **"is a person there to answer?"**. Before,
        it asked ``--json`` instead. That flag is about the look of the output, so it
        cannot answer the question, and its answer was wrong in both directions. With the
        flag, a piped run failed cleanly. Without it, a scheduled run on a terminal waited
        for a password, but no person was there to type it. ``ctx.interactive`` reads stdin,
        which is where the answer is.

        Args:
            ctx: The shared application context.
            admin_node: The node that we will log in to.
            params: The tool parameters (they can hold an explicit ``password``).

        Returns:
            The password for the login.

        Raises:
            typer.BadParameter: If no password is available and MeshTerm cannot ask
                anyone for one. This is a usage error, not a device failure: nothing was
                transmitted, and a retry cannot help until the command line has the
                password.
        """
        password = params.get("password") or ctx.admin_store.get(admin_node)
        if password:
            return str(password)
        if not ctx.interactive:
            raise typer.BadParameter(
                f"no admin password for {admin_node.name!r}; pass --password or run once "
                "interactively to store it."
            )
        # This resolver runs only on the scripted CLI path (the menu logs in from the pushed
        # sweep screen). Thus the surface here is PlainUi: no screen stack, no floating.
        entered = await ctx.ui.text(f"Admin password for {admin_node.name}:", password=True)
        if not entered:
            raise typer.BadParameter("an admin password is required to tune a remote node.")
        return entered

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``tx-optimize`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _tx_optimize(
            path: str = typer.Option(
                ...,
                "--path",
                # No ``-p`` short form. ``-p`` is the global ``--profile``, and
                # ``_globals_first`` moves a group option in front of the subcommand, at any
                # position where it is typed. Thus a leaf ``-p`` can never get to this option.
                # It can only hide it.
                help="Forced path ending at the target (e.g. 'Repeater,Target' or '3d,f2')",
            ),
            samples: int = typer.Option(3, "--samples", "-n", help="Traces per TX level"),
            step: int = typer.Option(3, "--step", help="Coarse sweep step"),
            tx_min: int | None = typer.Option(None, "--min", help="Lowest TX power"),
            tx_max: int | None = typer.Option(None, "--max", help="Highest TX power"),
            password: str | None = typer.Option(
                None, "--password", help="Admin password (else remembered/prompted)"
            ),
            apply: bool = typer.Option(True, "--apply/--no-apply", help="Set the winner"),
        ) -> None:
            tool_params: dict[str, Any] = {
                "path": path,
                "samples": samples,
                "step": step,
                "apply": apply,
            }
            if tx_min is not None:
                tool_params["tx_min"] = tx_min
            if tx_max is not None:
                tool_params["tx_max"] = tx_max
            if password is not None:
                tool_params["password"] = password
            run_tool_command(self, tool_params)


def _resolve_link(path: str, contacts: list[Contact]) -> tuple[Contact, str]:
    """Divide a forced path into the admin node that we tune and the label of the target.

    Args:
        path: The hash path, with commas between the hops (the output of
            ``parse_trace_path``).
        contacts: The known contacts. They change hashes back into names and keys.

    Returns:
        ``(admin_node, target_label)``: the second-to-last hop as a full :class:`Contact`
        (it is necessary for the login), and a friendly name for the last hop.

    Raises:
        typer.BadParameter: If the path has fewer than two hops. The argument is wrong and
            nothing was transmitted. Thus that is a usage error (exit 2), not a device
            failure.
        DeviceCommandError: If no known contact with a public key matches the admin hop.
            That error *is* about the mesh: the path is correct, but we have not heard
            from the node that it names.
    """
    hops = [h for h in path.split(",") if h]
    if len(hops) < 2:
        raise typer.BadParameter(
            "the path needs at least two hops: the node to tune and the target after it "
            "(e.g. 'AdminNode,Target')."
        )
    admin = _contact_for_hash(hops[-2], contacts)
    if admin is None or not (admin.public_key or "").strip():
        raise DeviceCommandError(
            f"the node before the target ({hops[-2]}) isn't a known contact with a public "
            "key, so we can't log in to tune it. Receive an advert from it first."
        )
    target = _contact_for_hash(hops[-1], contacts)
    return admin, (target.name if target else hops[-1])


def _contact_for_hash(hash_hex: str, contacts: list[Contact]) -> Contact | None:
    """Return the contact whose key matches the hash of a path hop, if there is one.

    Args:
        hash_hex: The hash of a path hop (the first part of the public key of the node).
        contacts: The known contacts to compare with.

    Returns:
        The :class:`Contact` that matches, or ``None``.
    """
    needle = hash_hex.lower().removeprefix("0x")
    for c in contacts:
        pub = (c.public_key or "").lower().removeprefix("0x")
        prefix = (c.key_prefix or "").lower().removeprefix("0x")
        if pub.startswith(needle):
            return c
        if prefix and (prefix.startswith(needle) or needle.startswith(prefix)):
            return c
    return None


def _target_items(contacts: list[Contact], admin: Contact) -> list:
    """The rows of the *measure at* list, in the order of how recently each node was heard.

    The value of each row is the **name** of the contact, because a typed hex prefix can
    go in the same place (refer to :meth:`TxOptimizeTool._run_live`). The list is empty
    when the tuned node is the only contact. Then the caller asks for a typed prefix.

    Args:
        contacts: The known contacts of the device.
        admin: The tuned node, which the user already selected. It is not in the list,
            because it cannot measure itself.

    Returns:
        One :class:`~meshterm.ui.tui.select.Choice` for each other contact, the most
        recently heard first.
    """
    from rich.text import Text

    from ..ui.theme import name_style
    from ..ui.tui import Choice
    from ..ui.widgets import DEFAULT_GLYPH, NODE_GLYPHS

    def row(contact: Contact) -> Any:
        # The type mark keeps its own fixed hue. The *name* gets the hue of the node, which
        # comes from its key, the same as in each other list of nodes. (A style on the
        # Text itself becomes the base of the row, and then the name also gets the colour
        # of the type.)
        glyph, glyph_style = NODE_GLYPHS.get(contact.node_type, DEFAULT_GLYPH)
        label = Text()
        label.append(f"{glyph} ", style=glyph_style)
        label.append(
            contact.name,
            style=name_style(contact.name, contact.public_key or contact.key_prefix),
        )
        return Choice(title=label, value=contact.name)

    return [
        row(c)
        for c in sorted(
            (c for c in contacts if c.name != admin.name),
            key=lambda c: -(c.last_seen.timestamp() if c.last_seen else 0.0),
        )
    ]


def _fmt_snr(snr: float | None) -> str:
    """Format an optional SNR for the progress description.

    Args:
        snr: The SNR in dB, or ``None``.

    Returns:
        A short string with a fixed width, for example ``+5.1`` or ``  n/a``.
    """
    return f"{snr:+.1f}" if snr is not None else " n/a"


def _sweep_report(
    result: TxOptResult, path: str, admin_node: Contact, contacts: list[Contact]
) -> tuple:
    """State a TX sweep: the result, then each measured level.

    The best level comes first, because the command was asked for it, and a caller acts
    on it. The records of each level come after it, so that a person can compare the
    choice with the measurements that it came from. The ``★`` that the menu puts on the
    best row has no column here. ``optimal_tx_dbm`` above the table already names that
    level, and a mark is a thing to look at, not a thing to test.

    The two nodes use the shared node shape. Here that is more important than in other
    places. On the plain line, only their names tell them apart. Without the shape, a
    caller who compares a sweep with a trace has no key to join them on.

    Args:
        result: The completed sweep.
        path: The forced route that the traces went through, as hex hops.
        admin_node: The node whose TX power was tuned.
        contacts: The contact list, to find the target by its name.

    Returns:
        The blocks of the report.
    """
    from ..ui import fields
    from ..ui.fields import NodeRef
    from ..ui.report import Facts, Listing

    def node(name: str) -> NodeRef | None:
        """One end of the tuned link, in the shared node shape."""
        contact = next((c for c in contacts if c.name == name), None)
        if contact is None:
            return NodeRef(name=name) if name else None
        return NodeRef(
            name=contact.name,
            key=(contact.public_key or "").lower() or None,
            hash=(contact.key_prefix or "").lower() or None,
            type=NODE_TYPE_LABELS.get(contact.node_type),
        )

    facts = Facts(
        key="tx_optimize",
        fields=(
            fields.node("tuning_node", lanes=(("name", "tuning_node"),)),
            fields.node("target", lanes=(("name", "target"),)),
            fields.spec(),
            fields.integer("optimal_tx_dbm", "optimal_tx_dbm"),
            fields.snr("target_snr_db", "target_snr_db"),
            fields.decimal("reliability", "reliability", ".2f"),
            fields.integer("previous_tx_dbm", "previous_tx_dbm"),
            fields.flag("applied", "applied"),
        ),
        values={
            "tuning_node": node(result.admin_node) or NodeRef(name=admin_node.name),
            "target": node(result.target),
            "path": path,
            "optimal_tx_dbm": result.best_tx,
            "target_snr_db": result.best_snr,
            # A fraction in [0, 1], not a formatted "1.00". The plain column rounds it so
            # that it is easy to read, and the document keeps the number as a number.
            "reliability": result.best_success_rate,
            "previous_tx_dbm": result.original_tx,
            "applied": result.applied,
        },
    )
    levels = Listing(
        key="levels",
        columns=(
            fields.integer("tx_dbm", "TX_DBM"),
            fields.snr("target_snr_db", "TARGET_SNR_DB"),
            fields.integer("successes", "SUCCESSES"),
            fields.integer("samples", "SAMPLES"),
        ),
        rows=[
            {
                "tx_dbm": level.tx_power,
                "target_snr_db": level.target_snr,
                "successes": level.successes,
                "samples": level.samples,
            }
            for level in result.sorted_by_tx()
        ],
    )
    return (facts, levels)
