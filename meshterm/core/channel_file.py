# SPDX-License-Identifier: Apache-2.0
"""The channel file: export the channels of a device, and import them onto another device.

When the user changes to a new device, the channels must come too. The new device ships
with only ``Public``, and each other channel must be added again by hand: each private key
pasted, each ``#`` name typed, each send scope and mute set again. A channel file holds all
of these things, so that one import sets up the new device.

The file is TOML, with one ``[[channels]]`` table for each channel, in slot order:

.. code-block:: toml

    [[channels]]
    name = "Public"
    secret = "8b3387e9c5cdea6ac9e5edbaa115cd72"

    [[channels]]
    name = "#bots"          # the key comes from the name, so the file has no secret

    [[channels]]
    name = "Family"
    secret = "…32 hex digits…"
    scope = "yul"
    muted = true

The table has the same shape as the ``[[channels]]`` table of ``config backup``
(:mod:`meshterm.core.config_io`). Thus a config backup is also a channel file. Its
``index`` field is not read, because the order of the tables gives the order of the slots.

An import adds channels and puts them in order. It never removes a channel from the device.
A channel that is on the device and not in the file stays, after the channels of the file.
A private channel that MeshTerm removed would lose its key, if no other place has a copy of
that key. Thus a removal stays a decision that the user makes on each channel, with its own
confirm.

This module is pure logic: it reads and writes files, and it plans. The channel manager
(:mod:`meshterm.ui.channels`) and the ``channels`` CLI do the writes to the device.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import tomli_w

from .atomicwrite import write_atomically
from .channels import channel_identity, derive_secret, is_name_derived, normalize_secret
from .regions import RegionNameError
from .regions import validate as validate_region

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib


#: The comment at the top of an exported file. A user who opens the file finds what it is,
#: how to use it, and that it holds keys.
_HEADER = (
    "# MeshTerm channels. Import this file on the Channels page, or with\n"
    "# meshterm channels import --file <this file>\n"
    "# It holds the keys of private channels. Keep it private.\n\n"
)


class ChannelFileError(ValueError):
    """A channel file that MeshTerm cannot use: not readable, not TOML, or a bad entry."""


class SlotLike(Protocol):
    """A channel on the device, as the plan reads it.

    The device gives a :class:`~meshterm.core.channel_probe.ChannelSlot`.
    """

    idx: int
    name: str
    secret: bytes

    @property
    def identity(self) -> str:
        """The identity of the channel, which does not depend on the slot."""
        ...


@dataclass(frozen=True, slots=True)
class ChannelEntry:
    """One channel in a channel file.

    Attributes:
        name: The channel name.
        secret: The 16-byte key, or ``None`` when the key comes from the name (a ``#``
            channel). Then the device calculates the key itself.
        scope: The send scope of the channel, or ``None`` for the default scope.
        muted: Whether the notifications of the channel are muted.
    """

    name: str
    secret: bytes | None = None
    scope: str | None = None
    muted: bool = False

    @property
    def key(self) -> bytes:
        """The key of the channel: the stored key, or the key that comes from the name."""
        return self.secret if self.secret is not None else derive_secret(self.name)

    @property
    def identity(self) -> str:
        """The identity of the channel. It is the same value that the slot of the device gives."""
        return channel_identity(self.name, self.key)

    @property
    def write_secret(self) -> bytes | None:
        """The secret to send with a write: ``None`` when the device can derive the key.

        This is the same rule that a reorder uses (``_write_slot`` in
        :mod:`meshterm.ui.channels`).
        """
        return None if is_name_derived(self.name, self.key) else self.key


def entry_from_slot(
    slot: SlotLike, *, scope: str | None = None, muted: bool = False
) -> ChannelEntry:
    """The file entry for one channel of the device.

    Args:
        slot: The channel, as the device reports it.
        scope: The send scope of the channel (from the region store).
        muted: Whether the channel is muted (from the mute store).

    Returns:
        The entry. Its secret is ``None`` when the key comes from the name.
    """
    secret = None if is_name_derived(slot.name, slot.secret) else bytes(slot.secret)
    return ChannelEntry(name=slot.name, secret=secret, scope=scope, muted=muted)


def write_channel_file(path: Path, entries: Iterable[ChannelEntry]) -> Path:
    """Write the channels to a TOML file, in the order of ``entries``.

    The file holds the keys of private channels. Thus the write limits the file to its
    owner, where the platform lets it (refer to
    :func:`~meshterm.core.atomicwrite.write_atomically`).

    Args:
        path: The destination file. The function makes the parent directories.
        entries: The channels to write.

    Returns:
        The path that the function wrote.
    """
    tables: list[dict[str, Any]] = []
    for entry in entries:
        table: dict[str, Any] = {"name": entry.name}
        if entry.secret is not None:
            table["secret"] = entry.secret.hex()
        if entry.scope:
            table["scope"] = entry.scope
        if entry.muted:
            table["muted"] = True
        tables.append(table)
    write_atomically(path, _HEADER + tomli_w.dumps({"channels": tables}), owner_only=True)
    return path


def read_channel_file(path: Path) -> list[ChannelEntry]:
    """Read a channel file, and check each entry.

    The function removes a second copy of a channel (the same identity), and keeps the
    first copy. A field that the function does not know (the ``index`` of a config backup)
    is not read.

    Args:
        path: The file to read.

    Returns:
        The channels of the file, in file order.

    Raises:
        ChannelFileError: If the file cannot be read, is not TOML, has no channels, or has
            an entry that is not valid. The message names the entry.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ChannelFileError(f"no such file: {path}") from None
    except OSError as exc:
        raise ChannelFileError(f"cannot read {path}: {exc.strerror or exc}") from None
    try:
        doc = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ChannelFileError(f"{path.name} is not a TOML file: {exc}") from None
    tables = doc.get("channels")
    if not isinstance(tables, list) or not tables:
        raise ChannelFileError(f"{path.name} has no [[channels]] table")
    entries: list[ChannelEntry] = []
    seen: set[str] = set()
    for number, table in enumerate(tables, start=1):
        entry = _entry(number, table)
        if entry.identity not in seen:
            seen.add(entry.identity)
            entries.append(entry)
    return entries


