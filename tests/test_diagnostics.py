# SPDX-License-Identifier: Apache-2.0
"""The Diagnostics block: what it says, what it refuses to say, and how it says it.

The privacy tests here are the load-bearing ones. This block exists to be pasted in public
by somebody who has not read it, so "no secret is in it" has to be a property of the
feature rather than a habit of whoever last edited it. Two tests hold that line from
different sides: one plants real secrets in the stores that hold them and asserts none
came out, and one refuses the *names* such a field would go under — so a value the suite
has no way to plant is still caught by the key it would arrive as.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from meshterm.context import AppContext
from meshterm.core import hostinfo
from meshterm.core.admin_store import AdminStore
from meshterm.core.channel_store import ChannelStore
from meshterm.core.config import Settings
from meshterm.core.device_store import DeviceStore
from meshterm.core.models import Contact
from meshterm.core.preferences import Preferences
from meshterm.persistence.repository import Repository
from meshterm.tools.diagnostics import build_report, markdown_source
from meshterm.ui import fields
from meshterm.ui.about import AboutPage
from meshterm.ui.markdown import render_markdown
from meshterm.ui.renderers import (
    JsonRenderer,
    PlainRenderer,
    facts_pairs,
    markdown_blocks,
)
from meshterm.ui.report import Facts, Listing

#: Planted into the stores that really hold this kind of value. Each is the kind of thing
#: the block must never carry: one administers a repeater, one decrypts a channel.
_ADMIN_PASSWORD = "correct-horse-battery"
_CHANNEL_SECRET = "8f" * 16

#: Key *segments* no field in this block may be built from — matched on the underscore
#: parts of a key rather than as substrings, so ``platform`` is not read as a latitude.
#: The first row names a secret and the second names a person or a place; ``key`` and
#: ``name`` close the set, because the block identifies no node at all, not even ours.
_FORBIDDEN_SEGMENTS = frozenset(
    {
        "password",
        "passphrase",
        "secret",
        "private",
        "privkey",
        "pin",
        "token",
        "credential",
        "contact",
        "contacts",
        "lat",
        "latitude",
        "lon",
        "longitude",
        "position",
        "message",
        "messages",
        "key",
        "name",
    }
)


@pytest.fixture
def ctx(tmp_path: Path) -> AppContext:
    """A context on a throwaway home, with a secret planted in the stores that hold them."""
    settings = Settings(config_dir=tmp_path)
    admin_store = AdminStore(tmp_path / "admin.json")
    admin_store.remember(
        Contact(name="Hilltop-Repeater", public_key="3d" * 32, key_prefix="3d63c642"),
        _ADMIN_PASSWORD,
    )
    channel_store = ChannelStore(tmp_path / "channels.json")
    channel_store.remember("ab" * 32, 1, "Lakeside", bytes.fromhex(_CHANNEL_SECRET))
    return AppContext(
        console=Console(),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "device.json"),
        admin_store=admin_store,
        channel_store=channel_store,
        preferences=Preferences(tmp_path / "preferences.toml", {"log_level": "DEBUG"}),
        mock=True,
    )


# --- what it must never say -----------------------------------------------------------


def test_no_planted_secret_reaches_either_face(ctx: AppContext) -> None:
    """A remembered admin password and a channel secret appear in neither face.

    The point of the feature: a reporter pastes this into a public issue without reading
    it first.
    """
    report = _report(ctx)
    for secret in (_ADMIN_PASSWORD, _CHANNEL_SECRET):
        assert secret not in _plain(report)
        assert secret not in _machine(report)


def test_no_field_is_named_after_a_secret_or_a_person(ctx: AppContext) -> None:
    """No key in the block reads like a credential, a position or somebody's identity.

    The durable half of the guarantee. A value this suite cannot plant — a PIN read live
    off a radio, a private key exported on demand — still has to arrive under a name, and
    every name a secret would plausibly take is refused here.
    """
    for key in _keys(_report(ctx)):
        offending = _FORBIDDEN_SEGMENTS & set(key.lower().split("_"))
        assert not offending, f"{key!r} is named after {', '.join(sorted(offending))}"


def test_the_mesh_is_described_only_in_aggregate(ctx: AppContext) -> None:
    """The mesh reaches the block as counts, never as a row anybody could be named in.

    Every table is *counted*, and the count is an integer: the moment a listing starts
    carrying a row out of one of those tables, a contact's name or a message body is one
    edit away from the clipboard.
    """
    tables = _flat(_report(ctx))["tables"]
    assert tables, "the schema should always have tables to count"
    assert all(set(row) == {"table", "rows"} for row in tables)
    assert all(isinstance(row["rows"], int) for row in tables)


# --- what it must say -----------------------------------------------------------------


def test_states_the_facts_a_bug_report_opens_with(ctx: AppContext) -> None:
    """Version, install shape, host, terminal and device all reach the block."""
    from meshterm import __version__

    values = _flat(_report(ctx))
    assert values["meshterm"] == __version__
    assert values["install"] in {"frozen", "source", "package"}
    assert values["os"] and values["python"] and values["arch"]
    assert "x" in values["terminal_size"]
    assert values["platform"] in {"regular", "picocalc-lyra"}
    # The simulator answers like a radio, so the device half is populated rather than
    # skipped: this is the shape a real connected run produces.
    assert values["connected"] is True
    assert values["transport"] == "mock"
    assert values["firmware"]


def test_an_unreachable_radio_becomes_a_fact_not_a_failure(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A radio that will not connect leaves the block intact and says why.

    The report about a radio that never connects is exactly the one most worth filing, so
    the device read is best-effort: the failure lands in ``error`` and every other fact
    still arrives.
    """

    async def refuse(self):
        raise RuntimeError("no companion answered on COM7")

    monkeypatch.setattr(AppContext, "device", refuse)
    values = _flat(_report(ctx))
    assert values["connected"] is False
    assert "COM7" in values["error"]
    assert values["firmware"] is None
    assert values["meshterm"]  # the rest of the block is untouched


