# SPDX-License-Identifier: Apache-2.0
"""Interactive picker for the companion device: the startup splash before the menu.

The picker shows one time, at the start of the interactive menu, when no port was given
explicitly. It is different from the prompts in the menu: it is drawn as a chromeless
splash. That is, the MeshTerm wordmark is centred above a box that has the size of its
content, and there are no status bars in a header or a footer.

The splash lists the discovered devices in aligned columns: a TYPE glyph (serial,
Bluetooth, network, or SPI), the name, a HARDWARE column (the firmware model of the
confirmed device, else the USB vendor), and the connection target last. It tags the
devices that are already confirmed as MeshCore companions. It marks the remembered "last
known good" device, and it preselects that device as the default. The splash never
shortens a field to make it fit: a row that is wider than the box goes past its edge, and
the ←→ keys pan all the rows together to show the rest.

Confirmed companions are at the top, the most recently used first. Each one shows the mesh
node name that MeshTerm learned when it last talked to the companion. This name is white,
so that it is different from the ports that MeshTerm only detected. The other devices
follow: first the devices that look like companions, then the rest, each group in
discovery order.

When the user selects a device, a smoke test runs immediately (through the ``verify``
callback). If the device is a real MeshCore companion, MeshTerm remembers it as confirmed,
permanently, and it becomes the active device of the session. If not, the user goes back
to the list to select another device.

Some machines have devices that are always connected and are not companions: a debug
probe, a programmer, a serial adapter. The splash shows them each time, in front of the one
device that the user wants. Thus a row can be **hidden**: ``h`` removes the highlighted
device from this list and remembers that choice (refer to
:meth:`~meshterm.core.device_store.DeviceStore.hide`), and ``⇧H`` shows all the hidden
devices again. This choice is about the list only. A hidden device is still remembered,
you can still reach it with ``--port``, and it is not hidden any more as soon as MeshTerm
connects to it again.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text

from ..core.config import DeviceProfile
from ..core.connection import (
    DeviceAuthenticationError,
    DeviceCommandError,
    UnrecognisedConnectError,
)
from ..core.device_store import DeviceStore, RememberedDevice
from ..core.discovery import (
    DEFAULT_TCP_PORT,
    DiscoveredDevice,
    discover_ble_devices,
    discover_devices,
    parse_tcp_endpoint,
    serial_device,
    tcp_device,
)
from ..core.spiradio import spi_radios, without_radio_ports
from ..persistence.logging import log_file_for
from .logo import load_logo
from .menus import Lane, align_icons, column_header
from .tui import Choice, DeleteRequest, KeyRequest, Separator

if TYPE_CHECKING:
    from .surface import Ui

#: A smoke test. It probes a selected device, with an optional Bluetooth PIN. It returns the
#: self-info dict of the device if it is a MeshCore companion, else ``None``. The caller
#: supplies it, so that this UI module has no connection logic. It can raise
#: :class:`DeviceAuthenticationError` when the device must have a PIN (then the picker asks
#: for one and tries again with it). It can also raise another :class:`DeviceCommandError`
#: for a different failure that the user can act on (the picker shows it word for word).
#: The second argument is the PIN to try, or ``None`` to use the default that the caller
#: has.
Verify = Callable[[DiscoveredDevice, str | None], Awaitable[dict | None]]

#: The value of the Quit row. When the user selects it, the user leaves the splash without
#: a device, the same as with the Esc key. The caller treats this result as "exit the
#: program".
_QUIT = object()

#: The value of the "add a network device" row. When the user selects it, a host:port
#: prompt opens (discovery cannot find TCP companions, so the user types their address),
#: and a smoke test examines the result.
_ADD_TCP = object()

#: The two shortcut tokens that the splash declares, and the keys that send them. ``h`` acts
#: on the highlighted row. ``⇧H`` acts on the full screen, because a hidden row is not there
#: for a key press. The user must be able to show the hidden rows again from any row in the
#: list.
_HIDE = object()
_SHOW_ALL = object()
_SHORTCUTS = {"h": _HIDE, "H": _SHOW_ALL}

#: Glyphs for the TYPE column, which show how a device connects. They are module constants,
#: so that you can change the appearance of the splash without a change to the code that
#: builds the rows. ``ᛒ`` is the Bjarkan rune, from which the Bluetooth logo is made. It is
#: rendered white on the Bluetooth blue (refer to the ``bluetooth`` theme style), with the
#: half-blocks below on its two sides, so that it looks like a narrow badge with round ends.
#: ``🔌`` is a plain plug, for a wired serial link.
_BLE_ICON = "ᛒ"
_SERIAL_ICON = "🔌"

#: The TYPE-column glyph for a TCP companion: a globe. It shows a device that MeshTerm
#: reaches over the network, not over a wired or Bluetooth link. It is two cells wide, the
#: same as the serial plug, so it aligns the same way.
_TCP_ICON = "🌐"

#: The TYPE-column glyph for a radio on the SPI bus of the host: a pin, because the radio is
#: here, on this machine. It is two cells wide, so it aligns with the plug and the globe.
_SPI_ICON = "📍"

#: Half-block glyphs that make the ends of the Bluetooth badge narrower. ``▐`` fills the
#: right half of a cell, so it touches the left edge of the rune. ``▌`` fills the left half,
#: so it touches the right edge. They are drawn in the blue of the badge on the terminal
#: background, and they make the blue half a cell wider on each side.
_BADGE_LEFT = "▐"
_BADGE_RIGHT = "▌"


def _type_cell(device: DiscoveredDevice) -> Text:
    """The transport badge at the start of the row of ``device``, as a styled fragment.

    Serial is a bare plug emoji (two cells, in its own colour). Bluetooth is the rune on its
    blue badge, with a narrow half-block on each side in the same blue. Thus the fill looks
    like a chip with slightly round ends, a little wider than the rune alone, instead of one
    cell with hard edges.

    The plug has a space before it, so that its two cells are centred on the Bluetooth rune
    (which the left half-block of the badge already moves one cell to the right), instead
    of at the left edge of the column.
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
    """Pad ``text`` on the right with spaces to ``width`` cells.

    The function counts the true width of a wide character.
    """
    return text + " " * max(0, width - cell_len(text))


