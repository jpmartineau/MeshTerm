# SPDX-License-Identifier: Apache-2.0
"""Keep each password and each secret out of each log: the log file and the run log.

MeshTerm keeps a record of what it does in two places: the log file (``meshterm.log``) and
the ``runs`` table, which stores the parameters and the summary of each run. Neither place
may ever hold a password, a channel secret, a pairing PIN, or a private key (JP,
2026-10-18: "NEVER log passwords or secrets!"). Each one is written as :data:`SECRET_TEXT`
instead. This rule does not depend on a preference, and the ``anonymize`` preference
(refer to :mod:`meshterm.core.anonymize`) adds to it and does not replace it.

A secret gets into a record in many shapes, and this module has one rule for each shape.
No rule depends on a list of the tools or of their parameters. Thus a tool that is added
later is covered on the day that it arrives:

- **A named value.** A value is a secret when its name says so (:func:`names_secret`):
  a mapping key (``{"password": …}``), a dataclass field (``Conversation.secret``), the
  ``key`` of a record (``{"key": "device_pin", "value": …}``), the name in an operation
  (``("set", "device_pin", "123456")``), or the name before ``=`` in a text
  (``…&secret=8f…``, ``secret=b'…'``).
- **A wrapped value.** A tool can wrap a value in :class:`Secret` when nothing names it.
- **Bytes.** The parameters of a run carry bytes only for keys and secrets. Thus each
  ``bytes`` value is a secret.
- **A remote command.** ``password <new>`` and ``set guest.password <new>`` carry the
  secret as their last word.
- **A known value in free text.** Each value that a rule above hid, and each secret that
  MeshTerm reads (a stored password, a channel secret, a pairing PIN), is registered
  (:func:`remember`). :func:`hide` then finds it in any text: an error message that
  repeats it, or a frame that a library writes in hex.

:func:`loggable` applies the rules to a structure (the parameters and the summary of a
run). :func:`hide` applies them to a text (a log record).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import PurePath
from typing import Any

#: What a log and the run log show in the place of a secret.
SECRET_TEXT = "<secret>"

#: The words that make a name the name of a secret. A name matches when one of its words
#: is in this set. The words of a name are the parts between the characters that are not
#: letters or digits. Thus ``device_pin``, ``guest.password``, and ``bridge.secret`` match,
#: and ``pinned`` and ``spin`` do not. ``prv`` is the private key of a repeater
#: (``prv.key``). ``key`` alone is not a secret word, because most keys in MeshTerm are
#: public keys.
SECRET_WORDS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "secret",
        "pin",
        "private",
        "privkey",
        "prv",
        "psk",
        "token",
        "credential",
        "credentials",
    }
)

#: The entries of a record whose ``key`` names a secret, and which hold its value.
_RECORD_VALUES = frozenset({"value", "previous", "default", "old", "new"})

#: The entries of a record that name the thing that the record is about.
_RECORD_NAMES = ("key", "setting")

#: The shortest text that :func:`remember` registers. A shorter value matches too much
#: ordinary text in a log (``abc``, ``123``). The structural rules still hide it where it
#: has a name.
_MIN_REMEMBERED = 4

#: The shortest hex value that :func:`hide` finds inside a longer run of hex. A channel
#: secret has 32 hex digits. With 16 or more, a match by chance is not probable.
_MIN_HEX_SUBSTRING = 16

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
_HEX = re.compile(r"[0-9a-fA-F]+")

#: ``name = value`` in a text: a URL query, a ``repr`` of an object, or a ``key=value``
#: log line. A name with no quotes takes ``=``. A quoted name (a ``repr`` of a dict, or
#: JSON) also takes ``:``. Prose ("device_pin: must be six digits") is not an assignment.
#: The value is a quoted literal (also a ``bytes`` literal), or a run of characters up to
#: the end of the value.
_ASSIGNMENT = re.compile(
    r"(?P<name>(?<![\w.-])[A-Za-z_][\w.-]*)"
    r"(?P<sep>\s*=\s*|['\"]\s*[=:]\s*)"
    r"(?P<value>b?'(?:[^'\\]|\\.)*'|b?\"(?:[^\"\\]|\\.)*\"|<secret>|[^\s'\",;)}\]&<>]+)"
)

#: A remote command that sets a password: ``password <new>``.
_PASSWORD_COMMAND = re.compile(r"^(?P<head>\s*password\s+)(?P<value>\S.*)$", re.IGNORECASE)

#: A remote command that sets a setting: ``set <name> <value>``.
_SET_COMMAND = re.compile(r"^(?P<head>\s*set\s+(?P<name>\S+)\s+)(?P<value>\S.*)$", re.IGNORECASE)

_known: set[str] = set()
_known_pattern: re.Pattern[str] | None = None
_known_lock = threading.Lock()


class Secret:
    """A value that no log may keep, for example a private key or a password.

    :meth:`~meshterm.tools.base.Tool.execute` stores the parameters of each run in the
    ``runs`` table, and logs them at debug level. A tool that has a secret that nothing
    names (a key in a tuple) puts it in this wrapper. :func:`loggable` and each text form
    of this object show :data:`SECRET_TEXT`, and the tool reads :attr:`value` (refer to
    :func:`reveal`).

    The class does not derive from ``str``, because ``json.dumps`` writes the value of a
    ``str`` subclass and never calls its ``default`` for it.

    Attributes:
        value: The secret itself.
    """

    __slots__ = ("value",)

    def __init__(self, value: str) -> None:
        """Keep ``value`` out of each text form of this object, and register it."""
        self.value = value
        remember(value)

    def __repr__(self) -> str:
        """:data:`SECRET_TEXT`, never the value."""
        return SECRET_TEXT

    __str__ = __repr__


def reveal(value: Any) -> Any:
    """The value inside a :class:`Secret`, or ``value`` itself if it is not a secret."""
    return value.value if isinstance(value, Secret) else value


def names_secret(name: str) -> bool:
    """Whether ``name`` is the name of a secret (refer to :data:`SECRET_WORDS`).

    Args:
        name: A parameter name, a field name, a setting key, or a word of a text.

    Returns:
        ``True`` when one of the words of ``name`` is a secret word.
    """
    return not SECRET_WORDS.isdisjoint(_WORD_SPLIT.split(name.lower()))


def remember(value: Any) -> None:
    """Register a secret, so that :func:`hide` finds it in any text after this call.

    A value of bytes registers as hex, the form in which a log writes bytes. A value that
    is too short to find safely, or that is a secret word (a password of ``password``), is
    not registered: each mention of the word in the log would become the mark. Each other
    rule still hides such a value where it has a name.

    Args:
        value: The secret: text, a number (a PIN), or bytes.
    """
    global _known_pattern
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        text = bytes(value).hex()
    else:
        text = str(value).strip()
    if len(text) < _MIN_REMEMBERED or text == SECRET_TEXT or text.lower() in SECRET_WORDS:
        return
    with _known_lock:
        if text in _known:
            return
        _known.add(text)
        _known_pattern = _compile_known(_known)


def hide(text: str) -> str:
    """Replace each secret in ``text`` with :data:`SECRET_TEXT`.

    Args:
        text: A log record, an error message, a remote command, or a URL.

    Returns:
        The text with each secret that a rule finds replaced.
    """
    match = _PASSWORD_COMMAND.match(text)
    if match is not None:
        _remember_text(match.group("value"))
        return match.group("head") + SECRET_TEXT
    match = _SET_COMMAND.match(text)
    if match is not None and names_secret(match.group("name")):
        _remember_text(match.group("value"))
        return match.group("head") + SECRET_TEXT
    text = _ASSIGNMENT.sub(_hide_assignment, text)
    known = _known_pattern
    if known is not None:
        text = known.sub(SECRET_TEXT, text)
    return text


def without_secret(command: str) -> str:
    """A remote command with its secret value removed, for a history to recall.

    The history of the remote command line keeps ``password `` for ``password hunter2``.
    Thus ↑ recalls the start of the command, and the user types the new value again. If
    the history kept :data:`SECRET_TEXT`, a recall and Enter would set the password to
    that text.

    Args:
        command: A command as the user typed it.

    Returns:
        The command up to its secret value, or the command itself when it has none.
    """
    for pattern in (_PASSWORD_COMMAND, _SET_COMMAND):
        match = pattern.match(command)
        if match is not None and (pattern is _PASSWORD_COMMAND or names_secret(match["name"])):
            return match.group("head")
    return command


def loggable(value: Any) -> Any:
    """A copy of ``value`` that a log can keep: each secret in it is :data:`SECRET_TEXT`.

    The copy has the same shape: mappings stay mappings, and lists and tuples keep their
    types. A dataclass becomes an object whose text form is the ``repr`` of the dataclass
    with its secret fields hidden, so the log line looks as it did. Each text goes through
    :func:`hide`. Each value that a rule hides is registered (:func:`remember`). Thus the
    same value is also hidden when an error message repeats it later in the run.

    Args:
        value: The parameters or the summary of a run, or any part of them.

    Returns:
        The copy, ready for a log line or for ``json.dumps(..., default=str)``.
    """
    if isinstance(value, Secret):
        remember(value.value)
        return SECRET_TEXT
    if isinstance(value, (bytes, bytearray, memoryview)):
        return _hidden(value)
    if isinstance(value, str):
        return hide(value)
    if value is None or isinstance(value, (bool, int, float, Enum, PurePath, date, datetime)):
        return value
    if isinstance(value, Mapping):
        return _loggable_mapping(value)
    if is_dataclass(value) and not isinstance(value, type):
        items = []
        for f in fields(value):
            item = getattr(value, f.name)
            items.append((f.name, _hidden(item) if names_secret(f.name) else loggable(item)))
        return _Shown(type(value).__name__, items)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = _loggable_sequence(list(value))
        return tuple(items) if isinstance(value, tuple) else items
    return _Shown(hide(repr(value)))


class _Shown:
    """An object in a log, with its secrets hidden: its ``repr`` and its ``str`` are the same.

    ``json.dumps(..., default=str)`` writes an object as its ``str``, and a log line writes
    a value in a dict as its ``repr``. Both forms of the original object can hold the
    secret, so both forms of this object are the hidden form.
    """

    __slots__ = ("_text",)

    def __init__(self, name: str, items: list[tuple[str, Any]] | None = None) -> None:
        """Build the form ``Name(a=1, b=<secret>)``, or keep ``name`` as the whole text."""
        if items is None:
            self._text = name
        else:
            inner = ", ".join(f"{field}={_repr(value)}" for field, value in items)
            self._text = f"{name}({inner})"

    def __repr__(self) -> str:
        """The hidden form."""
        return self._text

    __str__ = __repr__


def _repr(value: Any) -> str:
    """The ``repr`` of a value in a hidden form, with :data:`SECRET_TEXT` not in quotes."""
    return SECRET_TEXT if value == SECRET_TEXT else repr(value)


def _hidden(value: Any) -> Any:
    """:data:`SECRET_TEXT` for a value that a name or a type makes secret, after it is registered.

    An absent value (``None``, or an empty text) stays as it is. That a password was not
    given is a fact that a maintainer can use, and it hides nothing.
    """
    if value is None or value == "" or value == b"":
        return value
    remember(reveal(value))
    return SECRET_TEXT


def _loggable_mapping(value: Mapping) -> dict:
    """:func:`loggable` for a mapping: hide each value that its key or its record names secret."""
    record_names = [value.get(name) for name in _RECORD_NAMES]
    secret_record = any(isinstance(name, str) and names_secret(name) for name in record_names)
    shown: dict = {}
    for key, item in value.items():
        named = names_secret(str(key)) or (secret_record and key in _RECORD_VALUES)
        shown[key] = _hidden(item) if named else loggable(item)
    return shown


def _loggable_sequence(items: list) -> list:
    """:func:`loggable` for a sequence, and for an operation that sets a secret.

    An operation that sets a value names the setting before the value:
    ``("set", "device_pin", "123456")``. Thus, in a sequence of three items that starts
    with ``set`` and whose second item names a secret, the third item is the secret. No
    other sequence has a value after a name. A list of setting names
    (``["name", "device_pin", "radio_freq"]``) stays as it is.
    """
    shown = [loggable(item) for item in items]
    verb, name = (items[0], items[1]) if len(items) == 3 else (None, None)
    if isinstance(verb, str) and verb.lower() == "set" and isinstance(name, str):
        if names_secret(name):
            shown[2] = _hidden(items[2])
    return shown


def _hide_assignment(match: re.Match[str]) -> str:
    """Hide the value of ``name = value`` when the name is the name of a secret."""
    value = match.group("value")
    if not names_secret(match.group("name")) or value in (SECRET_TEXT, "None", "null"):
        return match.group(0)
    if value in ("''", '""', "b''", 'b""'):
        return match.group(0)
    _remember_text(value)
    return match.group("name") + match.group("sep") + SECRET_TEXT


def _remember_text(value: str) -> None:
    """Register a value that a text rule found, without the quotes of a literal."""
    text = value.strip()
    if len(text) >= 2 and text[-1] in "'\"" and text.lstrip("b")[:1] == text[-1]:
        text = text.lstrip("b")[1:-1]
    remember(text)


def _compile_known(known: set[str]) -> re.Pattern[str]:
    """The pattern that finds each registered secret.

    A long hex value matches also inside a longer run of hex, in any letter case, because a
    library writes a whole frame as one run of hex. Each other value matches only as a
    whole word, so a PIN of ``123456`` does not hide a part of ``71234567``.
    """
    parts: list[str] = []
    for text in sorted(known, key=len, reverse=True):
        if len(text) >= _MIN_HEX_SUBSTRING and _HEX.fullmatch(text):
            parts.append(f"(?i:{re.escape(text)})")
        else:
            parts.append(rf"(?<![\w]){re.escape(text)}(?![\w])")
    return re.compile("|".join(parts))


__all__ = [
    "SECRET_TEXT",
    "SECRET_WORDS",
    "Secret",
    "hide",
    "loggable",
    "names_secret",
    "remember",
    "reveal",
    "without_secret",
]
