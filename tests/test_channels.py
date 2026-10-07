# SPDX-License-Identifier: Apache-2.0
"""Tests for the channel-management feature: the pure logic, the QR rendering, and the tool.

The logic (secret derivation, and the round trip of a share URL) needs no hardware. The
tool actions run against the :class:`MockDevice` simulator and a temporary database.
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
    """A derived key is sha256(name)[:16]. This is the public-channel scheme of the firmware."""
    assert derive_secret("#public") == sha256(b"#public").digest()[:16]
    assert len(derive_secret("#anything")) == CHANNEL_SECRET_BYTES
    assert derive_secret("#a") == derive_secret("#a")  # deterministic


def test_random_secret_is_16_unique_bytes() -> None:
    """A random private key is 16 bytes, and it (in practice) never repeats."""
    a, b = random_secret(), random_secret()
    assert len(a) == CHANNEL_SECRET_BYTES and len(b) == CHANNEL_SECRET_BYTES
    assert a != b


def test_normalize_secret_accepts_common_shapes() -> None:
    """A pasted key is accepted with 0x, spaces, or colons. The length is checked."""
    raw = "000102030405060708090a0b0c0d0e0f"
    expected = bytes(range(16))
    assert normalize_secret(raw) == expected
    assert normalize_secret("0x" + raw) == expected
    assert normalize_secret("0001 0203 0405 0607 0809 0a0b 0c0d 0e0f") == expected
    assert normalize_secret("00:01:02:03:04:05:06:07:08:09:0a:0b:0c:0d:0e:0f") == expected


@pytest.mark.parametrize("bad", ["", "abcd", "zz" * 16, "00" * 15, "00" * 17])
def test_normalize_secret_rejects_bad_keys(bad: str) -> None:
    """A key that is not hex, or has the wrong length, raises an error.

    Thus the prompt can ask again.
    """
    with pytest.raises(ValueError):
        normalize_secret(bad)


def test_channel_hash_is_two_hex_chars() -> None:
    """The channel hash is the first byte of sha256(secret), as the companion reports it."""
    secret = bytes(range(16))
    assert channel_hash(secret) == sha256(secret).hexdigest()[:2]
    assert len(channel_hash(secret)) == 2


def test_full_channel_hash_is_the_whole_digest_led_by_the_short_hash() -> None:
    """The full hash is the complete sha256(secret) digest, and it starts with the short hash."""
    secret = bytes(range(16))
    full = full_channel_hash(secret)
    assert full == sha256(secret).hexdigest()
    assert len(full) == 64
    assert full[:2] == channel_hash(secret)  # the first byte, which the screen lights


def test_share_url_round_trips_with_encoding() -> None:
    """A share URL builds and parses back to the same name and secret, with the encoding."""
    name, secret = "Ops Team #1", bytes(range(16))
    url = share_url(name, secret)
    assert url.startswith("meshcore://channel/add?")
    assert "Ops%20Team" in url  # the space is percent-encoded
    parsed = parse_share_url(url)
    assert parsed == (name, secret)


def test_parse_share_url_rejects_non_channel_links() -> None:
    """Text that is not a valid channel-add link parses to None."""
    assert parse_share_url("https://example.com") is None
    assert parse_share_url("meshcore://contact/add?name=x&public_key=ab") is None
    assert parse_share_url("meshcore://channel/add?name=x") is None  # missing secret
    assert parse_share_url("meshcore://channel/add?name=x&secret=nothex") is None
    assert parse_share_url("meshcore://channel/add?secret=" + "00" * 16) is None  # no name


# -- channel-text decryption ---------------------------------------------------


def _grp_txt_frame(
    name: str, secret: bytes, text: str, *, timestamp: int = 1_700_000_000, attempt: int = 0
) -> tuple[str, str, str]:
    """Build a GRP_TXT packet as the firmware does: (chan_hash, cipher_mac, crypted), all hex.

    This function does the encoding that :func:`~meshterm.core.channels.decrypt_channel_text`
    reverses. The plain data is a 4-byte little-endian timestamp, an attempt/type byte, and
    the text. Zero padding makes it a whole AES block. The function encrypts it in ECB mode.
    Then it makes a MAC with HMAC-SHA256, cut to 2 bytes.
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
    """A packet that is encrypted for a channel with a key that we hold decodes correctly."""
    name, secret = "#general", derive_secret("#general")
    chash, mac, crypted = _grp_txt_frame(name, secret, "hello mesh")
    result = decrypt_channel_text(chash, mac, crypted, [(name, secret)])
    assert result is not None
    assert result.channel_name == name
    assert result.text == "hello mesh"
    assert result.attempt == 0
    assert result.sent_at is not None


def test_decrypt_channel_text_reports_a_resend_attempt() -> None:
    """The resend counter of the sender is in the decrypted result."""
    name, secret = "Ops", random_secret()
    chash, mac, crypted = _grp_txt_frame(name, secret, "again", attempt=2)
    result = decrypt_channel_text(chash, mac, crypted, [(name, secret)])
    assert result is not None and result.attempt == 2


def test_decrypt_channel_text_returns_none_for_an_unknown_channel() -> None:
    """A packet from a channel with a key that we do not hold decrypts to nothing."""
    name, secret = "#general", derive_secret("#general")
    chash, mac, crypted = _grp_txt_frame(name, secret, "hello mesh")
    other = ("Ops", random_secret())
    assert decrypt_channel_text(chash, mac, crypted, [other]) is None
    assert decrypt_channel_text(chash, mac, crypted, []) is None


