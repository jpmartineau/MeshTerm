# SPDX-License-Identifier: Apache-2.0
# PyInstaller spec for the downloadable builds. Run from the repository root:
#
#     pyinstaller packaging/meshterm.spec
#
# The build is one file, in console mode. Console mode is necessary: MeshTerm is a terminal
# program. A windowed build has no place to draw, and it exits at once.
#
# Build on the machine that is the target. There is no cross-compiling here. The Windows
# build comes from a Windows runner, and the macOS build comes from macOS. This is the
# purpose of the workflow.

import re
import sys

from PyInstaller.utils.hooks import collect_submodules

# `packaging/notices.py` builds THIRD-PARTY-NOTICES.txt at build time. It is next to this
# spec and not under `meshterm/`, because it must not be importable from the installed
# package. Only the freezer imports it. The same is true of `entry.py` above. `SPECPATH`
# is one of the names that PyInstaller adds to the namespace of a .spec that runs. It is
# the directory that holds this file. PyInstaller resolves it from the path that was given
# to `pyinstaller`, not from the cwd of the process. Thus the spec finds `notices.py`
# with any way of starting it.
sys.path.insert(0, SPECPATH)
import notices  # noqa: E402 - the import must come after the sys.path change above

# The build makes this file new each time. It is not in git. It depends on the
# distributions that are installed in this interpreter. A copy that is not current can
# become wrong when the licence text of a dependency changes upstream and nobody notices.
# `workpath` is the temporary directory of PyInstaller for this type of build-time
# artefact. `--clean` cleans it, and nobody can mistake it for a source file. The file goes
# to `<workpath>/<specname>/THIRD-PARTY-NOTICES.txt`. PyInstaller adds the name of the spec,
# without its extension, to the workpath that it receives (refer to `build()` in
# `PyInstaller/building/build_main.py`). For this spec, this is `build/meshterm/`.
third_party_notices = notices.write_third_party_notices(
    os.path.join(workpath, "THIRD-PARTY-NOTICES.txt")
)

# The version resource of the Windows build. It is the Details tab of the Properties of the
# file, and the name and version that Windows shows for it. Without it, the .exe is only
# "meshterm.exe" with no product, and SignPath refuses to sign it. The signing configuration
# of SignPath checks the product name and version that this resource has. Thus a file
# without them cannot pass as a MeshTerm release. The spec builds the resource from the
# `__version__` of the package. It does not keep a second copy, because a second copy can
# become wrong. The other platforms have no such resource, and PyInstaller gives a warning
# if the spec gives one there.
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

    # This is the checkout that PyInstaller freezes. It is not a MeshTerm that the
    # interpreter has installed. `Analysis` below also reads its sources from here.
    sys.path.insert(0, os.path.dirname(SPECPATH))
    import meshterm  # noqa: E402 - the import must come after the sys.path change above

    # The fixed half of the resource has exactly four numbers: 0.10.2 becomes (0, 10, 2, 0).
    # A suffix such as `rc1` has no place there, and the spec removes it. The text fields
    # below keep the version exactly as written.
    numbers = [int(n) for n in re.match(r"\d+(?:\.\d+)*", meshterm.__version__)[0].split(".")]
    numbers = tuple((numbers + [0, 0, 0, 0])[:4])
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[
            StringFileInfo(
                [
                    # 0409 is US English, and 04B0 shows that the strings are Unicode.
                    # Nearly all Windows programs declare this pair.
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

# The assets must go to `meshterm/assets`. `ui/about.py` finds them with
# `Path(__file__).parent.parent / "assets"`, and this path must resolve in the bundle in the
# same way as in a checkout.
datas = [
    ("../meshterm/assets", "meshterm/assets"),
    # The node of the SPI radio runs by path under another Python. This is the Python that
    # has the radio library, and this build does not have it. Thus the node must exist as a
    # source file, next to the place where `spiradio.node_script()` looks for it. Bytecode in
    # the archive is not sufficient.
    ("../meshterm/core/radionode.py", "meshterm/core"),
    # A build of one file is a copy of MeshTerm. Apache-2.0 §4(a) and §4(d) require the
    # LICENSE and the NOTICE of MeshTerm with each copy. They must not be inside the assets
    # of the app. They must be at the root of the bundle, as they are at the root of the
    # repository.
    ("../LICENSE", "."),
    ("../NOTICE", "."),
    # The notice of each MIT, BSD, and PSF dependency must go with the copies in the same
    # way. Refer to packaging/notices.py for how the build makes it.
    (str(third_party_notices), "."),
]

hiddenimports = [
    # Each tool module. `meshterm.tools` finds these with `pkgutil.iter_modules` at import
    # time, so no static code names a tool module. A frozen build without this line starts
    # with an empty registry: no menu entries except Quit, and no CLI subcommands. The app
    # starts and looks correct, and this is the worst way for the failure to appear.
    *collect_submodules("meshterm.tools"),
    # bleak chooses its backend at run time, by platform. Thus static analysis never sees
    # the backend that the app uses. Collect all the backends, and keep the unused ones.
    *collect_submodules("bleak.backends"),
    # The same is true for the transports of the companion library.
    *collect_submodules("meshcore"),
    # Rich and prompt_toolkit both use some modules dynamically.
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
    # The app does not use these modules, and each one adds a GUI toolkit or a test framework.
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
    # UPX is off on purpose. It saves a few megabytes, but antivirus software often flags
    # the result. For a first release, this is not a good trade.
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
)