def _entry(number: int, table: object) -> ChannelEntry:
    """Check one ``[[channels]]`` table, and make its entry.

    Raises:
        ChannelFileError: If the table is not valid. The message names the entry by its
            number (from 1) and by its name, when it has one.
    """
    if not isinstance(table, dict):
        raise ChannelFileError(f"channel {number} is not a table")
    name = table.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ChannelFileError(f"channel {number} has no name")
    name = name.strip()
    where = f"channel {number} ({name})"
    raw_secret = table.get("secret")
    secret: bytes | None = None
    if raw_secret not in (None, ""):
        if not isinstance(raw_secret, str):
            raise ChannelFileError(f"{where}: the secret must be a string of hex digits")
        try:
            secret = normalize_secret(raw_secret)
        except ValueError as exc:
            raise ChannelFileError(f"{where}: {exc}") from None
        if is_name_derived(name, secret):
            secret = None  # the device derives it. A config backup writes it for each channel
    elif not name.startswith("#"):
        # Only a # name gives its key. Any other name with no secret is a private channel
        # whose key is not in the file, and no device can join it.
        raise ChannelFileError(f"{where}: a private channel needs its secret")
    scope = table.get("scope")
    if scope not in (None, ""):
        if not isinstance(scope, str):
            raise ChannelFileError(f"{where}: the scope must be a region name")
        try:
            scope = validate_region(scope)
        except RegionNameError as exc:
            raise ChannelFileError(f"{where}: {exc}") from None
    else:
        scope = None
    muted = table.get("muted", False)
    if not isinstance(muted, bool):
        raise ChannelFileError(f"{where}: muted must be true or false")
    return ChannelEntry(name=name, secret=secret, scope=scope, muted=muted)


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """What an import does to one device.

    Attributes:
        writes: The slot writes, in order. Each is ``(slot, name, secret)``, with the
            arguments of ``Device.set_channel``. An empty name clears a slot whose channel
            moved to a lower slot.
        added: The channels of the file that the device does not have, and that fit, each
            with the slot that the plan writes it to.
        skipped: The channels of the file that the device does not have, and that do not
            fit, because each slot is full.
        present: The channels of the file that the device already has.
        layout: Each channel on the device after the import, in slot order, as
            ``(slot, name, action)``. The action is ``add``, ``move``, or ``keep``.
    """

    writes: tuple[tuple[int, str, bytes | None], ...]
    added: tuple[tuple[int, ChannelEntry], ...]
    skipped: tuple[ChannelEntry, ...]
    present: tuple[ChannelEntry, ...]
    layout: tuple[tuple[int, str, str], ...]

    @property
    def moved(self) -> int:
        """The number of channels that are already on the device and change slot."""
        return sum(1 for _, _, action in self.layout if action == "move")

    @property
    def changes_device(self) -> bool:
        """Whether the import writes anything to the device."""
        return bool(self.writes)


