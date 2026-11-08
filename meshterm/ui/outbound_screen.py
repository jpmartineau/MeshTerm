# SPDX-License-Identifier: Apache-2.0
"""The Outbound paths pages: the sites of this computer, the fixed devices, and their paths.

An outbound path is a path out of our node into the mesh, which the user trusts for the
first hops of a route (refer to :mod:`meshterm.core.outbound_store`). The pages have three
levels, each a place on the stack:

* **Outbound paths** (the tool page). The summary line says which list applies now: the
  list of a fixed device, the list of the active site, or none. Below it are the sites,
  with a ``✓`` on the active site, and the fixed devices. Enter opens one of them. Two
  action rows make a new site, or make the connected device a fixed device.
* ``Site — name`` and ``Device — name``. One editor for a list of outbound paths, in
  order. Each path is a row: its rank and its route, with the evidence for its first
  link under it. ``out`` is how well the first hop hears our node, and ``in`` is how well
  our node hears it. A radio link is not symmetric, and the evidence of MeshTerm comes
  mostly from what our node hears. Thus the two directions show apart (refer to
  :func:`~meshterm.services.outbound.first_link`). Enter on a path floats a small dialog:
  edit it in the composer, move it up or down, or remove it. Del removes it too. The
  changes are staged, and Apply writes them (the ``exit_rows`` shape of the app). Below
  the list are the actions of the site or the device, which act at once: use the site,
  rename it, delete it, or unfix the device.

An outbound path is composed in the path composer, in its outbound mode (refer to
:class:`~meshterm.ui.path_composer.PathComposerScreen`). The suggestions come from the
evidence graph, the same as for a trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.cells import cell_len
from rich.text import Text

from ..core.outbound_store import (
    SOURCE_DEVICE,
    SOURCE_SITE,
    OutboundPath,
    OutboundStore,
    clean_path,
)
from ..persistence.logging import get_logger
from ..services import trace_runner
from ..services.device_routes import report_notice
from ..services.outbound import (
    DeviceIdentity,
    FirstLink,
    device_identity,
    first_link,
    session_contacts,
    session_topology,
)
from ..services.topology import MeshTopology
from .menus import (
    confirm_discard,
    exit_rows,
    fit_cells,
    icon_lane,
    marked_label,
    menu_rows,
    section_heading,
)
from .path_composer import PathComposerScreen
from .pathline import cut_to, path_line
from .theme import snr_style
from .tui.screen import CANCEL
from .tui.select import Choice, DeleteRequest, SelectScreen, Separator
from .widgets import age_seconds, format_age

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..context import AppContext
    from ..core.models import Contact
    from ..services.site_suggest import SiteSuggestion

#: The hint of the tool page: each row opens a page.
_PAGE_HINT = "↑↓ move · Enter open · Esc back"
#: The hint of an editor page.
_EDIT_HINT = "↑↓ move · Enter select · Esc back"
#: The limits of the name lane of the sites and the fixed devices, in cells. The lane fits
#: the longest name between the two, the same as the Rooms page.
_NAME_MIN = 8
_NAME_MAX = 16
#: The longest name that the user can give a site, in characters. A longer name only makes
#: the rows of the tool page longer, and a site needs a short name.
SITE_NAME_MAX = 24

_NEW = ("new",)
_FIX = ("fix",)
_ADD = ("add",)
_APPLY = ("apply",)
_BACK = ("back",)
_USE = ("use",)
_UNUSE = ("unuse",)
_RENAME = ("rename",)
_DELETE = ("delete",)
_UNFIX = ("unfix",)


@dataclass(slots=True)
class _Env:
    """What each page needs from the session, read one time for the visit.

    Attributes:
        identity: The connected companion (empty with no device).
        contacts: Its contacts, for the names of the hops.
        topology: The evidence graph around our node.
        width_bytes: The path-hash width at which a hop with no name shows its hash.
    """

    identity: DeviceIdentity
    contacts: list[Contact]
    topology: MeshTopology
    width_bytes: int
    _resolve: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Build the fallback resolver one time (it scans the contacts)."""
        self._resolve = trace_runner.make_node_resolver(self.contacts)

    def name(self, hop: str | None) -> str | None:
        """The name of a hop: the name in the graph, else the first contact that matches."""
        if hop is None:
            return None
        return self.topology.display_name(hop) or self._resolve(hop) or hop


