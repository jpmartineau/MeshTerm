# SPDX-License-Identifier: Apache-2.0
"""Persistence for remembered remote-node admin passwords.

The remote-admin optimizer logs in to a repeater to tune its TX power; storing the
password means the user isn't prompted on every run. Like the remembered device
(:mod:`meshterm.core.device_store`), this lives in a small JSON file in the config
directory (``<config_dir>/admin.json``) rather than the per-invocation SQLite database,
because admin credentials are global machine state.

The file is read into :class:`AdminCredential` records and written back out of them,
never round-tripped as the raw JSON it was read as, so a field no record knows is gone
after the next write (see :data:`AdminCredential.RETIRED`).

Passwords are stored in plaintext, so the file is written with owner-only permissions
where the platform supports it. This is a local operator tool tuning their own mesh, not
a multi-user secret store; treat the file accordingly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from .atomicwrite import write_atomically
from .models import Contact, LoginResult, utcnow


def admin_key(node: Contact) -> str:
    """Return the lookup key for a node's stored password.

    Prefers the full public key (stable and unambiguous); falls back to the key prefix
    and finally the name so a contact without a key can still be remembered.

    Args:
        node: The contact to key on.

    Returns:
        A normalized, lowercased identifier.
    """
    key = node.public_key or node.key_prefix or node.name
    return key.lower().removeprefix("0x")


@dataclass(slots=True)
class AdminCredential:
    """A remembered admin password for one remote node.

    Attributes:
        key: The :func:`admin_key` the password is stored under (the record's key in the
            file, not one of its fields).
        password: The node's admin password (plaintext).
        label: A friendly node name for display.
        last_used: ISO-8601 timestamp of the last successful login.
    """

    key: str
    password: str
    label: str
    last_used: str

    #: Fields this record once held and must never hold again under another meaning (see
    #: :data:`meshterm.core.preferences.RETIRED` for why a name is never reused).
    RETIRED: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def read(cls, key: str, raw: object) -> AdminCredential | None:
        """Build a record from one file entry, or ``None`` when it holds no password."""
        if not isinstance(raw, dict) or raw.get("password") is None:
            return None
        return cls(
            key=key,
            password=str(raw["password"]),
            label=str(raw.get("label") or ""),
            last_used=str(raw.get("last_used") or ""),
        )

    def to_json(self) -> dict:
        """The record as written: its own fields and nothing it happened to be read with."""
        return {"password": self.password, "label": self.label, "last_used": self.last_used}


class AdminStore:
    """Reads and writes remembered remote-node admin passwords."""

    def __init__(self, path: Path) -> None:
        """Open the store against a JSON file location.

        Args:
            path: Path to the JSON state file (created lazily on first write).
        """
        self._path = path

    def _load(self) -> dict[str, AdminCredential]:
        """Read every credential, keyed by :func:`admin_key`; empty on a missing/corrupt file."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        records = {}
        for key, raw in data.items():
            record = AdminCredential.read(str(key), raw)
            if record is not None:
                records[record.key] = record
        return records

    def get(self, node: Contact) -> str | None:
        """Return the remembered password for ``node``, or ``None`` if not stored.

        Args:
            node: The contact to look up.

        Returns:
            The stored password, or ``None``.
        """
        record = self._load().get(admin_key(node))
        return record.password if record is not None else None

    def remember(self, node: Contact, password: str) -> None:
        """Store (or update) the admin password for ``node``.

        Args:
            node: The contact the password belongs to.
            password: The admin password to remember.
        """
        records = self._load()
        key = admin_key(node)
        records[key] = AdminCredential(
            key=key, password=password, label=node.name, last_used=utcnow().isoformat()
        )
        self._write(records)

    def record(self, node: Contact, password: str, outcome: LoginResult) -> None:
        """Update what we remember about ``node`` from how a login attempt ended.

        THE credential policy, in one place because it was wrong in five: a password is
        remembered when the node accepts it and forgotten when the node *rejects* it — and
        left exactly as it is when nothing came back. Silence is not a denial. Every caller
        used to collapse "refused" and "no reply" into one ``False`` and forget on both, so
        opening the admin flow on a repeater that happened to be down erased its password;
        the node had said nothing at all, and we took the silence as a verdict.

        Callers still phrase their own message (the sweep says "sweep again", the CLI says
        "re-run"), but none of them decides this.

        Args:
            node: The contact the attempt addressed.
            password: The password that was tried.
            outcome: What the device reported.
        """
        if outcome is LoginResult.ACCEPTED:
            self.remember(node, password)
        elif outcome is LoginResult.REFUSED:
            self.forget(node)

    def forget(self, node: Contact) -> None:
        """Remove any remembered password for ``node`` (e.g. after it stops working).

        Args:
            node: The contact to forget.
        """
        records = self._load()
        if records.pop(admin_key(node), None) is not None:
            self._write(records)

    def _write(self, records: dict[str, AdminCredential]) -> None:
        """Persist ``records`` atomically with owner-only permissions where supported."""
        data = {key: record.to_json() for key, record in records.items()}
        write_atomically(self._path, json.dumps(data, indent=2), owner_only=True)
