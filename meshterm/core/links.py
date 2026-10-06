# SPDX-License-Identifier: Apache-2.0
"""Find the links in a line of text: with a scheme, or without one where the meaning is clear.

A link with a scheme is any text in the form ``scheme://…``. Any scheme is accepted, not
only the schemes in a list. Thus a ``meshcore://`` share in a chat is found, the same as a
web link.

A link without a scheme is accepted only when it can have no other meaning. It is a host
that starts with ``www.``, or a host whose last label is a top-level domain (TLD) that
IANA delegated (``meshterm.net``, ``github.com/…``). Thus ``file.txt`` is not a link,
because ``txt`` is not a TLD. Also, ``3.14``, ``e.g.``, and an address such as
``jp@meshterm.net`` are not links. A host counts only at the start of a word, never in an
address, a URL path, or another host.

The list of TLDs is the IANA list, copied exactly into ``assets/tlds.txt`` with its
version line at the top. To update it, download the file again
(https://data.iana.org/TLD/tlds-alpha-by-domain.txt). Some delegated TLDs are also file
extensions (``.py``, ``.md``, ``.zip``), so a name that ends with one of them is a link
here. This result comes from the registry, not from a guess. When it is wrong, MeshTerm
offers a QR code for a link that nobody wanted. That error costs less than a link that
MeshTerm does not find.

Each link is returned as the URL that it opens. A link without a scheme gets ``https://``
at its start, because a phone reads a bare host in a QR code as text.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

#: The IANA list of the delegated top-level domains: one domain on each line, in upper
#: case, with ``#`` comments.
TLDS_FILE = Path(__file__).resolve().parent.parent / "assets" / "tlds.txt"

#: One host label: letters, digits, and hyphens that are not at an end. The maximum
#: length is 63.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"

#: A link with a scheme: the scheme, ``://``, and all the text up to the next space.
_SCHEMED = r"\b[a-z][a-z0-9+.-]*://[^\s<>\"]+"

#: A host without a scheme, then an optional port and URL path. The host must be at the
#: start of a word. It must not come after a letter, a digit, ``@`` (an address), ``/`` or
#: ``:`` (a URL path), or ``.`` or ``-`` (the middle of a longer name). Its last label must
#: have only letters, because each TLD has only letters. Thus a version number or a
#: decimal number does not match.
_BARE = (
    r"(?<![\w@/:.-])"
    rf"(?P<host>(?:{_LABEL}\.)+(?P<tld>[a-z]{{2,63}}))(?![\w-])"
    r"(?::\d{1,5})?"
    r"(?:[/?#][^\s<>\"]*)?"
)

#: Either type of link. The pattern with a scheme comes first. Thus a host in a link with a
#: scheme is part of that match, and the pattern does not find it a second time.
_LINK = re.compile(rf"(?P<scheme>{_SCHEMED})|(?P<bare>{_BARE})", re.IGNORECASE)

#: Punctuation that follows a link in a sentence and is not part of the link.
_TRAILING = ".,;:!?'\""

#: Each closing bracket and its opening bracket. A closing bracket ends a link only when
#: the link has no opening bracket for it. Thus ``(see https://example.com/a_(b))`` keeps
#: the ``)`` that is part of the link.
_CLOSERS = {")": "(", "]": "[", "}": "{"}


@cache
def tlds() -> frozenset[str]:
    """All the delegated top-level domains, in lower case.

    The file is read one time, at the first search for a link.
    """
    lines = TLDS_FILE.read_text(encoding="ascii").splitlines()
    return frozenset(line.strip().lower() for line in lines if line.strip()[:1] not in ("", "#"))


def _trimmed(link: str) -> str:
    """``link`` without the sentence punctuation after it, or a bracket that it did not open."""
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
    """All the links in ``text`` as the URLs that they open, in order, and each link once.

    Punctuation after a link is part of the sentence, not of the link. A link without a
    scheme is returned with ``https://`` at its start. Thus ``meshterm.net`` and
    ``https://meshterm.net`` in one message are the same link, and the list has it once.
    """
    found: list[str] = []
    for match in _LINK.finditer(text):
        link = _trimmed(match.group())
        if match.group("scheme"):
            if link.endswith("://"):
                continue  # only a scheme, with no text after it
        else:
            host = match.group("host").lower()
            if not host.startswith("www.") and match.group("tld").lower() not in tlds():
                continue
            link = "https://" + link
        if link not in found:
            found.append(link)
    return found


__all__ = ["TLDS_FILE", "tlds", "urls"]