async def _env(ctx: AppContext) -> _Env:
    """Read the session for a visit: the device, its contacts, the graph, and the width."""
    identity = await device_identity(ctx)
    contacts = await session_contacts(ctx)
    try:
        from .trace_screen import collapse_trace_width

        width_bytes = collapse_trace_width(int(await ctx.devstate.path_hash_mode()))
    except Exception:  # noqa: BLE001 - an optional read. One byte is the protocol default.
        width_bytes = 1
    return _Env(identity, contacts, session_topology(ctx, identity.key, contacts), width_bytes)


# --- the tool page --------------------------------------------------------------------


async def manage_outbound(ctx: AppContext) -> None:
    """Run the Outbound paths page until the user backs out.

    Args:
        ctx: The shared application context (which runs the interactive TUI surface).
    """
    env = await _env(ctx)
    store = ctx.outbound_store
    title, prompt, items = _page(store, env)
    menu = SelectScreen(title, items, prompt=prompt, footer_hint=_PAGE_HINT, filterable=False)
    async with ctx.ui.session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            if choice is CANCEL or choice is None:
                return
            if choice == _NEW:
                name = await _ask_site_name(ctx)
                if name is not None:
                    store.put_site(name, [])
                    if store.active_site() is None:
                        await switch_site(ctx, name)  # the first site is where the user is
                    await _site_page(ctx, env, name)
            elif choice == _FIX and env.identity.key:
                active = store.active_site()
                store.fix_device(
                    env.identity.key, env.identity.name, list(active.paths) if active else []
                )
                await _device_page(ctx, env, env.identity.key)
            elif choice[0] == "site":
                await _site_page(ctx, env, choice[1])
            elif choice[0] == "device":
                await _device_page(ctx, env, choice[1])
            title, prompt, items = _page(store, env)
            menu.replace_items(items, title=title, prompt=prompt)


def _page(store: OutboundStore, env: _Env) -> tuple[str, str, list]:
    """The title, the summary line, and the rows of the tool page."""
    sites = store.sites()
    active = store.active_site()
    devices = store.fixed_devices()
    choice = store.lookup(env.identity.key or None)
    if choice.source == SOURCE_DEVICE:
        prompt = f"Now: {choice.name or 'this device'}'s own paths (a fixed device)"
    elif choice.source == SOURCE_SITE:
        prompt = f"Now: site {choice.name}"
    else:
        prompt = "Now: no site — routes come from what MeshTerm heard"

    lane = icon_lane(("✎",))
    labels = [site.name for site in sites] + [d.name or k[:12] for k, d in devices.items()]
    name_w = max(_NAME_MIN, min(_NAME_MAX, max((cell_len(n) for n in labels), default=0)))
    items: list = [section_heading("Sites")]
    for site in sites:
        is_active = active is not None and site.name == active.name
        items.append(_list_row(env, ("site", site.name), site.name, site.paths, is_active, name_w))
    if not sites:
        items.append(
            Separator("  no sites yet — a site holds the paths for where you are", style="muted")
        )
    items += menu_rows(
        [(marked_label("✎", "New site…", "", lane=lane), "Name it, then add its paths", _NEW)]
    )

    items.append(section_heading("Fixed devices"))
    for key, device in devices.items():
        label = device.name or key[:12]
        items.append(_list_row(env, ("device", key), label, device.paths, False, name_w))
    if not devices:
        items.append(Separator("  none — a fixed device keeps its own paths", style="muted"))
    if env.identity.key and env.identity.key not in devices:
        name = env.identity.name or "this device"
        items += menu_rows(
            [
                (
                    marked_label("✎", f"Fix {name}…", "", lane=lane),
                    "It never moves: give it its own paths",
                    _FIX,
                )
            ]
        )
    return "Outbound paths", prompt, items


