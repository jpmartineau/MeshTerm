# SPDX-License-Identifier: Apache-2.0
"""About MeshTerm tests: the four pages, their placeholders, and their menu section.

The pages carry no state and no controls, so what is worth pinning is what they *say*
and how they are framed: the live package facts they must never drift from, the
indent block their prose hangs in, the derived footer that only names the pager when
there is something to page, the landmarks their sections leave for the sticky heading
and the section jump, and the section they hang under in the main menu. How markdown
itself is drawn is ``test_markdown``'s subject; width discipline on both platforms is
the gallery's (``test_gallery``).
"""

from __future__ import annotations

import pytest

from meshterm import __author__, __version__, copyright_notice
from meshterm.ui.about import (
    AboutPage,
    about_author,
    about_meshterm,
    join_discord,
    support_project,
)
from tests.conftest import plain


def _page(builder, *, viewport: int = 40, width: int = 72) -> str:
    """Render one page as a reader sees it, with the frame's viewport already recorded."""
    screen = AboutPage("About MeshTerm", builder())
    screen.note_viewport(viewport)
    lines = screen.render_body(width)
    screen.note_metrics(len(lines), viewport)
    return plain(lines)


def test_about_meshterm_leads_with_live_package_facts() -> None:
    """The About page leads with facts read live from the package.

    The version and copyright come from the package itself, so the page can't
    describe a build it isn't running inside.
    """
    text = _page(about_meshterm)

    assert f"MeshTerm v{__version__}" in text
    assert copyright_notice() in text


def test_about_author_names_the_author_from_the_package() -> None:
    """The author line is package metadata too — one spelling, one place to change it."""
    assert __author__ in _page(about_author)


#: Every ``##`` a written page carries, in the order the reader meets them. These are
#: the page's landmarks, so one list answers both questions asked of them below: that
#: they render in order, and that each one is a stop the sticky heading can pin.
PAGE_HEADINGS = [
    (
        about_meshterm,
        (
            "What it is",
            "Where it came from",
            "Platform-specific developments",
            "What the future holds",
            "Licence",
        ),
    ),
    (
        about_author,
        (
            "Who am I IRL",
            "Who am I on the mesh",
            "On the subject of AI assisted development",
            "How to reach me",
        ),
    ),
    (support_project, ("Why it needs support", "Chip in", "Other ways to help")),
]


@pytest.mark.parametrize(("builder", "headings"), PAGE_HEADINGS)
def test_each_page_shows_its_sections(builder, headings) -> None:  # noqa: ANN001
    """The headings are the shape a page is written into — they render, in order."""
    text = _page(builder)

    at = [text.index(heading) for heading in headings]
    assert at == sorted(at), f"{headings} are out of order in the rendered page"


def test_prose_wraps_into_its_own_indented_block() -> None:
    """A wrapped paragraph hangs under its heading rather than falling to column 0.

    *Support MeshTerm* is the page to ask: every one of its sections is plain prose and
    it carries neither of the two blocks that are page frame rather than body (the
    standfirst and the colophon, which sit flush by design), so the only thing entitled
    to column 0 there is a ``##`` heading.
    """
    screen = AboutPage("Support MeshTerm", support_project())
    screen.note_viewport(40)
    # Narrow enough to force every paragraph to wrap several times.
    lines = plain(screen.render_body(28)).splitlines()

    headings = {lines[at].strip() for at, _ in screen._sticky_headers}
    flush = [line.strip() for line in lines if line.strip() and not line.startswith(" ")]
    assert flush, "the page rendered nothing at all"
    assert all(line in headings for line in flush), "prose escaped its indent block"


def test_footer_names_the_pager_only_when_there_is_something_to_page() -> None:
    """The derived hint obeys the rule that a footer never advertises a dead key."""
    screen = AboutPage("About MeshTerm", about_meshterm())

    screen.note_metrics(12, 40)  # the whole page fits — nothing to scroll
    assert screen.footer_hint == "Esc back"

    screen.note_metrics(60, 20)  # taller than the viewport — the pager is live
    assert screen.footer_hint == "↑↓ PgUp/PgDn scroll · Esc back"


def test_page_is_a_full_screen_not_a_floating_view() -> None:
    """These are pages, so Esc reads *back*; a floating read-only view would say *close*."""
    assert AboutPage("About MeshTerm", about_meshterm()).floating is False


