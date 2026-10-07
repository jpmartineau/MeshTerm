# SPDX-License-Identifier: Apache-2.0
"""Tests for ``packaging/notices.py``, the generator of the third-party notices.

The tests load the module by file path and not as ``packaging.notices``. ``packaging`` is
also the name of a PyPI library that this module imports (``packaging.markers`` and
``packaging.requirements``). The ``packaging/`` directory of the repository is not a real
package. It has no ``__init__.py`` on purpose, so that it can never hide that library. A
regular package anywhere on ``sys.path`` always wins over a directory with the same name
and no ``__init__.py``. This is the reason that ``import packaging.markers`` finds the real
library, also with the root of the repository on the path. The load by file does not
depend on this rule for all time.

The tests skip when ``mesh-term`` is not installed in the interpreter that runs the tests.
The generator walks the installed dependency closure. Without an installed distribution to
walk, there is nothing to test here.
"""

from __future__ import annotations

import importlib.util
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import pytest
from packaging.requirements import Requirement

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - the code runs only on 3.10
    import tomli as tomllib

try:
    distribution("mesh-term")
except PackageNotFoundError:
    pytest.skip("mesh-term is not installed in this interpreter", allow_module_level=True)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "meshterm_packaging_notices", _REPO_ROOT / "packaging" / "notices.py"
)
assert _spec is not None and _spec.loader is not None
notices = importlib.util.module_from_spec(_spec)
# The code registers the module before exec. `Notice` is a dataclass under `from
# __future__ import annotations`, and dataclasses resolves deferred annotations through
# `sys.modules[cls.__module__]`. A module that is not registered there fails with a
# confusing `NoneType has no attribute '__dict__'` when the class body runs. This error
# has no relation to what this test checks.
sys.modules[_spec.name] = notices
_spec.loader.exec_module(notices)

#: The copyleft licences that no runtime dependency of MeshTerm can have. A check for
#: "GPL" alone also finds LGPL and AGPL, because both have it as a substring. MPL needs its
#: own check. PyInstaller is GPL with a bootloader exception. It must never appear here,
#: because it is a build tool and not part of the runtime closure that this module walks.
#: This test guards the absence of PyInstaller from the generated notices. The absence is
#: not an error to work around.
_COPYLEFT_MARKERS = ("GPL", "MPL")


@pytest.fixture(scope="module")
def all_notices() -> list:
    """Each notice that the install of ``mesh-term`` in this interpreter generates."""
    return notices.collect_notices()


def _direct_runtime_dependencies() -> list[Requirement]:
    """The ``[project.dependencies]`` that MeshTerm declares and that apply here.

    The function does the same marker evaluation as :func:`notices.dependency_closure`. It
    sets ``extra`` to ``""``, which means that no extras are requested. A dependency can be
    for a platform or a Python version that this interpreter is not. Examples are the
    ``tomli`` backport before Python 3.11, and one day a package for Windows only or macOS
    only. The function excludes such a dependency in the same way as the generated notices
    do, and does not flag it as "missing".
    """
    from packaging.markers import default_environment

    environment = default_environment()
    environment["extra"] = ""
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(raw) for raw in pyproject["project"]["dependencies"]]
    return [req for req in requirements if req.marker is None or req.marker.evaluate(environment)]


def test_every_direct_dependency_has_a_notice(all_notices: list) -> None:
    """Each applicable ``pyproject.toml`` dependency is in the generated notices."""
    from packaging.utils import canonicalize_name

    covered = {canonicalize_name(n.name) for n in all_notices}
    missing = [
        req.name
        for req in _direct_runtime_dependencies()
        if canonicalize_name(req.name) not in covered
    ]
    assert not missing, f"no notice generated for: {missing}"


def test_every_notice_has_license_text(all_notices: list) -> None:
    """No notice in the generated set has an empty licence text or a text of whitespace only."""
    empty = [f"{n.name} {n.version}" for n in all_notices if not n.text.strip()]
    assert not empty, f"empty license text for: {empty}"


def test_python_entry_present_and_mentions_psf(all_notices: list) -> None:
    """The PSF licence of Python is the first entry, and it is the PSF text."""
    assert all_notices, "no notices generated at all"
    python_entry = all_notices[0]
    assert python_entry.name == "Python"
    assert "PSF" in python_entry.summary or "PSF" in python_entry.text


def test_no_copyleft_dependency(all_notices: list) -> None:
    """The declared licence of a runtime dependency is never GPL, LGPL, AGPL, or MPL.

    The dependency closure of MeshTerm is only MIT, BSD, ISC, PSF, and Apache-2.0.
    PyInstaller is GPL with a bootloader exception, but it is a build tool. It is never a
    runtime dependency of the frozen app. Thus it must never appear in ``all_notices``.
    This test confirms that. It also guards against a future dependency that adds a
    copyleft licence with no message.
    """
    offenders = [
        f"{n.name} {n.version} ({n.summary})"
        for n in all_notices
        if any(marker in n.summary.upper() for marker in _COPYLEFT_MARKERS)
    ]
    assert not offenders, f"copyleft license found: {offenders}"


def test_output_is_deterministic() -> None:
    """Two renders of the same closure give output that is the same, byte for byte."""
    first = notices.render(notices.collect_notices())
    second = notices.render(notices.collect_notices())
    assert first == second


def test_rendered_output_is_newline_terminated_utf8() -> None:
    """The rendered document ends with one newline, and it encodes as UTF-8."""
    rendered = notices.render(notices.collect_notices())
    assert rendered.endswith("\n")
    assert not rendered.endswith("\n\n")
    rendered.encode("utf-8")  # this raises an exception for anything that does not round-trip


def test_write_third_party_notices_writes_utf8_lf(tmp_path: Path) -> None:
    """The file on disk is exactly the same as ``render()``, with no newline translation."""
    out = notices.write_third_party_notices(tmp_path / "THIRD-PARTY-NOTICES.txt")
    raw = out.read_bytes()
    assert b"\r\n" not in raw
    assert raw.decode("utf-8") == notices.render(notices.collect_notices())


def test_every_vendored_license_gap_has_its_file() -> None:
    """Each vendored licence gap has its file.

    A gap-table entry with a missing file fails only on the platform that uses it. Only a
    macOS build uses the pyobjc entries, and only a Windows build uses the winrt entries.
    Thus a typo in a path would show on a release runner and not here. This test checks
    each entry on each platform. The file must exist and must not be empty. The key must
    be in the canonical form (lower case, with hyphens) that ``collect_notices`` looks up.
    """
    for name, paths in notices._VENDORED_LICENSE_GAPS.items():
        assert name == name.lower().replace("_", "-"), name
        for rel in paths:
            path = Path(notices.__file__).parent / rel
            assert path.is_file(), f"{name}: {rel} is missing"
            assert path.read_text(encoding="utf-8").strip(), f"{name}: {rel} is empty"
