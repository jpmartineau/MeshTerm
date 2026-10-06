# SPDX-License-Identifier: Apache-2.0
"""Declarative registry of MeshTerm's own preferences, and the TOML file that keeps them.

This module separates three types of "setting". The app had all three before, but it did
not give them different names:

* :mod:`meshterm.core.device_config` describes the settings of the **radio**. They are
  in the firmware of the companion. MeshTerm reads them live in each session, and the
  user edits them on the Device config screen.
* :mod:`meshterm.core.config` (``config.toml``) describes **where things are and which
  device to talk to**: the config directory, the database, and the named device profiles.
  It is machine setup. The user edits it in a text editor, and it has no place on a
  full-screen page.
* This module describes **how MeshTerm itself behaves**: whether it opens the link at
  the start, how long it waits between transmissions, how much history it keeps, and how
  the map chooses what to show. These values were once in many places in the code, as
  module constants and as a small number of ``config.toml`` keys with no documentation
  and no screen. Now they are in this module, with one place to change them (the
  Preferences page) and one file to keep them in.

Each :class:`PrefSpec` gives the group, the type, the bounds, and the **default** of a
preference. The default is the main purpose of this design. MeshTerm writes a preference
to disk only after it is changed from that default. Thus the file is a short list of the
places where you do not agree with the built-in behaviour. It is not a snapshot that pins
each value forever with no notice. If you delete a key (or use *Reset to defaults*), the
default of the code applies again.

The file is ``<config_dir>/preferences.toml``. MeshTerm writes it flat, in groups, with
comments, so that it reads the same as the page. It is TOML, the same as ``config.toml``
and the device-config backups. Thus each file that a person edits by hand uses one
syntax. Reads are tolerant: an unknown key, a malformed value, or a line that TOML
refuses costs only that one override. It never costs the rest of the file, and never
the session.

Two access paths, on purpose:

* :attr:`~meshterm.context.AppContext.preferences` is the explicit path. Each tool,
  service, and screen must use it.
* :func:`current` is for the render and session layer, which has no context to go
  through. This is the same reason why :func:`meshterm.platforms.get_platform` exists.
  The context installs itself there when it is built.
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

#: The groups that the page and the file show, in their order. This order is the order of
#: a session: what MeshTerm does to the radio when the link opens, what it transmits and
#: at which power, what it watches for, how it draws the world, what it keeps, and how it
#: paints. Nothing is in alphabetical order. A user who looks for "how long before I give
#: up on a message" must not have to know that it starts with a D.
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
    """Raised when MeshTerm cannot parse a preference value, or the value fails validation.

    The message is for the user. The CLI prints it, and the typed prompt of the editor
    shows it under the input as the reason for the refusal.
    """


@dataclass(frozen=True, slots=True)
class PrefSpec:
    """Specification for one preference.

    Attributes:
        key: The canonical key: the attribute name on :class:`Preferences`, and the key
            in the TOML file.
        label: The name for people, as the SETTING lane of the page shows it.
        help: A description of one line, as the DESCRIPTION lane of the page shows it.
        group: One of :data:`GROUPS`.
        value_type: ``"str" | "int" | "float" | "bool" | "enum"``.
        default: The built-in value. MeshTerm uses it when the file has no override.
        choices: For ``enum``: a mapping from each permitted value to its label.
        minimum: The inclusive lower bound for numeric types, if any.
        maximum: The inclusive upper bound for numeric types, if any.
        unit: A short suffix that MeshTerm adds when it formats the value (``"s"``,
            ``"dBm"``, ``"days"``). Thus a bare number in the VALUE lane still tells
            what it counts.
        relaunch: Whether the new value applies only at the next start. It is part of
            the description, instead of a mark of its own, because it is a caveat about
            one preference. It is not a status that the user looks for in a column.
        platforms: The :class:`~meshterm.platforms.Platform` names on which the page
            offers the preference, or ``None`` (the default) for all of them. Some
            preferences are about hardware that only one flavour has (the console font
            of the PicoCalc). On the desktop, such a row answers a question that the
            desktop cannot ask, so the page does not show it there. This applies only to
            the page. The key stays in the registry on each platform. Thus a file written
            on the handheld still parses on a desktop, and the desktop keeps its value and
            writes it back to the file. A machine that cannot act on the key does not
            silently remove it.
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
        """The text of the DESCRIPTION lane: the help, and the relaunch caveat if it applies."""
        return f"{self.help} (next launch)" if self.relaunch else self.help

    def offered_on(self, platform: str) -> bool:
        """Whether the editor draws a row for this preference on ``platform``.

        Args:
            platform: A :attr:`~meshterm.platforms.Platform.name`.

        Returns:
            ``True``, unless :attr:`platforms` names a set that does not include this
            platform.
        """
        return self.platforms is None or platform in self.platforms


