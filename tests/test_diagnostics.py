# SPDX-License-Identifier: Apache-2.0
"""The Diagnostics block: what it says, what it does not say, and how it says it.

The privacy tests are the most important tests in this file. A user can paste this block
in public without a read of it. Thus "no secret is in it" must be a property of the
feature, not a habit of the person who last changed the code. Two tests hold this
property from different sides. One test puts real secrets in the stores that keep them,
and asserts that no secret is in the output. The other test refuses the names that such a
field can have. Thus a value that the suite cannot plant is still caught by the key that
it arrives under.
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

#: The tests put these values in the stores that really keep this type of value. The block
#: must never have a value of this type. One value administers a repeater. One value
#: decrypts a channel.
_ADMIN_PASSWORD = "correct-horse-battery"
_CHANNEL_SECRET = "8f" * 16

#: The key segments that no field in this block can have. The test matches the parts of a
#: key between underscores, not substrings, so ``platform`` is not read as a latitude. The
#: first words name a secret. The next words name a person or a place. ``key`` and ``name``
#: complete the set, because the block does not identify a node, not even our node.
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
    """A context on a temporary home, with a secret in each store that keeps secrets."""
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
    """A stored admin password and a channel secret are in neither face.

    This is the purpose of the feature: a user can paste the block into a public issue
    without a read of it first.
    """
    report = _report(ctx)
    for secret in (_ADMIN_PASSWORD, _CHANNEL_SECRET):
        assert secret not in _plain(report)
        assert secret not in _machine(report)


def test_no_field_is_named_after_a_secret_or_a_person(ctx: AppContext) -> None:
    """No key in the block looks like a credential, a position, or the identity of a person.

    This is the lasting half of the guarantee. The suite cannot plant some values: a PIN
    that the code reads live from a radio, or a private key that the code exports on
    demand. Such a value must still arrive under a name. This test refuses each name that a
    secret can have.
    """
    for key in _keys(_report(ctx)):
        offending = _FORBIDDEN_SEGMENTS & set(key.lower().split("_"))
        assert not offending, f"{key!r} is named after {', '.join(sorted(offending))}"


def test_the_mesh_is_described_only_in_aggregate(ctx: AppContext) -> None:
    """The mesh arrives in the block as counts, never as a row that can name a person.

    The block counts each table, and the count is an integer. If a listing starts to carry
    a row from one of these tables, the name of a contact or the body of a message is one
    edit away from the clipboard.
    """
    tables = _flat(_report(ctx))["tables"]
    assert tables, "the schema should always have tables to count"
    assert all(set(row) == {"table", "rows"} for row in tables)
    assert all(isinstance(row["rows"], int) for row in tables)


# --- what it must say -----------------------------------------------------------------


def test_states_the_facts_a_bug_report_opens_with(ctx: AppContext) -> None:
    """The block has the version, the install type, the host, the terminal, and the device."""
    from meshterm import __version__

    values = _flat(_report(ctx))
    assert values["meshterm"] == __version__
    assert values["install"] in {"frozen", "source", "package"}
    assert values["os"] and values["python"] and values["arch"]
    assert "x" in values["terminal_size"]
    assert values["platform"] in {"regular", "picocalc-lyra"}
    # The simulator answers as a radio does, so the device half has values and the test does
    # not skip it. A real connected run produces this form.
    assert values["connected"] is True
    assert values["transport"] == "mock"
    assert values["firmware"]


def test_an_unreachable_radio_becomes_a_fact_not_a_failure(
    ctx: AppContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A radio that does not connect leaves the block complete, and the block says why.

    The report about a radio that never connects is the report that is most important to
    file. Thus the read of the device is best-effort: the failure goes in ``error``, and
    each other fact still arrives.
    """

    async def refuse(self):
        raise RuntimeError("no companion answered on COM7")

    monkeypatch.setattr(AppContext, "device", refuse)
    values = _flat(_report(ctx))
    assert values["connected"] is False
    assert "COM7" in values["error"]
    assert values["firmware"] is None
    assert values["meshterm"]  # the rest of the block has no change


