# SPDX-License-Identifier: Apache-2.0
"""The ``info`` tool: show the identity and the radio settings of the connected device."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import typer
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..context import AppContext
from ..core.connection import Device, derived_scope_key
from ..core.device_config import current_preset
from ..core.models import node_type_label
from .base import Tool, ToolResult, register

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ui.report import Column, Facts, Listing

#: The unit of each kind of telemetry reading that a companion can report. A reading whose
#: kind is not here has no unit (a count, a state, or a reading with several fields). The
#: names are the names that the Cayenne LPP decoder gives
#: (refer to :func:`~meshterm.core.connection.lpp_readings`).
_UNITS = {
    "temperature": "°C",
    "humidity": "%",
    "percentage": "%",
    "voltage": "V",
    "current": "A",
    "barometer": "hPa",
    "illuminance": "lux",
    "altitude": "m",
    "distance": "m",
    "energy": "kWh",
    "power": "W",
    "load": "kg",
    "concentration": "ppm",
    "direction": "°",
    "frequency": "Hz",
    "time": "s",
}


@dataclass(frozen=True, slots=True)
class AppFacts:
    """What MeshTerm itself knows about the connected node, beside what the radio reports.

    The radio answers for its own settings and its own counters. These facts come from the
    other side: the stores of this computer, the caches of the session, and what MeshTerm
    derives from the radio settings. The Device info page shows them with the facts of the
    radio, because the user reads one node and not two sources.

    Attributes:
        place: The named area nearest the location of the node, or ``None``.
        site: The name of the active site, or ``None`` when no site is active.
        outbound_paths: How many outbound paths apply to this device.
        fixed: Whether those paths come from a fixed-device list and not from the site.
        kept_routes: How many device routes the user keeps for this device.
        contacts: How many contacts the device holds, or ``None`` when no read gave them.
        channels: How many channels are set, or ``None`` when no read gave them.
        preset: The name of the radio preset that the four radio settings match, or
            ``None`` when they match no preset.
    """

    place: str | None = None
    site: str | None = None
    outbound_paths: int | None = None
    fixed: bool = False
    kept_routes: int | None = None
    contacts: int | None = None
    channels: int | None = None
    preset: str | None = None


@register
class InfoTool(Tool):
    """Show the identity and the radio settings of the device.

    The list of known contacts is in the separate ``nodes`` tool.
    """

    name = "info"
    title = "Device info"
    icon = "📋"
    help = "Show device status, identity, and radio config"
    category = "This node"
    order = 10  # what it is, before the tools that change it

    async def run(self, ctx: AppContext, params: dict[str, Any]) -> ToolResult:
        """Query the device, and render its live status and all its settings.

        Both faces answer with the same three parts, in the same order: the status of the
        device, its sensor readings, and every setting that it holds. The menu draws the
        parts as a panel over the settings table. The scripted face returns them as three
        blocks, and the renderer of the boundary prints them.

        The settings come from one definition, :func:`~meshterm.tools.config.settings_listing`,
        which ``config show`` answers with. Thus the scripted face of this command says the
        same about a device as the page does, and no setting is spelled two ways.

        Args:
            ctx: The shared application context.
            params: Not used.

        Returns:
            A :class:`ToolResult` with the device name in its summary.
        """
        from rich.console import Group

        from ..ui.config_editor import cached_snapshot, config_table, has_pin

        device = await ctx.device()
        snapshot = await cached_snapshot(ctx, device)
        readings = await read_status(device)
        app = await app_facts(ctx, snapshot)

        # On the CLI, the status block, the sensor readings, and the settings, in that
        # order. The settings keep the keys that `config get` and `config set` take.
        session = getattr(ctx.ui, "session", None)
        if session is None:
            from .config import settings_listing

            custom = await device.get_custom_vars()
            return ToolResult(
                summary={"name": snapshot.get("name")},
                report=(
                    status_facts(snapshot, readings, app),
                    telemetry_listing(readings["telemetry"]),
                    settings_listing(snapshot, custom),
                ),
            )

        from ..ui.device_info_screen import DeviceInfoScreen

        custom = await device.get_custom_vars()
        # A live status panel (identity, place, scope, site, health, and the traffic
        # statistics) above all the current settings, each with a short explanation. The
        # panel is built one time. Only the table is different between the two states, and
        # it is fast to build again.
        panel = status_panel(snapshot, readings, app)

        def page(reveal_pin: bool) -> Group:
            return Group(panel, Text(""), config_table(snapshot, custom, reveal_pin=reveal_pin))

        # The page is a separate screen, because it has its own keyboard key: the pairing
        # PIN is masked until ``^E`` shows it.
        await session.run_screen(
            DeviceInfoScreen(session, page, title=self.title, conceals=has_pin(snapshot))
        )
        return ToolResult(summary={"name": snapshot.get("name")})

    def register_cli(self, app: typer.Typer) -> None:
        """Register the ``info`` subcommand.

        Args:
            app: The Typer application.
        """
        from ..cli import run_tool_command

        @app.command(name=self.name, help=self.help)
        def _info() -> None:
            run_tool_command(self, {})


# -- the reads ------------------------------------------------------------------


async def read_status(device: Device) -> dict:
    """Read each live status value that the firmware gives, as a best effort.

    This is the only place that asks the device for its live state. Both faces use what it
    returns: the panel of the menu makes prose rows from these values, and the CLI prints
    them one fact on each line (refer to :func:`status_panel` and :func:`status_facts`).
    Each read is optional, because firmware that is older than a query gives nothing for
    it. Thus this function works on any device.

    The device-query frame is not read here. :func:`~meshterm.core.device_config.build_snapshot`
    already merges it into the snapshot, and one frame read one time is enough.

    Args:
        device: The connected device to query.

    Returns:
        The merged raw readings: ``battery``, ``stats``, ``clock``, and ``telemetry``.
    """
    return {
        "battery": await _try(device.get_battery) or {},
        "stats": await _try(device.get_stats) or {},
        "clock": await _try(device.get_time),
        "telemetry": await _try(device.get_self_telemetry) or [],
    }


async def app_facts(ctx: AppContext, snapshot: dict) -> AppFacts:
    """Collect what MeshTerm knows about the connected node, from its own stores.

    Nothing here asks the radio. The place comes from the place service or from what it
    stored before, the site and the paths from the outbound store, the counts from the
    caches of the session, and the preset from the four radio settings.

    The count of contacts comes from the device-state cache, which is one read and which
    the menu fills at startup. The count of channels comes from the same cache, but this
    function never fills it: that probe reads every slot index, and a count is not worth
    that cost on a report command. Thus the scripted face leaves the count of channels out
    and keeps the capacity, which the device-query frame gives for nothing.

    Args:
        ctx: The shared application context.
        snapshot: The device snapshot (it gives the key, the location, and the radio
            settings).

    Returns:
        The facts, each one ``None`` where MeshTerm has nothing to say.
    """
    key = str(snapshot.get("public_key") or "")
    preset = current_preset(snapshot)
    site, paths, fixed, kept = _outbound(ctx, key)
    return AppFacts(
        place=_place(ctx, key, snapshot),
        site=site,
        outbound_paths=paths,
        fixed=fixed,
        kept_routes=kept,
        contacts=await _contact_count(ctx),
        channels=_channel_count(ctx),
        preset=preset.name if preset is not None else None,
    )


def _place(ctx: AppContext, key: str, snapshot: dict) -> str | None:
    """The place of our own node, and a request to find it when a session can.

    MeshTerm never hears its own node, so the finder knows where this node is only when a
    page tells it. The node page does the same (refer to
    :meth:`~meshterm.services.places.PlaceService.want_at`). The request is for the next
    time that this page opens: the panel is built one time, and a place that arrives after
    that is not drawn again.
    """
    from ..services import places as place_service

    service = place_service.current()
    if service is not None:
        service.want_at(key, snapshot.get("adv_lat"), snapshot.get("adv_lon"))
    place = place_service.place_of(ctx, key)
    return place.label if place is not None else None


def _outbound(ctx: AppContext, key: str) -> tuple[str | None, int | None, bool, int | None]:
    """The active site, the outbound paths that apply here, and the kept device routes."""
    from ..core.outbound_store import SOURCE_DEVICE

    store = getattr(ctx, "outbound_store", None)
    if store is None:
        return None, None, False, None
    try:
        choice = store.lookup(key or None)
        site = store.active_site()
        if not choice.name and site is None:
            return None, None, False, None
        return (
            site.name if site is not None else None,
            len(choice.paths),
            choice.source == SOURCE_DEVICE,
            len(store.kept_routes(key or None)),
        )
    except Exception:  # noqa: BLE001 - a store that cannot be read costs one row, not the page
        return None, None, False, None


async def _contact_count(ctx: AppContext) -> int | None:
    """How many contacts the device holds, or ``None`` when the read did not answer."""
    devstate = getattr(ctx, "devstate", None)
    if devstate is None:
        return None
    try:
        return len(await devstate.contacts())
    except Exception:  # noqa: BLE001 - an optional count. Its absence is acceptable.
        return None


def _channel_count(ctx: AppContext) -> int | None:
    """How many channels are set, from the cache only (refer to :func:`app_facts`)."""
    devstate = getattr(ctx, "devstate", None)
    if devstate is None:
        return None
    try:
        slots = devstate.peek_channel_slots()
    except Exception:  # noqa: BLE001 - an optional count. Its absence is acceptable.
        return None
    return None if slots is None else len(slots)


# -- the status panel -----------------------------------------------------------


def status_panel(snapshot: dict, readings: dict, app: AppFacts) -> Panel:
    """Build the live status panel: who this node is, how it is, and what it heard.

    This is the menu face of :func:`read_status` and :func:`app_facts`. Each row is a short
    phrase, not an atomic value, because a person reads "-110 dBm noise floor · last RSSI
    -62 dBm" in one step. A script wants the three numbers separately.

    The rows sit in a two-column grid, so a value that is wider than the panel wraps under
    its own column and never goes back to column zero. A key is the one value that this
    always costs a second line, and it stays whole: the page is where a user reads the key
    of this node out to another person.

    Args:
        snapshot: The device snapshot (the identity, the radio settings, and the facts of
            the device-query frame).
        readings: The live readings from :func:`read_status`.
        app: What MeshTerm knows itself, from :func:`app_facts`.

    Returns:
        A Rich :class:`Panel` of status rows, each with a label and a value.
    """
    rows = _status_rows(snapshot, readings, app)
    if not rows:
        return Panel(
            Text("no status reported", style="muted"),
            title="[title.border]Status[/]",
            border_style="border",
            expand=False,
        )
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="muted", no_wrap=True)
    grid.add_column(overflow="fold")
    for label, value in rows:
        grid.add_row(label, value if value is not None else "")
    return Panel(grid, title="[title.border]Status[/]", border_style="border", expand=False)


def _status_rows(snapshot: dict, readings: dict, app: AppFacts) -> list[tuple[str, Text | None]]:
    """The rows of the status panel, in four groups with a blank row between them.

    The groups answer four questions, in the order in which a user asks them: what this
    node is, what MeshTerm does with it, how it is doing, and what it heard and sent. A row
    whose value nothing reported is left out, so a panel never has a column of question
    marks.
    """
    battery, stats = readings["battery"], readings["stats"]
    groups: list[list[tuple[str, Text]]] = [[], [], [], []]

    # 1. What this node is.
    adv_type = snapshot.get("adv_type")
    if adv_type is not None:
        groups[0].append(("role", Text(node_type_label(adv_type), style="brand")))
    key = str(snapshot.get("public_key") or "")
    if key:
        groups[0].append(("key", _key_text(key, snapshot)))
    if snapshot.get("model"):
        groups[0].append(("model", Text(str(snapshot["model"]))))
    firmware = _firmware_text(snapshot)
    if firmware is not None:
        groups[0].append(("firmware", firmware))
    if app.place:
        groups[0].append(("place", Text(app.place)))

    # 2. What MeshTerm does with it.
    groups[1].append(("scope", _scope_text(snapshot)))
    site = _site_text(app)
    if site is not None:
        groups[1].append(("site", site))
    groups[1].append(("preset", Text(app.preset or "custom", style="" if app.preset else "muted")))

    # 3. How it is doing.
    level = battery.get("level")
    if level:
        groups[2].append(("battery", Text(f"{int(level) / 1000:.2f} V")))
    if battery.get("total_kb"):
        groups[2].append(
            ("storage", Text(f"{battery.get('used_kb', 0)} / {battery['total_kb']} kB"))
        )
    telemetry = _telemetry_text(readings["telemetry"])
    if telemetry is not None:
        groups[2].append(("telemetry", telemetry))
    clock = readings["clock"]
    if clock:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(clock))
        groups[2].append(("clock", Text.assemble(stamp, (f"  {_drift(clock)}", "muted"))))
    if stats.get("uptime_secs") is not None:
        groups[2].append(("uptime", Text(_uptime(int(stats["uptime_secs"])))))
    contacts = _capacity_text(app.contacts, snapshot.get("max_contacts"))
    if contacts is not None:
        groups[2].append(("contacts", contacts))
    channels = _capacity_text(app.channels, snapshot.get("max_channels"))
    if channels is not None:
        groups[2].append(("channels", channels))
    if stats.get("queue_len") is not None:
        groups[2].append(("queue", Text(f"{stats['queue_len']} waiting to send")))
    if stats.get("errors") is not None:
        groups[2].append(("errors", _count_text(int(stats["errors"]))))

    # 4. What it heard and sent.
    if stats.get("noise_floor") is not None:
        parts = Text(f"{stats['noise_floor']} dBm noise floor")
        if stats.get("last_rssi") is not None:
            parts.append(f" · last RSSI {stats['last_rssi']} dBm", style="muted")
        if stats.get("last_snr") is not None:
            parts.append(f" · last SNR {stats['last_snr']:+.1f} dB", style="muted")
        groups[3].append(("radio", parts))
    if stats.get("tx_air_secs") is not None:
        groups[3].append(
            ("airtime", Text(f"TX {stats['tx_air_secs']} s · RX {stats.get('rx_air_secs', 0)} s"))
        )
    if stats.get("recv") is not None:
        packets = Text(f"{stats.get('sent', 0)} sent · {stats['recv']} received")
        if stats.get("recv_errors"):
            packets.append(f" · {stats['recv_errors']} receive errors", style="warn")
        groups[3].append(("packets", packets))
    for label, sent, received in (
        ("flood", "flood_tx", "flood_rx"),
        ("direct", "direct_tx", "direct_rx"),
    ):
        if stats.get(sent) is None and stats.get(received) is None:
            continue
        groups[3].append(
            (label, Text(f"{stats.get(sent, 0)} sent · {stats.get(received, 0)} received"))
        )

    rows: list[tuple[str, Text | None]] = []
    for group in groups:
        if not group:
            continue
        if rows:
            rows.append(("", None))
        rows.extend(group)
    return rows


def _key_text(key: str, snapshot: dict) -> Text:
    """The public key of this node, with the bytes that the mesh routes on lit."""
    from ..ui.widgets import highlighted_hash

    mode = snapshot.get("path_hash_mode")
    prefix = (int(mode) + 1) if isinstance(mode, int) and 0 <= mode <= 3 else 0
    return highlighted_hash(key, prefix)


def _firmware_text(snapshot: dict) -> Text | None:
    """The firmware version and build, with the companion protocol version after it."""
    version = _firmware(snapshot)
    protocol = snapshot.get("fw ver")
    if not version and protocol is None:
        return None
    text = Text(version or "unknown", style="" if version else "muted")
    if protocol is not None:
        text.append(f" · protocol {protocol}", style="muted")
    return text


def _scope_text(snapshot: dict) -> Text:
    """The default flood scope: its name and key, or the plain statement that it has none.

    A scope that the device stores with a key that its own name does not give is a fault
    that nothing else shows. The row says so, because a packet that goes out under that
    key reaches nobody who listens for the name.
    """
    name = snapshot.get("flood_scope")
    if not name:
        text = Text("unscoped")
        text.append(" · floods reach everyone", style="muted")
        return text
    text = Text(str(name))
    stored = str(snapshot.get("flood_scope_key") or "")
    if stored:
        text.append(f" · key {stored}", style="muted")
        if stored != derived_scope_key(str(name)):
            text.append(" · the key does not match the name", style="warn")
    return text


def _site_text(app: AppFacts) -> Text | None:
    """The active site, the outbound paths that apply, and the kept device routes."""
    if app.site is None and app.outbound_paths is None:
        return None
    text = Text(app.site or "no site", style="" if app.site else "muted")
    if app.outbound_paths is not None:
        text.append(f" · {app.outbound_paths} paths", style="muted")
        text.append(" (this device)" if app.fixed else "", style="muted")
    if app.kept_routes:
        text.append(f" · {app.kept_routes} kept routes", style="muted")
    return text


def _capacity_text(count: int | None, capacity: Any) -> Text | None:
    """``27 / 300``, or one of the two numbers when the other one is not known."""
    total = int(capacity) if isinstance(capacity, (int, float)) else None
    if count is None and total is None:
        return None
    if count is None:
        return Text.assemble((f"{total}", "muted"), (" capacity", "muted"))
    if total is None:
        return Text(f"{count}")
    return Text.assemble(f"{count}", (f" / {total}", "muted"))


def _count_text(count: int) -> Text:
    """A count of faults: plain while it is zero, and in the warn colour once it is not."""
    return Text(str(count), style="warn" if count else "")


def _telemetry_text(telemetry: list[dict]) -> Text | None:
    """The sensor readings of this node on one line, each with its unit."""
    if not telemetry:
        return None
    text = Text()
    for i, reading in enumerate(telemetry):
        if i:
            text.append(" · ", style="muted")
        text.append(f"{reading.get('name')} ", style="muted")
        text.append(_reading_value(reading))
    return text


def _reading_value(reading: dict) -> str:
    """One telemetry value with its unit: ``21.5 °C``, or the fields of a compound reading."""
    value = reading.get("value")
    if isinstance(value, dict):
        return ", ".join(f"{name} {_number(item)}" for name, item in value.items())
    unit = _UNITS.get(str(reading.get("name") or ""), "")
    return f"{_number(value)} {unit}".strip()


def _number(value: Any) -> str:
    """A reading as the shortest form that keeps it exact (``4.1``, not ``4.100000``)."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def _firmware(snapshot: dict) -> str:
    """The firmware version and build, joined, or ``""`` if the device reported neither."""
    return " ".join(str(snapshot[k]) for k in ("ver", "fw_build") if snapshot.get(k))


