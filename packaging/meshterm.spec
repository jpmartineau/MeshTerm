# SPDX-License-Identifier: Apache-2.0
# PyInstaller spec for the downloadable builds. Run from the repository root:
#
#     pyinstaller packaging/meshterm.spec
#
# One file, console mode. Console is not a detail: MeshTerm *is* a terminal program, and
# a windowed build would start with nowhere to draw and exit immediately.
#
# Built on the machine it targets. There is no cross-compiling here — the Windows build
# comes off a Windows runner, the macOS one off macOS. That is what the workflow is for.

import re
import sys

from PyInstaller.utils.hooks import collect_submodules

# `packaging/notices.py` builds THIRD-PARTY-NOTICES.txt at build time; it sits beside
# this spec rather than under `meshterm/` because — like `entry.py` above — it has no
# business being importable from the installed package, only from the freezer. `SPECPATH`
# is one of the names PyInstaller injects into a running .spec's namespace (the directory
# holding this file, resolved from the path given to `pyinstaller`, not the process's
# cwd), so this finds it however the spec was invoked.
sys.path.insert(0, SPECPATH)
import notices  # noqa: E402 (import must follow the sys.path edit above)

# Generated fresh on every build rather than checked into git: it is a function of
# whichever distributions are actually installed in *this* interpreter, and a stale copy
# would drift the moment a dependency's license text changed upstream without anyone
# noticing. `workpath` is PyInstaller's own scratch directory for exactly this kind of
# build-time artefact — cleaned by `--clean`, never mistaken for a source file. It lands
# at `<workpath>/<specname>/THIRD-PARTY-NOTICES.txt` (PyInstaller appends the spec's own
# name, minus its extension, to whatever workpath it was given — see `build()` in
# `PyInstaller/building/build_main.py`), which for this spec means `build/meshterm/`.
third_party_notices = notices.write_third_party_notices(
    os.path.join(workpath, "THIRD-PARTY-NOTICES.txt")
)

# The Windows build's version resource: the Details tab of the file's Properties, and the
# name and version Windows shows for it. Without one the .exe is just "meshterm.exe" with
# no product behind it, and SignPath refuses to sign it — their signing configuration
# checks the product name and version written here, so a file that doesn't carry them
# can't be passed off as a MeshTerm release. Built from the package's own `__version__`
# rather than kept as a second copy that would drift. The other platforms have no such
# resource, and PyInstaller warns if handed one there.
version_info = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    # The checkout being frozen, not whichever MeshTerm the interpreter happens to have
    # installed: `Analysis` below reads its sources from here too.
    sys.path.insert(0, os.path.dirname(SPECPATH))
    import meshterm  # noqa: E402 (import must follow the sys.path edit above)

    # The resource's fixed half holds exactly four numbers: 0.10.2 → (0, 10, 2, 0). A
    # suffix such as `rc1` has nowhere to go there and is dropped; the text fields below
    # keep the version exactly as written.
    numbers = [int(n) for n in re.match(r"\d+(?:\.\d+)*", meshterm.__version__)[0].split(".")]
    numbers = tuple((numbers + [0, 0, 0, 0])[:4])
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo(
                [
                    # 0409 is US English and 04B0 says the strings are Unicode — the
                    # pairing nearly every Windows program declares.
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", meshterm.__author__),
                            StringStruct("FileDescription", "MeshTerm"),
                            StringStruct("FileVersion", meshterm.__version__),
                            StringStruct("InternalName", "meshterm"),
                            StringStruct("LegalCopyright", meshterm.copyright_notice()),
                            StringStruct("OriginalFilename", "meshterm.exe"),
                            StringStruct("ProductName", "MeshTerm"),
                            StringStruct("ProductVersion", meshterm.__version__),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )

# The assets have to land at `meshterm/assets`, because `ui/about.py` finds them with
# `Path(__file__).parent.parent / "assets"` and that path has to keep resolving inside
# the bundle exactly as it does in a checkout.
datas = [
    ("../meshterm/assets", "meshterm/assets"),
    # The SPI radio's node is run by path under *another* Python — the one with the radio
    # library, which this build doesn't carry — so it has to exist as a source file, beside
    # where `spiradio.node_script()` looks for it, not only as bytecode in the archive.
    ("../meshterm/core/radionode.py", "meshterm/core"),
    # A one-file build is a *copy* of MeshTerm, and Apache-2.0 §4(a)/(d) want MeshTerm's
    # own LICENSE and NOTICE distributed with every copy — not folded into the app's own
    # assets, but sitting at the bundle root the way they sit at the repository root.
    ("../LICENSE", "."),
    ("../NOTICE", "."),
    # Every MIT/BSD/PSF dependency's own notice wants to travel with copies the same way;
    # see packaging/notices.py for how this is built.
    (str(third_party_notices), "."),
]

hiddenimports = [
    # Every tool module. `meshterm.tools` finds these with `pkgutil.iter_modules` at
    # import time, so nothing static ever names one, and a frozen build without this line
    # starts with an empty registry: no menu entries but Quit, and no CLI subcommands.
    # The app launches and looks like it works, which is the worst way for this to fail.
    *collect_submodules("meshterm.tools"),
    # bleak picks its backend at runtime by platform, so static analysis never sees the
    # one that actually gets used. Collect all of them and let the unused ones sit.
    *collect_submodules("bleak.backends"),
    # Same story for the companion library's transports.
    *collect_submodules("meshcore"),
    # Rich and prompt_toolkit both reach for things dynamically.
    "rich.console",
    "prompt_toolkit.output.win32",
    "prompt_toolkit.output.vt100",
]

a = Analysis(
    ["entry.py"],
    pathex=[".."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Nothing here is used, and each one drags in a GUI toolkit or a test framework.
    excludes=["tkinter", "pytest", "IPython", "matplotlib", "numpy", "PIL"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="meshterm",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is off deliberately: it saves a few megabytes and gets the result flagged by
    # antivirus often enough that it is not a trade worth making for a first release.
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
)