def test_only_overridden_preferences_are_listed(ctx: AppContext) -> None:
    """The preference listing carries the disagreements with the defaults, and only those.

    A dump of all forty would bury the one the reporter actually changed; the defaults are
    in the source and identical for everyone.
    """
    assert _flat(_report(ctx))["preferences"] == [{"preference": "log_level", "value": "DEBUG"}]


# --- how it says it -------------------------------------------------------------------


def test_the_page_and_the_command_line_state_the_same_facts(ctx: AppContext) -> None:
    """Every key/value the page draws is one the scripted face prints, in the same words.

    The two faces share :func:`~meshterm.ui.renderers.facts_pairs` precisely so a field
    added to the report reaches both without anyone remembering to, and this is what would
    catch the page growing a projection of its own.
    """
    report = _report(ctx)
    printed = _plain(report)
    for block in report:
        if isinstance(block, Facts):
            for key, value in facts_pairs(block):
                assert key in printed
                assert value in printed


def test_the_page_is_a_written_page_with_landmarks(ctx: AppContext) -> None:
    """The page is markdown through the About-page renderer, and its sections are landmarks.

    The sections are the point of the format: a heading pins to the top row while its own
    rows scroll under it, the section jumps step by them, and the PicoCalc lane earns its
    ``Sect up``/``Sect down`` pair for the same reason a grouped list does.
    """
    page = _page(ctx)
    assert isinstance(page, AboutPage)
    assert page.bare is False
    assert {"Build", "Host", "Terminal", "Radio", "Storage"} <= set(page._doc.sections)


def test_the_save_is_advertised_on_both_platforms(ctx: AppContext) -> None:
    """The verb is named in the footer hint and on the lane's one free slot.

    Never in the page's body, which belongs to the report — and never nowhere, which is
    what a bare frame would have forced. F1/F2 are the section pair and F4/F5 the pager,
    so F3 is the slot the About pages leave free.
    """
    from meshterm.ui.diagnostics import SAVE_ACTION, SAVE_KEY

    page = _page(ctx)
    assert f"{SAVE_KEY} save" in page.footer_hint
    assert page.footer_hint.endswith("Esc back")
    assert page.picocalc_lyra_lane[2] is not None
    assert page.picocalc_lyra_lane[2].label == "Save"
    assert page.picocalc_lyra_lane[2].action == SAVE_ACTION


# --- saving it ------------------------------------------------------------------------


