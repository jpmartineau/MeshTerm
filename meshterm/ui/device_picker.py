# SPDX-License-Identifier: Apache-2.0
"""Interactive companion-device picker: the startup splash shown before the menu.

Shown once at the start of the interactive menu when no port was given explicitly. Unlike the
in-menu prompts, it is drawn as a chromeless splash — the MeshTerm wordmark centered above a
content-sized box, with no header/footer status bars. It lists the discovered devices in
aligned columns — name, connection target, a TYPE glyph (wired serial vs Bluetooth), and a
HARDWARE column (the confirmed device's firmware model, else the USB vendor) — tags the ones
already confirmed as MeshCore companions, marks the remembered "last known good" one, and
preselects it as the default.

Confirmed companions are sorted to the top, most-recently-used first, and shown by the mesh
node name we learned when we last talked to them (in white, so they stand out from ports we've
merely detected). Everything else follows in discovery order.

Selecting a device runs an immediate smoke test (via the ``verify`` callback): a genuine
MeshCore companion is remembered — forever — as confirmed and becomes the session's active
device; anything else sends the user back to the list to choose another.

A machine with things permanently plugged into it that are not companions — a debug probe, a
programmer, a serial adapter — shows them here every single time, in front of the one device
the user actually wants. So a row can be **hidden**: ``h`` drops the highlighted device from
this list and remembers that (see :meth:`~meshterm.core.device_store.DeviceStore.hide`), and
``⇧H`` brings every hidden one back. It is a *listing* choice and nothing more: a hidden
device is still remembered, still reachable by ``--port``, and un-hides itself the moment it
is connected to again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text

from ..core.config import DeviceProfile, SpiWiring
from ..core.connection import DeviceAuthenticationError, DeviceCommandError
from ..core.device_store import DeviceStore, RememberedDevice
from ..core.discovery import (
    DEFAULT_TCP_PORT,
    DiscoveredDevice,
    discover_ble_devices,
    discover_devices,
    parse_tcp_endpoint,
    serial_device,
    spi_device,
    tcp_device,
)
from ..core.spiradio import spi_present
from ..platforms import get_platform
from .logo import load_logo
from .menus import Lane, align_icons, column_header, fit_cells
from .tui import Choice, DeleteRequest, KeyRequest, Separator

if TYPE_CHECKING:
    from .surface import Ui

#: A smoke test: probe a chosen device with an optional Bluetooth PIN and return its self-info
#: dict if it is a MeshCore companion, else ``None``. Supplied by the caller so this UI module
#: stays free of the connection *logic*. It may raise :class:`DeviceAuthenticationError` when
#: the device needs a PIN (the picker then collects one and retries with it), or another
#: :class:`DeviceCommandError` for a different actionable failure (shown verbatim). The second
#: argument is the PIN to try, or ``None`` to use whatever default the caller holds.
Verify = Callable[[DiscoveredDevice, str | None], Awaitable[dict | None]]

#: The Quit row's value. Selecting it — like pressing Esc — leaves the splash without a
#: device, which the caller treats as "exit the program".
_QUIT = object()

#: The "add a network device" row's value. Selecting it opens a host:port prompt (TCP
#: companions aren't discoverable, so they're named by hand) and smoke-tests the result.
_ADD_TCP = object()

#: The two shortcut tokens the splash declares, and the keys that raise them. ``h`` acts on
#: the highlighted row; ``⇧H`` is screen-wide, because a hidden row is not there to press a
#: key on — the way back has to be reachable from anywhere in the list.
_HIDE = object()
_SHOW_ALL = object()
_SHORTCUTS = {"h": _HIDE, "H": _SHOW_ALL}

#: TYPE-column glyphs marking how a device connects. Kept as module constants so the splash's
#: look can be retuned without touching the row-building logic. ``ᛒ`` is the *Bjarkan* rune the
#: Bluetooth logo is drawn from — rendered white on the Bluetooth blue (see the ``bluetooth``
#: theme style), flanked by the half-blocks below so it reads as a slim rounded badge — and
#: ``🔌`` is a plain plug for a wired serial link.
_BLE_ICON = "ᛒ"
_SERIAL_ICON = "🔌"

#: TYPE-column glyph for a TCP companion — a globe, marking a device reached over the network
#: rather than a wired or Bluetooth link. Two cells like the serial plug, so it aligns the same.
_TCP_ICON = "🌐"

#: TYPE-column glyph for a radio on the host's own SPI bus — a pin, because the radio is
#: right here, on this machine. Two cells, aligning with the plug and the globe.
_SPI_ICON = "📍"

#: Half-block glyphs that taper the Bluetooth badge: ``▐`` fills a cell's right half (so it
#: hugs the rune's left edge) and ``▌`` its left half (hugging the right edge). Drawn in the
#: badge's blue over the terminal background, they widen the blue by half a cell on each side.
_BADGE_LEFT = "▐"
_BADGE_RIGHT = "▌"


def _type_cell(device: DiscoveredDevice) -> Text:
    """The TYPE-column badge for ``device`` as a styled fragment.

    Serial is a bare plug emoji (two cells, its own colour). Bluetooth is the rune on its blue
    badge, flanked by half-block slivers in the same blue so the fill reads as a slightly
    rounded chip a touch wider than the lone rune rather than a single hard-edged cell.

    The plug carries a leading space so its two cells sit centred under the "TYPE" heading,
    lining up with the Bluetooth rune (which the badge's left half-block already nudges in a
    cell) rather than hugging the column's left edge.
    """
    if device.is_tcp:
        return Text(" " + _TCP_ICON)
    if device.is_spi:
        return Text(" " + _SPI_ICON)
    if not device.is_ble:
        return Text(" " + _SERIAL_ICON)
    cell = Text()
    cell.append(_BADGE_LEFT, style="bluetooth.edge")
    cell.append(_BLE_ICON, style="bluetooth")
    cell.append(_BADGE_RIGHT, style="bluetooth.edge")
    return cell


def _pad(text: str, width: int) -> str:
    """Right-pad ``text`` with spaces to ``width`` display cells (wide-char aware)."""
    return text + " " * max(0, width - cell_len(text))


def _hardware_name(device: DiscoveredDevice) -> str:
    """The device's product name without its trailing ``(port)`` (that is its own column)."""
    if device.is_tcp:
        return device.name or device.product or device.description or "Network device"
    if device.is_ble:
        return device.name or device.product or device.description or "Bluetooth device"
    if device.is_spi:
        return device.name or "SPI radio"
    name = device.product or device.description or device.vendor_label or "Serial device"
    suffix = f"({device.port})"
    if name.endswith(suffix):
        name = name[: -len(suffix)].rstrip()
    return name