# -- the scripted faces ---------------------------------------------------------


def status_facts(snapshot: dict, readings: dict, app: AppFacts) -> Facts:
    """The live status of the device, as one set of facts.

    This is the scripted face of :func:`read_status` and :func:`app_facts`. The panel
    writes a phrase in each row, but this function writes one value for each key. It puts
    the unit in the key (``battery_v``, ``uptime_s``, ``noise_floor_dbm``), so that no
    program must get the number out of prose. On both faces, a number never has a unit in
    its value.

    The three readings in seconds and the clock drift have a **gloss** next to the number:
    ``uptime_s  93784  (1d 2h)``. This is the only key/value block that can have a gloss.
    The reason is that it is the only block with no ``get`` and no ``set`` behind it. Thus
    there is no round trip to protect, the key still tells what the number counts, and
    ``93784`` is a value that no person can easily understand. The document holds only the
    number, because it has no useful place for a phrase.

    ``flood_scope`` is also a setting, and the settings block of this command has it too.
    Both blocks print the one value that ``config get flood_scope`` returns, in the one
    spelling that ``config set`` takes back. The status block keeps it because
    ``flood_scope_key`` is here, and a key with no name next to it says little.

    If the firmware gave no answer for a reading, the plain block **leaves out** that
    reading, and it does not show a dash. These are optional queries. A missing key says
    "this device does not report it", but a dash says that it reported nothing. The
    document keeps the key and writes ``null``. Refer to
    :attr:`~meshterm.ui.report.Facts.omit_absent` for the reason why the two faces are
    different here on purpose.

    Args:
        snapshot: The device snapshot (the identity, the settings, and the facts of the
            device-query frame).
        readings: The live readings from :func:`read_status`.
        app: What MeshTerm knows itself, from :func:`app_facts`.

    Returns:
        The facts block.
    """
    from ..ui import fields
    from ..ui.report import Facts

    battery, stats = readings["battery"], readings["stats"]
    level = battery.get("level")
    adv_type = snapshot.get("adv_type")
    clock = readings["clock"]
    scope = snapshot.get("flood_scope")
    values: dict[str, Any] = {
        # The identity is first: which device this is. All the values that it can be set
        # to are in the settings block below the status block.
        "name": snapshot.get("name"),
        "public_key": str(snapshot.get("public_key") or "").lower() or None,
        "role": node_type_label(adv_type),
        "model": snapshot.get("model"),
        "firmware": _firmware(snapshot) or None,
        "protocol_version": snapshot.get("fw ver"),
        "place": app.place,
        "flood_scope": None if scope is None else fields.Rendered(scope, scope or '""'),
        "flood_scope_key": snapshot.get("flood_scope_key"),
        "preset": app.preset,
        "site": app.site,
        "outbound_paths": app.outbound_paths,
        "kept_routes": app.kept_routes,
        "battery_v": round(int(level) / 1000, 2) if level else None,
        "storage_used_kb": battery.get("used_kb", 0) if battery.get("total_kb") else None,
        "storage_total_kb": battery.get("total_kb"),
        "contacts": app.contacts,
        "max_contacts": snapshot.get("max_contacts"),
        "channels": app.channels,
        "max_channels": snapshot.get("max_channels"),
        "clock_at": (
            datetime.fromtimestamp(clock, tz=timezone.utc).astimezone() if clock else None
        ),
        "clock_drift_s": (clock - int(time.time())) if clock else None,
        "uptime_s": stats.get("uptime_secs"),
        "queue_len": stats.get("queue_len"),
        "firmware_errors": stats.get("errors"),
        "noise_floor_dbm": stats.get("noise_floor"),
        "last_rssi_dbm": stats.get("last_rssi"),
        "last_snr_db": stats.get("last_snr"),
        "tx_air_s": stats.get("tx_air_secs"),
        "rx_air_s": stats.get("rx_air_secs"),
        "packets_sent": stats.get("sent"),
        "packets_sent_flood": stats.get("flood_tx"),
        "packets_sent_direct": stats.get("direct_tx"),
        "packets_received": stats.get("recv"),
        "packets_received_flood": stats.get("flood_rx"),
        "packets_received_direct": stats.get("direct_rx"),
        "receive_errors": stats.get("recv_errors"),
    }
    return Facts(
        key="info",
        fields=(
            fields.word("name", "name"),
            fields.hexid("public_key", "public_key"),
            fields.word("role", "role"),
            fields.word("model", "model"),
            fields.word("firmware", "firmware"),
            fields.integer("protocol_version", "protocol_version"),
            fields.name("place", "place"),
            fields.rendered("flood_scope", "flood_scope"),
            fields.hexid("flood_scope_key", "flood_scope_key"),
            fields.word("preset", "preset"),
            fields.name("site", "site"),
            fields.integer("outbound_paths", "outbound_paths"),
            fields.integer("kept_routes", "kept_routes"),
            fields.decimal("battery_v", "battery_v", ".2f"),
            fields.integer("storage_used_kb", "storage_used_kb"),
            fields.integer("storage_total_kb", "storage_total_kb"),
            fields.integer("contacts", "contacts"),
            fields.integer("max_contacts", "max_contacts"),
            fields.integer("channels", "channels"),
            fields.integer("max_channels", "max_channels"),
            fields.instant("clock_at", "clock_at"),
            _glossed("clock_drift_s", _drift_gloss),
            _glossed("uptime_s", _span_gloss),
            fields.integer("queue_len", "queue_len"),
            fields.integer("firmware_errors", "firmware_errors"),
            fields.integer("noise_floor_dbm", "noise_floor_dbm"),
            fields.integer("last_rssi_dbm", "last_rssi_dbm"),
            fields.snr("last_snr_db", "last_snr_db"),
            _glossed("tx_air_s", _span_gloss),
            _glossed("rx_air_s", _span_gloss),
            fields.integer("packets_sent", "packets_sent"),
            fields.integer("packets_sent_flood", "packets_sent_flood"),
            fields.integer("packets_sent_direct", "packets_sent_direct"),
            fields.integer("packets_received", "packets_received"),
            fields.integer("packets_received_flood", "packets_received_flood"),
            fields.integer("packets_received_direct", "packets_received_direct"),
            fields.integer("receive_errors", "receive_errors"),
        ),
        values=values,
        omit_absent=True,
    )


