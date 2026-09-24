# SPDX-License-Identifier: Apache-2.0
"""Declarative registry of MeshTerm's own preferences, and the TOML file they live in.

The distinction this module draws is between the three kinds of "setting" the app already
had and never named apart:

* :mod:`meshterm.core.device_config` describes the **radio's** settings — they live in
  the companion's firmware, are read live each session, and are edited through the Device
  config screen.
* :mod:`meshterm.core.config` (``config.toml``) describes **where things are and which
  device to talk to** — the config directory, the database, the named device profiles.
  It is machine setup, edited in a text editor, and has no place in a full-screen page.
* This module describes **how MeshTerm itself behaves** — whether it opens the link at
  launch, how long it waits between transmissions, how much history it keeps, how a map
  frames itself. Those values were scattered through the code as module constants and a
  handful of undocumented ``config.toml`` keys with no screen behind them; they are
  gathered here, given one place to be changed (the Preferences page), and one file to be
  remembered in.

Each :class:`PrefSpec` states a preference's group, type, bounds, and — the point of the
whole exercise — its **default**. A preference is only written to disk once it is changed
away from that default, so the file is a short list of your disagreements with the
built-in behaviour rather than a snapshot that silently pins every value forever. Delete a
key (or use *Reset to defaults*) and the code's default takes over again.

The file is ``<config_dir>/preferences.toml``, written flat, grouped and commented so it
reads the way the page does — TOML, like ``config.toml`` and the device-config backups,
so every file a person edits by hand speaks one syntax. Reads are tolerant: an unknown
key, a malformed value, or a line TOML refuses costs that one override, never the rest of
the file and never the session.

Two access paths, deliberately:

* :attr:`~meshterm.context.AppContext.preferences` is the explicit one, and what every
  tool, service, and screen should use.
* :func:`current` is for the render and session layer, which has no context to reach
  through — the same reason :func:`meshterm.platforms.get_platform` exists. The context
  installs itself there as it is built.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .advert_store import DEFAULT_QUIET_S, QUIET_CHOICES_S
from .atomicwrite import write_atomically
from .courier_store import DONE_CAP
from .geo import DEFAULT_VIEW_FRACTION
from .watch_store import ALERT_CAP, DEFAULT_SILENCE_HOURS, OFF, SILENCE_CHOICES_H

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib

#: Display groups, in the order the page and the file present them. The order is the
#: order a session happens in — what MeshTerm does to the radio as the link opens, what
#: it puts on the air, how loud, what it watches for, how it draws the world, what it
#: keeps, and how it paints. Nothing sorts alphabetically: a reader looking for "how long
#: before I give up on a message" should not have to know it starts with a D.
GROUPS: tuple[str, ...] = (
    "Device",
    "Sending",
    "TX optimize",
    "Watchtower",
    "Map",
    "History",
    "Display",
    "Diagnostics",
)


class PreferenceError(ValueError):
    """Raised when a preference value cannot be parsed or fails validation.

    The message is user-facing — the CLI prints it, and the editor's typed prompt shows
    it under the input as the reason the value was refused.
    """


@dataclass(frozen=True, slots=True)
class PrefSpec:
    """Specification for one preference.

    Attributes:
        key: Canonical key — the attribute name on :class:`Preferences`, and the key in
            the TOML file.
        label: Human-friendly name, as the page's SETTING lane shows it.
        help: One-line description, as the page's DESCRIPTION lane shows it.
        group: One of :data:`GROUPS`.
        value_type: ``"str" | "int" | "float" | "bool" | "enum"``.
        default: The built-in value, used whenever the file names no override.
        choices: For ``enum``, a mapping of allowed value to label.
        minimum: Inclusive lower bound for numeric types, if any.
        maximum: Inclusive upper bound for numeric types, if any.
        unit: Short suffix appended when the value is formatted (``"s"``, ``"dBm"``,
            ``"days"``) so a bare number in the VALUE lane still says what it counts.
        relaunch: Whether the new value only takes hold next launch. Woven into the
            description rather than shown as its own mark — it is a caveat about one
            preference, not a status the reader scans a column for.
        platforms: Which :class:`~meshterm.platforms.Platform` names the preference is
            *offered* on, or ``None`` (the default) for all of them. A preference about
            hardware only one flavour has — the PicoCalc's console font — would be a row
            answering a question the desktop cannot ask, so the page leaves it out there.
            Only the page: the key stays in the registry on every platform, so a file
            written on the handheld still parses, keeps its value and saves back out on a
            desktop rather than being silently dropped by a machine that merely cannot
            act on it.
    """

    key: str
    label: str
    help: str
    group: str
    value_type: str
    default: Any
    choices: dict[Any, str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    relaunch: bool = False
    platforms: frozenset[str] | None = None

    @property
    def description(self) -> str:
        """The DESCRIPTION lane's text: the help, plus the relaunch caveat where it applies."""
        return f"{self.help} (next launch)" if self.relaunch else self.help

    def offered_on(self, platform: str) -> bool:
        """Whether the editor draws a row for this preference on ``platform``.

        Args:
            platform: A :attr:`~meshterm.platforms.Platform.name`.

        Returns:
            ``True`` unless :attr:`platforms` names a set this platform is not in.
        """
        return self.platforms is None or platform in self.platforms