def _display_name(device: DiscoveredDevice, registry: dict[str, RememberedDevice]) -> str:
    """The name to show for ``device``: its remembered mesh node name, else the hardware name.

    Any device we've confirmed before — not just the single most-recent one — is shown by the
    mesh node name we learned at connect time, so a known ``COM11`` reads as "BaseStation"
    rather than the OS's generic "USB Serial Device". Devices with no record (or an empty
    remembered name) fall back to their hardware/product name.
    """
    record = registry.get(device.stable_id)
    if record is not None and record.node_name:
        return record.node_name
    return _hardware_name(device)


def _where(device: DiscoveredDevice) -> str:
    """The connection target shown in the middle column: serial port or BLE address."""
    return device.target


def _fit_target(text: str, width: int) -> str:
    """Fit a connection target into ``width`` cells, keeping its **end**.

    Deliberately not :func:`~meshterm.ui.menus.fit_cells`, which keeps the head: what tells
    two targets apart is their *tail* — ``usbmodem1101`` from ``usbmodem1102``,
    ``ttyUSB0`` from ``ttyUSB1`` — while the head they share (``/dev/cu.``) is boilerplate.
    Cutting the front is the cut that leaves the column able to do its one job in a picker,
    and cutting the back would render two different ports identically. A CoreBluetooth UUID
    is equally identifiable from either end, so the rule that is right for ports costs the
    platform that needed the cap nothing.

    Args:
        text: The target to fit.
        width: The cells available. Text already inside it is returned unchanged, so the
            platforms whose targets already fit the lane are untouched.

    Returns:
        The target, or its tail behind a leading ``…``, measuring at most ``width`` cells.
    """
    if cell_len(text) <= width:
        return text
    keep = max(0, width - 1)  # the leading ellipsis costs a cell
    tail = ""
    for ch in reversed(text):
        if cell_len(tail) + cell_len(ch) > keep:
            break
        tail = ch + tail
    return "…" + tail


def _hardware_label(device: DiscoveredDevice, registry: dict[str, RememberedDevice]) -> str:
    """The HARDWARE column text: the remembered firmware model, else the USB vendor.

    A confirmed device shows what it actually is ("Seeed Tracker T1000-E", learned from the
    device-query at connect time) — the only reliable source, since a BLE companion advertises
    no maker. A device we've never connected has no model on file, so it falls back to the USB
    vendor name (blank for an unconnected BLE advert, which genuinely tells us nothing yet).
    """
    record = registry.get(device.stable_id)
    if record is not None and record.hardware_model:
        return record.hardware_model
    return device.vendor_label


def _node_name_from(info: dict) -> str:
    """Return a device's own mesh node name from its self-info payload, or ``""``."""
    return str(info.get("adv_name") or info.get("name") or "")


def _model_from(info: dict) -> str:
    """Return the firmware's hardware model from the probe payload, or ``""``.

    The smoke-test probe folds the device-query's ``model`` into the identity dict, so this
    is the one chance to learn (and then remember) what the box actually is.
    """
    return str(info.get("model") or "")


async def _smoke_test(
    ui: Ui,
    chosen: DiscoveredDevice,
    name: str,
    where: str,
    verify: Verify,
) -> dict | None:
    """Smoke-test ``chosen`` behind the splash spinner, collecting a Bluetooth PIN if needed.

    Runs the ``verify`` probe on the chromeless splash (its wordmark and box unchanged, only an
    animated spinner where the device list was). If the device answers but demands a pairing
    PIN, opens the :class:`~meshterm.ui.tui.prompt.PinDialog` popup and retries with what the
    user enters — re-opening it with a "rejected" note on a wrong code — until the device
    connects or the user presses Esc.

    Args:
        ui: The interactive surface used for the spinner, PIN dialog, and failure notices.
        chosen: The device being tested.
        name: Its display name, woven into the spinner line and the PIN prompt.
        where: A human phrase for its transport ("over Bluetooth" / "on COM5").
        verify: The smoke-test callback (see :data:`Verify`); called with the PIN to try.

    Returns:
        The device's self-info dict once it answers, or ``None`` — after showing the relevant
        notice — to send the user back to the device list (not a MeshCore endpoint, an
        unrecoverable failure, or a cancelled PIN prompt).
    """
    pin: str | None = None
    pin_error = ""  # empty on the first ask; set once a PIN has been rejected
    while True:
        # BLE connect and service discovery take a few seconds, so the spinner matters most here.
        try:
            info = await ui.busy_startup(
                f"Talking to {name} {where}…",
                verify(chosen, pin),
                title="Checking companion",
                banner=load_logo(),
            )
        except DeviceAuthenticationError:
            # The device answered the scan but won't connect until it's bonded (or the last PIN
            # was wrong). Collect one in the popup and loop to retry; Esc returns to the list.
            entered = await ui.prompt_pin_startup(
                name,
                error=pin_error,
                help_text="The 6-digit code shown on the device or in the MeshCore app",
                banner=load_logo(),
            )
            if entered is None:
                return None  # the user gave up → back to the device list
            pin = entered
            pin_error = "That PIN was rejected — check the code and try again."
            continue
        except DeviceCommandError as exc:
            # A different actionable failure (not a PIN): show its remedy verbatim, then back to
            # the list. Plain styled text so the message's own punctuation isn't parsed as markup.
            notice = Text()
            notice.append(str(exc), style="warn")
            notice.append("\nChoose another device.")
            await ui.notify_startup(notice, title="Can't connect yet", banner=load_logo())
            return None

        if info is None:
            if chosen.is_tcp:
                reason = (
                    "Check the host and port — it may be unreachable, powered off, already "
                    "connected elsewhere, or busy."
                )
            elif chosen.is_spi:
                reason = (
                    "Its node started but never answered — its log is node.log in "
                    "MeshTerm's radio folder."
                )
            elif chosen.is_ble:
                reason = (
                    "It may be out of range, powered off, already connected elsewhere, or busy."
                )
            else:
                reason = "It may be a different kind of serial device, powered off, or busy."
            await ui.notify_startup(
                Text.from_markup(
                    f"[warn]{name} {where} didn't answer as a MeshCore device.[/warn]\n"
                    f"{reason}\n"
                    "Choose another device."
                ),
                title="Not a MeshCore device",
                banner=load_logo(),
            )
            return None

        return info


