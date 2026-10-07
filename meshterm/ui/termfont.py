# SPDX-License-Identifier: Apache-2.0
"""Terminal capability detection: what can this terminal draw?

There are two questions. MeshTerm answers both out-of-band, because it cannot ask a
terminal in-band. A glyph that the terminal does not have still occupies its cell, so the
screen looks the same if the character arrived or not.

**Powerline separators**, below, are a question about the font. MeshTerm reads the
configured face of the terminal and matches it against fonts that are known to have the
block.

**Emoji** (:func:`emoji_support`) are a question about the host, and only on Windows. The
classic console (``conhost``, which a program that the user double-clicks still gets)
rasterizes through GDI with the console font and nothing behind it. Thus an emoji is a
replacement box, in any font configuration. All the hosts that replaced it draw emoji
correctly. So the verdict identifies the host, from its structure and not from the
environment. (``WT_SESSION`` is inherited across process launches. A program that starts
from Windows Terminal into a console of its own still says that it is in Windows
Terminal.) A ``False`` sends each icon through the compact single-glyph table
:func:`meshterm.ui.theme.glyph`, which MeshTerm already keeps for the PicoCalc. All of
this vocabulary is in the BMP, so it draws wherever the plain status marks draw.

Both verdicts are hints that pick a default. Each has an override for the user who knows
better than the probe: ``MESHTERM_POWERLINE`` and ``MESHTERM_EMOJI``.

The path-line widget (:mod:`~meshterm.ui.pathline`) can render a sequence of hops as
interlocking powerline segments. Each hop is a chip with a colour fill. The solid
triangle U+E0B0 joins the chips. Its foreground is the fill of the previous chip, and its
background is the fill of the next chip (the oh-my-posh look). That triangle is in the
Private Use Area, so it draws only when the configured font of the terminal (or the
terminal itself) supplies the glyph. A font without the glyph shows tofu boxes. MeshTerm
cannot ask in-band if the terminal has this glyph. A missing glyph still occupies its
cell, so even a probe of the cursor position sees nothing. Thus this is a problem of
out-of-band detection. MeshTerm identifies the terminal, reads its configuration, and
matches the configured face against fonts that are known to have the powerline block.

There are four sources of truth, in the order of confidence:

* **An explicit override.** ``MESHTERM_POWERLINE`` (``1``/``full``, ``core``,
  ``0``/``off``) always wins. It is the same gate pattern as ``MESHTERM_FULL_WIDTH``. The
  user knows their own display better than any probe.
* **The font of a handheld.** Where the platform names a glyph contract
  (:attr:`~meshterm.platforms.Platform.font`), the render boundary folds each frame down
  to that font, in any terminal that started the app. Thus the inventory is the answer,
  and a question to the terminal is a question to the wrong display. The panel of the
  Cardputer is Terminus, which has the core chevrons. The 512-glyph console font of the
  PicoCalc draws the two chevrons in two donor slots. Thus the paths are chips on both
  handhelds. MeshTerm does not probe: :data:`~meshterm.ui.fontset.FONTS` already says
  what each font can draw.
* **The configured font**, found for each terminal. The terminals are Windows Terminal
  (``WT_SESSION`` + ``WT_PROFILE_ID`` → the ``font.face`` of the profile in
  ``settings.json``), the integrated terminal of VS Code (``TERM_PROGRAM=vscode`` →
  ``terminal.integrated.fontFamily``, the workspace before the user, with
  ``editor.fontFamily`` as the fallback), and classic conhost (``GetCurrentConsoleFontEx``,
  which MeshTerm trusts only when it detects no ConPTY host, because the hidden conhost
  under a ConPTY host reports a stub face and not what is on the screen). MeshTerm
  matches the face against :data:`RECOMMENDED_FONTS` (the list of fonts that MeshTerm
  recommends to users). The list has the alias spellings that Nerd Fonts use (``Hack Nerd
  Font Mono`` / ``Hack NFM``). A generic rule, "any Nerd Font", is also in the match,
  because each Nerd Font patch has the full powerline block.
* **The renderer of the terminal.** Several terminals draw the core triangles in any
  font. Windows Terminal falls back to its bundled Cascadia Code NF for the symbol ranges
  (version 1.22 and later). The terminal of VS Code draws them as custom glyphs
  (``terminal.integrated.customGlyphs``, on by default). kitty, WezTerm, and Alacritty
  have built-in powerline glyphs. These terminals count as ``core`` support, also when
  the configured font matches nothing.

There are two levels of coverage. ``full`` is a Nerd Font patch: the triangles and the
extended block (rounded caps and slants). ``core`` is only U+E0B0–U+E0B3, which many
stock coder fonts (Fira Code, JetBrains Mono, Source Code Pro) have natively. The path
widget uses only the core triangle, so ``core`` is enough to switch it on. ``full`` is
more than the widget needs, for more complex chrome. When MeshTerm cannot know the
answer, for example in an ssh session (the font is on the machine of the far client) or
in an unrecognized terminal, the verdict is ``unknown``. Then callers must keep the
plain-arrow rendering.

Each probe does its best. Unreadable settings, missing registry keys, or a failed Win32
call lower the verdict, and never raise an exception. The result is a hint that picks a
default. It is not a gate that the user must fight.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..core import win32dll
from ..platforms import Platform, on_platform
from .fontset import FONTS

#: The levels of coverage that a verdict (or a recommended font) can have, the strongest first.
FULL = "full"
CORE = "core"
NONE = "none"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class RecommendedFont:
    """One font that MeshTerm recommends for the rendering of powerline paths.

    Attributes:
        name: The canonical display name (what a screen of recommendations shows).
        aliases: The normalized name prefixes that identify the family. They are all the
            spellings that the config of a terminal can have, in lower case with
            collapsed spaces. Nerd Fonts have several spellings on purpose: ``hack nerd
            font mono`` and ``hack nfm`` are the same file.
        coverage: :data:`FULL` when the family has the whole powerline block (Nerd Font
            patches). :data:`CORE` when it has only the four triangles.
    """

    name: str
    aliases: tuple[str, ...]
    coverage: str


#: The fonts that MeshTerm recommends, with the full-coverage Nerd Fonts first. The order
#: is important, because the first alias that matches wins. Thus ``JetBrainsMono Nerd
#: Font`` must match its ``full`` entry before the plain ``JetBrains Mono`` prefix can
#: claim it as ``core``.
RECOMMENDED_FONTS: tuple[RecommendedFont, ...] = (
    RecommendedFont(
        "MesloLGM Nerd Font",
        (
            "meslolgm nerd font",
            "meslolgs nerd font",
            "meslolgl nerd font",
            "meslolgm nf",
            "meslolgs nf",
            "meslolgl nf",
            "meslo lgm nerd font",
            "meslo lgs nerd font",
        ),
        FULL,
    ),
    RecommendedFont(
        "Hack Nerd Font",
        ("hack nerd font", "hack nf", "hack nfm", "hack nfp"),
        FULL,
    ),
    RecommendedFont(
        "CaskaydiaCove Nerd Font",
        (
            "caskaydiacove nerd font",
            "caskaydiacove nf",
            "caskaydiacove nfm",
            "caskaydiamono nerd font",
            "caskaydiamono nf",
            "caskaydia cove nerd font",
        ),
        FULL,
    ),
    RecommendedFont(
        "Cascadia Code NF",
        ("cascadia code nf", "cascadia mono nf"),
        FULL,
    ),
    RecommendedFont(
        "FiraCode Nerd Font",
        ("firacode nerd font", "firacode nf", "firacode nfm", "fira code nerd font"),
        FULL,
    ),
    RecommendedFont(
        "JetBrainsMono Nerd Font",
        (
            "jetbrainsmono nerd font",
            "jetbrainsmono nf",
            "jetbrainsmono nfm",
            "jetbrains mono nerd font",
        ),
        FULL,
    ),
    RecommendedFont("Cascadia Code PL", ("cascadia code pl", "cascadia mono pl"), CORE),
    RecommendedFont("Fira Code", ("fira code",), CORE),
    RecommendedFont("JetBrains Mono", ("jetbrains mono",), CORE),
    RecommendedFont("Source Code Pro", ("source code pro", "source code variable"), CORE),
    RecommendedFont("Iosevka", ("iosevka",), CORE),
)

#: The catch-all for a patched font that the explicit list does not name. Each family
#: whose name has the Nerd Font branding has the full powerline block. The branding is the
#: long ``… Nerd Font [Mono|Propo]`` or the short suffixes of version 3
#: (``NF``/``NFM``/``NFP``).
NERD_FONT_GENERIC = RecommendedFont("Nerd Font (patched)", (), FULL)

_NERD_RE = re.compile(r"\bnerd font\b|\bnf[mp]?\b")


@dataclass(frozen=True)
class FontDetection:
    """The configured terminal font, and the terminal that MeshTerm read it from.

    Attributes:
        face: The font family name as configured (not normalized).
        source: ``"windows-terminal"``, ``"vscode"``, or ``"conhost"``.
    """

    face: str
    source: str


@dataclass(frozen=True)
class PowerlineSupport:
    """The verdict: how certain MeshTerm is that this terminal can draw powerline separators.

    Attributes:
        level: :data:`FULL`, :data:`CORE`, :data:`NONE`, or :data:`UNKNOWN`.
        source: What decided the verdict. ``"env"`` is the override. ``"platform:<name>"``
            is the font inventory of a handheld. ``"font:<terminal>"`` is a configured
            face that matched, or that definitely did not match. ``"renderer:<terminal>"``
            is a terminal that draws the glyphs itself. ``"ssh"`` is a font on the far
            client, which MeshTerm cannot know. ``"unknown"`` is all other cases.
        face: The configured face, when MeshTerm found one. It is for information.
        matched: The entry of the recommended list that the face matched, if any.
    """

    level: str
    source: str
    face: str | None = None
    matched: RecommendedFont | None = None

    @property
    def capable(self) -> bool:
        """Check if the core triangle (the only glyph that the path widget needs) will draw."""
        return self.level in (FULL, CORE)


# --- name matching ---------------------------------------------------------------


def primary_family(value: str) -> str:
    """The first family of a font list in the CSS style, without quotes.

    VS Code stores ``"'Hack Nerd Font Mono', monospace"``. The renderer tries the first
    family first, so MeshTerm judges that family.

    Args:
        value: The value of a font-family setting (a single name or a comma list).

    Returns:
        The first family, with the quotes and the outer whitespace removed.
    """
    first = value.split(",")[0]
    return first.strip().strip("'\"").strip()


def normalize_face(face: str) -> str:
    """Fold a face name to its form for a match: lower case, single-spaced, without quotes.

    Args:
        face: A font family name as a config file spells it.

    Returns:
        The normalized name (it can be empty).
    """
    return " ".join(face.strip().strip("'\"").lower().split())


def match_recommended(face: str | None) -> RecommendedFont | None:
    """Match a configured face against the recommended-font list.

    A face matches an entry when it is one of the aliases of the entry, or when it extends
    one alias as a longer family name (``hack nerd font mono`` extends ``hack nerd
    font``). The generic Nerd Font rule (:data:`NERD_FONT_GENERIC`) matches the patched
    families that the explicit list does not name.

    Args:
        face: The configured font family (a single name or a comma list). ``None`` does
            nothing and is not an error.

    Returns:
        The matched :class:`RecommendedFont`, or ``None`` when the face is not known.
    """
    if not face:
        return None
    norm = normalize_face(primary_family(face))
    if not norm:
        return None
    for spec in RECOMMENDED_FONTS:
        if any(norm == alias or norm.startswith(alias + " ") for alias in spec.aliases):
            return spec
    if _NERD_RE.search(norm):
        return NERD_FONT_GENERIC
    return None


# --- config-file reading ---------------------------------------------------------


def _strip_jsonc(text: str) -> str:
    """Remove the ``//`` and ``/* */`` comments from JSON with comments. Strings are safe."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:  # keep the escaped character exactly
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _read_jsonc(path: Path) -> dict | None:
    """Parse a JSONC settings file (comments, trailing commas). Return ``None`` on any failure.

    Windows Terminal and VS Code both write JSON with comments. A strict parser fails at
    the first ``//``. After the removal of the comments, which does not change strings,
    a regex removes the trailing commas. The regex does its best. A string that contains
    ``", }"`` can in theory be cut, but no font config has such a string.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
        data = json.loads(re.sub(r",\s*([}\]])", r"\1", _strip_jsonc(text)))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _profile_face(profile: object) -> str | None:
    """The configured font face of a Windows Terminal profile.

    The function reads the new ``font.face`` or the legacy ``fontFace``. It returns
    ``None`` when the profile leaves the choice to the chain of defaults.
    """
    if not isinstance(profile, dict):
        return None
    font = profile.get("font")
    if isinstance(font, dict):
        face = font.get("face")
        if isinstance(face, list) and face:  # defensive: for an array face, use its first item
            face = face[0]
        if isinstance(face, str) and face.strip():
            return face
    legacy = profile.get("fontFace")
    if isinstance(legacy, str) and legacy.strip():
        return legacy
    return None


def _wt_settings_paths(environ: Mapping[str, str]) -> list[Path]:
    """The places where Windows Terminal can keep its ``settings.json``.

    These are the packaged, preview, and unpackaged locations. The list has only files
    that exist.
    """
    local = environ.get("LOCALAPPDATA")
    if not local:
        return []
    base = Path(local)
    candidates: list[Path] = []
    try:
        packages = base / "Packages"
        if packages.is_dir():
            candidates.extend(
                sorted(packages.glob("Microsoft.WindowsTerminal*/LocalState/settings.json"))
            )
    except OSError:
        pass
    candidates.append(base / "Microsoft" / "Windows Terminal" / "settings.json")
    return [p for p in candidates if p.is_file()]


def _windows_terminal_face(environ: Mapping[str, str]) -> str:
    """The face that Windows Terminal uses to render this session.

    The function finds the face in the same way as the terminal. First it uses the font
    of the ``WT_PROFILE_ID`` profile. If there is none, it uses ``profiles.defaults``.
    If there is none, it uses the built-in default ``Cascadia Mono``. An unreadable
    settings file also gives the built-in default, which is the same as a new
    installation.
    """
    guid = (environ.get("WT_PROFILE_ID") or "").strip().lower()
    for path in _wt_settings_paths(environ):
        data = _read_jsonc(path)
        if data is None:
            continue
        profiles = data.get("profiles")
        plist: list = []
        defaults: object = {}
        if isinstance(profiles, dict):
            plist = profiles.get("list") or []
            defaults = profiles.get("defaults") or {}
        elif isinstance(profiles, list):  # the old flat-list schema
            plist = profiles
        profile = (
            next(
                (
                    p
                    for p in plist
                    if isinstance(p, dict) and str(p.get("guid", "")).strip().lower() == guid
                ),
                None,
            )
            if guid
            else None
        )
        face = _profile_face(profile) or _profile_face(defaults)
        if face:
            return face
        break  # a settings file that parsed but has no face: use the built-in default
    return "Cascadia Mono"


def _vscode_settings_paths(environ: Mapping[str, str], start: Path | None) -> list[Path]:
    """The settings files of VS Code, the nearest first.

    First is the closest workspace. Then come the user files (stable and Insiders, in
    their locations for each platform). The list has only files that exist.
    """
    paths: list[Path] = []
    try:
        here = (start or Path.cwd()).resolve()
        for folder in (here, *here.parents):
            candidate = folder / ".vscode" / "settings.json"
            if candidate.is_file():
                paths.append(candidate)
                break
    except OSError:
        pass
    roots: list[Path] = []
    appdata = environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata))
    home = environ.get("HOME") or environ.get("USERPROFILE")
    if home:
        roots.append(Path(home) / ".config")
        roots.append(Path(home) / "Library" / "Application Support")
    for root in roots:
        for flavour in ("Code", "Code - Insiders"):
            candidate = root / flavour / "User" / "settings.json"
            if candidate.is_file():
                paths.append(candidate)
    return paths


def _vscode_default_face() -> str:
    """The first name of the default editor font family of VS Code on this platform."""
    if sys.platform == "win32":
        return "Consolas"
    if sys.platform == "darwin":
        return "Menlo"
    return "Droid Sans Mono"


def _vscode_face(environ: Mapping[str, str], start: Path | None = None) -> str:
    """The face that the integrated terminal of VS Code uses to render.

    ``terminal.integrated.fontFamily`` wins over ``editor.fontFamily`` (VS Code's own
    fallback). For each key, the workspace file wins over the user file. The stored
    value is a comma list in the CSS style, and MeshTerm judges the first family.
    """
    paths = _vscode_settings_paths(environ, start)
    settings = [data for data in (_read_jsonc(p) for p in paths) if data is not None]
    for key in ("terminal.integrated.fontFamily", "editor.fontFamily"):
        for data in settings:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return primary_family(value)
    return _vscode_default_face()


def _conhost_face() -> str | None:
    """The face of the classic console, from ``GetCurrentConsoleFontEx``, or ``None``.

    The result has a meaning only on a real conhost. The caller first makes sure that
    there are no ConPTY markers, because the hidden conhost under Windows Terminal or
    VS Code reports a stub face and not the glyphs on the screen.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wintypes

        kernel32 = win32dll.kernel32()
        if not kernel32.GetConsoleWindow():
            return None

        class _COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class _CONSOLE_FONT_INFOEX(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.ULONG),
                ("nFont", wintypes.DWORD),
                ("dwFontSize", _COORD),
                ("FontFamily", wintypes.UINT),
                ("FontWeight", wintypes.UINT),
                ("FaceName", ctypes.c_wchar * 32),
            ]

        info = _CONSOLE_FONT_INFOEX()
        info.cbSize = ctypes.sizeof(_CONSOLE_FONT_INFOEX)
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if not kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(info)):
            return None
        return info.FaceName or None
    except Exception:
        return None


