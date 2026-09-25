# SPDX-License-Identifier: Apache-2.0
"""The application context: a small dependency container passed to every tool.

``AppContext`` owns the shared, expensive singletons (console, settings, repository,
logger) and lazily manages the device connection so tools that don't touch the radio
never open a serial port.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rich.console import Console

from .core.admin_store import AdminStore
from .core.advert_store import AdvertStore
from .core.channel_store import ChannelStore
from .core.config import DeviceProfile, Settings, SpiWiring
from .core.connection import Device, make_device
from .core.contact_store import ContactStore
from .core.courier_store import CourierStore
from .core.device_store import DeviceStore
from .core.discovery import DiscoveredDevice, discover_devices
from .core.mute_store import MuteStore
from .core.preferences import PREFERENCES_FILENAME, Preferences, report_dropped
from .core.preferences import install as install_preferences
from .core.region_store import RegionStore
from .core.remote_store import RemoteStore
from .core.selection import resolve_device
from .core.settings_store import SettingsStore
from .core.watch_store import WatchStore
from .persistence.logging import get_logger
from .persistence.repository import Repository
from .ui.renderers import OutputFormat

if TYPE_CHECKING:
    from .services.advert_scheduler import AdvertScheduler
    from .services.basemap import BasemapSource
    from .services.battery_service import BatteryService
    from .services.chat_service import ChatService
    from .services.clock_sync import ClockSync
    from .services.courier import CourierService
    from .services.device_state import DeviceState
    from .services.event_hub import EventHub
    from .services.monitor_service import MonitorService
    from .services.watchtower import WatchtowerService
    from .ui.surface import Ui


@dataclass(slots=True)
class AppContext:
    """Shared services and per-invocation state handed to tools.

    Attributes:
        console: Rich console for all rendering.
        settings: Loaded application settings — where things live and which device to
            talk to (config directory, database, named profiles). *How MeshTerm behaves*
            is ``preferences``, not this.
        preferences: MeshTerm's own preferences, backed by
            ``<config_dir>/preferences.toml`` and edited on the Preferences page. Defaults
            to a set loaded from that file when not injected, and is installed as the
            process-wide :func:`~meshterm.core.preferences.current` set so the render
            layer — which has no context to reach through — reads the same values.
        repo: Database repository.
        profile: Active device profile, if one was resolved.
        device_store: Store for the remembered "last known good" device.
        admin_store: Store for remembered remote-node admin passwords.
        advert_store: Store for per-device background-advert schedules (defaults to
            ``<config_dir>/adverts.json`` when not injected).
        remote_store: Store for remote-node admin state — cached settings and CLI
            history (defaults to ``<config_dir>/remote.json`` when not injected).
        watch_store: Store for the Watchtower — watched nodes, rules, and the alert
            log (defaults to ``<config_dir>/watchtower.json`` when not injected).
        courier_store: Store for the Courier's outbox — queued, delivered, and
            given-up messages (defaults to ``<config_dir>/courier.json``).
        mute_store: Store for muted channel notifications — the channels whose new
            messages don't raise the unread badge (defaults to
            ``<config_dir>/mutes.json`` when not injected).
        region_store: Store for the regions known by name and each channel's send scope —
            what a scoped flood is resolved against (defaults to
            ``<config_dir>/regions.json`` when not injected).
        channel_store: Store for channels created through MeshTerm, replayed into a device
            that forgot them (a firmware-less radio bridge). Keyed by device public key;
            defaults to ``<config_dir>/channels.json`` when not injected.
        settings_store: Store for settings changed through MeshTerm, offered back on connect to
            a device that forgot them (a firmware-less radio bridge). Keyed by device public
            key; defaults to ``<config_dir>/settings.json`` when not injected.
        contact_store: Store for contacts read through MeshTerm, merged back into the contact
            lists for a device that forgot them (a firmware-less radio bridge). Keyed by device
            public key; defaults to ``<config_dir>/contacts.json`` when not injected.
        mock: Whether the simulator device is in use.
        port_override: Explicit serial port (from ``--port`` or the interactive picker),
            overriding the profile.
        ble_override: Explicit Bluetooth address (from ``--ble`` or the interactive picker),
            selecting the BLE transport. Takes precedence over ``port_override``.
        tcp_override: Explicit network address ``host:port`` (from ``--tcp`` or the
            interactive picker), selecting the TCP transport. Takes precedence over
            ``ble_override`` and ``port_override``.
        ble_pin: Optional BLE pairing PIN for the chosen Bluetooth device.
        output: Which format a scripted run prints its answer in (see
            :class:`~meshterm.ui.renderers.OutputFormat`). A tool never reads this to
            decide *what* to say — it states its answer as a
            :class:`~meshterm.ui.report.Report` and the CLI boundary picks the renderer —
            and the two streaming commands read it only to open the right kind of stream.
        interactive: Whether this run may stop and ask a person something. False for every
            scripted run whose stdin is not a terminal, which is the question a tool
            actually wants answered before it prompts. ``tx-optimize`` used to ask
            ``--json`` instead, and a flag about *output format* cannot answer it: without
            the flag, a scheduled run on a terminal would sit waiting for a password.
        selected_device: The discovered device chosen for this session, when known, so it
            can be remembered after a successful connection.
        explicit_selection: Whether ``--port``/``--ble``/``--profile`` was passed explicitly
            (which suppresses the interactive picker and auto-discovery).
    """

    console: Console
    settings: Settings
    repo: Repository
    device_store: DeviceStore
    admin_store: AdminStore
    preferences: Preferences | None = None
    advert_store: AdvertStore | None = None
    remote_store: RemoteStore | None = None
    watch_store: WatchStore | None = None
    courier_store: CourierStore | None = None
    mute_store: MuteStore | None = None
    region_store: RegionStore | None = None
    channel_store: ChannelStore | None = None
    settings_store: SettingsStore | None = None
    contact_store: ContactStore | None = None
    profile: DeviceProfile | None = None
    mock: bool = False
    port_override: str | None = None
    ble_override: str | None = None
    tcp_override: str | None = None
    spi_override: bool = False
    ble_pin: str | None = None
    output: OutputFormat = OutputFormat.PLAIN
    interactive: bool = True
    selected_device: DiscoveredDevice | None = None
    explicit_selection: bool = False
    _device: Device | None = field(default=None, init=False, repr=False)
    _active_port: str | None = field(default=None, init=False, repr=False)
    _active_transport: str | None = field(default=None, init=False, repr=False)
    _active_address: str | None = field(default=None, init=False, repr=False)
    _active_endpoint: str | None = field(default=None, init=False, repr=False)
    #: A live ``bleak.BLEDevice`` handed to :meth:`reconnect` by the flow that just heard the
    #: companion advertise again. Preferred over the startup scan's handle when reopening the
    #: link, because a device that dropped and came back is a *new* peripheral to the OS and
    #: the older handle no longer names it. Typed ``object`` so ``bleak`` stays optional.
    _ble_handle: object | None = field(default=None, init=False, repr=False)
    unpair_on_exit: bool = field(default=False, init=False, repr=False)
    #: Set by the config editor just before it sends a reboot command, so the session's
    #: disconnect watcher can label the ensuing (expected) link drop as a reboot in
    #: progress rather than a surprise unplug. Cleared by the reconnect dialog that
    #: consumes it (see :func:`meshterm.ui.menu._handle_disconnect`).
    reboot_in_progress: bool = field(default=False, init=False, repr=False)
    _resume_intent: tuple[bool, bool, bool] | None = field(default=None, init=False, repr=False)
    _events: EventHub | None = field(default=None, init=False, repr=False)
    _monitor: MonitorService | None = field(default=None, init=False, repr=False)
    _chat: ChatService | None = field(default=None, init=False, repr=False)
    _adverts: AdvertScheduler | None = field(default=None, init=False, repr=False)
    _watchtower: WatchtowerService | None = field(default=None, init=False, repr=False)
    _clock_sync: ClockSync | None = field(default=None, init=False, repr=False)
    _courier: CourierService | None = field(default=None, init=False, repr=False)
    _battery: BatteryService | None = field(default=None, init=False, repr=False)
    _devstate: DeviceState | None = field(default=None, init=False, repr=False)
    _basemap_source: BasemapSource | None = field(default=None, init=False, repr=False)
    _ui: Ui | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Load the preferences and derive the stores' default locations, where not injected.

        The preferences are also installed as the process-wide set (see
        :func:`~meshterm.core.preferences.install`), because the session's renderer and the
        stores built without a context still have to read them.
        """
        if self.preferences is None:
            self.preferences = Preferences.load(self.settings.config_dir / PREFERENCES_FILENAME)
            report_dropped(self.preferences, self.log)
        install_preferences(self.preferences)
        if self.advert_store is None:
            self.advert_store = AdvertStore(self.settings.config_dir / "adverts.json")
        if self.remote_store is None:
            self.remote_store = RemoteStore(self.settings.config_dir / "remote.json")
        if self.watch_store is None:
            self.watch_store = WatchStore(self.settings.config_dir / "watchtower.json")
        if self.courier_store is None:
            self.courier_store = CourierStore(self.settings.config_dir / "courier.json")
        if self.mute_store is None:
            self.mute_store = MuteStore(self.settings.config_dir / "mutes.json")
        if self.region_store is None:
            self.region_store = RegionStore(self.settings.config_dir / "regions.json")
        if self.channel_store is None:
            self.channel_store = ChannelStore(self.settings.config_dir / "channels.json")
        if self.settings_store is None:
            self.settings_store = SettingsStore(self.settings.config_dir / "settings.json")
        if self.contact_store is None:
            self.contact_store = ContactStore(self.settings.config_dir / "contacts.json")

    @property
    def profile_name(self) -> str | None:
        """Name of the active profile, if any."""
        return self.profile.name if self.profile else None

    @property
    def is_connected(self) -> bool:
        """Whether a device connection is currently open."""
        return self._device is not None

    @property
    def active_port(self) -> str | None:
        """The serial port the current connection is open on, if any (``None`` for --mock/BLE).

        Set when a real *serial* connection is opened so the reconnect flow knows which OS
        port to wait on. ``None`` for the simulator and for BLE/TCP connections (which have no
        serial port). Falls back to an explicit ``--port`` override when a connection hasn't
        recorded one yet.
        """
        if self.mock or self.active_transport in ("ble", "tcp", "spi"):
            return None
        return self._active_port or self.port_override

    @property
    def active_address(self) -> str | None:
        """The Bluetooth address of the current BLE connection, if any (``None`` otherwise).

        Set when a real *BLE* connection is opened, so post-session teardown (e.g. the quit
        dialog's unpair step) can address the peripheral even after the device handle is torn
        down. ``None`` for the simulator and for serial connections, which have no BLE address.
        """
        if self.mock or self.active_transport != "ble":
            return None
        return self._active_address or self.ble_override

    @property
    def active_endpoint(self) -> str | None:
        """The network ``host:port`` of the current TCP connection, if any (``None`` otherwise).

        Set when a real *TCP* connection is opened, so the reconnect flow and header can name
        the endpoint. ``None`` for the simulator and for serial/BLE connections. Falls back to
        an explicit ``--tcp`` override when a connection hasn't recorded one yet.
        """
        if self.mock or self.active_transport != "tcp":
            return None
        return self._active_endpoint or self.tcp_override

    @property
    def active_transport(self) -> str | None:
        """Which transport the current or selected connection uses.

        One of ``"serial"``, ``"ble"``, ``"tcp"``, ``"spi"``, or ``None`` for the simulator.
        For a real device it reflects the open connection when one exists, otherwise the
        transport implied by the pending selection (an explicit ``--tcp`` selects TCP,
        ``--ble`` selects BLE, ``--spi`` the SPI radio), defaulting to serial.
        """
        if self.mock:
            return None
        if self._active_transport is not None:
            return self._active_transport
        if self.spi_override or (self.profile is not None and self.profile.is_spi):
            return "spi"
        if self.tcp_override:
            return "tcp"
        return "ble" if self.ble_override else "serial"

    async def link_alive(self) -> bool:
        """Whether the current device's transport link is still up (best-effort, non-invasive).

        Delegates to the connected device's :meth:`~meshterm.core.connection.Device.link_present`
        — OS port enumeration for serial, the BLE client's connection flag for Bluetooth — so
        the session's liveness watcher is transport-agnostic. Returns ``False`` when nothing is
        connected (there is no live link), and ``True`` on any check hiccup so a transient
        lookup failure never fakes a disconnect.
        """
        device = self._device
        if device is None:
            return False
        try:
            return await device.link_present()
        except Exception:  # noqa: BLE001 - a liveness-check failure must not fake a disconnect
            return True

    @property
    def ui(self) -> Ui:
        """The active UI surface, defaulting to the plain console surface for the CLI.

        The interactive menu replaces this with a full-screen TUI surface for the session;
        scripted CLI runs use the lazily-created plain surface, which prints directly. The
        surface module (and prompt_toolkit) is imported lazily here to keep startup fast.
        """
        if self._ui is None:
            from .ui.surface import PlainUi

            self._ui = PlainUi(self.console)
        return self._ui

    @ui.setter
    def ui(self, value: Ui) -> None:
        """Install a UI surface (used by the menu to switch to the full-screen TUI)."""
        self._ui = value

    @property
    def events(self) -> EventHub:
        """Return the session's always-on event hub, creating it on first use.

        The hub owns the single device event subscription and fans events out to any
        number of subscribers (the passive monitor's logging is one of them). It is
        created idle here; the interactive session starts it once a device is available.
        """
        if self._events is None:
            from .services.event_hub import EventHub

            self._events = EventHub(self)
        return self._events

    @property
    def monitor(self) -> MonitorService:
        """Return the session's passive-monitor service, creating it on first use.

        The service is built lazily so the (cheap) database read for the "total"
        observation count happens only once, when monitoring is first referenced.
        """
        if self._monitor is None:
            from .services.monitor_service import MonitorService

            self._monitor = MonitorService(self)
        return self._monitor

    @property
    def chat(self) -> ChatService:
        """Return the session's chat service, creating it on first use.

        The service records inbound messages to history (as a subscriber of the always-on
        event hub) and owns the outbound send path and the per-conversation unread counts.
        It is created idle here; the interactive session starts it once a device is
        available (and the live chat screen starts it lazily otherwise).
        """
        if self._chat is None:
            from .services.chat_service import ChatService

            self._chat = ChatService(self)
        return self._chat

    @property
    def adverts(self) -> AdvertScheduler:
        """Return the session's background-advert scheduler, creating it on first use.

        Created idle here; the interactive session starts it alongside the other
        always-on services. Scripted CLI runs never start it, so a one-shot command
        can't fire a background transmission.
        """
        if self._adverts is None:
            from .services.advert_scheduler import AdvertScheduler

            self._adverts = AdvertScheduler(self)
        return self._adverts

    @property
    def watchtower(self) -> WatchtowerService:
        """Return the session's Watchtower sentinel, creating it on first use.

        Created idle here; the interactive session starts it alongside the other
        always-on services. It only ever listens (rules over hub events), so scripted
        CLI runs simply never start it.
        """
        if self._watchtower is None:
            from .services.watchtower import WatchtowerService

            self._watchtower = WatchtowerService(self)
        return self._watchtower

    @property
    def clock_sync(self) -> ClockSync:
        """Return the session's on-connect clock setter, creating it on first use.

        Created idle here; the interactive session starts it alongside the other
        always-on services, and :meth:`device` tells it about each connection as it
        settles. Scripted CLI runs never start it, so a one-shot command that only reads
        the radio never writes its clock.
        """
        if self._clock_sync is None:
            from .services.clock_sync import ClockSync

            self._clock_sync = ClockSync(self)
        return self._clock_sync

    @property
    def courier(self) -> CourierService:
        """Return the session's store-and-forward courier, creating it on first use.

        Created idle here; the interactive session starts it alongside the other
        always-on services. Scripted CLI runs never start the loop, so queueing a
        message from a script transmits nothing until an interactive session runs.
        """
        if self._courier is None:
            from .services.courier import CourierService

            self._courier = CourierService(self)
        return self._courier

    @property
    def battery(self) -> BatteryService:
        """Return the session's battery poller, creating it on first use.

        Created idle here; the interactive session starts it alongside the other
        always-on services. It only ever reads the pack (best-effort, skipping quietly
        without a device), so scripted CLI runs simply never start the loop.
        """
        if self._battery is None:
            from .services.battery_service import BatteryService

            self._battery = BatteryService(self)
        return self._battery

    @property
    def devstate(self) -> DeviceState:
        """Return the session's device-state cache, creating it on first use.

        Holds the stable facts screens read from the companion on open (contacts, self-info,
        path-hash mode, channel slots) so navigation doesn't re-read them from the radio every
        time — the chief cause of slow screen transitions over Bluetooth. Created idle here;
        each getter fetches lazily. Cleared on a reconnect (see :meth:`reconnect`).
        """
        if self._devstate is None:
            from .services.device_state import DeviceState

            self._devstate = DeviceState(self)
        return self._devstate

    @property
    def basemap_source(self) -> BasemapSource:
        """Return the session's shared map tile source, built once and reused.

        The map, the location picker, and the Node-detail location preview all draw the
        same OpenStreetMap basemap; each used to build its own :class:`BasemapSource`, so
        each paid the one-off TileJSON resolve — a blocking network round-trip the first
        time ``.max_zoom``/``.available`` is touched — on *every* open. Holding one source
        for the session means that resolve happens once (warmed behind the menu, see
        :func:`meshterm.ui.menu._warm_basemap`), and the on-disk tile cache is shared too.
        Independent of the radio link, so it survives reconnects untouched.
        """
        if self._basemap_source is None:
            from .services.basemap import BasemapSource
            from .ui.map_render import DRAWN_LAYERS

            self._basemap_source = BasemapSource(
                self.settings.config_dir / "tilecache",
                layers=DRAWN_LAYERS,
                tilejson_url=self.preferences.basemap_tilejson_url,
            )
        return self._basemap_source

    @property
    def log(self):  # type: ignore[no-untyped-def]
        """The application logger."""
        return get_logger()

    def adopt_device(self, device: Device) -> None:
        """Adopt an already-connected device as the session's device.

        Used by the startup picker: it opens and confirms the chosen companion during its
        smoke test, and hands that live connection here so :meth:`device` reuses it instead
        of opening the radio a second time (many boards reset on each serial open, making a
        reconnect slow and unreliable).

        Args:
            device: A connected :class:`Device` to serve as this session's radio.
        """
        self._device = device
        # Record the transport and endpoint the picker opened directly, bypassing the
        # resolution in ``device()`` that normally sets them — so the liveness watcher knows
        # what to poll and ``reconnect`` can rebuild the same connection.
        self._active_transport = getattr(device, "transport", "serial")
        self._active_port = getattr(device, "_port", None)
        self._active_address = getattr(device, "_address", None)
        self._active_endpoint = (
            getattr(device, "endpoint", None) if (self._active_transport == "tcp") else None
        )

    async def _open(self, what: str) -> None:
        """Open :attr:`_device`, reporting a failure to open as a *selection* failure.

        The two device exit statuses answer different questions — ``NO_DEVICE`` says
        nothing was transmitted and the caller should look at what is plugged in;
        ``DEVICE`` says the radio was reached and the operation failed, so retrying is
        reasonable. Every failure on this path is the first kind: the connection does not
        exist yet, so nothing has been sent over it. It used to come back as ``DEVICE``
        with the message "the connection to the device was lost", which describes a
        connection there had never been — and ``--port NOSUCHPORT`` is the most common way
        to reach this line.

        Args:
            what: The thing being opened, for the message (a port, an address, a host).

        Raises:
            DeviceSelectionError: If the connection cannot be established.
        """
        from .core.selection import DeviceSelectionError

        try:
            await self._device.connect()
        except Exception as exc:  # noqa: BLE001 - reclassified, then re-raised
            raise DeviceSelectionError(f"could not open {what}: {exc}") from exc

    async def device(self) -> Device:
        """Return a connected :class:`Device`, opening the connection on first use.

        For real hardware this resolves which serial port to use (explicit ``--port`` /
        profile / remembered default / sole attached device) and, on a successful
        connection, records the device as the new "last known good" default.

        Returns:
            The shared, connected device for this invocation.

        Raises:
            ValueError: If no serial port can be chosen and the simulator is not enabled.
            DeviceSelectionError: If discovery is ambiguous (subclass of ``ValueError``).
        """
        if self._device is not None:
            return self._device

        if self.mock:
            self._device = make_device(mock=True, port=None)
            self._active_transport = None
            self._active_port = None
            self._active_address = None
            self._active_endpoint = None
            await self._open("the simulator")
            return self._device

        # A radio on the host's own SPI bus (``--spi``, an SPI profile, or the remembered
        # default) has nothing to find: MeshTerm starts the node that answers on it.
        spi = self.resolve_spi()
        if spi is not None:
            from .core.spiradio import state_dir

            self._device = make_device(
                mock=False,
                port=None,
                transport="spi",
                spi=spi,
                state=state_dir(self.settings.config_dir, spi),
            )
            self._active_transport = "spi"
            self._active_endpoint = spi.spidev
            self._active_port = None
            self._active_address = None
            await self._open(f"SPI radio {spi.spidev}")
            await self._settle_connection()
            return self._device

        # A network endpoint (an explicit ``--tcp``, a TCP profile, a device picked at startup,
        # or the remembered TCP default) is opened directly by host:port — TCP companions
        # aren't discoverable, so this remembered/explicit endpoint is the only way to reach one.
        tcp_host, tcp_port = self._resolve_tcp_endpoint()
        if tcp_host and tcp_port:
            self._device = make_device(
                mock=False, port=None, transport="tcp", host=tcp_host, tcp_port=tcp_port
            )
            self._active_transport = "tcp"
            self._active_endpoint = f"{tcp_host}:{tcp_port}"
            self._active_port = None
            self._active_address = None
            await self._open(f"TCP companion {tcp_host}:{tcp_port}")
            await self._settle_connection()
            return self._device

        # A Bluetooth endpoint (an explicit ``--ble``, a BLE profile, a device picked at
        # startup, or the remembered BLE default) is opened directly by address — no serial
        # resolution, and no re-scan needed to reconnect to a known address.
        ble_address, ble_pin = self._resolve_ble_endpoint()
        if ble_address:
            # When a scan has produced a live BLEDevice for this address, hand it to the
            # connection so bleak opens it directly instead of re-discovering the address (a
            # fresh internal scan that intermittently misses a slow-advertising companion).
            # A remembered/explicit address has no scan result and connects by address alone.
            # A handle from the reconnect watch wins over the startup picker's: it was scanned
            # seconds ago, after the drop, so it names the peripheral that is on the air *now*.
            selected = self.selected_device
            ble_device = self._ble_handle or (
                selected.ble_device
                if selected is not None and selected.is_ble and selected.address == ble_address
                else None
            )
            self._device = make_device(
                mock=False,
                port=None,
                transport="ble",
                address=ble_address,
                pin=ble_pin,
                ble_device=ble_device,
            )
            self._active_transport = "ble"
            self._active_address = ble_address
            self._active_port = None
            self._active_endpoint = None
            await self._open(f"BLE companion {ble_address}")
            await self._settle_connection()
            return self._device

        resolution = resolve_device(
            discover_devices(),
            self.device_store.load(),
            explicit_port=self.port_override,
            profile=self.profile,
        )
        if resolution.device is not None:
            self.selected_device = resolution.device
        baudrate = self.profile.baudrate if self.profile else 115200
        self._device = make_device(mock=False, port=resolution.port, baudrate=baudrate)
        self._active_transport = "serial"
        self._active_port = resolution.port
        self._active_address = None
        self._active_endpoint = None
        await self._open(f"serial port {resolution.port}")
        await self._settle_connection()
        return self._device

    def _resolve_tcp_endpoint(self) -> tuple[str | None, int | None]:
        """Return the ``(host, port)`` to open over TCP, or ``(None, None)`` for other transports.

        Resolves a network endpoint in priority order — an explicit ``--tcp``, a TCP
        :class:`~meshterm.core.config.DeviceProfile`, then the remembered TCP default — so a
        network companion is honored wherever a serial/Bluetooth one would be. A malformed
        endpoint yields ``(None, None)`` (the session then falls through to the other
        transports rather than crashing on a typo).
        """
        from .core.discovery import parse_tcp_endpoint
        from .core.selection import DeviceSelectionError

        # An explicit ``--tcp`` or TCP profile is authoritative — a malformed value is the
        # user's typo, so surface the parse error as a clean, actionable message rather than
        # silently falling through to another transport.
        explicit_endpoint = self.tcp_override or (
            self.profile.tcp_endpoint if self.profile is not None and self.profile.is_tcp else None
        )
        if explicit_endpoint:
            try:
                return parse_tcp_endpoint(explicit_endpoint)
            except ValueError as exc:
                raise DeviceSelectionError(str(exc)) from exc
        # A remembered TCP default only applies when nothing else was selected explicitly, and
        # a corrupt stored value must never crash startup — fall through to the other transports.
        explicit_other = (
            bool(self.port_override)
            or bool(self.ble_override)
            or self.spi_override
            or (
                self.profile is not None
                and (bool(self.profile.port) or bool(self.profile.address) or self.profile.is_spi)
            )
        )
        if explicit_other:
            return None, None
        remembered = self.device_store.load()
        if remembered is not None and remembered.is_tcp and remembered.target:
            try:
                host, port = parse_tcp_endpoint(remembered.target)
            except ValueError:
                return None, None
            self.selected_device = None  # remembered, not freshly discovered this session
            return host, port
        return None, None

    def resolve_spi(self) -> SpiWiring | None:
        """The wiring of the SPI radio to open, or ``None`` when another transport is meant.

        In priority order: an SPI profile, the radio picked on the startup splash, ``--spi``
        (the one radio attached — see :meth:`_spi_for_flag`), then the remembered default when it
        was an SPI radio and nothing else was named. Anything else named explicitly — a port,
        an address, a host, another kind of profile — means this session is not about the SPI
        radio.
        """
        if self.profile is not None and self.profile.is_spi:
            return self.profile.spi or SpiWiring()
        chosen = self.selected_device
        if chosen is not None and chosen.is_spi:
            return chosen.spi or self.spi_wiring_for(chosen.port)  # type: ignore[return-value]
        if self.spi_override:
            return self._spi_for_flag()
        if self.port_override or self.ble_override or self.tcp_override or self.profile:
            return None
        remembered = self.device_store.load()
        if remembered is None or remembered.transport != "spi" or not remembered.target:
            return None
        self.selected_device = None  # remembered, not freshly discovered this session
        return self.spi_wiring_for(remembered.target)

    def _spi_for_flag(self) -> SpiWiring:
        """The radio a bare ``--spi`` means: the one there is, and never a guess between two.

        Nearly everyone with a radio on the SPI bus has exactly one, and for them ``--spi``
        needs nothing more — attached radios are counted first, so a single one is used
        whether or not a profile names it. Only when two are attached, or (with none
        attached to count) two SPI profiles are configured, is the flag ambiguous; it used
        to take whichever profile ``config.toml`` listed first, which is a coin toss that
        transmits. With none attached and one profile or none, the choice is made from the
        configuration, so the connect attempt can say what is missing.

        Raises:
            DeviceSelectionError: When more than one radio could be meant.
        """
        from .core.selection import DeviceSelectionError
        from .core.spiradio import spi_radios

        profiles = self.settings.profiles
        attached = spi_radios(profiles)
        if len(attached) == 1:
            return attached[0].spi  # type: ignore[return-value]
        configured = [p for p in profiles.values() if p.is_spi]
        if not attached and len(configured) <= 1:
            return (configured[0].spi or SpiWiring()) if configured else SpiWiring()
        if attached:
            rows = [f"  {d.name or '(no profile)':<16} {d.port}" for d in attached]
            what = "Several radios are attached to the SPI bus"
        else:
            rows = [f"  {p.name:<16} {(p.spi or SpiWiring()).spidev}" for p in configured]
            what = "Several SPI profiles are configured"
        raise DeviceSelectionError(
            f"{what}, and --spi doesn't say which.\n"
            + "\n".join(rows)
            + "\nChoose one with -p <PROFILE>. A radio with no profile needs one first — see "
            "'Adding a radio on the SPI bus' in docs/configuration.md."
        )

    def spi_wiring_for(self, spidev: str) -> SpiWiring:
        """The wiring for the radio on ``spidev``: a profile's that names it, else the AIO's.

        A remembered or listed SPI radio is known by its device node alone, so its pins come
        from whichever SPI profile is on that node; with none, the defaults are the only
        wiring there is (and the right one for the board they describe).
        """
        for profile in self.settings.profiles.values():
            if profile.is_spi and (profile.spi or SpiWiring()).spidev == spidev:
                return profile.spi or SpiWiring()
        return SpiWiring()

    def _resolve_ble_endpoint(self) -> tuple[str | None, str | None]:
        """Return the ``(address, pin)`` to open over Bluetooth, or ``(None, None)`` for serial.

        Resolves a BLE endpoint in priority order — an explicit ``--ble``, a BLE
        :class:`~meshterm.core.config.DeviceProfile`, then the remembered BLE default — so a
        Bluetooth companion is honored wherever a serial one would be. Returns ``(None, None)``
        when the session should fall through to serial resolution.
        """
        if self.ble_override:
            return self.ble_override, self.ble_pin
        # An explicit serial selection (``--port`` or a serial profile with a port) wins over a
        # remembered BLE default, mirroring the serial resolution priority.
        explicit_serial = bool(self.port_override) or (
            self.profile is not None and not self.profile.is_ble and bool(self.profile.port)
        )
        if self.profile is not None and self.profile.is_ble and self.profile.address:
            return self.profile.address, self.ble_pin or self.profile.ble_pin
        if explicit_serial:
            return None, None
        remembered = self.device_store.load()
        if remembered is not None and remembered.is_ble and remembered.target:
            self.selected_device = None  # remembered, not freshly discovered this session
            return remembered.target, self.ble_pin
        return None, None

    async def _remember_connected(self) -> None:
        """Record the just-connected device as the last known good default (best-effort).

        Learns the device's mesh node name (a probe failure must not block a good
        connection). Only devices discovered this session carry a
        :class:`~meshterm.core.discovery.DiscoveredDevice` to remember; a bare ``--port`` or
        remembered-by-address reconnect has nothing new to upsert.
        """
        if self.selected_device is not None:
            self.device_store.remember(
                self.selected_device,
                node_name=await self._node_name(),
                hardware_model=await self._hardware_model(),
            )

    async def _settle_connection(self) -> None:
        """The steps every fresh connection gets once the link is open, in order.

        Remember the device as the default, replay its remembered channels, and hand it to
        the on-connect clock setter — which returns at once and does its work in the
        background, so the connection is usable the moment this returns. Each step is
        best-effort on its own; none can fail the connection.
        """
        await self._remember_connected()
        await self._reconcile_channels()
        if self._device is not None:
            self.clock_sync.on_connected(self._device)

    async def _reconcile_channels(self) -> None:
        """Replay channels remembered for this device that it isn't reporting (best-effort).

        A firmware-less radio bridge loses its channels whenever it restarts, so the channels
        you added through MeshTerm are restored into free slots on connect (see
        :func:`~meshterm.core.channel_store.reconcile`). With nothing remembered for the device
        this is a single identity probe, so a firmware radio pays almost nothing. A failure here
        must never break connecting — it is logged and swallowed.
        """
        if self._device is None or self.channel_store is None:
            return
        from .core.channel_store import reconcile

        try:
            restored = await reconcile(self.channel_store, self._device)
        except Exception as exc:  # noqa: BLE001 - channel replay must not block a connection
            self.log.debug("channels: reconcile on connect failed: %s", exc)
            return
        if restored:
            self.log.info("channels: restored %d remembered channel(s) to the device", restored)

    async def release_link(self) -> None:
        """Tear down the dead connection and the services riding on it, keeping what to resume.

        The first half of :meth:`reconnect`, split out because it is also worth doing *before*
        going looking for the device. Over Bluetooth that ordering is load-bearing: a companion
        only advertises while nothing is connected to it, so a peripheral we are still holding
        a link to — however dead we believe that link to be — stays off the air and can never
        be found by the scan waiting for it to come back. Releasing first puts it back on the
        air, and is harmless on the transports that don't care.

        What was running is captured *once* and held across retries: this stops the services,
        so a later attempt would otherwise read the now-idle flags and restore nothing. The
        intent is cleared only after :meth:`reconnect` actually succeeds. Idempotent — calling
        it again with nothing connected is a no-op.
        """
        if self._resume_intent is None:
            self._resume_intent = (
                self._events is not None and self._events.active,
                self._monitor is not None and self._monitor.active,
                self._chat is not None and self._chat.active,
            )

        # Release the stale hub/service subscriptions and discard the dead device. The
        # subscriptions are in-process (to the hub), so they survive the link drop and must
        # be torn down explicitly before a fresh connection is opened underneath them.
        if self._monitor is not None:
            await self._monitor.stop()
        if self._chat is not None:
            await self._chat.stop()
        if self._events is not None:
            await self._events.stop()
        if self._device is not None:
            try:
                await self._device.disconnect()
            except Exception:  # noqa: BLE001 - the link is already gone; best-effort
                pass
            self._device = None

        # Facts cached against the connection that just dropped may be stale on the fresh link
        # (a reboot could have changed the identity or config), so re-read them on next use.
        if self._devstate is not None:
            self._devstate.reset()

    async def reconnect(self, *, ble_device: object | None = None) -> None:
        """Drop a lost device connection and rebuild it, restoring live services.

        Called after the companion link is detected as gone (see
        :func:`~meshterm.core.connection.is_connection_lost`). Tears down the dead
        connection and the services riding on it, opens a fresh connection to the same
        device, then restarts whatever was running before — so passive monitoring and chat
        recording resume transparently across a replug.

        Args:
            ble_device: The live ``bleak.BLEDevice`` the caller just scanned for this address,
                when it has one (see :func:`~meshterm.core.discovery.find_ble_device`). Any
                previously-held handle is dropped either way: the peripheral that comes back
                from a power-cycle is a new one to the OS, so the scan result the session
                opened with is not merely stale but wrong, and reusing it fails the connect
                on Windows while the device sits there advertising. Passing ``None`` (serial,
                TCP, or a BLE retry with nothing scanned) reconnects by address alone.

        Raises:
            Exception: If a new connection could not be opened (e.g. the device is still
                absent); the caller can surface it and offer to retry.
        """
        self._ble_handle = ble_device
        await self.release_link()
        resume_events, resume_monitor, resume_chat = self._resume_intent

        # Open a fresh connection (raises if the device still can't be reached, leaving the
        # remembered intent in place for the next attempt), then restart whatever was running
        # before it dropped and clear the intent now that we're back.
        await self.device()
        if resume_events:
            await self.events.start()
        if resume_monitor:
            await self.monitor.start()
        if resume_chat:
            await self.chat.start()
        self._resume_intent = None
        # The handle has been opened; it names a link that is now live rather than a device to
        # go find, so the next drop starts from a clean slate and scans afresh.
        self._ble_handle = None

    async def _node_name(self) -> str:
        """Return the connected device's own mesh node name, or ``""`` if unavailable."""
        try:
            info = await self._device.get_self_info()
        except Exception:  # noqa: BLE001 - identity probe is best-effort, never fatal
            return ""
        return str(info.get("adv_name") or info.get("name") or "")

    async def _hardware_model(self) -> str:
        """Return the connected device's firmware model string, or ``""`` if unavailable.

        Sourced from the device-query frame — the only place the model is exposed — so a
        reconnect keeps the remembered hardware column fresh. Best-effort: a probe failure or
        older firmware just leaves any previously-remembered model untouched.
        """
        try:
            info = await self._device.get_device_info()
        except Exception:  # noqa: BLE001 - model lookup is best-effort, never fatal
            return ""
        return str(info.get("model") or "")

    async def aclose(self) -> None:
        """Stop monitoring and chat, stop the event hub, disconnect, and close the repo."""
        if self._devstate is not None:
            await self._devstate.aclose()
        if self._courier is not None:
            await self._courier.aclose()
        if self._battery is not None:
            await self._battery.aclose()
        if self._watchtower is not None:
            await self._watchtower.aclose()
        if self._clock_sync is not None:
            await self._clock_sync.aclose()
        if self._adverts is not None:
            await self._adverts.aclose()
        if self._monitor is not None:
            await self._monitor.aclose()
        if self._chat is not None:
            await self._chat.aclose()
        if self._events is not None:
            await self._events.aclose()
        if self._device is not None:
            try:
                await self._device.disconnect()
            except Exception:  # noqa: BLE001 - a dead/lost link must not crash teardown
                pass
            self._device = None
        self.repo.close()