def _hardware_name(device: DiscoveredDevice) -> str:
    """The product name of the device, without the ``(port)`` at its end.

    The port has its own column.
    """
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

    Each device that MeshTerm confirmed before (not only the most recent one) shows the
    mesh node name that MeshTerm learned when it connected. Thus a known ``COM11`` shows as
    "BaseStation", instead of the generic "USB Serial Device" of the operating system. A
    device with no record (or with an empty remembered name) shows its hardware or product
    name instead.
    """
    record = registry.get(device.stable_id)
    if record is not None and record.node_name:
        return record.node_name
    return _hardware_name(device)


def _where(device: DiscoveredDevice) -> str:
    """The connection target in the last column: a serial port, a BLE address, or a host:port."""
    return device.target


def _hardware_label(device: DiscoveredDevice, registry: dict[str, RememberedDevice]) -> str:
    """The text of the HARDWARE column: the remembered firmware model, else the USB vendor.

    A confirmed device shows what it is ("Seeed Tracker T1000-E", from the device query when
    MeshTerm connected). This source is the only reliable one, because the BLE
    advertisement of a companion does not name its maker. A device that MeshTerm never
    connected to has no stored model, so it shows the USB vendor name instead. For a BLE
    advertisement from a device that MeshTerm did not connect to, this name is blank,
    because the advertisement does not tell anything yet.
    """
    record = registry.get(device.stable_id)
    if record is not None and record.hardware_model:
        return record.hardware_model
    return device.vendor_label


def _node_name_from(info: dict) -> str:
    """Return the mesh node name of a device from its self-info payload, or ``""``."""
    return str(info.get("adv_name") or info.get("name") or "")


def _model_from(info: dict) -> str:
    """Return the hardware model that the firmware tells, from the probe payload, or ``""``.

    The probe of the smoke test puts the ``model`` of the device query into the identity
    dict. Thus this payload is the only chance to learn (and then remember) what the
    hardware is.
    """
    return str(info.get("model") or "")


#: The sentence of a connection failure starts in lower case, because it is written for the
#: ``meshterm: …`` of the command line. A dialog shows it as a sentence, so a plain first
#: word gets a capital letter. Only a plain word changes: a port, an address, or a host
#: name at the start keeps its spelling.
_LEADING_WORD = re.compile(r"^[a-z][a-z']*(?=\s)")


def connect_failure_text(exc: BaseException) -> Text:
    """The reason why a connection failed, as a dialog shows it.

    The sentence comes from the connection itself: it is the first
    :class:`~meshterm.core.connection.DeviceCommandError` in the chain, below the errors that
    wrap it. (The ``could not open …`` of the context only repeats the endpoint that the
    sentence names.) It is plain text in the ``warn`` style, so that brackets in the words
    of a library show as brackets and are not parsed as markup. When MeshTerm had no name for
    the failure (:class:`~meshterm.core.connection.UnrecognisedConnectError`), a muted line
    follows that tells where the log is, because the log has the traceback. A failure with a
    name gets no such line, because then the dialog already tells all that the log tells.

    Args:
        exc: The exception that the connection attempt raised.

    Returns:
        The lines of the dialog.
    """
    named: DeviceCommandError | None = None
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, DeviceCommandError):
            named = current
            break
        current = current.__cause__ or current.__context__
    sentence = str(named or exc).strip() or type(exc).__name__
    sentence = _LEADING_WORD.sub(lambda word: word.group().capitalize(), sentence, count=1)
    text = Text()
    text.append(sentence, style="warn")  # a span, not the base: text added later stays plain
    if isinstance(named, UnrecognisedConnectError):
        log = log_file_for(logging.WARNING)
        if log is not None:
            text.append(f"\nThe full error is in {log}", style="muted")
    return text


async def _smoke_test(
    ui: Ui,
    chosen: DiscoveredDevice,
    name: str,
    where: str,
    verify: Verify,
) -> dict | None:
    """Smoke-test ``chosen`` behind the splash spinner, and get a Bluetooth PIN if necessary.

    Run the ``verify`` probe on the chromeless splash. The wordmark and the box of the splash
    do not change, and an animated spinner replaces the device list. If the device answers
    but asks for a pairing PIN, open the :class:`~meshterm.ui.tui.prompt.PinDialog` dialog
    and try again with the PIN that the user types. If the device refuses again, open the
    dialog again with the hint of the new error. Continue until the device connects or the
    user presses Esc.

    Args:
        ui: The interactive surface for the spinner, the PIN dialog, and the failure notices.
        chosen: The device to test.
        name: Its display name, which goes into the spinner line and the PIN prompt.
        where: A plain phrase that names its transport ("over Bluetooth" / "on COM5").
        verify: The smoke-test callback (refer to :data:`Verify`). The function calls it
            with the PIN to try.

    Returns:
        The self-info dict of the device when it answers. Or ``None``, after the related
        notice shows, to send the user back to the device list (when the device is not a
        MeshCore endpoint, when the failure is not recoverable, or when the user cancelled
        the PIN prompt).
    """
    pin: str | None = None
    while True:
        # A BLE connection and service discovery take a few seconds, so the spinner is most
        # important here.
        try:
            info = await ui.busy_startup(
                f"Talking to {name} {where}…",
                verify(chosen, pin),
                title="Checking companion",
                banner=load_logo(),
            )
        except DeviceAuthenticationError as exc:
            # The device answered the scan, but it will not connect until it is bonded (or the
            # last PIN was wrong). Get a PIN in the dialog, and go through the loop again to
            # try again. The Esc key goes back to the list. The error tells which refusal
            # occurred: a stale bond, a wrong PIN, or a device that refused the pairing. The
            # dialog shows that line below its question, instead of a "rejected PIN" for
            # each refusal.
            entered = await ui.prompt_pin_startup(
                name,
                error=exc.hint,
                help_text="The 6-digit code shown on the device or in the MeshCore app",
                banner=load_logo(),
            )
            if entered is None:
                return None  # the user cancelled → back to the device list
            pin = entered
            continue
        except DeviceCommandError as exc:
            # A different failure that the user can act on (not a PIN). Show its remedy, then
            # go back to the list.
            notice = connect_failure_text(exc)
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


#: How often the splash enumerates the serial ports again. `comports()` walks SetupAPI or
#: sysfs, and it is not a free read, so it runs outside the event loop. But its cost is low
#: enough to repeat it at this rate, and nobody sees a difference. It covers the usual
#: case: a USB radio that the user connects after the splash opened.
_POLL_S = 2.0

#: The cost of Bluetooth is not low in the same way. Each scan is a listen with a time
#: limit. If a scan always runs, the Bluetooth adapter listens for as long as this screen is
#: open. And this screen is the one that a user leaves open while they go to find a cable,
#: on a handheld, on a battery. Thus Bluetooth gets a duty cycle instead of a loop: one
#: time window of scanning at this interval, and only while it is still probable that
#: somebody waits for one.
_BLE_EVERY_S = 15.0
_BLE_WINDOW_S = 4.0

#: How many time windows to run before MeshTerm stops the search for a companion that
#: nobody turned on. When the list has a device, the user has what they came for. While
#: the list is still empty, the count is ignored, because an empty splash is exactly where
#: somebody waits. There, one more listen is worth its cost.
_BLE_WINDOWS = 8


async def prompt_device(
    ui: Ui,
    devices: list[DiscoveredDevice],
    store: DeviceStore,
    verify: Verify,
    profiles: Mapping[str, DeviceProfile] | None = None,
) -> DiscoveredDevice | None:
    """Ask the user to select a companion device on the startup splash.

    MeshTerm runs a smoke test on the selected device before it accepts the device. Only a
    device that answers the MeshCore identity query is returned (and stored as confirmed).
    If a device fails the test, the picker opens again with a message that asks the user to
    select another device.

    Args:
        ui: The interactive UI surface that renders the picker.
        devices: The discovered devices (the likely LoRa devices first).
        store: The registry of confirmed devices. The picker uses it to tag and preselect
            the known devices, and to store a device when its smoke test passes.
        verify: An async smoke test. It returns the self-info dict of a device, or ``None``
            if the device is not a MeshCore companion that MeshTerm can reach.
        profiles: The configured device profiles (``config.toml`` ``[profiles.*]``). Their TCP
            and serial entries are added to the list. Thus a network companion that the user
            wrote by hand shows here, and the user does not have to connect to it first
            (refer to :func:`_profile_tcp_devices` and :func:`_profile_serial_devices`).

    Returns:
        The selected and confirmed :class:`DiscoveredDevice`, or ``None`` if the user left
        the picker without a selection (with the Esc key or the Quit row). The caller treats
        ``None`` as a request to exit.
    """
    # Serial and Bluetooth devices are in different lists, because MeshTerm refreshes them
    # at different intervals. A poll replaces all the attached devices, and a Bluetooth time
    # window replaces all the devices in range. One must not erase the results of the other.
    wired = [d for d in devices if not d.is_ble]
    wireless = [d for d in devices if d.is_ble]
    #: The row that the list must open on the next time that it shows, when the last pass
    #: moved the list under the user (refer to :func:`_after_hiding`). The loop clears it as
    #: soon as it uses it.
    focus: DiscoveredDevice | None = None

    def assemble() -> tuple[
        list[DiscoveredDevice], list, RememberedDevice | None, dict[str, RememberedDevice]
    ]:
        """The list as it is now: the devices that are there, without the hidden devices.

        One function builds it, because two callers use it now: the loop below, and the
        rescan that draws the rows again under the user while the splash is open.
        """
        # A port that a radio on the SPI bus owns (the GPS of the Cap) belongs to that radio.
        # It is not a companion.
        scanned = without_radio_ports(wired, profiles) + wireless
        remembered = store.load()
        # The full registry (not only the last device), keyed by stable_id. With it, each
        # confirmed companion can get its name, its white colour, and its place at the top.
        # The registry is read again on each pass, so that an addition or a removal shows
        # the next time that the list is drawn.
        registry = store.load_all()
        # Discovery cannot find a TCP companion. Thus a TCP companion that was confirmed
        # before shows again only if the code builds it again from its remembered endpoint
        # and adds it to the list with the scanned devices (the scan never finds it). The
        # configured TCP profiles are added in the same way, after the scanned and remembered
        # devices. Thus an endpoint that is already known keeps its remembered row, which
        # has more data, and the profile does not hide it.
        listed = scanned + _remembered_tcp_devices(scanned, registry)
        listed += _profile_tcp_devices(listed, profiles)
        # The scan also does not find a serial profile on a soldered platform UART (for
        # example /dev/ttyS1), so add those profiles too. Add them after the scanned
        # devices, so that a port that pyserial does enumerate keeps its scanned row, which
        # has more data, instead of the bare profile.
        listed += _profile_serial_devices(listed, profiles)
        # A radio on the SPI bus does not answer until MeshTerm starts its node. Thus the
        # list shows it when its device file is present, the same as a serial port.
        listed += spi_radios(profiles, listed)
        # The devices that the user told this splash not to show (h). They are read on each
        # pass, so a row that the user hid a moment ago is gone the next time that the list
        # is drawn. No other feedback is necessary for a hide.
        hidden = store.hidden_ids()
        # In display order, so that a shortcut on a row can tell which row comes after it.
        order = _order([d for d in listed if d.stable_id not in hidden], registry)
        items = _build_items(order, remembered, registry, hidden=len(hidden))
        return order, items, remembered, registry

    async def keep_looking(redraw: Callable[[list], None]) -> None:
        """Enumerate the devices again while the splash is open, and draw again on a change.

        The screen once scanned only one time, when it opened. Thus a radio that the user
        connected ten seconds late did not show until MeshTerm started again. And the screen
        that told the user so offered only a network address to type by hand, or to quit.
        To go and get the cable is the obvious thing to do, and it was the only thing that
        did not work.

        Only a change draws the rows again. A poll that finds the same ports does nothing.
        This is important, because the user may be in the middle of a move down the list
        with the arrow keys.
        """
        windows = 0
        waited = 0.0
        while True:
            await asyncio.sleep(_POLL_S)
            before = {d.stable_id for d in wired + wireless}

            # ``comports()`` blocks, so it runs outside the event loop and the splash stays live.
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
        # Preselect the remembered "last known good" device when it is attached or in range
        # now. But if the last pass asked for a specific row, use that row. A hide does this,
        # so that the highlight goes to the place of the removed row, instead of back to the
        # default.
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
        )
        # The Esc key (``None``) and the Quit row both mean "leave the picker". Return
        # ``None`` to the caller for both, so that it can exit the program, and not continue
        # without a device.
        if chosen is None or chosen is _QUIT:
            return None

        if isinstance(chosen, KeyRequest):
            focus = _run_shortcut(store, chosen, ordered)
            continue

        if chosen is _ADD_TCP:
            # The user types the address of a network companion, and a smoke test examines
            # it. If the test passes, the device is returned like any selected device. If the
            # user cancels or the test fails, the loop goes back to the list.
            added = await _add_network_device(ui, store, registry, verify)
            if added is not None:
                return added
            continue

        if isinstance(chosen, DeleteRequest):
            # The user pressed Delete on a row that can be removed (a network row). Confirm,
            # forget the device, and draw the list again. The removed row is the visible
            # feedback. The list goes to the confirm, so that the confirm floats over it (and
            # the row that it removes keeps the highlight).
            await _remove_network_device(ui, store, registry, chosen.value, backdrop_items=items)
            continue

        name = _display_name(chosen, registry)
        # A plain phrase for the transport, because an address or an endpoint is less clear
        # than a plain word.
        where = _where_phrase(chosen)
        # Smoke-test the selected device here (ask for a PIN and try again if it must have
        # one).
        # ``None`` means that the smoke test failed and already told the user why. Select
        # again.
        info = await _smoke_test(ui, chosen, name, where, verify)
        if info is None:
            continue

        # Confirmed: remember it permanently. The function returns immediately, so it is
        # not necessary to add the device to the local registry for another render.
        store.remember(chosen, node_name=_node_name_from(info), hardware_model=_model_from(info))
        return chosen


def _run_shortcut(
    store: DeviceStore, request: KeyRequest, listed: list[DiscoveredDevice]
) -> DiscoveredDevice | None:
    """Act on ``h`` or ``⇧H``, and tell which row the list must open on when it shows again.

    No message tells about either key. After a hide, the row is gone. After a show-all, the
    hidden rows are back. A dialog that acknowledges a change that the user sees directly is
    the type of acknowledgement that this app removes from screens, and does not add. But
    when the list shows again, it must keep the place of the user (refer to
    :func:`_after_hiding`).

    Args:
        store: The registry that holds the hidden set.
        request: The shortcut that the splash returned.
        listed: The devices as shown, in display order.

    Returns:
        The device that gets the highlight when the list shows again, or ``None`` to let the
        remembered default decide. A show-all wants ``None``, because the list that it
        restores is a different list, and the place of the user in the old list has no
        meaning in it.
    """
    if request.action is _SHOW_ALL:
        store.show_all()
        return None
    if request.action is _HIDE and isinstance(request.value, DiscoveredDevice):
        # Only a device row can be hidden. ``h`` on Add a network device or on Quit does
        # nothing, the same as Delete on a row that did not opt into removal.
        store.hide(request.value.stable_id)
        return _after_hiding(listed, request.value)
    return None


def _after_hiding(
    listed: list[DiscoveredDevice], hidden: DiscoveredDevice
) -> DiscoveredDevice | None:
    """The row that gets the highlight after ``hidden`` leaves the list.

    It is the next device down, so that the user can hide a series of adapters with a series
    of key presses, and the hand does not move. If there is no next device, it is the device
    above, because the highlight is at the end and it cannot go further down. The highlight
    never rolls over to the top (nothing in the app does that). An empty list has no row for
    the highlight.
    """
    remaining = [d for d in listed if d.stable_id != hidden.stable_id]
    if not remaining:
        return None
    at = next((i for i, d in enumerate(listed) if d.stable_id == hidden.stable_id), 0)
    return remaining[min(at, len(remaining) - 1)]


def _shortcut_hint(hidden: int) -> Callable[[object], str]:
    """Name the hide shortcuts that have an effect here, at this time.

    The select screen asks this function about the highlighted row at each paint (refer to
    ``key_hint`` on :class:`~meshterm.ui.tui.select.SelectScreen`). Thus the footer follows
    the same rule as ``Del remove``: the hint names a key where the key acts, and nowhere
    else.

    * ``h hide`` shows only on a device row. On *Add a network device* or *Quit*, there is
      nothing to hide, and the key does nothing there.
    * ``⇧H show all`` shows only while a device is hidden. It acts on the full screen, not on
      one row (a hidden row is not there for a key press). Thus it shows with the highlight,
      on each row. But it is not on the line at all while it has no effect. That rule also
      makes sure that the splash never names a key that nobody can use.
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
    """The muted row that replaces a list that the hide shortcut made empty.

    The row shows only in that case. While the list still has devices, the footer already
    names ``⇧H`` on each row. A permanent line with the same information uses a row of the
    box for chrome. When the list has no devices, there is no footer atom that tells it. And
    "no companion devices detected" is false on a machine that has a radio connected to it.
    """
    it = "it" if hidden == 1 else "them"
    device = "device" if hidden == 1 else "devices"
    return Separator(f"  {hidden} {device} hidden — press ⇧H to show {it}")