def telemetry_listing(telemetry: list[dict]) -> Listing:
    """The sensor readings of this node, one record for each reading.

    The readings are a listing and not more keys in the status block, because a device
    reports whichever sensors it has. A key for each possible sensor would be a contract
    that no firmware keeps. A record names the channel that the reading came from, the kind
    of reading, and its value.

    The unit is a field of the record and not part of the key, because the kind of reading
    is data here and not a key that MeshTerm declared. The plain face puts the unit in the
    value, where a person reads it. The document keeps the number alone and the unit beside
    it.

    Args:
        telemetry: The readings from :func:`read_status`.

    Returns:
        The listing. It has no records on a device that reports no sensors, and the plain
        face then draws nothing.
    """
    from ..ui import fields
    from ..ui.report import Listing

    rows = [
        {
            "channel": reading.get("channel"),
            "name": reading.get("name"),
            "unit": _UNITS.get(str(reading.get("name") or "")) or None,
            "value": fields.Rendered(reading.get("value"), _reading_value(reading)),
        }
        for reading in telemetry
    ]
    return Listing(
        key="telemetry",
        columns=(
            fields.integer("channel", "CHANNEL"),
            fields.name("name", "READING"),
            fields.rendered("value", "VALUE"),
            fields.hidden("unit"),
        ),
        rows=rows,
    )


