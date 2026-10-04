# SPDX-License-Identifier: Apache-2.0
"""The markdown renderer: what each construct becomes in MeshTerm's visual language.

The About pages are written rather than composed (see :mod:`meshterm.ui.markdown`), so
what is worth pinning here is the *translation* — that a heading takes the same accent
every body section uses, that prose and lists hang in their own block instead of falling
back to column 0, that a rail survives wrapping, that a link says where it goes on a
terminal where nothing is clickable — and that the whole vocabulary survives the
PicoCalc's 512 glyphs and 16 palette slots.
"""

from __future__ import annotations

import pytest
from rich.cells import cell_len
from rich.padding import Padding
from rich.text import Text

from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
from meshterm.ui.fontset import FONT_CODEPOINTS
from meshterm.ui.markdown import MarkdownDoc, render_markdown
from meshterm.ui.tui.render import render_lines
from tests.conftest import plain

#: One page exercising every construct the renderer knows, used wherever a test wants
#: the whole vocabulary at once rather than one mark of it.
KITCHEN_SINK = """# MeshTerm *v0.1.0*

a terminal companion for MeshCore LoRa mesh devices

## What it is · the short version

MeshTerm is a **full-screen** companion for *MeshCore* devices, with `traces`,
maps and a ~~telepathic~~ courier queue over the top.

### Under the hood

- contacts, sorted however you like
  - discovered nodes, and the ones you added
- a map you can walk with the arrow keys

1. plug the radio in
2. run `meshterm`

> Nothing here transmits without being asked.

```
meshterm --mock --platform picocalc-lyra
```

| Platform | Cols | Colours |
|---|---:|---|
| regular | 72 | truecolor |

Find it at [the repo](https://github.com/example/meshterm).

---

Copyright © 2026 Somebody
"""


def _lines(source: str, width: int = 72) -> list[str]:
    """The page as a reader sees it: plain text, one entry per rendered line.

    Trailing padding is dropped — an indented block pads out to the full width, which no
    reader can see and no assertion here is about.
    """
    rendered = plain(render_lines(render_markdown(source), width)).splitlines()
    return [line.rstrip() for line in rendered]


def _texts(source: str) -> list[Text]:
    """Every block of the page that is a run of text, unwrapped from its indent."""
    texts = []
    for block in render_markdown(source).blocks:
        renderable = block.renderable
        while isinstance(renderable, Padding):
            renderable = renderable.renderable
        if isinstance(renderable, Text):
            texts.append(renderable)
    return texts


def _spans(source: str) -> list[tuple[str, str]]:
    """Every styled run in the page's block texts, as ``(text, style)`` pairs."""
    return [
        (text.plain[span.start : span.end], str(span.style))
        for text in _texts(source)
        for span in text.spans
    ]


# -- headings and the page frame ------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "style"),
    [("# Title", "brand"), ("## Section", "accent"), ("### Sub", "md.strong")],
)
def test_each_heading_level_takes_the_hue_its_rank_calls_for(source, style) -> None:  # noqa: ANN001
    """Each heading level takes the hue its rank calls for.

    ``#`` is the page's own name, ``##`` a section in the app's body accent, and
    ``###`` a sub-heading inside one.
    """
    assert _spans(source)[0][1] == style


def test_a_headings_dot_opens_the_standard_muted_aside() -> None:
    """``## Title · note`` is the ``accent title · muted note`` shape body headings use."""
    doc = render_markdown("## What it is · the short version")
    heading = doc.blocks[0].renderable

    assert heading.plain == "What it is  ·  the short version"
    assert [str(span.style) for span in heading.spans][-1] == "muted"


def test_emphasis_inside_a_heading_is_its_qualifier() -> None:
    """That is how the wordmark carries a muted version beside it, in one line of source."""
    doc = render_markdown("# MeshTerm *v0.1.0*")

    assert doc.blocks[0].renderable.plain == "MeshTerm v0.1.0"
    assert [str(span.style) for span in doc.blocks[0].renderable.spans] == ["brand", "muted"]