def test_decrypt_channel_text_rejects_a_fingerprint_collision() -> None:
    """A candidate with the hash byte of the packet but not its key fails the MAC check.

    The channel hash fingerprint has one byte. It can be the same for channels that have
    no relation to each other. The MAC is what confirms the key. Thus a wrong channel with
    the same fingerprint must not be taken as a match. Decryption must continue and try the
    next (correct) candidate. It must not stop at the collision.
    """
    name, secret = "#general", derive_secret("#general")
    chash, mac, crypted = _grp_txt_frame(name, secret, "hello mesh")
    # Find a decoy with the same fingerprint and a wrong key by brute force. The hash has
    # 1 byte, so the search is cheap.
    decoy = next(
        s
        for s in (random_secret() for _ in range(10_000))
        if channel_hash(s) == chash and s != secret
    )
    result = decrypt_channel_text(chash, mac, crypted, [("decoy", decoy), (name, secret)])
    assert result is not None and result.text == "hello mesh"  # continues to the real key


def test_decrypt_channel_text_rejects_malformed_hex() -> None:
    """Hex fields with garbage fail with no result, and they do not raise an error."""
    assert decrypt_channel_text("zz", "zzzz", "zzzzzzzzzzzzzzzz", [("x", random_secret())]) is None
    # a body of 15 bytes is not aligned to a block
    assert decrypt_channel_text("00", "0000", "00" * 15, [("x", random_secret())]) is None


# -- QR rendering -------------------------------------------------------------


def test_qr_text_renders_white_on_black_with_a_quiet_zone() -> None:
    """qr_text draws white ink on a black field, with any theme of the terminal.

    A camera reads contrast, and pure white on pure black is the highest contrast that a
    screen has. A scanner expects light on dark from a screen. The code names both colours,
    so the palette around it cannot make either colour weaker.
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
    # A QR code of the correct version for this URL is at least approximately 25 modules
    # wide, plus a quiet zone of 8 modules. The finder pattern makes the code large enough.
    assert len(lines[0]) >= 30
    assert any("█" in line for line in lines)  # dark modules were drawn


def test_a_code_fits_itself_to_the_frame_it_is_drawn_in() -> None:
    """A contact card is 57 cells and 29 rows at the standard fit, and frames are smaller.

    A code that is cut cannot be scanned. Thus the code does not get cut. It steps down: it
    uses a lighter error level first (a screen is never smudged), and then the narrower
    quiet zone. The PicoCalc panel is 53 cells across, and a regular terminal is 24 rows.
    """
    from rich.cells import cell_len

    url = "meshcore://contact/add?name=YUL-Cartierville&public_key=" + "ab" * 32 + "&type=2"
    standard = qr_text(url).plain.splitlines()
    assert len(standard[0]) > 53 and len(standard) > 24  # the standard fit is too big for both
    for width in (80, 53, 45):
        fitted = fit_qr(url, width).plain.splitlines()
        assert max(cell_len(line) for line in fitted) <= width, width
    assert len(fit_qr(url, 80, 24).plain.splitlines()) <= 24


def test_the_share_screen_refits_its_code_to_the_frame_every_paint() -> None:
    """The screen fits the code and the link first, then the code alone, and it centres the rows."""
    from rich.cells import cell_len

    url = "meshcore://contact/add?name=YUL-Cartierville&public_key=" + "ab" * 32 + "&type=2"
    screen = QrScreen(url, title="Share YUL-Cartierville")
    assert screen.bare and not screen.floating

    def code_rows(width: int, rows: int) -> list[str]:
        screen.note_viewport(rows)  # compose_bare gives this before it asks for the body
        lines = [_ANSI.sub("", line) for line in screen.render_body(width)]
        assert max(cell_len(line) for line in lines) <= width
        assert url[:40] in "".join(lines), "the link is under the code"
        blank = next(i for i in range(len(lines) - 1, -1, -1) if not lines[i].strip())
        return lines[:blank]  # the code is all the rows above the row that separates it

    roomy = code_rows(120, 40)
    assert len(roomy) == 29  # the standard fit, when there is room for it
    assert roomy[0].startswith(" " * 30), "centred across the whole width"
    # The code is centred as a block, never for each row. Each row has the same indent and
    # the same width, so the finder squares stack square. If each row is centred, the
    # centring removes the light modules at the end of the row before it adds the padding.
    # Then the bottom finder moves by one cell.
    assert len({len(row) for row in roomy}) == 1, "rows differ in width — a skewed code"
    finder = "█▀▀▀▀▀█"
    assert len({row.index(finder) for row in roomy if finder in row}) == 1, "a finder is skewed"
    assert len(code_rows(72, 24)) <= 24  # a regular terminal: the whole code, and the link below
    assert len(code_rows(53, 26)) <= 26  # the PicoCalc: the same


def test_several_links_are_one_share_screen_that_arrows_step_through() -> None:
    """←→ move between the codes and stop at the ends. The URL line shows which way has more.

    One link draws no arrows, because there is no other code to step to. Thus a share card
    keeps its bare frame as it was.
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
    screen.handle("right")  # clamped at the last, and it never goes back to the first
    assert screen.url == second
    assert "→" in screen._link().plain and screen._link().spans[-1].style == "muted"
    assert screen._link().spans[0].style == "accent"  # ← is lit: the first code is that way

    single = QrScreen(first, title="Share Lakeside")
    single.note_viewport(24)
    lines = [_ANSI.sub("", line) for line in single.render_body(72)]
    assert next(line for line in lines if "https://" in line).strip() == first


