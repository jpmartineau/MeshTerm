# SPDX-License-Identifier: Apache-2.0
"""Enforce SPDX license identifiers on all Python files.

Every Python source file in the MeshTerm project must declare its license with
a SPDX-License-Identifier comment on line 1 (or line 2 if it starts with a
shebang). This test walks the entire codebase and asserts the identifier is
present. A file lifted out of the repository carries no license marking
otherwise, so this gate ensures no file lands unmarked.
"""

from pathlib import Path


def test_spdx_headers():
    """Every Python file must have an SPDX-License-Identifier header."""
    repo_root = Path(__file__).parent.parent
    python_files = []

    # Collect all Python files from the required directories
    for directory in ["meshterm", "tests", "packaging", "scripts/picocalc-lyra/xiao-radio"]:
        dir_path = repo_root / directory
        if dir_path.exists():
            if directory == "packaging":
                # For packaging, include both .py files and .spec files
                python_files.extend(dir_path.glob("*.py"))
                python_files.extend(dir_path.glob("*.spec"))
            else:
                python_files.extend(dir_path.rglob("*.py"))

    # Also check the extensionless scripts/uconsole/meshterm-spi-bridge script
    spi_bridge = repo_root / "scripts" / "uconsole" / "meshterm-spi-bridge"
    if spi_bridge.exists():
        python_files.append(spi_bridge)

    missing_spdx = []
    for filepath in sorted(python_files):
        # A file that cannot be read as UTF-8 is a finding, not something to skip: every
        # source file here is UTF-8, and a silent skip would let an unmarked file through.
        head = filepath.read_text(encoding="utf-8").split("\n", 2)[:2]
        if not any("SPDX-License-Identifier:" in line for line in head):
            missing_spdx.append(str(filepath.relative_to(repo_root)))

    assert not missing_spdx, (
        f"Found {len(missing_spdx)} Python file(s) without SPDX-License-Identifier:\n"
        + "\n".join(f"  {f}" for f in missing_spdx)
    )
