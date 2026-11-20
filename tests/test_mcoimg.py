# SPDX-License-Identifier: Apache-2.0
"""Tests for the recognition of a pixel image (MCOimg) in the body of a message.

The cases come from the reference test vectors of the format
(``docs/mcoimg-js/tests/v3-js-*-fixtures.json`` and ``v2-js-fixtures.json`` in the
HDDen/meshcore-open repository, MIT). Each one is the text of an image and the size and
the palette profile that its encoder used.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest

from meshterm.core import mcoimg, mcoimg_palettes
from meshterm.core.mcoimg import PixelImage, base91_decode, recognize
from meshterm.core.mcoimg_decode import Picture, _decode_refs, decode

#: (text, width, height, colours) from the version 3 vectors: one case for each mode of
#: the dimensions and for each palette profile that the vectors use.
_V3_CASES = [
    ("im3:g/5WjAL`NA", 4, 2, 4),  # rawGlobal, master4, mode 1
    ("im3:g/LAU)", 1, 1, 4),  # solidBackground, the shortest image
    ("im3:g/)FCtB", 2, 2, 4),  # implicitWhiteSolid, mode 0
    ("im3:TAYL>iBNGbA", 9, 7, 2),  # mono, from the encoder vectors
]


@pytest.mark.parametrize(("text", "width", "height", "colours"), _V3_CASES)
def test_a_version_3_image_gives_its_size_and_colours(
    text: str, width: int, height: int, colours: int
) -> None:
    """The header of a version 3 image gives its size and the colours of its profile."""
    assert recognize(text) == PixelImage(3, width, height, colours)


def test_base91_decodes_to_the_payload_of_the_vector() -> None:
    """``im3:g/5WjAL`NA`` is the app payload ``E3oBjQBCvgE=`` (base64) of the vectors."""
    assert base91_decode("g/5WjAL`NA") == bytes.fromhex("137a018d0042be01")


def test_a_version_1_or_2_image_has_no_header_to_read() -> None:
    """An ``im:`` image is an image, but MeshTerm cannot read its header."""
    assert recognize("im:CC&tj68MFSB") == PixelImage(None)


def test_spaces_at_the_ends_are_permitted() -> None:
    """Spaces and newlines around the image do not stop the recognition."""
    assert recognize("  im3:g/5WjAL`NA \n") == PixelImage(3, 4, 2, 4)


@pytest.mark.parametrize(
    "body",
    [
        "hello",
        "im: ok",  # a person typed it
        "im:abc",  # too short to be an image with no header
        "im3:g/5WjAL`NA and more",  # an image is the whole body
        "see im3:g/5WjAL`NA",
        "im3:hello",  # Base91, but not the version 3 byte
        "im:CC&tj68M'SB",  # ' is not in the alphabet
    ],
)
def test_text_that_is_not_an_image(body: str) -> None:
    """Words that a person typed are not an image."""
    assert recognize(body) is None


def test_an_unknown_version_is_an_image_with_no_header() -> None:
    """A version that MeshTerm does not know is an image with no header."""
    assert recognize("im4:CC&tj68MFSB") == PixelImage(4)


# The pixel decoder of version 3.

_DATA = Path(__file__).parent / "data" / "mcoimg"

#: The palette profiles by name, in the order of their ids.
_PROFILE_NAMES = (
    "mono",
    "master4",
    "master8",
    "master16",
    "master32",
    "master64",
    "grayscale16",
    "grayscale32",
    "grayscale8",
    "dynamicGlobal8",
    "dynamicGlobal16",
    "dynamicGlobal32",
    "dynamicGlobal64",
    "dynamicGlobal128",
    "dynamicGlobal256",
    "dynamicGlobal512",
)


def _vectors() -> list[dict]:
    """All the vectors of the two files of ``tests/data/mcoimg/``."""
    found = []
    for name in ("v3-js-decoder-fixtures.json", "v3-js-encoder-fixtures.json"):
        found += json.loads((_DATA / name).read_text(encoding="utf-8"))["fixtures"]
    return found


_VECTORS = _vectors()


def test_the_vector_files_have_all_their_cases() -> None:
    """The two vector files have 39 and 6 cases."""
    assert len(_VECTORS) == 45


@pytest.mark.parametrize("vector", _VECTORS, ids=[vector["name"] for vector in _VECTORS])
def test_a_vector_decodes_to_its_palette_references(vector: dict) -> None:
    """Each vector decodes to its size, its profile, and the reference of each pixel."""
    indexed = _decode_refs(vector["text"])
    assert indexed is not None
    assert (indexed.width, indexed.height) == (vector["width"], vector["height"])
    assert _PROFILE_NAMES[indexed.profile] == vector["paletteProfile"]
    assert indexed.refs == vector["pixels"]
    assert indexed.transparent == vector.get("transparentColor")


