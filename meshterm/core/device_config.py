# SPDX-License-Identifier: Apache-2.0
"""Declarative registry of all the device settings that MeshTerm can change.

Each :class:`SettingSpec` describes one setting that the user can change. It tells how to
read the current value from a device snapshot, how to parse, validate, and format the
value, and how to apply the value to the :class:`~meshterm.core.connection.Device`. The
same registry controls the interactive editor, the ``config`` CLI subcommands, and the
TOML backup and restore. When you add a setting one time, it shows in all these places.
This is the same pattern as the tool registry.

Some settings have a protocol command that takes several fields together (radio,
coordinates, tuning, telemetry modes). For these settings, the apply builds the full
command again from the current snapshot and the one changed field. Thus the user can
edit one setting alone.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import regions
from .connection import Device, repeat_freq_allowed

# The display categories, in the order that the editor and `config show` show them.
CATEGORIES = ("Identity", "Radio", "Tuning", "Behavior", "Experimental")


class DeviceConfigError(ValueError):
    """Raised when a value cannot be parsed, or when it is not valid.

    The message is for the user (the CLI and the editor print it directly).
    """


@dataclass(slots=True)
class SettingSpec:
    """The specification for one setting.

    Attributes:
        key: The canonical key (the same as the ``SELF_INFO`` field name, if there is
            one).
        label: The name that the user sees.
        help: A description on one line.
        category: One of :data:`CATEGORIES`.
        value_type: ``"str" | "int" | "float" | "bool" | "enum"``.
        choices: For ``enum``, a map from each valid int value to its label.
        minimum: The inclusive lower limit for numeric types, if there is one.
        maximum: The inclusive upper limit for numeric types, if there is one.
        strict_choices: When ``True`` (the default), an ``enum`` value must be one of
            :attr:`choices`. When ``False``, the choices are a menu for convenience, but
            the spec accepts all integers in the range. Use it for firmware fields for
            which we do not list all the possible values.
        max_key: The snapshot key that holds the inclusive maximum of this setting, as
            the device reports it (for example, ``max_tx_power`` is the limit for
            ``tx_power``). :func:`parse_value` checks it when it gets a snapshot. This
            makes the static :attr:`maximum` smaller, to the value that the connected
            hardware supports.
        max_length: For ``str`` values, the maximum length of an accepted string (the
            widths of protocol fields, for example the 31-byte name slot of the flood
            scope).
        decimals: For ``float`` values, the fixed number of decimal places. The row shows
            the value with this number of places, and the parse rounds the value to it.
            Thus the applied value is the value that the row showed. ``None`` keeps the
            value as it is given.
        validate: A last rule for a typed value in the range. It gets the snapshot when
            there is one. It returns the complaint, or ``None``. Use it for a rule that a
            limit cannot give: for example, the PIN's "zero or six digits", or the relay
            only on a frequency that the firmware accepts.
        getter: Gets the current value from a snapshot dict.
        apply: A coroutine that applies a parsed value to a device. It gets the snapshot.
    """

    key: str
    label: str
    help: str
    category: str
    value_type: str
    getter: Callable[[dict], Any]
    apply: Callable[[Device, Any, dict], Awaitable[None]]
    choices: dict[int, str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    strict_choices: bool = True
    max_key: str | None = None
    max_length: int | None = None
    decimals: int | None = None
    validate: Callable[[Any, dict | None], str | None] | None = None


# --- parse and format a value ------------------------------------------------

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def parse_value(spec: SettingSpec, raw: Any, snapshot: dict | None = None) -> Any:
    """Parse and validate a raw value (usually a CLI or TOML string) for ``spec``.

    Args:
        spec: The target setting.
        raw: The raw value to convert (a string, or a scalar that has a type already).
        snapshot: An optional device snapshot. If it is given and the spec names a
            :attr:`~SettingSpec.max_key`, the maximum that the device reports there makes
            the static limit smaller (for example, the TX power is limited to the maximum
            of this board).

    Returns:
        The typed value, with its range checked, ready for :meth:`SettingSpec.apply`.

    Raises:
        DeviceConfigError: If the value has the wrong type, is out of range, or breaks
            the :attr:`~SettingSpec.validate` rule of the setting.
    """
    value = _parse_typed(spec, raw, snapshot)
    if spec.validate is not None:
        complaint = spec.validate(value, snapshot)
        if complaint:
            raise DeviceConfigError(f"{spec.key}: {complaint}")
    return value


def _parse_typed(spec: SettingSpec, raw: Any, snapshot: dict | None) -> Any:
    """:func:`parse_value` without the rule of the setting itself: type, limits, choices."""
    text = str(raw).strip()
    if spec.value_type == "str":
        # `config show` prints an empty string as the two characters `""`, because a key
        # with no text after it looks like a truncated line, not like an empty value. A
        # value that the dump prints must be a value that `set` accepts (CLAUDE.md, "A
        # value must round-trip"). Thus the quotes are removed here. If they stay, a dump
        # that you give back as input replaces each empty setting with a pair of
        # quotation marks, and nothing tells you. The next dump looks the same, thus you
        # never know.
        if text == '""':
            text = ""
        if spec.max_length is not None and len(text) > spec.max_length:
            raise DeviceConfigError(
                f"{spec.key}: must be at most {spec.max_length} characters, got {len(text)}"
            )
        return text
    if spec.value_type == "bool":
        low = text.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise DeviceConfigError(f"{spec.key}: expected a boolean, got {raw!r}")

    # Numeric (int / enum / float): the try wraps only the conversion. Thus this handler
    # does not catch and wrap again a DeviceConfigError from the range and enum checks
    # below.
    try:
        if spec.value_type == "float":
            value: Any = float(text)
            if spec.decimals is not None:
                value = round(value, spec.decimals)
        else:  # int or enum
            value = int(text, 0) if isinstance(raw, str) else int(raw)
    except (ValueError, TypeError) as exc:
        raise DeviceConfigError(f"{spec.key}: invalid {spec.value_type} value {raw!r}") from exc

    if (
        spec.value_type == "enum"
        and spec.strict_choices
        and spec.choices is not None
        and value not in spec.choices
    ):
        allowed = ", ".join(str(k) for k in spec.choices)
        raise DeviceConfigError(f"{spec.key}: must be one of {allowed}, got {value}")
    if spec.minimum is not None and value < spec.minimum:
        raise DeviceConfigError(f"{spec.key}: must be >= {spec.minimum}, got {value}")
    maximum = effective_maximum(spec, snapshot)
    if maximum is not None and value > maximum:
        raise DeviceConfigError(f"{spec.key}: must be <= {maximum:g}, got {value}")
    return value


def effective_maximum(spec: SettingSpec, snapshot: dict | None) -> float | None:
    """The inclusive maximum for ``spec``: the device value if it is known, else the static one.

    Args:
        spec: The setting.
        snapshot: The device snapshot from which the function reads the reported maximum
            (can be ``None``).

    Returns:
        The smaller of the static limit of the spec and the ``max_key`` value of the
        snapshot, or ``None`` when neither exists.
    """
    maximum = spec.maximum
    if snapshot is not None and spec.max_key is not None:
        reported = snapshot.get(spec.max_key)
        if reported is not None:
            try:
                reported_f = float(reported)
            except (TypeError, ValueError):
                return maximum
            maximum = reported_f if maximum is None else min(maximum, reported_f)
    return maximum


def format_value(spec: SettingSpec, value: Any) -> str:
    """Render a value to show it.

    Args:
        spec: The setting of the value.
        value: The current value (can be ``None`` if it is not known).

    Returns:
        A string that a person can read.
    """
    if value is None:
        return "?"
    if spec.value_type == "str" and value == "":
        return "(not set)"
    if spec.value_type == "bool":
        return "true" if value else "false"
    if spec.value_type == "enum" and spec.choices is not None and value in spec.choices:
        return f"{value} ({spec.choices[value]})"
    if spec.value_type == "float" and spec.decimals is not None:
        return f"{float(value):.{spec.decimals}f}"
    return str(value)


# --- snapshot ----------------------------------------------------------------


async def build_snapshot(
    device: Device,
    *,
    self_info: dict | None = None,
    path_hash_mode: int | None = None,
) -> dict:
    """Read all the current settings of a device into one dict.

    The function merges ``SELF_INFO`` with the tuning parameters and the path-hash mode,
    which it reads separately. If a firmware does not support an optional read, the
    function skips that read and tells nothing. Thus the remaining part of the snapshot
    still renders.

    Args:
        device: A connected device.
        self_info: A ``SELF_INFO`` dict that was read already, to use again instead of a
            new request to the device. A menu caller gives the devstate session cache
            here, and this saves a round trip each time that a screen opens. Omit it to
            read the live values (a new read after a restore or a reset must not trust a
            cache).
        path_hash_mode: A path-hash mode that was read already, to use again with the
            same conditions.

    Returns:
        A dict with the keys of ``SELF_INFO``, and also ``rx_delay``,
        ``airtime_factor``, ``path_hash_mode``, ``autoadd_config``,
        ``autoadd_max_hops``, and ``flood_scope``. Below these, it has the facts of the
        device-query frame (``ble_pin``, ``model``, client ``repeat``). If the firmware
        can relay, the dict also has its ``repeat_freqs``.
    """
    snapshot = dict(self_info if self_info is not None else await device.get_self_info())
    try:
        snapshot.update(await device.get_tuning())
    except Exception:  # noqa: BLE001 - an optional read. Its absence is acceptable.
        pass
    if path_hash_mode is not None:
        snapshot["path_hash_mode"] = path_hash_mode
    else:
        try:
            snapshot["path_hash_mode"] = await device.get_path_hash_mode()
        except Exception:  # noqa: BLE001 - an optional read. Its absence is acceptable.
            pass
    try:
        snapshot["autoadd_config"] = await device.get_autoadd_config()
    except Exception:  # noqa: BLE001 - an optional read. Its absence is acceptable.
        pass
    try:
        hops = await device.get_autoadd_max_hops()
    except Exception:  # noqa: BLE001 - an optional read. Its absence is acceptable.
        hops = None
    if hops is not None:
        snapshot["autoadd_max_hops"] = hops
    try:
        snapshot["flood_scope"] = await device.get_default_flood_scope()
    except Exception:  # noqa: BLE001 - an optional read. Its absence is acceptable.
        pass
    # The device-query frame is a payload that is different from SELF_INFO, and some
    # settings are only there. One of them is the BLE pairing code: real firmware reports
    # it as ``ble_pin``, and never puts it in SELF_INFO. Without this read, the Device PIN
    # row can only render "?" on hardware. The user can write the PIN, but cannot read
    # back what was written. The frame goes below SELF_INFO in the merge. Thus, if
    # SELF_INFO also has a key, the SELF_INFO value stays (``probe_device`` uses the same
    # priority when it merges the two).
    try:
        snapshot = {**await device.get_device_info(), **snapshot}
    except Exception:  # noqa: BLE001 - an optional read. Its absence is acceptable.
        pass
    # Client repeat (firmware v9+) can be switched on only where the firmware lets it. The
    # ranges cost one more round trip. Thus MeshTerm asks for them only from firmware that
    # can relay.
    if "repeat" in snapshot:
        try:
            freqs = await device.get_allowed_repeat_freqs()
        except Exception:  # noqa: BLE001 - an optional read. Its absence is acceptable.
            freqs = []
        if freqs:
            snapshot["repeat_freqs"] = [tuple(pair) for pair in freqs]
    return snapshot


# --- coupled-command apply helpers -------------------------------------------


# Several settings do not have their own command. The firmware takes latitude and
# longitude together, and the four radio parameters together. Thus, to change one of them,
# MeshTerm must send the related fields again with no change. MeshTerm reads these related
# fields from ``snapshot``, which is the image of the device that the caller has. The
# caller reads this image only one time, before the first write. Thus each of these
# appliers writes the value that it set into the snapshot. The reason is a bug that
# occurred on a real device. A backup had a different latitude and a different longitude.
# Its restore applied ``adv_lat`` (and kept the old longitude), then ``adv_lon`` (and kept
# the old latitude). Thus the second write cancelled the first, and nothing showed it. The
# same occurred for a restore that changed two radio fields. Because each applier writes
# its value back, each later related field in the same batch is correct. This is true
# when the batch comes from ``config restore``, and when it comes from the staged changes
# of the editor.


def _radio_apply(field_name: str) -> Callable[[Device, Any, dict], Awaitable[None]]:
    """Build an apply that updates one radio field, and keeps the other fields."""

    async def apply(device: Device, value: Any, snapshot: dict) -> None:
        params = {
            "freq": snapshot.get("radio_freq"),
            "bw": snapshot.get("radio_bw"),
            "sf": snapshot.get("radio_sf"),
            "cr": snapshot.get("radio_cr"),
        }
        params[field_name] = value
        # Client repeat goes in the same command, and the firmware reads its absence as
        # off. Thus send it again. If not, a change of the radio settings stops the relay
        # of the node, and nothing shows it.
        await device.set_radio(
            params["freq"],
            params["bw"],
            params["sf"],
            params["cr"],
            repeat=snapshot.get("repeat"),
        )
        snapshot[f"radio_{field_name}"] = value

    return apply


def _coords_apply(field_name: str) -> Callable[[Device, Any, dict], Awaitable[None]]:
    """Build an apply that updates one coordinate, and keeps the other."""

    async def apply(device: Device, value: Any, snapshot: dict) -> None:
        lat = value if field_name == "adv_lat" else snapshot.get("adv_lat", 0.0)
        lon = value if field_name == "adv_lon" else snapshot.get("adv_lon", 0.0)
        await device.set_coords(float(lat or 0.0), float(lon or 0.0))
        snapshot[field_name] = value

    return apply


def _tuning_apply(field_name: str) -> Callable[[Device, Any, dict], Awaitable[None]]:
    """Build an apply that updates one tuning field, and keeps the other."""

    async def apply(device: Device, value: Any, snapshot: dict) -> None:
        rx = value if field_name == "rx_delay" else snapshot.get("rx_delay", 0.0)
        af = value if field_name == "airtime_factor" else snapshot.get("airtime_factor", 0.0)
        await device.set_tuning(float(rx or 0.0), float(af or 0.0))
        snapshot[field_name] = value

    return apply


def _telemetry_apply(field_name: str) -> Callable[[Device, Any, dict], Awaitable[None]]:
    """Build an apply that updates one telemetry mode, and keeps the others."""

    async def apply(device: Device, value: Any, snapshot: dict) -> None:
        base = (
            value if field_name == "telemetry_mode_base" else snapshot.get("telemetry_mode_base", 0)
        )
        loc = value if field_name == "telemetry_mode_loc" else snapshot.get("telemetry_mode_loc", 0)
        env = value if field_name == "telemetry_mode_env" else snapshot.get("telemetry_mode_env", 0)
        await device.set_telemetry_modes(int(base or 0), int(loc or 0), int(env or 0))

    return apply


async def _client_repeat_apply(device: Device, value: Any, snapshot: dict) -> None:
    """Switch client repeat, and send again the four radio fields that go with it."""
    radio = [snapshot.get(key) for key in ("radio_freq", "radio_bw", "radio_sf", "radio_cr")]
    if any(field is None for field in radio):
        raise DeviceConfigError(
            "client_repeat: the radio settings were never read, so they can't be restated"
        )
    await device.set_radio(*radio, repeat=bool(value))
    snapshot["repeat"] = bool(value)


def _flood_scope_valid(value: Any, snapshot: dict | None) -> str | None:
    """Refuse a default scope name that the firmware refuses (refer to :func:`regions.validate`).

    ``max_length`` counts characters, but the firmware counts UTF-8 bytes. Also, a
    repeater can never list back a name that has a space or a comma in it. An empty value
    is valid: it clears the scope.
    """
    if not str(value or "").strip():
        return None
    try:
        regions.validate(str(value))
    except regions.RegionNameError as exc:
        return str(exc)
    return None


async def _autoadd_hops_apply(device: Device, value: Any, snapshot: dict) -> None:
    """Set the auto-add hop limit, and send again the bitmask that comes before it."""
    flags = snapshot.get("autoadd_config")
    if flags is None:
        raise DeviceConfigError(
            "autoadd_max_hops: the auto-add bitmask was never read, so it can't be restated"
        )
    await device.set_autoadd_config(int(flags), max_hops=int(value))
    snapshot["autoadd_max_hops"] = value


def _pin_rule(value: Any, snapshot: dict | None) -> str | None:
    """The firmware accepts only a pairing PIN of zero (no PIN) or of exactly six digits."""
    if value == 0 or 100000 <= value <= 999999:
        return None
    return f"must be 0 (no PIN) or six digits, got {value}"


def _repeat_rule(value: Any, snapshot: dict | None) -> str | None:
    """The relay goes on only at a frequency that the firmware lets it use (if both are known)."""
    if not value or not snapshot:
        return None
    ranges, freq = snapshot.get("repeat_freqs"), snapshot.get("radio_freq")
    if not ranges or freq is None or repeat_freq_allowed(freq, ranges):
        return None
    allowed = ", ".join(f"{low:g}" if low == high else f"{low:g}–{high:g}" for low, high in ranges)
    return f"this firmware relays only on {allowed} MHz, not {float(freq):g}"


def _get(key: str) -> Callable[[dict], Any]:
    """Build a getter that reads ``key`` from a snapshot."""
    return lambda snapshot: snapshot.get(key)


# TELEM_MODE_DENY / TELEM_MODE_ALLOW_FLAGS / TELEM_MODE_ALLOW_ALL of the firmware: nobody,
# the contacts that have the telemetry permission flag set, or all who ask. There is no
# mode 3. The labels are short, because the widest value sets the width of the value lane
# of the editor for all rows.
_TELEMETRY_CHOICES = {0: "deny", 1: "by contact", 2: "allow all"}

# adv_loc_policy / multi_acks are single firmware bytes. We list the values that we know
# from real use, but the choices are not strict. Thus a value that we do not know is still
# accepted.
_ADV_LOC_CHOICES = {0: "off", 1: "on"}
_MULTI_ACKS_CHOICES = {0: "off", 1: "on"}
# path_hash_mode is a 2-bit field. The hash size for each hop is mode + 1 bytes. The field
# has space for a mode 3, but CMD_SET_PATH_HASH_MODE refuses it (``cmd_frame[2] >= 3``).
_PATH_HASH_CHOICES = {
    0: "1-byte hashes (default)",
    1: "2-byte hashes",
    2: "3-byte hashes",
}


def _telemetry_spec(key: str, label: str) -> SettingSpec:
    """Build a telemetry-mode setting spec (the same shape for base, loc, and env)."""
    return SettingSpec(
        key=key,
        label=label,
        help="Who may read this node's sensor readings",
        category="Behavior",
        value_type="enum",
        choices=_TELEMETRY_CHOICES,
        minimum=0,
        maximum=2,
        getter=_get(key),
        apply=_telemetry_apply(key),
    )


# --- radio presets -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RadioPreset:
    """A named, standard set of radio parameters that MeshTerm applies as one unit.

    Attributes:
        name: The name of the preset, spelled exactly as the MeshCore list spells it.
            Thus you can compare this list with the phone app or the web flasher.
        freq: The carrier frequency in MHz.
        bw: The channel bandwidth in kHz.
        sf: The LoRa spreading factor.
        cr: The denominator of the LoRa coding rate (5-8 = 4/5-4/8).
        path_hash_size: The path-hash size for each hop in bytes, if the preset names one
            (some areas use 2-byte hashes). ``None`` does not change that setting.
    """

    name: str
    freq: float
    bw: float
    sf: int
    cr: int
    path_hash_size: int | None = None

    @property
    def summary(self) -> str:
        """The four parameters on one line, in the order that the MeshCore list prints them."""
        return f"{self.freq:.3f} SF{self.sf} BW{self.bw:g} CR{self.cr}"

    def as_settings(self) -> dict[str, Any]:
        """Return this preset as a ``{setting_key: value}`` map that the editor can stage."""
        values: dict[str, Any] = {
            "radio_freq": self.freq,
            "radio_bw": self.bw,
            "radio_sf": self.sf,
            "radio_cr": self.cr,
        }
        if self.path_hash_size is not None:
            # The firmware stores the mode, which is one less than the size for each hop
            # (refer to _PATH_HASH_CHOICES). The config GUI of MeshCore converts it in the
            # same way.
            values["path_hash_mode"] = self.path_hash_size - 1
        return values


# The radio settings that MeshCore suggests, copied exactly (names, order, and values)
# from the list at ``https://api.meshcore.nz/api/v1/config``
# (``config.suggested_radio_settings.entries``). The MeshCore apps, the web flasher, and
# config.meshcore.dev all read this list. We downloaded it on 2026-09-04. The community
# maintains the presets, and they change. During 2025, most areas changed from the
# original 250 kHz / SF11 modulation to a "narrow" 62.5 kHz modulation. For this reason,
# MeshCore still lists the old settings with its own "(Deprecated)" names: some nodes did
# not change their radio settings, and they still use them.
#
# Each node on a mesh must have the same four parameters. Thus the only useful preset is
# the preset that the local mesh uses. Some areas also name a path-hash size. The preset
# stages it with the radio fields, exactly as the MeshCore GUI does.
RADIO_PRESETS: list[RadioPreset] = [
    RadioPreset("Australia", 915.800, 250.0, 10, 5),
    RadioPreset("Australia (Narrow)", 916.575, 62.5, 7, 8),
    RadioPreset("Australia (Mid)", 915.075, 125.0, 9, 5),
    RadioPreset("Australia: SA, WA", 923.125, 62.5, 8, 8),
    RadioPreset("Australia: QLD", 923.125, 62.5, 8, 5),
    RadioPreset("Brazil", 923.125, 62.5, 8, 8),
    RadioPreset("Costa Rica", 910.525, 125.0, 11, 5),
    RadioPreset("EU/UK (Narrow)", 869.618, 62.5, 8, 8),
    RadioPreset("EU/UK (Deprecated)", 869.525, 250.0, 11, 5),
    RadioPreset("Czech Republic (Narrow)", 869.432, 62.5, 7, 5),
    RadioPreset("EU 433MHz (Long Range)", 433.650, 250.0, 11, 5),
    RadioPreset("EU 433MHz (Narrow)", 433.650, 62.5, 8, 8),
    RadioPreset("Hungary", 869.618, 62.5, 7, 5, 2),
    RadioPreset("Netherlands", 869.618, 62.5, 7, 5),
    RadioPreset("New Zealand (Narrow)", 917.375, 62.5, 7, 5, 2),
    RadioPreset("New Zealand (Gisborne)", 917.375, 250.0, 11, 5, 1),
    RadioPreset("Portugal 433", 433.375, 62.5, 9, 6),
    RadioPreset("Portugal 868", 869.618, 62.5, 7, 6),
    RadioPreset("Slovakia", 869.618, 62.5, 7, 5, 2),
    RadioPreset("Switzerland", 869.618, 62.5, 8, 8),
    RadioPreset("USA/Canada (Recommended)", 910.525, 62.5, 7, 5),
    RadioPreset("Vietnam (Narrow)", 920.250, 62.5, 8, 5),
    RadioPreset("Vietnam (Deprecated)", 920.250, 250.0, 11, 5),
]

#: Frequencies are set in steps of 1 kHz. Thus a snapshot matches a preset when it is
#: within half a step.
_FREQ_TOLERANCE_MHZ = 0.0005


def current_preset(snapshot: dict[str, Any]) -> RadioPreset | None:
    """Return the preset that the radio is tuned to, or ``None`` if it matches no preset.

    The match uses only the four radio parameters, which must be the same on each node of
    the mesh. It never uses the path-hash size that some presets also have. The config
    GUI of MeshCore answers "which of these am I on?" in the same way.

    Args:
        snapshot: A device snapshot (or a snapshot with staged values merged over it).
    """
    for preset in RADIO_PRESETS:
        freq, bw = snapshot.get("radio_freq"), snapshot.get("radio_bw")
        if (
            isinstance(freq, (int, float))
            and isinstance(bw, (int, float))
            and abs(float(freq) - preset.freq) < _FREQ_TOLERANCE_MHZ
            and float(bw) == preset.bw
            and snapshot.get("radio_sf") == preset.sf
            and snapshot.get("radio_cr") == preset.cr
        ):
            return preset
    return None


# --- the registry ------------------------------------------------------------

DEVICE_SETTINGS: list[SettingSpec] = [
    # Identity
    SettingSpec(
        "name",
        "Node name",
        "The name other nodes see",
        "Identity",
        "str",
        max_length=31,  # the 32-byte name field of the firmware, with its terminator
        getter=_get("name"),
        apply=lambda d, v, s: d.set_name(v),
    ),
    SettingSpec(
        "adv_lat",
        "Latitude",
        "How far north or south this node says it is",
        "Identity",
        "float",
        minimum=-90.0,
        maximum=90.0,
        getter=_get("adv_lat"),
        apply=_coords_apply("adv_lat"),
    ),
    SettingSpec(
        "adv_lon",
        "Longitude",
        "How far east or west this node says it is",
        "Identity",
        "float",
        minimum=-180.0,
        maximum=180.0,
        getter=_get("adv_lon"),
        apply=_coords_apply("adv_lon"),
    ),
    SettingSpec(
        # MeshTerm writes it as ``device_pin`` (the name that the CLI, the backup TOML, and
        # ``set_devicepin`` use), but reads it as ``ble_pin``. This is the name that the
        # firmware uses in the device-query frame, which is the only place where it
        # occurs. Because we keep the key, old backups still restore. Because we read the
        # name of the firmware, the row shows a value instead of "?" on real hardware.
        # The fallback lets the canonical key work for all sources that report it
        # directly.
        "device_pin",
        "Device PIN",
        "Code a phone needs to pair over Bluetooth",
        "Identity",
        "int",
        minimum=0,
        maximum=999999,
        validate=_pin_rule,
        getter=lambda snapshot: snapshot.get("ble_pin", snapshot.get("device_pin")),
        apply=lambda d, v, s: d.set_device_pin(v),
    ),
    # Radio
    SettingSpec(
        "radio_freq",
        "Frequency (MHz)",
        "Must match every other node on your mesh",
        "Radio",
        "float",
        # The limits that CMD_SET_RADIO_PARAMS itself applies (150–2500 MHz, and 7–500 kHz
        # below).
        minimum=150.0,
        maximum=2500.0,
        getter=_get("radio_freq"),
        apply=_radio_apply("freq"),
    ),
    SettingSpec(
        "radio_bw",
        "Bandwidth (kHz)",
        "Wider is faster, narrower reaches further",
        "Radio",
        "float",
        minimum=7.0,
        maximum=500.0,
        getter=_get("radio_bw"),
        apply=_radio_apply("bw"),
    ),
    SettingSpec(
        "radio_sf",
        "Spreading factor",
        "Higher reaches further but sends slower",
        "Radio",
        "int",
        minimum=5,
        maximum=12,
        getter=_get("radio_sf"),
        apply=_radio_apply("sf"),
    ),
    SettingSpec(
        "radio_cr",
        "Coding rate",
        "Higher survives noise better, sends slower",
        "Radio",
        "int",
        minimum=5,
        maximum=8,
        getter=_get("radio_cr"),
        apply=_radio_apply("cr"),
    ),
    SettingSpec(
        "tx_power",
        "TX power (dBm)",
        "How loud this radio transmits",
        "Radio",
        "int",
        minimum=-9,  # the minimum of CMD_SET_RADIO_TX_POWER. The board sets the maximum
        maximum=30,
        max_key="max_tx_power",
        getter=_get("tx_power"),
        apply=lambda d, v, s: d.set_tx_power(v),
    ),
    SettingSpec(
        # Client repeat (firmware v9+): the companion relays mesh traffic as a repeater
        # does. The device-query frame reports it as ``repeat``. MeshTerm writes it as the
        # optional last byte of the radio command. Thus it sends the radio settings again,
        # and each radio change sends it again (refer to _radio_apply). The firmware lets
        # it work only on some frequencies.
        "client_repeat",
        "Repeat",
        "Relay mesh traffic; allowed frequencies only",
        "Radio",
        "bool",
        getter=_get("repeat"),
        apply=_client_repeat_apply,
        validate=_repeat_rule,
    ),
    # Tuning. Both are firmware floats, sent over the wire ×1000. The ranges are the
    # constrain() limits of the firmware itself. (The TX delay factors of a repeater are
    # not here: companion firmware ignores them. They are remote-CLI settings on
    # repeaters.)
    SettingSpec(
        "airtime_factor",
        "Airtime factor",
        "Caps how much of the air we use; 0 = no cap",
        "Tuning",
        "float",
        minimum=0.0,
        maximum=9.0,
        getter=_get("airtime_factor"),
        apply=_tuning_apply("airtime_factor"),
    ),
    SettingSpec(
        "rx_delay",
        "RX delay",
        "Wait this long before acting on what we hear",
        "Tuning",
        "float",
        minimum=0.0,
        maximum=20.0,
        decimals=1,
        getter=_get("rx_delay"),
        apply=_tuning_apply("rx_delay"),
    ),
    # Behavior
    SettingSpec(
        "manual_add_contacts",
        "Add contacts by hand",
        "Only save a node when you say so",
        "Behavior",
        "bool",
        getter=_get("manual_add_contacts"),
        apply=lambda d, v, s: d.set_manual_add_contacts(v),
    ),
    SettingSpec(
        "autoadd_config",
        "Auto-add contacts",
        "Which kinds of node get saved on their own; 0 = none",
        "Behavior",
        "int",
        minimum=0,
        maximum=255,
        getter=_get("autoadd_config"),
        apply=lambda d, v, s: d.set_autoadd_config(v),
    ),
    SettingSpec(
        # The firmware compares it with the path hash count of an advert: 0 is no limit, 1
        # is only direct neighbours, N accepts a maximum of N-1 hops. The maximum is 64.
        "autoadd_max_hops",
        "Auto-add max hops",
        "Only auto-add nodes this near; 1 = direct, 0 = any",
        "Behavior",
        "int",
        minimum=0,
        maximum=64,
        getter=_get("autoadd_max_hops"),
        apply=_autoadd_hops_apply,
    ),
    SettingSpec(
        "flood_scope",
        "Flood scope",
        "Keep floods in one region; empty reaches everyone",
        "Behavior",
        "str",
        max_length=30,
        validate=_flood_scope_valid,
        getter=_get("flood_scope"),
        apply=lambda d, v, s: d.set_default_flood_scope(v),
    ),
    SettingSpec(
        "adv_loc_policy",
        "Share location",
        "Tell other nodes where this one is",
        "Behavior",
        "enum",
        choices=_ADV_LOC_CHOICES,
        minimum=0,
        strict_choices=False,
        getter=_get("adv_loc_policy"),
        apply=lambda d, v, s: d.set_adv_loc_policy(v),
    ),
    SettingSpec(
        "multi_acks",
        "Multi-acks",
        "Send extra receipts so replies get through",
        "Behavior",
        "enum",
        choices=_MULTI_ACKS_CHOICES,
        minimum=0,
        strict_choices=False,
        getter=_get("multi_acks"),
        apply=lambda d, v, s: d.set_multi_acks(v),
    ),
    _telemetry_spec("telemetry_mode_base", "Telemetry mode (base)"),
    _telemetry_spec("telemetry_mode_loc", "Telemetry mode (location)"),
    _telemetry_spec("telemetry_mode_env", "Telemetry mode (environment)"),
    # Experimental
    SettingSpec(
        "path_hash_mode",
        "Path-hash mode",
        "Longer hop ids mix up fewer nodes, but cost space",
        "Experimental",
        "enum",
        choices=_PATH_HASH_CHOICES,
        minimum=0,
        maximum=2,
        getter=_get("path_hash_mode"),
        apply=lambda d, v, s: d.set_path_hash_mode(v),
    ),
]

_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in DEVICE_SETTINGS}

#: All the keys that were device settings before and are not now. There are none yet. A
#: retired key is never used again, for the reason that
#: :data:`meshterm.core.preferences.RETIRED` gives. Here the risk is larger: the settings
#: store keeps values to restore them to the radio. If a key comes back with a different
#: meaning, the store offers to write the number of the old value into the new setting.
#: ``tests/test_retired.py`` fails a registry that uses a retired key again.
RETIRED: frozenset[str] = frozenset()


def is_setting_key(key: str) -> bool:
    """Whether ``key`` names a device setting in the registry (and not a retired one)."""
    return key in _BY_KEY


def get_spec(key: str) -> SettingSpec:
    """Return the spec for ``key``.

    Args:
        key: A setting key.

    Returns:
        The matching :class:`SettingSpec`.

    Raises:
        DeviceConfigError: If there is no such setting.
    """
    spec = _BY_KEY.get(key)
    if spec is None:
        known = ", ".join(sorted(_BY_KEY))
        raise DeviceConfigError(f"unknown setting {key!r}. Known settings: {known}")
    return spec


def settings_by_category() -> list[tuple[str, list[SettingSpec]]]:
    """Return the settings in groups by category, in display order.

    Returns:
        A list of ``(category, specs)`` pairs, in the order of :data:`CATEGORIES`.
    """
    return [(cat, [s for s in DEVICE_SETTINGS if s.category == cat]) for cat in CATEGORIES]