def _where_phrase(device: DiscoveredDevice) -> str:
    """A plain phrase for the transport of a device, for the spinner line of the smoke test."""
    if device.is_tcp:
        return f"at {device.target}"
    if device.is_ble:
        return "over Bluetooth"
    return f"on {device.port}"


def _remembered_tcp_devices(
    discovered: list[DiscoveredDevice], registry: dict[str, RememberedDevice]
) -> list[DiscoveredDevice]:
    """Rebuild the confirmed TCP companions from the registry as :class:`DiscoveredDevice`.

    TCP companions do not announce themselves, so the scan never finds them. This function
    builds each remembered TCP companion again from its stored ``host:port`` (with its known
    node name for the DEVICE column), so that it shows in the picker again. If one of them
    is already in ``discovered`` (for example, when the function runs again in the same
    session), the function skips it, so that the list does not show it two times.
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
    """Rebuild the configured TCP profiles as :class:`DiscoveredDevice` picker rows.

    A ``[profiles.<alias>]`` block with ``transport = "tcp"`` names a network companion that
    the user wants to reach. But discovery cannot find a TCP endpoint. Without this
    function, the companion shows only through ``meshterm -p <alias>`` on the command line,
    and never on the interactive splash. Each such profile becomes a device with its
    alias as the name in the DEVICE column (the user uses this alias for it). Thus it shows
    in the list like a network device that the user added by hand, and its smoke test is the
    same.

    If the endpoint of a profile is already in the list (scanned, or a remembered companion
    with its real node name), the function skips the profile. Thus the existing row, which
    has more data, stays, and the bare profile does not make a second copy of it.

    Args:
        listed: The devices already in the list (scanned and remembered), to find duplicates.
        profiles: The configured profiles, or ``None`` when there are no profiles.

    Returns:
        One TCP :class:`DiscoveredDevice` for each TCP profile that is not yet in the list,
        in profile order.
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
    """Rebuild the configured serial profiles as :class:`DiscoveredDevice` picker rows.

    A ``[profiles.<alias>]`` serial block names a companion on a fixed port. The scan of
    pyserial cannot find a UART that is soldered to the bus of the platform (for example
    ``/dev/ttyS1`` on the Luckfox Lyra). Without this function, the companion shows
    only through ``meshterm --port``/``-p`` on the command line, and never on the
    interactive splash. Each such profile becomes a device with its alias as the name in the
    DEVICE column. It shows in the list like a scanned port, and its smoke test is the same.
    This function does for serial companions what :func:`_profile_tcp_devices` does for
    network companions.

    The function finds duplicates by **port**, because a scanned USB device uses its serial
    number or its VID:PID as its key, not its port name. If the port of a profile is already
    in the list (because pyserial does enumerate it, or because it is remembered), the
    function skips the profile. Thus the existing row, which has more data, stays, and the
    bare profile does not make a second copy of it.

    Args:
        listed: The devices already in the list (scanned, remembered, and TCP profiles), to
            find duplicates by port.
        profiles: The configured profiles, or ``None`` when there are no profiles.

    Returns:
        One serial :class:`DiscoveredDevice` for each serial profile that is not yet in the
        list, in profile order.
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


async def _add_network_device(
    ui: Ui,
    store: DeviceStore,
    registry: dict[str, RememberedDevice],
    verify: Verify,
) -> DiscoveredDevice | None:
    """Get a ``host:port``, smoke-test the network companion there, and remember it.

    The user must type the address of a network (TCP) companion, because it is not attached
    and it does not announce itself. Thus this function opens a text prompt on the splash,
    and parses the endpoint (a bare host gets the default port). Then it runs the same smoke
    test as the discovered transports. If the test passes, the function remembers the device
    permanently and returns it. If the user cancels the prompt or the test fails, the caller
    opens the device list again.

    Args:
        ui: The interactive surface for the prompt, the spinner, and the notices.
        store: The registry of confirmed devices. The function updates it when the device
            answers.
        registry: The current registry, for the name in the spinner line of the smoke test.
        verify: The smoke-test callback (refer to :data:`Verify`).

    Returns:
        The confirmed TCP :class:`DiscoveredDevice`, or ``None`` to go back to the device
        list.
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
        return None  # not a reachable companion: the smoke test already told the user why
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
    """Confirm, then forget a remembered network (TCP) device, and remove it from the picker.

    Only network rows offer removal. The list gets a TCP companion only from its remembered
    endpoint, so to forget it is what removes it from the picker. A scanned serial or BLE
    device shows again at the next scan. This function opens the Cancel/Remove confirm
    in the reserved red, as a modal dialog that floats over the device list
    (``backdrop_items``, and the row to remove keeps the highlight). Thus it looks like a
    dialog on top of the picker, not like a splash that replaces it.

    On Remove, the function deletes the record from the store. On Cancel or Esc, nothing
    changes. In both cases the caller opens the list again, so the missing row is the
    feedback.

    Args:
        ui: The interactive surface for the confirm dialog.
        store: The registry of confirmed devices, from which to delete the record.
        registry: The current registry, for the name of the device in the prompt.
        device: The network device that the user asked to remove.
        backdrop_items: The rows of the picker, drawn again behind the floating confirm.
    """
    if not device.is_tcp:
        return  # defensive: only network rows opt into deletion (refer to _build_items)
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


