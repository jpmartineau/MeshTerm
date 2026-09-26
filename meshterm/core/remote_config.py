# SPDX-License-Identifier: Apache-2.0
"""The remote-node settings catalog: what a repeater can be asked and told over the mesh.

Repeaters and room servers are administered through their text CLI — ``get``/``set``
commands sent as admin messages (see
:meth:`~meshterm.core.connection.Device.send_remote_command`) — not through the binary
companion protocol the local device-configuration editor drives. This module is the
data-driven bridge: one :class:`RemoteSetting` per knob the standard MeshCore repeater
firmware exposes, each carrying its CLI spelling, a one-line explanation, and enough
typing to prompt and validate sensibly. The repeater-admin screen renders the catalog in
the local editor's lanes, stages values, and applies them; anything the catalog doesn't
cover is one keystroke away in the raw command line.

The catalog is transcribed from the firmware itself — ``handleGetCmd``/``handleSetCmd``/
``handleCommand`` in MeshCore's ``src/helpers/CommonCLI.cpp`` — not from convention, and
three of its shapes only make sense against that source:

* **Some settings are not ``get``/``set`` keys at all.** ``powersaving``, ``gps`` and
  ``gps advert`` are top-level verbs that answer bare and take their value as an argument
  (:attr:`RemoteSetting.verb`).
* **Some settings travel together.** There is no ``get bw``: frequency, bandwidth,
  spreading factor and coding rate are read as one ``get radio`` reply and written as one
  ``set radio f,bw,sf,cr`` (``set freq`` alone is refused over the mesh — the firmware
  honours it only on its serial console). Those four are one
  :attr:`RemoteSetting.composite`, and :func:`read_plan`/:func:`write_plan` turn a screen's
  worth of rows into the fewest round trips that carry them.
* **One firmware value can have two spellings.** ``dutycycle`` (1.15+) and the older
  ``af`` both read and write ``airtime_factor``, so writing one stales the other
  (:attr:`RemoteSetting.overlaps`).

The catalog is not the whole page, though: it is what *any* MeshCore build might expose,
and a node can have settings beyond it (a third-party firmware's own knobs). Those are never
listed here and never probed — they are learned one node at a time from commands the reader
actually ran (:func:`parse_setting_command`, :func:`discovered_setting`), which is why
:attr:`RemoteSetting.discovered` exists and why :func:`learnable_from_get` has to care that
``get`` matches keys by prefix and ``set`` does not.

Replies are parsed *loosely* on purpose: repeater firmware answers tersely and has changed
its phrasing across versions (``"> 20"``, ``"tx: 20"``, ``"OK - repeat is now ON"``), so
values are extracted rather than pattern-matched. Firmware without a given key answers
with an error string (``??: key`` to a ``get``, ``unknown config: …`` to a ``set``) — or,
because ``get`` matches keys by prefix, with a shorter sibling's value (v1.15 answers
``get radio.fem.rxgain`` with the ``get radio`` line). The screen records either as *this
node doesn't have it* rather than breaking.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: Reply text (lowercased) that reads as the firmware refusing/unknowing a command. ``??:``
#: is how ``handleGetCmd`` answers a key it doesn't know; ``unknown config:`` is its ``set``
#: twin. ``err - `` is the ``region`` verbs' own refusal (``Err - not empty``).
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

#: A ``get`` reply's value echo. Whatever follows it is a value, even one that happens to
#: contain an error-ish word (a node named ``Unknown-Hill`` is not a refusal).
_ECHO = re.compile(r"^\s*-?>\s?")

#: The words a boolean reply may use for each state, beyond the setting's own
#: :attr:`RemoteSetting.words`.
_ON_WORDS = frozenset({"on", "true", "yes", "1"})
_OFF_WORDS = frozenset({"off", "false", "no", "0"})


@dataclass(frozen=True, slots=True)
class Option:
    """One value of an enumerated remote setting.

    Attributes:
        value: What the firmware takes in a ``set`` and answers to a ``get`` — also what
            the cache stores.
        label: What the value lane and the picker show for it.
        help: An optional one-line explanation for the picker row.
    """

    value: str
    label: str
    help: str = ""


@dataclass(frozen=True, slots=True)
class RemoteSetting:
    """One remote-CLI setting: its spelling, presentation, and typing.

    Attributes:
        key: The setting's identity: the CLI parameter name as the firmware spells it
            (``"txdelay"``) for a ``get``/``set`` key, the verb for a verb-shaped one, and
            the field's own name for a composite member. It is also the cache key.
        label: The display name shown in the editor lane.
        help: One-line explanation (no trailing period), shown beside the value.
        category: Editor section heading the setting sorts under.
        kind: ``"int"``, ``"float"``, ``"str"``, ``"bool"`` (on/off) or ``"enum"``.
        writable: Whether the firmware accepts a write for this setting.
        readable: Whether the firmware answers a read for it.
        minimum: Lower bound for numeric prompts, when known.
        maximum: Upper bound for numeric prompts, when known.
        unit: Display unit hint (e.g. ``"dBm"``, ``"min"``), purely cosmetic.
        decimals: Fixed decimal places a float is shown and sent with; ``None`` shows it
            as the firmware gave it, trailing zeros trimmed.
        zero_off: Whether ``0`` is accepted outside ``minimum``–``maximum``, meaning off
            (the advert intervals: 0, or 60–240 minutes).
        options: The values an ``enum`` takes, in picker order.
        words: What a ``bool`` sends for on and off. Most keys take ``on``/``off``;
            ``multi.acks`` is an ``atoi`` and needs ``1``/``0``.
        aliases: ``(reply word, value)`` pairs consulted before the kind's own parse, for
            a reply that names a value in words of its own — ``bridge.source`` answers
            ``logRx`` to what it is told as ``rx``.
        verb: For a verb-shaped setting, the command that reads it bare and writes it
            with the value appended (``"powersaving"``); empty for a ``get``/``set`` key.
        composite: The CLI key a group of settings is read and written through as one
            comma-joined value (``"radio"``); empty for a setting that stands alone.
        part: This setting's position within its composite's comma-joined value.
        overlaps: Keys that are another spelling of the same firmware value, whose cached
            reading a write to this one makes stale.
        discovered: Whether this setting is not in the catalog at all, but was learned from
            a command the reader ran on *one node's* command line (see
            :func:`discovered_setting`). Never true of a catalog entry.
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

    @property
    def get_command(self) -> str:
        """The CLI command that reads this setting (shared by a composite's members)."""
        if self.verb:
            return self.verb
        return f"get {self.composite or self.key}"

    def set_command(self, value: str) -> str:
        """The CLI command that writes ``value`` to this setting.

        Raises:
            ValueError: For a composite member, which is only ever written together with
                its siblings — see :func:`write_plan`.
        """
        if self.composite:
            raise ValueError(f"{self.key} is written through 'set {self.composite}'")
        if self.kind == "bool":
            value = self.words[0] if value == "on" else self.words[1]
        if self.verb:
            return f"{self.verb} {value}"
        return f"set {self.key} {value}"

    def display(self, value: str) -> str:
        """The value lane's text for a stored ``value`` (an enum shows its label)."""
        for option in self.options:
            if option.value == value:
                return option.label
        return value