#: How often the splash re-enumerates serial ports. `comports()` is a SetupAPI/sysfs
#: walk rather than a free read, so it runs off the event loop -- but it is cheap enough
#: to repeat at this rate without anyone noticing, and it covers the common case: a USB
#: radio plugged in after the splash appeared.
_POLL_S = 2.0

#: Bluetooth is not cheap in the same way. Each scan is a bounded listen, so keeping one
#: running means keeping the radio listening for as long as this screen is open -- and
#: this screen is the one a reader leaves up while they go and find a cable, on a handheld,
#: on a battery. So it gets a duty cycle rather than a loop: a window this often, and only
#: while it is still plausible somebody is waiting on one.
_BLE_EVERY_S = 15.0
_BLE_WINDOW_S = 4.0

#: How many windows to run before giving up on finding a companion nobody has turned on.
#: Once something *is* listed the reader has what they came for; while the list is still
#: empty the count is ignored, because an empty splash is exactly where somebody is
#: waiting and the cost of another listen is the cost of being useful.
_BLE_WINDOWS = 8


async def prompt_device(
    ui: Ui,
    devices: list[DiscoveredDevice],
    store: DeviceStore,
    verify: Verify,
    profiles: Mapping[str, DeviceProfile] | None = None,
) -> DiscoveredDevice | None:
    """Prompt the user to choose a companion device on the startup splash.

    The chosen device is smoke-tested before it is accepted: only a device that answers the
    MeshCore identity query is returned (and recorded as confirmed). A device that fails the
    test re-opens the picker with a message asking the user to choose another.

    Args:
        ui: The interactive UI surface used to render the picker.
        devices: Discovered devices (likely-LoRa first).
        store: The confirmed-device registry, used to tag/preselect known devices and to
            record a device once its smoke test passes.
        verify: Async smoke test returning a device's self-info dict, or ``None`` if it is
            not a reachable MeshCore companion.
        profiles: The configured device profiles (``config.toml`` ``[profiles.*]``). Their TCP
            entries are folded into the list so a hand-authored network companion appears here
            without having to be connected to first (see :func:`_profile_tcp_devices`).

    Returns:
        The chosen, confirmed :class:`DiscoveredDevice`, or ``None`` if the user chose to
        leave the picker without selecting one — by pressing Esc or choosing the Quit row —
        which the caller treats as a request to exit.
    """
    # Serial and Bluetooth are kept apart because they are refreshed on different clocks:
    # a poll replaces everything attached, a Bluetooth window replaces everything in range,
    # and one must not wipe the other's findings.
    wired = [d for d in devices if not d.is_ble]
    wireless = [d for d in devices if d.is_ble]
    #: The row the *next* redraw should open on, when the pass just finished moved the list
    #: under the reader (see :func:`_after_hiding`). Cleared as soon as it is spent.
    focus: DiscoveredDevice | None = None

    def assemble() -> tuple[
        list[DiscoveredDevice], list, RememberedDevice | None, dict[str, RememberedDevice]
    ]:
        """The list as it stands this instant: what is there, less what is hidden.

        Built in one place because two callers need it now -- the loop below, and the
        rescan that redraws the rows under the reader while the splash is open.
        """
        scanned = wired + wireless
        remembered = store.load()
        # The full registry (not just the single last device) so *every* confirmed companion
        # can be named, highlighted, and sorted to the top — keyed by stable_id. Reloaded each
        # pass so an add or a removal is reflected the next time the list is drawn.
        registry = store.load_all()
        # A TCP companion isn't discoverable, so a previously confirmed one only reappears if we
        # rebuild it from its remembered endpoint and fold it into the list alongside the
        # scanned devices (the scan never produces it). Configured TCP profiles are folded in
        # the same way, after the scanned/remembered set so an already-known endpoint keeps its
        # richer remembered row rather than being shadowed by the profile.
        listed = scanned + _remembered_tcp_devices(scanned, registry)
        listed += _profile_tcp_devices(listed, profiles)
        # A serial profile on a soldered platform UART (e.g. /dev/ttyS1) is likewise not
        # produced by the scan, so fold those in too — after the scanned set, so a port
        # pyserial *does* enumerate keeps its richer scanned row rather than the bare profile.
        listed += _profile_serial_devices(listed, profiles)
        # A radio on the SPI bus answers nothing until MeshTerm starts its node, so it is
        # listed from the device node's presence, the way a serial port is.
        listed += _spi_devices(listed, profiles)
        # Devices the user has told this splash to stop showing (h). Read each pass, so a
        # row hidden a moment ago is gone the next time the list is drawn — which is the
        # only feedback hiding needs.
        hidden = store.hidden_ids()
        # In display order, so a shortcut acting on a row can say what the row *after* it is.
        order = _order([d for d in listed if d.stable_id not in hidden], registry)
        items = _build_items(order, remembered, registry, hidden=len(hidden))
        return order, items, remembered, registry

    async def keep_looking(redraw: Callable[[list], None]) -> None:
        """Re-enumerate while the splash is up, and redraw when the answer changes.

        The screen used to scan once on the way in and never again, so a radio plugged in
        ten seconds late was invisible until MeshTerm was restarted -- and the screen that
        said so offered only a network address typed by hand, or quitting. Fetching the
        cable is the obvious thing to do and it was the one thing that did not work.

        Only a change redraws. A poll that finds the same ports is silence, which matters
        because the reader may be part-way through arrowing down the list.
        """
        windows = 0
        waited = 0.0
        while True:
            await asyncio.sleep(_POLL_S)
            before = {d.stable_id for d in wired + wireless}

            # ``comports()`` blocks; off the event loop so the splash stays live.
            wired[:] = await asyncio.to_thread(discover_devices)

            waited += _POLL_S
            if waited >= _BLE_EVERY_S and (windows < _BLE_WINDOWS or not wired + wireless):
                waited = 0.0
                windows += 1
                wireless[:] = await discover_ble_devices(_BLE_WINDOW_S)

            if {d.stable_id for d in wired + wireless} != before:
                _, rows, _, _ = assemble()
                redraw(rows)

    while True:
        ordered, items, remembered, registry = assemble()
        # Preselect the remembered "last known good" device when it is currently attached/in
        # range — unless the last pass asked for a particular row, which a hide does so the
        # highlight lands where the vanished row was rather than jumping back to the default.
        default = next((d for d in ordered if remembered and remembered.matches(d)), None)
        if focus is not None:
            default = focus if focus in ordered else default
            focus = None

        chosen = await ui.select_startup(
            "Select a companion device",
            items,
            default=default,
            banner=load_logo(),
            keys=_SHORTCUTS,
            key_hint=_shortcut_hint(len(store.hidden_ids())),
            live=keep_looking,
            # Said outright because the rows no longer imply it: scrolling is switched on by
            # a row pinning a head block, and none of these do any more.
            hscroll=True,
        )
        # Esc (``None``) and the Quit row both mean "leave the picker" — surface that to the
        # caller as ``None`` so it can exit the program instead of continuing device-less.
        if chosen is None or chosen is _QUIT:
            return None

        if isinstance(chosen, KeyRequest):
            focus = _run_shortcut(store, chosen, ordered)
            continue

        if chosen is _ADD_TCP:
            # Name a network companion by hand and smoke-test it. On success it's returned like
            # any picked device; on cancel/failure we loop back to the list.
            added = await _add_network_device(ui, store, registry, verify)
            if added is not None:
                return added
            continue

        if isinstance(chosen, DeleteRequest):
            # Delete was pressed on a removable (network) row: confirm, forget, and re-draw
            # the list — the row's disappearance is the visible feedback. The list is passed
            # through so the confirm floats over it (the row it removes stays highlighted).
            await _remove_network_device(ui, store, registry, chosen.value, backdrop_items=items)
            continue

        name = _display_name(chosen, registry)
        # A human phrase for the transport: an address/endpoint reads worse than a plain word.
        where = _where_phrase(chosen)
        # Smoke-test the choice in place (prompting for a PIN and retrying if it needs one).
        # ``None`` means the smoke test failed and already showed the user why — pick again.
        info = await _smoke_test(ui, chosen, name, where, verify)
        if info is None:
            continue

        # Confirmed: remember it forever. We return straight away, so there's no need to
        # fold it back into the local registry for a re-render.
        store.remember(chosen, node_name=_node_name_from(info), hardware_model=_model_from(info))
        return chosen