def _rgb(profile: str, ref: int) -> tuple[int, int, int]:
    """The colour of a palette reference of a profile, from the colour tables."""
    if profile.startswith("dynamic"):
        return mcoimg_palettes.GLOBAL_512[ref]
    names = {
        "mono": "MONO",
        "grayscale8": "GREY_8",
        "grayscale16": "GREY_16",
        "grayscale32": "GREY_32",
    }
    name = names.get(profile) or "MASTER_" + profile.removeprefix("master")
    return getattr(mcoimg_palettes, name)[ref]


@pytest.mark.parametrize("vector", _VECTORS, ids=[vector["name"] for vector in _VECTORS])
def test_a_vector_decodes_to_the_colours_of_its_profile(vector: dict) -> None:
    """Each vector decodes to the RGB colours of its profile, with transparent pixels."""
    picture = decode(vector["text"])
    assert picture is not None
    assert (picture.width, picture.height) == (vector["width"], vector["height"])
    clear = vector.get("transparentColor")
    expected = tuple(
        None if ref == clear else _rgb(vector["paletteProfile"], ref) for ref in vector["pixels"]
    )
    assert picture.pixels == expected


def test_mono_is_white_and_black() -> None:
    """In the ``mono`` profile, colour 0 is white and colour 1 is black."""
    picture = decode("im3:TAYL>iBNGbA")  # mono-checker-normal, 9 x 7
    assert picture is not None
    assert picture.pixels[:2] == ((255, 255, 255), (0, 0, 0))
    assert set(picture.pixels) == {(255, 255, 255), (0, 0, 0)}


def test_the_transparent_colour_gives_no_colour() -> None:
    """A pixel of the transparent colour has no colour."""
    vector = next(v for v in _VECTORS if v["name"] == "master8-transparent-normal")
    picture = decode(vector["text"])
    assert picture is not None
    clear = [pixel is None for pixel in picture.pixels]
    assert clear == [ref == 0 for ref in vector["pixels"]]
    assert any(clear)
    assert not all(clear)


def test_the_colour_tables_have_their_sizes() -> None:
    """Each colour table has the size of its profile, and a dynamic table has no duplicates."""
    sizes = {
        "MONO": 2,
        "MASTER_4": 4,
        "MASTER_8": 8,
        "MASTER_16": 16,
        "MASTER_32": 32,
        "MASTER_64": 64,
        "GREY_8": 8,
        "GREY_16": 16,
        "GREY_32": 32,
        "GLOBAL_512": 512,
    }
    for name, size in sizes.items():
        assert len(getattr(mcoimg_palettes, name)) == size, name
    for size in (8, 16, 32, 64, 128, 256, 512):
        table = getattr(mcoimg_palettes, f"DYNAMIC_{size}")
        assert len(table) == size
        assert len(set(table)) == size
        assert all(0 <= index < 512 for index in table)
    assert mcoimg_palettes.DYNAMIC_512 == tuple(range(512))


def test_decode_reads_only_version_3() -> None:
    """The decoder gives ``None`` for an image of version 1 or 2 and for words."""
    assert decode("im:CC&tj68MFSB") is None
    assert decode("hello") is None
    assert decode("im3:hello") is None


@pytest.mark.parametrize("vector", _VECTORS, ids=[vector["name"] for vector in _VECTORS])
def test_a_truncated_image_gives_none(vector: dict) -> None:
    """An image with the end of its text cut off gives ``None``."""
    text = vector["text"]
    for end in range(len("im3:"), len(text) - 1):
        assert decode(text[:end]) is None


def test_a_changed_image_does_not_raise() -> None:
    """Change each character of each vector. The result can be an image or ``None``."""
    alphabet = mcoimg._ALPHABET
    for vector in _VECTORS:
        text = vector["text"]
        for index in range(len("im3:"), len(text)):
            for char in alphabet[::7]:
                decode(text[:index] + char + text[index + 1 :])


def test_random_base91_does_not_raise() -> None:
    """Random Base91 text after the prefix gives a picture or ``None``, and never an error."""
    chooser = random.Random(1234)
    for _ in range(3000):
        length = chooser.randint(1, 200)
        text = "".join(chooser.choice(mcoimg._ALPHABET) for _ in range(length))
        decode("im3:g/" + text)
        decode("im3:" + text)


def test_a_very_long_text_gives_none() -> None:
    """The decoder does not read a text that is longer than the limit."""
    assert decode("im3:g/" + "A" * 10_000) is None