def plan_import(
    entries: Sequence[ChannelEntry], slots: Sequence[SlotLike], capacity: int
) -> ImportPlan:
    """Plan the import of a channel file onto a device.

    The device gets this order: first the channels of the file, in file order, then each
    channel that the device has and the file does not have, in its current order. The order
    fills the slots from slot 0. The plan writes only a slot whose channel changes. When the
    device has gaps (for example, channels in slots 0, 1, and 5), the channels move down to
    close them, and the plan clears each higher slot whose channel moved down. Otherwise the
    device holds that channel two times.

    The plan never removes a channel, so the device keeps each channel that it had. A file
    channel that does not fit (each slot is full) is in ``skipped``. The plan keeps the
    first channels of the file and skips the last ones.

    Args:
        entries: The channels of the file, in file order, with no second copies (as
            :func:`read_channel_file` returns them).
        slots: The channels of the device, as a complete probe reads them.
        capacity: The number of channel slots of the device.

    Returns:
        The plan.
    """
    current = sorted(slots, key=lambda s: s.idx)
    on_device = {s.identity for s in current}
    in_file = {e.identity for e in entries}
    present = [e for e in entries if e.identity in on_device]
    new = [e for e in entries if e.identity not in on_device]
    room = max(0, capacity - len(current))
    added, skipped = new[:room], new[room:]
    placed = {e.identity for e in present} | {e.identity for e in added}

    # The order to write: (identity, name, secret to send) for each slot from 0.
    order: list[tuple[str, str, bytes | None]] = [
        (e.identity, e.name, e.write_secret) for e in entries if e.identity in placed
    ]
    order += [
        (s.identity, s.name, None if is_name_derived(s.name, s.secret) else bytes(s.secret))
        for s in current
        if s.identity not in in_file
    ]

    occupant = {s.idx: s.identity for s in current}
    writes: list[tuple[int, str, bytes | None]] = [
        (idx, name, secret)
        for idx, (identity, name, secret) in enumerate(order)
        if occupant.get(idx) != identity
    ]
    # Each channel of the device is in ``order``. Thus a channel in a slot past the end of
    # ``order`` is now also in a lower slot, and this copy must go.
    writes += [(s.idx, "", None) for s in current if s.idx >= len(order)]
    new_slot = {identity: idx for idx, (identity, _, _) in enumerate(order)}
    old_slot = {s.identity: s.idx for s in current}
    layout = tuple(
        (
            idx,
            name,
            "add" if identity not in old_slot else "keep" if old_slot[identity] == idx else "move",
        )
        for idx, (identity, name, _) in enumerate(order)
    )
    return ImportPlan(
        writes=tuple(writes),
        added=tuple((new_slot[e.identity], e) for e in added),
        skipped=tuple(skipped),
        present=tuple(present),
        layout=layout,
    )


class _Scopes(Protocol):
    def channel_scope(self, channel_id: str | None) -> str | None: ...
    def set_channel_scope(self, channel_id: str, name: str | None) -> None: ...


class _Mutes(Protocol):
    def is_muted(self, channel_id: str | None) -> bool: ...
    def set_muted(self, channel_id: str, muted: bool) -> None: ...


def apply_preferences(
    entries: Iterable[ChannelEntry], scopes: _Scopes | None, mutes: _Mutes | None
) -> int:
    """Set the send scope and the mute that the file gives each channel.

    MeshTerm keeps these two values, not the device (refer to
    :class:`~meshterm.core.region_store.RegionStore` and
    :class:`~meshterm.core.mute_store.MuteStore`). Their key is the identity of the
    channel, so they apply to the channel in any slot, on any device. The function only
    sets what the file gives. A channel with no scope or no mute in the file keeps the
    values that this computer has for it, because an import adds and never removes.

    Args:
        entries: The channels whose values to set.
        scopes: The region store, or ``None``.
        mutes: The mute store, or ``None``.

    Returns:
        The number of values that changed.
    """
    changed = 0
    for entry in entries:
        if entry.scope and scopes is not None:
            if scopes.channel_scope(entry.identity) != entry.scope:
                scopes.set_channel_scope(entry.identity, entry.scope)
                changed += 1
        if entry.muted and mutes is not None and not mutes.is_muted(entry.identity):
            mutes.set_muted(entry.identity, True)
            changed += 1
    return changed