def test_the_saved_file_is_the_document_the_page_shows(ctx: AppContext, tmp_path: Path) -> None:
    """The file holds the very markdown the page was built from — one source, two places."""
    from meshterm.tools.diagnostics import markdown_source, write_markdown

    source = markdown_source(_report(ctx))
    written = write_markdown(source, tmp_path / "out.md")
    assert written.read_text(encoding="utf-8") == source
    assert source.startswith("# MeshTerm diagnostics")


def test_saving_creates_the_directory_it_needs(ctx: AppContext, tmp_path: Path) -> None:
    """A destination whose parent does not exist yet is made rather than refused."""
    from meshterm.tools.diagnostics import markdown_source, write_markdown

    written = write_markdown(markdown_source(_report(ctx)), tmp_path / "a" / "b" / "out.md")
    assert written.is_file()


def test_the_key_and_the_chip_both_save_and_answer_in_a_popup(
    ctx: AppContext, tmp_path: Path
) -> None:
    """Either affordance writes the file and reports the path in an acknowledgement popup.

    The chip dispatches the same action the key does, so it is not a second implementation
    of the same verb.
    """
    from meshterm.ui.diagnostics import SAVE_ACTION, SAVE_KEY

    for trigger, target in ((("text", SAVE_KEY), "key.md"), ((SAVE_ACTION, ""), "chip.md")):
        path = tmp_path / target
        session = _Session()
        page = _page(ctx, session=session, save_path=path)
        page.handle(*trigger)
        assert path.is_file()
        assert len(session.messages) == 1
        assert "\u2713" in session.messages[0]
        assert str(path) in session.messages[0]


def test_a_save_that_fails_is_reported_not_raised(ctx: AppContext, tmp_path: Path) -> None:
    """An unwritable destination reports itself and leaves the page readable.

    The page is still there to be read and copied off, which is the fallback that matters,
    so a failed write must never take it down. The mark is also what turns the popup red.
    """
    from meshterm.ui.diagnostics import SAVE_KEY

    # A path whose "directory" is an existing file: mkdir and write both refuse, on every
    # OS, without needing permissions the suite cannot count on.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    session = _Session()
    page = _page(ctx, session=session, save_path=blocker / "inside" / "out.md")

    page.handle("text", SAVE_KEY)
    assert "\u2717" in session.messages[0]
    assert "could not save" in session.messages[0]
    assert "Build" in _body(page)  # the page itself is untouched


def test_the_popup_carries_its_tone_as_spans_not_as_markup(ctx: AppContext, tmp_path: Path) -> None:
    """The message is a styled Text, so no tag is ever printed and the border reads right.

    A markup *string* reaches this dialog unparsed: the reader sees the literal tags and a
    failure is framed in the success colour, because the border is picked from the
    message's spans. The mark carries the tone; nothing else needs to.
    """
    from meshterm.ui.diagnostics import SAVE_KEY
    from meshterm.ui.tui.session import _message_border

    for target, expected in ((tmp_path / "ok.md", "accent"), (_blocked(tmp_path), "err")):
        session = _Session(keep=True)
        page = _page(ctx, session=session, save_path=target)
        page.handle("text", SAVE_KEY)
        message = session.shown[0]
        assert isinstance(message, Text)
        assert "[/" not in message.plain and "[ok]" not in message.plain
        assert _message_border(message) == expected


def test_a_held_save_key_does_not_stack_popups(ctx: AppContext, tmp_path: Path) -> None:
    """One save at a time: a second press while one is in flight is ignored."""
    from meshterm.ui.diagnostics import SAVE_KEY

    session = _Session(defer=True)
    page = _page(ctx, session=session, save_path=tmp_path / "out.md")
    page.handle("text", SAVE_KEY)
    page.handle("text", SAVE_KEY)
    assert len(session.started) == 1


def test_another_letter_falls_through_to_the_pager(ctx: AppContext, tmp_path: Path) -> None:
    """Any other letter is the scroll handler's, not the save's."""
    session = _Session()
    page = _page(ctx, session=session, save_path=tmp_path / "out.md")
    page.handle("text", "q")
    assert not (tmp_path / "out.md").exists()
    assert session.messages == []


# --- the markdown face ------------------------------------------------------------------

