# SPDX-License-Identifier: Apache-2.0
"""Declarative registry of every settable device configuration value.

Each :class:`SettingSpec` describes one tunable setting: how to read its current value
from a device snapshot, how to parse/validate/format it, and how to apply it back to the
:class:`~meshterm.core.connection.Device`. The same registry drives the interactive
editor, the ``config`` CLI subcommands, and TOML backup/restore — add a setting once and
it appears everywhere, mirroring the tool registry pattern.

Settings whose protocol command takes several fields at once (radio, coordinates, tuning,
telemetry modes) rebuild the full command from the current snapshot plus the one changed
field, so a single setting can be edited in isolation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .connection import Device, repeat_freq_allowed

# Display categories, in the order the editor and `config show` present them.
CATEGORIES = ("Identity", "Radio", "Tuning", "Behavior", "Experimental")


class DeviceConfigError(ValueError):
    """Raised when a value cannot be parsed or fails validation.

    The message is user-facing (printed directly by the CLI and editor).
    """


@dataclass(slots=True)
class SettingSpec:
    """Specification for one settable configuration value.

    Attributes:
        key: Canonical key (matches the ``SELF_INFO`` field name where one exists).
        label: Human-friendly name for display.
        help: One-line description.
        category: One of :data:`CATEGORIES`.
        value_type: ``"str" | "int" | "float" | "bool" | "enum"``.
        choices: For ``enum``, a mapping of allowed int value to label.
        minimum: Inclusive lower bound for numeric types, if any.
        maximum: Inclusive upper bound for numeric types, if any.
        strict_choices: When ``True`` (the default) an ``enum`` value must be one of
            :attr:`choices`. When ``False`` the choices are offered as a convenience menu
            but any in-range integer is still accepted — used for firmware fields whose
            full value domain we don't enumerate exhaustively.
        max_key: Snapshot key holding this setting's *device-reported* inclusive maximum
            (e.g. ``max_tx_power`` bounding ``tx_power``). Checked by :func:`parse_value`
            when it is given a snapshot, tightening the static :attr:`maximum` to what
            the connected hardware actually supports.
        max_length: For ``str`` values, the longest accepted string (protocol field
            widths, e.g. the flood scope's 31-byte name slot).
        decimals: For ``float`` values, the fixed decimal places a value is shown with and
            rounded to on parse, so what is applied is what the row showed; ``None`` keeps
            the value as given.
        validate: A last rule for a typed, in-range value, given the snapshot when there
            is one: it returns the complaint, or ``None``. For what a bound can't state —
            the PIN's "zero or six digits", relaying only on an allowed frequency.
        getter: Extracts the current value from a snapshot dict.
        apply: Coroutine applying a parsed value to a device, given the snapshot.
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


# --- value parsing / formatting ----------------------------------------------

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def parse_value(spec: SettingSpec, raw: Any, snapshot: dict | None = None) -> Any:
    """Parse and validate a raw value (typically a CLI/TOML string) for ``spec``.

    Args:
        spec: The target setting.
        raw: The raw value to coerce (string or already-typed scalar).
        snapshot: Optional device snapshot; when given and the spec names a
            :attr:`~SettingSpec.max_key`, the device-reported maximum found there
            tightens the static bound (e.g. TX power capped at this board's max).

    Returns:
        The typed, range-checked value ready for :meth:`SettingSpec.apply`.

    Raises:
        DeviceConfigError: If the value is the wrong type, out of range, or breaks the
            setting's own :attr:`~SettingSpec.validate` rule.
    """
    value = _parse_typed(spec, raw, snapshot)
    if spec.validate is not None:
        complaint = spec.validate(value, snapshot)
        if complaint:
            raise DeviceConfigError(f"{spec.key}: {complaint}")
    return value


def _parse_typed(spec: SettingSpec, raw: Any, snapshot: dict | None) -> Any:
    """:func:`parse_value` short of the setting's own rule: the type, bounds and choices."""
    text = str(raw).strip()
    if spec.value_type == "str":
        # `config show` prints an empty string as the two characters `""`, because a key
        # with nothing after it reads as a truncated line rather than as an empty value.
        # A value the dump prints has to be one `set` takes back (CLAUDE.md, "A value must
        # round-trip"), so the quotes come off here — otherwise feeding a dump back in
        # silently replaces every empty setting with a pair of quote marks, and the next
        # dump looks identical, so nothing ever tells you.
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

    # Numeric (int / enum / float): wrap only the conversion, so a DeviceConfigError
    # raised by the range/enum checks below is not caught and rewrapped here.
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
    """The inclusive maximum for ``spec``: the device-reported one when known, else static.

    Args:
        spec: The setting.
        snapshot: The device snapshot the reported maximum is read from (may be ``None``).

    Returns:
        The tighter of the spec's static bound and the snapshot's ``max_key`` value, or
        ``None`` when neither exists.
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
    """Render a value for display.

    Args:
        spec: The setting the value belongs to.
        value: The current value (may be ``None`` if unknown).

    Returns:
        A human-readable string.
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
    """Read a device's full current configuration into one dict.

    Merges ``SELF_INFO`` with the separately-read tuning parameters and path-hash mode.
    Optional reads that a given firmware does not support are skipped silently so the rest
    of the snapshot still renders.

    Args:
        device: A connected device.
        self_info: An already-read ``SELF_INFO`` dict to reuse instead of asking the
            radio again — a menu caller passes the devstate session cache here, saving
            a round-trip on every screen open. Omit to read live (a refresh after a
            restore or reset must not trust any cache).
        path_hash_mode: An already-read path-hash mode to reuse, on the same terms.

    Returns:
        A dict keyed like ``SELF_INFO`` plus ``rx_delay``, ``airtime_factor``,
        ``path_hash_mode``, ``autoadd_config``, ``autoadd_max_hops`` and ``flood_scope``,
        with the device-query frame's facts (``ble_pin``, ``model``, client ``repeat``)
        underneath and, where the firmware relays at all, its ``repeat_freqs``.
    """
    snapshot = dict(self_info if self_info is not None else await device.get_self_info())
    try:
        snapshot.update(await device.get_tuning())
    except Exception:  # noqa: BLE001 - optional read; absence is acceptable
        pass
    if path_hash_mode is not None:
        snapshot["path_hash_mode"] = path_hash_mode
    else:
        try:
            snapshot["path_hash_mode"] = await device.get_path_hash_mode()
        except Exception:  # noqa: BLE001 - optional read; absence is acceptable
            pass
    try:
        snapshot["autoadd_config"] = await device.get_autoadd_config()
    except Exception:  # noqa: BLE001 - optional read; absence is acceptable
        pass
    try:
        hops = await device.get_autoadd_max_hops()
    except Exception:  # noqa: BLE001 - optional read; absence is acceptable
        hops = None
    if hops is not None:
        snapshot["autoadd_max_hops"] = hops
    try:
        snapshot["flood_scope"] = await device.get_default_flood_scope()
    except Exception:  # noqa: BLE001 - optional read; absence is acceptable
        pass
    # The device-query frame is a *different* payload from SELF_INFO, and some settings
    # only exist there — the BLE pairing code among them, which real firmware reports as
    # ``ble_pin`` and never puts in SELF_INFO. Without this the Device PIN row could only
    # ever render "?" on hardware: writable, but with no way to read back what you wrote.
    # Merged underneath, so a key SELF_INFO also carries keeps the SELF_INFO value (the
    # same precedence ``probe_device`` uses when it folds the two together).
    try:
        snapshot = {**await device.get_device_info(), **snapshot}
    except Exception:  # noqa: BLE001 - optional read; absence is acceptable
        pass
    # Client repeat (firmware v9+) may only be switched on where the firmware allows. The
    # ranges are one more round trip, so they are asked only of firmware that can relay.
    if "repeat" in snapshot:
        try:
            freqs = await device.get_allowed_repeat_freqs()
        except Exception:  # noqa: BLE001 - optional read; absence is acceptable
            freqs = []
        if freqs:
            snapshot["repeat_freqs"] = [tuple(pair) for pair in freqs]
    return snapshot


# --- coupled-command apply helpers -------------------------------------------


# Several settings do not have a command of their own: the firmware takes latitude and
# longitude together, and the four radio parameters together, so changing one means
# re-sending its siblings unchanged. Those siblings are read from ``snapshot``, which is
# the caller's picture of the device — and that picture is read *once*, before the first
# write. So each of these appliers records what it just set, and the reason is a bug that
# reached a real device: restoring a backup that differed in both latitude and longitude
# applied ``adv_lat`` (preserving the stale longitude), then ``adv_lon`` (preserving the
# stale *latitude*) — and the second write silently undid the first. Same for a restore
# touching two radio fields. Writing back keeps every later sibling in the same batch
# honest, whether the batch comes from ``config restore`` or the editor's staged changes.


def _radio_apply(field_name: str) -> Callable[[Device, Any, dict], Awaitable[None]]:
    """Build an apply that updates one radio field, preserving the others."""

    async def apply(device: Device, value: Any, snapshot: dict) -> None:
        params = {
            "freq": snapshot.get("radio_freq"),
            "bw": snapshot.get("radio_bw"),
            "sf": snapshot.get("radio_sf"),
            "cr": snapshot.get("radio_cr"),
        }
        params[field_name] = value
        # Client repeat rides the same command, and the firmware reads its absence as off:
        # restate it, or retuning the radio quietly stops the node relaying.
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
    """Build an apply that updates one coordinate, preserving the other."""

    async def apply(device: Device, value: Any, snapshot: dict) -> None:
        lat = value if field_name == "adv_lat" else snapshot.get("adv_lat", 0.0)
        lon = value if field_name == "adv_lon" else snapshot.get("adv_lon", 0.0)
        await device.set_coords(float(lat or 0.0), float(lon or 0.0))
        snapshot[field_name] = value

    return apply


def _tuning_apply(field_name: str) -> Callable[[Device, Any, dict], Awaitable[None]]:
    """Build an apply that updates one tuning field, preserving the other."""

    async def apply(device: Device, value: Any, snapshot: dict) -> None:
        rx = value if field_name == "rx_delay" else snapshot.get("rx_delay", 0.0)
        af = value if field_name == "airtime_factor" else snapshot.get("airtime_factor", 0.0)
        await device.set_tuning(float(rx or 0.0), float(af or 0.0))
        snapshot[field_name] = value

    return apply


def _telemetry_apply(field_name: str) -> Callable[[Device, Any, dict], Awaitable[None]]:
    """Build an apply that updates one telemetry mode, preserving the others."""

    async def apply(device: Device, value: Any, snapshot: dict) -> None:
        base = (
            value if field_name == "telemetry_mode_base" else snapshot.get("telemetry_mode_base", 0)
        )
        loc = value if field_name == "telemetry_mode_loc" else snapshot.get("telemetry_mode_loc", 0)
        env = value if field_name == "telemetry_mode_env" else snapshot.get("telemetry_mode_env", 0)
        await device.set_telemetry_modes(int(base or 0), int(loc or 0), int(env or 0))

    return apply


async def _client_repeat_apply(device: Device, value: Any, snapshot: dict) -> None:
    """Switch client repeat, restating the four radio fields it travels with."""
    radio = [snapshot.get(key) for key in ("radio_freq", "radio_bw", "radio_sf", "radio_cr")]
    if any(field is None for field in radio):
        raise DeviceConfigError(
            "client_repeat: the radio settings were never read, so they can't be restated"
        )
    await device.set_radio(*radio, repeat=bool(value))
    snapshot["repeat"] = bool(value)


async def _autoadd_hops_apply(device: Device, value: Any, snapshot: dict) -> None:
    """Set the auto-add hop limit, restating the bitmask it travels behind."""
    flags = snapshot.get("autoadd_config")
    if flags is None:
        raise DeviceConfigError(
            "autoadd_max_hops: the auto-add bitmask was never read, so it can't be restated"
        )
    await device.set_autoadd_config(int(flags), max_hops=int(value))
    snapshot["autoadd_max_hops"] = value


def _pin_rule(value: Any, snapshot: dict | None) -> str | None:
    """The firmware takes a pairing PIN of zero (none) or exactly six digits, nothing else."""
    if value == 0 or 100000 <= value <= 999999:
        return None
    return f"must be 0 (no PIN) or six digits, got {value}"


def _repeat_rule(value: Any, snapshot: dict | None) -> str | None:
    """Relaying goes on only at a frequency the firmware allows it (when both are known)."""
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


# The firmware's TELEM_MODE_DENY / TELEM_MODE_ALLOW_FLAGS / TELEM_MODE_ALLOW_ALL: nobody, the
# contacts whose telemetry permission flag is set, or anyone who asks. There is no mode 3.
# The labels stay short because the widest value sizes the editor's value lane for every row.
_TELEMETRY_CHOICES = {0: "deny", 1: "by contact", 2: "allow all"}

# adv_loc_policy / multi_acks are single firmware bytes; we list the values seen in the
# wild but keep them non-strict so an unfamiliar value is still accepted.
_ADV_LOC_CHOICES = {0: "off", 1: "on"}
_MULTI_ACKS_CHOICES = {0: "off", 1: "on"}
# path_hash_mode is a 2-bit field; the hash size carried per hop is mode + 1 bytes. The field
# has room for a mode 3, but CMD_SET_PATH_HASH_MODE refuses it (``cmd_frame[2] >= 3``).
_PATH_HASH_CHOICES = {
    0: "1-byte hashes (default)",
    1: "2-byte hashes",
    2: "3-byte hashes",
}


def _telemetry_spec(key: str, label: str) -> SettingSpec:
    """Build a telemetry-mode setting spec (shared shape for base/loc/env)."""
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
    """A named, standard set of radio parameters applied as one unit.

    Attributes:
        name: The preset's name, spelled exactly as MeshCore's own list spells it, so
            this list can be read against the phone app or the web flasher.
        freq: Carrier frequency in MHz.
        bw: Channel bandwidth in kHz.
        sf: LoRa spreading factor.
        cr: LoRa coding-rate denominator (5-8 = 4/5-4/8).
        path_hash_size: Per-hop path-hash size in bytes where the preset names one (a
            few regions run 2-byte hashes); ``None`` leaves that setting where it is.
    """

    name: str
    freq: float
    bw: float
    sf: int
    cr: int
    path_hash_size: int | None = None

    @property
    def summary(self) -> str:
        """The four parameters on one line, in the order MeshCore's own list prints."""
        return f"{self.freq:.3f} SF{self.sf} BW{self.bw:g} CR{self.cr}"

    def as_settings(self) -> dict[str, Any]:
        """Return this preset as a ``{setting_key: value}`` mapping for staging."""
        values: dict[str, Any] = {
            "radio_freq": self.freq,
            "radio_bw": self.bw,
            "radio_sf": self.sf,
            "radio_cr": self.cr,
        }
        if self.path_hash_size is not None:
            # The firmware stores the *mode*, one less than the size it carries per hop
            # (see _PATH_HASH_CHOICES); MeshCore's own config GUI converts it the same way.
            values["path_hash_mode"] = self.path_hash_size - 1
        return values


# MeshCore's suggested radio settings, mirrored verbatim — names, order and values — from
# the list its apps, the web flasher and config.meshcore.dev all read at
# ``https://api.meshcore.nz/api/v1/config`` (``config.suggested_radio_settings.entries``),
# fetched 2026-09-04. The presets are community-maintained and they move: through 2025
# most regions left the original 250 kHz / SF11 modulation for a "narrow" 62.5 kHz one,
# which is why MeshCore still lists the settings it superseded under its own
# "(Deprecated)" names — nodes that never re-tuned are still out there on them.
#
# Every node on a mesh must match all four parameters, so the only useful preset is the
# one the local mesh actually runs. A handful of regions also name a path-hash size,
# which the preset stages alongside the radio fields exactly as MeshCore's GUI does.
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

#: Frequencies are set in kHz steps, so a snapshot matches a preset within half of one.
_FREQ_TOLERANCE_MHZ = 0.0005


def current_preset(snapshot: dict[str, Any]) -> RadioPreset | None:
    """Return the preset the radio is tuned to, or ``None`` where it matches none.

    Matched on the four radio parameters alone — the ones every node on the mesh has to
    agree on — never on the path-hash size a few presets also carry, which is how
    MeshCore's own config GUI reads "which of these am I on?".

    Args:
        snapshot: A device snapshot (or one with staged values merged over it).
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
        max_length=31,  # the firmware's 32-byte name field, its terminator included
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
        # Written as ``device_pin`` (the name the CLI, the backup TOML and ``set_devicepin``
        # all use) but *read* as ``ble_pin``, which is what the firmware calls it in the
        # device-query frame — the only place it appears. Keeping the key means existing
        # backups still restore; reading the firmware's own name means the row shows a
        # value instead of "?" on real hardware. The fallback keeps the canonical key
        # working for anything that reports it directly.
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
        # The bounds CMD_SET_RADIO_PARAMS itself enforces (150–2500 MHz, 7–500 kHz below).
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
        minimum=-9,  # CMD_SET_RADIO_TX_POWER's floor; the ceiling is the board's own
        maximum=30,
        max_key="max_tx_power",
        getter=_get("tx_power"),
        apply=lambda d, v, s: d.set_tx_power(v),
    ),
    SettingSpec(
        # Client repeat (firmware v9+): the companion relays mesh traffic like a repeater.
        # Reported in the device-query frame as ``repeat`` and written as the optional last
        # byte of the radio command, so it restates the radio and every radio change
        # restates it (see _radio_apply). The firmware allows it only on a few frequencies.
        "client_repeat",
        "Repeat",
        "Relay mesh traffic; allowed frequencies only",
        "Radio",
        "bool",
        getter=_get("repeat"),
        apply=_client_repeat_apply,
        validate=_repeat_rule,
    ),
    # Tuning. Both are firmware floats moved over the wire ×1000; the ranges are the
    # firmware's own constrain() bounds. (The repeater-side TX delay factors are *not*
    # here: companion firmware ignores them — they are remote-CLI settings on repeaters.)
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
        # The firmware compares it with an advert's path hash count: 0 is no limit, 1 is
        # direct neighbours only, N admits up to N-1 hops. Capped at 64.
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
        "Keep traffic to one group; empty reaches everyone",
        "Behavior",
        "str",
        max_length=30,
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

#: Every key that was once a device setting and no longer is — none yet. A retired key is
#: never reused, for the reason :data:`meshterm.core.preferences.RETIRED` gives, and with
#: more at stake here: the settings store remembers values to *restore onto the radio*, so a
#: key that came back meaning something else would offer to write the old value's number
#: into the new setting. ``tests/test_retired.py`` fails a registry that takes one back.
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
        DeviceConfigError: If no such setting exists.
    """
    spec = _BY_KEY.get(key)
    if spec is None:
        known = ", ".join(sorted(_BY_KEY))
        raise DeviceConfigError(f"unknown setting {key!r}. Known settings: {known}")
    return spec


def settings_by_category() -> list[tuple[str, list[SettingSpec]]]:
    """Return settings grouped by category in display order.

    Returns:
        A list of ``(category, specs)`` pairs following :data:`CATEGORIES` order.
    """
    return [(cat, [s for s in DEVICE_SETTINGS if s.category == cat]) for cat in CATEGORIES]