#: The two languages the plain CLI can print a time in. Named for what the reader sees
#: rather than for the mechanism: an *age* is how long ago, a *timestamp* is when.
_CLI_TIME_CHOICES: dict[str, str] = {"relative": "ages", "absolute": "timestamps"}

#: The silence-rule choices, drawn from the Watchtower's own ring so the two never drift.
_SILENCE_CHOICES: dict[int, str] = {
    **{h: f"{h} h" for h in SILENCE_CHOICES_H},
    OFF: "off",
}

#: How long the air must stay quiet before the weekly flood advert goes out, drawn from the
#: scheduler's own choices so the two never drift.
_QUIET_CHOICES: dict[int, str] = {s: f"{s} s" for s in QUIET_CHOICES_S}

#: How the terminal-width reclaim can be asked for: follow the platform's own verdict, or
#: overrule it in either direction. The row's description asks a yes/no question ("use the
#: column ... ?"), so the values answer it in those words rather than naming the mechanism
#: twice. See :func:`meshterm.ui.tui.session._reclaim_last_column` for why a terminal may
#: need to disagree with its platform.
_WIDTH_CHOICES: dict[str, str] = {"auto": "auto", "yes": "yes", "no": "no"}

#: How many colours to send the terminal. ``auto`` is the honest default and the only
#: value that is not itself a depth: it means "take prompt_toolkit's verdict unless we can
#: positively establish better", which is the difference between fixing a host and breaking
#: one. The rest are named for the count a reader can see rather than for the bit depth,
#: because "24-bit" is a fact about the wire and "16 million" is a fact about the screen.
#: See :func:`meshterm.ui.tui.session._color_depth` for why auto is not simply "the most
#: this terminal can do".
_COLOR_DEPTH_CHOICES: dict[str, str] = {
    "auto": "auto",
    "truecolor": "16 million",
    "256": "256",
    "16": "16",
}

#: What MeshTerm may offer when it lands in a console that cannot draw it. Only ever
#: consulted on the classic Windows console, the one host with no font fallback of its own.
_CONSOLE_SETUP_CHOICES: dict[str, str] = {
    "auto": "Automatic",
    "off": "Leave it alone",
}

#: The two console fonts ``scripts/picocalc/calculinux-console-font-6x12.sh`` (and its 6x8
#: companion) build for the PicoCalc's framebuffer panel. Each is named by its cell and by the
#: screen that cell buys, because choosing a font here is choosing how much fits — not a bitmap.
#: Plain ASCII ``x`` and not ``×``: the multiplication sign is not one of the 512 glyphs
#: the console font carries, so it would draw as a tofu box on the one platform this
#: preference is offered on. The value maps to a file in
#: :data:`meshterm.services.consolefont.FONT_FILES`, which is where a filename belongs —
#: a path on one device is not something a reader picks.
_CONSOLE_FONT_CHOICES: dict[str, str] = {"6x12": "6x12 (53x26)", "6x8": "6x8 (53x40)"}