def test_only_overridden_preferences_are_listed(ctx: AppContext) -> None:
    """The preference listing has the values that differ from the defaults, and only those.

    If the block shows all forty preferences, it hides the one that the user changed. The
    defaults are in the source, and they are the same for all users.
    """
    assert _flat(_report(ctx))["preferences"] == [{"preference": "log_level", "value": "DEBUG"}]


# --- how it says it -------------------------------------------------------------------


def test_the_page_and_the_command_line_state_the_same_facts(ctx: AppContext) -> None:
    """Each key and value that the page draws is printed by the scripted face, in the same words.

    The two faces share :func:`~meshterm.ui.renderers.facts_pairs`. Thus a field that
    someone adds to the report arrives in both faces, and nobody must remember to add it
    twice. This test finds a page that has a projection of its own.
    """
    report = _report(ctx)
    printed = _plain(report)
    for block in report:
        if isinstance(block, Facts):
            for key, value in facts_pairs(block):
                assert key in printed
                assert value in printed


def test_the_page_is_a_written_page_with_landmarks(ctx: AppContext) -> None:
    """The page is markdown that the About-page renderer draws, and its sections are landmarks.

    The sections are the purpose of the format. A heading pins to the top row while its own
    rows scroll under it. The section jumps step by the headings. The PicoCalc lane has its
    ``Sect up``/``Sect down`` pair for the same reason as a grouped list.
    """
    page = _page(ctx)
    assert isinstance(page, AboutPage)
    assert page.bare is False
    assert {"Build", "Host", "Terminal", "Radio", "Storage"} <= set(page._doc.sections)


def test_the_save_is_advertised_on_both_platforms(ctx: AppContext) -> None:
    """The verb is in the footer hint and in the one free slot of the lane.

    The verb is not in the body of the page, because the body is the report. The verb is
    also not missing from both places. A bare frame forced this problem in the past. F1 and
    F2 are the section pair, and F4 and F5 are the pager. Thus F3 is the slot that the About
    pages leave free.
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
    """The file has the same markdown that the page was built from: one source, two places."""
    from meshterm.tools.diagnostics import markdown_source, write_markdown

    source = markdown_source(_report(ctx))
    written = write_markdown(source, tmp_path / "out.md")
    assert written.read_text(encoding="utf-8") == source
    assert source.startswith("# MeshTerm diagnostics")


def test_saving_creates_the_directory_it_needs(ctx: AppContext, tmp_path: Path) -> None:
    """If the parent of the destination does not exist, the save makes it. It does not refuse."""
    from meshterm.tools.diagnostics import markdown_source, write_markdown

    written = write_markdown(markdown_source(_report(ctx)), tmp_path / "a" / "b" / "out.md")
    assert written.is_file()


def test_the_key_and_the_chip_both_save_and_answer_in_a_popup(
    ctx: AppContext, tmp_path: Path
) -> None:
    """The key and the chip each write the file and show the path in an acknowledgement dialog.

    The chip sends the same action as the key. Thus it is not a second implementation of the
    same verb.
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
    """If the destination cannot be written, the page reports it and stays readable.

    The user can still read the page and copy the text from it. This is the fallback that
    is important, so a failed write must never close the page. The mark also makes the
    dialog red.
    """
    from meshterm.ui.diagnostics import SAVE_KEY

    # A path whose "directory" is an existing file: mkdir and write both fail, on each OS,
    # and the test does not need permissions that the suite cannot rely on.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    session = _Session()
    page = _page(ctx, session=session, save_path=blocker / "inside" / "out.md")

    page.handle("text", SAVE_KEY)
    assert "\u2717" in session.messages[0]
    assert "could not save" in session.messages[0]
    assert "Build" in _body(page)  # the page itself has no change


