# SPDX-License-Identifier: Apache-2.0
r"""Anonymize a text: mask each personal detail in it, and keep a tidbit of each one.

The log and the diagnostics block go into public bug reports. A user who reads their own
log needs each detail as it is, so MeshTerm masks nothing by default. When the
``anonymize`` preference is on, :func:`anonymize` masks three types of personal detail:

- **The account name in a home folder.** ``C:\Users\jpmartineau`` becomes
  ``C:\Users\j*********u``, and ``/home/jp`` becomes ``/home/j*``.
- **The address of a radio.** A Bluetooth address ``DA:C5:21:6B:7C:7C`` becomes
  ``DA:xx:xx:xx:7C:7C``. A macOS peripheral id (a UUID) keeps its first two and its last
  four hex digits in the same way.
- **The address of a host.** An IPv4 address ``192.168.0.180`` becomes ``192.xxx.x.180``.
  The name of a network companion (``meshcore.local``) becomes ``m******e.local``, because
  MeshTerm remembers each host that it opens (:func:`remember_host`).

**A mask keeps a tidbit, and never removes the value** (JP, 2026-10-17). A card number
prints as ``51xx xxxx xxxx 4571``, and a masked value keeps its first and its last
characters in the same way. Thus two radios in one log stay two different radios, and a
maintainer can still match the address in a log line with the address in the
diagnostics. Also, a value keeps its length and its separators, so the line keeps its
shape. A digit masks to ``x``, the mark that a person expects in a number. A letter
masks to ``*``, because an ``x`` in a name is a letter.

The masks are idempotent. A masked value does not match its own rule again, or it masks
to itself. Thus a text can go through :func:`anonymize` two times with no change.

Node names, message text, and keys are not personal details in this sense, and this module
does not mask them. The diagnostics never contain them (refer to
:mod:`meshterm.tools.diagnostics`).
"""

from __future__ import annotations

import ipaddress
import re
import sys
import threading
from functools import lru_cache
from pathlib import Path

#: The mark that replaces a masked letter of a name.
NAME_MASK = "*"

#: The mark that replaces a masked digit of a number or an address.
DIGIT_MASK = "x"

_HEX = frozenset("0123456789abcdefABCDEF")

#: A Bluetooth address: six hex pairs, all with the same separator. The lookarounds keep a
#: longer run of hex pairs from matching in its middle. They refuse a separator only when
#: a hex digit is on its far side, because a log line can put a colon after an address
#: ("BLE companion DA:C5:21:6B:7C:7C: connected to ...").
_MAC = re.compile(
    r"(?<![0-9A-Fa-f])(?<![0-9A-Fa-f][:-])"
    r"[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}"
    r"(?![0-9A-Fa-f])(?![:-][0-9A-Fa-f])"
)

#: A UUID: the form in which macOS names a Bluetooth peripheral.
_UUID = re.compile(
    r"(?<![0-9A-Fa-f])(?<![0-9A-Fa-f]-)"
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f])(?!-[0-9A-Fa-f])"
)

#: An IPv4 address. The octets are checked after the match (refer to :func:`_mask_ipv4`).
#: Thus a version with four parts and a large part (``10.0.19045.1234``) stays as it is.
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\.?\d)")

#: A home folder in the standard place of each OS: ``C:\Users\<name>``, ``/home/<name>``,
#: and ``/Users/<name>``. The separators match in each spelling: a backslash, a slash, and
#: the doubled backslash of a ``repr``. This rule also masks the short form of a long
#: account name on Windows (``JPMART~1``), which the exact rule cannot know. The account
#: can contain ``*``, so that a name that is already masked masks to itself.
_HOME_ROOT = re.compile(
    r"(?:(?<!\w)[A-Za-z]:(?:\\|/)+[Uu][Ss][Ee][Rr][Ss](?:\\|/)+|(?<![\w.])/(?:home|Users)/)"
    r"(?P<account>[^\\/\s'\"<>|:,;()\[\]]+)"
)

#: The hosts that :func:`remember_host` registered, and the pattern that finds them.
_hosts: dict[str, str] = {}
_host_pattern: re.Pattern[str] | None = None
_hosts_lock = threading.Lock()


def mask_name(name: str) -> str:
    """Mask a name, and keep its first and its last characters: ``j*********u``.

    The masked name has the same length as the name. A name of two characters keeps only
    its first character, and a name of one character keeps nothing.

    Args:
        name: The name to mask.

    Returns:
        The masked name.
    """
    if len(name) <= 1:
        return NAME_MASK * len(name)
    if len(name) == 2:
        return name[0] + NAME_MASK
    return name[0] + NAME_MASK * (len(name) - 2) + name[-1]