# --- the ladder -------------------------------------------------------------------


def detect_terminal_font(
    environ: Mapping[str, str] | None = None,
    *,
    cwd: Path | None = None,
    conhost_probe: Callable[[], str | None] = _conhost_face,
) -> FontDetection | None:
    """Identify the terminal and read the font that it is configured to render with.

    The ladder goes from the most certain to the least certain source:

    * Windows Terminal. ``WT_SESSION`` names the app and ``WT_PROFILE_ID`` names the
      exact profile.
    * VS Code (``TERM_PROGRAM=vscode``).
    * A real classic conhost, which the function asks directly through Win32. It has no
      ConPTY markers and no ``TERM``. A ``TERM`` that is set means that another emulator
      hosts the console.

    For all other cases the result is ``None``, for example an unrecognized terminal or an
    ssh session. MeshTerm cannot know the face from here.

    Args:
        environ: The environment to examine (the default is ``os.environ``).
        cwd: Where the search for the VS Code workspace starts (the default is the cwd of
            the process).
        conhost_probe: The function that reads the face of the classic console (a
            parameter, so that tests can replace it).

    Returns:
        The configured face and its source, or ``None`` when no source applies.
    """
    env = os.environ if environ is None else environ
    if env.get("WT_SESSION"):
        return FontDetection(_windows_terminal_face(env), "windows-terminal")
    if (env.get("TERM_PROGRAM") or "").lower() == "vscode":
        return FontDetection(_vscode_face(env, cwd), "vscode")
    if not env.get("TERM") and not env.get("TERM_PROGRAM"):
        face = conhost_probe()
        if face:
            return FontDetection(face, "conhost")
    return None


