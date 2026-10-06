# SPDX-License-Identifier: Apache-2.0
"""Terminal capability detection — what can this terminal actually draw?

Two questions, both answered out-of-band because a terminal cannot be asked in-band: a
glyph it does not have still occupies its cell, so the screen looks the same whether the
character arrived or not.

**Powerline separators**, below, are a *font* question — read the terminal's configured
face and match it against fonts known to carry the block.

**Emoji** (:func:`emoji_support`) are a *host* question, and only on Windows. The classic
console — ``conhost``, which is what a double-clicked program still gets — rasterises
through GDI with the console font and nothing behind it, so an emoji lands as a
replacement box however the font is configured. Everything that replaced it draws them
fine. So the verdict identifies the host, structurally rather than from the environment
(``WT_SESSION`` is inherited across process launches, so a program started from Windows
Terminal into a console of its own still claims to be in one), and a ``False`` routes
every icon through the compact single-glyph table :func:`meshterm.ui.theme.glyph` already
keeps for the PicoCalc — that whole vocabulary is BMP, so it draws wherever the plain
status marks already do.

Both verdicts are hints that pick a default, and both have an override for the reader who
knows better than the probe: ``MESHTERM_POWERLINE`` and ``MESHTERM_EMOJI``.

The path-line widget (:mod:`~meshterm.ui.pathline`) can render a hop sequence as
interlocking powerline segments — each hop a colour-filled chip, joined by the solid
triangle U+E0B0 whose foreground is the previous chip's fill and whose background is
the next's, the oh-my-posh look. That triangle lives in the Private Use Area, so it
only draws when the *terminal's configured font* (or the terminal itself) supplies the
glyph; a font without it shows tofu boxes. Nothing in-band can ask "do you have this
glyph?" — a missing glyph still occupies its cell, so even cursor-position probes see
nothing — which makes this an *out-of-band* detection problem: identify the terminal,
read its configuration, and match the configured face against fonts known to carry the
powerline block.

Four sources of truth, in confidence order:

* **An explicit override** — ``MESHTERM_POWERLINE`` (``1``/``full``, ``core``,
  ``0``/``off``) always wins, the same gate pattern as ``MESHTERM_FULL_WIDTH``. The
  user knows their glass better than any probe.
* **A handheld's own font.** Where the platform names a glyph contract
  (:attr:`~meshterm.platforms.Platform.font`), every frame is folded down to that font
  at the render boundary, whatever terminal started the app — so the inventory is the
  answer, and asking the terminal would be asking the wrong glass. The Cardputer's panel
  is Terminus, which carries the core chevrons. The PicoCalc's 512-glyph console font
  draws the two chevrons in two donor slots. Thus the paths are chips on both handhelds.
  Nothing is probed: :data:`~meshterm.ui.fontset.FONTS` already says what each font can
  draw.
* **The configured font**, resolved per terminal: Windows Terminal (``WT_SESSION`` +
  ``WT_PROFILE_ID`` → the profile's ``font.face`` in ``settings.json``), VS Code's
  integrated terminal (``TERM_PROGRAM=vscode`` → ``terminal.integrated.fontFamily``,
  workspace over user, falling back to ``editor.fontFamily``), and classic conhost
  (``GetCurrentConsoleFontEx`` — trusted only when no ConPTY host is detected, because
  the hidden conhost under one reports a stub face, not what's on screen). The face is
  matched against :data:`RECOMMENDED_FONTS` — the list MeshTerm recommends to users —
  with the alias spellings Nerd Fonts ship under (``Hack Nerd Font Mono`` / ``Hack
  NFM``), plus a generic "any Nerd Font" rule, since every Nerd Font patch carries the
  full powerline block.
* **The terminal's own renderer**: several terminals draw the core triangles
  regardless of font — Windows Terminal falls back to its bundled Cascadia Code NF for
  the symbol ranges (≥ 1.22), VS Code's terminal draws them as custom glyphs
  (``terminal.integrated.customGlyphs``, default on), and kitty / WezTerm / Alacritty
  ship built-in powerline glyphs. Those count as ``core`` support even when the
  configured font matches nothing.

Coverage comes in two levels: ``full`` (a Nerd Font patch — triangles *and* the
extended block: rounded caps, slants) and ``core`` (just U+E0B0–U+E0B3, which many
stock coder fonts — Fira Code, JetBrains Mono, Source Code Pro — include natively).
The path widget only uses the core triangle, so ``core`` is enough to switch it on;
``full`` is headroom for fancier chrome. When nothing can be known — an ssh session
(the font lives on the far client's machine), an unrecognised terminal — the verdict
is honestly ``unknown`` and callers should keep the plain-arrow rendering.

Every probe is best-effort: unreadable settings, missing registry keys, or a failed
Win32 call degrade the verdict, never raise. The result is a *hint that picks a
default*, not a gate the user has to fight.
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

#: Coverage levels a verdict (or a recommended font) can carry, strongest first.
FULL = "full"
CORE = "core"
NONE = "none"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class RecommendedFont:
    """One font MeshTerm recommends for powerline path rendering.

    Attributes:
        name: The canonical display name (what a recommendation screen shows).
        aliases: Normalised name prefixes that identify the family — every spelling a
            terminal config might hold, lower-case with collapsed spaces (Nerd Fonts
            deliberately ship several: ``hack nerd font mono`` and ``hack nfm`` are the
            same file).
        coverage: :data:`FULL` when the family carries the whole powerline block
            (Nerd Font patches), :data:`CORE` when it ships just the four triangles.
    """

    name: str
    aliases: tuple[str, ...]
    coverage: str


#: The fonts MeshTerm recommends, full-coverage Nerd Fonts first — order matters, the
#: first alias hit wins, so ``JetBrainsMono Nerd Font`` must match its ``full`` entry
#: before the plain ``JetBrains Mono`` prefix could claim it as ``core``.
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

#: The catch-all for a patched font the explicit list doesn't spell out: any family
#: whose name carries the Nerd Font branding — the long ``… Nerd Font [Mono|Propo]``
#: or the v3 short suffixes ``NF``/``NFM``/``NFP`` — has the full powerline block.
NERD_FONT_GENERIC = RecommendedFont("Nerd Font (patched)", (), FULL)

_NERD_RE = re.compile(r"\bnerd font\b|\bnf[mp]?\b")


@dataclass(frozen=True)
class FontDetection:
    """The configured terminal font, and which terminal it was read from.

    Attributes:
        face: The font family name as configured (unnormalised).
        source: ``"windows-terminal"``, ``"vscode"``, or ``"conhost"``.
    """

    face: str
    source: str


@dataclass(frozen=True)
class PowerlineSupport:
    """The verdict: how confidently this terminal can draw powerline separators.

    Attributes:
        level: :data:`FULL`, :data:`CORE`, :data:`NONE`, or :data:`UNKNOWN`.
        source: What decided it — ``"env"`` (the override), ``"platform:<name>"`` (a
            handheld's own font inventory), ``"font:<terminal>"``
            (a configured face matched, or definitively didn't), ``"renderer:<terminal>"``
            (the terminal draws the glyphs itself), ``"ssh"`` (the font lives on the far
            client, unknowable), or ``"unknown"``.
        face: The configured face when one was found, for the curious.
        matched: The recommended-list entry the face matched, if any.
    """

    level: str
    source: str
    face: str | None = None
    matched: RecommendedFont | None = None

    @property
    def capable(self) -> bool:
        """Whether the core triangle (all the path widget needs) will draw."""
        return self.level in (FULL, CORE)


# --- name matching ---------------------------------------------------------------


def primary_family(value: str) -> str:
    """The first family of a CSS-style font list, unquoted.

    VS Code stores ``"'Hack Nerd Font Mono', monospace"``; the first family is the
    one the renderer tries first, so it is the one we judge.

    Args:
        value: A font-family setting value (single name or comma list).

    Returns:
        The first family with quotes and outer whitespace stripped.
    """
    first = value.split(",")[0]
    return first.strip().strip("'\"").strip()


def normalize_face(face: str) -> str:
    """Fold a face name to its matchable form: lower-case, single-spaced, unquoted.

    Args:
        face: A font family name as a config file spells it.

    Returns:
        The normalised name (may be empty).
    """
    return " ".join(face.strip().strip("'\"").lower().split())


def match_recommended(face: str | None) -> RecommendedFont | None:
    """Match a configured face against the recommended-font list.

    A face matches an entry when it *is* one of the entry's aliases or extends one as
    a longer family name (``hack nerd font mono`` extends ``hack nerd font``); the
    generic Nerd Font rule (:data:`NERD_FONT_GENERIC`) catches patched families the
    explicit list doesn't spell out.

    Args:
        face: The configured font family (single name or comma list); ``None`` is a
            polite no-op.

    Returns:
        The matched :class:`RecommendedFont`, or ``None`` when the face is unknown.
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
    """Strip ``//`` and ``/* */`` comments from JSON-with-comments, string-safely."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:  # keep the escaped char verbatim
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
    """Parse a JSONC settings file (comments, trailing commas), ``None`` on any failure.

    Both Windows Terminal and VS Code write JSON-with-comments; a strict parser dies
    on the first ``//``. Trailing commas are swept with a best-effort regex after the
    string-aware comment strip (a *string* containing ``", }"`` could theoretically be
    clipped — no font config holds one).
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
        data = json.loads(re.sub(r",\s*([}\]])", r"\1", _strip_jsonc(text)))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _profile_face(profile: object) -> str | None:
    """A Windows Terminal profile's configured font face.

    Reads the new ``font.face`` or the legacy ``fontFace``, and answers ``None`` when
    the profile leaves the choice to the defaults chain.
    """
    if not isinstance(profile, dict):
        return None
    font = profile.get("font")
    if isinstance(font, dict):
        face = font.get("face")
        if isinstance(face, list) and face:  # defensive: an array face takes its head
            face = face[0]
        if isinstance(face, str) and face.strip():
            return face
    legacy = profile.get("fontFace")
    if isinstance(legacy, str) and legacy.strip():
        return legacy
    return None


def _wt_settings_paths(environ: Mapping[str, str]) -> list[Path]:
    """Where Windows Terminal might keep its ``settings.json``.

    The packaged, preview, and unpackaged locations — existing files only.
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
    """The face Windows Terminal is rendering this session with.

    Resolved the way the terminal itself does: the ``WT_PROFILE_ID`` profile's font,
    else ``profiles.defaults``, else the built-in default ``Cascadia Mono``. An
    unreadable settings file lands on the built-in default too — a pristine install
    is exactly that.
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
        elif isinstance(profiles, list):  # the ancient flat-list schema
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
        break  # a parsed settings file with no face set → the built-in default
    return "Cascadia Mono"


def _vscode_settings_paths(environ: Mapping[str, str], start: Path | None) -> list[Path]:
    """VS Code's settings files, nearest first.

    The closest workspace, then the user's own (stable and Insiders, in their
    per-platform locations) — existing files only.
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
    """VS Code's per-platform default editor font family's first name."""
    if sys.platform == "win32":
        return "Consolas"
    if sys.platform == "darwin":
        return "Menlo"
    return "Droid Sans Mono"


def _vscode_face(environ: Mapping[str, str], start: Path | None = None) -> str:
    """The face VS Code's integrated terminal is rendering with.

    ``terminal.integrated.fontFamily`` wins over ``editor.fontFamily`` (VS Code's own
    fallback), and within each key the workspace file wins over the user file. The
    stored value is a CSS-style comma list; the first family is judged.
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
    """The classic-console face via ``GetCurrentConsoleFontEx``, or ``None``.

    Only meaningful on a *genuine* conhost — the caller gates on the absence of ConPTY
    markers first, because the hidden conhost under Windows Terminal / VS Code reports
    a stub face, not the glyphs on screen.
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
    """Identify the terminal and read the font it is configured to render with.

    The ladder, most to least certain: Windows Terminal (``WT_SESSION`` names the app,
    ``WT_PROFILE_ID`` the exact profile), VS Code (``TERM_PROGRAM=vscode``), then a
    genuine classic conhost (no ConPTY markers, no ``TERM`` — a set ``TERM`` means some
    other emulator is hosting the console) asked directly via Win32. Anything else —
    an unrecognised terminal, an ssh session — is ``None``: the face is unknowable
    from here.

    Args:
        environ: The environment to inspect (defaults to ``os.environ``).
        cwd: Where the VS Code workspace walk starts (defaults to the process cwd).
        conhost_probe: The classic-console face reader (injectable for tests).

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
    """The terminal id when the terminal draws the core triangles itself.

    Windows Terminal falls back to its bundled Cascadia Code NF for the powerline
    symbol ranges (≥ 1.22), VS Code's terminal draws them as custom glyphs
    (``terminal.integrated.customGlyphs``, default on), and kitty / WezTerm /
    Alacritty ship built-in powerline glyphs — on those, the separators render
    whatever the configured font holds.
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


#: The glyphs the path widget draws at ``core``: the solid triangle every seam is, and the
#: thin one that joins two fills the eye can't tell apart (:mod:`~meshterm.ui.pathline`).
_CORE_GLYPHS = (0xE0B0, 0xE0B1)

#: What ``full`` adds that the widget uses: the rounded caps a path's square ends become.
_FULL_GLYPHS = (0xE0B4, 0xE0B6)


def _inventory_coverage(inventory: frozenset[int]) -> str:
    """The coverage level a known glyph inventory earns — no guess, it is all listed."""
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
    """The uncached verdict (see :func:`powerline_support` for the ladder).

    Args:
        environ: The environment to inspect (defaults to ``os.environ``).
        cwd: Where the VS Code workspace walk starts (defaults to the process cwd).
        conhost_probe: The classic-console face reader (injectable for tests).
        handheld: The platform's name and glyph inventory where a handheld's own font
            draws the screen, or ``None`` on a terminal.
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
    if detected:  # a face we could read, matching nothing, on a terminal with no fallback
        return PowerlineSupport(NONE, f"font:{detected.source}", detected.face)
    if env.get("SSH_CONNECTION") or env.get("SSH_TTY") or env.get("SSH_CLIENT"):
        return PowerlineSupport(UNKNOWN, "ssh")
    return PowerlineSupport(UNKNOWN, "unknown")


@lru_cache(maxsize=1)
def powerline_support() -> PowerlineSupport:
    """The session's powerline verdict, decided once and cached.

    The ladder: the ``MESHTERM_POWERLINE`` override, then a handheld platform's own font
    inventory, then the configured font matched against :data:`RECOMMENDED_FONTS`, then
    a renderer that draws the glyphs itself, then an honest ``none`` (face read, no
    match, no fallback) or ``unknown`` (ssh, or a terminal we can't identify). Cached
    because the environment and settings files don't change mid-session, and cleared on
    a platform switch (:func:`_bind`); tests exercise :func:`_powerline_support` directly.

    Returns:
        The :class:`PowerlineSupport` verdict.
    """
    return _powerline_support(os.environ, handheld=_HANDHELD_FONT)


def powerline_enabled() -> bool:
    """Whether path lines should default to powerline separators (see ``.capable``)."""
    return powerline_support().capable


def powerline_full() -> bool:
    """Whether the *extended* block is there too — the rounded caps at U+E0B4+.

    Only a Nerd Font patch carries them, so this is strictly narrower than
    :func:`powerline_enabled`: a stock coder font (Fira Code, JetBrains Mono) draws
    the core triangles natively and nothing beyond, and a terminal that supplies the
    separators from its own renderer is only promising those four. Callers use it for
    chrome that must degrade — :mod:`~meshterm.ui.pathline` rounds a path's two outer
    ends when it is true and squares them off when it isn't.
    """
    return powerline_support().level == FULL


#: The active handheld's name and glyph inventory, or ``None`` where a terminal draws
#: whatever it is sent. Bound at platform-switch time (:func:`_bind`).
_HANDHELD_FONT: tuple[str, frozenset[int]] | None = None


@on_platform
def _bind(platform: Platform) -> None:
    """Bind the handheld font the verdict reads, and drop the verdict the last one made."""
    global _HANDHELD_FONT
    inventory = FONTS.get(platform.font)
    _HANDHELD_FONT = (platform.name, inventory) if inventory is not None else None
    powerline_support.cache_clear()


# --- installed-font scan ----------------------------------------------------------


def _installed_families() -> Iterable[str]:
    """Every installed font family name this platform will admit to, best-effort."""
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
    """The best recommended font *installed* on this machine, selected or not.

    The weaker, always-available check that a future recommendation screen splits its
    message on: "not installed — here's the list" versus "installed but not selected
    in your terminal's profile" versus "active". Prefers :data:`FULL` coverage over
    :data:`CORE`; returns ``None`` when nothing recommended is installed (or the
    platform offers no way to ask).
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
    """Whether this terminal can show an emoji at all, and what decided that.

    Attributes:
        supported: ``True`` when an emoji icon reaches the screen as itself.
        source: ``"env"`` (the ``MESHTERM_EMOJI`` override), ``"platform"`` (not Windows,
            so not the classic console's problem), ``"console"`` (the host was identified
            — see :func:`_is_classic_console`), or ``"no-console"`` (nothing to identify:
            output is redirected, or the call failed).
    """

    supported: bool
    source: str


def _is_classic_console() -> bool | None:
    """Whether this process is drawing into a genuine ``conhost`` window.

    Which is the whole question, because that host renders through GDI with the console
    font and no emoji font behind it, while everything that replaced it — Windows
    Terminal, VS Code's terminal, anything over ssh — draws emoji as a matter of course.

    It cannot be asked in band. A missing glyph still occupies its cell, so the screen
    reads back exactly as it would have if the character had drawn: measured on Windows 10
    22H2, a classic console stores ``📡`` intact and advances two cells while showing a
    single replacement box, and a ConPTY (which does draw it) reads back as U+FFFD because
    the buffer behind it is updated asynchronously. A write-and-read-back probe answers
    both hosts *backwards*, which is the trap this docstring exists to record.

    Nor can the environment be trusted alone: ``WT_SESSION`` is inherited by child
    processes, so a program launched from Windows Terminal into a console of its own still
    carries it and would answer for the wrong host.

    So the host is identified structurally, from two independent facts, and both must
    agree before anything is dimmed (the costly mistake is stripping icons from a terminal
    that could draw them):

    * **The console font has a real pixel width.** A ConPTY's hidden conhost reports a
      stub — face ``Consolas``, cell width ``0`` — because nothing is rasterised there.
    * **The buffer is taller than its window.** A classic console owns its scrollback
      (9001 rows by default); under a ConPTY the terminal owns it, so the buffer is
      exactly the window.

    Returns:
        ``True`` for a genuine classic console, ``False`` for anything else drawing to a
        console, and ``None`` when there is no console on stdout (redirected output, not
        Windows, or the call failed) — an unknown is never a reason to degrade.
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

        # Safe to declare: this handle is ours alone (see meshterm.core.win32dll). The
        # explicit restype matters — ctypes would assume a 32-bit int and truncate the
        # 64-bit HANDLE, turning every later call into a silent failure.
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
        # Fails when stdout is a pipe or a file — nothing is being drawn, so there is
        # nothing to answer, and this is what keeps redirected and CI runs out of it.
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return None

        font = _CONSOLE_FONT_INFOEX()
        font.cbSize = ctypes.sizeof(_CONSOLE_FONT_INFOEX)
        if not kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(font)):
            return None

        rasterised = font.dwFontSize.X > 0
        owns_scrollback = info.dwSize.Y > (info.srWindow.Bottom - info.srWindow.Top + 1)
        return rasterised and owns_scrollback
    except Exception:  # pragma: no cover - a probe that fails is an unknown, not a crash
        return None


def _emoji_support(
    environ: Mapping[str, str] | None = None,
    *,
    console_probe: Callable[[], bool | None] = _is_classic_console,
    system: str | None = None,
) -> EmojiSupport:
    """The uncached verdict (see :func:`emoji_support` for the ladder)."""
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
    """Whether this session is drawing into a genuine ``conhost`` window.

    The one host with no font fallback of its own, so the only one where the configured
    font's own repertoire decides what the reader sees. See :func:`_is_classic_console`
    for how it is identified, and why not from the environment.
    """
    return _is_classic_console() is True


@lru_cache(maxsize=1)
def emoji_support() -> EmojiSupport:
    """Whether emoji icons can be drawn here, decided once and cached.

    The ladder: the ``MESHTERM_EMOJI`` override, then everything that isn't Windows (where
    a UTF-8 terminal draws them as a matter of course), then :func:`_is_classic_console`.
    Cached because a session does not change host halfway through.

    Returns:
        The :class:`EmojiSupport` verdict.
    """
    return _emoji_support(os.environ)


def emoji_enabled() -> bool:
    """Whether icons should render as emoji rather than through the compact table."""
    return emoji_support().supported


# --- chart glyphs -----------------------------------------------------------------


#: Families measured to carry the Braille Patterns block (U+2800–U+28FF) that
#: :mod:`meshterm.ui.braillechart` draws every timeline from.
#:
#: The list is short on purpose, and each entry is a measurement rather than a
#: reputation: reading the ``cmap`` of every font installed on a development machine
#: (2026-09-07) found that nearly no monospace font carries the block at all. Hack Nerd
#: Font, JetBrains Mono, Fira Code, Source Code Pro, Consolas, Lucida Console and — the
#: surprise — DejaVu Sans *Mono* all measure zero of 256. DejaVu *Sans*, which is
#: proportional, carries all 256, which is where the widespread "install DejaVu for
#: braille" advice comes from: it works only because terminals fall back from the mono
#: face to the proportional one for that block.
#:
#: Which is the point. On every host that falls back — Windows Terminal, VS Code's
#: terminal, macOS, Linux — the charts draw whatever the chosen font holds, so this list
#: never has to be right. It matters on the one host with no fallback at all, the classic
#: Windows console, where a font outside this list means boxes where the charts should be.
CHART_FONTS: tuple[str, ...] = (
    # Microsoft's console font: 44/44, and the family MeshTerm bundles (see
    # meshterm.core.consolefont). Covers the PL and NF builds by prefix.
    "cascadia mono",
    "cascadia code",
    # The Nerd Font patches of the same, which keep what they patch.
    "caskaydiacove",
    "caskaydiamono",
    # Widely reported to carry the block; not measured here, no copy to hand.
    "iosevka",
)


def face_draws_charts(face: str | None) -> bool:
    """Whether ``face`` can draw the braille the timelines are made of.

    Args:
        face: A configured font family, in any spelling, or ``None``.

    Returns:
        Whether it is one of :data:`CHART_FONTS`.
    """
    if not face:
        return False
    normal = normalize_face(face)
    return any(normal.startswith(known) for known in CHART_FONTS)


def installed_chart_font() -> str | None:
    """The first installed family that can draw the charts, or ``None``.

    What separates "select the font they already have" — every Windows 11 machine, and
    anyone who has installed Windows Terminal — from "offer to install ours".

    Returns:
        The family name as installed, or ``None`` when nothing installed can do it.
    """
    for family in _installed_families():
        if face_draws_charts(family):
            return family
    return None