#: What the log file keeps. The plain level names, which are what every other tool calls
#: these — someone being talked through a problem is being told "set it to debug", not
#: "set it to everything".
_LOG_LEVEL_CHOICES: dict[str, str] = {
    "ERROR": "Error",
    "WARNING": "Warning",
    "INFO": "Info",
    "DEBUG": "Debug",
}


#: Every preference MeshTerm has, in page order. Adding one here gives it a row on the
#: Preferences page, a key in the TOML file, a ``preferences get``/``set`` CLI face, and a
#: default — nothing else has to follow. A default that a module already states as its
#: code-level behaviour is named, never re-typed: one source, so changing it cannot leave
#: the constant and the registry disagreeing about what MeshTerm does.
PREFERENCES: tuple[PrefSpec, ...] = (
    # --- Device ------------------------------------------------------------------
    PrefSpec(
        key="set_clock_on_connect",
        label="Set clock on connect",
        help="Set the device clock from this computer when it connects",
        group="Device",
        value_type="bool",
        # Off: writing to a radio nobody asked to be written to is the owner's call. The
        # set is silent (the log records it) and happens once per connection — see
        # :mod:`meshterm.services.clock_sync`.
        default=False,
    ),
    # --- Sending -----------------------------------------------------------------
    PrefSpec(
        key="trace_cooldown_s",
        label="Transmit cooldown",
        help="Wait this long before transmitting again",
        group="Sending",
        value_type="float",
        default=5.0,
        minimum=0.1,  # a floor, not a switch: there is no "off" for duty-cycle courtesy
        maximum=60.0,
        unit="s",
    ),
    PrefSpec(
        key="flood_advert_cooldown_s",
        label="Flood cooldown",
        help="Extra wait before another mesh-wide advert",
        group="Sending",
        value_type="float",
        default=60.0,
        minimum=5.0,  # every repeater in range rebroadcasts one; five seconds is the floor
        maximum=3600.0,
        unit="s",
    ),
    PrefSpec(
        key="weekly_flood_advert",
        label="Weekly advert",
        help="Flood an advert after a week without one",
        group="Sending",
        value_type="bool",
        # Off: a companion never advertises on its own, so this is the only advert MeshTerm
        # would ever send unasked, and every repeater in range rebroadcasts it. Turning it
        # on starts the week rather than sending — see meshterm.services.advert_scheduler.
        default=False,
    ),
    PrefSpec(
        key="advert_quiet_s",
        label="Advert quiet",
        help="Silence to wait for before the weekly advert",
        group="Sending",
        value_type="enum",
        default=DEFAULT_QUIET_S,
        choices=_QUIET_CHOICES,
    ),
    PrefSpec(
        key="direct_message_soft_retries",
        label="Message retries",
        help="Times to resend a message that gets no reply",
        group="Sending",
        value_type="int",
        default=0,  # one shot: a re-send is a second transmission on a shared mesh
        minimum=0,
        maximum=2,
    ),
    # --- TX optimize -------------------------------------------------------------
    PrefSpec(
        key="tx_opt_min",
        label="Range min",
        help="Weakest power it will try",
        group="TX optimize",
        value_type="int",
        default=18,
        minimum=1,
        maximum=30,
        unit="dBm",
    ),
    PrefSpec(
        key="tx_opt_max",
        label="Range max",
        help="Strongest power it will try",
        group="TX optimize",
        value_type="int",
        default=28,
        minimum=1,
        maximum=30,
        unit="dBm",
    ),
    PrefSpec(
        key="tx_snr_tolerance_db",
        label="Tie margin",
        help="Signals this close are a tie, so less power wins",
        group="TX optimize",
        value_type="float",
        default=1.0,
        minimum=0.0,
        maximum=10.0,
        unit="dB",
    ),
    # --- Watchtower --------------------------------------------------------------
    PrefSpec(
        key="watch_silence_hours",
        label="Alert after",
        help="Warn when a watched node goes quiet this long",
        group="Watchtower",
        value_type="enum",
        default=DEFAULT_SILENCE_HOURS,
        choices=_SILENCE_CHOICES,
    ),
    PrefSpec(
        key="watch_alerts_kept",
        label="Alerts kept",
        help="How many past alerts to keep",
        group="Watchtower",
        value_type="int",
        default=ALERT_CAP,
        minimum=10,
        maximum=5000,
    ),
    # --- Map ---------------------------------------------------------------------
    PrefSpec(
        key="map_view_fraction",
        label="Map fit",
        help="Share of nodes to fit on screen; 1 shows them all",
        group="Map",
        value_type="float",
        default=DEFAULT_VIEW_FRACTION,
        minimum=0.1,
        maximum=1.0,
    ),
    PrefSpec(
        key="basemap_tilejson_url",
        label="Map tiles",
        help="Where map images come from",
        group="Map",
        value_type="str",
        default="https://tiles.openfreemap.org/planet",
        relaunch=True,
    ),
    # --- History kept ------------------------------------------------------------
    PrefSpec(
        key="history_days",
        label="Keep packets for",
        help="Older ones are deleted; 0 keeps everything",
        group="History",
        value_type="int",
        default=365,
        minimum=0,
        maximum=36500,
        unit="days",
    ),
    PrefSpec(
        key="chat_history_limit",
        label="Chat history",
        help="Old messages to load when you open a chat",
        group="History",
        value_type="int",
        default=200,
        minimum=20,
        maximum=5000,
    ),
    PrefSpec(
        key="courier_history_kept",
        label="Courier history",
        help="Finished outbox messages to keep",
        group="History",
        value_type="int",
        default=DONE_CAP,
        minimum=10,
        maximum=5000,
    ),
    # --- Display -----------------------------------------------------------------
    PrefSpec(
        key="cli_time_format",
        label="Command-line times",
        help="Whether the command line prints ages or timestamps",
        group="Display",
        value_type="enum",
        # Relative, because a person at a prompt asking "heard recently?" should not have
        # to subtract an ISO instant from `date` to find out. `--absolute` overrides this
        # for one run, and the JSON face is UTC either way — it is read somewhere else and
        # often later, where a relative age has nothing to be relative to.
        default="relative",
        choices=_CLI_TIME_CHOICES,
    ),
    PrefSpec(
        key="fast_render",
        label="Fast redraw",
        help="Faster screen updates; turn off if it looks wrong",
        group="Display",
        value_type="bool",
        default=True,
        relaunch=True,
    ),
    PrefSpec(
        key="full_width",
        label="Last column",
        help="Use the extra column some terminals hide",
        group="Display",
        value_type="enum",
        default="auto",
        choices=_WIDTH_CHOICES,
    ),
    PrefSpec(
        key="color_depth",
        label="Colours",
        help="How many colours to send this terminal",
        group="Display",
        value_type="enum",
        # Auto, because the count a terminal accepts is a fact about the terminal and the
        # reader should not have to know it. It is only ever consulted to *raise* the
        # verdict prompt_toolkit already reached, never to lower it — a terminal that
        # cannot be shown to do better keeps exactly what it had.
        default="auto",
        choices=_COLOR_DEPTH_CHOICES,
        relaunch=True,
    ),
    PrefSpec(
        key="console_setup",
        label="Console setup",
        help="Move to a better terminal when this console can't draw MeshTerm",
        group="Display",
        value_type="enum",
        # Reopening in Windows Terminal is done, not asked: the classic console cannot
        # draw a single icon whatever font it is given, so there is only one sensible
        # answer and the reader has not seen the app yet to judge it. Installing a *font*
        # still asks, because that writes to their machine. Declining that twice writes
        # "off" here, which turns both off for good.
        default="auto",
        choices=_CONSOLE_SETUP_CHOICES,
    ),
    PrefSpec(
        key="console_font",
        label="Console font",
        help="Bigger text, or more rows on the handheld's panel",
        group="Display",
        value_type="enum",
        # The 6x12 Terminus derivative, because it is the font the device boots with and
        # the one every screen's row budget is designed to (Platform.readable_rows is 26).
        # The 6x8 build is the same glyph inventory in a shorter cell — fourteen more rows
        # for anyone who would rather see more of a list than read it comfortably.
        default="6x12",
        choices=_CONSOLE_FONT_CHOICES,
        platforms=frozenset({"picocalc"}),
    ),
    # --- Diagnostics ---------------------------------------------------------------
    PrefSpec(
        key="log_level",
        label="Log detail",
        help="How much MeshTerm writes to its log file",
        group="Diagnostics",
        value_type="enum",
        # Problems, not a narration of a working session. The file used to take
        # everything, which buried the dozen interesting lines under twenty thousand
        # dull ones and made it awkward to attach to a bug report.
        default="WARNING",
        choices=_LOG_LEVEL_CHOICES,
        relaunch=True,
    ),
)