#: The two forms in which the plain CLI can print a time. Their names tell what the user
#: sees, not the mechanism: an age is how long ago, and a timestamp is when.
_CLI_TIME_CHOICES: dict[str, str] = {"relative": "ages", "absolute": "timestamps"}

#: The choices for the silence rule. They come from the ring of the Watchtower itself, so
#: that the two lists always agree.
_SILENCE_CHOICES: dict[int, str] = {
    **{h: f"{h} h" for h in SILENCE_CHOICES_H},
    OFF: "off",
}

#: How long the air must stay quiet before the weekly flood advert goes out. The values
#: come from the choices of the scheduler itself, so that the two lists always agree.
_QUIET_CHOICES: dict[int, str] = {s: f"{s} s" for s in QUIET_CHOICES_S}

#: The ways to ask for the reclaim of the terminal width: follow the size probe of the
#: terminal, or overrule it in either direction. The description of the row asks a yes/no
#: question ("use the column ... ?"), so the values answer it in those words. They do not
#: name the mechanism a second time. Refer to
#: :func:`meshterm.ui.tui.session._reclaim_last_column` for the terminals that hide a
#: column, and for the cost when MeshTerm reclaims a column that is not hidden.
_WIDTH_CHOICES: dict[str, str] = {"auto": "auto", "yes": "yes", "no": "no"}

#: How many colours to send to the terminal. ``auto`` is the honest default, and it is the
#: only value that is not a depth. It means "take prompt_toolkit's verdict unless we can
#: positively establish better". This rule is the difference between a repaired host and a
#: broken one. The names of the other values tell the count that the user can see, not the
#: bit depth, because "24-bit" is a fact about the wire and "16 million" is a fact about
#: the screen. Refer to :func:`meshterm.ui.tui.session._color_depth` for the reason why
#: auto is not only "the most this terminal can do".
_COLOR_DEPTH_CHOICES: dict[str, str] = {
    "auto": "auto",
    "truecolor": "16 million",
    "256": "256",
    "16": "16",
}

#: What MeshTerm can offer when it starts in a console that cannot draw it. MeshTerm reads
#: this value only on the classic Windows console. That console is the only host with no
#: font fallback of its own.
_CONSOLE_SETUP_CHOICES: dict[str, str] = {
    "auto": "Automatic",
    "off": "Leave it alone",
}

#: The two console fonts that ``scripts/picocalc-lyra/calculinux-console-font-6x12.sh``
#: (and the 6x8 script next to it) build for the framebuffer display of the PicoCalc. The
#: name of each font tells its cell and the screen size that the cell gives. The reason:
#: here, the choice of a font is a choice of how much text fits, not a choice of a bitmap.
#: The names use plain ASCII ``x``, not ``×``. The multiplication sign is not one of the
#: 512 glyphs of the console font. If a name uses it, it draws as a tofu box on the only
#: platform that offers this preference. The value maps to a file in
#: :data:`meshterm.services.consolefont.FONT_FILES`, which is the correct place for a file
#: name. A path on one handheld is not a thing that a user chooses.
_CONSOLE_FONT_CHOICES: dict[str, str] = {"6x12": "6x12 (53x26)", "6x8": "6x8 (53x40)"}

