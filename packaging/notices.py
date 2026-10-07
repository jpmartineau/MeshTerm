# SPDX-License-Identifier: Apache-2.0
"""Build ``THIRD-PARTY-NOTICES.txt``: each licence that the frozen build must carry.

A PyInstaller build of one file is a copy of MeshTerm and of all the code that MeshTerm
imports at run time. Apache-2.0 §4(a) and §4(d) require the LICENSE and NOTICE of MeshTerm
with each such copy. Each MIT, BSD, and PSF dependency also requires that its own notice
stays with the copies. Before this module existed, the spec (``packaging/meshterm.spec``)
bundled neither of them. The five installers carried the licence of the bundled font and
nothing more.

This module is a generator on purpose, and not a file in git. A copy in git becomes wrong,
with no message, when a dependency changes owner or its licence text, or when somebody
changes a version pin. The generator runs at build time (refer to the spec) and again in
:mod:`tests.test_notices`. It examines the real runtime dependency closure of the
``mesh-term`` distribution that is installed in this interpreter. Thus the notices of a
build always match the packages that the build bundled, on the platform that built it. A
dependency that only one platform needs (a bleak backend, ``tomli`` below 3.11) is only in
the notices of that platform, because it was never in the closures of the other platforms.

Two rules have no exception:

* If a dependency has no licence file that the module can find, the whole build fails with
  an error. The build must not ship with no message and without the file. Refer to
  :data:`_VENDORED_LICENSE_GAPS` for the one exception that we know, and the reason that
  it is a documented repair and not a silent one.
* The PSF licence of Python is always the first entry. The module finds it in the same way
  as the ``license`` builtin. It uses the search of that builtin for candidate files, and it
  does not work out the layout again. Thus it resolves correctly with a venv or without
  one, on Windows or POSIX. A future interpreter layout that the code does not expect
  also resolves correctly, or it fails with an error and does not guess wrongly.
"""

from __future__ import annotations

import builtins
import platform
import sys
from dataclasses import dataclass
from importlib.metadata import Distribution, PackagePath, distribution
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

#: The distribution that this build ships (refer to ``pyproject.toml``). Callers must not
#: change it as a parameter. A notices file for the closure of a different distribution
#: does not describe what this build bundles.
ROOT_DISTRIBUTION = "mesh-term"

#: A small number of dependencies. Their wheels on PyPI have a licence *classifier* or
#: *expression*, but the authors built and uploaded them without the licence text. We
#: verified this by an examination of each wheel (``unzip -l``). We did not assume it from
#: the metadata alone. The real text is stored here, with no change, from the repository of
#: each project. The header of each stored file gives the exact source and the date of the
#: download. No other dependency gets this treatment. A dependency that is not in this list
#: still fails the build if its wheel has the same gap. This is the purpose of the list: it
#: is a documented repair for known problems that we verified. It is not a general way out
#: for an unknown problem in the future.
#:
#: * ``pyserial``: no newer release exists. Version 3.5 (2020) is still the latest pyserial
#:   on PyPI, and its wheel never included a LICENSE, COPYING, or NOTICE file.
#: * The nine ``winrt-*`` packages: the Windows BLE backend of bleak
#:   (``bleak.backends.winrt``) adds these only on ``sys_platform == "win32"``, so they are
#:   only in the closure of a Windows build. The pywinrt project generates and publishes all
#:   nine together. They share one upstream licence file, which none of their wheels
#:   include.
#: * ``pyobjc-core`` and ``pyobjc-framework-libdispatch``: two of the four pyobjc packages
#:   that the macOS BLE backend of bleak (``bleak.backends.corebluetooth``) adds on
#:   ``sys_platform == "darwin"``. Thus they are only in the closure of a macOS build, and
#:   the release build of 0.3.3 found them there. Their 12.2.2 wheels have no licence file.
#:   The other two (Cocoa, CoreBluetooth) have one, and the module reads it from their
#:   dist-info as it does for all other packages. All four come from the one pyobjc
#:   repository and its single MIT text.
_VENDORED_LICENSE_GAPS: dict[str, tuple[str, ...]] = {
    "pyserial": ("vendored-licenses/pyserial-LICENSE.txt",),
    "pyobjc-core": ("vendored-licenses/pyobjc-LICENSE.txt",),
    "pyobjc-framework-libdispatch": ("vendored-licenses/pyobjc-LICENSE.txt",),
    "winrt-runtime": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-devices-bluetooth": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-devices-bluetooth-advertisement": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-devices-bluetooth-genericattributeprofile": (
        "vendored-licenses/pywinrt-LICENSE.txt",
    ),
    "winrt-windows-devices-enumeration": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-devices-radios": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-foundation": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-foundation-collections": ("vendored-licenses/pywinrt-LICENSE.txt",),
    "winrt-windows-storage-streams": ("vendored-licenses/pywinrt-LICENSE.txt",),
}

