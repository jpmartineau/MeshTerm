# SPDX-License-Identifier: Apache-2.0
"""The store for the rooms that this machine joined: the password of each room, and our access.

A room server has two passwords. The *admin password* is the password that Repeater admin
already remembers (:mod:`meshterm.core.admin_store`), and it lets you into a room as its
admin. The *room password* is the password that the owner gives to the members, and this
store keeps it. To join a room is to log in with one of the two passwords. Thus a room is
"joined" here exactly when MeshTerm has a password with which it can log in. When the user
opens the room again, MeshTerm logs in with that password silently, and does not ask.

This store never writes the password of an admin login, only the access that the login
got. The admin password is in one place, where Repeater admin can change it. Thus MeshTerm
never logs in to a room with an old copy of it. If the owner of a room cleared the room
password, the room is open to all, and its remembered password is the empty string. That
is a real answer, not an absence. Thus :meth:`RoomStore.password` returns ``None`` for
"nothing remembered".

This store is global machine state in a small JSON file (``<config_dir>/rooms.json``), the
same as the admin passwords. MeshTerm writes the file with owner-only permissions, because
it holds passwords in plaintext. The file is read into :class:`RoomMembership` records and
written back from them. Thus a field that no record knows is removed after the next write
(refer to :data:`RoomMembership.RETIRED`).
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
    """What MeshTerm remembers about one room that it joined.

    Attributes:
        key: The :func:`~meshterm.core.admin_store.admin_key` under which the room is
            stored. It is the same key as for its admin password, so that the two stores
            agree about which room is which. (It is the key of the record in the file, not
            one of its fields.)
        password: The room password with which MeshTerm got in. ``""`` for a blank login
            that the room accepted (an open room, or a room that knew our node). ``None``
            when no room password is known: MeshTerm got in as the admin of the room, and
            the admin password is in the admin store.
        access: The access that the room gave at our last login: a
            :class:`~meshterm.core.models.RoomAccess` value.
        label: The name of the room, to show.
        last_login: The ISO-8601 timestamp of the last accepted login.
    """

    key: str
    password: str | None
    access: str
    label: str
    last_login: str

    #: The fields that this record held before. It must never hold them again with a
    #: different meaning (refer to :data:`meshterm.core.preferences.RETIRED` for the
    #: reason that a name is never used again).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, key: str, raw: object) -> RoomMembership | None:
        """Build a record from one file entry, or ``None`` when MeshTerm cannot use it."""
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
        """The record as written: its own fields only, not the other fields of the read entry."""
        return {
            "password": self.password,
            "access": self.access,
            "label": self.label,
            "last_login": self.last_login,
        }


class RoomStore:
    """Reads and writes the rooms that this machine joined."""

    def __init__(self, path: Path) -> None:
        """Open the store on the location of a JSON file.

        Args:
            path: Path to the JSON state file (made only at the first join).
        """
        self._path = path

    def _load(self) -> dict[str, RoomMembership]:
        """Read all the memberships by room (empty if the file is missing or corrupt)."""
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
        """What MeshTerm remembers about ``room``, or ``None`` if it never joined the room."""
        return self._load().get(admin_key(room))

    def password(self, room: Contact) -> str | None:
        """The room password for a login to ``room``, or ``None`` when none is remembered.

        ``""`` is a remembered password: the room is open. An admin membership remembers
        no password here (its password is in the admin store).
        """
        record = self.membership(room)
        return record.password if record is not None else None

    def access(self, room: Contact) -> RoomAccess | None:
        """The access from the last login to ``room``, or ``None`` if it was never joined."""
        record = self.membership(room)
        return RoomAccess(record.access) if record is not None else None

    def record(self, room: Contact, password: str, login: RoomLogin) -> None:
        """Change what MeshTerm remembers about ``room``, from the result of a login to it.

        For the room password, the function uses the credential policy of the admin store
        (:meth:`~meshterm.core.admin_store.AdminStore.record`). The password is kept when
        the room accepts it. It is forgotten when the node refuses it. It stays as it is
        when no answer came back, because no answer is not a verdict. A room also gives no
        answer to a wrong password. Thus a password that never worked is never written.

        But "accepted" is not one single proof. A password is kept only for what it proved:

        * **Admin** with a typed password: the admin store keeps the password (it is the
          admin password of the room). This store keeps only the access, next to the room
          password that it already remembered.
        * **Member** with a typed password: that password is the room password.
        * **Read-only** proves nothing about the password. A room that lets read-only
          users in lets any password in. Thus it never replaces a password that is
          already remembered, which can be the password of a member. It is kept only
          where no password was remembered.
        * **Blank** asks "do you know me?" and proves only that the room knows our node.
          It also never replaces a remembered password. Else, if the room forgets a
          member at its next restart, the member loses a password that works, and keeps
          only a password that cannot work.

        Args:
            room: The room to which the login went.
            password: The password that was tried.
            login: How the login ended.
        """
        records = self._load()
        key = admin_key(room)
        known = records.get(key)
        if login.result is LoginResult.ACCEPTED and login.access is not None:
            kept = known.password if known is not None else None
            if login.access is RoomAccess.ADMIN and password:
                remembered = kept
            elif login.access is RoomAccess.MEMBER and password:
                remembered = password
            else:
                remembered = kept if kept is not None else password
            records[key] = RoomMembership(
                key=key,
                password=remembered,
                access=login.access.value,
                label=room.name,
                last_login=utcnow().isoformat(),
            )
            self._write(records)
        elif login.result is LoginResult.REFUSED and known is not None:
            # Forget only the password that the node refused.
            if known.password == password:
                records.pop(key)
                self._write(records)

    def forget(self, room: Contact) -> None:
        """Remove all that MeshTerm remembers about ``room``.

        Args:
            room: The room to forget.
        """
        records = self._load()
        if records.pop(admin_key(room), None) is not None:
            self._write(records)

    def _write(self, records: dict[str, RoomMembership]) -> None:
        """Write ``records`` atomically, with owner-only permissions where the OS has them."""
        data = {key: record.to_json() for key, record in records.items()}
        write_atomically(self._path, json.dumps(data, indent=2), owner_only=True)