_BY_KEY: dict[str, PrefSpec] = {spec.key: spec for spec in PREFERENCES}

#: Every key that was once a preference and no longer is. A retired key is never reused:
#: a value can outlive its key in a file nobody saved since, and if the name came back
#: with a new meaning — seconds become minutes, a range moves — an old ``30`` that still
#: passes the new spec would load cleanly and mean something else, which no validation
#: can catch. So a preference that returns changed returns under a new name (a unit in the
#: key makes that the natural move: ``_s`` to ``_min``), and ``tests/test_retired.py``
#: fails any registry that takes one of these back. Loading refuses them outright.
RETIRED: frozenset[str] = frozenset(
    {
        # Per-device background advert cadences, moved to Device config and since replaced
        # by the weekly flood advert (``weekly_flood_advert``).
        "advert_direct_hours",
        "advert_flood_hours",
    }
)

#: The reason :attr:`Preferences.dropped` gives for a key found in :data:`RETIRED`.
DROPPED_RETIRED = "retired"

#: The reason :attr:`Preferences.dropped` gives for a key the registry has never held.
DROPPED_UNKNOWN = "unknown"


def get_spec(key: str) -> PrefSpec:
    """Look up one preference's spec.

    Args:
        key: The preference key.

    Returns:
        Its :class:`PrefSpec`.

    Raises:
        PreferenceError: If no preference is registered under ``key``.
    """
    try:
        return _BY_KEY[key]
    except KeyError:
        if key in RETIRED:
            raise PreferenceError(f"{key!r} is no longer a preference") from None
        raise PreferenceError(f"unknown preference: {key!r}") from None