#: The usual licence file names that a wheel from before PEP 639 can bundle at its dist-info
#: root, with no declaration in the metadata. The match ignores case and uses the prefix.
#: Thus ``LICENSE``, ``LICENSE.txt``, ``LICENSE-MIT``, and ``License.rst`` all match.
_CONVENTIONAL_LICENSE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")


class NoticesError(RuntimeError):
    """A licence notice that this build needs is not available.

    The module raises this error instead of a silent omission of an entry. A bundle that
    drops a notice with no message is the exact failure that this module prevents. Thus a
    missing notice stops the build, and the build does not ship.
    """


@dataclass(frozen=True)
class Notice:
    """One entry in the generated notices file.

    Attributes:
        name: The name of the distribution, as PyPI spells it. For Python itself, it is the
            spelling of the ``platform`` module. The name is not canonicalised. Thus the
            file shows the name that a person expects when they search for the package.
        version: The installed version.
        summary: The licence expression or classifiers that this distribution declares, for
            the header line. It is never the licence text. The licence text is :attr:`text`.
        text: The licence text, word for word and with no change (one or more files,
            joined).
    """

    name: str
    version: str
    summary: str
    text: str


def _python_notice() -> Notice:
    """Return the PSF licence notice for the interpreter that runs this build.

    The function uses the search for candidate files that the ``license`` builtin does.
    ``site.setcopyright`` builds the search from ``os.path.dirname(os.__file__)``, the
    install-root layout of Windows, the lib layout of POSIX, and a fallback in the same
    directory. The function does not work out these paths again, because a layout that it
    does not expect can occur. It also resolves correctly in a venv, with no special code
    for a venv. This is because ``os.__file__`` always points to the standard library of
    the base interpreter.

    Raises:
        NoticesError: No candidate file exists. The fallback text of the ``license``
            builtin is a pointer to a URL and not licence text, so it is not good enough
            to ship as a notice.
    """
    printer = getattr(builtins, "license", None)
    if printer is None:
        # `site` usually installs this at the start of the interpreter. It is missing only
        # if the interpreter started with `-S`. Install it here and do not give up. A build
        # environment that starts the interpreter in this way must not lose the notice.
        import site

        site.setcopyright()
        printer = builtins.license
    candidates = getattr(printer, "_Printer__filenames", None)
    if not candidates:
        raise NoticesError(
            "could not find Python's own license: the 'license' builtin exposes no "
            "candidate file list (unexpected interpreter internals)"
        )
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return Notice(
                name="Python",
                version=platform.python_version(),
                summary="PSF License Agreement",
                text=path.read_text(encoding="utf-8"),
            )
    raise NoticesError(
        "could not find Python's own license text; looked in: "
        + ", ".join(str(c) for c in candidates)
    )


def _license_summary(dist: Distribution) -> str:
    """The short "which licence" line for the header of a distribution.

    The first choice is the PEP 639 ``License-Expression`` (the SPDX expression that a
    modern build backend writes). The second choice is the ``License ::`` classifiers that
    older packages use. The third choice is the old free-text ``License`` field, but only
    if it is short enough to be a label and not a pasted licence. "unspecified" is the last
    choice, and it is not a failure. An unclear label is a problem of readability. A missing
    licence file is a problem of compliance, and it raises an error (refer to
    :func:`_license_text`).
    """
    metadata = dist.metadata
    expression = metadata.get("License-Expression")
    if expression:
        return expression
    classifiers = [c for c in metadata.get_all("Classifier") or [] if c.startswith("License")]
    if classifiers:
        return "; ".join(classifiers)
    legacy = metadata.get("License")
    if legacy and "\n" not in legacy and len(legacy) <= 100:
        return legacy
    return "unspecified"