#: A value that is markdown in every direction: backslashes markdown reads as escapes,
#: asterisks and underscores it reads as emphasis. None of it may be reinterpreted.
_HOSTILE = r"C:\Users\*jp*\_meshterm_"


def test_every_value_survives_markdown_untouched() -> None:
    """Values are code spans, so a path's backslashes and stars arrive as themselves.

    A path, or an error message a stranger's firmware wrote, is not markdown and must never
    be silently read as it. A value already holding a backtick gets a longer fence.
    """
    source = markdown_blocks(
        (
            Facts(
                key="x",
                caption="X",
                fields=(fields.word("p", "p"), fields.word("q", "q")),
                values={"p": _HOSTILE, "q": "a `backtick` inside"},
            ),
        )
    )
    drawn = _render(render_markdown(source))
    assert _HOSTILE in drawn
    assert "a `backtick` inside" in drawn


def test_a_caption_becomes_the_section_heading() -> None:
    """A block's caption is its ``##``; a block without one is drawn with no heading."""
    assert markdown_blocks((Facts(key="x", caption="Radio", fields=()),)).startswith("## Radio")
    assert not markdown_blocks((Facts(key="x", fields=()),)).startswith("#")


def test_an_empty_section_says_so_rather_than_vanishing() -> None:
    """An absence is a fact here: "no preferences changed" is something to act on.

    The plain face drops an empty listing because a record stream has nothing to say about
    one. A document does — a section silently missing only raises the question of whether
    it was ever built.
    """
    empty = Listing(key="preferences", caption="Changed preferences", columns=(), rows=[])
    assert markdown_blocks((empty,)).endswith("*none*")
    assert "none" in _render(render_markdown(markdown_blocks((empty,))))


# --- the host probe -------------------------------------------------------------------


@pytest.mark.parametrize(
    "environ,expected",
    [
        ({"WT_SESSION": "abc", "TERM": "xterm"}, "Windows Terminal"),
        ({"TERM_PROGRAM": "Apple_Terminal", "TERM": "xterm-256color"}, "Terminal.app"),
        ({"TERM_PROGRAM": "vscode"}, "VS Code"),
        # A terminal this module has never heard of still names itself, because
        # TERM_PROGRAM's *value* is the name — which is why that rung exists at all.
        ({"TERM_PROGRAM": "WezTerm"}, "WezTerm"),
        ({"TERM": "linux"}, "linux"),
        ({}, None),
    ],
)
def test_the_terminal_is_identified_from_what_it_says_about_itself(
    environ: dict, expected: str | None
) -> None:
    """Each rung of the ladder, down to the honest ``None`` when nothing names one."""
    assert hostinfo.terminal_program(environ) == expected


def test_an_inner_terminal_wins_over_the_one_hosting_it() -> None:
    """A marker variable beats ``TERM_PROGRAM``, because the inner emulator is the answer.

    VS Code's terminal inherits the outer terminal's environment and adds its own marker;
    reporting the outer one would name a terminal that is not drawing anything.
    """
    env = {"WT_SESSION": "abc", "TERM_PROGRAM": "vscode"}
    assert hostinfo.terminal_program(env) == "Windows Terminal"


def test_ssh_is_read_from_either_variable() -> None:
    """Being over ssh is a fact the block states, because it changes what is knowable."""
    assert hostinfo.terminal({"SSH_TTY": "/dev/pts/0"}).over_ssh is True
    assert hostinfo.terminal({"SSH_CONNECTION": "10.0.0.2 51000 10.0.0.1 22"}).over_ssh is True
    assert hostinfo.terminal({}).over_ssh is False


def test_the_host_block_is_populated_on_whatever_runs_the_suite() -> None:
    """Every host field has an answer, on any OS CI runs this on."""
    machine = hostinfo.host()
    assert machine.install in {"frozen", "source", "package"}
    assert machine.os and machine.arch and machine.python
    assert machine.python.count(".") == 2


# --- the repository aggregates --------------------------------------------------------