def test_where_braille_is_solid_every_code_is_drawn_in_it() -> None:
    """On the PicoCalc a code is braille: one module for each dot, and eight in a cell.

    Thus the contact card is 29 cells by 15 rows at the standard fit, and it shares the
    panel of 53x26 with its link. With half blocks, the link was one page down. The code of
    a page is also braille. Each module must stay after the packing, so the test decodes the
    braille and compares it with the matrix of the code. The braille of a desktop font is
    dotted, so the regular platform never draws a code in braille.
    """
    import segno

    from meshterm.platforms import PICOCALC_LYRA, set_platform

    def is_braille(text: str) -> bool:
        return any("⠀" <= ch <= "⣿" for ch in text)

    url = "meshcore://contact/add?name=YUL-Cartierville&public_key=" + "ab" * 32 + "&type=2"
    channel = "meshcore://channel/add?name=Test&secret=" + "ab" * 16
    assert not is_braille(fit_qr(url, 53, 22).plain) and not is_braille(qr_text(channel).plain)

    set_platform(PICOCALC_LYRA)
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
    """A slot with a # name, or with a key from its name, is public.

    A slot with a random key is private.
    """
    public = ChannelSlot(idx=0, name="#public", secret=derive_secret("#public"))
    assert public.is_public
    derived = ChannelSlot(idx=1, name="general", secret=derive_secret("general"))
    assert derived.is_public  # the key comes from the name, also with no # at the start
    private = ChannelSlot(idx=2, name="Ops", secret=bytes(range(16)))
    assert not private.is_public
    assert not private.is_name_derived
    # The firmware default "Public" is public, but its key is a fixed well-known secret, not
    # a key from its name. Thus it is public without a key from its name. The code must keep
    # its key.
    default_public = ChannelSlot(idx=0, name="Public", secret=DEFAULT_PUBLIC_SECRET)
    assert default_public.is_public
    assert not default_public.is_name_derived
    assert channel_hash(DEFAULT_PUBLIC_SECRET) == "11"  # the fingerprint that devices report for it
    assert private.conversation.is_channel and private.conversation.channel_idx == 2
    assert private.conversation.label == "Ops"  # the raw name, with no # added at the start
    # The key of the conversation is the identity of the channel, not its slot.
    assert private.conversation.channel_id == private.identity
    assert private.conversation.key == f"chan:{private.identity}"


def test_next_free_slot_finds_gaps_and_full() -> None:
    """The next free slot skips the indices that are used. It is None when each slot is taken."""
    slots = [
        ChannelSlot(idx=0, name="a", secret=b"\x00" * 16),
        ChannelSlot(idx=2, name="c", secret=b"\x00" * 16),
    ]
    assert _next_free_slot(slots, capacity=8) == 1
    full = [ChannelSlot(idx=i, name=str(i), secret=b"\x00" * 16) for i in range(8)]
    assert _next_free_slot(full, capacity=8) is None
    # If the discovered capacity is larger, the same set that is full at 8 still has room.
    assert _next_free_slot(full, capacity=16) == 8


async def test_channel_capacity_is_probed_not_assumed(ctx: AppContext) -> None:
    """The capacity comes from a probe of the device, and it counts slots, not their contents."""
    device = await ctx.device()
    assert await device.channel_capacity() == 8  # the mock simulates stock firmware with 8 slots
    # If some slots are used, the maximum must not change. The capacity is the number of slots.
    await device.set_channel(0, "Alpha", bytes(range(16)))
    await device.set_channel(3, "Bravo", bytes(range(16)))
    assert await device.channel_capacity() == 8


async def test_channel_capacity_tracks_a_larger_ceiling(ctx: AppContext) -> None:
    """Firmware with more slots is found to have more slots. The code has no fixed 8.

    This is the reason that the empty-run bound is not used for the discovery of the
    capacity. Firmware with more slots can have empty upper slots. The discovery must still
    find it when the device rejects a slot. The discovery must not stop early because of a
    run of empty slots.
    """
    device = await ctx.device()
    device._max_channels = 12  # simulate a firmware build with a larger slot table
    assert await device.channel_capacity() == 12


async def test_read_channel_slots_bounds_a_never_rejecting_probe() -> None:
    """A probe that the device never rejects still stops by itself.

    Some firmware answers for each slot. For this firmware, the scan of the configured slots
    stops after a run of empty slots. It does not go through all of ``CHANNEL_SLOT_PROBE_CAP``.
    """

    class NeverRejects:
        """A device that reports two channels, then empty slots with no end. It never raises."""

        def __init__(self) -> None:
            self.reads = 0

        async def get_channel(self, idx: int):
            self.reads += 1
            if idx in (0, 1):
                return {"channel_name": f"c{idx}", "channel_secret": b"\x00" * 16}
            return None  # an empty slot, and the device never rejects a higher index

    device = NeverRejects()
    slots = await read_channel_slots(device)  # type: ignore[arg-type]
    assert [s.name for s in slots] == ["c0", "c1"]  # both real channels found
    # The scan stopped after the two channels and one run of empty slots. It did not go
    # through all the 64 slots.
    assert device.reads == 2 + CHANNEL_SLOT_EMPTY_RUN
    assert device.reads < CHANNEL_SLOT_PROBE_CAP


