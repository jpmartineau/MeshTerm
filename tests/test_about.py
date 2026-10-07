# SPDX-License-Identifier: Apache-2.0
"""About MeshTerm tests: the four pages, their placeholders, and their menu section.

The pages have no state and no controls. Thus the tests check what the pages say and how
they are framed. These are the things that the tests check:

- The live package facts, which the pages must always agree with.
- The indent block in which the prose hangs.
- The derived footer, which names the pager only when there is something to page.
- The landmarks that the sections leave for the sticky heading and for the section jump.
- The section of the main menu that contains the pages.

``test_markdown`` tests how markdown is drawn. ``test_gallery`` tests the width on both
platforms.
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
    """Render one page as the user sees it, with the viewport of the frame already noted."""
    screen = AboutPage("About MeshTerm", builder())
    screen.note_viewport(viewport)
    lines = screen.render_body(width)
    screen.note_metrics(len(lines), viewport)
    return plain(lines)


def test_about_meshterm_leads_with_live_package_facts() -> None:
    """The About page starts with facts that it reads live from the package.

    The version and the copyright come from the package itself. Thus the page cannot
    describe a build that is not the build that runs it.
    """
    text = _page(about_meshterm)

    assert f"MeshTerm v{__version__}" in text
    assert copyright_notice() in text


def test_about_author_names_the_author_from_the_package() -> None:
    """The author line is also package metadata: it has one spelling and one place to change."""
    assert __author__ in _page(about_author)


#: Each ``##`` heading of a written page, in the order in which the user meets them. These
#: are the landmarks of the page. One list answers both questions that the tests ask about
#: them: do they render in order, and can the sticky heading pin each one?
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
    """The headings are the shape of a page, and they render in order."""
    text = _page(builder)

    at = [text.index(heading) for heading in headings]
    assert at == sorted(at), f"{headings} are out of order in the rendered page"


def test_prose_wraps_into_its_own_indented_block() -> None:
    """A wrapped paragraph hangs under its heading and does not fall to column 0.

    The test uses *Support MeshTerm*, because each section of this page is plain prose.
    The page has neither of the two blocks that are page frame and not body (the
    standfirst and the colophon, which sit flush on purpose). Thus only a ``##`` heading
    can be at column 0 on this page.
    """
    screen = AboutPage("Support MeshTerm", support_project())
    screen.note_viewport(40)
    # The width is narrow, so that each paragraph wraps more than one time.
    lines = plain(screen.render_body(28)).splitlines()

    headings = {lines[at].strip() for at, _ in screen._sticky_headers}
    flush = [line.strip() for line in lines if line.strip() and not line.startswith(" ")]
    assert flush, "the page rendered nothing at all"
    assert all(line in headings for line in flush), "prose escaped its indent block"


def test_footer_names_the_pager_only_when_there_is_something_to_page() -> None:
    """The derived hint obeys the rule that a footer never advertises a key that does nothing."""
    screen = AboutPage("About MeshTerm", about_meshterm())

    screen.note_metrics(12, 40)  # The whole page fits, so there is nothing to scroll.
    assert screen.footer_hint == "Esc back"

    screen.note_metrics(60, 20)  # The page is taller than the viewport, so the pager is live.
    assert screen.footer_hint == "↑↓ PgUp/PgDn scroll · Esc back"


def test_page_is_a_full_screen_not_a_floating_view() -> None:
    """These are pages, so the Esc verb is "back". A floating read-only view has "close"."""
    assert AboutPage("About MeshTerm", about_meshterm()).floating is False


def test_the_licence_section_states_the_terms_and_the_credit_it_owes() -> None:
    """This page is where a user who has no shell finds the terms and the credit.

    The page names the terms under which MeshTerm ships, the project on which it is
    built, and the map data with which it draws. The ODbL asks for that credit by name.
    A credit on a page that nobody can read is not a credit.
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

    The link is the whole page. On a console that has no clipboard, the QR code is the
    way for the link to leave the screen. Thus both must stay in the render.
    """
    text = _page(join_discord)

    assert "https://discord.gg/AZwe5Uvb3S" in text
    assert "█" in text, "the scannable code didn't render"


def test_pages_are_written_markdown_that_ships_with_the_package() -> None:
    """To fill a page in, edit its ``.md`` file. No Python change is necessary."""
    from meshterm.ui.about import _PAGES

    for name in ("about", "author", "discord", "support"):
        source = (_PAGES / f"{name}.md").read_text(encoding="utf-8")
        assert source.lstrip().startswith("#"), f"{name}.md doesn't open with a heading"


def test_no_package_placeholder_survives_into_the_rendered_page() -> None:
    """MeshTerm fills in the live facts as the page opens, so no brace reaches the user."""
    for builder in (about_meshterm, about_author, join_discord, support_project):
        text = _page(builder)
        assert "{" not in text and "}" not in text, text


@pytest.mark.parametrize(("builder", "headings"), PAGE_HEADINGS)
def test_each_section_heading_is_a_landmark_the_page_can_pin(builder, headings) -> None:  # noqa: ANN001
    """A page is a document, so its ``##`` headings pin and ^PgUp/^PgDn step by them.

    While the screen renders, it records where each heading is. A grouped select list
    uses the same machinery. Thus a heading stays on the top row while its prose scrolls
    under it.
    """
    screen = AboutPage("About MeshTerm", builder())
    screen.note_viewport(40)
    lines = plain(screen.render_body(72)).splitlines()

    assert [lines[at].strip() for at, _ in screen._sticky_headers] == list(headings)
    # When the page scrolls one line past a heading, it pins that heading and no other.
    at = screen._sticky_headers[1][0]
    assert plain(screen.sticky_block(at + 1)) == headings[1]


def test_the_console_lane_claims_the_section_step_only_while_the_page_scrolls() -> None:
    """The console lane claims the section step only while the page actually scrolls.

    On the PicoCalc, the lane is the only place where a chord can advertise itself. But
    the lane never advertises a key that does nothing (``CLAUDE.md``). A page that is
    visible whole has no section to step to.
    """
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform

    set_platform(PICOCALC_LYRA)
    try:
        screen = AboutPage("About MeshTerm", about_meshterm())
        screen.note_metrics(12, 40)  # The whole page fits.
        assert [pair.label for pair in screen.picocalc_lyra_lane[:2]] == ["Sect ↑", "Sect ↓"]
        assert not any(pair.enabled for pair in screen.picocalc_lyra_lane[:2])

        screen.note_metrics(60, 20)  # The page is taller than the viewport.
        assert all(pair.enabled for pair in screen.picocalc_lyra_lane[:2])
        assert [pair.action for pair in screen.picocalc_lyra_lane[:2]] == [
            "ctrl_pageup",
            "ctrl_pagedown",
        ]
    finally:
        set_platform(REGULAR)


def test_menu_rows_carry_the_icon_and_the_titles_do_not() -> None:
    """Each page has a menu icon, and the icon does not leak into the title of the screen."""
    from meshterm.tools import get_tool

    for name in ("about", "about-author", "discord", "support"):
        tool = get_tool(name)
        assert tool is not None
        assert tool.icon, f"{name} has no menu icon"
        assert tool.icon not in tool.title
        assert tool.title.isascii(), f"{name}'s title carries a non-ascii glyph"


def test_every_page_icon_has_a_picocalc_glyph() -> None:
    """No emoji reaches the console. Each icon folds to a character of the font (CLAUDE.md)."""
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
