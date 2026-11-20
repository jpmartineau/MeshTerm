# SPDX-License-Identifier: Apache-2.0
"""Tests for the recognition of a pixel image (MCOimg) in the body of a message.

The cases come from the reference test vectors of the format
(``docs/mcoimg-js/tests/v3-js-*-fixtures.json`` and ``v2-js-fixtures.json`` in the
HDDen/meshcore-open repository, MIT). Each one is the text of an image and the size and
the palette profile that its encoder used.
"""

from __future__ import annotations

import pytest

from meshterm.core.mcoimg import PixelImage, base91_decode, recognize

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
    assert recognize(text) == PixelImage(3, width, height, colours)


def test_base91_decodes_to_the_payload_of_the_vector() -> None:
    """``im3:g/5WjAL`NA`` is the app payload ``E3oBjQBCvgE=`` (base64) of the vectors."""
    assert base91_decode("g/5WjAL`NA") == bytes.fromhex("137a018d0042be01")


def test_a_version_1_or_2_image_has_no_header_to_read() -> None:
    assert recognize("im:CC&tj68MFSB") == PixelImage(None)


def test_spaces_at_the_ends_are_permitted() -> None:
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
    assert recognize(body) is None


def test_an_unknown_version_is_an_image_with_no_header() -> None:
    assert recognize("im4:CC&tj68MFSB") == PixelImage(4)