def _list_row(env: _Env, value: tuple, name: str, paths: list, active: bool, name_w: int) -> Choice:
    """One site or fixed device: the active mark, the name, the path count, the first path."""

    def title(width: int) -> Text:
        text = Text(no_wrap=True)
        text.append("✓ " if active else "  ", style="ok")
        text.append(fit_cells(name, name_w))
        count = len(paths)
        counted = f"{count} path{'' if count == 1 else 's'}"
        text.append(f"  {counted:<8}  ", style="muted")
        if paths:
            text.append_text(_route(env, paths[0]))
        return cut_to(text, width)

    return Choice(title=title, value=value, fitted=True)


def _route(env: _Env, path: OutboundPath) -> Text:
    """An outbound path as a path line: our ``★``, then its hops, with an open end."""
    return path_line(
        [None, *path],
        env.name,
        prefix_bytes=env.width_bytes,
        hash_bytes=env.width_bytes,
        bare_self=True,
        dim_self=False,
        to_destination=False,
    ).text()


async def _ask_site_name(ctx: AppContext, *, current: str = "") -> str | None:
    """Ask for the name of a site. A name that another site has is refused."""
    store = ctx.outbound_store

    def valid(text: str) -> bool | str:
        name = text.strip()
        if not name:
            return "a site needs a name"
        if len(name) > SITE_NAME_MAX:
            return f"at most {SITE_NAME_MAX} characters"
        other = store.site(name)
        if other is not None and other.name.casefold() != current.casefold():
            return f"a site named {other.name} already exists"
        return True

    answer = await ctx.ui.session.text(
        "Rename site" if current else "New site",
        prompt="Site name (where you are: Home, Work, …):",
        default=current,
        validate=valid,
        floating=True,
    )
    return answer.strip() if answer is not None else None


# --- the editor pages -------------------------------------------------------------------


async def _site_page(ctx: AppContext, env: _Env, name: str) -> None:
    """The page of one site: its outbound paths, then the actions of the site."""
    store = ctx.outbound_store
    site = store.site(name)
    if site is None:  # pragma: no cover - the row came from this store
        return
    state = {"name": site.name}

    def actions() -> list:
        return _site_actions(store, state["name"])

    def summary(paths: list[OutboundPath]) -> str:
        return _site_summary(store, state["name"], paths)

    async def act(value: Any) -> bool:
        """Run one action of the site. Return ``True`` when the page must close."""
        if value == _USE:
            await switch_site(ctx, state["name"])
        elif value == _UNUSE:
            await switch_site(ctx, None)
        elif value == _RENAME:
            new = await _ask_site_name(ctx, current=state["name"])
            if new is not None and new != state["name"]:
                current = store.site(state["name"])
                store.put_site(new, list(current.paths) if current else [], old_name=state["name"])
                state["name"] = new
        elif value == _DELETE:
            if await ctx.ui.dialog(
                f"Delete the site {state['name']} and its outbound paths?",
                [("Cancel", False), ("Delete", True)],
                title=f"Delete — {state['name']}",
                default=1,
                destructive=True,
            ):
                store.remove_site(state["name"])
                return True
        return False

    def save(paths: list[OutboundPath]) -> None:
        store.put_site(state["name"], paths)

    await _paths_page(
        ctx,
        env,
        title=lambda: f"Site — {state['name']}",
        summary=summary,
        paths=list(site.paths),
        actions=actions,
        act=act,
        save=save,
    )