def by_group(platform: str | None = None) -> list[tuple[str, list[PrefSpec]]]:
    """Every preference grouped for display, in :data:`GROUPS` order.

    Args:
        platform: A :attr:`~meshterm.platforms.Platform.name` to draw the page for, which
            drops the preferences that platform is not offered (see
            :meth:`PrefSpec.offered_on`). ``None`` — the default, and what the file writer
            uses — keeps every preference, so an override made on one flavour survives
            being loaded and saved on another.

    Returns:
        ``(group, specs)`` pairs; a group holding no preferences is omitted.
    """
    kept = [s for s in PREFERENCES if platform is None or s.offered_on(platform)]
    grouped = [(g, [s for s in kept if s.group == g]) for g in GROUPS]
    return [(g, specs) for g, specs in grouped if specs]


# --- value parsing / formatting -----------------------------------------------

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def parse_value(spec: PrefSpec, raw: Any) -> Any:
    """Parse and validate a raw value (a typed scalar, or text from the CLI/file/prompt).

    Args:
        spec: The target preference.
        raw: The value to coerce.

    Returns:
        The typed, range-checked value.

    Raises:
        PreferenceError: If the value is the wrong type, out of range, or not a choice.
    """
    text = str(raw).strip()
    if spec.value_type == "bool":
        low = text.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise PreferenceError(f"{spec.key}: expected on or off, got {raw!r}")

    if spec.value_type == "str":
        return text

    if spec.value_type == "enum":
        choices = spec.choices or {}
        if raw in choices and not isinstance(raw, bool):
            return raw
        if isinstance(raw, bool):
            # A hand edit that answers a yes/no choice with a TOML boolean
            # (``full_width = true``) hands us ``True`` where the choice is spelled "yes".
            # We quote ours on the way out, but someone typing the obvious thing should
            # still be understood: map the boolean back to whichever spelling this offers.
            for choice in choices:
                if str(choice).lower() in (_TRUE if raw else _FALSE):
                    return choice
        # Text arriving from the CLI or a hand-edited file: match a choice by its own
        # spelling, so `preferences set watch_silence_hours 6` and a TOML `6` land alike.
        for choice in choices:
            if text.lower() == str(choice).lower():
                return choice
        allowed = ", ".join(str(c) for c in choices)
        raise PreferenceError(f"{spec.key}: must be one of {allowed}, got {raw!r}")

    try:
        value: Any = float(text) if spec.value_type == "float" else int(text, 0)
    except (ValueError, TypeError) as exc:
        raise PreferenceError(f"{spec.key}: invalid {spec.value_type} value {raw!r}") from exc
    if spec.minimum is not None and value < spec.minimum:
        raise PreferenceError(f"{spec.key}: must be >= {spec.minimum:g}, got {value:g}")
    if spec.maximum is not None and value > spec.maximum:
        raise PreferenceError(f"{spec.key}: must be <= {spec.maximum:g}, got {value:g}")
    return value