def _run_shortcut(
    store: DeviceStore, request: KeyRequest, listed: list[DiscoveredDevice]
) -> DiscoveredDevice | None:
    """Act on ``h`` / ``⇧H``, and say which row the redrawn list should open on.

    Nothing is *said* about either: after a hide the row is gone, after a show-all the hidden
    rows are back, and a dialog acknowledging a change the reader is looking straight at is
    the acknowledgement this app takes out of screens rather than adding to them. What the
    redraw does owe them is their place — see :func:`_after_hiding`.

    Args:
        store: The registry the hidden set lives in.
        request: The shortcut the splash resolved with.
        listed: The devices as displayed, in display order.

    Returns:
        The device to highlight when the list is redrawn, or ``None`` to let the remembered
        default decide (which is what a show-all wants: the list it restores is a different
        list, and the reader's place in the old one means nothing in it).
    """
    if request.action is _SHOW_ALL:
        store.show_all()
        return None
    if request.action is _HIDE and isinstance(request.value, DiscoveredDevice):
        # Only a device row can be hidden — ``h`` on Add a network device or Quit is inert,
        # the way Delete is on a row that never opted into removal.
        store.hide(request.value.stable_id)
        return _after_hiding(listed, request.value)
    return None


def _after_hiding(
    listed: list[DiscoveredDevice], hidden: DiscoveredDevice
) -> DiscoveredDevice | None:
    """The row the highlight should land on once ``hidden`` leaves the list.

    The next device down, so a run of adapters can be cleared with a run of presses without
    the hand moving; the one above where there is no next, because the highlight has reached
    the end and there is nowhere further down to go. Neither ever rolls round to the top —
    nothing in the app does — and an emptied list has nothing to land on at all.
    """
    remaining = [d for d in listed if d.stable_id != hidden.stable_id]
    if not remaining:
        return None
    at = next((i for i, d in enumerate(listed) if d.stable_id == hidden.stable_id), 0)
    return remaining[min(at, len(remaining) - 1)]