async def test_read_channel_slots_scans_past_gaps_within_capacity() -> None:
    """A cleared slot in the middle (a gap) does not end the scan.

    The scan still reads the channels above the gap.

    The empty-run bound ends the scan only after as many empty slots, one after the other,
    as the stock capacity. A gap that is inside the capacity cannot be this long. Thus the
    scan still finds a channel that is above a gap, on firmware that never rejects a slot.
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

    # Channels at slots 0 and 7, with slots 1 to 6 empty (a gap of 6 slots, one slot shorter
    # than the stop run).
    device = NeverRejects({0: "low", 7: "high"})
    slots = await read_channel_slots(device)  # type: ignore[arg-type]
    assert [s.idx for s in slots] == [0, 7]  # the gap did not end the scan early


async def test_apply_order_relays_channels_into_new_positions(ctx: AppContext) -> None:
    """A new order puts the channels in the same slots again, in the order of the display."""
    device = await ctx.device()
    await device.set_channel(0, "Alpha", bytes(range(16)))  # private
    await device.set_channel(1, "#beta", None)  # public, the key comes from the name
    await device.set_channel(2, "Gamma", bytes(range(16, 32)))  # private

    slots = await read_channel_slots(device)
    # Reverse the display order: the row that was third moves to first, and the first row
    # moves to last.
    writes = await _apply_order(ctx, device, slots, [2, 1, 0])
    assert writes == 2  # the middle channel keeps its slot, and the two ends swap

    after = {s.idx: s for s in await read_channel_slots(device)}
    assert after[0].name == "Gamma" and after[0].secret == bytes(range(16, 32))
    assert after[1].name == "#beta" and after[1].is_public  # the public key is derived in place
    assert after[2].name == "Alpha" and after[2].secret == bytes(range(16))


def test_channel_identity_is_stable_across_slot_moves() -> None:
    """The identity of a channel depends on its key material, not on the slot that it uses."""
    at_two = ChannelSlot(idx=2, name="Ops", secret=bytes(range(16)))
    at_five = ChannelSlot(idx=5, name="Ops", secret=bytes(range(16)))
    assert at_two.identity == at_five.identity  # the same channel in another slot has this identity

    other = ChannelSlot(idx=2, name="Ops", secret=bytes(range(16, 32)))
    assert other.identity != at_two.identity  # a different key makes a different channel


class _ScriptedUi:
    """A UI surface that replays answers from a queue, to run the channel manager with no screen.

    ``select``, ``text``, and ``dialog`` each pop their next scripted answer. The methods
    that show output do nothing. This is enough of the :class:`~meshterm.ui.surface.Ui`
    contract for the create and clear flows of the channel manager.
    """

    def __init__(self, selects: list, texts: list, dialogs: list) -> None:
        self._selects = list(selects)
        self._texts = list(texts)
        self._dialogs = list(dialogs)

    def show(self, *renderables) -> None:  # noqa: ANN002
        pass

    @asynccontextmanager
    async def busy_dialog(self, message: str = "", *, title: str = ""):  # noqa: ANN201
        """Show no card, because there is no screen stack. Each path that changes data uses this."""
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
    """If a channel is cleared and another channel uses its slot, the histories must not mix.

    This test reproduces the reported bug. A received channel message has only a slot index.
    The chat service maps the index to a channel identity through a cache. When a slot is
    cleared and a new channel takes it, a stale cache filed the messages of the new channel
    under the old channel. Thus the old channel showed the transcript of the new channel.
    The manager must refresh the cache after the change, so that a message on the reused
    slot goes to the right channel.
    """
    from meshterm.core.channels import channel_identity
    from meshterm.core.events import MeshEvent
    from meshterm.core.models import Message
    from meshterm.ui.channels import _CLEAR, _CREATE, manage_channels

    device = await ctx.device()
    await device.set_channel(0, "Public", DEFAULT_PUBLIC_SECRET)
    public_id = channel_identity("Public", DEFAULT_PUBLIC_SECRET)

    await ctx.chat.start()  # fill the slot-to-identity cache (slot 0 is Public)
    try:
        # A message on Public arrives before we change anything, and it goes under Public.
        # The worker for received messages resolves it against the slot as it is now
        # (Public). This is what happens in a live run: MeshTerm stores messages when they
        # arrive, before any later edit.
        ctx.events.publish(
            MeshEvent.message_event(Message(text="hi public", channel=0, is_channel=True))
        )
        await ctx.chat._queue.join()

        # Drive the manager: open the detail of Public and clear it. Then create a new
        # private channel (which uses the freed slot 0), then leave with Esc.
        ctx.ui = _ScriptedUi(
            selects=[0, _CLEAR, _CREATE, None],
            texts=["Ops"],  # the name of the new channel
            dialogs=["clear"],  # confirm the clear on its Cancel/Clear dialog
        )
        await manage_channels(ctx)

        ops = next(s for s in await read_channel_slots(device) if s.name == "Ops")
        assert ops.idx == 0  # the new channel took the freed slot

        # A message now arrives on slot 0. The slot is Ops, not Public.
        ctx.events.publish(
            MeshEvent.message_event(Message(text="ops secret", channel=0, is_channel=True))
        )
        await ctx.chat._queue.join()

        public_msgs = ctx.repo.recent_chat_messages(is_channel=True, channel_id=public_id)
        ops_msgs = ctx.repo.recent_chat_messages(is_channel=True, channel_id=ops.identity)
        assert [m.text for m in public_msgs] == ["hi public"]  # no change, and no leak
        assert [m.text for m in ops_msgs] == ["ops secret"]  # filed under the right channel
    finally:
        await ctx.chat.stop()


async def test_reorder_keeps_history_because_key_is_intrinsic(ctx: AppContext) -> None:
    """If channels change order, each channel keeps its transcript. No migration is necessary.

    This test reproduces the setup of the reported bug (a channel moves to a new slot) and
    asserts the correction. The key of the history is the channel identity, not the slot.
    Thus the channel that moved keeps its own messages, and the slot that it moved to gets
    none of them.
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

        # Swap the slots of the two channels, then refresh the slot-to-identity cache of the
        # service.
        writes = await _apply_order(ctx, device, slots, [1, 0])
        assert writes == 2
        await chat.refresh_channels()

        after = {s.name: s for s in await read_channel_slots(device)}
        assert after["Alpha"].idx == 1 and after["Beta"].idx == 0  # the slots did swap
        assert after["Alpha"].identity == alpha_id  # the move does not change the identity

        # The history still resolves for each channel by identity. No channel got the
        # transcript of another channel.
        alpha_msgs = ctx.repo.recent_chat_messages(is_channel=True, channel_id=alpha_id)
        beta_msgs = ctx.repo.recent_chat_messages(is_channel=True, channel_id=beta_id)
        assert [m.text for m in alpha_msgs] == ["hello from alpha"]
        assert [m.text for m in beta_msgs] == ["hello from beta"]
    finally:
        await chat.stop()


# -- message statistics in the manager ----------------------------------------