def format_value(spec: PrefSpec, value: Any) -> str:
    """Render one preference's value for display.

    Booleans read ``on``/``off`` (the words the editor's button pair offers), an enum
    reads its own label, and a number carries its unit so a bare figure in the VALUE lane
    still says what it counts.

    Args:
        spec: The preference the value belongs to.
        value: The value to render.

    Returns:
        The display string.
    """
    if spec.value_type == "bool":
        return "on" if value else "off"
    if spec.value_type == "enum" and spec.choices is not None:
        return str(spec.choices.get(value, value))
    if spec.value_type in ("int", "float"):
        text = f"{value:g}"
        return f"{text} {spec.unit}" if spec.unit else text
    return str(value)


def range_hint(spec: PrefSpec) -> str:
    """A muted "allowed values" hint for a typed prompt, or ``""`` where nothing bounds it."""
    unit = f" {spec.unit}" if spec.unit else ""
    if spec.minimum is not None and spec.maximum is not None:
        return f"Allowed: {spec.minimum:g} – {spec.maximum:g}{unit}"
    if spec.minimum is not None:
        return f"Allowed: >= {spec.minimum:g}{unit}"
    if spec.maximum is not None:
        return f"Allowed: <= {spec.maximum:g}{unit}"
    return ""


# --- the store ----------------------------------------------------------------

#: The file's name inside the config directory — one spelling for every place that opens it.
PREFERENCES_FILENAME = "preferences.toml"

#: The file's opening comment. It says the one thing a reader hand-editing the file needs
#: to know — that absence means "use the default", so deleting a line is how a preference
#: is undone — and points at the two neighbouring kinds of setting so nobody looks for a
#: device's radio parameters in here.
_HEADER = """\
# MeshTerm preferences -- how the app itself behaves.
#
# Every preference has a built-in default. A key is listed here only while you have
# changed it away from that default, so deleting a line (or "Reset to defaults" on the
# Preferences page) hands the value back to the code. `meshterm preferences show` prints
# every preference, what it is set to, and what it defaults to.
#
# One `key = value` per line: text in quotes ("DEBUG"), numbers and true/false bare.
#
# Device profiles, the database location, and the rest of the machine setup stay in
# config.toml; the radio's own settings live on the radio, under Device config.
"""


def _salvage(text: str) -> dict[str, Any]:
    """Read a file TOML refused as a document, one ``key = value`` line at a time.

    MeshTerm writes the file flat — no tables, one entry to a line — so a line is a whole
    entry and can be judged on its own. A line TOML accepts keeps its typed value. One it
    refuses keeps its right-hand side as text, which is exactly what :func:`parse_value`
    takes from the command line, so the likeliest hand edit of all — a word left unquoted,
    ``log_level = DEBUG`` — is still understood. Whatever the spec then refuses is dropped
    by :meth:`Preferences.update`, the same as any other bad value.
    """
    data: dict[str, Any] = {}
    for line in text.splitlines():
        entry = line.strip()
        if not entry or entry.startswith(("#", "[")) or "=" not in entry:
            continue
        try:
            data.update(tomllib.loads(entry))
        except tomllib.TOMLDecodeError:
            key, _, value = entry.partition("=")
            data[key.strip()] = value.split("#", 1)[0].strip()
    return data