async def _device_page(ctx: AppContext, env: _Env, key: str) -> None:
    """The page of one fixed device: its outbound paths, then the action that unfixes it."""
    store = ctx.outbound_store
    device = store.fixed_device(key)
    if device is None:  # pragma: no cover - the row came from this store
        return
    label = device.name or key[:12]

    def actions() -> list:
        return _device_actions()

    def summary(paths: list[OutboundPath]) -> str:
        return _device_summary(key == env.identity.key, paths)

    async def act(value: Any) -> bool:
        if value == _UNFIX and await ctx.ui.dialog(
            f"Unfix {label}? Its own outbound paths are forgotten, and the active site "
            "applies to it again.",
            [("Cancel", False), ("Unfix", True)],
            title=f"Unfix — {label}",
            default=1,
            destructive=True,
        ):
            store.unfix_device(key)
            return True
        return False

    def save(paths: list[OutboundPath]) -> None:
        store.fix_device(key, device.name, paths)

    await _paths_page(
        ctx,
        env,
        title=lambda: f"Device — {label}",
        summary=summary,
        paths=list(device.paths),
        actions=actions,
        act=act,
        save=save,
    )


def _paths_phrase(count: int) -> str:
    """``"no outbound paths"``, ``"1 outbound path"``, ``"3 outbound paths"``."""
    if not count:
        return "no outbound paths"
    return f"{count} outbound path{'' if count == 1 else 's'}"


def _site_summary(store: OutboundStore, name: str, paths: list[OutboundPath]) -> str:
    """The summary line of a site page: whether the site is active, and its path count."""
    active = store.active_site()
    word = "active" if active is not None and active.name == name else "not active"
    return f"{word} · {_paths_phrase(len(paths))}"


def _device_summary(connected: bool, paths: list[OutboundPath]) -> str:
    """The summary line of a device page."""
    here = " · connected" if connected else ""
    return f"fixed{here} · {_paths_phrase(len(paths))} · the active site does not apply"


def _site_actions(store: OutboundStore, name: str) -> list:
    """The actions of a site, which act at once: use it (or stop), rename it, delete it."""
    lane = icon_lane(("✓", "✎", "🗑"))
    active = store.active_site()
    rows: list = []
    if active is None or active.name != name:
        rows.append(
            (
                marked_label("✓", "Use this site", "ok", lane=lane),
                "Its paths apply to each device that is not fixed",
                _USE,
            )
        )
    else:
        rows.append(
            (
                marked_label("○", "Stop using this site", "muted", lane=lane),
                "Routes then come from what MeshTerm heard",
                _UNUSE,
            )
        )
    rows.append((marked_label("✎", "Rename…", "", lane=lane), "Give the site a new name", _RENAME))
    rows.append(
        (
            marked_label("🗑", "Delete this site…", "err", lane=lane),
            "Forget the site and its paths",
            _DELETE,
        )
    )
    return [section_heading("Site"), *menu_rows(rows)]


def _device_actions() -> list:
    """The one action of a fixed device: unfix it."""
    lane = icon_lane(("🗑",))
    return [
        section_heading("Device"),
        *menu_rows(
            [
                (
                    marked_label("🗑", "Unfix this device…", "err", lane=lane),
                    "Forget its paths: it follows the active site",
                    _UNFIX,
                )
            ]
        ),
    ]


def _editor_items(env: _Env, staged: list[OutboundPath], owner: list, changes: int) -> list:
    """The rows of an editor page: the paths, the Add row, the owner's actions, the exit pair."""
    lane = icon_lane(("✎",))
    rows: list = [section_heading("Outbound paths")]
    rows.append(Separator("  out: its first hop hears you · in: you hear it", style="muted"))
    for index, path in enumerate(staged):
        rows.append(_path_row(env, index, path))
    if not staged:
        rows.append(Separator("  none yet — add the way out that works from here", style="muted"))
    rows += menu_rows(
        [
            (
                marked_label("✎", "Add an outbound path…", "", lane=lane),
                "Compose it hop by hop",
                _ADD,
            )
        ]
    )
    rows += owner
    rows += exit_rows(changes, apply_value=_APPLY, back_value=_BACK)
    return rows