def _glossed(key: str, gloss: Callable[[int], str]) -> Column:
    """A reading in seconds, printed as its number with a readable duration next to it."""
    from ..ui import script
    from ..ui.report import Column, Lane

    def cell(value: Any) -> str:
        if value is None:
            return script.NONE
        return f"{value}  ({gloss(int(value))})"

    return Column(key=key, lanes=(Lane(header=key, render=cell),))


def _span_gloss(seconds: int) -> str:
    """``93784`` → ``1d 2h``: a value that a person can easily understand."""
    from ..ui import script

    return script.duration(seconds)


def _drift_gloss(seconds: int) -> str:
    """The error of the device clock, in the words that a person uses (fast or slow)."""
    from ..ui import script

    if abs(seconds) < 2:
        return "in sync"
    return f"{script.duration(seconds)} {'fast' if seconds > 0 else 'slow'}"


async def _try(read) -> Any | None:
    """Await a device read. Return ``None`` if the firmware does not support it."""
    try:
        return await read()
    except Exception:  # noqa: BLE001 - an optional read. If it is absent, that is acceptable.
        return None


def _drift(device_epoch: int) -> str:
    """Describe the drift of the device clock, compared with the clock of this computer.

    The drift is a duration that a person reads, and not a count of seconds: a clock that
    was never set is a year out, and ``31330116 s`` says nothing. The scripted face writes
    the same duration beside its own number (refer to :func:`_drift_gloss`), so the two
    faces spell one drift one way.
    """
    from ..ui import script

    drift = device_epoch - int(time.time())
    if abs(drift) < 2:
        return "(in sync)"
    direction = "ahead" if drift > 0 else "behind"
    return f"({script.duration(abs(drift))} {direction})"


def _uptime(secs: int) -> str:
    """Render an uptime as ``3d 2h 41m`` (the seconds show only below one minute)."""
    if secs < 60:
        return f"{secs} s"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)
