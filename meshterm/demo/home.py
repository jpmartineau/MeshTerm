# SPDX-License-Identifier: Apache-2.0
"""The home of the demo: its own data directory, written again at each start.

``--demo`` never uses the data directory of the user. It uses ``<config_dir>/demo``, and
before the app opens, :func:`prepare` writes everything in it again from the demo world:
the database (:mod:`meshterm.demo.history`) and the stores of the config directory (the
joined room, the admin passwords, the regions, the Watchtower). Thus each run starts from
the same state, whatever the previous run changed, and the history of the user never
gets a node of the demo.

Two things in the directory stay from one run to the next: the tile cache of the map,
because the tiles are the same and a download is slow, and the log.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from ..core.admin_store import AdminStore
from ..core.channels import channel_identity
from ..core.config import DB_FILENAME
from ..core.instancelock import hold_instance_lock
from ..core.models import Contact, LoginResult, RoomAccess, RoomLogin
from ..core.region_sim import SimulatedRegionMap
from ..core.region_store import RegionStore
from ..core.regions import WILDCARD, parse_region_list
from ..core.room_store import RoomStore
from ..core.watch_store import WatchStore
from ..persistence.repository import Repository
from . import history, script, world

#: The name of the demo directory, under the config directory of the user.
DEMO_DIRNAME = "demo"

#: The files that the demo writes again at each start. A store that the demo does not
#: fill is deleted, so that nothing that a previous run changed stays.
_STORES = (
    "preferences.toml",
    "contacts.json",
    "channels.json",
    "rooms.json",
    "admin.json",
    "regions.json",
    "watchtower.json",
    "courier.json",
    "mutes.json",
    "remote.json",
    "settings.json",
    "adverts.json",
    "devices.json",
)


def demo_home(config_dir: Path) -> Path:
    """The data directory of the demo, under the config directory of the user."""
    return config_dir / DEMO_DIRNAME


def prepare(home: Path, now: datetime) -> None:
    """Write the demo directory again: a new history that ends at ``now``, and new stores.

    The function holds the instance lock of the directory while it writes. Thus it never
    writes under a demo that runs in another window.

    Args:
        home: The demo directory (refer to :func:`demo_home`).
        now: The moment that the history ends at.

    Raises:
        InstanceBusy: Another MeshTerm runs the demo now.
    """
    with hold_instance_lock(home):
        for name in _STORES:
            (home / name).unlink(missing_ok=True)
        db_path = home / DB_FILENAME
        _empty(db_path)
        repo = Repository(db_path)
        try:
            history.seed(repo, now)
        finally:
            repo.close()
        _write_stores(home, now)


def _empty(db_path: Path) -> None:
    """Delete each row of an existing database, and keep the file.

    The function does not delete the file, because another process can have it open: the
    ``emulate`` command opens the database of the demo, then starts the MeshTerm that runs
    in its window, which prepares the demo. Windows does not let a process delete a file
    that is open.
    """
    if not db_path.exists():
        return
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            if row[0] != "sqlite_sequence" and not row[0].startswith("sqlite_")
        ]
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in tables:
            conn.execute(f'DELETE FROM "{table}"')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
            conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
    finally:
        conn.close()


def contact(node: world.Node) -> Contact:
    """The contact that the demo device holds for ``node``, as a store keys it."""
    return Contact(
        name=node.name,
        public_key=node.public_key,
        key_prefix=node.node_id,
        node_type=node.node_type,
    )


def _write_stores(home: Path, now: datetime) -> None:
    """Write the stores of the config directory that the demo fills.

    Each store writes its own file through its own methods, so that the files have the
    form that the app reads.

    Args:
        home: The demo directory.
        now: The start of the demo, from which the times of the alerts come.
    """
    room = world.node(world.JOINED_ROOM)
    RoomStore(home / "rooms.json").record(
        contact(room),
        world.ROOM_PASSWORD,
        RoomLogin(LoginResult.ACCEPTED, RoomAccess.MEMBER, flood=False),
    )
    admins = AdminStore(home / "admin.json")
    for name in world.ADMINISTERED:
        admins.remember(contact(world.node(name)), world.ADMIN_PASSWORD)
    regions = RegionStore(home / "regions.json")
    for repeater in world.REPEATERS:
        listed = SimulatedRegionMap(world.REGION_TABLES.get(repeater.name)).allowed_names()
        regions.learn_carried(repeater.node_id, [WILDCARD, *parse_region_list(listed)])
    for channel in world.CHANNELS:
        if channel.scope:
            regions.set_channel_scope(channel_identity(channel.name, channel.secret), channel.scope)
    watch = WatchStore(home / "watchtower.json")
    for name in script.WATCHED:
        node = world.node(name)
        watch.watch(node.node_id, node.name, node_type=node.node_type)
    for age_min, kind, name, message in script.ALERTS:
        watch.add_alert(kind, name, message, when=now - timedelta(minutes=age_min))
    for node in world.NODES:
        if node.quiet_hours is not None:
            watch.remember_known(node.node_id)
    watch.flush()