async def _paths_page(
    ctx: AppContext,
    env: _Env,
    *,
    title: Any,
    summary: Any,
    paths: list[OutboundPath],
    actions: Any,
    act: Any,
    save: Any,
) -> None:
    """The editor of one list of outbound paths, with the actions of its owner below it.

    The changes to the list are staged. Apply writes them with ``save``, and Esc or Back
    with staged changes asks first (:func:`~meshterm.ui.menus.confirm_discard`). The
    actions of the owner act at once, through ``act``.

    Args:
        ctx: The shared application context.
        env: The session values of the visit.
        title: A callable that gives the title (a rename changes it).
        summary: A callable that gives the summary line for the staged list.
        paths: The stored list, the preferred path first.
        actions: A callable that gives the rows of the actions of the owner.
        act: An async callable that runs one action. It returns ``True`` when the page
            must close (the owner was deleted).
        save: A callable that writes a list.
    """
    stored = list(paths)
    staged = list(paths)
    edits = 0

    def changes() -> int:
        return 0 if staged == stored else max(1, edits)

    def items() -> list:
        return _editor_items(env, staged, actions(), changes())

    menu = SelectScreen(
        title(),
        items(),
        prompt=summary(staged),
        footer_hint=_EDIT_HINT,
        filterable=False,
        delete_hint="Del remove",
    )
    async with ctx.ui.session.stay(menu) as visit:
        while True:
            choice = await visit.result()
            if choice is CANCEL or choice is None or choice == _BACK:
                if changes() and not await confirm_discard(ctx, changes()):
                    continue
                return
            if isinstance(choice, DeleteRequest):
                index = choice.value[1]
                del staged[index]
                edits += 1
            elif choice == _APPLY:
                save(list(staged))
                stored = list(staged)
                edits = 0
            elif choice == _ADD:
                added = await _compose(ctx, env, ())
                if added is not None and added not in staged:
                    staged.append(added)
                    edits += 1
            elif isinstance(choice, tuple) and choice[0] == "path":
                edits += await _path_actions(ctx, env, staged, choice[1])
            elif await act(choice):
                return
            menu.replace_items(items(), title=title(), prompt=summary(staged))


def _path_row(env: _Env, index: int, path: OutboundPath) -> Choice:
    """One outbound path: its rank and route, with the evidence of its first link below."""

    def title(width: int) -> Text:
        text = Text(f"{index + 1}  ", style="accent", no_wrap=True)
        text.append_text(_route(env, path))
        return cut_to(text, width)

    return Choice(
        title=title,
        value=("path", index),
        detail=_evidence(first_link(env.topology, path)),
        deletable=True,
    )


def _evidence(link: FirstLink) -> Text:
    """The two directions of a first link: ``out +9 dB ×12 · in +11 dB ×2.1k · 3m``."""
    if not link.out_count and not link.in_count:
        return Text("   no readings of its first hop yet", style="muted")
    text = Text("   ")
    for word, snr, count in (
        ("out", link.out_snr, link.out_count),
        ("in", link.in_snr, link.in_count),
    ):
        if word == "in":
            text.append(" · ", style="muted")
        text.append(f"{word} ", style="muted")
        if snr is None:
            text.append("-", style="muted")
        else:
            text.append(f"{snr:+.0f} dB", style=snr_style(snr))
            text.append(f" ×{_count(count)}", style="muted")
    if link.last_heard is not None:
        text.append(f" · {format_age(age_seconds(link.last_heard))}", style="muted")
    return text


def _count(count: int) -> str:
    """A reading count in at most four cells: ``12``, ``980``, ``2.1k``, ``14k``."""
    if count < 1000:
        return str(count)
    if count < 10_000:
        return f"{count / 1000:.1f}k"
    return f"{count // 1000}k"