def _encode_raw_global(width: int, height: int, profile: int, refs: list[int]) -> str:
    """Encode an image as ``compactBlock`` and ``rawGlobal``: the minimal encoder of the spec.

    Only square sizes up to 64 are done here (dimension mode 0).
    """
    assert width == height <= 64
    bits = max(1, (len(_PROFILE_COLOURS_BY_ID[profile]) - 1).bit_length())
    fields = [(0, 2), (width - 1, 6), (0x20, 8)] + [(ref, bits) for ref in refs]
    value = position = 0
    for field, count in fields:
        value |= field << position
        position += count
    data = bytes([0x13, 0x5A, profile]) + value.to_bytes((position + 7) // 8, "little")
    return "im3:" + _base91_encode(data)


def _base91_encode(data: bytes) -> str:
    """The basE91 encoder of the spec."""
    alphabet = mcoimg._ALPHABET
    out = []
    queue = count = 0
    for byte in data:
        queue |= byte << count
        count += 8
        if count > 13:
            value = queue & 8191
            if value > 88:
                queue >>= 13
                count -= 13
            else:
                value = queue & 16383
                queue >>= 14
                count -= 14
            out += [alphabet[value % 91], alphabet[value // 91]]
    if count:
        out.append(alphabet[queue % 91])
        if count > 7 or queue > 90:
            out.append(alphabet[queue // 91])
    return "".join(out)


_PROFILE_COLOURS_BY_ID = {5: mcoimg_palettes.MASTER_64}


def test_a_64_by_64_image_decodes_quickly() -> None:
    """A 64 x 64 image of 64 colours decodes in less than 50 ms."""
    chooser = random.Random(5)
    refs = [chooser.randrange(64) for _ in range(64 * 64)]
    text = _encode_raw_global(64, 64, 5, refs)
    start = time.perf_counter()
    picture = decode(text)
    elapsed = time.perf_counter() - start
    assert picture is not None
    assert picture.pixels == tuple(mcoimg_palettes.MASTER_64[ref] for ref in refs)
    assert elapsed < 0.05


def test_a_picture_draws_two_pixels_in_each_cell() -> None:
    """Half blocks: the top pixel is the colour, the bottom pixel is the background."""
    from meshterm.ui.pixelart import picture_text

    red, blue = (255, 0, 0), (0, 0, 255)
    picture = Picture(2, 3, (red, None, blue, red, red, None))
    lines = picture_text(picture, 10, indent=2).plain.split("\n")
    # Row 0 over row 1: red over blue, nothing over red. Row 2 alone: red, nothing.
    assert lines == ["  ▀▄", "  ▀ "]


def test_a_wide_picture_becomes_smaller_to_fit_the_lane() -> None:
    """An image wider than the lane keeps its proportions and fits the lane."""
    from meshterm.ui.pixelart import fit

    assert fit(64, 64, 40, 40) == (40, 40)
    assert fit(16, 16, 40, 40) == (16, 16)  # never larger
    assert fit(30, 30, 53, 5) == (10, 10)  # five rows hold ten pixels


def test_the_image_screen_makes_a_small_image_larger() -> None:
    """On its bare frame, a 4 x 2 image grows by a whole number to fill the frame."""
    from meshterm.ui.pixelart import fit

    assert fit(4, 2, 72, 22, grow=True) == (72, 36)  # 18 times: the width is the limit
    assert fit(16, 16, 53, 24, grow=True) == (48, 48)  # 3 times: 24 rows hold 48 pixels
    assert fit(4, 2, 72, 22) == (4, 2)  # under a message, never larger


@pytest.mark.parametrize(("cols", "rows"), [(72, 24), (120, 40), (53, 26), (53, 14)])
@pytest.mark.parametrize("text", ["im3:g/5WjAL`NA", "im3:TAYL>iBNGbA"])
def test_the_image_screen_centres_the_image_with_a_margin(text: str, cols: int, rows: int) -> None:
    """On its bare frame, the image is centred, a little above the middle, with page around it."""
    import re

    from meshterm.ui.pixelart import ImageScreen
    from meshterm.ui.tui.frame import compose_bare
    from meshterm.ui.widgets import pixel_image_text

    screen = ImageScreen(decode(text), pixel_image_text(recognize(text)))
    for _ in range(2):  # the first paint tells the screen its rows
        frame = compose_bare(screen, cols, rows)
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in frame.split("\n")]
    ink = [index for index, line in enumerate(lines) if line.strip()]
    above, below = ink[0], rows - 1 - ink[-1]
    assert 1 <= above <= below
    art = lines[ink[0]]
    left, right = len(art) - len(art.lstrip()), cols - len(art.rstrip())
    assert left >= 2 and right >= 2
    assert abs(left - right) <= 1


def test_the_image_screen_shows_the_text_of_the_message_under_the_caption() -> None:
    """Under the image and its caption, the full screen shows the ``im3:`` string itself."""
    import re

    from meshterm.ui.pixelart import ImageScreen
    from meshterm.ui.tui.frame import compose_bare
    from meshterm.ui.widgets import pixel_image_text

    text = "im3:TAYL>iBNGbA"
    screen = ImageScreen(decode(text), pixel_image_text(recognize(text)), text)
    for _ in range(2):
        frame = compose_bare(screen, 72, 24)
    lines = [re.sub(r"\x1b\[[0-9;]*m", "", line).strip() for line in frame.split("\n")]
    caption = next(i for i, line in enumerate(lines) if "image · 9×7 · 2 colours" in line)
    assert text in lines[caption + 1 :]
