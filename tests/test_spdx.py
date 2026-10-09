# SPDX-License-Identifier: Apache-2.0
"""Make sure that all Python files have an SPDX licence identifier.

Each Python source file in the MeshTerm project must declare its licence with a
SPDX-License-Identifier comment on line 1 (or on line 2 if the file starts with a shebang).
This test examines the entire code base and asserts that the identifier is there. If
someone copies a file out of the repository, the file has no licence marking without this
identifier. Thus this gate makes sure that no file has no marking.
"""

from pathlib import Path

from tests.conftest import not_ignored


def test_spdx_headers():
    """Each Python file must have an SPDX-License-Identifier header."""
    repo_root = Path(__file__).parent.parent
    python_files = []

    # Collect all the Python files from the necessary directories
    for directory in ["meshterm", "tests", "packaging", "scripts/picocalc-lyra/xiao-radio"]:
        dir_path = repo_root / directory
        if dir_path.exists():
            if directory == "packaging":
                # For packaging, include the .py files and the .spec files
                python_files.extend(dir_path.glob("*.py"))
                python_files.extend(dir_path.glob("*.spec"))
            else:
                python_files.extend(dir_path.rglob("*.py"))

    # Also examine the script scripts/uconsole/meshterm-spi-bridge, which has no extension
    spi_bridge = repo_root / "scripts" / "uconsole" / "meshterm-spi-bridge"
    if spi_bridge.exists():
        python_files.append(spi_bridge)

    missing_spdx = []
    for filepath in not_ignored(sorted(python_files)):
        # A file that MeshTerm cannot read as UTF-8 is a finding. The test must not skip it.
        # Each source file here is UTF-8, and a skip with no message lets a file with no
        # marking pass.
        head = filepath.read_text(encoding="utf-8").split("\n", 2)[:2]
        if not any("SPDX-License-Identifier:" in line for line in head):
            missing_spdx.append(str(filepath.relative_to(repo_root)))

    assert not missing_spdx, (
        f"Found {len(missing_spdx)} Python file(s) without SPDX-License-Identifier:\n"
        + "\n".join(f"  {f}" for f in missing_spdx)
    )
