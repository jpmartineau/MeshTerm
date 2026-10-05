# SPDX-License-Identifier: Apache-2.0
"""Persistence for the rooms this machine has joined: each room's password and our access.

A room server has two passwords. The *admin* password is the one Repeater admin already
remembers (:mod:`meshterm.core.admin_store`), and it gets you into a room as its admin; the
*room* password is the one its owner hands out to members, and it is what this store keeps.
Joining a room is logging in with one of them, so a room is "joined" here exactly when
MeshTerm holds a password it can log in with — and opening the room again logs in with it
silently instead of asking.

An admin login's password is never written here, only the access it earned: the admin
password lives in one place, where Repeater admin can change it, so a room never logs in
with a copy that has gone stale. A room whose owner cleared the room password is open to
anyone, and its remembered password is the empty string — a real answer, not an absence,
which is why :meth:`RoomStore.password` returns ``None`` for "nothing remembered".

Like the admin passwords, this is global machine state in a small JSON file
(``<config_dir>/rooms.json``), written owner-only because it holds passwords in plaintext.
The file is read into :class:`RoomMembership` records and written back out of them, so a
field no record knows is gone after the next write (see :data:`RoomMembership.RETIRED`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .admin_store import admin_key
from .atomicwrite import write_atomically
from .models import Contact, LoginResult, RoomAccess, RoomLogin, utcnow


@dataclass(slots=True)
class RoomMembership:
    """What we remember about one room we have joined.

    Attributes:
        key: The :func:`~meshterm.core.admin_store.admin_key` the room is stored under —
            the same key its admin password has, so the two stores agree on which room is
            which (the record's key in the file, not one of its fields).
        password: The room password that got us in, ``""`` for a room open to anyone, or
            ``None`` when we got in as its admin (that password lives in the admin store).
        access: The access the room granted at our last login: a
            :class:`~meshterm.core.models.RoomAccess` value.
        label: The room's name, for display.
        last_login: ISO-8601 timestamp of the last accepted login.
    """

    key: str
    password: str | None
    access: str
    label: str
    last_login: str

    #: Fields this record once held and must never hold again under another meaning (see
    #: :data:`meshterm.core.preferences.RETIRED` for why a name is never reused).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, key: str, raw: object) -> RoomMembership | None:
        """Build a record from one file entry, or ``None`` when it is not one we can use."""
        if not isinstance(raw, dict):
            return None
        password = raw.get("password")
        access = raw.get("access")
        if access not in {a.value for a in RoomAccess}:
            return None
        return cls(
            key=key,
            password=None if password is None else str(password),
            access=str(access),
            label=str(raw.get("label") or ""),
            last_login=str(raw.get("last_login") or ""),
        )

    def to_json(self) -> dict:
        """The record as written: its own fields and nothing it happened to be read with."""
        return {
            "password": self.password,
            "access": self.access,
            "label": self.label,
            "last_login": self.last_login,
        }


class RoomStore:
    """Reads and writes the rooms this machine has joined."""

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on the first join).
        """
        self._path = path

    def _load(self) -> dict[str, RoomMembership]:
        """Read every membership, keyed by room; empty on a missing or corrupt file."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        records = {}
        for key, raw in data.items():
            record = RoomMembership.read(str(key), raw)
            if record is not None:
                records[record.key] = record
        return records

    def membership(self, room: Contact) -> RoomMembership | None:
        """What we remember about ``room``, or ``None`` if we never joined it."""
        return self._load().get(admin_key(room))

    def password(self, room: Contact) -> str | None:
        """The room password to log in to ``room`` with, or ``None`` when none is remembered.

        ``""`` is a remembered password — the room is open — and an admin membership
        remembers none here (its password is the admin store's).
        """
        record = self.membership(room)
        return record.password if record is not None else None

    def access(self, room: Contact) -> RoomAccess | None:
        """The access ``room`` granted at our last login, or ``None`` if we never joined it."""
        record = self.membership(room)
        return RoomAccess(record.access) if record is not None else None

    def record(self, room: Contact, password: str, login: RoomLogin) -> None:
        """Update what we remember about ``room`` from how a login to it ended.

        The admin store's credential policy (:meth:`~meshterm.core.admin_store.AdminStore.
        record`), for the room password: kept when the room accepts it, forgotten when the
        node *refuses* it, and left as it is when nothing came back — silence is not a
        verdict, and from a room it is also how a wrong password sounds, so a password that
        never once worked is simply never written. An admin login is remembered as the
        access alone; its password is the admin store's to keep.

        Args:
            room: The room the login addressed.
            password: The password that was tried.
            login: How the login ended.
        """
        if login.result is LoginResult.ACCEPTED and login.access is not None:
            records = self._load()
            key = admin_key(room)
            records[key] = RoomMembership(
                key=key,
                password=None if login.access is RoomAccess.ADMIN else password,
                access=login.access.value,
                label=room.name,
                last_login=utcnow().isoformat(),
            )
            self._write(records)
        elif login.result is LoginResult.REFUSED:
            self.forget(room)

    def forget(self, room: Contact) -> None:
        """Remove everything remembered about ``room``.

        Args:
            room: The room to forget.
        """
        records = self._load()
        if records.pop(admin_key(room), None) is not None:
            self._write(records)

    def _write(self, records: dict[str, RoomMembership]) -> None:
        """Persist ``records`` atomically with owner-only permissions where supported."""
        data = {key: record.to_json() for key, record in records.items()}
        write_atomically(self._path, json.dumps(data, indent=2), owner_only=True)