def test_the_licence_section_states_the_terms_and_the_credit_it_owes() -> None:
    """This page is where a reader with no shell finds them.

    The terms MeshTerm ships under, the project it stands on, and the map data it draws
    with — the ODbL asks for that credit by name, and a page nobody can read it on is no
    credit at all.
    """
    prose = " ".join(_page(about_meshterm).split())

    owed = (
        "Apache License, Version 2.0",
        "MeshTerm and its logo are trademarks",
        "MeshCore",
        "OpenStreetMap contributors",
        "ODbL",
    )
    for credit in owed:
        assert credit in prose, credit


def test_the_discord_page_carries_the_invite_link_and_a_code_to_scan_it_with() -> None:
    """The Discord page carries both the invite link and a QR code for it.

    The link is the whole page, and on a console with no clipboard the QR is how it
    leaves the screen, so both have to survive the render.
    """
    text = _page(join_discord)

    assert "https://discord.gg/AZwe5Uvb3S" in text
    assert "█" in text, "the scannable code didn't render"


def test_pages_are_written_markdown_that_ships_with_the_package() -> None:
    """Filling a page in is editing its ``.md`` file — no Python has to follow."""
    from meshterm.ui.about import _PAGES

    for name in ("about", "author", "discord", "support"):
        source = (_PAGES / f"{name}.md").read_text(encoding="utf-8")
        assert source.lstrip().startswith("#"), f"{name}.md doesn't open with a heading"


def test_no_package_placeholder_survives_into_the_rendered_page() -> None:
    """The live facts are filled in as the page opens, so none of the braces reach a reader."""
    for builder in (about_meshterm, about_author, join_discord, support_project):
        text = _page(builder)
        assert "{" not in text and "}" not in text, text


@pytest.mark.parametrize(("builder", "headings"), PAGE_HEADINGS)
def test_each_section_heading_is_a_landmark_the_page_can_pin(builder, headings) -> None:  # noqa: ANN001
    """A page is a document, so its ``##`` headings pin and its ^PgUp/^PgDn steps by them.

    The screen records where each one landed while rendering (the same machinery a
    grouped select list uses), which is what makes a heading stay on the top row while
    its own prose scrolls under it.
    """
    screen = AboutPage("About MeshTerm", builder())
    screen.note_viewport(40)
    lines = plain(screen.render_body(72)).splitlines()

    assert [lines[at].strip() for at, _ in screen._sticky_headers] == list(headings)
    # Scrolled one line past its heading, the page pins that heading and nothing else.
    at = screen._sticky_headers[1][0]
    assert plain(screen.sticky_block(at + 1)) == headings[1]


def test_the_console_lane_claims_the_section_step_only_while_the_page_scrolls() -> None:
    """The console lane claims the section step only while the page actually scrolls.

    On the PicoCalc the lane is the only place a chord can advertise itself, but it
    never advertises a key that would do nothing (``CLAUDE.md``) — and a page you can
    see whole has no section to step to.
    """
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform

    set_platform(PICOCALC_LYRA)
    try:
        screen = AboutPage("About MeshTerm", about_meshterm())
        screen.note_metrics(12, 40)  # the whole page fits
        assert [pair.label for pair in screen.picocalc_lyra_lane[:2]] == ["Sect ↑", "Sect ↓"]
        assert not any(pair.enabled for pair in screen.picocalc_lyra_lane[:2])

        screen.note_metrics(60, 20)  # taller than the viewport
        assert all(pair.enabled for pair in screen.picocalc_lyra_lane[:2])
        assert [pair.action for pair in screen.picocalc_lyra_lane[:2]] == [
            "ctrl_pageup",
            "ctrl_pagedown",
        ]
    finally:
        set_platform(REGULAR)


def test_menu_rows_carry_the_icon_and_the_titles_do_not() -> None:
    """Each page has a menu icon; none of it leaks into the title the screen draws."""
    from meshterm.tools import get_tool

    for name in ("about", "about-author", "discord", "support"):
        tool = get_tool(name)
        assert tool is not None
        assert tool.icon, f"{name} has no menu icon"
        assert tool.icon not in tool.title
        assert tool.title.isascii(), f"{name}'s title carries a non-ascii glyph"


def test_every_page_icon_has_a_picocalc_glyph() -> None:
    """No emoji reaches the console: each icon folds to a font character (CLAUDE.md)."""
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
    from meshterm.tools import get_tool
    from meshterm.ui.fontset import FONT_CODEPOINTS
    from meshterm.ui.theme import glyph

    set_platform(PICOCALC_LYRA)
    try:
        for name in ("about", "about-author", "discord", "support"):
            tool = get_tool(name)
            assert tool is not None
            compact = glyph(tool.icon)
            assert compact and compact != tool.icon, f"{name}'s icon is unmapped"
            assert all(ord(ch) in FONT_CODEPOINTS for ch in compact), compact
    finally:
        set_platform(REGULAR)