def _shortcut_hint(hidden: int) -> Callable[[object], str]:
    """Name the hide shortcuts that would actually do something right here, right now.

    Asked of the highlighted row on every paint (see ``key_hint`` on
    :class:`~meshterm.ui.tui.select.SelectScreen`), so the footer follows the same rule
    ``Del remove`` does: a key is named where it acts and nowhere else.

    * ``h hide`` only on a device row — on *Add a network device* or *Quit* there is nothing
      to hide, and the key is inert there.
    * ``⇧H show all`` only while something is actually hidden. It is screen-wide rather than
      per-row (a hidden row is not there to press a key on), so it rides whichever row the
      reader is standing on — but it stays off the line entirely while it would do nothing,
      which is also what keeps the splash from ever naming a key nobody could act on.
    """

    def atoms_for(value: object) -> str:
        atoms = []
        if isinstance(value, DiscoveredDevice):
            atoms.append("h hide")
        if hidden:
            atoms.append("⇧H show all")
        return " · ".join(atoms)

    return atoms_for


def _hidden_note(hidden: int) -> Separator:
    """The muted row that stands in for a list hiding has emptied.

    Only then. With devices still listed, the footer already names ``⇧H`` on every row and a
    standing line saying the same thing is a row of the box spent on chrome. With *nothing*
    listed there is no footer atom to read it off — and "no companion devices detected" would
    be a lie about a machine with a radio plugged into it.
    """
    it = "it" if hidden == 1 else "them"
    device = "device" if hidden == 1 else "devices"
    return Separator(f"  {hidden} {device} hidden — press ⇧H to show {it}")


def _where_phrase(device: DiscoveredDevice) -> str:
    """A human phrase for a device's transport, woven into the smoke-test spinner line."""
    if device.is_tcp:
        return f"at {device.target}"
    if device.is_ble:
        return "over Bluetooth"
    return f"on {device.port}"


def _remembered_tcp_devices(
    discovered: list[DiscoveredDevice], registry: dict[str, RememberedDevice]
) -> list[DiscoveredDevice]:
    """Rebuild the confirmed TCP companions from the registry as :class:`DiscoveredDevice`.

    TCP companions don't advertise, so the scan never finds them; this reconstructs each
    remembered one from its stored ``host:port`` (carrying its known node name for the DEVICE
    column) so it reappears in the picker. Any that happen to already be in ``discovered``
    (e.g. re-run within a session) are skipped so they aren't listed twice.
    """
    seen = {d.stable_id for d in discovered}
    rebuilt: list[DiscoveredDevice] = []
    for record in registry.values():
        if not record.is_tcp or not record.host or not record.tcp_port:
            continue
        device = tcp_device(record.host, record.tcp_port, name=record.node_name)
        if device.stable_id not in seen:
            rebuilt.append(device)
    return rebuilt


def _profile_tcp_devices(
    listed: list[DiscoveredDevice],
    profiles: Mapping[str, DeviceProfile] | None,
) -> list[DiscoveredDevice]:
    """Rebuild configured TCP profiles as :class:`DiscoveredDevice` rows for the picker.

    A ``[profiles.<alias>]`` block with ``transport = "tcp"`` names a network companion the
    user wants to reach — but a TCP endpoint isn't discoverable, so without this it would only
    ever surface via ``meshterm -p <alias>`` on the command line, never in the interactive
    splash. Each such profile is turned into a device carrying its alias as the DEVICE-column
    name (that is how the user addresses it), so it lists like a hand-added network device and
    smoke-tests the same way. Any profile whose endpoint is already present — scanned, or a
    remembered companion carrying its real node name — is skipped so the richer existing row
    wins rather than being duplicated by the bare profile.

    Args:
        listed: The devices already gathered (scanned + remembered), for de-duplication.
        profiles: The configured profiles, or ``None`` when none are loaded.

    Returns:
        One TCP :class:`DiscoveredDevice` per not-yet-listed TCP profile, in profile order.
    """
    if not profiles:
        return []
    seen = {d.stable_id for d in listed}
    rebuilt: list[DiscoveredDevice] = []
    for profile in profiles.values():
        if not profile.is_tcp or not profile.host:
            continue
        port = profile.tcp_port or DEFAULT_TCP_PORT
        device = tcp_device(profile.host, port, name=profile.name)
        if device.stable_id in seen:
            continue
        seen.add(device.stable_id)
        rebuilt.append(device)
    return rebuilt


