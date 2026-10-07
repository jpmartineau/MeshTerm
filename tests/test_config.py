# SPDX-License-Identifier: Apache-2.0
"""Tests for the load of the machine setup: where files are, and which device to use.

The values of MeshTerm's own behaviour are preferences. Their tests are in
``tests/test_preferences.py``. This file has only the tests for what ``config.toml`` still
holds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshterm.core.config import CONFIG_DIR_ENV, Settings, default_config_dir


def test_connect_on_start_defaults_true() -> None:
    """MeshTerm connects when it starts, if the config does not say otherwise."""
    assert Settings().connect_on_start is True


def test_connect_on_start_loads_from_toml(tmp_path: Path) -> None:
    """The value that tells the session to open the radio link at start loads from the config."""
    config = tmp_path / "config.toml"
    config.write_text("connect_on_start = false\n", encoding="utf-8")
    assert Settings.load(config).connect_on_start is False


def test_tcp_profile_inferred_and_loaded(tmp_path: Path) -> None:
    """A profile with a ``host`` loads as a TCP profile and gives its host:port endpoint."""
    config = tmp_path / "config.toml"
    config.write_text(
        '[profiles.wifi]\nhost = "192.168.1.50"\ntcp_port = 6000\n',
        encoding="utf-8",
    )
    profile = Settings.load(config).profiles["wifi"]
    assert profile.is_tcp and profile.transport == "tcp"
    assert profile.host == "192.168.1.50" and profile.tcp_port == 6000
    assert profile.tcp_endpoint == "192.168.1.50:6000"


def test_tcp_profile_defaults_port(tmp_path: Path) -> None:
    """A TCP profile that has only a host gets the default port in its endpoint."""
    config = tmp_path / "config.toml"
    config.write_text('[profiles.wifi]\nhost = "meshcore.local"\n', encoding="utf-8")
    profile = Settings.load(config).profiles["wifi"]
    assert profile.is_tcp and profile.tcp_endpoint == "meshcore.local:5000"


def test_config_dir_defaults_to_the_home_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    """If no override is set, the config and the data are in ``.meshterm`` in the home directory."""
    monkeypatch.delenv(CONFIG_DIR_ENV, raising=False)
    assert default_config_dir() == Path.home() / ".meshterm"


def test_config_dir_follows_the_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``$MESHTERM_HOME`` moves the whole directory, so a build can run beside a real one.

    Thus a downloaded binary does not open the same database as the checkout that it was
    built from. The history is the most valuable part of an install. Before this override,
    a second copy could not use another directory.
    """
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "elsewhere"))
    assert default_config_dir() == tmp_path / "elsewhere"


def test_config_dir_ignores_an_empty_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty or whitespace value uses the default directory. It does not resolve to nowhere.

    A shell often exports the variable with an empty value. If MeshTerm used "" to mean "put
    the data in the current directory", the history of a user would be in many directories.
    """
    monkeypatch.setenv(CONFIG_DIR_ENV, "   ")
    assert default_config_dir() == Path.home() / ".meshterm"