def test_the_page_frame_sits_flush_and_muted() -> None:
    """The page frame sits flush and muted.

    The standfirst under the title and the colophon under the closing rule describe
    the page rather than saying anything in it, so both recede and neither is
    indented.
    """
    source = "# Title\n\nthe standfirst\n\n## Section\n\nbody\n\n---\n\nthe colophon\n"
    lines = _lines(source)
    styles = dict(_spans(source))

    assert lines[1] == "the standfirst"  # flush, not hanging at INDENT
    assert lines[-1] == "the colophon"
    assert styles["the standfirst"] == "muted" and styles["the colophon"] == "muted"


def test_a_paragraph_is_only_a_colophon_at_the_foot_of_the_page() -> None:
    """A rule mid-page is just a rule; the paragraph under it is ordinary prose."""
    lines = _lines("## Section\n\n---\n\nstill body\n\nand more\n")

    assert "  still body" in lines


# -- prose, lists, rails --------------------------------------------------------------


def test_prose_hangs_under_its_heading_on_every_wrapped_line() -> None:
    """A continuation line falling back to column 0 would read as a new, unheaded thought."""
    lines = [line for line in _lines("## S\n\n" + "word " * 40, 30) if line.strip()]

    assert len(lines) > 3  # it really did wrap
    assert all(line.startswith("  ") for line in lines[1:]), lines


def test_a_wrapped_list_item_aligns_under_its_own_text() -> None:
    """The bullet is a gutter, so the item's second line starts where its first line did."""
    lines = [line for line in _lines("- " + "word " * 20, 30) if line.strip()]

    assert lines[0].startswith("  • word")
    assert all(line.startswith("    word") for line in lines[1:]), lines


def test_a_nested_list_steps_in_under_the_item_it_belongs_to() -> None:
    """And takes the sub-item bullet, so depth reads without counting spaces."""
    lines = _lines("- parent\n  - child\n")

    assert lines[0] == "  • parent"
    assert lines[1].startswith("    ▸ child")


def test_a_numbered_list_counts_from_its_own_start() -> None:
    """Markdown's ``3.`` means three, and a two-digit run keeps its numbers aligned."""
    lines = [line for line in _lines("9. nine\n10. ten\n") if line.strip()]

    assert lines[0].startswith("  9.  nine")
    assert lines[1].startswith("  10. ten")


def test_a_quote_keeps_its_rail_down_every_line_it_wraps_to() -> None:
    """A quote keeps its rail down every line it wraps to.

    A rail that only marked the first line would make a three-line quote look like
    one quoted line followed by two loose ones.
    """
    lines = [line for line in _lines("> " + "word " * 20, 30) if line.strip()]

    assert len(lines) > 2
    assert all(line.startswith("  │ ") for line in lines), lines


def test_a_fenced_block_keeps_its_own_lines() -> None:
    """Code is quoted verbatim, one rendered line per source line."""
    lines = [line for line in _lines("```\none\ntwo\n```\n") if line.strip()]

    assert [line.rstrip() for line in lines] == ["  │ one", "  │ two"]


def test_a_qr_fence_draws_the_code_rather_than_the_url_inside_it() -> None:
    """The one fence that isn't quoted: its body is a link, and the page shows the code.

    Drawn live from the fence, never pasted in as half-block art — art loses a row whose
    first module is light, because that row starts with a space and markdown eats it.
    """
    source = """```qr
https://example.com/x
```
"""
    lines = [line for line in _lines(source) if line.strip()]

    assert lines, "the fence rendered nothing"
    assert not any("example.com" in line for line in lines), "the URL was printed as text"
    assert all(set(line.strip()) <= set("█▀▄ ") for line in lines), lines
    # Two of the three finder squares sit at either end of the code's first module row.
    top = lines[0].strip()
    assert top.startswith("█▀▀▀▀▀█") and top.endswith("█▀▀▀▀▀█"), top


