# SPDX-License-Identifier: Apache-2.0
"""Tests for the platform seam.

The tests cover the resolution order, the singleton for the active platform, and the
checks of the two platform specs. The PicoCalc is strictly the narrower and plainer
flavour.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

import meshterm.platforms as platforms_mod
from meshterm.platforms import (
    PICOCALC_LYRA,
    REGULAR,
    get_platform,
    resolve,
    set_platform,
    without_emoji,
)


def test_regular_is_todays_behaviour() -> None:
    """REGULAR is the desktop terminal, with exactly the behaviour that it has without this seam."""
    assert REGULAR.name == "regular"
    assert REGULAR.width_reclaim is True
    assert REGULAR.emoji is True
    assert REGULAR.truecolor is True
    assert REGULAR.frame_border is True
    assert REGULAR.readable_cols == 72


def test_picocalc_is_the_narrower_plainer_flavour() -> None:
    """PICOCALC_LYRA never claims a capability that the console does not have."""
    assert PICOCALC_LYRA.name == "picocalc-lyra"
    assert PICOCALC_LYRA.readable_cols < REGULAR.readable_cols
    assert PICOCALC_LYRA.width_reclaim is False  # a phantom column would tear the exact-width frame
    assert PICOCALC_LYRA.emoji is False
    assert PICOCALC_LYRA.truecolor is False


def test_get_platform_defaults_to_regular() -> None:
    """Each test starts on REGULAR (refer to the autouse fixture in conftest.py)."""
    assert get_platform() is REGULAR


def test_set_platform_is_visible_through_get_platform() -> None:
    """``set_platform`` has an effect at once.

    Each caller that reads through ``get_platform()`` sees it.
    """
    set_platform(PICOCALC_LYRA)
    assert get_platform() is PICOCALC_LYRA
    set_platform(REGULAR)
    assert get_platform() is REGULAR


def test_set_platform_reaches_a_dotted_import_too() -> None:
    """A consumer that imports the module, and not the name, sees the change at once.

    This is the exact trap that the module docstring warns about. ``from meshterm.platforms
    import PLATFORM`` at the top level of a module does not see the change. Access through
    the module name (``platforms_mod.PLATFORM``) does see it. Each real binding point in
    this code base reads through :func:`get_platform`, and it also sees it.
    """
    set_platform(PICOCALC_LYRA)
    assert platforms_mod.PLATFORM is PICOCALC_LYRA


def test_resolve_defaults_to_regular_off_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no flag, no environment variable, and no device tree, the result is REGULAR.

    The source is 'default'. This is the case of each desktop and CI machine.
    """
    monkeypatch.delenv("MESHTERM_PLATFORM", raising=False)
    monkeypatch.setattr(platforms_mod, "_DEVICE_TREE_MODEL_PATH", Path("/nonexistent/model"))
    r = resolve()
    assert r.platform is REGULAR
    assert r.source == "default"
    assert r.flag is None and r.env is None and r.detected_model is None


def test_resolve_flag_wins_over_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit --platform overrides both the environment variable and the auto-detection."""
    monkeypatch.setenv("MESHTERM_PLATFORM", "picocalc-lyra")
    monkeypatch.setattr(platforms_mod, "_DEVICE_TREE_MODEL_PATH", Path("/nonexistent/model"))
    r = resolve("regular")
    assert r.platform is REGULAR
    assert r.source == "--platform flag"
    assert r.env == "picocalc-lyra"  # the result records it, but it did not decide the outcome


def test_resolve_env_wins_when_no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """MESHTERM_PLATFORM decides when the user did not pass --platform."""
    monkeypatch.setenv("MESHTERM_PLATFORM", "picocalc-lyra")
    monkeypatch.setattr(platforms_mod, "_DEVICE_TREE_MODEL_PATH", Path("/nonexistent/model"))
    r = resolve(None)
    assert r.platform is PICOCALC_LYRA
    assert r.source == "MESHTERM_PLATFORM env var"


def test_resolve_matching_is_case_insensitive_and_trims_space() -> None:
    """A flag value or an environment value matches in any case, and with extra whitespace."""
    assert resolve(" PicoCalc-Lyra ").platform is PICOCALC_LYRA
    assert resolve("REGULAR").platform is REGULAR


@pytest.mark.parametrize("old", ["picocalc", "cardputer"])
def test_the_old_device_names_are_refused_with_the_new_ones_listed(old: str) -> None:
    """The code refuses the old handheld names, and it lists the new names.

    ``picocalc`` and ``cardputer`` were renamed for the variant of the handheld, with no
    alias. M5Stack makes three Cardputers, and the PicoCalc takes several cores. Thus the
    bare names were ambiguous. A launcher that still passes one of them fails with a clear
    error that names the real choices. It does not go back to the desktop layout with no
    message.
    """
    with pytest.raises(ValueError) as exc:
        resolve(old)
    assert "picocalc-lyra" in str(exc.value) and "cardputer-zero" in str(exc.value)


def test_resolve_unknown_flag_raises_with_the_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown --platform gives a clear ValueError that lists the choices.

    A typing error in --platform must not go back to REGULAR with no message.
    """
    monkeypatch.delenv("MESHTERM_PLATFORM", raising=False)
    with pytest.raises(ValueError) as exc:
        resolve("laptop")
    message = str(exc.value)
    assert "laptop" in message and "--platform" in message
    assert "picocalc-lyra" in message and "regular" in message