def _dist_info_files(dist: Distribution) -> list[PackagePath]:
    """Each file that RECORD lists under the own ``.dist-info`` directory of this distribution.

    The function uses :attr:`Distribution.files`. It does not use the private
    ``Distribution._path`` that some tools use. Thus the function stays correct if a future
    ``importlib.metadata`` puts the metadata of a distribution in a different layout on the
    disk. The function only asks the distribution for files that it already says it owns.
    """
    files = dist.files or []
    return [f for f in files if str(f).split("/", 1)[0].endswith(".dist-info")]


def _license_files(dist: Distribution) -> list[PackagePath]:
    """The dist-info files that make the licence text of this distribution.

    If the metadata has ``License-File`` entries (PEP 639), the function uses them. It
    matches them by base name, because a build backend can put them in a subdirectory.
    (hatchling puts each one in a ``licenses/`` subdirectory. Other tools leave them at the
    dist-info root.) If there is no declaration, the function uses the files that the wheel
    has at the dist-info root, with a usual name. Most wheels from before 2024 have no
    declaration. A person who looks in the directory searches for the same files.
    """
    dist_files = _dist_info_files(dist)
    declared = dist.metadata.get_all("License-File") or []
    if declared:
        wanted = [Path(name).name for name in declared]
        by_name = {Path(str(f)).name: f for f in dist_files}
        # Use the declared order, not the order of RECORD. Thus the joined text below has
        # the order that the author of the package declared.
        return [by_name[name] for name in wanted if name in by_name]
    return sorted(
        (
            f
            for f in dist_files
            # Only the dist-info root: one path segment below the directory itself. A
            # nested `licenses/` folder that no metadata declaration names is not a
            # convention that the function must guess.
            if str(f).count("/") == 1
            and Path(str(f)).name.upper().startswith(_CONVENTIONAL_LICENSE_PREFIXES)
        ),
        key=str,
    )


def _vendored_license_files(canonical_name: str) -> list[Path]:
    """The local fallback licence text for a dependency whose wheel has none."""
    here = Path(__file__).parent
    return [here / rel for rel in _VENDORED_LICENSE_GAPS.get(canonical_name, ())]


def _license_text(dist: Distribution, canonical_name: str) -> str:
    """The licence text of one distribution, joined and word for word.

    Raises:
        NoticesError: The dist-info has no licence file, and no file is stored for the
            distribution. No later step can notice a notice that is missing with no
            message. Thus this error stops the build, and the build does not ship a gap.
    """
    matches = _license_files(dist)
    if matches:
        return "\n\n".join(
            f"--- {Path(str(f)).name} ---\n{f.locate().read_text(encoding='utf-8')}"
            for f in matches
        )
    vendored = _vendored_license_files(canonical_name)
    if vendored:
        for path in vendored:
            if not path.is_file():
                raise NoticesError(
                    f"{dist.metadata['Name']} {dist.version}: vendored license file "
                    f"missing at {path} (see _VENDORED_LICENSE_GAPS)"
                )
        return "\n\n".join(
            f"--- {path.name} ---\n{path.read_text(encoding='utf-8')}" for path in vendored
        )
    raise NoticesError(
        f"{dist.metadata['Name']} {dist.version}: no license file found in its "
        "dist-info (checked declared License-File entries and "
        "LICENSE*/LICENCE*/COPYING*/NOTICE* at the dist-info root) and none is "
        "vendored for it either — a bundle would ship this dependency without its "
        "license notice"
    )


