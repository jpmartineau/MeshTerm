# SPDX-License-Identifier: Apache-2.0
"""Finding links in a line of text — with a scheme, and without one where it is plain."""

from __future__ import annotations

from meshterm.core.links import TLDS_FILE, tlds, urls


def test_urls_are_found_whole_once_and_without_the_sentence_around_them() -> None:
    """A URL ends where its sentence's punctuation begins, and keeps a bracket it opened."""
    body = (
        "map at https://meshterm.net. see (https://en.wikipedia.org/wiki/Foo_(bar)), "
        "'https://example.org/a?b=1' and meshcore://channel/add?name=Ops&secret=ab — "
        "again https://meshterm.net! nothing here: https:// or note:x"
    )
    assert urls(body) == [
        "https://meshterm.net",
        "https://en.wikipedia.org/wiki/Foo_(bar)",
        "https://example.org/a?b=1",
        "meshcore://channel/add?name=Ops&secret=ab",
    ]


def test_a_link_without_a_scheme_is_found_by_www_or_a_delegated_tld() -> None:
    """``www.`` or a real TLD says *link*; each comes back as the https URL it opens."""
    body = (
        "map at meshterm.net/map, flasher at flasher.meshcore.co.uk. "
        "WWW.Example.org (or www.anything.local) and github.com/jpmartineau/MeshTerm?tab=1!"
    )
    assert urls(body) == [
        "https://meshterm.net/map",
        "https://flasher.meshcore.co.uk",
        "https://WWW.Example.org",
        "https://www.anything.local",  # www. is enough on its own; .local is no TLD
        "https://github.com/jpmartineau/MeshTerm?tab=1",
    ]


def test_a_dotted_word_that_is_no_link_stays_text() -> None:
    """A file, a number, an abbreviation, an address, a path: no TLD, or not a word's start."""
    body = (
        "see file.txt and notes.pdf, pi is 3.14 and v1.2.3, e.g. this, i.e. that, "
        "mail jp@meshterm.net, open C:/logs/meshterm.log or ./run.sh, @Alice.Smith"
    )
    assert urls(body) == []


def test_the_same_link_written_two_ways_is_found_once() -> None:
    """``meshterm.net`` and ``https://meshterm.net`` open the same page — one code, not two."""
    assert urls("meshterm.net or https://meshterm.net") == ["https://meshterm.net"]
    # A host inside a schemed link is part of it, never a second link of its own.
    assert urls("https://github.com/x/meshterm.net") == ["https://github.com/x/meshterm.net"]


def test_the_tld_registry_is_ianas_list_read_whole() -> None:
    """The file is IANA's, version line and all; the set is every TLD in it, lower case."""
    assert TLDS_FILE.read_text(encoding="ascii").startswith("# Version ")
    assert {"com", "net", "org", "dev", "io", "ca", "uk"} <= tlds()
    assert not {"txt", "pdf", "log", "local"} & tlds()
    assert len(tlds()) > 1000
