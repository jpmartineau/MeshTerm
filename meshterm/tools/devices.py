# SPDX-License-Identifier: Apache-2.0
"""The ``devices`` tool: list the connected serial, SPI, and in-range Bluetooth companions.

Discovery never opens the device. It only lists the devices that are connected or that
send a BLE advertisement. This tool is a read-only inventory, for the CLI only
(``menu_visible = False``). When the interactive menu opens, a device is already selected,
so the listing has no purpose there. It is a diagnostic on the command line at startup,
for the question "which port/address is my radio?". It marks the devices that MeshTerm
already confirmed as MeshCore companions, and it marks the active device. The user selects
a companion only at startup. Give ``--port`` or ``--ble`` on the CLI (MeshTerm remembers
it after it connects), or select from the prompt that shows when the menu starts. That
prompt does a smoke test of the selection before it confirms it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer

from ..context import AppContext
from ..core import exitcodes
from ..core.discovery import (
    TRANSPORT_MOCK,
    DiscoveredDevice,
    ble_unavailable_reason,
    discover_all,
    discover_devices,
)
from ..core.selection import DeviceSelectionError
from ..core.spiradio import spi_radios, without_radio_ports
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Listing

#: The ``MESHCORE`` verdict for each discovery confidence tier, for the devices that
#: MeshTerm did not confirm yet. The USB vendor ID is only a hint: a native-USB board or a
#: bare bridge chip is a "maybe", never a "yes". Thus no device is called MeshCore until a
#: connection proves it.
_MAYBE: dict[str, str] = {"board": "maybe", "bridge": "maybe", "unknown": "no"}


@register
class DevicesTool(Tool):
    """List the serial, SPI, and in-range Bluetooth companions, and mark likely and active ones."""

    name = "devices"
    title = "Devices"
    help = "List attached serial, SPI, and Bluetooth devices, flagging likely companions"
    category = "This node"
    order = 5
    menu_visible = False  # CLI-only: a startup diagnostic with no place in a connected session

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Find the connected devices and the devices that send BLE advertisements, as a listing.

        Args:
            ctx: The shared application context.
            params: Not used, except for the selection bookkeeping of the menu.

        Returns:
            A :class:`ToolResult` with the number of devices found.
        """
        if ctx.mock:
            # `--mock` promises a session with no real hardware, and a scan is the only
            # thing that this tool does. Before, the tool examined the serial ports and
            # switched the Bluetooth radio on, also with `--mock`. Thus a new user without
            # a companion saw that promise broken on the first command that they tried.
            # Now, with --mock, the simulator is the inventory: one row, and the tool does
            # not touch the machine.
            devices = [
                DiscoveredDevice(
                    transport=TRANSPORT_MOCK,
                    description="the built-in simulator (nothing transmits)",
                    name="simulator",
                )
            ]
        else:
            scan_ble = params.get("ble", True)
            devices = await discover_all(ble=scan_ble) if scan_ble else discover_devices()
            # A radio on the SPI bus is connected as a serial port is, but nothing lists it
            # automatically. Thus the tool lists it from its device file, as the device
            # screen lists it. The port on which the GPS of its board answers is listed as
            # part of the radio, not next to it.
            devices = without_radio_ports(devices, ctx.settings.profiles)
            devices += spi_radios(ctx.settings.profiles, devices)
        known = ctx.device_store.load_all()
        remembered = ctx.device_store.load()
        active = ctx.selected_device
        try:
            spi = (
                ctx.resolve_spi()
                if ctx.spi_override or (ctx.profile is not None and ctx.profile.is_spi)
                else None
            )
        except DeviceSelectionError:
            spi = None  # an ambiguous --spi: the listing shows the user which one

        active_target = (
            ctx.ble_override
            or ctx.port_override
            or (spi.spidev if spi is not None else None)
            or (active.target if active else None)
        )

        # A refused Bluetooth scan is not the same as a scan that found nothing. The
        # listing does not show the difference, because both have no BLE rows. Thus tell
        # the user, also if the scan found other devices. A serial board that is present
        # does not change the fact that a companion that the user expected over Bluetooth
        # is missing.
        blocked = ble_unavailable_reason()
        if blocked:
            from ..ui import script

            script.stderr_console().print(
                f"meshterm: Bluetooth not scanned — {blocked}", style="warn", highlight=False
            )

        if not devices:
            # Not an error: the scan ran and found nothing. The message goes to stderr, so
            # that a caller that redirects stdout still gets it. The exit status is
            # NO_RESULT, so that a script can branch on it without a match on the text.
            # The empty listing still goes to the machine face as `[]`, which is a document
            # that a consumer can read. An empty stdout is a parse error, and the consumer
            # must then tell it apart from a real parse error.
            from ..ui import script

            script.stderr_console().print(
                "meshterm: no companion devices detected", style="warn", highlight=False
            )

        return ToolResult(
            summary={"count": len(devices)},
            report=(_listing(devices, known, remembered, active_target),),
            # Before, this returned 0 with --json. We changed it, because a rendering flag
            # must not change the report, and the plain path always gave 5 here.
            exit_code=exitcodes.OK if devices else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``devices`` subcommand (only an inventory, with no connection).

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(
            name=self.name,
            help=self.help,
            # Before, the listing ended with a line that explained its markers and how to
            # use a row. That text is help, and help goes here.
            epilog="Select a device with --port TARGET or --ble TARGET, using the "
            "TARGET column verbatim; an SPI radio with --spi, or -p and its profile.",
        )
        def _devices(
            ble: bool = typer.Option(
                True, "--ble/--no-ble", help="Include a Bluetooth LE scan (adds a few seconds)"
            ),
        ) -> None:
            run_tool_command(self, {"ble": ble})


def _listing(devices: list, known: dict, remembered: object, active_target: str | None) -> Listing:
    """The device inventory, given one time for both faces.

    ``TARGET`` is first, because a caller uses that field: ``--port`` and ``--ble`` take it
    exactly as it is. The two markers of the menu become columns (``ACTIVE``, and
    ``MESHCORE`` for the confirmed star), because a person can look at a glyph in a
    margin, but a program cannot test it.

    ``MESHCORE`` has three values, and it keeps them:

    - ``yes`` only after a connection proved that the device uses the protocol.
    - ``maybe`` for a USB vendor ID that suggests a LoRa board or a bridge chip.
    - ``no`` for all other devices.

    A vendor ID is a hint. If the column changes a hint to ``yes``, the column gives false
    information. For this reason, the machine face has the same three words, and not
    the boolean that it had before.

    Args:
        devices: The discovered devices.
        known: The remembered device records, keyed by stable id.
        remembered: The remembered default device, if there is one.
        active_target: The target that this run uses (or would use).

    Returns:
        The listing.
    """
    from ..ui import fields
    from ..ui.report import Listing

    rows = []
    for device in devices:
        confirmed = known.get(device.stable_id)
        rows.append(
            {
                "target": device.target,
                "transport": device.transport,
                "port": device.port,
                "address": device.address,
                "label": (confirmed.node_name if confirmed else "") or device.label,
                "hardware": (confirmed.hardware_model if confirmed else "") or device.vendor_label,
                "meshcore": "yes" if confirmed else _MAYBE.get(device.confidence, "no"),
                "confidence": device.confidence,
                "serial_number": device.serial_number,
                "stable_id": device.stable_id,
                "confirmed": confirmed is not None,
                "remembered": remembered is not None and remembered.matches(device),
                "active": device.target == active_target,
            }
        )
    return Listing(
        key="devices",
        columns=(
            fields.word("target", "TARGET"),
            fields.word("transport", "TRANSPORT"),
            # The two possible forms of `target`, kept separate. Thus a caller knows which
            # transport it has, and it does not have to parse the target for a colon.
            fields.hidden("port"),
            fields.hidden("address"),
            fields.name("label", "NAME"),
            fields.name("hardware", "HARDWARE"),
            fields.word("meshcore", "MESHCORE"),
            # The source of the "maybe". A person who reads the column sees the vendor
            # label next to it, but a program cannot.
            fields.hidden("confidence"),
            fields.word("serial_number", "SERIAL"),
            fields.hidden("stable_id"),
            fields.hidden("confirmed"),
            fields.hidden("remembered"),
            fields.flag("active", "ACTIVE"),
        ),
        rows=rows,
    )
