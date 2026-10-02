# SPDX-License-Identifier: Apache-2.0
"""Find the links in a line of text — written with a scheme, or without one where it is plain.

A link with a scheme is anything ``scheme://…``: a scheme rather than a list of them, so a
``meshcore://`` share pasted into a chat is found as surely as a web link is. One written
without a scheme is accepted only where nothing else could be meant — a host starting
``www.``, or one whose last label is a top-level domain IANA has delegated
(``meshterm.net``, ``github.com/…``). So ``file.txt`` is not a link, ``txt`` being no TLD,
and neither is ``3.14``, ``e.g.`` or an address like ``jp@meshterm.net``: a host only
counts where a word begins, never inside an address, a path or another host.

The registry is IANA's own list, kept verbatim in ``assets/tlds.txt`` with its version
line on top; refreshing it is downloading the file again
(https://data.iana.org/TLD/tlds-alpha-by-domain.txt). Some delegated TLDs are also file
extensions — ``.py``, ``.md``, ``.zip`` — and a name ending in one reads as a link here.
That is the registry's answer rather than a guess, and the cost of it is a code offered
for a link nobody meant, which is the cheaper way to be wrong.

Every link is handed back as the URL it opens: one written without a scheme gains
``https://``, because a phone handed a bare host in a QR code reads it as text.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

#: IANA's list of delegated top-level domains, one per line, upper case, ``#`` comments.
TLDS_FILE = Path(__file__).resolve().parent.parent / "assets" / "tlds.txt"

#: One host label: letters, digits and inner hyphens, at most 63 long.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"

#: A link with a scheme: the scheme, ``://``, and everything up to the next space.
_SCHEMED = r"\b[a-z][a-z0-9+.-]*://[^\s<>\"]+"

#: A host written without a scheme, then an optional port and path. It must start a word —
#: not follow a letter, a digit, ``@`` (an address), ``/`` or ``:`` (a path), or a ``.``
#: or ``-`` (the middle of a longer name) — and its last label must be all letters, the
#: shape of every TLD, so a version number or a decimal never gets this far.
_BARE = (
    r"(?<![\w@/:.-])"
    rf"(?P<host>(?:{_LABEL}\.)+(?P<tld>[a-z]{{2,63}}))(?![\w-])"
    r"(?::\d{1,5})?"
    r"(?:[/?#][^\s<>\"]*)?"
)

#: Either kind. The scheme comes first, so a host inside a scheme's link is consumed with
#: it and never found a second time on its own.
_LINK = re.compile(rf"(?P<scheme>{_SCHEMED})|(?P<bare>{_BARE})", re.IGNORECASE)

#: Punctuation that follows a link in a sentence and isn't part of it.
_TRAILING = ".,;:!?'\""

#: Each closing bracket and its opener: a closer ends a link only when it has no opener
#: inside it, so ``(see https://example.com/a_(b))`` keeps the ``)`` that belongs to it.
_CLOSERS = {")": "(", "]": "[", "}": "{"}


@cache
def tlds() -> frozenset[str]:
    """Every delegated top-level domain, lower case — read once, on the first link looked for."""
    lines = TLDS_FILE.read_text(encoding="ascii").splitlines()
    return frozenset(line.strip().lower() for line in lines if line.strip()[:1] not in ("", "#"))


def _trimmed(link: str) -> str:
    """``link`` without the sentence's punctuation after it, or a bracket it never opened."""
    while link:
        last = link[-1]
        if last in _TRAILING or (
            last in _CLOSERS and link.count(last) > link.count(_CLOSERS[last])
        ):
            link = link[:-1]
        else:
            break
    return link


def urls(text: str) -> list[str]:
    """Every link in ``text`` as the URL it opens, in reading order, each only once.

    Trailing punctuation is the sentence's, not the link's; a link written without a
    scheme comes back with ``https://`` in front of it, so ``meshterm.net`` and
    ``https://meshterm.net`` in one message are the same link, found once.
    """
    found: list[str] = []
    for match in _LINK.finditer(text):
        link = _trimmed(match.group())
        if match.group("scheme"):
            if link.endswith("://"):
                continue  # a scheme and nothing after it
        else:
            host = match.group("host").lower()
            if not host.startswith("www.") and match.group("tld").lower() not in tlds():
                continue
            link = "https://" + link
        if link not in found:
            found.append(link)
    return found


__all__ = ["TLDS_FILE", "tlds", "urls"]
