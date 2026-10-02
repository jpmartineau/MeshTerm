# SPDX-License-Identifier: Apache-2.0
"""Tests for the channel-management feature: pure logic, QR rendering, and the tool.

The logic (secret derivation, share-URL round-tripping) is hardware-free; the tool actions
run against the :class:`MockDevice` simulator and a temporary database.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from rich.console import Console

from meshterm.context import AppContext
from meshterm.core.admin_store import AdminStore
from meshterm.core.channel_probe import ChannelSlot, read_channel_slots
from meshterm.core.channels import (
    CHANNEL_SECRET_BYTES,
    CHANNEL_SLOT_EMPTY_RUN,
    CHANNEL_SLOT_PROBE_CAP,
    DEFAULT_PUBLIC_SECRET,
    channel_hash,
    decrypt_channel_text,
    derive_secret,
    full_channel_hash,
    normalize_secret,
    parse_share_url,
    random_secret,
    share_url,
)
from meshterm.core.config import Settings
from meshterm.core.device_store import DeviceStore
from meshterm.persistence.repository import Repository
from meshterm.services.chat_service import ChatService
from meshterm.tools.channels import ChannelsTool
from meshterm.ui.channels import _CREATE, _apply_order, _next_free_slot
from meshterm.ui.qr import QrScreen, fit_qr, qr_text

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


# -- pure channel logic -------------------------------------------------------


def test_derive_secret_matches_firmware_formula() -> None:
    """A derived key is sha256(name)[:16] — the firmware's public-channel scheme."""
    assert derive_secret("#public") == sha256(b"#public").digest()[:16]
    assert len(derive_secret("#anything")) == CHANNEL_SECRET_BYTES
    assert derive_secret("#a") == derive_secret("#a")  # deterministic


def test_random_secret_is_16_unique_bytes() -> None:
    """A random private key is 16 bytes and (practically) never repeats."""
    a, b = random_secret(), random_secret()
    assert len(a) == CHANNEL_SECRET_BYTES and len(b) == CHANNEL_SECRET_BYTES
    assert a != b


def test_normalize_secret_accepts_common_shapes() -> None:
    """A pasted key is accepted with 0x, spaces, or colons; length is enforced."""
    raw = "000102030405060708090a0b0c0d0e0f"
    expected = bytes(range(16))
    assert normalize_secret(raw) == expected
    assert normalize_secret("0x" + raw) == expected
    assert normalize_secret("0001 0203 0405 0607 0809 0a0b 0c0d 0e0f") == expected
    assert normalize_secret("00:01:02:03:04:05:06:07:08:09:0a:0b:0c:0d:0e:0f") == expected


@pytest.mark.parametrize("bad", ["", "abcd", "zz" * 16, "00" * 15, "00" * 17])
def test_normalize_secret_rejects_bad_keys(bad: str) -> None:
    """Non-hex or wrong-length keys raise, so the prompt can re-ask."""
    with pytest.raises(ValueError):
        normalize_secret(bad)


def test_channel_hash_is_two_hex_chars() -> None:
    """The channel hash is the leading byte of sha256(secret), as the companion reports."""
    secret = bytes(range(16))
    assert channel_hash(secret) == sha256(secret).hexdigest()[:2]
    assert len(channel_hash(secret)) == 2


def test_full_channel_hash_is_the_whole_digest_led_by_the_short_hash() -> None:
    """The full hash is the complete sha256(secret) digest, opening with the short hash."""
    secret = bytes(range(16))
    full = full_channel_hash(secret)
    assert full == sha256(secret).hexdigest()
    assert len(full) == 64
    assert full[:2] == channel_hash(secret)  # the highlighted first byte


def test_share_url_round_trips_with_encoding() -> None:
    """A share URL builds and parses back to the same name and secret, encoding included."""
    name, secret = "Ops Team #1", bytes(range(16))
    url = share_url(name, secret)
    assert url.startswith("meshcore://channel/add?")
    assert "Ops%20Team" in url  # the space is percent-encoded
    parsed = parse_share_url(url)
    assert parsed == (name, secret)


def test_parse_share_url_rejects_non_channel_links() -> None:
    """Anything that isn't a valid channel-add link parses to None."""
    assert parse_share_url("https://example.com") is None
    assert parse_share_url("meshcore://contact/add?name=x&public_key=ab") is None
    assert parse_share_url("meshcore://channel/add?name=x") is None  # missing secret
    assert parse_share_url("meshcore://channel/add?name=x&secret=nothex") is None
    assert parse_share_url("meshcore://channel/add?secret=" + "00" * 16) is None  # no name


# -- channel-text decryption ---------------------------------------------------


def _grp_txt_frame(
    name: str, secret: bytes, text: str, *, timestamp: int = 1_700_000_000, attempt: int = 0
) -> tuple[str, str, str]:
    """Build a firmware-shaped GRP_TXT frame (chan_hash, cipher_mac, crypted), all hex.

    Mirrors the encoding :func:`~meshterm.core.channels.decrypt_channel_text` reverses:
    a 4-byte little-endian timestamp, an attempt/type byte, the text, zero-padded to a
    whole AES block, encrypted ECB, then MAC'd with HMAC-SHA256 truncated to 2 bytes.
    """
    from Crypto.Cipher import AES
    from Crypto.Hash import HMAC, SHA256

    from meshterm.core.channels import effective_secret

    key = effective_secret(name, secret)
    plain = timestamp.to_bytes(4, "little") + bytes([attempt & 3]) + text.encode("utf-8")
    plain += b"\x00" * (-len(plain) % 16)
    crypted = AES.new(key, AES.MODE_ECB).encrypt(plain)
    mac = HMAC.new(key, digestmod=SHA256)
    mac.update(crypted)
    return channel_hash(key), mac.digest()[:2].hex(), crypted.hex()


def test_decrypt_channel_text_round_trips_a_known_channel() -> None:
    """A frame encrypted for a channel we hold the key for decodes cleanly."""
    name, secret = "#general", derive_secret("#general")
    chash, mac, crypted = _grp_txt_frame(name, secret, "hello mesh")
    result = decrypt_channel_text(chash, mac, crypted, [(name, secret)])
    assert result is not None
    assert result.channel_name == name
    assert result.text == "hello mesh"
    assert result.attempt == 0
    assert result.sent_at is not None


def test_decrypt_channel_text_reports_a_resend_attempt() -> None:
    """The sender's resend counter survives into the decrypted result."""
    name, secret = "Ops", random_secret()
    chash, mac, crypted = _grp_txt_frame(name, secret, "again", attempt=2)
    result = decrypt_channel_text(chash, mac, crypted, [(name, secret)])
    assert result is not None and result.attempt == 2


def test_decrypt_channel_text_returns_none_for_an_unknown_channel() -> None:
    """A frame from a channel we don't hold the key for decrypts to nothing."""
    name, secret = "#general", derive_secret("#general")
    chash, mac, crypted = _grp_txt_frame(name, secret, "hello mesh")
    other = ("Ops", random_secret())
    assert decrypt_channel_text(chash, mac, crypted, [other]) is None
    assert decrypt_channel_text(chash, mac, crypted, []) is None