#: What the log file keeps. The labels are the plain level names, the names that all other
#: tools use. A person who gets help with a problem hears "set it to debug", not "set it
#: to everything".
_LOG_LEVEL_CHOICES: dict[str, str] = {
    "ERROR": "Error",
    "WARNING": "Warning",
    "INFO": "Info",
    "DEBUG": "Debug",
}


#: All the preferences of MeshTerm, in page order. When you add one here, it gets a row on
#: the Preferences page, a key in the TOML file, a ``preferences get``/``set`` CLI face,
#: and a default. No other change is necessary. When a module already states a default as
#: its behaviour in code, this registry uses the name of that constant, and never types the
#: value again. Thus there is one source, and a change to it cannot make the constant and
#: the registry disagree about what MeshTerm does.
PREFERENCES: tuple[PrefSpec, ...] = (
    # --- Device ------------------------------------------------------------------
    PrefSpec(
        key="set_clock_on_connect",
        label="Set clock on connect",
        help="Set the device clock from this computer when it connects",
        group="Device",
        value_type="bool",
        # Off: only the owner can decide to write to a device when nobody asked for it.
        # The set is silent (the log shows it), and it occurs one time for each
        # connection. Refer to :mod:`meshterm.services.clock_sync`.
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
        minimum=0.1,  # a floor, not a switch: duty-cycle courtesy has no "off"
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
        minimum=5.0,  # each repeater in range relays one. Five seconds is the floor
        maximum=3600.0,
        unit="s",
    ),
    PrefSpec(
        key="weekly_flood_advert",
        label="Weekly advert",
        help="Flood an advert after a week without one",
        group="Sending",
        value_type="bool",
        # Off: a companion never advertises on its own. Thus this advert is the only one
        # that MeshTerm can send without a request, and each repeater in range relays it.
        # When the user sets it on, the week starts, and MeshTerm sends nothing at that
        # time. Refer to meshterm.services.advert_scheduler.
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
        default=0,  # one try: a resend is a second transmission on a shared mesh
        minimum=0,
        maximum=2,
    ),
    PrefSpec(
        key="room_relogin_minutes",
        label="Room re-login",
        help="Opening a room this quiet logs in again",
        group="Sending",
        value_type="int",
        # A login is one exchange, and a login restarts the catch-up of a room. After
        # three posts to a member get no acknowledgement, the room sends no more to that
        # member. When a room restarts, it also forgets each member that is not an admin.
        # A room that was heard in this time still sends, so when the user opens it
        # again, MeshTerm sends nothing.
        default=30,
        minimum=1,
        maximum=1440,
        unit="min",
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
        # Relative, so that a person at a prompt who asks "heard recently?" does not have
        # to subtract an ISO instant from `date` to find the answer. `--absolute`
        # overrides this value for one run. The JSON face is in UTC in all cases, because
        # a program reads it in a different place and often later, where a relative age
        # has no reference point.
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
        # Auto, because the count that a terminal accepts is a fact about the terminal,
        # and the user does not have to know it. MeshTerm reads this value only to raise
        # the verdict that prompt_toolkit already reached, never to lower it. A terminal
        # that MeshTerm cannot prove to do better keeps exactly what it had.
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
        # MeshTerm opens again in Windows Terminal without a question. The classic
        # console cannot draw one icon, with any font. Thus there is only one sensible
        # answer, and the user has not seen the app yet and cannot judge it. To install a
        # font, MeshTerm still asks, because that writes to the machine of the user. If
        # the user says no two times, MeshTerm writes "off" here, and that sets both off
        # permanently.
        default="auto",
        choices=_CONSOLE_SETUP_CHOICES,
    ),
    PrefSpec(
        key="console_font",
        label="Console font",
        help="Bigger text, or more rows on the handheld's panel",
        group="Display",
        value_type="enum",
        # The 6x12 Terminus derivative, because the handheld boots with this font, and
        # the row budget of each screen is designed for it (Platform.readable_rows is 26).
        # The 6x8 build has the same glyph inventory in a shorter cell. It gives fourteen
        # more rows. It is for a person who wants to see more of a list, and accepts text
        # that is less comfortable to read.
        default="6x12",
        choices=_CONSOLE_FONT_CHOICES,
        platforms=frozenset({"picocalc-lyra"}),
    ),
    # --- Diagnostics ---------------------------------------------------------------
    PrefSpec(
        key="log_level",
        label="Log detail",
        help="How much MeshTerm writes to its log file",
        group="Diagnostics",
        value_type="enum",
        # Problems, not a narration of a session that works. The file once kept all the
        # lines. Thus the approximately twelve useful lines were under twenty thousand
        # lines of no interest, and it was difficult to attach the file to a bug report.
        default="WARNING",
        choices=_LOG_LEVEL_CHOICES,
        relaunch=True,
    ),
)