def _profile_serial_devices(
    listed: list[DiscoveredDevice],
    profiles: Mapping[str, DeviceProfile] | None,
) -> list[DiscoveredDevice]:
    """Rebuild configured serial profiles as :class:`DiscoveredDevice` rows for the picker.

    A ``[profiles.<alias>]`` serial block names a companion on a fixed port. A soldered
    platform-bus UART (e.g. ``/dev/ttyS1`` on the Luckfox Lyra) is invisible to pyserial's scan,
    so without this it would only ever surface via ``meshterm --port``/``-p`` on the command
    line, never in the interactive splash. Each such profile becomes a device carrying its alias
    as the DEVICE-column name, listed like a scanned port and smoke-tested the same way —
    mirroring :func:`_profile_tcp_devices` for network companions. De-duplication is by **port**
    (a scanned USB device keys on its serial/VID:PID, not its port name): a profile whose port is
    already present — pyserial *does* enumerate it, or it is remembered — is skipped so the
    richer existing row wins rather than being duplicated by the bare profile.

    Args:
        listed: The devices already gathered (scanned + remembered + TCP profiles), for
            de-duplication by port.
        profiles: The configured profiles, or ``None`` when none are loaded.

    Returns:
        One serial :class:`DiscoveredDevice` per not-yet-listed serial profile, in profile order.
    """
    if not profiles:
        return []
    seen_ports = {d.port for d in listed if d.port}
    rebuilt: list[DiscoveredDevice] = []
    for profile in profiles.values():
        if profile.is_tcp or profile.is_ble or not profile.port:
            continue
        if profile.port in seen_ports:
            continue
        seen_ports.add(profile.port)
        rebuilt.append(serial_device(profile.port, name=profile.name))
    return rebuilt


def _spi_devices(
    listed: list[DiscoveredDevice],
    profiles: Mapping[str, DeviceProfile] | None,
) -> list[DiscoveredDevice]:
    """The radios on this machine's own SPI bus, as picker rows.

    One row per ``transport = "spi"`` profile whose ``/dev/spidev*`` node exists, named by
    the profile; and, where no profile covers it, one for the uConsole AIO's device node when
    it exists, wired with the defaults. Nothing is probed to list them — a radio on the bus
    has no node running until one is chosen — so a row appears exactly when there is a device
    node to open. A device node already listed is not listed twice.

    Args:
        listed: The devices already gathered, for de-duplication.
        profiles: The configured profiles, or ``None`` when none are loaded.

    Returns:
        One SPI :class:`DiscoveredDevice` per radio not already listed.
    """
    seen = {d.stable_id for d in listed}
    rows: list[DiscoveredDevice] = []
    wirings = [(p.spi or SpiWiring(), p.name) for p in (profiles or {}).values() if p.is_spi]
    covered = {wiring.spidev for wiring, _name in wirings}
    if SpiWiring().spidev not in covered:
        wirings.append((SpiWiring(), ""))
    for wiring, name in wirings:
        if not spi_present(wiring):
            continue
        device = spi_device(wiring, name=name)
        if device.stable_id in seen:
            continue
        seen.add(device.stable_id)
        rows.append(device)
    return rows


async def _add_network_device(
    ui: Ui,
    store: DeviceStore,
    registry: dict[str, RememberedDevice],
    verify: Verify,
) -> DiscoveredDevice | None:
    """Collect a ``host:port``, smoke-test the network companion there, and remember it.

    A network (TCP) companion is named by hand — it isn't attached and doesn't advertise — so
    this opens a text prompt on the splash, parses the endpoint (a bare host defaults its
    port), and runs the same smoke test the discovered transports use. On success the device
    is remembered forever and returned; on a cancelled prompt or a failed test the caller
    re-opens the device list.

    Args:
        ui: The interactive surface for the prompt, spinner, and notices.
        store: The confirmed-device registry, updated once the device answers.
        registry: The current registry, for naming the smoke-test spinner line.
        verify: The smoke-test callback (see :data:`Verify`).

    Returns:
        The confirmed TCP :class:`DiscoveredDevice`, or ``None`` to return to the device list.
    """

    def _validate(text: str) -> object:
        try:
            parse_tcp_endpoint(text)
        except ValueError as exc:
            return str(exc)
        return True

    entered = await ui.prompt_text_startup(
        "Add a network device",
        prompt="Enter the companion's network address:",
        validate=_validate,
        help_text=f"host or host:port — the port defaults to {DEFAULT_TCP_PORT}",
        banner=load_logo(),
    )
    if entered is None:
        return None  # cancelled → back to the device list
    host, port = parse_tcp_endpoint(entered)  # already validated above
    device = tcp_device(host, port)
    name = _display_name(device, registry)
    info = await _smoke_test(ui, device, name, _where_phrase(device), verify)
    if info is None:
        return None  # not a reachable companion — the smoke test already explained why
    store.remember(device, node_name=_node_name_from(info), hardware_model=_model_from(info))
    return device


async def _remove_network_device(
    ui: Ui,
    store: DeviceStore,
    registry: dict[str, RememberedDevice],
    device: DiscoveredDevice,
    *,
    backdrop_items: list,
) -> None:
    """Confirm and forget a remembered network (TCP) device, dropping it from the picker.

    Removal is offered only on network rows: a TCP companion is listed solely from its
    remembered endpoint, so forgetting it is what makes it leave the picker — a scanned serial
    or BLE device would just reappear on the next scan. Opens the reserved-red Cancel/Remove
    confirm as a modal popup floating over the device list (``backdrop_items``, with the row
    being removed left highlighted), so it reads as a dialog on top of the picker rather than a
    splash that replaces it. On Remove the record is pruned from the store, on Cancel/Esc
    nothing changes. Either way the caller re-opens the list, so the row's absence is the
    feedback.

    Args:
        ui: The interactive surface for the confirm dialog.
        store: The confirmed-device registry to prune.
        registry: The current registry, for naming the device in the prompt.
        device: The network device the user asked to remove.
        backdrop_items: The picker's rows, redrawn behind the floating confirm.
    """
    if not device.is_tcp:
        return  # defensive: only network rows opt into deletion (see _build_items)
    name = _display_name(device, registry)
    confirmed = await ui.confirm_startup(
        f"Remove {name} ({device.target}) from the device list?",
        title="Remove network device",
        confirm_label="Remove",
        banner=load_logo(),
        backdrop_items=backdrop_items,
        backdrop_default=device,
    )
    if confirmed:
        store.forget(device.stable_id)