def test_channel_stats_aggregates_totals_window_and_recency(ctx: AppContext) -> None:
    """The stats of a channel count all messages, the messages in the window, and the last time."""
    from datetime import timedelta

    from meshterm.core.models import ChatMessage, utcnow

    stale = utcnow() - timedelta(days=30)  # outside the activity window
    fresh = utcnow() - timedelta(minutes=2)
    for text, when in (("old a", stale), ("old b", stale), ("new", fresh)):
        ctx.repo.record_chat_message(
            ChatMessage(text=text, is_channel=True, channel_id="ops", created_at=when)
        )
    ctx.repo.record_chat_message(  # a direct message must not go into the channel stats
        ChatMessage(text="dm", is_channel=False, peer="abc123", created_at=fresh)
    )
    ctx.repo.record_chat_message(  # an old row with no identity has no channel to count under
        ChatMessage(text="legacy", is_channel=True, channel_id=None, created_at=fresh)
    )

    stats = ctx.repo.channel_stats()
    assert set(stats) == {"ops"}
    ops = stats["ops"]
    assert ops.total == 3
    assert ops.recent == 1  # only the fresh message is inside the window
    assert ops.last_at is not None
    assert abs((ops.last_at - fresh).total_seconds()) < 1
    # The histogram spans the window, newest first. The message that is 2 minutes old is in
    # the "now" (first) bucket, and the messages that are 30 days old are in no bucket. The
    # histogram has the full window (longer than the sparkline draws), so that the shared
    # peak for the scale has history.
    assert len(ops.histogram) == 72
    assert ops.histogram[0] == 1 and sum(ops.histogram) == 1


def test_activity_sparkline_packs_24_buckets_into_braille() -> None:
    """The sparkline draws two buckets in each cell, scaled to the shared peak.

    The newest bucket is on the right.
    """
    from meshterm.ui.channels import _activity_sparkline

    assert _activity_sparkline((0,) * 24, 0).plain == "⣀" * 12  # no traffic: a flat line
    assert _activity_sparkline((), 0).plain == "⣀" * 12  # a channel with no stats at all
    assert _activity_sparkline((21,) * 24, 21).plain == "⣿" * 12  # each bucket is at the peak
    # A bucket that is much lower than the shared peak is a single dot on the baseline, in
    # the right column of the last cell ("now" is at the right edge). Thus the glyph is the
    # same as the flat line, and the ok-green style marks it as traffic.
    lone = _activity_sparkline((1,) + (0,) * 23, 16)
    assert lone.plain == "⣀" * 12
    styles = [span.style for span in lone.spans]
    assert styles[-1] == "ok" and set(styles[:-1]) == {"faint"}
    # The heights scale to the peak. A ramp of 16, 12, 8, 4 climbs from a quarter to full,
    # and the newest pair (the fullest here) is in the cell on the right.
    ramp = _activity_sparkline((16, 12, 8, 4) + (0,) * 20, 16).plain
    assert ramp[-2:] == chr(0x2800 | 0x40 | 0xA0) + chr(0x2800 | 0x46 | 0xB8)
    # If the sparkline is drawn shorter, it keeps the newest buckets: the same ramp, with
    # less history.
    short = _activity_sparkline((16, 12, 8, 4) + (0,) * 20, 16, 3).plain
    assert len(short) == 3 and short[-2:] == ramp[-2:]


async def test_channel_rows_carry_stats_unread_and_lanes(ctx: AppContext) -> None:
    """A list row shows the channel type, the unread badge, the counts, the age, and the meter.

    The meter shows the activity.
    """
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
    ctx.chat._unread[slot.conversation.key] = 2  # as the service does after two arrivals

    title, items = _menu_items(ctx, slots, 8, _LiveStats(ctx))
    assert title == "Channels · 1/8 slots"  # a status atom is joined with ·, not an em dash
    row = next(it for it in items if isinstance(it, Choice) and it.value == 0)
    plain = row.label.plain  # the title is a live callable, and .label resolves it
    assert "Ops" in plain
    # There is no TYPE lane and no HASH lane. The glyph shows if the channel is open, and
    # Show key shows the hash. The cells that are free keep the activity lane visible at
    # 72 columns.
    assert "private" not in plain and slot.hash not in plain
    assert "● 2" in plain  # the unread badge
    assert "now" in plain  # the age of the message that MeshTerm just stored
    # Both messages that MeshTerm just stored are in the newest bucket of the sparkline. Only
    # this channel has traffic, so the bucket is the shared peak. Thus the right column of
    # the last cell is full height at the right edge of the row, and the rest of the window
    # is on the flat line.
    assert plain.rstrip().endswith("⣀" * 11 + chr(0x2800 | 0x40 | 0xB8))
    label = row.label
    assert label.spans[-1].style == "ok"  # and its newest cell shows live traffic

    # If the width is less than the whole row, the chart gets shorter to fit. It is not cut.
    # The row ends exactly at the edge, on the same newest cell, with less history behind it.
    natural = cell_len(plain)
    now_cell = chr(0x2800 | 0x40 | 0xB8)
    for width in (natural - 1, natural - 5, natural - 11):
        fitted = row.text(width).plain
        assert cell_len(fitted) == width and "…" not in fitted
        assert fitted.endswith(now_cell)
    # If no cell is left for the chart, MeshTerm leaves it out. It does not draw a small stub.
    lanes_only = row.text(natural - 13).plain
    assert now_cell not in lanes_only and "⣀" not in lanes_only


