# SPDX-License-Identifier: Apache-2.0
"""The catalog of remote-node settings: what MeshTerm can ask and tell a repeater over the mesh.

MeshTerm administers repeaters and room servers through their text CLI: ``get``/``set``
commands sent as admin messages (refer to
:meth:`~meshterm.core.connection.Device.send_remote_command`). It does not use the binary
companion protocol that the local Device config editor uses. This module is the
data-driven bridge. It has one :class:`RemoteSetting` for each parameter that the
standard MeshCore repeater firmware exposes. Each one has its CLI spelling, a one-line
explanation, and sufficient type data to prompt and validate correctly. The repeater-admin
screen renders the catalog in the lanes of the local editor, stages values, and applies
them. For a setting that the catalog does not have, the raw command line is one key press
away.

We copied the catalog from the firmware itself
(``handleGetCmd``/``handleSetCmd``/``handleCommand`` in ``src/helpers/CommonCLI.cpp`` of
MeshCore), not from convention. Three of its shapes make sense only when you compare them
with that source:

* **Some settings are not ``get``/``set`` keys.** ``powersaving``, ``gps``, and
  ``gps advert`` are top-level verbs. Each one replies when it is sent alone, and takes
  its value as an argument (:attr:`RemoteSetting.verb`).
* **Some settings go together.** There is no ``get bw``. MeshTerm reads frequency,
  bandwidth, spreading factor, and coding rate as one ``get radio`` reply, and writes them
  as one ``set radio f,bw,sf,cr``. (The firmware refuses ``set freq`` alone over the mesh.
  It accepts it only on its serial console.) These four are one
  :attr:`RemoteSetting.composite`. :func:`read_plan`/:func:`write_plan` change the rows of
  a screen into the smallest number of round trips that carry them.
* **One firmware value can have two spellings.** ``dutycycle`` (1.15+) and the older
  ``af`` both read and write ``airtime_factor``. Thus a write to one makes the cached
  value of the other stale (:attr:`RemoteSetting.overlaps`).

But the catalog is not the full page. It is what any MeshCore build can expose, and a node
can have more settings (the parameters of a third-party firmware). This module never
lists these settings and never probes for them. MeshTerm learns them one node at a time,
from commands that the user ran (:func:`parse_setting_command`,
:func:`discovered_setting`). For this reason, :attr:`RemoteSetting.discovered` exists.
Also for this reason, :func:`learnable_from_get` must know that ``get`` matches keys by
prefix and ``set`` does not.

The parser is not strict, on purpose. Repeater firmware replies with few words, and it
changed its phrasing between versions (``"> 20"``, ``"tx: 20"``,
``"OK - repeat is now ON"``). Thus the parser extracts the values, and does not match a
full pattern. Firmware that does not have a key replies with an error string (``??: key``
to a ``get``, ``unknown config: …`` to a ``set``). Or, because ``get`` matches keys by
prefix, it replies with the value of a shorter related key (v1.15 replies to
``get radio.fem.rxgain`` with the ``get radio`` line). The screen stores both replies as a
sign that this node does not have the setting, and it does not break.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from .models import is_room

#: Reply text (in lower case) that shows that the firmware refused a command or does not
#: know it. ``handleGetCmd`` replies ``??:`` to a key that it does not know.
#: ``unknown config:`` is the same reply for ``set``. ``err - `` is the refusal of the
#: ``region`` verbs (``Err - not empty``).
_ERRORISH = (
    "??:",
    "unknown",
    "error",
    "err:",
    "err - ",
    "invalid",
    "denied",
    "bad ",
    "not supported",
    "unsupported",
)

#: The value echo of a ``get`` reply. All text after it is a value, also when the value
#: contains a word that looks like an error (a node named ``Unknown-Hill`` is not a
#: refusal).
_ECHO = re.compile(r"^\s*-?>\s?")

#: The words that a boolean reply can use for each state, in addition to the
#: :attr:`RemoteSetting.words` of the setting.
_ON_WORDS = frozenset({"on", "true", "yes", "1"})
_OFF_WORDS = frozenset({"off", "false", "no", "0"})


@dataclass(frozen=True, slots=True)
class Option:
    """One value of an enumerated remote setting.

    Attributes:
        value: The value that the firmware takes in a ``set`` and gives in reply to a
            ``get``. The cache also stores this value.
        label: The text that the value lane and the picker show for the value.
        help: An optional one-line explanation for the picker row.
    """

    value: str
    label: str
    help: str = ""


@dataclass(frozen=True, slots=True)
class RemoteSetting:
    """One remote-CLI setting: its spelling, its presentation, and its type.

    Attributes:
        key: The identity of the setting. For a ``get``/``set`` key, it is the CLI
            parameter name as the firmware spells it (``"txdelay"``). For a verb-shaped
            setting, it is the verb. For a composite member, it is the name of the field.
            It is also the cache key.
        label: The display name that the editor lane shows.
        help: A one-line explanation (with no period at the end), shown next to the
            value.
        category: The editor section heading under which the setting sorts.
        kind: ``"int"``, ``"float"``, ``"str"``, ``"bool"`` (on/off) or ``"enum"``.
        writable: Whether the firmware accepts a write for this setting.
        readable: Whether the firmware replies to a read for it.
        minimum: The lower limit for numeric prompts, when it is known.
        maximum: The upper limit for numeric prompts, when it is known.
        unit: The display unit (for example ``"dBm"``, ``"min"``). It changes only the
            appearance.
        decimals: The fixed number of decimal places with which a float is shown and
            sent. ``None`` shows it as the firmware gave it, with the trailing zeros
            removed.
        zero_off: Whether ``0`` is accepted outside the range from ``minimum`` to
            ``maximum``, with the meaning off (the advert intervals: 0, or 60–240 minutes).
        options: The values that an ``enum`` takes, in picker order.
        words: The words that a ``bool`` sends for on and off. Most keys take
            ``on``/``off``. ``multi.acks`` is an ``atoi``, and it must have ``1``/``0``.
        aliases: ``(reply word, value)`` pairs that the parser examines before the parse
            of the kind, for a reply that names a value with its own words. For example,
            ``bridge.source`` replies ``logRx`` for the value that it got as ``rx``.
        verb: For a verb-shaped setting, the command that reads it when sent alone, and
            writes it with the value added at the end (``"powersaving"``). Empty for a
            ``get``/``set`` key.
        composite: The CLI key through which MeshTerm reads and writes a group of
            settings as one comma-joined value (``"radio"``). Empty for a setting that is
            alone.
        part: The position of this setting in the comma-joined value of its composite.
        overlaps: Keys that are a different spelling of the same firmware value. A write
            to this setting makes their cached values stale.
        discovered: Whether this setting is not in the catalog, but MeshTerm learned it
            from a command that the user ran on the command line of one node (refer to
            :func:`discovered_setting`). Never true for a catalog entry.
        as_room: ``(label, help)`` for a setting that has a different meaning on a room
            server. The firmwares share their command line, but not the purpose of each
            word. Empty for a setting that has the same meaning on both. Refer to
            :meth:`for_node`.
    """

    key: str
    label: str
    help: str
    category: str
    kind: str = "str"
    writable: bool = True
    readable: bool = True
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    decimals: int | None = None
    zero_off: bool = False
    options: tuple[Option, ...] = ()
    words: tuple[str, str] = ("on", "off")
    aliases: tuple[tuple[str, str], ...] = ()
    verb: str = ""
    composite: str = ""
    part: int = 0
    overlaps: tuple[str, ...] = ()
    discovered: bool = False
    as_room: tuple[str, str] = ()

    def for_node(self, node_type: int | None) -> RemoteSetting:
        """This setting as a node of ``node_type`` shows it: with the words of a room server.

        The ``guest.password`` of a repeater lets read-only guests in. On a room server, it
        is the password with which its members join and post. The key and the value are
        the same, but the meaning for the user is different. Thus the page names the
        setting for the node on which it is open, and the catalog stays one list.

        Args:
            node_type: The advert type of the administered node.

        Returns:
            The setting with its room label and help, if it has them and the node is a
            room server. Otherwise, this same setting.
        """
        if self.as_room and is_room(node_type):
            label, help_text = self.as_room
            return replace(self, label=label, help=help_text)
        return self

    @property
    def get_command(self) -> str:
        """The CLI command that reads this setting (the same for all members of a composite)."""
        if self.verb:
            return self.verb
        return f"get {self.composite or self.key}"

    def set_command(self, value: str) -> str:
        """The CLI command that writes ``value`` to this setting.

        Raises:
            ValueError: For a composite member, which MeshTerm writes only together with
                the other members (refer to :func:`write_plan`).
        """
        if self.composite:
            raise ValueError(f"{self.key} is written through 'set {self.composite}'")
        if self.kind == "bool":
            value = self.words[0] if value == "on" else self.words[1]
        if self.verb:
            return f"{self.verb} {value}"
        return f"set {self.key} {value}"

    def display(self, value: str) -> str:
        """The text of the value lane for a stored ``value`` (an enum shows its label)."""
        for option in self.options:
            if option.value == value:
                return option.label
        return value


def _radio(key: str, label: str, help_text: str, part: int, **typing) -> RemoteSetting:
    """One of the four fields that ``get radio`` gives and ``set radio`` takes together."""
    return RemoteSetting(
        key=key,
        label=label,
        help=help_text,
        category="Radio",
        composite="radio",
        part=part,
        **typing,
    )


#: All the settings that the standard MeshCore repeater and room firmware exposes over the
#: mesh, in the display order of the editor. The catalog lists the settings that depend on
#: the board or the build (the gains of the front-end module, GPS, the bridge) as it lists
#: all others. A node built without them replies ``??:``, and the row shows this. Thus the
#: catalog does not have to guess which board it talks to. These are not in the list, on
#: purpose: ``prv.key`` and ``freq`` writes (serial console only), ``extra.sf`` (only
#: LR2021 builds, with a format that the board defines), and the read-only diagnostics
#: (``public.key``, ``role``, ``bootloader.ver``, ``pwrmgt.*``). The diagnostics are facts,
#: not settings, and each one costs a paced round trip at each read. The command line can
#: get to all of them.
REPEATER_SETTINGS: tuple[RemoteSetting, ...] = (
    # -- Identity ---------------------------------------------------------------
    RemoteSetting(
        key="name",
        label="Name",
        category="Identity",
        help="The node's advertised name",
    ),
    RemoteSetting(
        key="owner.info",
        label="Owner info",
        category="Identity",
        help="Owner contact details (| starts a new line)",
    ),
    RemoteSetting(
        key="lat",
        label="Latitude",
        category="Identity",
        kind="float",
        minimum=-90,
        maximum=90,
        help="Advertised position, decimal degrees",
    ),
    RemoteSetting(
        key="lon",
        label="Longitude",
        category="Identity",
        kind="float",
        minimum=-180,
        maximum=180,
        help="Advertised position, decimal degrees",
    ),
    # -- Radio ------------------------------------------------------------------
    _radio(
        "freq",
        "Frequency",
        "Carrier frequency — every node on the mesh must match; reboot to apply",
        0,
        kind="float",
        unit="MHz",
        minimum=150,
        maximum=2500,
    ),
    _radio(
        "bw",
        "Bandwidth",
        "Channel bandwidth — must match the mesh; reboot to apply",
        1,
        kind="float",
        unit="kHz",
        minimum=7,
        maximum=500,
    ),
    _radio(
        "sf",
        "Spreading factor",
        "LoRa spreading factor — must match the mesh; reboot to apply",
        2,
        kind="int",
        minimum=5,
        maximum=12,
    ),
    _radio(
        "cr",
        "Coding rate",
        "LoRa coding rate denominator — must match the mesh; reboot to apply",
        3,
        kind="int",
        minimum=5,
        maximum=8,
    ),
    RemoteSetting(
        key="tx",
        label="TX power",
        category="Radio",
        kind="int",
        unit="dBm",
        minimum=1,
        maximum=30,
        help="Transmit power — the TX optimize tool tunes this by measurement",
    ),
    RemoteSetting(
        key="radio.rxgain",
        label="Boosted RX gain",
        category="Radio",
        kind="bool",
        help="The receiver's boosted gain mode (firmware 1.14.1+)",
    ),
    RemoteSetting(
        key="radio.fem.rxgain",
        label="FEM RX gain",
        category="Radio",
        kind="bool",
        help="The front-end module's receive gain (boards with one)",
    ),
    RemoteSetting(
        key="radio.fem.txgain",
        label="FEM TX gain",
        category="Radio",
        kind="bool",
        help="The front-end module's transmit gain (boards with one)",
    ),
    RemoteSetting(
        key="cad",
        label="Activity detection",
        category="Radio",
        kind="bool",
        help="Listen for LoRa activity in hardware before transmitting",
    ),
    RemoteSetting(
        key="int.thresh",
        label="Interference threshold",
        category="Radio",
        kind="int",
        help="Local interference threshold (0 = off)",
    ),
    RemoteSetting(
        key="agc.reset.interval",
        label="AGC reset interval",
        category="Radio",
        kind="int",
        unit="s",
        minimum=0,
        help="Seconds between receiver gain resets, in steps of 4 (0 = off)",
    ),
    # -- Repeating ----------------------------------------------------------------
    RemoteSetting(
        key="repeat",
        label="Repeat",
        category="Repeating",
        kind="bool",
        help="Whether the node rebroadcasts mesh traffic at all",
    ),
    RemoteSetting(
        key="txdelay",
        label="TX delay",
        category="Repeating",
        kind="float",
        decimals=1,
        minimum=0,
        maximum=2,
        help="Delay factor before rebroadcasting a flood packet",
    ),
    RemoteSetting(
        key="direct.txdelay",
        label="Direct TX delay",
        category="Repeating",
        kind="float",
        decimals=1,
        minimum=0,
        maximum=2,
        help="Delay factor before forwarding a directed packet",
    ),
    RemoteSetting(
        key="rxdelay",
        label="RX delay",
        category="Repeating",
        kind="float",
        decimals=1,
        minimum=0,
        maximum=20,
        help="Base delay before acting on a received packet",
    ),
    RemoteSetting(
        key="dutycycle",
        label="Duty cycle",
        category="Repeating",
        kind="float",
        decimals=1,
        unit="%",
        minimum=1,
        maximum=100,
        overlaps=("af",),
        help="Share of the air the node may transmit in (firmware 1.15+)",
    ),
    RemoteSetting(
        key="af",
        label="Airtime factor",
        category="Repeating",
        kind="float",
        decimals=2,
        minimum=0,
        maximum=9,
        overlaps=("dutycycle",),
        help="Older firmware's duty-cycle knob: duty cycle = 100 / (af + 1) %",
    ),
    RemoteSetting(
        key="loop.detect",
        label="Loop detection",
        category="Repeating",
        kind="enum",
        options=(
            Option("off", "off"),
            Option("minimal", "minimal"),
            Option("moderate", "moderate"),
            Option("strict", "strict"),
        ),
        help="How readily the node drops a packet that loops back through it",
    ),
    RemoteSetting(
        key="path.hash.mode",
        label="Path-hash mode",
        category="Repeating",
        kind="enum",
        # The firmware refuses 3 here (``mode < 3``), but the field has space for it. The
        # labels are short, because the widest value sets the width of the lane for all
        # rows. When each label had "hashes", the descriptions went off a 72-cell screen
        # after one value was staged.
        options=(
            Option("0", "1-byte", "The default"),
            Option("1", "2-byte"),
            Option("2", "3-byte"),
        ),
        help="Longer hop ids mix up fewer nodes, but cost space",
    ),
    RemoteSetting(
        key="multi.acks",
        label="Multi-acks",
        category="Repeating",
        kind="bool",
        words=("1", "0"),
        help="Send extra receipts so replies get through",
    ),
    # -- Flooding -----------------------------------------------------------------
    RemoteSetting(
        key="flood.max",
        label="Flood max",
        category="Flooding",
        kind="int",
        unit="hops",
        minimum=0,
        maximum=64,
        help="A flood that already took this many hops isn't rebroadcast",
    ),
    RemoteSetting(
        key="flood.max.unscoped",
        label="Flood max (unscoped)",
        category="Flooding",
        kind="int",
        unit="hops",
        minimum=0,
        maximum=64,
        help="The same cap, for floods sent without a region scope",
    ),
    RemoteSetting(
        key="flood.max.advert",
        label="Flood max (adverts)",
        category="Flooding",
        kind="int",
        unit="hops",
        minimum=0,
        maximum=64,
        help="The same cap, for flooded adverts",
    ),
    # -- Adverts -----------------------------------------------------------------
    RemoteSetting(
        key="advert.interval",
        label="Advert interval",
        category="Adverts",
        kind="int",
        unit="min",
        minimum=60,
        maximum=240,
        zero_off=True,
        help="Minutes between the node's own zero-hop adverts, in steps of 2 (0 = off)",
    ),
    RemoteSetting(
        key="flood.advert.interval",
        label="Flood advert interval",
        category="Adverts",
        kind="int",
        unit="h",
        minimum=3,
        maximum=168,
        zero_off=True,
        help="Hours between the node's own flood adverts (0 = off)",
    ),
    # -- Access ------------------------------------------------------------------
    RemoteSetting(
        key="guest.password",
        label="Guest password",
        category="Access",
        help="Password for read-only (guest) logins",
        as_room=("Room password", "The password members join and post with"),
    ),
    RemoteSetting(
        key="allow.read.only",
        label="Allow read-only",
        category="Access",
        kind="bool",
        help="Allow read-only access (room servers)",
        as_room=("Allow read-only", "Let any password in to read, but not to post"),
    ),
    # -- Power -------------------------------------------------------------------
    RemoteSetting(
        key="powersaving",
        label="Power saving",
        category="Power",
        kind="bool",
        verb="powersaving",
        help="Sleep between transmissions to save battery",
    ),
    RemoteSetting(
        key="adc.multiplier",
        label="ADC multiplier",
        category="Power",
        kind="float",
        decimals=3,
        minimum=0,
        maximum=10,
        help="Battery voltage calibration (0 = the board's default)",
    ),
    # -- GPS ---------------------------------------------------------------------
    RemoteSetting(
        key="gps",
        label="GPS",
        category="GPS",
        kind="bool",
        verb="gps",
        # A board with a receiver replies "on, active|deactivated, fix, N sats", also when
        # the GPS is not switched on. The second word tells the state.
        aliases=(("active", "on"), ("deactivated", "off")),
        help="Run the node's GPS receiver (boards with one)",
    ),
    RemoteSetting(
        key="gps advert",
        label="Advertised position",
        category="GPS",
        kind="enum",
        verb="gps advert",
        options=(
            Option("none", "none", "Advertise no position"),
            Option("share", "share", "Advertise the live GPS fix"),
            Option("prefs", "prefs", "Advertise the latitude and longitude set above"),
        ),
        help="Which position the node's adverts carry",
    ),
    # -- Bridge ------------------------------------------------------------------
    RemoteSetting(
        key="bridge.type",
        label="Bridge type",
        category="Bridge",
        writable=False,
        help="The bridge this firmware was built with: none, rs232, or espnow",
    ),
    RemoteSetting(
        key="bridge.enabled",
        label="Bridge enabled",
        category="Bridge",
        kind="bool",
        help="Relay packets over the bridge",
    ),
    RemoteSetting(
        key="bridge.source",
        label="Bridge source",
        category="Bridge",
        kind="enum",
        options=(
            Option("rx", "received", "Bridge the packets the node receives"),
            Option("tx", "transmitted", "Bridge the packets the node transmits"),
        ),
        aliases=(("logrx", "rx"), ("logtx", "tx")),
        help="Which packets cross the bridge",
    ),
    RemoteSetting(
        key="bridge.delay",
        label="Bridge delay",
        category="Bridge",
        kind="int",
        unit="ms",
        minimum=0,
        maximum=10000,
        help="Delay before a packet crosses the bridge",
    ),
    RemoteSetting(
        key="bridge.baud",
        label="Bridge baud rate",
        category="Bridge",
        kind="enum",
        options=tuple(
            Option(rate, f"{rate} baud") for rate in ("9600", "19200", "38400", "57600", "115200")
        ),
        help="Serial speed (RS-232 bridges)",
    ),
    RemoteSetting(
        key="bridge.channel",
        label="Bridge channel",
        category="Bridge",
        kind="int",
        minimum=1,
        maximum=14,
        help="Wi-Fi channel (ESP-NOW bridges)",
    ),
    RemoteSetting(
        key="bridge.secret",
        label="Bridge secret",
        category="Bridge",
        help="Shared secret, up to 15 characters (ESP-NOW bridges)",
    ),
)


#: Commands that the remote CLI screen offers as completions, in addition to the read and
#: write spellings of the catalog: the fixed verbs to which a MeshCore repeater replies over
#: the mesh. The verbs that its firmware keeps for the serial console (``erase``, ``log``
#: dumps, ``stats-*``) are not in the list, because a completion of them only gets a
#: refusal.
KNOWN_COMMANDS: tuple[str, ...] = (
    "advert",
    "advert.zerohop",
    "board",
    "clear stats",
    "clkreboot",
    "clock",
    "clock sync",
    "discover.neighbors",
    "get ",
    "get public.key",
    "get role",
    "gps setloc",
    "gps sync",
    "log erase",
    "log start",
    "log stop",
    "neighbor.remove ",
    "neighbors",
    "password ",
    "poweroff",
    "reboot",
    # ``region load`` is not in the list, on purpose. It switches the CLI into a mode that
    # reads one line at a time until a blank line, and a remote command line cannot send
    # a blank line.
    "region",
    "region allowf ",
    "region def ",
    "region default",
    "region denyf ",
    "region get ",
    "region home",
    "region list allowed",
    "region list denied",
    "region put ",
    "region remove ",
    "region save",
    "sensor get ",
    "sensor list",
    "sensor set ",
    "set ",
    "setperm ",
    "start ota",
    "tempradio ",
    "time ",
    "ver",
)


def known_commands() -> list[str]:
    """All completions that the remote CLI offers: verbs, and the catalog read/write spellings."""
    commands = set(KNOWN_COMMANDS)
    for spec in REPEATER_SETTINGS:
        if spec.readable:
            commands.add(spec.get_command)
        if spec.writable:
            if spec.composite:
                commands.add(f"set {spec.composite} ")
            elif spec.verb:
                commands.add(f"{spec.verb} ")
            else:
                commands.add(f"set {spec.key} ")
    return sorted(commands)


def get_setting(key: str) -> RemoteSetting | None:
    """Find a catalog setting by its key."""
    return next((s for s in REPEATER_SETTINGS if s.key == key), None)


def settings_by_category() -> list[tuple[str, list[RemoteSetting]]]:
    """The catalog in groups by category, in declaration order (the layout of the editor)."""
    grouped: dict[str, list[RemoteSetting]] = {}
    for spec in REPEATER_SETTINGS:
        grouped.setdefault(spec.category, []).append(spec)
    return list(grouped.items())


def composite_members(composite: str) -> list[RemoteSetting]:
    """The settings that a composite key carries, in their comma-joined order."""
    return sorted((s for s in REPEATER_SETTINGS if s.composite == composite), key=lambda s: s.part)


#: The category under which a discovered setting sorts, and the heading of its section.
DISCOVERED_CATEGORY = "Extra"

#: The text of the description lane for a discovered setting. The catalog has no
#: explanation to give. It knows only that this node replied to the key. Thus the row says
#: exactly that, and in few words, because the lane also holds descriptions that were
#: written to fit a 72-cell screen.
DISCOVERED_HELP = "Found on this node"

#: Keys whose value MeshTerm never writes to disk, in any way that it was asked for.
#: ``prv.key`` is the identity of the node itself. The firmware replies to it only on the
#: serial console (``sender_timestamp == 0`` in ``handleGetCmd``), and it is a fact, not a
#: setting. If a reply with it gets to MeshTerm, MeshTerm must not be the first program
#: that writes the private key of a repeater to a file. The command line still runs the
#: command and still prints the answer. Only the storage stops here.
NEVER_STORED = frozenset({"prv.key"})


@dataclass(frozen=True, slots=True)
class SettingCommand:
    """A remote-CLI command that MeshTerm recognizes as a read or a write of one setting.

    Attributes:
        verb: ``"get"`` or ``"set"``.
        key: The key of the setting, as the user spelled it.
        value: The value that a ``set`` writes. ``""`` for a ``get``.
    """

    verb: str
    key: str
    value: str = ""


def parse_setting_command(command: str) -> SettingCommand | None:
    """Read one typed CLI command as a setting read or write, or ``None`` if it is neither.

    The grammar is the filter, on purpose. ``get <key>`` and ``set <key> <value>`` are how
    the firmware itself says that this is a setting. Thus no list of commands to exclude is
    necessary. Each top-level verb (``reboot``, ``advert``, ``clock sync``, ``start ota``,
    ``powersaving``, ``setperm``) does not have this shape, and the parser ignores it. A
    key must have the spelling that the keys of the firmware use. Thus a line with a typing
    error cannot make a new key.
    """
    parts = command.strip().split()
    if len(parts) < 2 or parts[0] not in ("get", "set"):
        return None
    key = parts[1]
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9._]*", key):
        return None
    if parts[0] == "get":
        return SettingCommand("get", key) if len(parts) == 2 else None
    value = command.strip()[len("set") :].strip()[len(key) :].strip()
    return SettingCommand("set", key, value) if value else None


def get_match_keys() -> list[str]:
    """All the spellings that a firmware ``get`` matches, the longest first.

    The members of a composite share the key of their composite (``get radio``).
    Verb-shaped settings are not ``get`` keys.
    """
    keys = {s.composite or s.key for s in REPEATER_SETTINGS if s.readable and not s.verb}
    return sorted(keys, key=len, reverse=True)


def shadowing_key(key: str) -> str | None:
    """The catalog ``get`` key that replies to a ``get key`` on firmware without ``key``.

    ``handleGetCmd`` compares with ``memcmp(config, "radio", 5)``, with no trailing space.
    Thus a key that it does not know goes into the branch of a shorter key that is a prefix
    of it, and the firmware replies with the value of that shorter key. On stock firmware,
    ``get radio.rxps`` returns ``> 910.525,62.5,7,5``. This is not an error, and it is not
    a value of the key in the request. ``handleSetCmd`` matches ``"radio "`` with the
    space, so a write has no such shadow.
    """
    return next((k for k in get_match_keys() if key != k and key.startswith(k)), None)


def learnable_from_get(key: str, reply: str, known: Mapping[str, str]) -> bool:
    """Whether a ``get key`` reply came from ``key``, and not from a shorter related key.

    MeshTerm asks this only for a key that the catalog does not have. For such a key, there
    is no type that can reject an answer with the wrong shape. The function trusts the
    reply when no key shadows the key (:func:`shadowing_key`), or when the reply cannot be
    the answer of the shadow. That is, the reply does not parse as the type of the shadow,
    or it parses to a value that is different from the last value of the shadow. If the
    value of the shadow was never read, the function cannot answer the question, and a
    question with no answer is not a discovery.

    Args:
        key: The key that the user asked for.
        reply: The reply of the node.
        known: The last values that MeshTerm read from the node, with the same keys as the
            cache.
    """
    shadow = shadowing_key(key)
    if shadow is None:
        return True
    members = composite_members(shadow)
    if not members:
        spec = get_setting(shadow)
        members = [spec] if spec is not None else []
    if not members:  # pragma: no cover - every get key is a catalog key
        return True
    parsed = {m.key: parse_reply_value(m, reply) for m in members}
    if any(value is None for value in parsed.values()):
        return True  # the reply does not have the shape of the answer of the shadow
    if any(known.get(k) is None for k in parsed):
        return False  # it can be the answer of the shadow, and there is nothing to compare
    return any(known[k] != value for k, value in parsed.items())


def discovered_setting(key: str) -> RemoteSetting:
    """A catalog entry for a key that the catalog does not have, but one node said it has.

    All the code after the catalog (the read plan, the reply parser, the lanes of the
    editor, the stage, :func:`write_plan`) uses :class:`RemoteSetting`. Thus a discovered
    key becomes a :class:`RemoteSetting`, and does not get a second path next to the first.
    It has only the data that MeshTerm learned: the key as the node spells it, and a
    string value. It has no range, no words, and no options, because the firmware did not
    give them. If MeshTerm guessed them, a row could show false data about a node whose
    build we have never examined.
    """
    return RemoteSetting(
        key=key,
        label=key,
        help=DISCOVERED_HELP,
        category=DISCOVERED_CATEGORY,
        kind="str",
        discovered=True,
    )


def reply_is_error(reply: str) -> bool:
    """Whether a reply text shows that the firmware refused the command.

    A reply that starts with the ``>`` value echo of a ``get`` is a value, whatever it
    says.
    """
    if _ECHO.match(reply):
        return False
    lowered = reply.lower()
    return any(marker in lowered for marker in _ERRORISH)


def parse_reply_value(spec: RemoteSetting, reply: str | None) -> str | None:
    """Extract a stored value from the reply to a read, or ``None`` if the reply is not usable.

    The function extracts numbers from the phrasing that the firmware used, and formats
    them as :func:`normalize_value` formats a typed number. Thus a value that MeshTerm
    reads back is equal to the same value when it is staged. A composite member takes its
    own field of the comma-joined reply. A reply that looks like an error parses as
    ``None``. Thus a firmware without the key never makes the error string a value.

    Args:
        spec: The setting of the reply.
        reply: The raw reply text, or ``None`` if the node did not reply.

    Returns:
        The value as stored text (the :attr:`Option.value` of an enum, ``on``/``off`` for
        a bool), ``""`` for a string that the node reports as empty, or ``None``.
    """
    if reply is None or reply_is_error(reply):
        return None
    echoed = bool(_ECHO.match(reply))
    text = _ECHO.sub("", reply.strip(), count=1).strip()
    # Some firmware versions echo the key instead of ">": "tx: 20", "name = Yagi".
    text = re.sub(rf"^{re.escape(spec.key)}\s*[:=]\s*", "", text, flags=re.I)
    if spec.composite:
        pieces = text.split(",")
        if len(pieces) != len(composite_members(spec.composite)):
            return None
        text = pieces[spec.part].strip()
    tokens = re.findall(r"[\w.+-]+", text.lower())
    for word, value in spec.aliases:
        if word in tokens:
            return value
    if spec.kind in ("int", "float"):
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        return None if match is None else _format_number(spec, float(match.group()))
    if spec.kind == "bool":
        on_words = _ON_WORDS | {spec.words[0]}
        off_words = _OFF_WORDS | {spec.words[1]}
        for token in tokens:
            if token in on_words:
                return "on"
            if token in off_words:
                return "off"
        return None
    if spec.kind == "enum":
        by_value = {option.value.lower(): option.value for option in spec.options}
        return next((by_value[t] for t in tokens if t in by_value), None)
    if echoed:
        return text  # "> " with no text after it is an empty string, which is a value
    return text or None


def _format_number(spec: RemoteSetting, value: float) -> str:
    """One number as the cache stores it: whole, with fixed decimals, or trimmed."""
    if spec.kind == "int":
        return str(int(round(value)))
    if spec.decimals is not None:
        text = f"{value:.{spec.decimals}f}"
    else:
        # The ftoa of the firmware: a maximum of 7 places, with the trailing zeros removed.
        text = f"{value:.7f}".rstrip("0").rstrip(".")
    return text.lstrip("-") if float(text) == 0 else text


def normalize_value(spec: RemoteSetting, raw: str) -> str:
    """A validated typed value in the form that the cache stores and the firmware gets.

    If the user enters TX delay as ``0.33``, it becomes ``0.3``. The value is rounded to
    the decimals of the setting, and spelled exactly as a read of the same value spells
    it. Thus, if the user stages the value that the node holds already, the stage is
    removed, and MeshTerm does not queue a ``set`` that does nothing.
    """
    text = raw.strip()
    if spec.kind in ("int", "float"):
        return _format_number(spec, float(text))
    if spec.kind == "bool":
        return text.lower()
    if spec.kind == "enum":
        return next((o.value for o in spec.options if o.value.lower() == text.lower()), text)
    return text


def validate_value(spec: RemoteSetting, raw: str) -> bool | str:
    """Validate a prompted value for ``spec``: ``True``, or the error message to show."""
    text = raw.strip()
    if not text:
        return "Enter a value."
    if spec.kind == "bool":
        if text.lower() in ("on", "off"):
            return True
        return "Enter on or off."
    if spec.kind == "enum":
        if any(o.value.lower() == text.lower() for o in spec.options):
            return True
        return "Enter one of " + ", ".join(o.value for o in spec.options) + "."
    if spec.kind in ("int", "float"):
        try:
            value = float(text)
        except ValueError:
            return "Enter a number."
        if spec.kind == "int" and not value.is_integer():
            return "Enter a whole number."
        if spec.zero_off and value == 0:
            return True
        low, high = spec.minimum, spec.maximum
        if spec.zero_off and low is not None and high is not None and not low <= value <= high:
            return f"Must be 0, or {low:g} – {high:g}."
        if low is not None and value < low:
            return f"Must be ≥ {low:g}."
        if high is not None and value > high:
            return f"Must be ≤ {high:g}."
    return True


def range_hint(spec: RemoteSetting) -> str:
    """The help line of the prompt: what the validation accepts, and in what unit."""
    low, high = spec.minimum, spec.maximum
    if low is not None and high is not None:
        hint = f"Allowed: {low:g} – {high:g}"
    elif low is not None:
        hint = f"Allowed: ≥ {low:g}"
    elif high is not None:
        hint = f"Allowed: ≤ {high:g}"
    else:
        hint = ""
    if hint and spec.zero_off:
        hint = hint.replace("Allowed: ", "Allowed: 0 (off), or ", 1)
    if spec.unit:
        hint = f"{hint}  ({spec.unit})" if hint else f"In {spec.unit}."
    return hint


def read_plan(specs: Iterable[RemoteSetting]) -> list[tuple[str, list[RemoteSetting]]]:
    """The read commands that answer ``specs``, each one time, with the settings that it fills.

    Each command is one paced round trip. Thus the four radio fields send ``get radio``
    only one time for all four. A request for one of them fills all four, because the
    reply carries them all, and a radio line with only half of its values updated does not
    agree with itself. The function skips the settings that cannot be read.
    """
    plan: dict[str, list[RemoteSetting]] = {}
    for spec in specs:
        if not spec.readable:
            continue
        fills = plan.setdefault(spec.get_command, [])
        for member in composite_members(spec.composite) if spec.composite else [spec]:
            if member not in fills:
                fills.append(member)
    return list(plan.items())


@dataclass(frozen=True, slots=True)
class Write:
    """One write command, planned from the staged values.

    Attributes:
        command: The CLI command to send, or ``""`` when it cannot be built yet.
        values: Setting key -> the value that the node holds after this command. For a
            composite, this includes the members that are not staged, which the command
            sends again.
        missing: The composite members that are not staged and not known. They are the
            reason that ``command`` is empty. Read them first (:func:`composite_reads`).
    """

    command: str
    values: dict[str, str]
    missing: tuple[str, ...] = ()


def write_plan(pending: Mapping[str, str], known: Mapping[str, str]) -> list[Write]:
    """Put staged values into groups, as the commands that send them, in catalog order.

    A setting that is alone has its own ``set``. MeshTerm sends a composite member with
    all the other members in one command. For the members that are not staged, it sends
    again the last values from the node, because ``set radio`` cannot change one field
    alone.

    Args:
        pending: Setting key -> staged value.
        known: Setting key -> the last value read from the node, for the other members of
            a composite.

    Returns:
        The planned writes.
    """
    writes: list[Write] = []
    planned: set[str] = set()
    for spec in REPEATER_SETTINGS:
        if spec.key not in pending or not spec.writable:
            continue
        if not spec.composite:
            value = pending[spec.key]
            writes.append(Write(spec.set_command(value), {spec.key: value}))
            continue
        if spec.composite in planned:
            continue
        planned.add(spec.composite)
        members = composite_members(spec.composite)
        values = {m.key: pending.get(m.key, known.get(m.key)) for m in members}
        missing = tuple(key for key, value in values.items() if value is None)
        if missing:
            staged = {m.key: pending[m.key] for m in members if m.key in pending}
            writes.append(Write("", staged, missing))
        else:
            joined = ",".join(str(values[m.key]) for m in members)
            writes.append(Write(f"set {spec.composite} {joined}", dict(values)))  # type: ignore[arg-type]
    # A staged key that the catalog does not have is a discovered key (no other key can
    # stage a row). MeshTerm writes it in the only way that it knows: its own plain `set`.
    for key, value in pending.items():
        if get_setting(key) is None:
            writes.append(Write(f"set {key} {value}", {key: value}))
    return writes


def composite_reads(pending: Mapping[str, str], known: Mapping[str, str]) -> list[str]:
    """The reads that a write must do first: staged composites with members that are not known."""
    commands: list[str] = []
    for write in write_plan(pending, known):
        if write.missing:
            spec = get_setting(write.missing[0])
            if spec is not None and spec.get_command not in commands:
                commands.append(spec.get_command)
    return commands