#: The narrowest the HARDWARE column is worth keeping before the name lane has to give way.
#: Below this a model string says nothing at all, and the lane is better spent on the name.
_HARDWARE_MIN = 10

#: The narrowest the PORT / ADDRESS lane is worth shrinking to before DEVICE has to give.
#: This is the lane that adapts: a target is how you tell two similar rows apart, and ten
#: cells still does that (the tail of a UUID, the end of a port path), while a *name* is
#: what the reader actually came to read. macOS is why any of this is needed —
#: CoreBluetooth reports no MAC at all but a per-machine 36-cell UUID, and even its ports
#: run long (every Mac carries a 31-cell ``/dev/cu.Bluetooth-Incoming-Port``) — where a MAC
#: is 17 and ``/dev/ttyUSB0`` is 12, so no other platform ever presses on the row at all.
_ADDRESS_MIN = 10


def _lane_widths(
    devices: list[DiscoveredDevice], registry: dict[str, RememberedDevice]
) -> tuple[int, int]:
    """The DEVICE and PORT / ADDRESS widths: the name in full, the address taking the rest.

    The row has a fixed budget, and something has to give when the two lanes together
    outrun it. **The address gives first.** A name is what the reader came to read and what
    they recognise their own radio by; an address is a disambiguator, and a disambiguator
    does its whole job from its last ten cells (:func:`_fit_target` keeps the tail for
    exactly this reason). So DEVICE is sized to the longest name it has, and ADDRESS takes
    whatever is left over, down to :data:`_ADDRESS_MIN`.

    Only once the address is down to that floor does the name start ellipsizing, and it
    still must: a USB adapter's own product string runs to forty-odd characters ("CP2102
    USB to UART Bridge Controller"), and a lane sized to *that* pushed everything after it
    off the right-hand edge — the row ended mid-port and the HARDWARE column that says what
    the thing actually *is* was not on screen, nor reachable by scrolling, because the
    pinned head was already wider than the box. HARDWARE keeps :data:`_HARDWARE_MIN` out of
    the budget so it always has a readable stub; it is the row's scrolling tail, so beyond
    that stub it is free to run long.

    Args:
        devices: The devices being listed.
        registry: Confirmed companions, for their remembered names.

    Returns:
        ``(name_w, port_w)`` in cells.
    """
    platform = get_platform()
    # What the splash's box gives a row: the terminal, less the gutter it floats over, less
    # its own border and padding, less the pointer and star columns the row leads with.
    content = platform.readable_cols - platform.dialog_margin - 4 - 4
    # ...less the three two-cell gaps, the TYPE badge, and HARDWARE's readable stub.
    budget = content - 6 - len("TYPE") - _HARDWARE_MIN

    name_natural = max(cell_len(_display_name(d, registry)) for d in devices)
    port_natural = max(cell_len(_where(d)) for d in devices)
    # Never reserve more room for the address than it actually wants: a lone "COM3" should
    # hand its slack to the name rather than sit in a ten-cell lane holding four cells.
    floor = min(port_natural, _ADDRESS_MIN)

    name_w = max(len("DEVICE"), min(name_natural, budget - floor))
    port_w = max(floor, min(port_natural, budget - name_w))

    if name_w < name_natural and port_w < port_natural:
        # The name is being ellipsized whatever happens, so spending two more of its cells
        # to keep the address whole trades one cut for none rather than adding a second.
        port_w = min(port_natural, budget - len("DEVICE"))
        name_w = max(len("DEVICE"), budget - port_w)
    return name_w, port_w


def _order(
    devices: list[DiscoveredDevice], registry: dict[str, RememberedDevice]
) -> list[DiscoveredDevice]:
    """Confirmed companions first (most-recently-used first), then everything else as found.

    The devices we've actually spoken to are the ones the user almost always wants, so they
    rise to the top ordered by their last-connected timestamp (newest first). Unknown ports
    keep their incoming discovery order — a stable sort preserves it since they all tie.
    """
    known = [d for d in devices if d.stable_id in registry]
    others = [d for d in devices if d.stable_id not in registry]
    known.sort(key=lambda d: registry[d.stable_id].last_connected, reverse=True)
    return known + others