async def test_standard_public_row_appears_only_while_it_is_absent(ctx: AppContext) -> None:
    """The 'Standard Public channel' add action shows until a slot has the default fixed key."""
    from meshterm.core.channels import DEFAULT_PUBLIC_SECRET
    from meshterm.ui.channels import _DEFAULT_PUBLIC, _add_default_public, _LiveStats, _menu_items
    from meshterm.ui.tui import Choice

    device = await ctx.device()

    def has_default_row(slots: list) -> bool:
        _, items = _menu_items(ctx, slots, 8, _LiveStats(ctx))
        return any(isinstance(it, Choice) and it.value == _DEFAULT_PUBLIC for it in items)

    # The default is absent in an empty table, and while a channel that is not related to it
    # uses a slot.
    assert has_default_row([])
    await device.set_channel(1, "Ops", bytes(range(16)))
    slots = await read_channel_slots(device)
    assert has_default_row(slots)

    # If the user adds it, the well-known secret goes to the next free slot, and the offer
    # is removed.
    added = await _add_default_public(ctx, device, slots, 8)
    assert added == 1
    slots = await read_channel_slots(device)
    public = next(s for s in slots if s.secret == DEFAULT_PUBLIC_SECRET)
    assert public.name == "Public" and public.is_public
    assert not has_default_row(slots)


async def test_clearing_a_channel_reads_off_its_unread(ctx: AppContext) -> None:
    """If a channel is cleared, its unread count leaves the global total, not only the slot."""
    from meshterm.ui.channels import _clear

    device = await ctx.device()
    await device.set_channel(0, "Ops", bytes(range(16)))
    slot = (await read_channel_slots(device))[0]
    ctx.chat._unread[slot.conversation.key] = 3  # as the service does after three arrivals
    assert ctx.chat.unread_total() == 3

    ctx.ui = _ScriptedUi(selects=[], texts=[], dialogs=["clear"])
    assert await _clear(ctx, device, slot) is True
    assert ctx.chat.unread_total() == 0


async def test_show_key_popup_wraps_values_under_header_labels() -> None:
    """The key dialog is blocks with labels, not a table, and long values wrap and stay whole."""
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

    # Render at a dialog width that is narrow on purpose. Each value must stay whole, wrapped
    # across lines, with no ellipsis cut anywhere.
    lines = [_ANSI.sub("", line) for line in render_lines(captured["renderable"], 40)]
    text = "\n".join(lines)
    for label in ("NAME", "TYPE", "HASH", "KEY", "LINK"):
        assert label in text
    assert "…" not in text
    joined = text.replace("\n", "").replace(" ", "")
    assert slot.secret.hex() in joined  # the key of 32 hex digits, joined again after the wraps
    assert full_channel_hash(slot.secret) in joined  # the hash of 64 hex digits, the same
    assert share_url("Ops", slot.secret).replace(" ", "") in joined


async def test_detail_summary_reads_slot_totals_and_unread(ctx: AppContext) -> None:
    """The summary line of the detail screen has the slot, the totals, the unread count, and age."""
    from meshterm.core.models import ChatMessage
    from meshterm.ui.channels import _detail_summary, _LiveStats

    device = await ctx.device()
    await device.set_channel(3, "Ops", bytes(range(16)))
    slot = next(s for s in await read_channel_slots(device) if s.idx == 3)

    # What the channel is comes first. The openness and the hash moved here from a title that
    # was 42 cells wide. That title left no room on the console for the bar that shows that
    # Esc leaves.
    assert _detail_summary(ctx, slot, _LiveStats(ctx)) == (
        "private · hash be · slot 3 · no messages yet"
    )

    ctx.repo.record_chat_message(ChatMessage(text="hi", is_channel=True, channel_id=slot.identity))
    ctx.chat._unread[slot.conversation.key] = 1
    summary = _detail_summary(ctx, slot, _LiveStats(ctx))
    # A new age is the bare word "now" (the format_ago grammar of the whole app, never
    # "now ago").
    assert summary == "private · hash be · slot 3 · 1 msg · 1 unread · last now"


async def test_the_detail_summary_sheds_atoms_rather_than_wrapping(ctx: AppContext) -> None:
    """The summary is one line on both platforms: the traffic atoms go before the line wraps.

    The atoms that identify the channel are on the left, and the atoms that describe it are
    on the right. Thus a line that is too long for the console loses what the user could
    already see in the row from which the user opened this screen. It never loses the
    identity of the channel.
    """
    from meshterm.core.models import ChatMessage
    from meshterm.platforms import PICOCALC_LYRA, REGULAR, set_platform
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
        for platform in (REGULAR, PICOCALC_LYRA):
            set_platform(platform)
            line = _detail_summary(ctx, slot, _LiveStats(ctx))
            assert cell_len(line) <= platform.readable_cols, (platform.name, line)
            assert line.startswith("private · hash be · slot 0")  # the identity always stays
    finally:
        set_platform(REGULAR)


# -- muting channel notifications ---------------------------------------------


def test_mute_store_round_trips_and_persists(tmp_path: Path) -> None:
    """A muted channel is remembered after the store loads again.

    If the user unmutes the channel, the store forgets it.
    """
    from meshterm.core.mute_store import MuteStore

    path = tmp_path / "mutes.json"
    store = MuteStore(path)
    assert store.is_muted("wardriving-id") is False
    assert store.is_muted(None) is False  # a channel that is not resolved is never muted

    store.set_muted("wardriving-id", True)
    assert store.is_muted("wardriving-id") is True
    assert MuteStore(path).is_muted("wardriving-id") is True  # it stayed after a new read from disk

    store.set_muted("wardriving-id", False)
    assert store.is_muted("wardriving-id") is False
    assert MuteStore(path).muted() == set()  # the unmute was also stored