_BY_KEY: dict[str, PrefSpec] = {spec.key: spec for spec in PREFERENCES}

#: All the keys that were once preferences and are not now. MeshTerm never uses a retired
#: key again. A value can stay in a file after its key is retired, if nobody saved the file
#: after that time. If the name comes back with a new meaning (seconds become minutes, or
#: a range moves), an old ``30`` that passes the new spec loads without an error and means
#: a different thing. No validation can catch that. Thus a preference that comes back with
#: a change comes back under a new name (a unit in the key makes this the natural step:
#: ``_s`` to ``_min``). ``tests/test_retired.py`` fails each registry that takes one of
#: these keys back. When MeshTerm loads the file, it refuses them completely.
RETIRED: frozenset[str] = frozenset(
    {
        # The background advert intervals for each device. They moved to Device config,
        # and later the weekly flood advert (``weekly_flood_advert``) replaced them.
        "advert_direct_hours",
        "advert_flood_hours",
    }
)

#: The reason that :attr:`Preferences.dropped` gives for a key in :data:`RETIRED`.
DROPPED_RETIRED = "retired"

#: The reason that :attr:`Preferences.dropped` gives for a key that the registry never had.
DROPPED_UNKNOWN = "unknown"


def get_spec(key: str) -> PrefSpec:
    """Find the spec of one preference.

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
    """All the preferences in groups for the page, in :data:`GROUPS` order.

    Args:
        platform: A :attr:`~meshterm.platforms.Platform.name` to draw the page for. It
            removes the preferences that the page does not offer on that platform (refer
            to :meth:`PrefSpec.offered_on`). ``None`` (the default, and the value that
            the file writer uses) keeps all the preferences. Thus an override made on one
            flavour stays when another flavour loads and saves the file.

    Returns:
        ``(group, specs)`` pairs. A group with no preferences is not included.
    """
    kept = [s for s in PREFERENCES if platform is None or s.offered_on(platform)]
    grouped = [(g, [s for s in kept if s.group == g]) for g in GROUPS]
    return [(g, specs) for g, specs in grouped if specs]


# --- value parsing / formatting -----------------------------------------------

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def parse_value(spec: PrefSpec, raw: Any) -> Any:
    """Parse and validate a raw value (a typed scalar, or text from the CLI, file, or prompt).

    Args:
        spec: The target preference.
        raw: The value to coerce.

    Returns:
        The typed value, with its range checked.

    Raises:
        PreferenceError: If the value has the wrong type, is out of range, or is not a
            choice.
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
            # A hand edit can answer a yes/no choice with a TOML boolean
            # (``full_width = true``). Then we get ``True`` where the choice is spelled
            # "yes". We put our own values in quotes when we write them, but MeshTerm must
            # understand a person who types the obvious thing. Thus map the boolean back
            # to the spelling that this choice offers.
            for choice in choices:
                if str(choice).lower() in (_TRUE if raw else _FALSE):
                    return choice
        # Text from the CLI or from a file edited by hand: match a choice by its own
        # spelling. Thus `preferences set watch_silence_hours 6` and a TOML `6` give the
        # same result.
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
    """Render the value of one preference for the screen.

    A boolean shows as ``on`` or ``off`` (the words of the button pair in the editor). An
    enum shows its own label. A number has its unit, so that a bare figure in the VALUE
    lane still tells what it counts.

    Args:
        spec: The preference of the value.
        value: The value to render.

    Returns:
        The string to show.
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
    """A muted "allowed values" hint for a typed prompt, or ``""`` when no bound applies."""
    unit = f" {spec.unit}" if spec.unit else ""
    if spec.minimum is not None and spec.maximum is not None:
        return f"Allowed: {spec.minimum:g} – {spec.maximum:g}{unit}"
    if spec.minimum is not None:
        return f"Allowed: >= {spec.minimum:g}{unit}"
    if spec.maximum is not None:
        return f"Allowed: <= {spec.maximum:g}{unit}"
    return ""


# --- the store ----------------------------------------------------------------

#: The name of the file in the config directory: one spelling for each place that opens it.
PREFERENCES_FILENAME = "preferences.toml"

#: The first comment in the file. It tells the one thing that a person who edits the file
#: by hand must know: a missing key means "use the default", so when you delete a line,
#: you undo a preference. It also refers to the two other types of setting, so that nobody
#: looks for the radio parameters of a device in this file.
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
    """Read a file that TOML refused as a document, one ``key = value`` line at a time.

    MeshTerm writes the file flat (no tables, and one entry on each line). Thus a line is
    a full entry, and the function can examine it alone. A line that TOML accepts keeps
    its typed value. A line that TOML refuses keeps its right side as text. That text is
    exactly what :func:`parse_value` takes from the command line. Thus the most probable
    hand edit (a word with no quotes, ``log_level = DEBUG``) is still understood. If the
    spec then refuses the value, :meth:`Preferences.update` removes it, the same as any
    other bad value.
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
    """The effective preferences: the built-in defaults, with the file overrides on top.

    Read a preference as an attribute (``preferences.trace_cooldown_s``). The attribute
    resolves through the registry. Thus a typing error raises :class:`AttributeError` at
    the call site, and does not silently read ``None``. :meth:`set` takes a raw value,
    validates it through the spec of the key, and stores it only while it is different
    from the default. :meth:`save` writes the result.

    Nothing in this class writes on its own. The Preferences page stages changes and saves
    them on *Apply*. That path and the CLI are the only paths to :meth:`save`.
    """

    def __init__(self, path: Path | None = None, values: Mapping[str, Any] | None = None) -> None:
        """Build the preferences from an optional file and an optional set of overrides.

        Args:
            path: Where :meth:`save` writes. ``None`` for a set in memory only (for
                tests, and for the fallback that :func:`current` returns before a context
                is built).
            values: The overrides to start from. The invalid ones are removed. A value
                that is equal to its default is not stored as an override.
        """
        self._path = path
        self._values: dict[str, Any] = {}
        self._dropped: dict[str, str] = {}
        if values:
            self._absorb(values)

    @classmethod
    def load(cls, path: Path) -> Preferences:
        """Read ``path``. Return all the defaults for a missing, empty, or unreadable file.

        TOML refuses a full document because of one bad line. But a hand edit must cost
        only its own line, not the file. Thus, when TOML refuses a document, this method
        reads it again one line at a time (:func:`_salvage`), and keeps each entry that
        still makes sense.

        Args:
            path: The TOML file to read.

        If the file has an entry that the registry cannot take, the next :meth:`save`
        does not keep it, because it writes only the overrides that apply. A retired key,
        a key that was never registered (most probably a typing error), and a value that
        its spec refuses are all left out. :attr:`dropped` tells which entries and why,
        so that the caller can log them after logging is configured (refer to
        :func:`report_dropped`).

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
        """Take in the entries of a file, and store in :attr:`dropped` each refused entry."""
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
        """What the load refused, key by key (a copy).

        The reason is :data:`DROPPED_RETIRED` or :data:`DROPPED_UNKNOWN`. For a key that
        is registered but whose value its spec refused, the reason is the refusal itself.
        """
        return dict(self._dropped)

    @property
    def path(self) -> Path | None:
        """Where :meth:`save` writes, or ``None`` for a set in memory only."""
        return self._path

    def __getattr__(self, name: str) -> Any:
        """Resolve a registered preference key as an attribute.

        Raises:
            AttributeError: For each name that is not a preference key. Thus
                ``preferences.trace_cooldwn_s``, with a typing error, fails where it is
                written, and does not silently read nothing.
        """
        if name.startswith("_") or name not in _BY_KEY:
            raise AttributeError(name)
        return self.get(name)

    def get(self, key: str) -> Any:
        """The effective value for ``key``: the override if there is one, else the default.

        Raises:
            PreferenceError: If ``key`` is not a registered preference.
        """
        spec = get_spec(key)
        return self._values.get(key, spec.default)

    def is_overridden(self, key: str) -> bool:
        """Whether ``key`` is now different from its built-in default."""
        return key in self._values

    def overrides(self) -> dict[str, Any]:
        """The values that are different from their defaults, in registry order (a copy)."""
        return {s.key: self._values[s.key] for s in PREFERENCES if s.key in self._values}

    def set(self, key: str, raw: Any) -> Any:
        """Validate ``raw`` for ``key`` and store it, or remove it when it is the default.

        An override is always a disagreement with the default. Thus, when you set a value
        back to its default, the entry is cleared, not pinned. Because of this, a later
        change to the default in the code gets to an install that never asked to be
        exempt.

        Args:
            key: The preference key.
            raw: The new value, typed or as text.

        Returns:
            The parsed value that now applies.

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
        """Apply several changes, and skip each one that the registry or a spec refuses.

        This method is tolerant on purpose. A file edited by hand comes in through this
        path, and one bad line must cost only that line, not the file.

        Args:
            values: ``key -> value`` pairs.

        Returns:
            The number of accepted changes.
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
        """Remove all the overrides, and return how many there were."""
        count = len(self._values)
        self._values.clear()
        return count

    def as_toml(self) -> str:
        """Render the current overrides as the file text: the header, then grouped entries.

        Each group that has an override gets its own comment heading. Each entry has its
        help line and its default above it. Thus the file reads the same as the page, and
        not as a bare map that a person must look up in a different place.
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
        """Write the overrides to :attr:`path` atomically.

        If a crash occurs during the write, the old file stays.

        Raises:
            RuntimeError: If this set was built without a path (a set in memory only).
        """
        if self._path is None:
            raise RuntimeError("these preferences have no file to save to")
        write_atomically(self._path, self.as_toml())


#: The set installed for this process. At the start, it is an instance with only the
#: defaults and no file. Thus the render layer works before (and without) an
#: :class:`~meshterm.context.AppContext`: in a specimen render, in a unit test, and in the
#: early boot of the CLI.
_current: Preferences = Preferences()


def report_dropped(preferences: Preferences, log: logging.Logger) -> None:
    """Log what the load of the file refused: the entries that its next save will leave out.

    A retired key is expected, because an older MeshTerm wrote it. Thus the function logs
    it at INFO. Any other entry was probably typed by hand, and it does not do what its
    author wanted. Thus it is a WARNING, and the line names the entry, so that the user
    can find the typing error.

    Args:
        preferences: A set built by :meth:`Preferences.load`.
        log: The logger to report to.
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
    """The preferences that apply for this process.

    This function is for the layers that have no context to go through: the renderer of
    the session, and the stores constructed without a context. It works the same as
    :func:`meshterm.platforms.get_platform`. Code that holds an
    :class:`~meshterm.context.AppContext` reads
    :attr:`~meshterm.context.AppContext.preferences` instead.
    """
    return _current


def install(preferences: Preferences) -> None:
    """Make ``preferences`` the set that :func:`current` returns (the context calls it at boot)."""
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