def _renderer_backed(environ: Mapping[str, str]) -> str | None:
    """The terminal id, when the terminal draws the core triangles itself.

    Windows Terminal falls back to its bundled Cascadia Code NF for the powerline symbol
    ranges (version 1.22 and later). The terminal of VS Code draws them as custom glyphs
    (``terminal.integrated.customGlyphs``, on by default). kitty, WezTerm, and Alacritty
    have built-in powerline glyphs. In these terminals, the separators render in any
    configured font.
    """
    if environ.get("WT_SESSION"):
        return "windows-terminal"
    term_program = (environ.get("TERM_PROGRAM") or "").lower()
    if term_program == "vscode":
        return "vscode"
    if term_program == "wezterm" or environ.get("WEZTERM_EXECUTABLE"):
        return "wezterm"
    if environ.get("KITTY_WINDOW_ID") or environ.get("TERM") == "xterm-kitty":
        return "kitty"
    if (
        environ.get("ALACRITTY_WINDOW_ID")
        or environ.get("ALACRITTY_SOCKET")
        or environ.get("TERM") == "alacritty"
    ):
        return "alacritty"
    return None


#: The glyphs that the path widget draws at ``core``: the solid triangle that each seam
#: is, and the thin triangle that joins two fills that the eye cannot tell apart
#: (:mod:`~meshterm.ui.pathline`).
_CORE_GLYPHS = (0xE0B0, 0xE0B1)