def test_the_popup_carries_its_tone_as_spans_not_as_markup(ctx: AppContext, tmp_path: Path) -> None:
    """The message is a styled Text, so the dialog prints no tag and the border is correct.

    If the message is a markup string, the dialog does not parse it. The user sees the
    literal tags, and a failure has the border in the colour of success, because the
    dialog chooses the border from the spans of the message. The mark carries the tone, and
    nothing else is necessary.
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
    """The page does one save at a time. It ignores a second press while a save is in progress."""
    from meshterm.ui.diagnostics import SAVE_KEY

    session = _Session(defer=True)
    page = _page(ctx, session=session, save_path=tmp_path / "out.md")
    page.handle("text", SAVE_KEY)
    page.handle("text", SAVE_KEY)
    assert len(session.started) == 1


def test_another_letter_falls_through_to_the_pager(ctx: AppContext, tmp_path: Path) -> None:
    """Each other letter is for the scroll handler, not for the save."""
    session = _Session()
    page = _page(ctx, session=session, save_path=tmp_path / "out.md")
    page.handle("text", "q")
    assert not (tmp_path / "out.md").exists()
    assert session.messages == []


# --- the markdown face ------------------------------------------------------------------

#: A value that has markdown in all its parts: backslashes that markdown reads as escapes,
#: and asterisks and underscores that it reads as emphasis. Markdown must not reinterpret
#: any of it.
_HOSTILE = r"C:\Users\*jp*\_meshterm_"


def test_every_value_survives_markdown_untouched() -> None:
    """Values are code spans, so the backslashes and stars of a path arrive without a change.

    A path, or an error message that the firmware of another person wrote, is not markdown.
    The renderer must never read it as markdown without a warning. If a value has a
    backtick, the renderer uses a longer fence.
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
    """The caption of a block is its ``##``. A block with no caption has no heading."""
    assert markdown_blocks((Facts(key="x", caption="Radio", fields=()),)).startswith("## Radio")
    assert not markdown_blocks((Facts(key="x", fields=()),)).startswith("#")


