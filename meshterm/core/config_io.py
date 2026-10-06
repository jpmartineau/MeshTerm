# SPDX-License-Identifier: Apache-2.0
"""TOML backup and restore for the device configuration.

A backup keeps the current value of each setting in the registry, the experimental custom
variables, and the configured channels. Thus you can archive the configuration of a
device, compare it with a diff, or copy it onto another node. The restore changes a
backup into the same list of operations that the ``config`` tool runs for live edits. The
restore can also show a dry-run diff against the current state of the device.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from .device_config import DEVICE_SETTINGS, get_spec, parse_value

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib


@dataclass(slots=True)
class Backup:
    """A parsed configuration backup.

    Attributes:
        settings: Registry setting key -> raw value.
        custom: Experimental custom variable name -> value.
        channels: One dict for each channel, with ``index``, ``name``, and a hex
            ``secret``.
    """

    settings: dict[str, Any] = field(default_factory=dict)
    custom: dict[str, str] = field(default_factory=dict)
    channels: list[dict[str, Any]] = field(default_factory=list)


def backup_config(
    path: Path,
    snapshot: dict,
    custom_vars: dict[str, str],
    channels: list[dict],
) -> Path:
    """Write the current configuration to a TOML file.

    Args:
        path: The path of the destination file. The function makes the parent
            directories.
        snapshot: A device snapshot (refer to ``device_config.build_snapshot``).
        custom_vars: The experimental custom variables.
        channels: Channel dicts (``channel_idx``, ``channel_name``, and
            ``channel_secret`` as raw bytes).

    Returns:
        The path that the function wrote.
    """
    settings: dict[str, Any] = {}
    for spec in DEVICE_SETTINGS:
        value = spec.getter(snapshot)
        if value is not None:
            settings[spec.key] = value

    doc: dict[str, Any] = {"settings": settings}
    if custom_vars:
        doc["custom"] = dict(custom_vars)
    if channels:
        doc["channels"] = [
            {
                "index": ch.get("channel_idx", idx),
                "name": ch.get("channel_name", ""),
                "secret": _to_hex(ch.get("channel_secret")),
            }
            for idx, ch in enumerate(channels)
        ]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(doc, fh)
    return path


def read_backup(path: Path) -> Backup:
    """Read and parse a TOML configuration backup.

    Args:
        path: The backup file to read.

    Returns:
        A :class:`Backup`.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
    """
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    return Backup(
        settings=dict(doc.get("settings") or {}),
        custom=dict(doc.get("custom") or {}),
        channels=list(doc.get("channels") or []),
    )


def plan_restore(
    backup: Backup,
    snapshot: dict,
    current_custom: dict[str, str],
) -> list[tuple]:
    """Diff a backup against the current state and return the operations to apply.

    The function returns only the values that are different from the current configuration
    of the device. Thus a restore (or its dry-run preview) shows exactly what will change.

    Args:
        backup: The parsed backup.
        snapshot: The current snapshot of the device.
        current_custom: The current custom variables of the device.

    Returns:
        A list of operation tuples that the ``config`` tool accepts:
        ``("set", key, value)``, ``("set_custom", key, value)``, and
        ``("set_channel", index, name, secret_bytes)``.

    Raises:
        DeviceConfigError: If a setting key or value in the backup is not valid.
    """
    ops: list[tuple] = []
    for key, raw in backup.settings.items():
        spec = get_spec(key)  # raises an error for an unknown key
        value = parse_value(spec, raw, snapshot)
        if value != spec.getter(snapshot):
            ops.append(("set", key, value))

    for key, value in backup.custom.items():
        if current_custom.get(key) != value:
            ops.append(("set_custom", key, str(value)))

    for ch in backup.channels:
        secret_hex = ch.get("secret") or ""
        secret = bytes.fromhex(secret_hex) if secret_hex else None
        ops.append(("set_channel", int(ch.get("index", 0)), str(ch.get("name", "")), secret))

    return ops


def _to_hex(secret: Any) -> str:
    """Render a channel secret (bytes or str) as a hex string."""
    if isinstance(secret, (bytes, bytearray)):
        return secret.hex()
    return str(secret or "")