#: What ``full`` adds that the widget uses: the rounded caps that replace the square ends
#: of a path.
_FULL_GLYPHS = (0xE0B4, 0xE0B6)


def _inventory_coverage(inventory: frozenset[int]) -> str:
    """The level of coverage that a known glyph inventory has.

    The result is not a guess, because the inventory lists all the glyphs.
    """
    if not all(code in inventory for code in _CORE_GLYPHS):
        return NONE
    return FULL if all(code in inventory for code in _FULL_GLYPHS) else CORE


def _powerline_support(
    environ: Mapping[str, str] | None = None,
    *,
    cwd: Path | None = None,
    conhost_probe: Callable[[], str | None] = _conhost_face,
    handheld: tuple[str, frozenset[int]] | None = None,
) -> PowerlineSupport:
    """The verdict without the cache (refer to :func:`powerline_support` for the ladder).

    Args:
        environ: The environment to examine (the default is ``os.environ``).
        cwd: Where the search for the VS Code workspace starts (the default is the cwd of
            the process).
        conhost_probe: The function that reads the face of the classic console (a
            parameter, so that tests can replace it).
        handheld: The name and the glyph inventory of the platform, where the font of
            a handheld itself draws the screen. ``None`` on a terminal.
    """
    env = os.environ if environ is None else environ
    override = (env.get("MESHTERM_POWERLINE") or "").strip().lower()
    if override in {"0", "off", "no", "none", "false"}:
        return PowerlineSupport(NONE, "env")
    if override in {"1", "on", "yes", "true", "full"}:
        return PowerlineSupport(FULL, "env")
    if override == "core":
        return PowerlineSupport(CORE, "env")
    if handheld is not None:
        name, inventory = handheld
        return PowerlineSupport(_inventory_coverage(inventory), f"platform:{name}")

    detected = detect_terminal_font(env, cwd=cwd, conhost_probe=conhost_probe)
    matched = match_recommended(detected.face) if detected else None
    if detected and matched:
        return PowerlineSupport(matched.coverage, f"font:{detected.source}", detected.face, matched)
    backed = _renderer_backed(env)
    if backed:
        return PowerlineSupport(CORE, f"renderer:{backed}", detected.face if detected else None)
    if detected:  # MeshTerm read a face, it matches nothing, and the terminal has no fallback
        return PowerlineSupport(NONE, f"font:{detected.source}", detected.face)
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY") or env.get("SSH_CLIENT"):
        return PowerlineSupport(UNKNOWN, "ssh")
    return PowerlineSupport(UNKNOWN, "unknown")