def test_an_empty_section_says_so_rather_than_vanishing() -> None:
    """An absence is a fact here: "no preferences changed" is information that the user can use.

    The plain face removes an empty listing, because a record stream has nothing to say
    about one. A document does have something to say. If a section is missing with no
    note, the user asks if the code ever built it.
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
        # A terminal that this module does not know still names itself, because the value
        # of TERM_PROGRAM is the name. This is the reason for this rung.
        ({"TERM_PROGRAM": "WezTerm"}, "WezTerm"),
        ({"TERM": "linux"}, "linux"),
        ({}, None),
    ],
)
def test_the_terminal_is_identified_from_what_it_says_about_itself(
    environ: dict, expected: str | None
) -> None:
    """Each rung of the ladder works, to the last ``None`` when nothing names a terminal."""
    assert hostinfo.terminal_program(environ) == expected


def test_an_inner_terminal_wins_over_the_one_hosting_it() -> None:
    """A marker variable has priority over ``TERM_PROGRAM``. The inner emulator is the answer.

    The terminal of VS Code gets the environment of the outer terminal and adds its own
    marker. If the report names the outer terminal, it names a terminal that draws nothing.
    """
    env = {"WT_SESSION": "abc", "TERM_PROGRAM": "vscode"}
    assert hostinfo.terminal_program(env) == "Windows Terminal"


def test_ssh_is_read_from_either_variable() -> None:
    """The block states a connection over ssh, because it changes what the code can know."""
    assert hostinfo.terminal({"SSH_TTY": "/dev/pts/0"}).over_ssh is True
    assert hostinfo.terminal({"SSH_CONNECTION": "10.0.0.2 51000 10.0.0.1 22"}).over_ssh is True
    assert hostinfo.terminal({}).over_ssh is False


def test_the_host_block_is_populated_on_whatever_runs_the_suite() -> None:
    """Each host field has an answer, on each OS where CI runs this test."""
    machine = hostinfo.host()
    assert machine.install in {"frozen", "source", "package"}
    assert machine.os and machine.arch and machine.python
    assert machine.python.count(".") == 2


# --- the repository aggregates --------------------------------------------------------


def test_table_counts_are_discovered_from_the_schema(tmp_path: Path) -> None:
    """The code counts each table, but not the internal tables of SQLite.

    The code reads the table names from ``sqlite_master``. Thus a table that someone adds
    to the schema is in the report on the day that it is added. A list that people write
    becomes old without a warning, and the count that is important is always for the table
    that nobody expected to be full.
    """
    counts = Repository(tmp_path / "meshterm.db").table_counts()
    assert {"runs", "observations", "messages", "traces"} <= set(counts)
    assert not any(name.startswith("sqlite_") for name in counts)
    assert all(isinstance(n, int) for n in counts.values())


def test_the_observation_span_is_absent_until_something_is_heard(tmp_path: Path) -> None:
    """An empty database has no span. It does not have a span of nothing."""
    repo = Repository(tmp_path / "meshterm.db")
    assert repo.observation_span() == (None, None)
    assert repo.failed_run_count() == 0


# --- helpers ---------------------------------------------------------------------------


def _report(ctx: AppContext):
    """Build the report, in the same way as both faces."""
    return asyncio.run(build_report(ctx))


def _flat(report) -> dict:
    """Each fact in the report, flattened in the same way as the JSON face."""
    flat: dict = {}
    for block in report:
        if isinstance(block, Facts):
            flat.update(block.values)
        else:
            flat[block.key] = list(block.rows)
    return flat


def _keys(report) -> list[str]:
    """Each key that the block declares, for the facts and for the columns of the listings."""
    keys: list[str] = []
    for block in report:
        if isinstance(block, Facts):
            keys.extend(column.key for column in block.fields)
        else:
            keys.extend(column.key for column in block.columns)
    return keys


def _plain(report) -> str:
    """The whole block, as the plain face writes it."""
    console = _console(200)
    with console.capture() as captured:
        PlainRenderer(console).render(report)
    return captured.get()


def _machine(report) -> str:
    """The document of the machine face, as text."""
    console = _console(10_000)
    with console.capture() as captured:
        JsonRenderer(console).render(report)
    return captured.get()


def _render(renderable) -> str:
    """A renderable as plain text, at the standard width."""
    console = _console(72)
    with console.capture() as captured:
        console.print(renderable)
    return captured.get()


def _console(width: int) -> Console:
    """A capture console that adds nothing between the content and the string."""
    return Console(width=width, color_system=None, highlight=False, markup=False)


def _blocked(tmp_path: Path) -> Path:
    """A destination that no OS accepts: a "directory" that is a file.

    Both the mkdir and the write fail, on each OS, and the test does not need permissions
    that the suite cannot rely on.
    """
    blocker = tmp_path / "blocker"
    if not blocker.exists():
        blocker.write_text("not a directory", encoding="utf-8")
    return blocker / "inside" / "out.md"


def _strip(line: str) -> str:
    """One rendered line without its SGR sequences."""
    return re.sub(r"\x1b\[[0-9;]*m", "", line)


class _Session:
    """A replacement for a session. It runs the save flow inline and records the dialog.

    In the real session, ``run_detached`` gives the flow of a key handler to the event loop.
    Here it runs the flow at once. Thus a test can press a key and assert on what the user
    sees. If ``defer`` is set, the class keeps the coroutine and does not run it. One test
    needs this for a save that is still in progress.
    """

    def __init__(self, *, defer: bool = False, keep: bool = False) -> None:
        """Start with nothing shown and nothing started."""
        self.messages: list[str] = []
        self.shown: list[object] = []
        self.started: list[object] = []
        self._defer = defer
        self._keep = keep

    def invalidate(self) -> None:
        """A request for a paint. A test has no screen to paint."""

    def run_detached(self, work):
        """Run the flow now. If ``defer`` is set, keep the flow and do not run it."""
        self.started.append(work)
        if self._defer:
            work.close()
            return None
        return asyncio.run(work)

    async def message_dialog(self, message, *, title: str = "") -> None:
        """Record the text that the acknowledgement dialog shows."""
        self.messages.append(str(message))
        if self._keep:
            self.shown.append(message)


def _page(ctx: AppContext, *, session: _Session | None = None, save_path: Path | None = None):
    """The page for the report of this context. It saves where the caller says."""
    from meshterm.ui.diagnostics import DiagnosticsPage

    target = save_path or (ctx.settings.config_dir / "meshterm-diagnostics.md")
    return DiagnosticsPage(
        markdown_source(_report(ctx)), session=session or _Session(), save_path=target
    )


def _body(page) -> str:
    """The rows that the page draws, as plain text."""
    return "\n".join(_strip(line) for line in page.render_body(100))