def dependency_closure(root: str = ROOT_DISTRIBUTION) -> list[Distribution]:
    """The installed distributions that ``root`` pulls in on this interpreter.

    The function walks ``Requires-Dist`` recursively (``importlib.metadata`` gives it as
    :attr:`Distribution.requires`). It evaluates the environment marker of each requirement
    against this interpreter and platform. It sets ``extra`` to the empty string. For a
    marker, this means that no extras are requested (a bare ``pip install mesh-term`` asks
    for none). Thus a requirement with ``extra == "dev"`` is false, and the tools of the
    ``dev`` extra (pytest, pytest-asyncio, ruff) never enter the closure. A marker that
    names a platform or a Python version that this interpreter does not match is excluded in
    the same way. This is the reason to evaluate the markers, and not to read each
    ``Requires-Dist`` line as plain text.

    Returns:
        The distributions, in the order of their canonical (PEP 503) name. The order is
        deterministic. The list never includes ``root`` itself, because the licence of
        MeshTerm is ``LICENSE`` and ``NOTICE``, and it is not a third-party notice.
    """
    environment = default_environment()
    environment["extra"] = ""
    root_key = canonicalize_name(root)
    seen: dict[str, Distribution] = {}
    pending = [root]
    while pending:
        name = pending.pop()
        key = canonicalize_name(name)
        if key in seen:
            continue
        dist = distribution(name)
        seen[key] = dist
        for raw in dist.requires or ():
            requirement = Requirement(raw)
            if requirement.marker is not None and not requirement.marker.evaluate(environment):
                continue
            pending.append(requirement.name)
    seen.pop(root_key, None)
    return sorted(seen.values(), key=lambda d: canonicalize_name(d.metadata["Name"]))


def collect_notices(root: str = ROOT_DISTRIBUTION) -> list[Notice]:
    """All the notices that ``THIRD-PARTY-NOTICES.txt`` of this build must carry.

    The PSF notice of Python is always first. Python is not a PyPI distribution that
    ``Requires-Dist`` can name, so it can never appear in :func:`dependency_closure`. Each
    dependency follows it, in the order of its name. Thus the difference between the notices
    file of one build and the next is stable, and a person can examine it.
    """
    notices = [_python_notice()]
    for dist in dependency_closure(root):
        canonical = canonicalize_name(dist.metadata["Name"])
        notices.append(
            Notice(
                name=dist.metadata["Name"],
                version=dist.version,
                summary=_license_summary(dist),
                text=_license_text(dist, canonical),
            )
        )
    return notices


_HEADER = """\
Third-party notices
====================

This build of MeshTerm bundles the packages listed below as part of its Python
runtime. Each entry below reproduces that package's own license, verbatim and
unmodified, as its terms require. MeshTerm's own license and copyright notice are
in the LICENSE and NOTICE files bundled alongside this one, not repeated here.

Generated by packaging/notices.py at build time -- do not edit by hand.
"""

_RULE = "=" * 78


def render(notices: list[Notice]) -> str:
    """Render ``notices`` as one UTF-8 document that is deterministic and ends with a newline."""
    blocks = [_HEADER]
    for notice in notices:
        header = f"{notice.name} {notice.version} — {notice.summary}"
        blocks.append(f"{_RULE}\n{header}\n{_RULE}\n\n{notice.text.rstrip()}\n")
    return "\n".join(blocks).rstrip("\n") + "\n"


def write_third_party_notices(path: str | Path, root: str = ROOT_DISTRIBUTION) -> Path:
    r"""Generate ``THIRD-PARTY-NOTICES.txt`` and write it to ``path``.

    The function writes raw UTF-8 bytes. It does not use text mode. Thus a Windows build
    does not change the ``\n`` line endings to ``\r\n``. The content of the file must be the
    same, byte for byte, on each platform that builds it.

    Returns:
        ``path``, as a :class:`~pathlib.Path`, for the convenience of the caller.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render(collect_notices(root)).encode("utf-8"))
    return path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python packaging/notices.py OUTPUT_PATH``."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: notices.py OUTPUT_PATH", file=sys.stderr)
        return 2
    written = write_third_party_notices(args[0])
    print(f"wrote {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