async def test_muting_suppresses_unread_but_still_records(ctx: AppContext) -> None:
    """A message on a muted channel goes to the history, but never raises the unread badge."""
    from meshterm.core.events import MeshEvent
    from meshterm.core.models import Message

    device = await ctx.device()
    await device.set_channel(0, "#wardriving", None)  # public, the key comes from the name
    slot = (await read_channel_slots(device))[0]
    ctx.mute_store.set_muted(slot.identity, True)

    await ctx.chat.start()
    try:
        ctx.events.publish(
            MeshEvent.message_event(Message(text="auto beacon", channel=0, is_channel=True))
        )
        await ctx.chat._queue.join()
        # The channel is muted: no unread count grew anywhere, but the whole transcript is
        # there.
        assert ctx.chat.unread(slot.conversation.key) == 0
        assert ctx.chat.unread_total() == 0
        stored = ctx.repo.recent_chat_messages(is_channel=True, channel_id=slot.identity)
        assert [m.text for m in stored] == ["auto beacon"]
    finally:
        await ctx.chat.stop()


async def test_unmuted_channel_still_bumps_unread(ctx: AppContext) -> None:
    """This is the control case: a normal (unmuted) channel does raise the unread badge."""
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
    """If the user mutes with the toggle on the detail screen, the unread count clears.

    The store also changes.
    """
    from meshterm.ui.channels import _is_muted, _toggle_mute

    device = await ctx.device()
    await device.set_channel(0, "Ops", bytes(range(16)))
    slot = (await read_channel_slots(device))[0]
    ctx.chat._unread[slot.conversation.key] = 4  # as the service does after four arrivals
    assert ctx.chat.unread_total() == 4

    _toggle_mute(ctx, slot)  # mute
    assert _is_muted(ctx, slot) is True
    assert ctx.chat.unread(slot.conversation.key) == 0  # the mute set it to zero
    assert ctx.chat.unread_total() == 0

    _toggle_mute(ctx, slot)  # unmute
    assert _is_muted(ctx, slot) is False


async def test_muted_channel_row_shows_bell_not_unread(ctx: AppContext) -> None:
    """The list row of a muted channel has the 🔕 glyph instead of an unread badge."""
    from meshterm.ui.channels import _LiveStats, _menu_items
    from meshterm.ui.tui import Choice

    device = await ctx.device()
    await device.set_channel(0, "#wardriving", None)
    slots = await read_channel_slots(device)
    slot = slots[0]
    ctx.chat._unread[slot.conversation.key] = 3  # an old unread count that a muted row hides
    ctx.mute_store.set_muted(slot.identity, True)

    _, items = _menu_items(ctx, slots, 8, _LiveStats(ctx))
    row = next(it for it in items if isinstance(it, Choice) and it.value == 0)
    plain = row.label.plain
    assert "🔕" in plain
    assert "● 3" not in plain  # the mute glyph takes the place of the unread badge


async def test_detail_mute_row_reflects_state(ctx: AppContext) -> None:
    """The notifications row of the detail screen reads 'Mute' when on, and 'Unmute' when muted."""
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
    """An application context with a mock device and the plain (console) UI surface."""
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
    """`add` with no secret creates a private channel with a random key, and `list` shows it."""
    tool = ChannelsTool()
    result = await tool.run(ctx, {"cli_action": "add", "index": 1, "name": "Ops", "secret": None})
    assert result.summary == {"index": 1, "name": "Ops"}

    slots = await read_channel_slots(await ctx.device())
    slot = next(s for s in slots if s.idx == 1)
    assert slot.name == "Ops"
    assert not slot.is_public  # a random key, not a key from the name


async def test_cli_add_public_derives_key(ctx: AppContext) -> None:
    """`add` with a name that starts with # creates a public channel with a key from the name."""
    tool = ChannelsTool()
    await tool.run(ctx, {"cli_action": "add", "index": 2, "name": "#general", "secret": None})
    slot = next(s for s in await read_channel_slots(await ctx.device()) if s.idx == 2)
    assert slot.name == "#general" and slot.is_public


async def test_cli_join_and_import_round_trip(ctx: AppContext) -> None:
    """A channel that the user joined with a key can be shared and imported with the same secret."""
    tool = ChannelsTool()
    secret = bytes(range(16))
    await tool.run(ctx, {"cli_action": "join", "index": 3, "name": "Squad", "secret": secret.hex()})
    slot = next(s for s in await read_channel_slots(await ctx.device()) if s.idx == 3)
    assert slot.name == "Squad" and slot.secret == secret

    # If the user imports the share link of the channel into another slot, it makes the same
    # channel again.
    await tool.run(ctx, {"cli_action": "import", "index": 4, "url": share_url("Squad", secret)})
    imported = next(s for s in await read_channel_slots(await ctx.device()) if s.idx == 4)
    assert imported.name == "Squad" and imported.secret == secret


async def test_cli_import_rejects_bad_link(ctx: AppContext) -> None:
    """If the user imports a link that is not a channel link, the result is a parameter error.

    It is not a crash.
    """
    import typer

    tool = ChannelsTool()
    with pytest.raises(typer.BadParameter):
        await tool.run(ctx, {"cli_action": "import", "index": 5, "url": "https://nope"})


async def test_cli_clear_removes_the_channel_from_its_slot(ctx: AppContext) -> None:
    """`clear` empties a configured slot, and `list` does not show it again."""
    tool = ChannelsTool()
    await tool.run(ctx, {"cli_action": "add", "index": 1, "name": "Ops", "secret": None})
    assert any(s.idx == 1 for s in await read_channel_slots(await ctx.device()))

    result = await tool.run(ctx, {"cli_action": "clear", "index": 1})
    assert result.summary == {"index": 1, "cleared": True}
    assert not any(s.idx == 1 for s in await read_channel_slots(await ctx.device()))


async def test_cli_clear_of_an_empty_slot_is_a_no_op(ctx: AppContext) -> None:
    """If the user clears a slot that is already empty, the result is "nothing cleared".

    It is not an error.
    """
    tool = ChannelsTool()
    result = await tool.run(ctx, {"cli_action": "clear", "index": 7})
    assert result.summary == {"index": 7, "cleared": False}