@lru_cache(maxsize=1)
def powerline_support() -> PowerlineSupport:
    """The powerline verdict of the session. MeshTerm decides it one time and caches it.

    The ladder has these steps, in this order:

    * The ``MESHTERM_POWERLINE`` override.
    * The own font inventory of a handheld platform.
    * The configured font, matched against :data:`RECOMMENDED_FONTS`.
    * A renderer that draws the glyphs itself.
    * ``none`` (MeshTerm read the face, but there is no match and no fallback) or
      ``unknown`` (an ssh session, or a terminal that MeshTerm cannot identify).

    The result is cached, because the environment and the settings files do not change
    during a session. A switch of platform clears the cache (:func:`_bind`). Tests call
    :func:`_powerline_support` directly.

    Returns:
        The :class:`PowerlineSupport` verdict.
    """
    return _powerline_support(os.environ, handheld=_HANDHELD_FONT)


def powerline_enabled() -> bool:
    """Check if path lines must use powerline separators by default (refer to ``.capable``)."""
    return powerline_support().capable


def powerline_full() -> bool:
    """Check if the extended block is also present: the rounded caps at U+E0B4 and later.

    Only a Nerd Font patch has these glyphs. Thus this check is narrower than
    :func:`powerline_enabled`. A stock coder font (Fira Code, JetBrains Mono) draws the
    core triangles natively and no other glyph. A terminal that supplies the separators
    from its own renderer promises only those four glyphs. Callers use this check for
    chrome that must degrade. When it is true, :mod:`~meshterm.ui.pathline` rounds the
    two outer ends of a path. When it is false, the module squares them off.
    """
    return powerline_support().level == FULL