def test_a_rule_runs_the_full_width_of_the_page() -> None:
    """``---`` is a page rule, not an indented one — it separates, so it spans."""
    line = next(line for line in _lines("a\n\n---\n\nb\n", 40) if "─" in line)

    assert line == "─" * 40


# -- inline marks ---------------------------------------------------------------------


def test_each_inline_mark_takes_its_own_style() -> None:
    """Strong, emphasis, code and a struck-out run are four voices, not one."""
    styles = dict(_spans("**bold** *soft* `code` ~~gone~~\n"))

    assert styles["bold"] == "md.strong"
    assert styles["soft"] == "md.em"
    assert styles["code"] == "md.code"
    assert styles["gone"] == "md.strike"


def test_a_link_shows_where_it_goes() -> None:
    """A link shows where it goes.

    Nothing is clickable on a framebuffer console, so a link that reads "the repo" has
    to name the repo or it says nothing at all — and with the scheme stripped, it is
    noise.
    """
    text = _texts("see [the repo](https://www.github.com/example/meshterm/)\n")[0]

    assert text.plain == "see the repo github.com/example/meshterm"


def test_a_link_that_already_says_its_target_says_it_once() -> None:
    """A bare URL or an autolink is its own label; repeating it would just eat cells."""
    text = _texts("mail <hello@example.com> or <https://example.com>\n")[0]

    assert text.plain == "mail hello@example.com or https://example.com"


def test_a_table_aligns_the_way_its_markdown_says() -> None:
    """``---:`` is a right-aligned column, and the header keeps the body's own hairline."""
    lines = [line for line in _lines("| A | B |\n|---|--:|\n| 1 | 22222 |\n", 30) if line.strip()]

    assert lines[0].split() == ["A", "B"]
    assert "─" in lines[1]
    assert lines[2].rstrip().endswith("22222")


# -- the document itself --------------------------------------------------------------


def test_sections_are_the_pages_h2_headings() -> None:
    """The landmarks a sticky heading and a section jump step by."""
    doc = render_markdown("# Title\n\n## One\n\na\n\n### deeper\n\n## Two\n\nb\n")

    assert doc.sections == ("One", "Two")


def test_a_page_written_entirely_in_top_level_headings_still_has_sections() -> None:
    """One authoring style shouldn't silently cost a page its landmarks."""
    assert render_markdown("# One\n\na\n\n# Two\n\nb\n").sections == ("One", "Two")


def test_the_document_is_a_plain_renderable_too() -> None:
    """The scripted CLI prints the same page it opens (``meshterm about``)."""
    assert isinstance(render_markdown(KITCHEN_SINK), MarkdownDoc)
    assert _lines(KITCHEN_SINK)[0] == "MeshTerm v0.1.0"


# -- the console --------------------------------------------------------------------


def test_every_construct_survives_the_picocalc() -> None:
    """The whole vocabulary inside 53 cells, the 512-glyph font and the 16 slots.

    The gallery makes this promise for the About pages themselves; this makes it for
    every construct a page *could* grow, so a table or a code fence added to one of them
    is a known quantity rather than tofu found on-device.
    """
    set_platform(PICOCALC_LYRA)
    try:
        rendered = render_lines(render_markdown(KITCHEN_SINK), PICOCALC_LYRA.readable_cols)
    finally:
        set_platform(REGULAR)

    for index, line in enumerate(rendered):
        assert cell_len(plain(line)) <= PICOCALC_LYRA.readable_cols, (index, line)
        assert "[38;2;" not in line and "[38;5;" not in line, (index, line)
        strays = {ch for ch in plain(line) if ord(ch) >= 0x20 and ord(ch) not in FONT_CODEPOINTS}
        assert not strays, (index, sorted(strays), line)