def test_table_counts_are_discovered_from_the_schema(tmp_path: Path) -> None:
    """Every table is counted, and SQLite's own internal tables are not.

    Read off ``sqlite_master`` so a table added to the schema starts being reported the day
    it lands. A curated list would go stale silently, and the interesting count is always
    the table nobody expected to be full.
    """
    counts = Repository(tmp_path / "meshterm.db").table_counts()
    assert {"runs", "observations", "messages", "traces"} <= set(counts)
    assert not any(name.startswith("sqlite_") for name in counts)
    assert all(isinstance(n, int) for n in counts.values())


def test_the_observation_span_is_absent_until_something_is_heard(tmp_path: Path) -> None:
    """An empty database has no span, rather than a span of nothing."""
    repo = Repository(tmp_path / "meshterm.db")
    assert repo.observation_span() == (None, None)
    assert repo.failed_run_count() == 0


# --- helpers ---------------------------------------------------------------------------


def _report(ctx: AppContext):
    """Build the report, as both faces do."""
    return asyncio.run(build_report(ctx))


def _flat(report) -> dict:
    """Every fact in the report, flattened the way the JSON face flattens it."""
    flat: dict = {}
    for block in report:
        if isinstance(block, Facts):
            flat.update(block.values)
        else:
            flat[block.key] = list(block.rows)
    return flat


def _keys(report) -> list[str]:
    """Every key the block declares, facts and listing columns alike."""
    keys: list[str] = []
    for block in report:
        if isinstance(block, Facts):
            keys.extend(column.key for column in block.fields)
        else:
            keys.extend(column.key for column in block.columns)
    return keys


def _plain(report) -> str:
    """The whole block as the plain face writes it."""
    console = _console(200)
    with console.capture() as captured:
        PlainRenderer(console).render(report)
    return captured.get()


def _machine(report) -> str:
    """The machine face's document, as text."""
    console = _console(10_000)
    with console.capture() as captured:
        JsonRenderer(console).render(report)
    return captured.get()


def _render(renderable) -> str:
    """A renderable as plain text at the standard width."""
    console = _console(72)
    with console.capture() as captured:
        console.print(renderable)
    return captured.get()


def _console(width: int) -> Console:
    """A capture console with nothing between the content and the string."""
    return Console(width=width, color_system=None, highlight=False, markup=False)


def _blocked(tmp_path: Path) -> Path:
    """A destination no OS will accept: a "directory" that is really a file.

    Refused by both the mkdir and the write, everywhere, without needing permissions the
    suite cannot count on.
    """
    blocker = tmp_path / "blocker"
    if not blocker.exists():
        blocker.write_text("not a directory", encoding="utf-8")
    return blocker / "inside" / "out.md"


def _strip(line: str) -> str:
    """One rendered line with its SGR runs removed."""
    return re.sub(r"\x1b\[[0-9;]*m", "", line)


class _Session:
    """A session stand-in that runs the save flow inline and records the popup.

    ``run_detached`` is where the real session hands a key handler's flow to the event
    loop; here it simply runs it, so a test can press a key and assert on what the reader
    would have been shown. ``defer`` holds the coroutine instead, for the one test that
    needs a save still in flight.
    """

    def __init__(self, *, defer: bool = False, keep: bool = False) -> None:
        """Start with nothing shown and nothing started."""
        self.messages: list[str] = []
        self.shown: list[object] = []
        self.started: list[object] = []
        self._defer = defer
        self._keep = keep

    def invalidate(self) -> None:
        """A repaint request, which a test has no screen to repaint."""

    def run_detached(self, work):
        """Run the flow now (or bank it, with ``defer``)."""
        self.started.append(work)
        if self._defer:
            work.close()
            return None
        return asyncio.run(work)

    async def message_dialog(self, message, *, title: str = "") -> None:
        """Record what the acknowledgement popup would have said."""
        self.messages.append(str(message))
        if self._keep:
            self.shown.append(message)


def _page(ctx: AppContext, *, session: _Session | None = None, save_path: Path | None = None):
    """The page over this context's report, saving wherever the caller wants."""
    from meshterm.ui.diagnostics import DiagnosticsPage

    target = save_path or (ctx.settings.config_dir / "meshterm-diagnostics.md")
    return DiagnosticsPage(
        markdown_source(_report(ctx)), session=session or _Session(), save_path=target
    )


def _body(page) -> str:
    """The page's drawn rows as plain text."""
    return "\n".join(_strip(line) for line in page.render_body(100))
