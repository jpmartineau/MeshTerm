# SPDX-License-Identifier: Apache-2.0
"""The masks of the ``anonymize`` preference: what each one hides, and what it keeps.

A mask has two jobs, and a test for each one is necessary. It must hide enough that a
user can post the text in public. It must also keep enough that a maintainer can still
tell two radios apart, and can still read the line. Thus each test here asserts what is
gone and what is still there.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from rich.console import Console

from meshterm.core import anonymize as module
from meshterm.core import preferences
from meshterm.core.anonymize import anonymize, mask_host, mask_name, remember_host
from meshterm.core.preferences import Preferences
from meshterm.persistence.logging import configure_logging, get_logger, log_path


@pytest.fixture
def windows_home(monkeypatch: pytest.MonkeyPatch) -> str:
    """A Windows home folder for this test, whatever OS runs the suite."""
    home = r"C:\Users\jpmartineau"
    monkeypatch.setattr(module, "_home", lambda: home)
    return home


@pytest.fixture
def posix_home(monkeypatch: pytest.MonkeyPatch) -> str:
    """A home folder outside the standard places, which only the exact rule can find."""
    home = "/srv/lakeside"
    monkeypatch.setattr(module, "_home", lambda: home)
    return home


# --- names ----------------------------------------------------------------------------


def test_a_name_keeps_its_first_and_last_characters() -> None:
    """JP's example, and the edges of the rule: a name keeps a tidbit and its length."""
    assert mask_name("jpmartineau") == "j*********u"
    assert mask_name("abc") == "a*c"
    assert mask_name("jp") == "j*"
    assert mask_name("j") == "*"
    assert mask_name("") == ""


def test_the_account_in_this_home_is_masked_in_each_spelling(windows_home: str) -> None:
    """A backslash, a slash, and the doubled backslash of a ``repr`` all reach the account."""
    text = (
        r"File C:\Users\jpmartineau\AppData\x.py, "
        "WindowsPath('C:/Users/jpmartineau/.meshterm'), "
        r"{'out': 'C:\\Users\\jpmartineau\\bug.md'}"
    )
    masked = anonymize(text)
    assert "jpmartineau" not in masked
    assert r"C:\Users\j*********u\AppData\x.py" in masked
    assert "C:/Users/j*********u/.meshterm" in masked
    assert r"C:\\Users\\j*********u\\bug.md" in masked


def test_a_home_outside_the_standard_places_is_masked(posix_home: str) -> None:
    """The exact rule finds the home of this machine also where no general rule looks."""
    assert anonymize("cd /srv/lakeside/.meshterm") == "cd /srv/l******e/.meshterm"
    # A folder that only starts with the account name is a different folder.
    assert anonymize("/srv/lakesides/x") == "/srv/lakesides/x"


def test_any_account_in_a_standard_home_is_masked(posix_home: str) -> None:
    """Another account, a macOS home, and the Windows short form of a long name are masked."""
    assert anonymize("/home/jp/MeshTerm/log") == "/home/j*/MeshTerm/log"
    assert anonymize("/Users/someone/.meshterm") == "/Users/s*****e/.meshterm"
    assert anonymize(r"C:\Users\JPMART~1\AppData\Local\Temp") == (
        r"C:\Users\J******1\AppData\Local\Temp"
    )


# --- radio addresses --------------------------------------------------------------------


def test_a_bluetooth_address_keeps_two_digits_at_the_start_and_four_at_the_end() -> None:
    """Two radios stay two radios, as the last four digits of two cards do."""
    assert anonymize("BLE link to DA:C5:21:6B:7C:7C failed") == (
        "BLE link to DA:xx:xx:xx:7C:7C failed"
    )
    assert anonymize("da-c5-21-6b-7c-7c") == "da-xx-xx-xx-7c-7c"


def test_an_address_that_a_colon_follows_is_still_masked() -> None:
    """A log line can put a colon after an address. The rule must not stop there."""
    line = "could not open BLE companion 3C:84:27:CB:DE:81: connected to 3C:84:27:CB:DE:81"
    assert anonymize(line) == (
        "could not open BLE companion 3C:xx:xx:xx:DE:81: connected to 3C:xx:xx:xx:DE:81"
    )


