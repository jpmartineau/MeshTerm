# SPDX-License-Identifier: Apache-2.0
"""The application context: a small dependency container that MeshTerm gives to each tool.

``AppContext`` owns the shared singletons that are expensive to make (console, settings,
repository, logger). It manages the device connection lazily. Thus a tool that does not
use the radio never opens a serial port.
"""

from __future__ import annotations

import asyncio
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
from .core.room_store import RoomStore
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
    from .services.rooms import RoomService
    from .services.watchtower import WatchtowerService
    from .ui.surface import Ui


@dataclass(slots=True)
class AppContext:
    """The shared services and the state of one run, which MeshTerm gives to tools.

    Attributes:
        console: The Rich console for all rendering.
        settings: The loaded application settings. They say where things are and which
            device to talk to (config directory, database, named profiles). How MeshTerm
            behaves is in ``preferences``, not here.
        preferences: The preferences of MeshTerm itself. They are backed by
            ``<config_dir>/preferences.toml``, and the user edits them on the Preferences
            page. If the caller does not inject them, the context loads them from that file.
            The context installs them as the process-wide
            :func:`~meshterm.core.preferences.current` set. Thus the render layer has no
            context to reach through, and it reads the same values.
        repo: The database repository.
        profile: The active device profile, if the context resolved one.
        device_store: The store for the remembered "last known good" device.
        admin_store: The store for the remembered admin passwords of remote nodes.
        advert_store: The store for the background-advert schedules of each device (if the
            caller does not inject it, the default is ``<config_dir>/adverts.json``).
        remote_store: The store for the admin state of remote nodes: cached settings and CLI
            history (if the caller does not inject it, the default is
            ``<config_dir>/remote.json``).
        watch_store: The store for the Watchtower: watched nodes, rules, and the alert log
            (if the caller does not inject it, the default is
            ``<config_dir>/watchtower.json``).
        courier_store: The store for the outbox of the Courier: queued, delivered, and
            given-up messages (the default is ``<config_dir>/courier.json``).
        mute_store: The store for muted channel notifications. These are the channels whose
            new messages do not raise the unread badge (if the caller does not inject it, the
            default is ``<config_dir>/mutes.json``).
        region_store: The store for the regions that have a name, and for the send scope of
            each channel. MeshTerm resolves a scoped flood against it (if the caller does not
            inject it, the default is ``<config_dir>/regions.json``).
        channel_store: The store for channels that the user created through MeshTerm. The
            context replays them into a device that forgot them (a radio bridge without
            firmware). The store uses the public key of the device as its key. If the caller
            does not inject it, the default is ``<config_dir>/channels.json``.
        settings_store: The store for settings that the user changed through MeshTerm.
            MeshTerm offers them again at connection to a device that forgot them (a radio
            bridge without firmware). The store uses the public key of the device as its key.
            If the caller does not inject it, the default is ``<config_dir>/settings.json``.
        contact_store: The store for contacts that MeshTerm read. MeshTerm merges them back
            into the contact lists for a device that forgot them (a radio bridge without
            firmware). The store uses the public key of the device as its key. If the caller
            does not inject it, the default is ``<config_dir>/contacts.json``.
        room_store: The store for the rooms that this machine joined: the password of each
            room and the access that it granted (if the caller does not inject it, the
            default is ``<config_dir>/rooms.json``).
        mock: Whether the context uses the simulator device.
        port_override: An explicit serial port (from ``--port`` or the interactive picker),
            which overrides the profile.
        ble_override: An explicit Bluetooth address (from ``--ble`` or the interactive
            picker), which selects the BLE transport. It has precedence over
            ``port_override``.
        tcp_override: An explicit network address ``host:port`` (from ``--tcp`` or the
            interactive picker), which selects the TCP transport. It has precedence over
            ``ble_override`` and ``port_override``.
        ble_pin: The optional BLE pairing PIN for the chosen Bluetooth device.
        output: The format in which a scripted run prints its answer (refer to
            :class:`~meshterm.ui.renderers.OutputFormat`). A tool never reads this to decide
            what to say. It states its answer as a :class:`~meshterm.ui.report.Report`, and
            the CLI boundary picks the renderer. The two streaming commands read it only to
            open the correct type of stream.
        interactive: Whether this run can stop and ask a person a question. It is false for
            each scripted run whose stdin is not a terminal. A tool must have the answer to
            this question before it prompts. ``tx-optimize`` used to check ``--json``
            instead. A flag about the output format cannot answer it. Without the flag, a
            scheduled run on a terminal would wait for a password.
        selected_device: The discovered device that was chosen for this session, if it is
            known. MeshTerm can remember it after a successful connection.
        explicit_selection: Whether the user passed ``--port``, ``--ble``, or ``--profile``
            explicitly. This suppresses the interactive picker and auto-discovery.
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
    room_store: RoomStore | None = None
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
    #: A live ``bleak.BLEDevice`` that the flow gives to :meth:`reconnect`. The flow just heard
    #: the companion advertise again. MeshTerm prefers it to the handle of the startup scan
    #: when it reopens the link. A device that dropped and came back is a new peripheral to
    #: the OS, and the older handle does not name it now. The type is ``object``, so that
    #: ``bleak`` stays optional.
    _ble_handle: object | None = field(default=None, init=False, repr=False)
    unpair_on_exit: bool = field(default=False, init=False, repr=False)
    #: :meth:`announce_reboot` sets this after a reboot command has gone out. Then the
    #: reconnect dialog shows the drop as a reboot in progress, and not as a surprise unplug.
    #: :meth:`take_reboot` clears it in the dialog that uses it (refer to
    #: :func:`meshterm.ui.menu._handle_disconnect`).
    reboot_in_progress: bool = field(default=False, init=False, repr=False)
    #: MeshTerm sets this together with :attr:`reboot_in_progress`. It wakes the disconnect
    #: watcher of the session at once. The watcher does not wait for a liveness poll to
    #: observe the drop, and the poll can fail to observe it at all, because a board
    #: behind a USB-UART bridge keeps its port through a reboot.
    _link_going_down: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _resume_intent: tuple[bool, bool, bool] | None = field(default=None, init=False, repr=False)
    _events: EventHub | None = field(default=None, init=False, repr=False)
    _monitor: MonitorService | None = field(default=None, init=False, repr=False)
    _chat: ChatService | None = field(default=None, init=False, repr=False)
    _rooms: RoomService | None = field(default=None, init=False, repr=False)
    _adverts: AdvertScheduler | None = field(default=None, init=False, repr=False)
    _watchtower: WatchtowerService | None = field(default=None, init=False, repr=False)
    _clock_sync: ClockSync | None = field(default=None, init=False, repr=False)
    _courier: CourierService | None = field(default=None, init=False, repr=False)
    _battery: BatteryService | None = field(default=None, init=False, repr=False)
    _devstate: DeviceState | None = field(default=None, init=False, repr=False)
    _basemap_source: BasemapSource | None = field(default=None, init=False, repr=False)
    _ui: Ui | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Load the preferences and derive the default locations of the stores.

        This applies to each item that the caller did not inject. The method also installs
        the preferences as the process-wide set (refer to
        :func:`~meshterm.core.preferences.install`). The renderer of the session and the
        stores that are built without a context must read them.
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
        if self.room_store is None:
            self.room_store = RoomStore(self.settings.config_dir / "rooms.json")

    @property
    def profile_name(self) -> str | None:
        """The name of the active profile, if there is one."""
        return self.profile.name if self.profile else None

    @property
    def is_connected(self) -> bool:
        """Whether a device connection is open now."""
        return self._device is not None

    @property
    def active_port(self) -> str | None:
        """The serial port of the current connection, if there is one (``None`` for --mock/BLE).

        MeshTerm sets this when it opens a real serial connection. Then the reconnect flow
        knows which OS port to wait for. It is ``None`` for the simulator and for BLE and TCP
        connections, which have no serial port. If a connection has not recorded a port yet,
        the value is the explicit ``--port`` override.
        """
        if self.mock or self.active_transport in ("ble", "tcp", "spi"):
            return None
        return self._active_port or self.port_override

    @property
    def active_address(self) -> str | None:
        """The Bluetooth address of the current BLE connection, if there is one.

        The value is ``None`` in all other cases. MeshTerm sets this when it opens a real BLE
        connection. Then the teardown after the session (for example the unpair step of the
        quit dialog) can address the peripheral, also after the handle of the device is
        closed. It is ``None`` for the simulator and for serial connections, which have no BLE
        address.
        """
        if self.mock or self.active_transport != "ble":
            return None
        return self._active_address or self.ble_override

    @property
    def active_endpoint(self) -> str | None:
        """The network ``host:port`` of the current TCP connection, if there is one.

        The value is ``None`` in all other cases. MeshTerm sets this when it opens a real TCP
        connection. Then the reconnect flow and the header can name the endpoint. It is
        ``None`` for the simulator and for serial and BLE connections. If a connection has
        not recorded an endpoint yet, the value is the explicit ``--tcp`` override.
        """
        if self.mock or self.active_transport != "tcp":
            return None
        return self._active_endpoint or self.tcp_override

    @property
    def active_transport(self) -> str | None:
        """The transport that the current or selected connection uses.

        The value is ``"serial"``, ``"ble"``, ``"tcp"``, or ``"spi"``, or ``None`` for the
        simulator. For a real device, it is the transport of the open connection if there is
        one. If not, it is the transport that the pending selection means (an explicit
        ``--tcp`` selects TCP, ``--ble`` selects BLE, and ``--spi`` selects the SPI radio).
        The default is serial.
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
        """Whether the transport link of the current device is still up.

        This is a best-effort check, and it does not disturb the link. The method calls
        :meth:`~meshterm.core.connection.Device.link_present` of the connected device. For
        serial, this is the enumeration of OS ports. For Bluetooth, it is the connection flag
        of the BLE client. Thus the liveness watcher of the session does not depend on the
        transport. The method returns ``False`` when nothing is connected, because there is no
        live link. If the check fails, it returns ``True``. Thus a temporary failure of the
        lookup never gives a false disconnect.
        """
        device = self._device
        if device is None:
            return False
        try:
            return await device.link_present()
        except Exception:  # noqa: BLE001 - a failure of the liveness check must not fake a disconnect
            return True

    def announce_reboot(self) -> None:
        """Declare the link down, because MeshTerm just told the companion to reboot.

        Call this method after MeshTerm sent the reboot command, and never before. The
        watcher that it wakes cancels the screen that sent the command. If the cancel occurs
        during the write, the command can be only partly sent. From then on, MeshTerm cannot
        talk to the device until it comes back. Thus the disconnect watcher of the session
        (:meth:`link_going_down`) fires at once, and the reconnect dialog takes over. The
        alternative is to hope that a liveness poll catches the drop. The poll misses a quick
        reboot completely where the serial port never goes away.
        """
        self.reboot_in_progress = True
        self._link_going_down.set()

    async def link_going_down(self) -> None:
        """Resolve when :meth:`announce_reboot` has declared the link down."""
        await self._link_going_down.wait()

    def take_reboot(self) -> bool:
        """Use a pending reboot announcement, and return whether there was one.

        The method clears both parts. Thus a later disconnect that is real uses the generic
        wording again, and the next watcher waits for a real drop again.
        """
        pending = self.reboot_in_progress
        self.reboot_in_progress = False
        self._link_going_down.clear()
        return pending

    @property
    def ui(self) -> Ui:
        """The active UI surface. The default for the CLI is the plain console surface.

        For the session, the interactive menu replaces it with a full-screen TUI surface.
        Scripted CLI runs use the plain surface, which MeshTerm creates lazily, and which
        prints directly. The property imports the surface module (and prompt_toolkit) lazily,
        to keep the startup fast.
        """
        if self._ui is None:
            from .ui.surface import PlainUi

            self._ui = PlainUi(self.console)
        return self._ui

    @ui.setter
    def ui(self, value: Ui) -> None:
        """Install a UI surface. The menu uses this to change to the full-screen TUI."""
        self._ui = value

    @property
    def events(self) -> EventHub:
        """Return the always-on event hub of the session, and create it at first use.

        The hub owns the single subscription to the device events. It sends the events to
        any number of subscribers (the logging of the passive monitor is one of them). The
        property creates it idle. The interactive session starts it when a device is
        available.
        """
        if self._events is None:
            from .services.event_hub import EventHub

            self._events = EventHub(self)
        return self._events

    @property
    def monitor(self) -> MonitorService:
        """Return the passive-monitor service of the session, and create it at first use.

        MeshTerm builds the service lazily. Thus the database read for the "total" count of
        observations (which is cheap) happens only one time, when the code first refers to
        monitoring.
        """
        if self._monitor is None:
            from .services.monitor_service import MonitorService

            self._monitor = MonitorService(self)
        return self._monitor

    @property
    def chat(self) -> ChatService:
        """Return the chat service of the session, and create it at first use.

        The service stores received messages in the history (as a subscriber of the always-on
        event hub). It owns the path for sent messages and the unread count of each
        conversation. The property creates it idle. The interactive session starts it when a
        device is available. If it is not started then, the live chat screen starts it
        lazily.
        """
        if self._chat is None:
            from .services.chat_service import ChatService

            self._chat = ChatService(self)
        return self._chat

    @property
    def rooms(self) -> RoomService:
        """Return the room service of the session, and create it at first use.

        The service joins room servers. For the session, it remembers what access each room
        server gave us, and when MeshTerm last heard it. This decides whether the opening
        of a room logs in to it again. The service never transmits on its own.
        """
        if self._rooms is None:
            from .services.rooms import RoomService

            self._rooms = RoomService(self)
        return self._rooms

    @property
    def adverts(self) -> AdvertScheduler:
        """Return the background-advert scheduler of the session, and create it at first use.

        The property creates it idle. The interactive session starts it with the other
        always-on services. Scripted CLI runs never start it. Thus a one-shot command cannot
        start a background transmission.
        """
        if self._adverts is None:
            from .services.advert_scheduler import AdvertScheduler

            self._adverts = AdvertScheduler(self)
        return self._adverts

    @property
    def watchtower(self) -> WatchtowerService:
        """Return the Watchtower sentinel of the session, and create it at first use.

        The property creates it idle. The interactive session starts it with the other
        always-on services. It only listens (it applies rules to the events of the hub).
        Scripted CLI runs never start it.
        """
        if self._watchtower is None:
            from .services.watchtower import WatchtowerService

            self._watchtower = WatchtowerService(self)
        return self._watchtower

    @property
    def clock_sync(self) -> ClockSync:
        """Return the clock setter that runs at connection, and create it at first use.

        The property creates it idle. The interactive session starts it with the other
        always-on services, and :meth:`device` tells it about each connection when the
        connection settles. Scripted CLI runs never start it. Thus a one-shot command that
        only reads the radio never writes its clock.
        """
        if self._clock_sync is None:
            from .services.clock_sync import ClockSync

            self._clock_sync = ClockSync(self)
        return self._clock_sync

    @property
    def courier(self) -> CourierService:
        """Return the store-and-forward courier of the session, and create it at first use.

        The property creates it idle. The interactive session starts it with the other
        always-on services. Scripted CLI runs never start the loop. Thus if a script queues a
        message, nothing is transmitted until an interactive session runs.
        """
        if self._courier is None:
            from .services.courier import CourierService

            self._courier = CourierService(self)
        return self._courier

    @property
    def battery(self) -> BatteryService:
        """Return the battery poller of the session, and create it at first use.

        The property creates it idle. The interactive session starts it with the other
        always-on services. It only reads the pack, as a best-effort action. If there is no
        device, it skips the read without a message. Scripted CLI runs never start the loop.
        """
        if self._battery is None:
            from .services.battery_service import BatteryService

            self._battery = BatteryService(self)
        return self._battery

    @property
    def devstate(self) -> DeviceState:
        """Return the device-state cache of the session, and create it at first use.

        The cache holds the stable facts that screens read from the companion when they open
        (contacts, self-info, path hash mode, channel slots). Thus navigation does not read
        them from the radio each time. That was the main cause of slow changes of screen over
        Bluetooth. The property creates it idle, and each getter reads lazily. A reconnect
        clears the cache (refer to :meth:`reconnect`).
        """
        if self._devstate is None:
            from .services.device_state import DeviceState

            self._devstate = DeviceState(self)
        return self._devstate

    @property
    def basemap_source(self) -> BasemapSource:
        """Return the shared map tile source of the session. MeshTerm builds it once and reuses it.

        The map, the location picker, and the location preview of Node detail all draw the
        same OpenStreetMap basemap. Each of them used to build its own :class:`BasemapSource`.
        Thus each of them paid the one-time TileJSON resolve at each open. The resolve is a
        blocking network round trip, and it occurs when the code first uses
        ``.max_zoom`` or ``.available``. If the session holds one source, the resolve
        happens one time. MeshTerm warms the source behind the menu (refer to
        :func:`meshterm.ui.menu._warm_basemap`), and the tile cache on the disk is shared
        also. The source does not depend on the radio link. Thus a reconnect does not change
        it.
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
        """The logger of the application."""
        return get_logger()

    def adopt_device(self, device: Device) -> None:
        """Adopt a device that is already connected as the device of the session.

        The startup picker uses this method. It opens the chosen companion and confirms it
        during its smoke test. Then it gives that live connection to this method. Thus
        :meth:`device` uses it again and does not open the radio a second time. Many boards
        reset at each serial open, and this makes a reconnect slow and unreliable.

        Args:
            device: A connected :class:`Device` that serves as the radio of this session.
        """
        self._device = device
        # Record the transport and the endpoint that the picker opened directly. This goes
        # around the resolution in ``device()``, which normally sets them. Then the liveness
        # watcher knows what to poll, and ``reconnect`` can build the same connection again.
        self._active_transport = getattr(device, "transport", "serial")
        self._active_port = getattr(device, "_port", None)
        self._active_address = getattr(device, "_address", None)
        self._active_endpoint = (
            getattr(device, "endpoint", None) if (self._active_transport == "tcp") else None
        )

    async def _open(self, what: str) -> None:
        """Open :attr:`_device`, and report a failure to open as a failure of selection.

        The two device exit statuses answer different questions. ``NO_DEVICE`` says that
        MeshTerm transmitted nothing, and that the caller must look at what is plugged in.
        ``DEVICE`` says that MeshTerm reached the radio and the operation failed, so a retry
        is reasonable. Each failure on this path is the first type. The connection does not
        exist yet, so MeshTerm sent nothing over it. The failure used to return as ``DEVICE``
        with the message "the connection to the device was lost". That message describes a
        connection that never existed, and ``--port NOSUCHPORT`` is the most common way to
        reach this line.

        MeshTerm does not keep a device that failed to open. It used to stay as
        :attr:`_device`. Then :attr:`is_connected` said yes, the next :meth:`device` returned
        it unopened, and each tool after a failed start failed with "not connected" and not
        with the reason. Now the method drops the device. Thus the next caller tries the
        connection again and gets the real answer.

        Args:
            what: The thing that MeshTerm opens, for the message (a port, an address, or a
                host).

        Raises:
            DeviceSelectionError: If MeshTerm cannot establish the connection.
        """
        from .core.selection import DeviceSelectionError

        try:
            await self._device.connect()
        except Exception as exc:  # noqa: BLE001 - reclassified, then raised again
            failed, self._device = self._device, None
            try:
                await failed.disconnect()  # the part of it that did open
            except Exception:  # noqa: BLE001 - a best-effort action. It never connected
                pass
            raise DeviceSelectionError(f"could not open {what}: {exc}") from exc

    async def device(self) -> Device:
        """Return a connected :class:`Device`, and open the connection at first use.

        For real hardware, the method resolves which serial port to use (the explicit
        ``--port``, the profile, the remembered default, or the only attached device). After
        a successful connection, it records the device as the new "last known good" default.

        Returns:
            The shared, connected device for this run.

        Raises:
            ValueError: If MeshTerm cannot choose a serial port and the simulator is not on.
            DeviceSelectionError: If the discovery is ambiguous (a subclass of
                ``ValueError``).
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

        # A radio on the SPI bus of the host (``--spi``, an SPI profile, or the remembered
        # default) has nothing to find. MeshTerm starts the node that answers on it.
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

        # MeshTerm opens a network endpoint directly by host:port (an explicit ``--tcp``, a
        # TCP profile, a device picked at startup, or the remembered TCP default). TCP
        # companions cannot be discovered, so this remembered or explicit endpoint is the only
        # way to reach one.
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

        # MeshTerm opens a Bluetooth endpoint directly by address (an explicit ``--ble``, a
        # BLE profile, a device picked at startup, or the remembered BLE default). There is
        # no serial resolution, and no new scan is necessary to reconnect to a known address.
        ble_address, ble_pin = self._resolve_ble_endpoint()
        if ble_address:
            # If a scan produced a live BLEDevice for this address, give it to the
            # connection. Then bleak opens it directly and does not discover the address
            # again. A new internal scan sometimes misses a companion that advertises slowly.
            # A remembered or explicit address has no scan result, and it connects by address
            # only. A handle from the reconnect watch wins over the handle of the startup
            # picker. The watch scanned it seconds ago, after the drop, so it names the
            # peripheral that is on the air now.
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

        from .core.spiradio import without_radio_ports

        resolution = resolve_device(
            # Never the GPS port of the radio itself. On a Cardputer Zero with nothing
            # remembered, it was the only port, and MeshTerm connects to an only port without
            # a question.
            without_radio_ports(discover_devices(), self.settings.profiles),
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

        The method resolves a network endpoint in this order of priority: an explicit
        ``--tcp``, a TCP :class:`~meshterm.core.config.DeviceProfile`, then the remembered TCP
        default. Thus a network companion is accepted in each place where a serial or
        Bluetooth companion is accepted. A malformed endpoint gives ``(None, None)``. Then the
        session continues with the other transports, and does not crash on a typing error.
        """
        from .core.discovery import parse_tcp_endpoint
        from .core.selection import DeviceSelectionError

        # An explicit ``--tcp`` or a TCP profile is authoritative. A malformed value is a
        # typing error of the user. Thus the method shows the parse error as a clean message
        # that the user can act on, and does not silently continue with another transport.
        explicit_endpoint = self.tcp_override or (
            self.profile.tcp_endpoint if self.profile is not None and self.profile.is_tcp else None
        )
        if explicit_endpoint:
            try:
                return parse_tcp_endpoint(explicit_endpoint)
            except ValueError as exc:
                raise DeviceSelectionError(str(exc)) from exc
        # A remembered TCP default applies only when the user selected nothing else
        # explicitly. A corrupt stored value must never crash the startup. In that case,
        # continue with the other transports.
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
            self.selected_device = None  # remembered, not discovered in this session
            return host, port
        return None, None

    def resolve_spi(self) -> SpiWiring | None:
        """The wiring of the SPI radio to open, or ``None`` when the user means another transport.

        The order of priority is: an SPI profile, the radio picked on the startup splash,
        ``--spi`` (the one radio that is attached, refer to :meth:`_spi_for_flag`), then the
        remembered default when it was an SPI radio and the user named nothing else. If the
        user names anything else explicitly (a port, an address, a host, or another type of
        profile), this session is not about the SPI radio.
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
        self.selected_device = None  # remembered, not discovered in this session
        return self.spi_wiring_for(remembered.target)

    def _spi_for_flag(self) -> SpiWiring:
        """The radio that a bare ``--spi`` means: the one that is there, never a guess.

        Nearly all users who have a radio on the SPI bus have exactly one. For them,
        ``--spi`` needs nothing more. The method counts the attached radios first. Thus it
        uses a single radio, if a profile names it or not. The flag is ambiguous only when two
        radios are attached, or (if none is attached to count) when two SPI profiles are
        configured. The flag used to take the profile that ``config.toml`` listed first. That
        is a coin toss that transmits. If no radio is attached, and there is one profile or
        none, the method makes the choice from the configuration. Then the connect attempt
        can say what is missing.

        Raises:
            DeviceSelectionError: When the user can mean more than one radio.
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
            "'Adding a radio on the SPI bus' in docs/guide/configuration.md."
        )

    def spi_wiring_for(self, spidev: str) -> SpiWiring:
        """The wiring for the radio on ``spidev``: of a profile, of a shipped board, or of the AIO.

        MeshTerm knows a remembered or listed SPI radio only by its device file. Thus its pins
        come from the SPI profile that is on that device file. If there is no such profile,
        they come from the board that MeshTerm knows for that device file, when this machine
        is that board (the Cap of the Cardputer Zero). If neither applies, the defaults are
        the only wiring.
        """
        from .core.spiradio import builtin_wiring

        for profile in self.settings.profiles.values():
            if profile.is_spi and (profile.spi or SpiWiring()).spidev == spidev:
                return profile.spi or SpiWiring()
        return builtin_wiring(spidev) or SpiWiring()

    def _resolve_ble_endpoint(self) -> tuple[str | None, str | None]:
        """Return the ``(address, pin)`` to open over Bluetooth, or ``(None, None)`` for serial.

        The method resolves a BLE endpoint in this order of priority: an explicit ``--ble``, a
        BLE :class:`~meshterm.core.config.DeviceProfile`, then the remembered BLE default.
        Thus a Bluetooth companion is accepted in each place where a serial companion is
        accepted. It returns ``(None, None)`` when the session must continue with the serial
        resolution.
        """
        if self.ble_override:
            return self.ble_override, self.ble_pin
        # An explicit serial selection (``--port`` or a serial profile with a port) wins over a
        # remembered BLE default. This is the same as the priority of the serial resolution.
        explicit_serial = bool(self.port_override) or (
            self.profile is not None and not self.profile.is_ble and bool(self.profile.port)
        )
        if self.profile is not None and self.profile.is_ble and self.profile.address:
            return self.profile.address, self.ble_pin or self.profile.ble_pin
        if explicit_serial:
            return None, None
        remembered = self.device_store.load()
        if remembered is not None and remembered.is_ble and remembered.target:
            self.selected_device = None  # remembered, not discovered in this session
            return remembered.target, self.ble_pin
        return None, None

    async def _remember_connected(self) -> None:
        """Record the device that just connected as the last known good default.

        This is a best-effort action. The method learns the mesh node name of the device. A
        failure of the probe must not block a good connection. Only devices that MeshTerm
        discovered in this session have a :class:`~meshterm.core.discovery.DiscoveredDevice`
        to remember. A bare ``--port``, or a reconnect that is remembered by address, has
        nothing new to upsert.
        """
        if self.selected_device is not None:
            self.device_store.remember(
                self.selected_device,
                node_name=await self._node_name(),
                hardware_model=await self._hardware_model(),
            )

    async def _settle_connection(self) -> None:
        """The steps that each new connection gets, in order, when the link is open.

        Remember the device as the default, replay its remembered channels, and give it to
        the clock setter that runs at connection. The clock setter returns at once and does
        its work in the background. Thus the connection is usable when this method returns.
        Each step is a best-effort action on its own. No step can make the connection fail.
        """
        await self._remember_connected()
        await self._reconcile_channels()
        if self._device is not None:
            self.clock_sync.on_connected(self._device)

    async def _reconcile_channels(self) -> None:
        """Replay the remembered channels that the device does not report.

        This is a best-effort action. A radio bridge without firmware loses its channels each
        time that it restarts. Thus MeshTerm restores the channels that you added through
        MeshTerm into free slots at connection (refer to
        :func:`~meshterm.core.channel_store.reconcile`). If MeshTerm remembers nothing for the
        device, this is a single probe of the identity. Thus a radio with firmware pays almost
        nothing. A failure here must never break the connection. MeshTerm logs it and does
        not raise it.
        """
        if self._device is None or self.channel_store is None:
            return
        from .core.channel_store import reconcile

        try:
            restored = await reconcile(self.channel_store, self._device)
        except Exception as exc:  # noqa: BLE001 - the channel replay must not block a connection
            self.log.debug("channels: reconcile on connect failed: %s", exc)
            return
        if restored:
            self.log.info("channels: restored %d remembered channel(s) to the device", restored)

    async def release_link(self) -> None:
        """Take down the dead connection and the services that use it, and keep what to resume.

        This is the first half of :meth:`reconnect`. It is a separate method, because it is
        also useful to do it before the search for the device. Over Bluetooth, this order is
        necessary. A companion advertises only while nothing is connected to it. If MeshTerm
        still holds a link to a peripheral, the peripheral stays off the air. This is true
        also when MeshTerm believes that the link is dead. Then the scan that waits for the
        peripheral to come back can never find it. If MeshTerm releases the link first, the
        peripheral is on the air again. This does no harm on the transports that do not need
        it.

        The method records what was running one time, and keeps the record across retries.
        The method stops the services. If a later attempt read the flags then, they would be
        idle, and it would restore nothing. MeshTerm clears the intent only after
        :meth:`reconnect` succeeds. The method is idempotent. If you call it again when
        nothing is connected, it does nothing.
        """
        if self._resume_intent is None:
            self._resume_intent = (
                self._events is not None and self._events.active,
                self._monitor is not None and self._monitor.active,
                self._chat is not None and self._chat.active,
            )

        # Release the old subscriptions of the hub and the services, and discard the dead
        # device. The subscriptions are in the process (to the hub). Thus they survive the
        # link drop, and MeshTerm must take them down explicitly before it opens a new
        # connection below them.
        if self._monitor is not None:
            await self._monitor.stop()
        if self._chat is not None:
            await self._chat.stop()
        if self._events is not None:
            await self._events.stop()
        if self._device is not None:
            try:
                await self._device.disconnect()
            except Exception:  # noqa: BLE001 - the link is already gone. This is best-effort
                pass
            self._device = None

        # The facts that MeshTerm cached for the connection that just dropped can be old on
        # the new link (a reboot can change the identity or the config). Thus MeshTerm reads
        # them again at the next use.
        if self._devstate is not None:
            self._devstate.reset()

    async def reconnect(self, *, ble_device: object | None = None) -> None:
        """Drop a lost device connection and build it again, and restore the live services.

        Call this method after MeshTerm detects that the companion link is gone (refer to
        :func:`~meshterm.core.connection.is_connection_lost`). The method takes down the dead
        connection and the services that use it. Then it opens a new connection to the same
        device, and starts again what was running before. Thus passive monitoring and chat
        recording resume without a visible break after a replug.

        Args:
            ble_device: The live ``bleak.BLEDevice`` that the caller just scanned for this
                address, if it has one (refer to
                :func:`~meshterm.core.discovery.find_ble_device`). In each case, the method
                drops any handle that it held before. The peripheral that comes back from a
                power cycle is a new peripheral to the OS. Thus the scan result that the
                session opened with is old and also wrong. If MeshTerm used it again, the
                connect would fail on Windows while the device is there and advertises. If you
                pass ``None`` (serial, TCP, or a BLE retry with nothing scanned), the method
                reconnects by address only.

        Raises:
            Exception: If MeshTerm cannot open a new connection (for example, the device is
                still absent). The caller can show the error and offer a retry.
        """
        self._ble_handle = ble_device
        await self.release_link()
        resume_events, resume_monitor, resume_chat = self._resume_intent

        # Open a new connection. If MeshTerm still cannot reach the device, this raises an
        # error and leaves the remembered intent in place for the next attempt. Then start
        # again what was running before the drop, and clear the intent, because the link is
        # back.
        await self.device()
        if resume_events:
            await self.events.start()
        if resume_monitor:
            await self.monitor.start()
        if resume_chat:
            await self.chat.start()
        self._resume_intent = None
        # MeshTerm opened the handle. It now names a link that is live, and not a device to
        # find. Thus the next drop starts with a clean state and scans again.
        self._ble_handle = None

    async def _node_name(self) -> str:
        """Return the mesh node name of the connected device, or ``""`` if it is not available."""
        try:
            info = await self._device.get_self_info()
        except Exception:  # noqa: BLE001 - the identity probe is best-effort and never fatal
            return ""
        return str(info.get("adv_name") or info.get("name") or "")

    async def _hardware_model(self) -> str:
        """Return the firmware model string of the connected device, or ``""``.

        The value comes from the device-query frame, which is the only place that shows the
        model. Thus a reconnect keeps the remembered hardware column current. This is a
        best-effort action. If the probe fails, or the firmware is older, the method leaves
        any model that MeshTerm remembered before as it is.
        """
        try:
            info = await self._device.get_device_info()
        except Exception:  # noqa: BLE001 - the model lookup is best-effort and never fatal
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
            except Exception:  # noqa: BLE001 - a dead or lost link must not crash the teardown
                pass
            self._device = None
        self.repo.close()