def _tag(device: DiscoveredDevice, is_known: bool) -> tuple[str, str]:
    """The muted qualifier after HARDWARE and its style, or ``("", "")`` for no qualifier.

    Only the devices that MeshTerm confirmed get the label of a MeshCore companion. A USB
    vendor ID (or a BLE advertisement) is a hint for the sort, not a claim. A bare serial
    bridge gets an honest label. A BLE advertisement with a MeshCore name gets the label of
    a likely companion.
    """
    if is_known:
        return "· MeshCore device", "ok"
    if device.is_tcp:
        return "· network companion", "muted"
    if device.is_ble:
        return "· Bluetooth companion", "muted"
    if device.confidence == "bridge":
        return "· serial adapter", "muted"
    return "", ""


def _order(
    devices: list[DiscoveredDevice], registry: dict[str, RememberedDevice]
) -> list[DiscoveredDevice]:
    """Confirmed companions (most recently used first), then likely companions, then the rest.

    The devices that MeshTerm talked to before are almost always the ones that the user
    wants. Thus they go to the top, in the order of their last-connected timestamp (the
    newest first).

    Below the confirmed companions, each device that looks like a companion (a BLE
    advertisement with a MeshCore name, a known board, a radio on the SPI bus) comes before a
    port that nothing confirms, across all the transports. The scan lists serial devices
    first, in the order that it finds them. Thus the bare UART of a board (the
    ``/dev/ttyS0`` of a Cardputer Zero, connected to the GPS of its Cap) once opened the
    splash with the highlight on it, above the Cap itself. Equal devices keep their
    discovery order, because the sort is stable.
    """
    known = [d for d in devices if d.stable_id in registry]
    others = [d for d in devices if d.stable_id not in registry]
    known.sort(key=lambda d: registry[d.stable_id].last_connected, reverse=True)
    others.sort(key=lambda d: not d.is_likely_lora)
    return known + others