def test_a_macos_peripheral_id_is_masked_in_the_same_way() -> None:
    """A UUID names a peripheral on macOS. It keeps the same two and four digits."""
    assert anonymize("1A2B3C4D-1111-2222-3333-444455556666") == (
        "1Axxxxxx-xxxx-xxxx-xxxx-xxxxxxxx6666"
    )


# --- host addresses ---------------------------------------------------------------------


def test_an_ipv4_address_keeps_its_first_and_last_octets() -> None:
    """The two middle octets go. The first and the last tell two hosts apart."""
    assert anonymize("no response from 192.168.0.180:5000.") == (
        "no response from 192.xxx.x.180:5000."
    )
    assert mask_host("10.0.0.5") == "10.x.x.5"


def test_a_registered_host_name_is_masked_where_it_occurs() -> None:
    """The name of a network companion is masked, also with a port and in another case."""
    remember_host("lakeside-companion.lan")
    masked = anonymize("couldn't connect to Lakeside-Companion.lan:5000")
    assert masked == "couldn't connect to l****************n.lan:5000"


def test_a_host_keeps_the_label_that_names_its_network() -> None:
    """The last label (``.local``, ``.lan``) says what type of network it is. It stays."""
    assert mask_host("meshcore.local") == "m******e.local"
    assert mask_host("companion") == "c*******n"
    assert mask_host("fe80::1ff:fe23:4567:890a") == "fe80::xxx:xxxx:xxxx:890a"


# --- what stays -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-17 14:23:11 WARNING  meshterm.core.connection: link lost",
        "trace 2f882ad1: path=3d63f2c23d63 flags=1 timeout=8.8s -> 1297ms",
        "os_build 10.0.19045.1234, firmware v1.15.0, python 3.10.11",
        "path a1,3d,f2 via 3d63c642",
        "could not open port 'COM20': PermissionError(13, 'Access is denied.')",
        "/dev/ttyACM0 at 115200",
    ],
)
def test_a_line_with_no_personal_detail_is_unchanged(text: str, posix_home: str) -> None:
    """Times, hashes, versions, paths of hops, and serial ports are not personal details.

    A mask that also hides these makes the log useless to the maintainer, who needs them.
    """
    assert anonymize(text) == text


def test_a_masked_text_masks_to_itself(windows_home: str) -> None:
    """A text can go through the mask two times with no change. Each mask is idempotent."""
    remember_host("hilltop.lan")
    text = (
        r"C:\Users\jpmartineau\x C:\Users\JPMART~1\y DA:C5:21:6B:7C:7C "
        "192.168.0.180 hilltop.lan:5000 1A2B3C4D-1111-2222-3333-444455556666"
    )
    once = anonymize(text)
    assert anonymize(once) == once


# --- the log ----------------------------------------------------------------------------


def test_the_log_is_masked_only_while_the_preference_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_home: str
) -> None:
    """The file gets each detail by default, and the masks from the next line after a change.

    The formatter reads the preference at each record. Thus the change on the Preferences
    page applies at once, with no restart.
    """
    prefs = Preferences()
    monkeypatch.setattr(preferences, "_current", prefs)
    configure_logging(Console(quiet=True), tmp_path, file_level=logging.WARNING, quiet=True)
    log = get_logger("core.connection")

    log.warning("BLE link to DA:C5:21:6B:7C:7C lost")
    prefs.set("anonymize", True)
    log.warning("BLE link to DA:C5:21:6B:7C:7C lost again")
    try:
        raise OSError(r"cannot read C:\Users\jpmartineau\.meshterm\x")
    except OSError:
        log.exception("read failed")

    first, second, *rest = _read(tmp_path).splitlines()
    assert "DA:C5:21:6B:7C:7C" in first
    assert "DA:xx:xx:xx:7C:7C" in second
    traceback = "\n".join(rest)
    assert "jpmartineau" not in traceback
    assert r"C:\Users\j*********u\.meshterm" in traceback


def _read(log_dir: Path) -> str:
    """The text of the log file, after each handler has written what it holds."""
    for handler in get_logger().handlers:
        handler.flush()
    return log_path(log_dir).read_text(encoding="utf-8")