#: The name and the glyph inventory of the active handheld, or ``None`` where a terminal
#: draws all that it receives. MeshTerm binds it when the platform switches (:func:`_bind`).
_HANDHELD_FONT: tuple[str, frozenset[int]] | None = None


@on_platform
def _bind(platform: Platform) -> None:
    """Bind the handheld font that the verdict reads, and clear the verdict of the last font."""
    global _HANDHELD_FONT
    inventory = FONTS.get(platform.font)
    _HANDHELD_FONT = (platform.name, inventory) if inventory is not None else None
    powerline_support.cache_clear()


# --- installed-font scan ----------------------------------------------------------


def _installed_families() -> Iterable[str]:
    """All the installed font family names that this platform reports. It does its best."""
    if sys.platform == "win32":
        try:
            import winreg

            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    key = winreg.OpenKey(
                        root, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
                    )
                except OSError:
                    continue
                with key:
                    for i in range(winreg.QueryInfoKey(key)[1]):
                        try:
                            name = winreg.EnumValue(key, i)[0]
                        except OSError:
                            break
                        # "Hack Nerd Font Mono (TrueType)" → "Hack Nerd Font Mono"
                        yield name.split(" (")[0]
        except Exception:
            return
        return
    try:
        import subprocess

        result = subprocess.run(
            ["fc-list", ":", "family"], capture_output=True, text=True, timeout=2
        )
        for line in result.stdout.splitlines():
            for family in line.split(","):
                if family.strip():
                    yield family.strip()
    except Exception:
        return


def installed_recommended() -> RecommendedFont | None:
    """The best recommended font that is installed on this machine, selected or not.

    This check is weaker, but it is always available. A future screen of recommendations
    can use it to choose between three messages: "not installed — here's the list",
    "installed but not selected in your terminal's profile", and "active". The function
    prefers :data:`FULL` coverage to :data:`CORE`. It returns ``None`` when no
    recommended font is installed, or when the platform has no way to ask.
    """
    best: RecommendedFont | None = None
    for family in _installed_families():
        matched = match_recommended(family)
        if matched is None:
            continue
        if matched.coverage == FULL:
            return matched
        best = best or matched
    return best