def _build_items(
    devices: list[DiscoveredDevice],
    remembered: RememberedDevice | None,
    registry: dict[str, RememberedDevice],
    *,
    hidden: int = 0,
) -> list:
    """Build the aligned splash rows (a muted header and one :class:`Choice` for each device).

    The row of each device starts with a badge that shows the transport. Then come its
    display name (the name of the remembered node when it is known, else the hardware name),
    the HARDWARE column (the remembered firmware model, else the USB vendor) with its tag,
    and the connection target last. The columns are padded to a shared width, so that they
    align. Confirmed companions go to the top (the most recent first), with their name in
    white and a bright tag. The remembered default has a star. The rows at the end let the
    user type the address of a network device, or quit here.

    Each lane has the width of its longest value, and nothing is shortened to make it fit:
    not a name, not a model string, not a heading. Each lane was once made narrower to fit a
    budget, so that the row fitted the box. On a narrow terminal (or next to a 36-cell
    CoreBluetooth UUID), that cut the name that the user wanted to read to its first five
    letters. Now a row that is wider than the box goes past its edge instead.

    The ←→ keys pan the full table: the header and all the device rows together
    (:attr:`~meshterm.ui.tui.select.Choice.pans`). Thus each lane stays below its label at
    each pan position, and the pan stays when the highlight moves. The address goes last,
    because the user has the least use for this lane. It tells two similar rows apart, and
    the name and the hardware usually do that already.

    Args:
        devices: The devices to list, without the hidden devices.
        remembered: The last connected device. When it is present, it gets a star and it is
            preselected.
        registry: All the confirmed companions, keyed by stable id.
        hidden: How many devices are not in the list because they are hidden. Only the empty
            state uses it, to tell "nothing is plugged in" apart from "you hid all of it".
    """
    if not devices:
        # Nothing is attached or in range. But the user can still type the address of a TCP
        # companion, so show a muted note above the same action rows, not a dead end. When
        # the list is empty because all of its devices are hidden, tell that instead:
        # "nothing detected" is false, and the user must be told about the key that shows
        # them again.
        note = (
            _hidden_note(hidden)
            if hidden
            else Separator("    no companion devices detected — add a network device, or quit")
        )
        return [note, *_action_rows()]
    devices = _order(devices, registry)
    known: set[str] = set(registry)

    # The last column holds a serial port, a BLE address, or a TCP host:port. Its label
    # names the types that are present, so that an endpoint that is not serial is never
    # under a bare "PORT" heading (a BLE address and a network host:port are both an
    # "address").
    has_serial = any(not d.is_ble and not d.is_tcp for d in devices)
    has_address = any(d.is_ble or d.is_tcp for d in devices)
    port_label = (
        "PORT / ADDRESS" if has_serial and has_address else "ADDRESS" if has_address else "PORT"
    )
    # The badge column is first, with no label: a plug or a Bluetooth rune shows what it
    # is, and a heading over three cells of picture only moves the name further to the
    # right.
    type_w = max(_type_cell(d).cell_len for d in devices)
    names = {d.stable_id: _display_name(d, registry) for d in devices}
    name_w = max(len("DEVICE"), *(cell_len(name) for name in names.values()))
    hardware_w = max(len("HARDWARE"), *(cell_len(_hardware_label(d, registry)) for d in devices))
    tags = {d.stable_id: _tag(d, d.stable_id in known) for d in devices}
    tag_w = max(cell_len(text) for text, _ in tags.values())

    # A muted, aligned header above the device rows, as their landmark. Thus a long list of
    # detected devices keeps the lane names at the top when it scrolls. Each label has only
    # one form, in full, and it pans with the rows, so a label is always above its lane. The
    # indent is the same as the row pointer (2) and the star column (2), so that each label
    # is above its own lane.
    lanes = [Lane("", type_w + 2), Lane("DEVICE", name_w + 2), Lane("HARDWARE", hardware_w + 2)]
    if tag_w:
        lanes.append(Lane("", tag_w + 2))  # the tag is under the heading of HARDWARE
    lanes.append(Lane(port_label))
    header = Separator(lambda width: column_header(lanes, width, indent=4), heading=True, pans=True)

    items: list = [header]
    for device in devices:
        is_remembered = remembered is not None and remembered.matches(device)
        is_known = device.stable_id in known
        row = Text()
        row.append("★" if is_remembered else " ", style="warn" if is_remembered else "")
        row.append(" ")
        # The badge shows the transport: a plug emoji for serial, or the Bluetooth rune on its
        # blue badge for BLE. The badge has its own colours, and then plain spaces pad the
        # column. Thus the blue fill covers only the badge, and badges of different widths
        # still align.
        cell = _type_cell(device)
        row.append_text(cell)
        row.append(" " * max(0, type_w - cell.cell_len))
        row.append("  ")
        # A confirmed companion shows its name in white, so that it is different from the
        # devices that MeshTerm only detected.
        row.append(_pad(names[device.stable_id], name_w), style="device.known" if is_known else "")
        row.append("  ")
        row.append(_pad(_hardware_label(device, registry), hardware_w), style="muted")
        row.append("  ")
        if tag_w:
            tag, tag_style = tags[device.stable_id]
            row.append(_pad(tag, tag_w), style=tag_style)
            row.append("  ")
        # Nothing is pinned here, so the ←→ keys pan the full row. The Trophy case pins its
        # rank, date, and score lanes, because those lanes always fit and only the walk is
        # too wide. Thus those lanes keep the place of the user in a long list. This list is
        # the opposite: each lane is drawn in full, so on a narrow terminal, any lane can go
        # past the edge. If the code pinned the start of the row, part of a long name would
        # be the only thing that the ←→ keys could not reach.
        row.append(_where(device), style="muted")
        # Only a network device opts into Delete-to-remove. The list gets it only from its
        # remembered endpoint, so to forget it is the only way to remove it from the picker.
        # A scanned serial or BLE device shows again, so Delete does nothing on those rows.
        items.append(Choice(title=row, value=device, deletable=device.is_tcp, pans=True))
    items.extend(_action_rows())
    return items


def _action_rows() -> list:
    """The last rows of the splash: type the address of a network device, then quit.

    A network (TCP) companion does not announce itself and is not attached, so a scan cannot
    find it. Thus the "add a network device" row opens a host:port prompt, where the user
    types the address of one. The Quit row is the same as on the main menu. The spaces at the
    start align the two rows below the device-name column. The icons share one measured
    column (:func:`~meshterm.ui.menus.align_icons`): today ``🌐`` and ``🚪`` have the same
    width, and the words stay aligned if one of them changes. When the platform draws no icon
    lane, the icons and their padding are removed, the same as on each command row.
    """
    add_label, quit_label = align_icons([f"{_TCP_ICON} Add a network device…", "🚪 Quit"])
    return [
        Separator(" "),
        Choice(title=Text.assemble("  ", add_label), value=_ADD_TCP),
        Choice(title=Text.assemble("  ", quit_label), value=_QUIT),
    ]