class _RecordingSession:
    """Records which surface :meth:`TuiUi.present` chose: the dialog or the result screen."""

    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    async def message_dialog(self, message, title: str = "") -> None:  # noqa: ANN001
        self.shown.append(("popup", title))

    async def scroll(self, renderable, *, title: str = "", footer_hint: str = "") -> None:  # noqa: ANN001
        self.shown.append(("window", title))


class _PresentingUi(_ScriptedUi):
    """Scripted answers, but real note buffering. Thus the test can assert what the visit shows."""

    def __init__(self, selects: list, texts: list, dialogs: list) -> None:
        from meshterm.ui.surface import TuiUi

        super().__init__(selects, texts, dialogs)
        self.session_spy = _RecordingSession()
        self.surface = TuiUi(self.session_spy)  # no ``.session`` attribute, so it stays headless

    def note(self, markup: str) -> None:
        self.surface.note(markup)

    def ack(self, markup: str) -> None:
        self.surface.ack(markup)


async def test_a_busy_visit_says_nothing_on_the_way_out(ctx: AppContext) -> None:
    """A visit that changed three channels closes silently, with no dialog and no result screen.

    This is the history that the test pins, in the order that it happened. Each action
    stored its own "✓ created …" note. Thus the outcome grew one line for each change, and
    the third line made it longer than the budget of the acknowledgement dialog. Then the
    outcome went to the full-frame result screen. The same visit reported itself in two
    different ways, which depended on the number of changes. We combined the notes into one
    summary line. This corrected the shape but not the real problem: the user got an
    acknowledgement for changes that the user had just watched arrive in the list. So we
    removed the line too. The count stays in ``summary`` for the run log, which nobody reads
    on the screen.
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
    # The end of the menu code: note the message of the tool, then present what the run
    # buffered.
    if result.message:
        ui.surface.note(result.message)
    await ui.surface.present(title="Channels")
    assert ui.session_spy.shown == []  # nothing shown, in either form


class _FlakyDevice:
    """A device whose channel reads stop to work in the middle of the probe.

    ``fail_from`` is the first slot index that raises an error, and ``error`` is the error
    that it raises. The probe must tell apart these two endings. In one, the firmware
    refuses a slot that it does not have (a plain rejection). In the other, the link stopped
    to answer (a timeout).
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
    """A timeout in the probe gives an incomplete result. A refused slot gives a finished list.

    The two lists are the same in the returned list: it is short in both cases. This was
    the reason that one read with a timeout could mean "this device has no channels".
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
    """One bad read must not mean "no channels" for the rest of the session."""
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
    assert await ctx.devstate.channel_slots() == []  # the read failed, and the answer says so
    # The answer is not kept. The next request probes again and finds the channel that was
    # there from the start.
    assert [s.name for s in await ctx.devstate.channel_slots()] == ["Alpha"]


async def test_a_slot_is_confirmed_empty_before_a_channel_is_written_over_it(
    ctx: AppContext,
) -> None:
    """A slot that is not in a short list must not be offered as free.

    This is the dangerous version of the bug above. A probe that stopped at slot 1 makes
    slot 1 look free, and each add flow writes to the slot that it gets and does not ask.
    """
    from meshterm.ui.channels import _pick_free_slot

    device = await ctx.device()
    await device.set_channel(0, "Alpha", bytes(range(16)))
    await device.set_channel(1, "Beta", bytes(range(16, 32)))

    ui = _ScriptedUi(selects=[], texts=[], dialogs=[])
    ctx.ui = ui
    truncated = [ChannelSlot(idx=0, name="Alpha", secret=bytes(range(16)))]  # slot 1 not heard
    assert await _pick_free_slot(ctx, device, truncated, 8) is None  # refused, not slot 1

    # If the list is correct, the function returns the next slot that is really free.
    full_list = list(await read_channel_slots(device))
    assert await _pick_free_slot(ctx, device, full_list, 8) == 2


async def test_a_rename_keeps_the_page_it_renamed(ctx: AppContext) -> None:
    """If the user renames a channel, the page reads it again in place.

    Only a clear closes the page.

    The channel is still there, and this is still its page. The page once closed for both
    actions. Then the user was in the list and had to find the row again. The only reason
    was that the snapshot of the page was stale after the rename.
    """
    from meshterm.ui.channels import _CHAT, _EDIT, _channel_detail, _LiveStats

    device = await ctx.device()
    await device.set_channel(0, "Ops", bytes(range(16)))
    slot = next(s for s in await read_channel_slots(device) if s.idx == 0)

    opened: list[str] = []
    ui = _ScriptedUi(
        # Rename, then open chat (this proves that the page is still open and knows the new
        # name), then Esc.
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

    assert opened == ["Lakeside"]  # the page stayed, for the channel with the new name
    assert changes == 1  # and it still reports the rename when it closes


async def test_the_standard_public_channel_is_not_added_twice(ctx: AppContext) -> None:
    """An add that fires again is refused where it runs, not only where the menu draws it.

    The menu removes the row when a slot has the channel. But a sentinel dispatches the row.
    If a key press was already on its way while the first write ran, it would add a second
    copy of a channel that has exactly one well-known key.
    """
    from meshterm.ui.channels import _add_default_public

    device = await ctx.device()
    ctx.ui = _ScriptedUi(selects=[], texts=[], dialogs=[])
    slots: list[ChannelSlot] = []

    assert await _add_default_public(ctx, device, slots, 8) == 1
    slots = list(await read_channel_slots(device))
    assert await _add_default_public(ctx, device, slots, 8) == 0  # refused, not copied
    assert [s.name for s in await read_channel_slots(device)] == ["Public"]


async def test_a_reorder_that_breaks_partway_says_so(ctx: AppContext) -> None:
    """A relay has no transaction under it, so the test reports a link that drops in the middle."""
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
    assert await _reorder_channels(ctx, device, slots) == 0  # not counted as a complete reorder
    assert said and "reorder stopped partway" in said[0]