class Preferences:
    """The effective preferences: the built-in defaults, with the file's overrides on top.

    Read a preference as an attribute — ``preferences.trace_cooldown_s`` — which resolves
    through the registry, so a typo raises :class:`AttributeError` at the call site
    instead of quietly reading ``None``. :meth:`set` takes a raw value, validates it
    through the key's spec, and records it only while it differs from the default;
    :meth:`save` writes the result.

    Nothing here writes on its own: the Preferences page stages changes and saves them on
    *Apply*, which — with the CLI — is the only path that reaches :meth:`save`.
    """

    def __init__(self, path: Path | None = None, values: Mapping[str, Any] | None = None) -> None:
        """Build a preferences view over an optional file and an optional set of overrides.

        Args:
            path: Where :meth:`save` writes. ``None`` for an in-memory set (tests, and
                the fallback :func:`current` hands out before a context is built).
            values: Overrides to start from; invalid ones are dropped, and a value equal
                to its default is not recorded as an override at all.
        """
        self._path = path
        self._values: dict[str, Any] = {}
        self._dropped: dict[str, str] = {}
        if values:
            self._absorb(values)

    @classmethod
    def load(cls, path: Path) -> Preferences:
        """Read ``path``, returning all-defaults for a missing, empty, or unreadable file.

        TOML refuses a whole document over one bad line, and a hand edit should cost the
        line it is on, not the file: a document TOML refuses is read again a line at a
        time (:func:`_salvage`), keeping every entry that still makes sense.

        Args:
            path: The TOML file to read.

        Nothing the file holds that the registry cannot take survives the next
        :meth:`save`, which writes only the overrides in force: a retired key, a key
        never registered (a typo, most likely), and a value its spec refuses are all
        left out, and :attr:`dropped` says which and why, for the caller to log once
        logging is configured (see :func:`report_dropped`).

        Returns:
            The loaded preferences, bound to ``path`` for a later :meth:`save`.
        """
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return cls(path)
        try:
            data: dict[str, Any] = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            data = _salvage(text)
        return cls(path, data)

    def _absorb(self, values: Mapping[str, Any]) -> None:
        """Take in a file's entries, recording in :attr:`dropped` each one refused."""
        for raw_key, value in values.items():
            key = str(raw_key)
            if key in RETIRED:
                self._dropped[key] = DROPPED_RETIRED
                continue
            try:
                self.set(key, value)
            except PreferenceError as exc:
                self._dropped[key] = str(exc) if key in _BY_KEY else DROPPED_UNKNOWN

    @property
    def dropped(self) -> dict[str, str]:
        """What loading refused, key by key (a copy).

        The reason is :data:`DROPPED_RETIRED`, :data:`DROPPED_UNKNOWN`, or — for a key
        that is registered but whose value its spec refused — the refusal itself.
        """
        return dict(self._dropped)

    @property
    def path(self) -> Path | None:
        """Where :meth:`save` writes, or ``None`` for an in-memory set."""
        return self._path

    def __getattr__(self, name: str) -> Any:
        """Resolve a registered preference key as an attribute.

        Raises:
            AttributeError: For any name that is not a preference key, so a mistyped
                ``preferences.trace_cooldwn_s`` fails where it is written rather than
                quietly reading nothing.
        """
        if name.startswith("_") or name not in _BY_KEY:
            raise AttributeError(name)
        return self.get(name)

    def get(self, key: str) -> Any:
        """The effective value for ``key``: the override where there is one, else the default.

        Raises:
            PreferenceError: If ``key`` is not a registered preference.
        """
        spec = get_spec(key)
        return self._values.get(key, spec.default)

    def is_overridden(self, key: str) -> bool:
        """Whether ``key`` currently differs from its built-in default."""
        return key in self._values

    def overrides(self) -> dict[str, Any]:
        """The values that differ from their defaults, in registry order (a copy)."""
        return {s.key: self._values[s.key] for s in PREFERENCES if s.key in self._values}

    def set(self, key: str, raw: Any) -> Any:
        """Validate ``raw`` for ``key`` and record it, dropping it when it *is* the default.

        An override is only ever a disagreement with the default, so setting a value back
        to its default clears the entry rather than pinning it. That is what lets a later
        change to the code's default reach an install that never asked to be exempt.

        Args:
            key: The preference key.
            raw: The new value, typed or as text.

        Returns:
            The parsed value now in force.

        Raises:
            PreferenceError: If ``key`` is unknown or ``raw`` fails its spec.
        """
        spec = get_spec(key)
        value = parse_value(spec, raw)
        if value == spec.default:
            self._values.pop(key, None)
        else:
            self._values[key] = value
        return value

    def update(self, values: Mapping[str, Any]) -> int:
        """Apply several changes at once, skipping any the registry or a spec refuses.

        Tolerant by design: this is the path a hand-edited file arrives through, and one
        bad line should cost that line, not the file.

        Args:
            values: ``key -> value`` pairs.

        Returns:
            How many were accepted.
        """
        applied = 0
        for key, value in values.items():
            try:
                self.set(str(key), value)
            except PreferenceError:
                continue
            applied += 1
        return applied

    def reset(self) -> int:
        """Drop every override, returning how many there were."""
        count = len(self._values)
        self._values.clear()
        return count

    def as_toml(self) -> str:
        """Render the current overrides as the file's text: header, then grouped entries.

        Each group holding an override gets its own comment heading, and each entry its
        help line and its default above it, so the file reads the way the page does
        rather than as a bare map someone has to look up elsewhere.
        """
        import tomli_w

        lines = [_HEADER]
        for group, specs in by_group():
            present = [s for s in specs if s.key in self._values]
            if not present:
                continue
            lines.append(f"# --- {group} ---")
            for spec in present:
                entry = tomli_w.dumps({spec.key: self._values[spec.key]}).rstrip("\n")
                lines.append(f"# {spec.help}")
                lines.append(f"# default: {format_value(spec, spec.default)}")
                lines.append(entry)
            lines.append("")
        if len(lines) == 1:
            lines.append("# (nothing overridden -- every preference is at its default)")
            lines.append("")
        return "\n".join(lines)

    def save(self) -> None:
        """Write the overrides to :attr:`path` atomically (a crash mid-write keeps the old file).

        Raises:
            RuntimeError: If this set was built without a path (an in-memory set).
        """
        if self._path is None:
            raise RuntimeError("these preferences have no file to save to")
        write_atomically(self._path, self.as_toml())