def _radio(key: str, label: str, help_text: str, part: int, **typing) -> RemoteSetting:
    """One of the four fields ``get radio`` answers and ``set radio`` takes together."""
    return RemoteSetting(
        key=key,
        label=label,
        help=help_text,
        category="Radio",
        composite="radio",
        part=part,
        **typing,
    )


#: Every setting the standard MeshCore repeater/room firmware exposes over the mesh, in the
#: editor's display order. Board- and build-dependent ones (the front-end module's gains,
#: GPS, the bridge) are listed like any other: a node built without them answers ``??:``,
#: and the row says so instead of the catalog guessing which board it is talking to. Left
#: out on purpose: ``prv.key`` and ``freq`` writes (serial console only), ``extra.sf``
#: (LR2021 builds only, format board-defined), and the read-only diagnostics
#: (``public.key``, ``role``, ``bootloader.ver``, ``pwrmgt.*``) — facts, not settings, and
#: each one a paced round trip on every read. The command line reaches them all.
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
        # The firmware refuses 3 here (``mode < 3``), though the field has room for it. The
        # labels stay short because the widest value sizes the lane for every row: "hashes"
        # in each one pushed the descriptions off a 72-column screen once one was staged.
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
    ),
    RemoteSetting(
        key="allow.read.only",
        label="Allow read-only",
        category="Access",
        kind="bool",
        help="Allow read-only access (room servers)",
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
        # A board with a receiver answers "on, active|deactivated, fix, N sats" whether or
        # not it is enabled; the second word is the one that says.
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


#: Commands the remote CLI screen offers as completions, beyond the catalog's own read and
#: write spellings: the fixed verbs a MeshCore repeater answers over the mesh. The ones its
#: firmware keeps to the serial console (``erase``, ``log`` dumps, ``stats-*``) are left out,
#: since completing them would only invite a refusal.
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
    # ``region load`` is left out on purpose: it switches the CLI into a line-by-line
    # reading mode ended by a blank line, which a remote command line cannot send.
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
    """Every completion the remote CLI offers: verbs plus the catalog's read/write spellings."""
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
    """Look up a catalog setting by its key."""
    return next((s for s in REPEATER_SETTINGS if s.key == key), None)


def settings_by_category() -> list[tuple[str, list[RemoteSetting]]]:
    """The catalog grouped by category, in declaration order (the editor's layout)."""
    grouped: dict[str, list[RemoteSetting]] = {}
    for spec in REPEATER_SETTINGS:
        grouped.setdefault(spec.category, []).append(spec)
    return list(grouped.items())


def composite_members(composite: str) -> list[RemoteSetting]:
    """The settings a composite key carries, in their comma-joined order."""
    return sorted((s for s in REPEATER_SETTINGS if s.composite == composite), key=lambda s: s.part)


#: The category a discovered setting sorts under, and the heading its section takes.
DISCOVERED_CATEGORY = "Extra"

#: The description lane's text for a discovered setting. The catalog has no explanation to
#: give — all it knows is that this node answered the key — so the row says exactly that, and
#: says it short: the lane is shared with descriptions written to fit a 72-column screen.
DISCOVERED_HELP = "Found on this node"

#: Keys whose value is never written to disk, however it was asked for. ``prv.key`` is the
#: node's own identity: the firmware answers it on the serial console only (``sender_timestamp
#: == 0`` in ``handleGetCmd``), it is a fact rather than a setting, and a reply that reached
#: us anyway must not be the first place a repeater's private key lands in a file. The
#: command line still runs the command and still prints the answer; only the remembering
#: stops here.
NEVER_STORED = frozenset({"prv.key"})


@dataclass(frozen=True, slots=True)
class SettingCommand:
    """A remote-CLI command recognised as a read or a write of one named setting.

    Attributes:
        verb: ``"get"`` or ``"set"``.
        key: The setting's key, as the reader spelled it.
        value: What a ``set`` writes; ``""`` for a ``get``.
    """

    verb: str
    key: str
    value: str = ""


def parse_setting_command(command: str) -> SettingCommand | None:
    """Read one typed CLI command as a setting read/write, or ``None`` if it is neither.

    The grammar is the filter, and deliberately so: ``get <key>`` and ``set <key> <value>``
    are the firmware's own way of saying *this is a setting*, so recognising them costs no
    list of what to exclude. Every top-level verb — ``reboot``, ``advert``, ``clock sync``,
    ``start ota``, ``powersaving``, ``setperm`` — simply isn't this shape and falls out.
    A key is held to the spelling the firmware's own keys use, so a mistyped line can't
    invent one.
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
    """Every spelling a firmware ``get`` matches on, longest first.

    A composite's members share their composite's key (``get radio``); verb-shaped settings
    aren't ``get`` keys at all.
    """
    keys = {s.composite or s.key for s in REPEATER_SETTINGS if s.readable and not s.verb}
    return sorted(keys, key=len, reverse=True)


def shadowing_key(key: str) -> str | None:
    """The catalog ``get`` key that would answer a ``get key`` on firmware lacking ``key``.

    ``handleGetCmd`` compares with ``memcmp(config, "radio", 5)`` — no trailing space — so a
    key it doesn't know falls into the branch of any *shorter* key that prefixes it, and
    answers with that one's value. ``get radio.rxps`` on stock firmware comes back
    ``> 910.525,62.5,7,5``: not an error, and not a value of the key that was asked for.
    ``handleSetCmd`` matches ``"radio "`` *with* the space, so a write has no such shadow.
    """
    return next((k for k in get_match_keys() if key != k and key.startswith(k)), None)


def learnable_from_get(key: str, reply: str, known: Mapping[str, str]) -> bool:
    """Whether a ``get key`` reply really came from ``key`` and not from a shorter sibling.

    Only asked of a key the catalog doesn't have, where there is no typing to reject a
    wrong-shaped answer with. The reply is trusted when nothing shadows the key
    (:func:`shadowing_key`), or when it cannot be the shadow's answer — it doesn't parse as
    the shadow's type, or it parses to something other than what the shadow last said. A
    shadow whose own value was never read leaves the question unanswerable, and an
    unanswerable question is not a discovery.

    Args:
        key: The key the reader asked for.
        reply: What the node answered.
        known: The node's last-read values, keyed as the cache keys them.
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
        return True  # the reply isn't even shaped like the shadow's answer
    if any(known.get(k) is None for k in parsed):
        return False  # it could be the shadow's answer, and there is nothing to compare
    return any(known[k] != value for k, value in parsed.items())


def discovered_setting(key: str) -> RemoteSetting:
    """A catalog entry for a key the catalog hasn't got, which one node said it has.

    Everything downstream of the catalog — the read plan, the reply parser, the editor's
    lanes, staging, :func:`write_plan` — speaks :class:`RemoteSetting`, so a discovered key
    becomes one rather than growing a second path beside it. What it carries is only what
    was actually learned: the key as the node spells it, and a string value. No range, no
    words, no options — the firmware never said, and guessing is how a row would start
    lying about a node whose build we have never seen.
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
    """Whether a reply text reads as the firmware refusing the command.

    A reply that opens with a ``get``'s ``>`` value echo is a value, whatever it says.
    """
    if _ECHO.match(reply):
        return False
    lowered = reply.lower()
    return any(marker in lowered for marker in _ERRORISH)


def parse_reply_value(spec: RemoteSetting, reply: str | None) -> str | None:
    """Extract a stored value from a read's reply, or ``None`` when unusable.

    Numbers are pulled out of whatever phrasing the firmware used and formatted the way
    :func:`normalize_value` formats a typed one, so a value read back compares equal to the
    same value staged. A composite member takes its own field of the comma-joined reply.
    Error-ish replies parse as ``None`` so a firmware without the key never adopts the error
    string as a value.

    Args:
        spec: The setting the reply answers.
        reply: The raw reply text, or ``None`` if the node never answered.

    Returns:
        The value as stored text (an enum's :attr:`Option.value`, a bool's ``on``/``off``),
        ``""`` for a string the node reports as empty, or ``None``.
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
        return text  # "> " with nothing after it is an empty string, which is a value
    return text or None


def _format_number(spec: RemoteSetting, value: float) -> str:
    """One number as the cache stores it: whole, fixed-decimal, or trimmed."""
    if spec.kind == "int":
        return str(int(round(value)))
    if spec.decimals is not None:
        text = f"{value:.{spec.decimals}f}"
    else:
        # The firmware's own ftoa: up to 7 places, trailing zeros trimmed.
        text = f"{value:.7f}".rstrip("0").rstrip(".")
    return text.lstrip("-") if float(text) == 0 else text


def normalize_value(spec: RemoteSetting, raw: str) -> str:
    """A validated typed value in the form the cache stores and the firmware is sent.

    TX delay entered as ``0.33`` becomes ``0.3``: rounded to the setting's decimals, and
    spelled exactly as a read of the same value would be, so staging what the node already
    holds unstages rather than queueing a no-op ``set``.
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
    """The prompt's help line: what validation will accept, and in what unit."""
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
    """The read commands that answer ``specs``, each once, with the settings it fills.

    Every command is one paced round trip, so the four radio fields ask ``get radio`` a
    single time between them — and asking for any one of them fills all four, because the
    reply carries them anyway and a radio line half-refreshed would disagree with itself.
    Unreadable settings are skipped.
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
        values: Setting key -> the value this command leaves the node holding. For a
            composite that includes the unstaged siblings it restates.
        missing: The composite siblings neither staged nor known — the reason
            ``command`` is empty. Read them first (:func:`composite_reads`).
    """

    command: str
    values: dict[str, str]
    missing: tuple[str, ...] = ()


def write_plan(pending: Mapping[str, str], known: Mapping[str, str]) -> list[Write]:
    """Group staged values into the commands that send them, in catalog order.

    A standalone setting is its own ``set``. A composite member is sent with all its
    siblings in one command, the unstaged ones restated from what the node last said —
    ``set radio`` has no way to change one field alone.

    Args:
        pending: Setting key -> staged value.
        known: Setting key -> the node's last-read value, for composite siblings.

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
    # A staged key the catalog hasn't got is a discovered one (nothing else can stage a
    # row), and it is written the only way anything knows how: its own plain `set`.
    for key, value in pending.items():
        if get_setting(key) is None:
            writes.append(Write(f"set {key} {value}", {key: value}))
    return writes


def composite_reads(pending: Mapping[str, str], known: Mapping[str, str]) -> list[str]:
    """The reads a write needs first: composites staged without all their siblings known."""
    commands: list[str] = []
    for write in write_plan(pending, known):
        if write.missing:
            spec = get_setting(write.missing[0])
            if spec is not None and spec.get_command not in commands:
                commands.append(spec.get_command)
    return commands