def _build_items(
    devices: list[DiscoveredDevice],
    remembered: RememberedDevice | None,
    registry: dict[str, RememberedDevice],
    *,
    hidden: int = 0,
) -> list:
    """Build the aligned splash rows (a muted header + one :class:`Choice` per device).

    The row for each device leads with its display name (the remembered node's name when
    known, else the hardware name), followed by its connection target, a TYPE glyph marking
    the transport, and the HARDWARE column (the remembered firmware model, else the USB
    vendor); columns are padded to a shared width so they align. Confirmed companions sort to
    the top (most-recent first), wear their name in white and a bright tag; the remembered
    default is starred. Trailing rows let the user name a network device by hand and quit here.

    A device row's fixed lanes are pinned and its HARDWARE tail scrolls: a firmware model
    string is as long as its vendor felt like making it, and on a narrow terminal the column
    that says *what the thing actually is* was the one being cut off. ←→ read it to its end,
    on the highlighted row alone, and only while that row overflows.

    Args:
        devices: The devices to list — already less anything hidden.
        remembered: The last-connected device, starred and preselected when present.
        registry: Every confirmed companion, keyed by stable id.
        hidden: How many devices were left out for being hidden. Only the empty state uses
            it, to tell "nothing is plugged in" apart from "you hid all of it".
    """
    if not devices:
        # Nothing attached or in range — but a TCP companion can still be reached by hand, so
        # show a muted note over the same action rows rather than a dead-end. When the list is
        # empty *because everything in it is hidden*, say that instead: "nothing detected"
        # would be a lie, and the way back is a key the reader has to be told about.
        note = (
            _hidden_note(hidden)
            if hidden
            else Separator("    no companion devices detected — add a network device, or quit")
        )
        return [note, *_action_rows()]
    devices = _order(devices, registry)
    known: set[str] = set(registry)

    # The middle column holds a serial port, a BLE address, or a TCP host:port; label it for
    # whichever kinds are present so a non-serial endpoint never sits under a bare "PORT"
    # heading (a BLE address and a network host:port both read as an "address").
    has_serial = any(not d.is_ble and not d.is_tcp for d in devices)
    has_address = any(d.is_ble or d.is_tcp for d in devices)
    port_label = (
        "PORT / ADDRESS" if has_serial and has_address else "ADDRESS" if has_address else "PORT"
    )
    # The TYPE column holds a small transport badge (at most 3 cells); its heading is wider,
    # so the four-cell "TYPE" label sets the column width and every badge pads out to it.
    type_w = len("TYPE")
    # DEVICE in full, ADDRESS taking what is left (see _lane_widths). The heading does not
    # set a floor here the way it used to: the lane may end up narrower than "PORT / ADDRESS"
    # spells, and column_header already carries the short form to fall back on.
    name_w, port_w = _lane_widths(devices, registry)
    hardware_w = max(cell_len(_hardware_label(d, registry)) for d in devices)
    hardware_w = max(hardware_w, len("HARDWARE"))

    # A muted, aligned header, resolved against the render width because it is *pinned*: it
    # leads the device rows as their landmark, so a long detection list keeps the lane names
    # overhead as it scrolls — and a pinned row that wraps is drawn outside the body slice,
    # where the second line costs the list a device. The indent mirrors the row pointer (2)
    # and the star column (2) so each label sits over its own lane.
    header = Separator(
        lambda width: column_header(
            [
                Lane("DEVICE", name_w + 2),
                Lane((port_label, "PORT" if has_serial else "ADDRESS"), port_w + 2),
                Lane("TYPE", type_w + 2),
                Lane(("HARDWARE", "HW")),
            ],
            width,
            indent=4,
        ),
        heading=True,
    )

    items: list = [header]
    for device in devices:
        is_remembered = remembered is not None and remembered.matches(device)
        is_known = device.stable_id in known
        row = Text()
        row.append("★" if is_remembered else " ", style="warn" if is_remembered else "")
        row.append(" ")
        # A confirmed companion wears its name in white so it stands out from mere detections.
        row.append(
            _pad(fit_cells(_display_name(device, registry), name_w), name_w),
            style="device.known" if is_known else "",
        )
        row.append("  ")
        row.append(_pad(_fit_target(_where(device), port_w), port_w), style="muted")
        row.append("  ")
        # The TYPE badge marks the transport: a plug emoji for serial, or the Bluetooth rune
        # on its blue badge for BLE. It's built with its own colours, then the column is
        # padded with plain spaces — so the blue fill hugs just the badge, and the differing
        # badge widths (emoji 2 cells, rune-plus-edges 3) still line up under "TYPE".
        cell = _type_cell(device)
        row.append_text(cell)
        row.append(" " * max(0, type_w - cell.cell_len))
        row.append("  ")
        # Nothing is pinned here, so ←→ slide the whole row. The Trophy case pins its
        # rank/date/score lanes because those always fit and only the walk overflows, which
        # makes them the reader's place in a long list. This list inverts that: on a narrow
        # terminal it is the device name and the address that get cut, so pinning them would
        # pin the truncation in place and leave the one thing ←→ could not reach being the
        # text somebody most wants to finish reading.
        row.append(_pad(_hardware_label(device, registry), hardware_w), style="muted")
        # Only devices we've actually confirmed are billed as MeshCore companions; a USB
        # vendor ID (or a BLE advert) is a sort hint, not a claim. A bare serial bridge earns
        # an honest label; a MeshCore-named BLE advert is flagged as a likely companion.
        if is_known:
            row.append("  ")
            row.append("· MeshCore device", style="ok")
        elif device.is_tcp:
            row.append("  ")
            row.append("· network companion", style="muted")
        elif device.is_ble:
            row.append("  ")
            row.append("· Bluetooth companion", style="muted")
        elif device.confidence == "bridge":
            row.append("  ")
            row.append("· serial adapter", style="muted")
        # Only a network device opts into Delete-to-remove: it's listed solely from its
        # remembered endpoint, so forgetting it is the only way it leaves the picker. A scanned
        # serial/BLE device would just reappear, so Delete stays inert on those rows.
        items.append(Choice(title=row, value=device, deletable=device.is_tcp))
    items.extend(_action_rows())
    return items


def _action_rows() -> list:
    """The trailing splash rows: name a network device by hand, then quit.

    A network (TCP) companion doesn't advertise and isn't attached, so it can't be scanned
    for — the "add a network device" row opens a host:port prompt to name one. The Quit row
    mirrors the main menu. The leading spaces line both up under the device-name column, and
    the icons share one measured column (:func:`~meshterm.ui.menus.align_icons`): ``🌐`` and
    ``🚪`` happen to be the same width today, and the words stay aligned if one changes. Where
    the platform draws no icon lane the icons go, padding and all, like every command row's.
    """
    add_label, quit_label = align_icons([f"{_TCP_ICON} Add a network device…", "🚪 Quit"])
    return [
        Separator(" "),
        Choice(title=Text.assemble("  ", add_label), value=_ADD_TCP),
        Choice(title=Text.assemble("  ", quit_label), value=_QUIT),
    ]