#: The set installed for this process. Starts as a defaults-only, file-less instance so
#: the render layer works before (and without) an :class:`~meshterm.context.AppContext` —
#: a specimen render, a unit test, the CLI's early boot.
_current: Preferences = Preferences()


def report_dropped(preferences: Preferences, log: logging.Logger) -> None:
    """Log what loading the file refused: the entries its next save will leave out.

    A retired key is expected — it was written by an older MeshTerm — so it is noted at
    INFO. Anything else was probably typed by hand and is not doing what its author
    meant, so it is a WARNING, and the line names it so the typo can be found.

    Args:
        preferences: A set built by :meth:`Preferences.load`.
        log: Where to report.
    """
    name = preferences.path.name if preferences.path else PREFERENCES_FILENAME
    for key, why in preferences.dropped.items():
        if why == DROPPED_RETIRED:
            log.info("%s: ignoring retired preference %r; the next save drops it", name, key)
        elif why == DROPPED_UNKNOWN:
            log.warning("%s: ignoring unknown preference %r; the next save drops it", name, key)
        else:
            log.warning("%s: ignoring %s; the next save drops it", name, why)


def current() -> Preferences:
    """The preferences in force for this process.

    For the layers that have no context to reach through — the session's renderer, the
    stores constructed without one — mirroring
    :func:`meshterm.platforms.get_platform`. Anything holding an
    :class:`~meshterm.context.AppContext` reads
    :attr:`~meshterm.context.AppContext.preferences` instead.
    """
    return _current


def install(preferences: Preferences) -> None:
    """Make ``preferences`` the set :func:`current` returns (the context does this at boot)."""
    global _current
    _current = preferences


__all__ = [
    "GROUPS",
    "PREFERENCES",
    "RETIRED",
    "PrefSpec",
    "PreferenceError",
    "Preferences",
    "by_group",
    "current",
    "format_value",
    "get_spec",
    "install",
    "parse_value",
    "range_hint",
    "report_dropped",
]