def test_resolve_unknown_env_raises_only_when_it_would_decide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad environment variable is an error only when nothing else (the flag) overrides it."""
    monkeypatch.setenv("MESHTERM_PLATFORM", "laptop")
    # A valid flag wins completely. The result records the bad variable, and never checks it.
    assert resolve("regular").platform is REGULAR
    with pytest.raises(ValueError) as exc:
        resolve(None)
    assert "MESHTERM_PLATFORM" in str(exc.value)


def test_resolve_auto_detects_the_lyra_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """The auto-detection finds the Lyra model.

    The test uses the exact string from the handheld (P0, 2026-08-01), with the space and
    the NUL at the end.
    """
    monkeypatch.delenv("MESHTERM_PLATFORM", raising=False)

    class _FakePath:
        def read_bytes(self) -> bytes:
            return b"Luckfox Lyra \x00"

    monkeypatch.setattr(platforms_mod, "_DEVICE_TREE_MODEL_PATH", _FakePath())
    r = resolve()
    assert r.platform is PICOCALC_LYRA
    assert r.source == "auto-detect"
    assert r.detected_model == "Luckfox Lyra"  # the code removes the space and the NUL at the end


def test_resolve_auto_detect_is_conservative(monkeypatch: pytest.MonkeyPatch) -> None:
    """The auto-detection is careful.

    A device tree that names other hardware never gives a guess of picocalc-lyra.
    """
    monkeypatch.delenv("MESHTERM_PLATFORM", raising=False)

    class _FakePath:
        def read_bytes(self) -> bytes:
            return b"Raspberry Pi 4 Model B\x00"

    monkeypatch.setattr(platforms_mod, "_DEVICE_TREE_MODEL_PATH", _FakePath())
    r = resolve()
    assert r.platform is REGULAR
    assert r.detected_model == "Raspberry Pi 4 Model B"


# -- the emoji-less variant ------------------------------------------------------------


def test_without_emoji_moves_one_flag_and_nothing_else() -> None:
    """``without_emoji`` changes one flag and nothing else.

    A terminal that cannot draw emoji is not a different flavour. The classic Windows
    console has the width, the colour depth, the borders, and the keyboard of the desktop.
    Only the icons must change. Each other field must stay, and ``font`` is the most
    important one. If the code folded to the font of a handheld here, it would remove
    accents from names that the console renders correctly.
    """
    plain = without_emoji(REGULAR)
    assert plain.emoji is False
    assert plain.font == ""
    assert plain.truecolor is True
    assert plain.readable_cols == REGULAR.readable_cols
    assert plain.footer_fkeys is REGULAR.footer_fkeys

    changed = {
        f.name
        for f in dataclasses.fields(REGULAR)
        if getattr(plain, f.name) != getattr(REGULAR, f.name)
    }
    assert changed == {"emoji"}


def test_without_emoji_leaves_an_already_compact_platform_alone() -> None:
    """``without_emoji`` leaves a platform that is already compact alone.

    A platform whose icons are already compact comes back unchanged. It is the same
    object. A copy would compare equal, but it would break each ``is`` check that the suite
    makes against PICOCALC_LYRA.
    """
    assert without_emoji(PICOCALC_LYRA) is PICOCALC_LYRA


def test_the_emoji_less_variant_still_drives_the_theme() -> None:
    """The platform without emoji still controls the theme.

    The derived platform is a real platform. When code binds it, the code sends icons
    through the table. This is the purpose of the design. The flags of the seam are
    independent. Thus when one flag is cleared, the change reaches the glyph funnel, and
    the fold of the console font does not come with it.
    """
    from meshterm.ui.theme import fold_text, glyph

    set_platform(without_emoji(REGULAR))
    assert glyph("📡") == "☼"  # the advert icon, in the compact form
    assert fold_text("café Montréal") == "café Montréal"  # no fold
    assert get_platform().readable_cols == 72