# --- emoji support ----------------------------------------------------------------


@dataclass(frozen=True)
class EmojiSupport:
    """The verdict if this terminal can show an emoji at all, and what decided it.

    Attributes:
        supported: ``True`` when an emoji icon reaches the screen as the emoji.
        source: ``"env"`` (the ``MESHTERM_EMOJI`` override), ``"platform"`` (not Windows,
            so the problem of the classic console does not apply), ``"console"`` (the
            host was identified, refer to :func:`_is_classic_console`), or
            ``"no-console"`` (there is nothing to identify: the output is redirected, or
            the call failed).
    """

    supported: bool
    source: str


def _is_classic_console() -> bool | None:
    """Check if this process draws into a real ``conhost`` window.

    This is the whole question. That host renders through GDI with the console font and
    has no emoji font behind it. All the hosts that replaced it draw emoji correctly.
    These are Windows Terminal, the terminal of VS Code, and anything over ssh.

    MeshTerm cannot ask in band. A missing glyph still occupies its cell, so the screen
    reads back the same as if the character had drawn. We measured this on Windows 10
    22H2. A classic console stores ``📡`` correctly and advances two cells, but it shows
    a single replacement box. A ConPTY (which does draw the emoji) reads back as U+FFFD,
    because the buffer behind it is updated asynchronously. A probe that writes and reads
    back gives the wrong answer for both hosts. This docstring records this trap.

    MeshTerm also cannot trust the environment alone. Child processes inherit
    ``WT_SESSION``. A program that starts from Windows Terminal into a console of its own
    still has it, and the answer is for the wrong host.

    So MeshTerm identifies the host from its structure, with two independent facts. Both
    facts must agree before MeshTerm dims anything. The costly mistake is to remove the
    icons from a terminal that can draw them.

    * **The console font has a real pixel width.** The hidden conhost of a ConPTY reports
      a stub (face ``Consolas``, cell width ``0``), because it rasterizes nothing.
    * **The buffer is taller than its window.** A classic console owns its scrollback
      (9001 rows by default). Under a ConPTY the terminal owns the scrollback, so the
      buffer is the same as the window.

    Returns:
        ``True`` for a real classic console. ``False`` for all other hosts that draw to a
        console. ``None`` when there is no console on stdout (the output is redirected,
        the platform is not Windows, or the call failed). An unknown result is never a
        reason to degrade.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class _SMALL_RECT(ctypes.Structure):
            _fields_ = [
                ("Left", wintypes.SHORT),
                ("Top", wintypes.SHORT),
                ("Right", wintypes.SHORT),
                ("Bottom", wintypes.SHORT),
            ]

        class _CSBI(ctypes.Structure):
            _fields_ = [
                ("dwSize", _COORD),
                ("dwCursorPosition", _COORD),
                ("wAttributes", wintypes.WORD),
                ("srWindow", _SMALL_RECT),
                ("dwMaximumWindowSize", _COORD),
            ]

        class _CONSOLE_FONT_INFOEX(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.ULONG),
                ("nFont", wintypes.DWORD),
                ("dwFontSize", _COORD),
                ("FontFamily", wintypes.UINT),
                ("FontWeight", wintypes.UINT),
                ("FaceName", ctypes.c_wchar * 32),
            ]

        # It is safe to declare these types, because this handle is only ours (refer to
        # meshterm.core.win32dll). The explicit restype is important. Without it, ctypes
        # assumes a 32-bit int and cuts the 64-bit HANDLE, and each later call fails
        # without a message.
        kernel32 = win32dll.kernel32()
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_CSBI)]
        kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
        kernel32.GetCurrentConsoleFontEx.argtypes = [
            wintypes.HANDLE,
            wintypes.BOOL,
            ctypes.POINTER(_CONSOLE_FONT_INFOEX),
        ]
        kernel32.GetCurrentConsoleFontEx.restype = wintypes.BOOL

        handle = kernel32.GetStdHandle(wintypes.DWORD(-11).value)  # STD_OUTPUT_HANDLE
        info = _CSBI()
        # This call fails when stdout is a pipe or a file. Then MeshTerm draws nothing and
        # there is no answer. This is the reason that redirected runs and CI runs do not
        # use the rest of this function.
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return None

        font = _CONSOLE_FONT_INFOEX()
        font.cbSize = ctypes.sizeof(_CONSOLE_FONT_INFOEX)
        if not kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(font)):
            return None

        rasterised = font.dwFontSize.X > 0
        owns_scrollback = info.dwSize.Y > (info.srWindow.Bottom - info.srWindow.Top + 1)
        return rasterised and owns_scrollback
    except Exception:  # pragma: no cover - a probe that fails gives an unknown result, not a crash
        return None


def _emoji_support(
    environ: Mapping[str, str] | None = None,
    *,
    console_probe: Callable[[], bool | None] = _is_classic_console,
    system: str | None = None,
) -> EmojiSupport:
    """The verdict without the cache (refer to :func:`emoji_support` for the ladder)."""
    env = os.environ if environ is None else environ
    override = (env.get("MESHTERM_EMOJI") or "").strip().lower()
    if override in {"0", "off", "no", "none", "false"}:
        return EmojiSupport(False, "env")
    if override in {"1", "on", "yes", "true"}:
        return EmojiSupport(True, "env")

    if (sys.platform if system is None else system) != "win32":
        return EmojiSupport(True, "platform")
    classic = console_probe()
    if classic is None:
        return EmojiSupport(True, "no-console")
    return EmojiSupport(not classic, "console")


@lru_cache(maxsize=1)
def classic_console() -> bool:
    """Check if this session draws into a real ``conhost`` window.

    This is the only host that has no font fallback of its own. Thus it is the only host
    where the glyphs of the configured font decide what the user sees. Refer to
    :func:`_is_classic_console` for how MeshTerm identifies it, and why it does not use
    the environment.
    """
    return _is_classic_console() is True


@lru_cache(maxsize=1)
def emoji_support() -> EmojiSupport:
    """Check if emoji icons can be drawn here. MeshTerm decides one time and caches it.

    The ladder has these steps, in this order: the ``MESHTERM_EMOJI`` override, then all
    platforms that are not Windows (where a UTF-8 terminal draws emoji correctly), then
    :func:`_is_classic_console`. The result is cached, because a session does not change
    its host during the session.

    Returns:
        The :class:`EmojiSupport` verdict.
    """
    return _emoji_support(os.environ)


def emoji_enabled() -> bool:
    """Check if icons must render as emoji instead of through the compact table."""
    return emoji_support().supported


# --- chart glyphs -----------------------------------------------------------------


#: The families that we measured to have the Braille Patterns block (U+2800–U+28FF).
#: :mod:`meshterm.ui.braillechart` draws each timeline from this block.
#:
#: The list is short on purpose. Each entry is a measurement, not a reputation. On
#: 2026-09-07 we read the ``cmap`` of each font that is installed on a development
#: machine. Almost no monospace font has the block. Hack Nerd Font, JetBrains Mono, Fira
#: Code, Source Code Pro, Consolas, Lucida Console, and DejaVu Sans *Mono* (a surprise)
#: all measure zero of 256. DejaVu *Sans* is proportional, and it has all 256. This is
#: the source of the common advice "install DejaVu for braille". The advice works only
#: because terminals fall back from the mono face to the proportional face for that
#: block.
#:
#: This is the important point. On each host that falls back (Windows Terminal, the
#: terminal of VS Code, macOS, Linux), the charts draw in any chosen font. Thus this list
#: does not have to be correct there. It matters on the one host that has no fallback, the
#: classic Windows console. There, a font that is not in this list gives boxes where the
#: charts must be.
CHART_FONTS: tuple[str, ...] = (
    # The console font of Microsoft: 44/44, and the family that MeshTerm bundles (refer to
    # meshterm.core.consolefont). The prefix also matches the PL and NF builds.
    "cascadia mono",
    "cascadia code",
    # The Nerd Font patches of the same fonts. They keep what they patch.
    "caskaydiacove",
    "caskaydiamono",
    # Many reports say that it has the block. We did not measure it, because we have no
    # copy.
    "iosevka",
)


def face_draws_charts(face: str | None) -> bool:
    """Check if ``face`` can draw the braille that the timelines use.

    Args:
        face: A configured font family, in any spelling, or ``None``.

    Returns:
        ``True`` if it is one of :data:`CHART_FONTS`.
    """
    if not face:
        return False
    normal = normalize_face(face)
    return any(normal.startswith(known) for known in CHART_FONTS)


def installed_chart_font() -> str | None:
    """The first installed family that can draw the charts, or ``None``.

    The result decides between two actions: "select the font that the user already has"
    (each Windows 11 machine, and each user who installed Windows Terminal) and "offer to
    install our font".

    Returns:
        The family name as installed, or ``None`` when no installed family can do it.
    """
    for family in _installed_families():
        if face_draws_charts(family):
            return family
    return None
