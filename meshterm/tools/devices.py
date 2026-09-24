# SPDX-License-Identifier: Apache-2.0
"""The ``devices`` tool: enumerate attached serial, SPI, and in-range Bluetooth companions.

Discovery never opens the radio — it only lists what is attached or advertising. This is a
CLI-only, read-only inventory (``menu_visible = False``): by the time the interactive menu is
up a device is already selected, so the listing has no job there — it belongs on the command
line as a startup-time "which port/address is my radio?" diagnostic. It marks devices already
confirmed as MeshCore companions (and the active one). Selecting a companion is a startup-only
concern: pass ``--port`` or ``--ble`` on the CLI (remembered after it connects), or pick from
the prompt shown when the menu launches (which smoke-tests the choice before confirming it).
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
from ..core.spiradio import spi_radios
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Listing

#: The ``MESHCORE`` verdict per discovery confidence tier, for devices we have *not* yet
#: confirmed. The USB vendor ID is only a hint — a native-USB board or a bare bridge chip
#: is a "maybe", never a "yes" — so nothing is billed as MeshCore until a connection proves
#: it.
_MAYBE: dict[str, str] = {"board": "maybe", "bridge": "maybe", "unknown": "no"}


@register
class DevicesTool(Tool):
    """List attached serial, SPI, and in-range Bluetooth companions; mark likely and active."""

    name = "devices"
    title = "Devices"
    help = "List attached serial, SPI, and Bluetooth devices, flagging likely companions"
    category = "This node"
    order = 5
    menu_visible = False  # CLI-only: a startup diagnostic with no place in a connected session

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Enumerate attached and advertising devices, and state them as a listing.

        Args:
            ctx: Shared application context.
            params: Unused beyond the menu's selection bookkeeping.

        Returns:
            A :class:`ToolResult` summarizing how many devices were found.
        """
        if ctx.mock:
            # `--mock` promises a session with no real hardware, and a scan is the one
            # thing this tool does — it walked the serial ports and switched the Bluetooth
            # radio on regardless, which is a promise broken on the first command a
            # stranger without a radio would try. So under --mock the simulator *is* the
            # inventory: one row, and nothing on the machine touched.
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
            # A radio on the SPI bus is attached the way a serial port is, but nothing
            # enumerates it: it is listed from its device node, as the device screen lists it.
            devices += spi_radios(ctx.settings.profiles, devices)
        known = ctx.device_store.load_all()
        remembered = ctx.device_store.load()
        active = ctx.selected_device
        spi = (
            ctx.resolve_spi()
            if ctx.spi_override or (ctx.profile is not None and ctx.profile.is_spi)
            else None
        )
        active_target = (
            ctx.ble_override
            or ctx.port_override
            or (spi.spidev if spi is not None else None)
            or (active.target if active else None)
        )

        # A refused Bluetooth scan is not the same as a quiet one, and the difference is
        # invisible in the listing — both simply lack BLE rows. Say so whether or not
        # anything else was found: a serial board being present does not make a companion
        # the reader expected over Bluetooth any less missing.
        blocked = ble_unavailable_reason()
        if blocked:
            from ..ui import script

            script.stderr_console().print(
                f"meshterm: Bluetooth not scanned — {blocked}", style="warn", highlight=False
            )

        if not devices:
            # Not an error: the scan ran and found nothing. Said on stderr so a caller
            # redirecting stdout still hears it, and reported as NO_RESULT so a script can
            # branch on it without matching prose. The empty listing still reaches the
            # machine face as `[]`, which is a document a consumer can read — where an
            # empty stdout is a parse error it would have to tell apart from a real one.
            from ..ui import script

            script.stderr_console().print(
                "meshterm: no companion devices detected", style="warn", highlight=False
            )

        return ToolResult(
            summary={"count": len(devices)},
            report=(_listing(devices, known, remembered, active_target),),
            # Changed from the 0 this used to return under --json: a rendering flag has no
            # business changing the report, and the plain path has always said 5 here.
            exit_code=exitcodes.OK if devices else exitcodes.NO_RESULT,
        )

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``devices`` subcommand (inventory only; no connection).

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(
            name=self.name,
            help=self.help,
            # The listing used to close with a line explaining its own markers and how to
            # act on a row. That is help, and this is where help goes.
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
    """The device inventory, stated once for both faces.

    ``TARGET`` leads because it is the field a caller acts on — it is what ``--port`` and
    ``--ble`` take, verbatim. The menu's two markers become columns of their own
    (``ACTIVE``, and ``MESHCORE`` for the confirmed star), because a glyph in a margin is
    something to look at rather than something to test.

    ``MESHCORE`` is three-valued and stays that way: ``yes`` only once a connection has
    proved the device speaks the protocol, ``maybe`` for a USB vendor ID that suggests a
    LoRa board or a bridge chip, ``no`` for anything else. A vendor ID is a hint, and the
    column would be lying if it rounded one up — which is why the machine face carries the
    same three words rather than the boolean it used to.

    Args:
        devices: The discovered devices.
        known: Remembered device records, keyed by stable id.
        remembered: The remembered default device, if there is one.
        active_target: The target this invocation is (or would be) using.

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
            # The two halves `target` is one of, kept apart for a caller that has to know
            # which transport it is holding without parsing the target for a colon.
            fields.hidden("port"),
            fields.hidden("address"),
            fields.name("label", "NAME"),
            fields.name("hardware", "HARDWARE"),
            fields.word("meshcore", "MESHCORE"),
            # What the "maybe" was derived from. A person reading the column has the
            # vendor label beside it and can see for themselves; a program cannot.
            fields.hidden("confidence"),
            fields.word("serial_number", "SERIAL"),
            fields.hidden("stable_id"),
            fields.hidden("confirmed"),
            fields.hidden("remembered"),
            fields.flag("active", "ACTIVE"),
        ),
        rows=rows,
    )
