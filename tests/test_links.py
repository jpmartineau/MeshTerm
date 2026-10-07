# SPDX-License-Identifier: Apache-2.0
"""Find links in a line of text: with a scheme, and without one where the meaning is clear."""

from __future__ import annotations

from meshterm.core.links import TLDS_FILE, tlds, urls


def test_urls_are_found_whole_once_and_without_the_sentence_around_them() -> None:
    """A URL ends where the punctuation of its sentence starts.

    A URL keeps a bracket that it opened.
    """
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
    """``www.`` or a real TLD shows that the text is a link.

    Each link returns as the https URL that it opens.
    """
    body = (
        "map at meshterm.net/map, flasher at flasher.meshcore.co.uk. "
        "WWW.Example.org (or www.anything.local) and github.com/jpmartineau/MeshTerm?tab=1!"
    )
    assert urls(body) == [
        "https://meshterm.net/map",
        "https://flasher.meshcore.co.uk",
        "https://WWW.Example.org",
        "https://www.anything.local",  # www. is sufficient alone, and .local is not a TLD
        "https://github.com/jpmartineau/MeshTerm?tab=1",
    ]


def test_a_dotted_word_that_is_no_link_stays_text() -> None:
    """A file, a number, an abbreviation, an address, and a path are not links.

    They have no TLD, or they do not start a word.
    """
    body = (
        "see file.txt and notes.pdf, pi is 3.14 and v1.2.3, e.g. this, i.e. that, "
        "mail jp@meshterm.net, open C:/logs/meshterm.log or ./run.sh, @Alice.Smith"
    )
    assert urls(body) == []


def test_the_same_link_written_two_ways_is_found_once() -> None:
    """``meshterm.net`` and ``https://meshterm.net`` open the same page.

    Thus there is one code and not two.
    """
    assert urls("meshterm.net or https://meshterm.net") == ["https://meshterm.net"]
    # A host in a link with a scheme is part of that link. It is never a second link.
    assert urls("https://github.com/x/meshterm.net") == ["https://github.com/x/meshterm.net"]


def test_the_tld_registry_is_ianas_list_read_whole() -> None:
    """The file is the IANA list with its version line.

    The set has all the TLDs in it, in lower case.
    """
    assert TLDS_FILE.read_text(encoding="ascii").startswith("# Version ")
    assert {"com", "net", "org", "dev", "io", "ca", "uk"} <= tlds()
    assert not {"txt", "pdf", "log", "local"} & tlds()
    assert len(tlds()) > 1000