async def _path_actions(ctx: AppContext, env: _Env, staged: list[OutboundPath], index: int) -> int:
    """Float the actions of one path, and apply the selected one to the staged list.

    Returns:
        ``1`` when the list changed, else ``0``.
    """
    lane = icon_lane(("✎", "⚡", "↕", "🗑"))
    rows: list = [(marked_label("✎", "Edit…", "", lane=lane), "Change its hops", "edit")]
    if env.identity.key:  # a measure transmits, so it needs the device
        rows.append(
            (
                marked_label("⚡", "Measure", "", lane=lane),
                "One trace out along it and back",
                "measure",
            )
        )
    if index > 0:
        rows.append(
            (marked_label("↕", "Move up", "", lane=lane), "Prefer it to the path above", "up")
        )
    if index < len(staged) - 1:
        rows.append(
            (marked_label("↕", "Move down", "", lane=lane), "Prefer the path below", "down")
        )
    rows.append((marked_label("🗑", "Remove", "err", lane=lane), "Take it off the list", "remove"))
    picked = await ctx.ui.session.select(
        f"Outbound path {index + 1}",
        menu_rows(rows),
        filterable=False,
        footer_hint="↑↓ move · Enter select · Esc cancel",
        floating=True,
    )
    if picked == "edit":
        changed = await _compose(ctx, env, staged[index])
        if changed is None or changed == staged[index]:
            return 0
        if changed in staged:  # the same path is already on the list: keep one copy
            del staged[index]
        else:
            staged[index] = changed
        return 1
    if picked == "measure":
        await _measure(ctx, env, staged[index])
        return 0
    if picked == "up":
        staged[index - 1], staged[index] = staged[index], staged[index - 1]
        return 1
    if picked == "down":
        staged[index + 1], staged[index] = staged[index], staged[index + 1]
        return 1
    if picked == "remove":
        del staged[index]
        return 1
    return 0


async def switch_site(ctx: AppContext, name: str | None) -> None:
    """Make a site active (or none), let the device routes follow, and say what changed.

    The routes that the radio learned at the old site are saved for it and cleared, and the
    routes saved for the new site come back (refer to
    :meth:`~meshterm.services.device_routes.RouteKeeper.change_site`). Each step can be
    undone by a change back, so nothing asks first. A notice says what changed, and only
    when something changed.
    """
    try:
        report = await ctx.routes.change_site(name)
    except KeyError:
        return  # the site was deleted while the page was open
    except Exception as exc:  # noqa: BLE001 - the site changed. Only the routes did not follow
        get_logger().warning("couldn't make the device routes follow the site: %s", exc)
        await ctx.ui.dialog(
            f"The site changed, but the radio refused a route change: {exc}",
            [("OK", None)],
            title="Device routes",
            default=0,
        )
        return
    notice = report_notice(report) if report is not None else ""
    if notice:
        await ctx.ui.dialog(f"{notice}.", [("OK", None)], title="Device routes", default=0)


async def offer_site(ctx: AppContext, suggestion: SiteSuggestion) -> None:
    """Ask the user to use the site whose way out the radio hears, and say why.

    The dialog names the node that the radio heard, how many times, and the site whose
    outbound path starts on it. It never changes the site without an answer. "Never
    suggest" sets the ``suggest_site`` preference off, so the user can stop the question
    where it asks (refer to :mod:`meshterm.services.site_suggest`).
    """
    store = ctx.outbound_store
    site = store.site(suggestion.site)
    if site is None:  # deleted since the radio heard it
        return
    contacts = await session_contacts(ctx)
    named = trace_runner.make_node_resolver(contacts)(suggestion.hop)
    label = named if named and named != suggestion.hop else suggestion.hop
    active = store.active_site()
    unheard = (
        f"no first hop of site {active.name} was heard"
        if active is not None
        else "no site is active now"
    )
    prompt = (
        f"Your radio heard {label} {suggestion.heard} times since MeshTerm started. It is the "
        f"first hop of an outbound path of site {site.name}, and {unheard}.\n\n"
        f"Use site {site.name}?"
    )
    answer = await ctx.ui.dialog(
        prompt,
        [("Not now", None), ("Never suggest", "never"), (f"Use {site.name}", "use")],
        title="Site",
        default=2,
    )
    if answer == "use":
        await switch_site(ctx, site.name)
    elif answer == "never":
        ctx.preferences.set("suggest_site", False)
        ctx.preferences.save()