def mask_host(host: str) -> str:
    """Mask a host: an IP address as a number, or a host name as a name.

    An IPv4 address keeps its first and its last octets (``192.xxx.x.180``). An IPv6
    address keeps its first and its last groups. A host name keeps its last label, which is
    the domain that tells a maintainer what type of network it is (``.local``, ``.lan``),
    and masks each other label as a name. A host name of one label is masked as a name.

    Args:
        host: The host, with no port and no brackets.

    Returns:
        The masked host.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.split(".")
        if len(labels) == 1:
            return mask_name(host)
        return ".".join([mask_name(label) for label in labels[:-1]] + [labels[-1]])
    if address.version == 4:
        return _IPV4.sub(_mask_ipv4, host)
    groups = host.split(":")
    return ":".join([groups[0]] + [_mask_digits(group) for group in groups[1:-1]] + [groups[-1]])


def remember_host(host: str) -> None:
    """Register a network host, so that :func:`anonymize` masks it each time it occurs.

    No pattern can find a host name in free text. Thus MeshTerm registers each host that it
    opens, and the mask finds that exact text. :class:`~meshterm.core.connection.MeshCoreDevice`
    calls this function for each TCP companion. An IPv4 address needs no registration,
    because its own rule finds it.

    Args:
        host: A host name or an IP address. Brackets around an IPv6 address are removed.
    """
    global _host_pattern
    text = host.strip().strip("[]")
    if not text or _IPV4.fullmatch(text):
        return
    with _hosts_lock:
        if text.lower() in _hosts:
            return
        _hosts[text.lower()] = mask_host(text)
        # The longest host first, so that a host never masks a part of a longer host. A
        # colon can follow a host, because the port comes after it ("meshcore.local:5000").
        alternatives = "|".join(re.escape(h) for h in sorted(_hosts, key=len, reverse=True))
        _host_pattern = re.compile(rf"(?<![\w.-])(?:{alternatives})(?![\w-])", re.IGNORECASE)


def anonymize(text: str) -> str:
    """Mask each personal detail in ``text``: home folders, radio addresses, and hosts.

    Refer to the module docstring for each rule. The home folder of this machine is masked
    also when it is not in the standard place (``/root``, or a folder that a user chose).

    Args:
        text: Any text: a log record with its traceback, or a value of the diagnostics.

    Returns:
        The text with each personal detail masked.
    """
    home = _home()
    if home:
        text = _exact_home_rule(home, sys.platform == "win32").sub(_mask_account, text)
    text = _HOME_ROOT.sub(_mask_account, text)
    text = _MAC.sub(_mask_address, text)
    text = _UUID.sub(_mask_address, text)
    text = _IPV4.sub(_mask_ipv4, text)
    hosts = _host_pattern
    if hosts is not None:
        text = hosts.sub(lambda m: _hosts.get(m.group(0).lower(), m.group(0)), text)
    return text


def _home() -> str:
    """The home folder of this machine, or ``""`` when the OS cannot say where it is."""
    try:
        return str(Path.home())
    except (RuntimeError, KeyError, OSError):
        return ""


@lru_cache(maxsize=4)
def _exact_home_rule(home: str, fold_case: bool) -> re.Pattern[str]:
    """The pattern that finds ``home`` in a text, with the separators in each spelling.

    The last folder of the home path is the account name. Only that folder is masked, so
    the masked path still shows where MeshTerm keeps its data. A home with no folder in it
    (``/``) gives a pattern that never matches.

    Args:
        home: The home folder of this machine.
        fold_case: Whether to ignore case, because Windows paths ignore it.

    Returns:
        A pattern with the group ``account``.
    """
    folders = [part for part in re.split(r"[\\/]+", home) if part]
    if not folders:
        return re.compile(r"(?!)")
    separator = r"(?:\\|/)+"
    lead = separator if home[:1] in ("/", "\\") else ""
    head = separator.join(re.escape(part) for part in folders[:-1])
    pattern = (
        rf"(?<![\w.]){lead}{head}{separator if head else ''}"
        rf"(?P<account>{re.escape(folders[-1])})(?![\w-])"
    )
    return re.compile(pattern, re.IGNORECASE if fold_case else 0)


def _mask_account(match: re.Match[str]) -> str:
    """Mask the ``account`` group of a home-folder match, and keep the rest of the match."""
    whole = match.group(0)
    start = match.start("account") - match.start()
    end = match.end("account") - match.start()
    return whole[:start] + mask_name(match.group("account")) + whole[end:]


def _mask_address(match: re.Match[str]) -> str:
    """Mask a radio address: keep the first two and the last four hex digits."""
    return _mask_digits(match.group(0), keep_head=2, keep_tail=4)


def _mask_ipv4(match: re.Match[str]) -> str:
    """Mask the two middle octets of an IPv4 address, or keep a match that is not one."""
    octets = match.groups()
    if any(int(octet) > 255 for octet in octets):
        return match.group(0)
    first, second, third, last = octets
    return ".".join((first, _mask_digits(second), _mask_digits(third), last))


def _mask_digits(text: str, *, keep_head: int = 0, keep_tail: int = 0) -> str:
    """Replace the hex digits of ``text`` with :data:`DIGIT_MASK`, but the ones to keep.

    The separators stay where they are, so the value keeps its shape.

    Args:
        text: The value to mask.
        keep_head: How many digits to keep at the start.
        keep_tail: How many digits to keep at the end.

    Returns:
        The masked value.
    """
    digits = [i for i, char in enumerate(text) if char in _HEX]
    masked = set(digits[keep_head : len(digits) - keep_tail])
    return "".join(DIGIT_MASK if i in masked else char for i, char in enumerate(text))


__all__ = ["DIGIT_MASK", "NAME_MASK", "anonymize", "mask_host", "mask_name", "remember_host"]