def test_decrypt_channel_text_rejects_a_fingerprint_collision() -> None:
    """A candidate sharing the frame's hash byte but not its key fails the MAC check.

    The one-byte channel-hash fingerprint can collide between unrelated channels;
    the MAC is what actually confirms the key, so a same-fingerprint wrong channel
    must not be mistaken for a match, and decryption must fall through to try the
    next (correct) candidate instead of stopping at the collision.
    """
    name, secret = "#general", derive_secret("#general")
    chash, mac, crypted = _grp_txt_frame(name, secret, "hello mesh")
    # Brute-force a same-fingerprint, wrong-key decoy (a 1-byte hash, so cheap to find).
    decoy = next(
        s
        for s in (random_secret() for _ in range(10_000))
        if channel_hash(s) == chash and s != secret
    )
    result = decrypt_channel_text(chash, mac, crypted, [("decoy", decoy), (name, secret)])
    assert result is not None and result.text == "hello mesh"  # falls through to the real key


def test_decrypt_channel_text_rejects_malformed_hex() -> None:
    """Garbage hex fields fail closed instead of raising."""
    assert decrypt_channel_text("zz", "zzzz", "zzzzzzzzzzzzzzzz", [("x", random_secret())]) is None
    # a 15-byte body is not block-aligned
    assert decrypt_channel_text("00", "0000", "00" * 15, [("x", random_secret())]) is None


# -- QR rendering -------------------------------------------------------------


def test_qr_text_renders_white_on_black_with_a_quiet_zone() -> None:
    """qr_text draws white ink on a black field, whatever the terminal's own theme.

    A camera reads contrast, and pure white on pure black is the most a screen has;
    light-on-dark is what a scanner expects of a screen. Both ends are named so the
    surrounding palette can't dilute either.
    """
    from meshterm.ui.theme import MESH_THEME

    console = Console(
        force_terminal=True,
        color_system="truecolor",
        width=120,
        file=__import__("io").StringIO(),
        theme=MESH_THEME,
    )
    console.print(qr_text("meshcore://channel/add?name=Test&secret=" + "ab" * 16))
    raw = console.file.getvalue()
    assert "38;2;255;255;255" in raw, "the ink isn't pure white"
    assert "48;2;0;0;0" in raw, "the field isn't pure black"
    plain = _ANSI.sub("", raw).rstrip("\n")
    lines = plain.splitlines()
    assert lines, "QR produced no output"
    # A version-appropriate QR for this URL is at least ~25 modules wide plus an 8-module
    # quiet zone; the finder pattern makes the code non-trivial.
    assert len(lines[0]) >= 30
    assert any("█" in line for line in lines)  # dark modules were drawn


def test_a_code_fits_itself_to_the_frame_it_is_drawn_in() -> None:
    """A contact card is 57 cells and 29 rows at the standard fit; frames are smaller.

    Rather than be cut — a cut code scans as nothing — the code steps down: a lighter
    error level first (a screen is never smudged), then the narrower quiet zone. The
    PicoCalc panel is 53 across; a regular terminal is 24 rows.
    """
    from rich.cells import cell_len

    url = "meshcore://contact/add?name=YUL-Cartierville&public_key=" + "ab" * 32 + "&type=2"
    standard = qr_text(url).plain.splitlines()
    assert len(standard[0]) > 53 and len(standard) > 24  # the standard fit overflows both
    for width in (80, 53, 45):
        fitted = fit_qr(url, width).plain.splitlines()
        assert max(cell_len(line) for line in fitted) <= width, width
    assert len(fit_qr(url, 80, 24).plain.splitlines()) <= 24


def test_the_share_screen_refits_its_code_to_the_frame_every_paint() -> None:
    """The screen fits code *and* link first, the code alone next, and centres the rows."""
    from rich.cells import cell_len

    url = "meshcore://contact/add?name=YUL-Cartierville&public_key=" + "ab" * 32 + "&type=2"
    screen = QrScreen(url, title="Share YUL-Cartierville")
    assert screen.bare and not screen.floating

    def code_rows(width: int, rows: int) -> list[str]:
        screen.note_viewport(rows)  # what compose_bare states before asking for the body
        lines = [_ANSI.sub("", line) for line in screen.render_body(width)]
        assert max(cell_len(line) for line in lines) <= width
        assert url[:40] in "".join(lines), "the link is under the code"
        blank = next(i for i in range(len(lines) - 1, -1, -1) if not lines[i].strip())
        return lines[:blank]  # the code is everything above the row that separates it

    roomy = code_rows(120, 40)
    assert len(roomy) == 29  # the standard fit, where there is room for it
    assert roomy[0].startswith(" " * 30), "centred across the whole width"
    # Centred as a block, never per row: every row carries the same indent and the same
    # width, so the finder squares stack square. Per-row centring stripped each row's
    # trailing light modules before padding it, and skewed the bottom finder a cell.
    assert len({len(row) for row in roomy}) == 1, "rows differ in width — a skewed code"
    finder = "█▀▀▀▀▀█"
    assert len({row.index(finder) for row in roomy if finder in row}) == 1, "a finder is skewed"
    assert len(code_rows(72, 24)) <= 24  # a regular terminal: the code whole, link below
    assert len(code_rows(53, 26)) <= 26  # the PicoCalc: likewise


def test_several_links_are_one_share_screen_that_arrows_step_through() -> None:
    """←→ move between the codes and stop at the ends; the URL line says which way has more.

    One link draws no arrows at all — there is nowhere to step — so a share card keeps
    its bare frame exactly as it was.
    """
    first, second = "https://meshterm.net", "https://github.com/jpmartineau/MeshTerm"
    screen = QrScreen(first, second, title="Links")
    screen.note_viewport(24)

    def link_line() -> str:
        lines = [_ANSI.sub("", line) for line in screen.render_body(72)]
        return next(line for line in lines if "https://" in line).strip()

    assert screen.url == first and link_line() == f"←  {first}  →"
    screen.handle("left")  # clamped: already at the first
    assert screen.url == first
    screen.handle("right")
    assert screen.url == second and link_line() == f"←  {second}  →"
    screen.handle("right")  # clamped at the last, never back round to the first
    assert screen.url == second
    assert "→" in screen._link().plain and screen._link().spans[-1].style == "muted"
    assert screen._link().spans[0].style == "accent"  # ← lit: the first is that way

    single = QrScreen(first, title="Share Lakeside")
    single.note_viewport(24)
    lines = [_ANSI.sub("", line) for line in single.render_body(72)]
    assert next(line for line in lines if "https://" in line).strip() == first


