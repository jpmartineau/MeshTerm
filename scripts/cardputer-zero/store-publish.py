#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
r"""Publish the Cardputer Zero package to M5Stack's app store, with ``czdev`` and two corrections.

``czdev publish`` (M5Stack's CardputerZero-AppBuilder) uploads the ``.deb`` to a release in
the fork of ``CardputerZero/packages``, pushes a branch with the store listing, and opens a
pull request. The store merges an update by itself when its checks pass. This script runs
``czdev`` in its own process with two corrections, and it sets up ``git`` for it:

* The manifest records the email of the uploader. ``czdev`` takes the public email of the
  GitHub profile. MeshTerm uses only ``johnputer@meshterm.net`` (JP, 2026-10-07), so the
  script gives ``czdev`` that address.
* For a fork, ``czdev`` makes the branch name from the clock two times: before the upload
  and after it. The upload takes seconds, so the pull request asks for a branch that was
  never pushed, and GitHub refuses it (HTTP 422). The script makes the second call return
  the first name.
* ``czdev`` pushes to ``git@github.com:``. A CI runner or a Cardputer has no SSH key for
  GitHub, so the script sends ``git`` over HTTPS with the token, and gives it a name and an
  email for the commit. These settings are in the environment of this process only.

The corrections replace two functions of ``czdev``. Thus the release workflow pins
AppBuilder to one commit. If a later ``czdev`` does not have these functions, the script
stops with an error and does not publish.

The script publishes nothing when the store already has this version, or when a pull
request for it is open. Thus a second run of a release does not open a second pull request.
It also publishes nothing while a pull request for an earlier version is open, because
the two would add the same files and conflict. A release in that time goes out as usual,
and its store job only shows a warning. After the merge, run that store job again, and it
publishes the version.

    python scripts/cardputer-zero/store-publish.py --czdev ~/CardputerZero-AppBuilder \
        --deb dist/meshterm_0.10.4-1_arm64.deb

The token comes from ``$CARDPUTER_STORE_TOKEN`` (the release workflow), or from the
credentials of ``czdev login`` (a Cardputer). It must have the ``public_repo`` scope.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

#: The only email of MeshTerm.
EMAIL = "johnputer@meshterm.net"
NAME = "Jean-Pierre Martineau"
#: The directory with ``app-builder.json``, the icon, and the screenshots.
STORE = Path(__file__).resolve().parent / "store"
TARGET = "CardputerZero/packages"
PUBLISHED_INDEX = "https://cardputerzero.github.io/packages/dists/stable/main/binary-arm64/Packages"
TOKEN_ENV = "CARDPUTER_STORE_TOKEN"


def _api(path: str, token: str) -> object:
    """GET one GitHub API path, as JSON."""
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - a fixed https host
        return json.load(response)


def _deb_field(deb: Path, field: str) -> str:
    """One field of the control file of ``deb``."""
    done = subprocess.run(["dpkg-deb", "-f", str(deb), field], capture_output=True, text=True)
    return done.stdout.strip()


def _published_versions(package: str) -> set[str]:
    """The versions of ``package`` in the store's index. Empty if the index cannot be read."""
    try:
        with urllib.request.urlopen(PUBLISHED_INDEX, timeout=30) as response:  # noqa: S310
            text = response.read().decode()
    except OSError:
        return set()
    versions: set[str] = set()
    for stanza in text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in stanza.splitlines() if ": " in line)
        if fields.get("Package") == package and "Version" in fields:
            versions.add(fields["Version"])
    return versions


def _open_pull_requests(package: str, login: str, token: str) -> dict[str, str]:
    """The open pull requests of ``login`` for ``package``, any version: title to URL."""
    pulls = _api(f"/repos/{TARGET}/pulls?state=open&per_page=100", token)
    prefix = f"publish: {package} "
    return {
        pull["title"]: pull["html_url"]
        for pull in pulls
        if pull["user"]["login"] == login and pull["title"].startswith(prefix)
    }


def _set_up_git(login: str, token: str) -> None:
    """Send ``czdev``'s git over HTTPS with ``token``, as MeshTerm's author."""
    os.environ["CZDEV_GIT_TOKEN"] = token
    os.environ["CZDEV_GIT_LOGIN"] = login
    helper = (
        '!f() { test "$1" = get && printf "username=%s\\npassword=%s\\n" '
        '"$CZDEV_GIT_LOGIN" "$CZDEV_GIT_TOKEN"; }; f'
    )
    settings = [
        ("url.https://github.com/.insteadOf", "git@github.com:"),
        ("credential.https://github.com.helper", helper),
    ]
    os.environ["GIT_CONFIG_COUNT"] = str(len(settings))
    for index, (key, value) in enumerate(settings):
        os.environ[f"GIT_CONFIG_KEY_{index}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{index}"] = value
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    for role in ("AUTHOR", "COMMITTER"):
        os.environ[f"GIT_{role}_NAME"] = NAME
        os.environ[f"GIT_{role}_EMAIL"] = EMAIL


def _correct(publish: object, client_class: type) -> None:
    """Make the two corrections in ``czdev`` (refer to the module docstring)."""
    get_user = client_class.get_user

    def get_user_with_meshterm_email(self: object):  # noqa: ANN202 - czdev's own User
        user = get_user(self)
        user.email = EMAIL
        return user

    client_class.get_user = get_user_with_meshterm_email
    names: dict[tuple[str, str], str] = {}
    branch_name = publish.branch_name
    publish.branch_name = lambda meta: names.setdefault(
        (meta["package"], meta["version"]), branch_name(meta)
    )


def main() -> int:
    """Check, correct, and run ``czdev publish``."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--czdev", required=True, type=Path, help="a CardputerZero-AppBuilder")
    parser.add_argument("--deb", required=True, type=Path, help="the package to publish")
    args = parser.parse_args()
    deb = args.deb.resolve()

    sys.path.insert(0, str(args.czdev.resolve() / "scripts"))
    from czdev import auth, publish  # noqa: PLC0415 - from the AppBuilder given on the command line
    from czdev.github_client import GitHubClient  # noqa: PLC0415

    for owner, name in ((publish, "branch_name"), (GitHubClient, "get_user"), (auth, "load_token")):
        if not callable(getattr(owner, name, None)):
            sys.stderr.write(
                f"store-publish: czdev has no {name} -- this czdev is not the pinned one\n"
            )
            return 1

    token = os.environ.get(TOKEN_ENV) or auth.load_token()
    if os.environ.get(TOKEN_ENV):
        auth.load_token = lambda: token

    package, version = _deb_field(deb, "Package"), _deb_field(deb, "Version")
    if not package or not version:
        sys.stderr.write(f"store-publish: {deb} is not a Debian package\n")
        return 1
    if version in _published_versions(package):
        print(f"the store already has {package} {version}: nothing to publish")
        return 0
    login = _api("/user", token)["login"]
    title = f"publish: {package} {version}"
    pending = _open_pull_requests(package, login, token)
    if title in pending:
        print(f"a pull request for {package} {version} is already open: {pending[title]}")
        return 0
    if pending:
        # A second pull request would add the same files as the open one, and the two
        # would conflict. Thus this version waits until the open one is merged. Then a
        # rerun of the release's store job publishes it.
        mark = "::warning::" if os.environ.get("GITHUB_ACTIONS") else ""
        for other, url in pending.items():
            print(f"{mark}{other} is still open ({url}); {version} waits for its merge")
        return 0

    _correct(publish, GitHubClient)
    _set_up_git(login, token)
    # czdev reads app-builder.json from the working directory.
    os.chdir(STORE)
    publish.run(deb=str(deb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