def measure_walk(path: OutboundPath) -> tuple[str, ...]:
    """The walk that measures an outbound path: out along it, and back the same way.

    The last hop of the path is the turning point, so it is in the walk one time
    (``A, B, C`` walks ``A, B, C, B, A``). The trace protocol has no return field, and the
    reply comes home from the last hop of the walk. Thus each link of the path is measured
    in both directions. The first hop reports how well it heard our node, and our node
    measures the last step home.
    """
    return (*path, *reversed(path[:-1]))


async def _measure(ctx: AppContext, env: _Env, path: OutboundPath) -> None:
    """Send one trace along ``path`` and back, store it, and show what each hop measured.

    One press sends one trace, after the shared transmit cooldown (refer to
    :func:`~meshterm.ui.cooldown.wait_for_cooldown`). Repeaters penalize a node that sends
    bursts. The trace is stored as a path walk, exactly as a Trace path run stores it.
    Thus its readings go into the evidence graph, and the evidence line of the path
    changes when the dialog closes.
    """
    from ..core.models import PATH_TRACE_TARGET
    from ..services.topology import render_custom_spec
    from .cooldown import wait_for_cooldown

    walk = measure_walk(path)
    spec = render_custom_spec(walk, env.width_bytes)
    if not await wait_for_cooldown(ctx, action="Measure"):
        return
    device = await ctx.device()
    hex_path = trace_runner.parse_trace_path(spec, env.contacts)
    run_id = ctx.repo.start_run(
        "trace", {"target": PATH_TRACE_TARGET, "path": hex_path}, ctx.profile_name
    )
    try:
        async with ctx.ui.busy_dialog("tracing out and back…", title="Measure"):
            result = await device.run_trace(PATH_TRACE_TARGET, path=hex_path)
    except BaseException as exc:
        ctx.repo.finish_run(run_id, "error", {"error": str(exc) or type(exc).__name__})
        raise
    ctx.repo.record_trace(run_id, result)
    ctx.repo.finish_run(
        run_id,
        "ok",
        {"target": PATH_TRACE_TARGET, "success": result.success, "min_snr": result.min_snr},
    )
    env.topology = session_topology(ctx, env.identity.key, env.contacts)
    await ctx.ui.session.message_dialog(_measured(env, walk, result), title="Measure")


def _measured(env: _Env, walk: tuple[str, ...], result: Any) -> Text:
    """What one measure found: who heard whom, at what SNR, or why it did not come back."""
    if not result.success:
        return Text("✗ no reply — a hop did not hear the trace, or did not relay it", style="err")
    text = Text("✓ ", style="ok")
    text.append("the trace came back")
    if result.round_trip_ms is not None:
        text.append(f" in {result.round_trip_ms / 1000:.1f} s", style="muted")
    senders = [None, *walk]
    receivers = [*walk, None]
    for hop, sender, receiver in zip(result.hops, senders, receivers, strict=False):
        text.append("\n")
        text.append(env.name(receiver) or "you" if receiver else "you")
        text.append(" heard ", style="muted")
        text.append(env.name(sender) or "you" if sender else "you")
        text.append(" at ", style="muted")
        text.append(f"{hop.snr:+.1f} dB", style=snr_style(hop.snr))
    return text


async def _compose(ctx: AppContext, env: _Env, path: OutboundPath) -> OutboundPath | None:
    """Open the composer in outbound mode. Return the composed path, or ``None``."""
    topo = env.topology
    screen = PathComposerScreen(
        device_label=env.identity.name or "you",
        device_hash=env.identity.key or None,
        topology=topo,
        width_bytes=env.width_bytes,
        hops=[topo.canonical(hop) or hop for hop in path],
        resolve=env.name,
        outbound=True,
    )
    result = await ctx.ui.session.run_screen(screen)
    if result is CANCEL or not isinstance(result, str):
        return None
    return clean_path(result)


__all__ = ["SITE_NAME_MAX", "manage_outbound"]
