#!/usr/bin/env python3
"""Preflight for a MeshTerm release, and the answer to "what's in the next one?".

Reports the seven things worth knowing before a version number is written down, and says
nothing else: the current version and the tag it belongs to, the commits since, whether
there is an `[Unreleased]` section to rename, which changelog link refs are missing,
whether the working tree is clean and level with `origin/main`, and the same for the site
checkout beside it, where the release's demo video goes (the `website-media` skill).

It changes nothing. Every finding is a line a person acts on, not a gate that blocks.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INIT = ROOT / "meshterm" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
SITE = ROOT.parent / "meshterm.net"

OK, WARN, INFO = "ok  ", "warn", "    "


def git(*args: str, repo: Path = ROOT) -> str:
    """Run a git command in ``repo``, returning stripped stdout ('' on failure)."""
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def say(mark: str, line: str) -> None:
    """Print one finding, indented under its heading; a blank mark is a plain note."""
    print(f"  {mark}  {line}" if mark.strip() else f"      {line}")


def current_version() -> str:
    """The version `meshterm/__init__.py` declares - the only place one is written."""
    found = re.search(r'^__version__\s*=\s*"([^"]+)"', INIT.read_text(encoding="utf-8"), re.M)
    return found.group(1) if found else "?"


def latest_tag() -> str:
    """The highest v-tag by version order, not by date — a re-tag must not reorder this."""
    tags = [t for t in git("tag", "-l", "v*").splitlines() if t]
    if not tags:
        return ""
    return sorted(tags, key=lambda t: [int(p) for p in re.findall(r"\d+", t)])[-1]


def next_patch(version: str) -> str:
    """The default bump. Any other part is JP's call, and he says which."""
    parts = version.split(".")
    if len(parts) != 3 or not parts[2].isdigit():
        return "?"
    return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"


def main() -> int:
    """Print the report. Nothing here blocks; every line is for a person to act on."""
    version, tag = current_version(), latest_tag()
    text = CHANGELOG.read_text(encoding="utf-8")

    print(f"\nMeshTerm {version}  |  last tag {tag or '(none)'}\n")

    print("Version")
    if tag and tag != f"v{version}":
        say(WARN, f"__version__ is {version} but the last tag is {tag} - already bumped?")
    else:
        say(OK, f"__version__ {version} matches the last tag")
    say(INFO, f"next patch would be {next_patch(version)}  (JP names anything else)")

    print("\nCommits since the tag")
    log = git("log", "--oneline", f"{tag}..HEAD") if tag else git("log", "--oneline")
    lines = [ln for ln in log.splitlines() if ln]
    if not lines:
        say(WARN, "none - there is nothing to release")
    else:
        say(INFO, f"{len(lines)} commit(s):")
        for ln in lines:
            print(f"        {ln}")

    print("\nChangelog")
    if re.search(r"^## \[Unreleased\]", text, re.M):
        body = text.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0].strip()
        if body:
            say(OK, "an [Unreleased] section with entries - rename it to the new version")
        else:
            say(WARN, "an [Unreleased] heading with nothing under it - the entry is unwritten")
    else:
        say(WARN, "no [Unreleased] heading - write the section from the commits above")

    versions = set(re.findall(r"^## \[(\d[^\]]*)\]", text, re.M))
    refs = set(re.findall(r"^\[(\d[^\]]*)\]:", text, re.M))
    missing = sorted(versions - refs, key=lambda v: [int(p) for p in re.findall(r"\d+", v)])
    if missing:
        # Cosmetic only: the heading renders as literal brackets instead of a link. The
        # release notes come from the section body, so a published release is unaffected.
        say(WARN, f"link refs missing at the bottom for: {', '.join(missing)}")
        say(INFO, "(cosmetic - the heading loses its link; release notes are unaffected)")
    else:
        say(OK, "every version heading has its link ref")

    print("\nTree")
    dirty = git("status", "--porcelain")
    say(
        OK if not dirty else WARN,
        "clean" if not dirty else f"{len(dirty.splitlines())} uncommitted change(s)",
    )
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    say(OK if branch == "main" else WARN, f"on {branch}")
    git("fetch", "--quiet", "origin")
    counts = git("rev-list", "--left-right", "--count", "origin/main...HEAD")
    if counts:
        behind, ahead = counts.split()
        say(OK if behind == "0" else WARN, f"{ahead} ahead / {behind} behind origin/main")

    print(f"\nSite ({SITE.name}, where the demo video goes)")
    if not (SITE / ".git").exists():
        say(WARN, f"no checkout at {SITE}: the video cannot be made")
    else:
        dirty = git("status", "--porcelain", "--", "src", repo=SITE)
        count = len(dirty.splitlines())
        say(OK if not dirty else WARN, "src/ clean" if not dirty else f"src/: {count} change(s)")
        git("fetch", "--quiet", "origin", repo=SITE)
        counts = git("rev-list", "--left-right", "--count", "origin/main...HEAD", repo=SITE)
        if counts:
            behind, ahead = counts.split()
            say(OK if behind == "0" else WARN, f"{ahead} ahead / {behind} behind origin/main")
            if behind != "0":
                say(INFO, "pull --ff-only before making the video")
            # These go out with the release's site push, before the tag.
            for subject in git("log", "--format=%s", "origin/main..HEAD", repo=SITE).splitlines():
                say(INFO, f"ahead: {subject}")

    print("\nRun the gates before tagging:")
    print("      python -m pytest -q && ruff check . && ruff format --check .\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