def test_where_braille_is_solid_every_code_is_drawn_in_it() -> None:
    """On the PicoCalc a code is braille, one module a dot — eight to a cell.

    So the contact card is 29 cells by 15 rows at the standard fit and shares the 53x26
    panel with its link, where half blocks sent the link a page down; and a page's code
    is braille too. Every module has to survive the packing, so the braille is decoded
    back and held against the code's own matrix. A desktop font's braille is dotted, so
    the regular platform never draws a code in it.
    """
    import segno

    from meshterm.platforms import PICOCALC, set_platform

    def is_braille(text: str) -> bool:
        return any("⠀" <= ch <= "⣿" for ch in text)

    url = "meshcore://contact/add?name=YUL-Cartierville&public_key=" + "ab" * 32 + "&type=2"
    channel = "meshcore://channel/add?name=Test&secret=" + "ab" * 16
    assert not is_braille(fit_qr(url, 53, 22).plain) and not is_braille(qr_text(channel).plain)

    set_platform(PICOCALC)
    assert is_braille(qr_text(channel).plain) and "█" not in qr_text(channel).plain
    screen = QrScreen(url, title="Share YUL-Cartierville")
    screen.note_viewport(26)
    lines = [_ANSI.sub("", line) for line in screen.render_body(53)]
    assert len(lines) <= 26 and url[:40] in "".join(lines), "code and link share the panel"
    blank = next(i for i in range(len(lines) - 1, -1, -1) if not lines[i].strip())
    cells = [line.lstrip(" ") for line in lines[:blank]]
    assert (len(cells[0]), len(cells)) == (29, 15)

    dots = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
    decoded = [
        [bool((ord(row[x // 2]) - 0x2800) & dots[x % 2][y % 4]) for x in range(57)]
        for y in range(57)
        for row in [cells[y // 4]]
    ]
    matrix = [[bool(v) for v in row] for row in segno.make(url, error="m").matrix_iter(border=4)]
    assert decoded == matrix, "a module was lost packing the code into braille"


# -- channel slot model -------------------------------------------------------


def test_channel_slot_classifies_public_and_private() -> None:
    """A #-named or name-derived slot reads as public; a random-key slot as private."""
    public = ChannelSlot(idx=0, name="#public", secret=derive_secret("#public"))
    assert public.is_public
    derived = ChannelSlot(idx=1, name="general", secret=derive_secret("general"))
    assert derived.is_public  # key derived from the name, even without a leading #
    private = ChannelSlot(idx=2, name="Ops", secret=bytes(range(16)))
    assert not private.is_public
    assert not private.is_name_derived
    # The firmware default "Public" is public but keyed by a fixed well-known secret, not by
    # its name — so it reads as public without being name-derived (its key must be preserved).
    default_public = ChannelSlot(idx=0, name="Public", secret=DEFAULT_PUBLIC_SECRET)
    assert default_public.is_public
    assert not default_public.is_name_derived
    assert channel_hash(DEFAULT_PUBLIC_SECRET) == "11"  # the fingerprint devices report for it
    assert private.conversation.is_channel and private.conversation.channel_idx == 2
    assert private.conversation.label == "Ops"  # raw name, no forced leading '#'
    # The conversation is keyed by the channel's identity, not its slot.
    assert private.conversation.channel_id == private.identity
    assert private.conversation.key == f"chan:{private.identity}"


def test_next_free_slot_finds_gaps_and_full() -> None:
    """The next free slot skips used indices and is None when every slot is taken."""
    slots = [
        ChannelSlot(idx=0, name="a", secret=b"\x00" * 16),
        ChannelSlot(idx=2, name="c", secret=b"\x00" * 16),
    ]
    assert _next_free_slot(slots, capacity=8) == 1
    full = [ChannelSlot(idx=i, name=str(i), secret=b"\x00" * 16) for i in range(8)]
    assert _next_free_slot(full, capacity=8) is None
    # With a larger discovered capacity the same "full-at-8" set still has room.
    assert _next_free_slot(full, capacity=16) == 8


async def test_channel_capacity_is_probed_not_assumed(ctx: AppContext) -> None:
    """Capacity comes from probing the device, and counts slots, not their occupants."""
    device = await ctx.device()
    assert await device.channel_capacity() == 8  # the mock models stock 8-slot firmware
    # Occupying some slots must not change the ceiling — capacity is how many slots exist.
    await device.set_channel(0, "Alpha", bytes(range(16)))
    await device.set_channel(3, "Bravo", bytes(range(16)))
    assert await device.channel_capacity() == 8


async def test_channel_capacity_tracks_a_larger_ceiling(ctx: AppContext) -> None:
    """Firmware with more slots is discovered as such — no hard-coded 8 anywhere.

    This is exactly why the empty-run bound is *not* applied to capacity discovery: a larger
    firmware whose upper slots are empty must still be found by reaching its rejection, not
    guessed short from a run of empties.
    """
    device = await ctx.device()
    device._max_channels = 12  # simulate a firmware build with a larger slot table
    assert await device.channel_capacity() == 12


async def test_read_channel_slots_bounds_a_never_rejecting_probe() -> None:
    """A probe that is never rejected still bounds itself.

    On firmware that answers every slot, the configured-slot scan stops after a run of
    empty slots instead of walking all of ``CHANNEL_SLOT_PROBE_CAP``.
    """

    class NeverRejects:
        """A device that reports two channels then empty slots forever, never raising."""

        def __init__(self) -> None:
            self.reads = 0

        async def get_channel(self, idx: int):
            self.reads += 1
            if idx in (0, 1):
                return {"channel_name": f"c{idx}", "channel_secret": b"\x00" * 16}
            return None  # an empty slot — and it will never reject a higher index

    device = NeverRejects()
    slots = await read_channel_slots(device)  # type: ignore[arg-type]
    assert [s.name for s in slots] == ["c0", "c1"]  # both real channels found
    # Stopped after the two channels plus one run of empties — not the full 64-slot walk.
    assert device.reads == 2 + CHANNEL_SLOT_EMPTY_RUN
    assert device.reads < CHANNEL_SLOT_PROBE_CAP


async def test_read_channel_slots_scans_past_gaps_within_capacity() -> None:
    """A cleared middle slot (a gap) never truncates the scan: channels above it are still read.

    The empty-run bound only ends the scan after a full stock-capacity's worth of consecutive
    empties, which a within-capacity gap can never reach — so a channel sitting above a gap on
    never-rejecting firmware is still found.
    """

    class NeverRejects:
        def __init__(self, occupied: dict[int, str]) -> None:
            self.occupied = occupied
            self.reads = 0

        async def get_channel(self, idx: int):
            self.reads += 1
            name = self.occupied.get(idx)
            if name is None:
                return None
            return {"channel_name": name, "channel_secret": b"\x00" * 16}

    # Channels at slots 0 and 7 with 1..6 empty (a 6-slot gap, one short of the stop run).
    device = NeverRejects({0: "low", 7: "high"})
    slots = await read_channel_slots(device)  # type: ignore[arg-type]
    assert [s.idx for s in slots] == [0, 7]  # the gap did not end the scan early


async def test_apply_order_relays_channels_into_new_positions(ctx: AppContext) -> None:
    """Applying a new order re-lays channels across the same slots in display order."""
    device = await ctx.device()
    await device.set_channel(0, "Alpha", bytes(range(16)))  # private
    await device.set_channel(1, "#beta", None)  # public, key derived from the name
    await device.set_channel(2, "Gamma", bytes(range(16, 32)))  # private

    slots = await read_channel_slots(device)
    # Reverse the display order: the row that was third moves first, first moves last.
    writes = await _apply_order(ctx, device, slots, [2, 1, 0])
    assert writes == 2  # the middle channel keeps its slot; the two ends swap

    after = {s.idx: s for s in await read_channel_slots(device)}
    assert after[0].name == "Gamma" and after[0].secret == bytes(range(16, 32))
    assert after[1].name == "#beta" and after[1].is_public  # public key re-derived in place
    assert after[2].name == "Alpha" and after[2].secret == bytes(range(16))


def test_channel_identity_is_stable_across_slot_moves() -> None:
    """A channel's identity depends on its key material, not on which slot it occupies."""
    at_two = ChannelSlot(idx=2, name="Ops", secret=bytes(range(16)))
    at_five = ChannelSlot(idx=5, name="Ops", secret=bytes(range(16)))
    assert at_two.identity == at_five.identity  # same channel, different slot → same identity

    other = ChannelSlot(idx=2, name="Ops", secret=bytes(range(16, 32)))
    assert other.identity != at_two.identity  # a different key → a different channel


class _ScriptedUi:
    """A UI surface that replays queued answers, for driving the channel manager headless.

    ``select``/``text``/``dialog`` each pop their next scripted answer; the display methods
    are no-ops. Enough of the :class:`~meshterm.ui.surface.Ui` contract for the channel
    manager's create/clear flows.
    """

    def __init__(self, selects: list, texts: list, dialogs: list) -> None:
        self._selects = list(selects)
        self._texts = list(texts)
        self._dialogs = list(dialogs)

    def show(self, *renderables) -> None:  # noqa: ANN002
        pass

    @asynccontextmanager
    async def busy_dialog(self, message: str = "", *, title: str = ""):  # noqa: ANN201
        """No card without a screen stack — every mutation path goes through this."""
        yield SimpleNamespace(message=message)

    def note(self, markup: str) -> None:
        pass

    def ack(self, markup: str) -> None:
        pass

    async def view(self, renderable, *, title: str = "", footer_hint: str = "") -> None:  # noqa: ANN001
        pass

    async def select(self, title: str, items: list, *, default=None):  # noqa: ANN001, ANN201
        return self._selects.pop(0) if self._selects else "__back__"

    async def text(  # noqa: ANN001, ANN201
        self,
        title: str,
        *,
        default: str = "",
        validate=None,
        help_text: str = "",
        password: bool = False,
    ):
        return self._texts.pop(0)

    async def dialog(  # noqa: ANN001, ANN201
        self,
        prompt,
        buttons,
        *,
        title: str = "",
        default: int = 0,
        keys=None,
        danger: bool = False,
        destructive: bool = False,
    ):
        return self._dialogs.pop(0)


async def test_recreating_a_slot_refiles_messages_to_the_new_channel(ctx: AppContext) -> None:
    """Clearing a channel and creating another that reuses its slot must not cross history.

    Reproduces the reported bug: inbound channel messages carry only a slot index, which the
    chat service maps to a channel identity through a cache. When a slot is cleared and a new
    channel takes it, a stale cache filed the newcomer's messages under the old channel — so
    opening the old channel showed the new one's transcript. The manager must refresh the
    cache after the mutation so a message on the reused slot lands in the right channel.
    """
    from meshterm.core.channels import channel_identity
    from meshterm.core.events import MeshEvent
    from meshterm.core.models import Message
    from meshterm.ui.channels import _CLEAR, _CREATE, manage_channels

    device = await ctx.device()
    await device.set_channel(0, "Public", DEFAULT_PUBLIC_SECRET)
    public_id = channel_identity("Public", DEFAULT_PUBLIC_SECRET)

    await ctx.chat.start()  # prime the slot→identity cache (slot 0 == Public)
    try:
        # A message on Public arrives before we touch anything — files under Public. The
        # inbound worker resolves it against the slot as it stands now (Public), which is
        # exactly what happens live: messages are recorded on arrival, ahead of any later edit.
        ctx.events.publish(
            MeshEvent.message_event(Message(text="hi public", channel=0, is_channel=True))
        )
        await ctx.chat._queue.join()

        # Drive the manager: open Public's detail → clear it, then create a new private
        # channel (which reuses freed slot 0), then leave with Esc.
        ctx.ui = _ScriptedUi(
            selects=[0, _CLEAR, _CREATE, None],
            texts=["Ops"],  # the new channel's name
            dialogs=["clear"],  # confirm the clear on its Cancel/Clear dialog
        )
        await manage_channels(ctx)

        ops = next(s for s in await read_channel_slots(device) if s.name == "Ops")
        assert ops.idx == 0  # the new channel took the freed slot

        # A message now arrives on slot 0 — which is Ops, not Public.
        ctx.events.publish(
            MeshEvent.message_event(Message(text="ops secret", channel=0, is_channel=True))
        )
        await ctx.chat._queue.join()

        public_msgs = ctx.repo.recent_chat_messages(is_channel=True, channel_id=public_id)
        ops_msgs = ctx.repo.recent_chat_messages(is_channel=True, channel_id=ops.identity)
        assert [m.text for m in public_msgs] == ["hi public"]  # unchanged, no leakage
        assert [m.text for m in ops_msgs] == ["ops secret"]  # filed under the right channel
    finally:
        await ctx.chat.stop()


async def test_reorder_keeps_history_because_key_is_intrinsic(ctx: AppContext) -> None:
    """Reordering channels leaves each channel's transcript with it — no migration needed.

    Reproduces the reported bug's setup (a channel moved to a new slot) and asserts the fix:
    because history is keyed by channel identity rather than slot, the moved channel keeps
    its own messages and the slot it landed on inherits none.
    """
    device = await ctx.device()
    await device.set_channel(0, "Alpha", bytes(range(16)))
    await device.set_channel(1, "Beta", bytes(range(16, 32)))

    chat = ChatService(ctx)
    await chat.start()
    try:
        await chat.send_channel(0, "hello from alpha", label="Alpha")
        await chat.send_channel(1, "hello from beta", label="Beta")

        slots = await read_channel_slots(device)
        before = {s.name: s for s in slots}
        alpha_id, beta_id = before["Alpha"].identity, before["Beta"].identity

        # Swap the two channels' slots, then refresh the service's slot→identity cache.
        writes = await _apply_order(ctx, device, slots, [1, 0])
        assert writes == 2
        await chat.refresh_channels()

        after = {s.name: s for s in await read_channel_slots(device)}
        assert after["Alpha"].idx == 1 and after["Beta"].idx == 0  # slots actually swapped
        assert after["Alpha"].identity == alpha_id  # identity is unchanged by the move

        # History still resolves per channel by identity — no transcript was inherited.
        alpha_msgs = ctx.repo.recent_chat_messages(is_channel=True, channel_id=alpha_id)
        beta_msgs = ctx.repo.recent_chat_messages(is_channel=True, channel_id=beta_id)
        assert [m.text for m in alpha_msgs] == ["hello from alpha"]
        assert [m.text for m in beta_msgs] == ["hello from beta"]
    finally:
        await chat.stop()


# -- message statistics in the manager ----------------------------------------


def test_channel_stats_aggregates_totals_window_and_recency(ctx: AppContext) -> None:
    """Per-channel stats count all messages, the trailing-window slice, and the last time."""
    from datetime import timedelta

    from meshterm.core.models import ChatMessage, utcnow

    stale = utcnow() - timedelta(days=30)  # outside the activity window
    fresh = utcnow() - timedelta(minutes=2)
    for text, when in (("old a", stale), ("old b", stale), ("new", fresh)):
        ctx.repo.record_chat_message(
            ChatMessage(text=text, is_channel=True, channel_id="ops", created_at=when)
        )
    ctx.repo.record_chat_message(  # a direct message must not leak into channel stats
        ChatMessage(text="dm", is_channel=False, peer="abc123", created_at=fresh)
    )
    ctx.repo.record_chat_message(  # a legacy row without an identity has nothing to count under
        ChatMessage(text="legacy", is_channel=True, channel_id=None, created_at=fresh)
    )

    stats = ctx.repo.channel_stats()
    assert set(stats) == {"ops"}
    ops = stats["ops"]
    assert ops.total == 3
    assert ops.recent == 1  # only the fresh message falls inside the window
    assert ops.last_at is not None
    assert abs((ops.last_at - fresh).total_seconds()) < 1
    # The histogram spans the window newest-first: the 2-minute-old message lands in the
    # "now" (first) bucket and the 30-day-old ones land nowhere. It carries the full
    # window (deeper than the sparkline draws) so the shared scaling peak has history.
    assert len(ops.histogram) == 72
    assert ops.histogram[0] == 1 and sum(ops.histogram) == 1


def test_activity_sparkline_packs_24_buckets_into_braille() -> None:
    """The sparkline draws two buckets per cell, scaled to the shared peak, newest right."""
    from meshterm.ui.channels import _activity_sparkline

    assert _activity_sparkline((0,) * 24, 0).plain == "⣀" * 12  # silent window: a flatline
    assert _activity_sparkline((), 0).plain == "⣀" * 12  # a channel with no stats at all
    assert _activity_sparkline((21,) * 24, 21).plain == "⣿" * 12  # every bucket at the peak
    # A bucket well under the shared peak rides the baseline as a single dot in the
    # *final* cell's right column (now sits at the right edge) — so the glyph matches
    # the flatline and the ok-green style is what marks it as traffic.
    lone = _activity_sparkline((1,) + (0,) * 23, 16)
    assert lone.plain == "⣀" * 12
    styles = [span.style for span in lone.spans]
    assert styles[-1] == "ok" and set(styles[:-1]) == {"faint"}
    # Heights scale to the peak: a 16/12/8/4 ramp climbs quarter→full, the newest
    # (fullest here) pair in the rightmost cell.
    ramp = _activity_sparkline((16, 12, 8, 4) + (0,) * 20, 16).plain
    assert ramp[-2:] == chr(0x2800 | 0x40 | 0xA0) + chr(0x2800 | 0x46 | 0xB8)


async def test_channel_rows_carry_stats_unread_and_lanes(ctx: AppContext) -> None:
    """A list row shows the channel's type, unread badge, counts, age, and activity meter."""
    from meshterm.core.models import ChatMessage
    from meshterm.ui.channels import _LiveStats, _menu_items
    from meshterm.ui.tui import Choice

    device = await ctx.device()
    await device.set_channel(0, "Ops", bytes(range(16)))
    slots = await read_channel_slots(device)
    slot = slots[0]
    for text in ("one", "two"):
        ctx.repo.record_chat_message(
            ChatMessage(text=text, is_channel=True, channel_id=slot.identity)
        )
    ctx.chat._unread[slot.conversation.key] = 2  # as the service would after two arrivals

    title, items = _menu_items(ctx, slots, 8, _LiveStats(ctx))
    assert title == "Channels · 1/8 slots"  # a status atom chains with ·, not an em dash
    row = next(it for it in items if isinstance(it, Choice) and it.value == 0)
    plain = row.label.plain  # the title is a live callable; .label resolves it
    assert "Ops" in plain
    # No TYPE or HASH lane: the glyph carries openness, Show key carries the hash —
    # the freed cells keep the activity lane visible at 72 columns.
    assert "private" not in plain and slot.hash not in plain
    assert "● 2" in plain  # the unread badge
    assert "now" in plain  # the just-recorded message's age
    # Both just-recorded messages sit in the sparkline's newest bucket. With only this
    # channel carrying traffic it *is* the shared peak, so the final cell's right column
    # fills to full height at the row's right edge, the rest of the window on the flatline.
    assert plain.rstrip().endswith("⣀" * 11 + chr(0x2800 | 0x40 | 0xB8))
    label = row.label
    assert label.spans[-1].style == "ok"  # …and its newest cell reads as live traffic


async def test_standard_public_row_appears_only_while_it_is_absent(ctx: AppContext) -> None:
    """The 'Standard Public channel' add-action shows until a slot holds the fixed-key default."""
    from meshterm.core.channels import DEFAULT_PUBLIC_SECRET
    from meshterm.ui.channels import _DEFAULT_PUBLIC, _add_default_public, _LiveStats, _menu_items
    from meshterm.ui.tui import Choice

    device = await ctx.device()

    def has_default_row(slots: list) -> bool:
        _, items = _menu_items(ctx, slots, 8, _LiveStats(ctx))
        return any(isinstance(it, Choice) and it.value == _DEFAULT_PUBLIC for it in items)

    # Absent on an empty table and while an unrelated channel occupies a slot.
    assert has_default_row([])
    await device.set_channel(1, "Ops", bytes(range(16)))
    slots = await read_channel_slots(device)
    assert has_default_row(slots)

    # Adding it lands the well-known secret on the next free slot and retires the offer.
    added = await _add_default_public(ctx, device, slots, 8)
    assert added == 1
    slots = await read_channel_slots(device)
    public = next(s for s in slots if s.secret == DEFAULT_PUBLIC_SECRET)
    assert public.name == "Public" and public.is_public
    assert not has_default_row(slots)


async def test_clearing_a_channel_reads_off_its_unread(ctx: AppContext) -> None:
    """Clearing a channel drops its unread from the global total, not just the slot."""
    from meshterm.ui.channels import _clear

    device = await ctx.device()
    await device.set_channel(0, "Ops", bytes(range(16)))
    slot = (await read_channel_slots(device))[0]
    ctx.chat._unread[slot.conversation.key] = 3  # as the service would after three arrivals
    assert ctx.chat.unread_total() == 3

    ctx.ui = _ScriptedUi(selects=[], texts=[], dialogs=["clear"])
    assert await _clear(ctx, device, slot) is True
    assert ctx.chat.unread_total() == 0


async def test_show_key_popup_wraps_values_under_header_labels() -> None:
    """The key popup is labelled blocks, not a table, and long values wrap whole."""
    from types import SimpleNamespace

    from meshterm.ui.channels import _show_key
    from meshterm.ui.tui.render import render_lines

    captured: dict = {}

    async def view(renderable, *, title="", footer_hint=""):  # noqa: ANN001, ANN202
        captured["renderable"] = renderable
        captured["title"] = title

    slot = ChannelSlot(idx=0, name="Ops", secret=bytes(range(16)))
    await _show_key(SimpleNamespace(ui=SimpleNamespace(view=view)), slot)
    assert captured["title"] == "Key — Ops"

    # Render at a deliberately narrow dialog width: every value must survive whole,
    # wrapped across lines, with no ellipsis truncation anywhere.
    lines = [_ANSI.sub("", line) for line in render_lines(captured["renderable"], 40)]
    text = "\n".join(lines)
    for label in ("NAME", "TYPE", "HASH", "KEY", "LINK"):
        assert label in text
    assert "…" not in text
    joined = text.replace("\n", "").replace(" ", "")
    assert slot.secret.hex() in joined  # the 32-hex key, reassembled across wraps
    assert full_channel_hash(slot.secret) in joined  # the 64-hex hash likewise
    assert share_url("Ops", slot.secret).replace(" ", "") in joined


async def test_detail_summary_reads_slot_totals_and_unread(ctx: AppContext) -> None:
    """The detail screen's vital-signs line covers slot, totals, unread, and recency."""
    from meshterm.core.models import ChatMessage
    from meshterm.ui.channels import _detail_summary, _LiveStats

    device = await ctx.device()
    await device.set_channel(3, "Ops", bytes(range(16)))
    slot = next(s for s in await read_channel_slots(device) if s.idx == 3)

    # What the channel *is* leads — the openness and hash moved here out of a title that
    # was 42 cells wide and left no room on the console for the bar that says Esc leaves.
    assert _detail_summary(ctx, slot, _LiveStats(ctx)) == (
        "private · hash be · slot 3 · no messages yet"
    )

    ctx.repo.record_chat_message(ChatMessage(text="hi", is_channel=True, channel_id=slot.identity))
    ctx.chat._unread[slot.conversation.key] = 1
    summary = _detail_summary(ctx, slot, _LiveStats(ctx))
    # A fresh age reads as bare "now" (the app-wide format_ago grammar — never "now ago").
    assert summary == "private · hash be · slot 3 · 1 msg · 1 unread · last now"


async def test_the_detail_summary_sheds_atoms_rather_than_wrapping(ctx: AppContext) -> None:
    """One line, on both platforms: the traffic atoms go before the line does.

    The identifying atoms are on the left and the describing ones on the right, so a line
    too long for the console loses what the reader could already see in the row they opened
    this screen from — never the channel's own identity.
    """
    from meshterm.core.models import ChatMessage
    from meshterm.platforms import PICOCALC, REGULAR, set_platform
    from meshterm.ui.channels import _detail_summary, _LiveStats

    device = await ctx.device()
    await device.set_channel(0, "Lakeside emergency net", bytes(range(16)))
    slot = next(s for s in await read_channel_slots(device) if s.idx == 0)
    for _ in range(3):
        ctx.repo.record_chat_message(
            ChatMessage(text="hi", is_channel=True, channel_id=slot.identity)
        )
    ctx.chat._unread[slot.conversation.key] = 2

    try:
        for platform in (REGULAR, PICOCALC):
            set_platform(platform)
            line = _detail_summary(ctx, slot, _LiveStats(ctx))
            assert cell_len(line) <= platform.readable_cols, (platform.name, line)
            assert line.startswith("private · hash be · slot 0")  # identity always survives
    finally:
        set_platform(REGULAR)


# -- muting channel notifications ---------------------------------------------


def test_mute_store_round_trips_and_persists(tmp_path: Path) -> None:
    """A muted channel is remembered across store reloads; unmuting forgets it."""
    from meshterm.core.mute_store import MuteStore

    path = tmp_path / "mutes.json"
    store = MuteStore(path)
    assert store.is_muted("wardriving-id") is False
    assert store.is_muted(None) is False  # an unresolved channel is never muted

    store.set_muted("wardriving-id", True)
    assert store.is_muted("wardriving-id") is True
    assert MuteStore(path).is_muted("wardriving-id") is True  # survived a reload from disk

    store.set_muted("wardriving-id", False)
    assert store.is_muted("wardriving-id") is False
    assert MuteStore(path).muted() == set()  # the unmute persisted too


async def test_muting_suppresses_unread_but_still_records(ctx: AppContext) -> None:
    """A muted channel's inbound message lands in history but never bumps the unread badge."""
    from meshterm.core.events import MeshEvent
    from meshterm.core.models import Message

    device = await ctx.device()
    await device.set_channel(0, "#wardriving", None)  # public, key derived from the name
    slot = (await read_channel_slots(device))[0]
    ctx.mute_store.set_muted(slot.identity, True)

    await ctx.chat.start()
    try:
        ctx.events.publish(
            MeshEvent.message_event(Message(text="auto beacon", channel=0, is_channel=True))
        )
        await ctx.chat._queue.join()
        # Muted: no unread accrued anywhere, but the transcript is all there.
        assert ctx.chat.unread(slot.conversation.key) == 0
        assert ctx.chat.unread_total() == 0
        stored = ctx.repo.recent_chat_messages(is_channel=True, channel_id=slot.identity)
        assert [m.text for m in stored] == ["auto beacon"]
    finally:
        await ctx.chat.stop()


async def test_unmuted_channel_still_bumps_unread(ctx: AppContext) -> None:
    """The control case: an ordinary (unmuted) channel does raise the unread badge."""
    from meshterm.core.events import MeshEvent
    from meshterm.core.models import Message

    device = await ctx.device()
    await device.set_channel(0, "Ops", bytes(range(16)))
    slot = (await read_channel_slots(device))[0]

    await ctx.chat.start()
    try:
        ctx.events.publish(MeshEvent.message_event(Message(text="hey", channel=0, is_channel=True)))
        await ctx.chat._queue.join()
        assert ctx.chat.unread(slot.conversation.key) == 1
    finally:
        await ctx.chat.stop()


async def test_toggle_mute_zeros_unread_and_persists(ctx: AppContext) -> None:
    """Muting through the detail toggle flips the store and clears the channel's unread."""
    from meshterm.ui.channels import _is_muted, _toggle_mute

    device = await ctx.device()
    await device.set_channel(0, "Ops", bytes(range(16)))
    slot = (await read_channel_slots(device))[0]
    ctx.chat._unread[slot.conversation.key] = 4  # as the service would after four arrivals
    assert ctx.chat.unread_total() == 4

    _toggle_mute(ctx, slot)  # mute
    assert _is_muted(ctx, slot) is True
    assert ctx.chat.unread(slot.conversation.key) == 0  # muting zeroed it
    assert ctx.chat.unread_total() == 0

    _toggle_mute(ctx, slot)  # unmute
    assert _is_muted(ctx, slot) is False


async def test_muted_channel_row_shows_bell_not_unread(ctx: AppContext) -> None:
    """A muted channel's list row carries the 🔕 glyph in place of an unread badge."""
    from meshterm.ui.channels import _LiveStats, _menu_items
    from meshterm.ui.tui import Choice

    device = await ctx.device()
    await device.set_channel(0, "#wardriving", None)
    slots = await read_channel_slots(device)
    slot = slots[0]
    ctx.chat._unread[slot.conversation.key] = 3  # stale unread that muting must not display
    ctx.mute_store.set_muted(slot.identity, True)

    _, items = _menu_items(ctx, slots, 8, _LiveStats(ctx))
    row = next(it for it in items if isinstance(it, Choice) and it.value == 0)
    plain = row.label.plain
    assert "🔕" in plain
    assert "● 3" not in plain  # the mute glyph replaces the unread badge


async def test_detail_mute_row_reflects_state(ctx: AppContext) -> None:
    """The detail's notifications row reads 'Mute' when on and 'Unmute' once muted."""
    from meshterm.ui.channels import _detail_items
    from meshterm.ui.tui import Choice

    device = await ctx.device()
    await device.set_channel(0, "Ops", bytes(range(16)))
    slot = (await read_channel_slots(device))[0]

    def mute_label() -> str:
        items = _detail_items(ctx, slot)
        row = next(it for it in items if isinstance(it, Choice) and it.value == "mute")
        return row.label.plain

    assert "🔕 Mute notifications" in mute_label()
    ctx.mute_store.set_muted(slot.identity, True)
    assert "🔔 Unmute notifications" in mute_label()


# -- the tool against the simulator -------------------------------------------


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    """A mock-backed application context with the plain (console) UI surface."""
    settings = Settings(config_dir=tmp_path, db_path=tmp_path / "chan.db")
    context = AppContext(
        console=Console(file=__import__("io").StringIO()),
        settings=settings,
        repo=Repository(settings.db_path),
        device_store=DeviceStore(tmp_path / "devices.json"),
        admin_store=AdminStore(tmp_path / "admin.json"),
        mock=True,
    )
    yield context
    context.repo.close()


async def test_cli_add_private_generates_key_and_lists(ctx: AppContext) -> None:
    """`add` with no secret creates a private channel with a random key; `list` sees it."""
    tool = ChannelsTool()
    result = await tool.run(ctx, {"cli_action": "add", "index": 1, "name": "Ops", "secret": None})
    assert result.summary == {"index": 1, "name": "Ops"}

    slots = await read_channel_slots(await ctx.device())
    slot = next(s for s in slots if s.idx == 1)
    assert slot.name == "Ops"
    assert not slot.is_public  # a random key, not derived from the name


async def test_cli_add_public_derives_key(ctx: AppContext) -> None:
    """`add` with a #-name creates a public channel keyed from the name."""
    tool = ChannelsTool()
    await tool.run(ctx, {"cli_action": "add", "index": 2, "name": "#general", "secret": None})
    slot = next(s for s in await read_channel_slots(await ctx.device()) if s.idx == 2)
    assert slot.name == "#general" and slot.is_public


async def test_cli_join_and_import_round_trip(ctx: AppContext) -> None:
    """A channel joined by key can be shared and re-imported to the same secret."""
    tool = ChannelsTool()
    secret = bytes(range(16))
    await tool.run(ctx, {"cli_action": "join", "index": 3, "name": "Squad", "secret": secret.hex()})
    slot = next(s for s in await read_channel_slots(await ctx.device()) if s.idx == 3)
    assert slot.name == "Squad" and slot.secret == secret

    # Importing the channel's own share link onto another slot reproduces it.
    await tool.run(ctx, {"cli_action": "import", "index": 4, "url": share_url("Squad", secret)})
    imported = next(s for s in await read_channel_slots(await ctx.device()) if s.idx == 4)
    assert imported.name == "Squad" and imported.secret == secret


async def test_cli_import_rejects_bad_link(ctx: AppContext) -> None:
    """Importing a non-channel link is a clean parameter error, not a crash."""
    import typer

    tool = ChannelsTool()
    with pytest.raises(typer.BadParameter):
        await tool.run(ctx, {"cli_action": "import", "index": 5, "url": "https://nope"})


async def test_cli_clear_removes_the_channel_from_its_slot(ctx: AppContext) -> None:
    """`clear` empties a configured slot; `list` no longer sees it."""
    tool = ChannelsTool()
    await tool.run(ctx, {"cli_action": "add", "index": 1, "name": "Ops", "secret": None})
    assert any(s.idx == 1 for s in await read_channel_slots(await ctx.device()))

    result = await tool.run(ctx, {"cli_action": "clear", "index": 1})
    assert result.summary == {"index": 1, "cleared": True}
    assert not any(s.idx == 1 for s in await read_channel_slots(await ctx.device()))


async def test_cli_clear_of_an_empty_slot_is_a_no_op(ctx: AppContext) -> None:
    """Clearing an already-empty slot reports nothing cleared rather than erroring."""
    tool = ChannelsTool()
    result = await tool.run(ctx, {"cli_action": "clear", "index": 7})
    assert result.summary == {"index": 7, "cleared": False}


class _RecordingSession:
    """Records which surface :meth:`TuiUi.present` chose — the popup or the result window."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    async def message_dialog(self, message, title: str = "") -> None:  # noqa: ANN001
        self.shown.append(("popup", title))

    async def scroll(self, renderable, *, title: str = "", footer_hint: str = "") -> None:  # noqa: ANN001
        self.shown.append(("window", title))


class _PresentingUi(_ScriptedUi):
    """Scripted answers, but real note buffering — so what the visit *shows* can be asserted."""

    def __init__(self, selects: list, texts: list, dialogs: list) -> None:
        from meshterm.ui.surface import TuiUi

        super().__init__(selects, texts, dialogs)
        self.session_spy = _RecordingSession()
        self.surface = TuiUi(self.session_spy)  # no ``.session`` attribute: stays headless

    def note(self, markup: str) -> None:
        self.surface.note(markup)

    def ack(self, markup: str) -> None:
        self.surface.ack(markup)


async def test_a_busy_visit_says_nothing_on_the_way_out(ctx: AppContext) -> None:
    """A visit that changed three channels closes silently — no popup, no window.

    The history this pins, in the order it happened: every action banked its own "✓ created
    …" note, so the outcome grew a line per change and the third pushed it past the
    acknowledgement popup's budget into the full-frame result window — the same visit
    reporting itself two different ways depending on how much had been done in it. Folding
    those into one summary line fixed the shape and left the real problem: an
    acknowledgement for changes the reader had just watched land in the list in front of
    them. So the line went too. The count survives in ``summary`` for the run log, which
    nobody reads off the screen.
    """
    ui = _PresentingUi(
        selects=[_CREATE, _CREATE, _CREATE, None],  # create three channels, then Esc
        texts=["One", "Two", "Three"],
        dialogs=[],
    )
    ctx.ui = ui
    result = await ChannelsTool().run(ctx, {})

    assert result.summary == {"changes": 3}  # the run log still records what was done
    assert result.message is None
    # The menu's own tail: note the tool's message, then present what the run buffered.
    if result.message:
        ui.surface.note(result.message)
    await ui.surface.present(title="Channels")
    assert ui.session_spy.shown == []  # nothing shown, in either shape


class _FlakyDevice:
    """A device whose channel reads stop working partway through the probe.

    ``fail_from`` is the first slot index that raises, and ``error`` is what it raises —
    the two endings the probe has to tell apart: a firmware refusing a slot it does not
    have (a plain rejection) and a link that stopped answering (a timeout).
    """

    def __init__(self, names: list[str], *, fail_from: int, error: BaseException) -> None:
        self._names = names
        self._fail_from = fail_from
        self._error = error
        self.written: list[tuple[int, str]] = []

    async def get_channel(self, idx: int):  # noqa: ANN201
        if idx >= self._fail_from:
            raise self._error
        if idx < len(self._names):
            return {"channel_name": self._names[idx], "channel_secret": bytes(range(16))}
        return None

    async def set_channel(self, idx: int, name: str, secret) -> None:  # noqa: ANN001
        self.written.append((idx, name))


async def test_a_probe_cut_short_by_a_failed_read_is_not_a_layout() -> None:
    """A timeout mid-probe reports incomplete; a refused slot reports a finished list.

    They look identical in the returned list — a short one either way — which is what let
    one timed-out read stand in for "this device has no channels".
    """
    from meshterm.core.channel_probe import probe_channel_slots
    from meshterm.core.connection import DeviceCommandError

    dropped = _FlakyDevice(["Alpha", "Beta"], fail_from=1, error=DeviceCommandError("no reply"))
    slots, complete = await probe_channel_slots(dropped)
    assert [s.name for s in slots] == ["Alpha"] and complete is False

    at_the_end = _FlakyDevice(["Alpha", "Beta"], fail_from=2, error=RuntimeError("out of range"))
    slots, complete = await probe_channel_slots(at_the_end)
    assert [s.name for s in slots] == ["Alpha", "Beta"] and complete is True


async def test_an_incomplete_probe_is_answered_but_never_cached(ctx: AppContext) -> None:
    """One bad read must not read as "no channels" for the rest of the session."""
    from meshterm.core.connection import DeviceCommandError

    device = await ctx.device()
    await device.set_channel(0, "Alpha", bytes(range(16)))

    calls = {"n": 0}
    real = device.get_channel

    async def flaky(idx: int):  # noqa: ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            raise DeviceCommandError("timed out")
        return await real(idx)

    device.get_channel = flaky  # type: ignore[method-assign]
    assert await ctx.devstate.channel_slots() == []  # the failed read, answered honestly
    # ...and not kept: the next ask re-probes and finds the channel that was there all along.
    assert [s.name for s in await ctx.devstate.channel_slots()] == ["Alpha"]


async def test_a_slot_is_confirmed_empty_before_a_channel_is_written_over_it(
    ctx: AppContext,
) -> None:
    """A slot missing from a short list must not be handed out as free.

    The dangerous version of the bug above: a probe that stopped at slot 1 leaves slot 1
    looking free, and every add flow writes to the slot it is given without asking.
    """
    from meshterm.ui.channels import _pick_free_slot

    device = await ctx.device()
    await device.set_channel(0, "Alpha", bytes(range(16)))
    await device.set_channel(1, "Beta", bytes(range(16, 32)))

    ui = _ScriptedUi(selects=[], texts=[], dialogs=[])
    ctx.ui = ui
    truncated = [ChannelSlot(idx=0, name="Alpha", secret=bytes(range(16)))]  # slot 1 unseen
    assert await _pick_free_slot(ctx, device, truncated, 8) is None  # refused, not slot 1

    # With the list telling the truth, the next genuinely free slot is handed out.
    full_list = list(await read_channel_slots(device))
    assert await _pick_free_slot(ctx, device, full_list, 8) == 2


async def test_a_rename_keeps_the_page_it_renamed(ctx: AppContext) -> None:
    """Renaming a channel re-reads it in place; only a clear closes the page.

    The channel is still there and this is still its page. It used to close on both, which
    left the reader back in the list to find the row again — for no better reason than the
    snapshot the page was holding having gone stale under the rename.
    """
    from meshterm.ui.channels import _CHAT, _EDIT, _channel_detail, _LiveStats

    device = await ctx.device()
    await device.set_channel(0, "Ops", bytes(range(16)))
    slot = next(s for s in await read_channel_slots(device) if s.idx == 0)

    opened: list[str] = []
    ui = _ScriptedUi(
        # Rename, then open chat (proving the page is still up and knows the new name), Esc.
        selects=[_EDIT, _CHAT, None],
        texts=["Lakeside", ""],  # the new name, and a blank key (derive it from the name)
        dialogs=[],
    )
    ctx.ui = ui

    import meshterm.ui.channels as channels_mod

    async def fake_chat(_ctx, opened_slot):  # noqa: ANN001, ANN202
        opened.append(opened_slot.name)

    original = channels_mod._open_chat
    channels_mod._open_chat = fake_chat
    try:
        changes = await _channel_detail(ctx, device, slot, _LiveStats(ctx))
    finally:
        channels_mod._open_chat = original

    assert opened == ["Lakeside"]  # the page stayed, pointing at the renamed channel
    assert changes == 1  # ...and still reports the rename when it finally closes


async def test_the_standard_public_channel_is_not_added_twice(ctx: AppContext) -> None:
    """A re-fired add is refused where it runs, not only where it is drawn.

    The menu drops the row once a slot holds the channel, but the row is dispatched by a
    sentinel: a press already on its way while the first write ran would add a second copy
    of a channel that has exactly one well-known key.
    """
    from meshterm.ui.channels import _add_default_public

    device = await ctx.device()
    ctx.ui = _ScriptedUi(selects=[], texts=[], dialogs=[])
    slots: list[ChannelSlot] = []

    assert await _add_default_public(ctx, device, slots, 8) == 1
    slots = list(await read_channel_slots(device))
    assert await _add_default_public(ctx, device, slots, 8) == 0  # refused, not duplicated
    assert [s.name for s in await read_channel_slots(device)] == ["Public"]


async def test_a_reorder_that_breaks_partway_says_so(ctx: AppContext) -> None:
    """A relay has no transaction under it, so a link that drops mid-way is reported."""
    from meshterm.ui.channels import _reorder_channels

    device = await ctx.device()
    await device.set_channel(0, "Alpha", bytes(range(16)))
    await device.set_channel(1, "Beta", bytes(range(16, 32)))
    slots = list(await read_channel_slots(device))

    said: list[str] = []

    class _Ui(_ScriptedUi):
        def note(self, markup: str) -> None:
            said.append(markup)

        async def reorder(self, title: str, labels: list):  # noqa: ANN201
            return [1, 0]  # swap them

    ctx.ui = _Ui(selects=[], texts=[], dialogs=[])

    real = device.set_channel
    calls = {"n": 0}

    async def flaky(idx: int, name: str, secret) -> None:  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("link dropped")
        await real(idx, name, secret)

    device.set_channel = flaky  # type: ignore[method-assign]
    assert await _reorder_channels(ctx, device, slots) == 0  # not counted as a clean reorder
    assert said and "reorder stopped partway" in said[0]
