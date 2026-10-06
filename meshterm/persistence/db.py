# SPDX-License-Identifier: Apache-2.0
"""The SQLite database: its start-up and its schema.

The schema is generic on purpose. A central ``runs`` table stores each run of a tool,
with its arguments and a JSON summary. The measurement tables refer to that table. Thus a
new tool can store data, and the schema does not have to change again and again.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 18

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Small key/value store for UI state that should survive across sessions (e.g. the last
-- map viewport), independent of any tool run. New keys need no schema change.
CREATE TABLE IF NOT EXISTS app_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per tool execution; the spine that measurements hang off of.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tool        TEXT    NOT NULL,
    profile     TEXT,
    args_json   TEXT    NOT NULL DEFAULT '{}',
    status      TEXT    NOT NULL DEFAULT 'running',  -- running | ok | error
    summary_json TEXT,
    started_at  TEXT    NOT NULL,
    finished_at TEXT
);

-- Aggregated outcome of a single path trace, linked to its run.
CREATE TABLE IF NOT EXISTS traces (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    target        TEXT    NOT NULL,
    success       INTEGER NOT NULL,
    hop_count     INTEGER NOT NULL DEFAULT 0,
    min_snr       REAL,
    round_trip_ms REAL,
    tx_power      INTEGER,
    created_at    TEXT    NOT NULL
);

-- Per-hop SNR for a trace.
CREATE TABLE IF NOT EXISTS trace_hops (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id  INTEGER NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    hop_index INTEGER NOT NULL,
    node      TEXT,
    snr       REAL    NOT NULL
);

-- One robust sample per (tx_power) tried during TX-power optimization.
CREATE TABLE IF NOT EXISTS tx_samples (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    target        TEXT    NOT NULL,
    tx_power      INTEGER NOT NULL,
    median_min_snr REAL,
    success_rate  REAL,
    samples       INTEGER NOT NULL,
    created_at    TEXT    NOT NULL
);

-- A candidate path evaluated during path optimization.
CREATE TABLE IF NOT EXISTS path_candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    target        TEXT    NOT NULL,
    path_json     TEXT    NOT NULL,
    bottleneck_snr REAL,
    success_rate  REAL,
    median_rtt_ms REAL,
    created_at    TEXT    NOT NULL
);

-- One entry of a remote repeater's neighbour table, fetched over the mesh (v8). Each row
-- is a link the repeater reported hearing directly, with SNR measured at the repeater;
-- refetching appends a new snapshot and readers take the latest row per pair.
CREATE TABLE IF NOT EXISTS neighbour_reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    repeater   TEXT    NOT NULL,  -- canonical id of the repeater that was asked
    neighbour  TEXT    NOT NULL,  -- reported neighbour's key-prefix hex, as replied
    snr        REAL,              -- dB, measured at the repeater
    heard_at   TEXT,              -- when the repeater last heard the neighbour
    fetched_at TEXT    NOT NULL
);

-- One record-holding walk in the trophy case (v11), scored from a trace that came
-- home (see services/records). Records live per
-- (category, width_bytes) because the per-hop hash width bounds both the walk's maximum
-- length (the path field is 64 bytes) and its collision odds — a 1-byte record is not
-- comparable to a 4-byte one. Standalone rows (no run_id): the walks' traces are already
-- persisted under their own runs, and a record must outlive history pruning. app_version
-- stamps the discoverer so future schema/scoring migrations can tell eras apart.
CREATE TABLE IF NOT EXISTS discovered_paths (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    category      TEXT    NOT NULL,
    width_bytes   INTEGER NOT NULL,
    spec          TEXT    NOT NULL,   -- the transmitted hex spec, comma-separated
    route_json    TEXT    NOT NULL,   -- canonical walked node ids, aligned with spec
    score         REAL    NOT NULL,
    stats_json    TEXT    NOT NULL,
    app_version   TEXT    NOT NULL,
    discovered_at TEXT    NOT NULL
);

-- One packet overheard while passively monitoring the mesh (advert/telemetry/...).
CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node        TEXT,             -- 12-hex canonical id (grouped/joined on)
    public_key  TEXT,             -- the node's full public key, when the advert carried one
    name        TEXT,
    kind        TEXT    NOT NULL DEFAULT 'advert',
    node_type   INTEGER,          -- advert type (repeater/chat/room/...), when carried
    snr         REAL,
    rssi        REAL,
    lat         REAL,
    lon         REAL,
    path        TEXT,             -- 'packet' rows: comma-separated relay-hop hex hashes
                                  --   (never a TRACE's: see trace_snrs)
    observed_at TEXT    NOT NULL,
    chan_hash   TEXT,             -- overheard channel frames: channel-hash fingerprint (hex)
    cipher_mac  TEXT,             -- …its 2-byte MAC (hex)
    crypted     TEXT,             -- …its ciphertext (hex), so a stored channel text still decrypts
    payload_typename TEXT,        -- 'packet' rows: the frame's payload class (GRP_TXT/TRACE/…)
    dest        TEXT,             -- …the recipient's key hash, for an addressed class (hex)
    src         TEXT,             -- …the sender's key hash, or its whole key (anon request)
    tag         TEXT,             -- …the frame's own token: an ack's checksum, a trace's tag
    trace_snrs  TEXT,             -- TRACE rows: comma-separated per-hop SNR readings (dB)
    route       TEXT,             -- 'packet' rows: FLOOD/DIRECT/… — what `path` MEANS here
    transport_code TEXT,          -- scoped (TC_*) rows: the 4-byte transport-codes field (hex)
    scope_body  TEXT              -- TC_FLOOD rows: payload type byte + payload (hex), the
                                  --   HMAC input a region name is resolved against
);

-- One chat message, sent or received, on a channel or with a contact. Unlike the other
-- measurement tables these are not tied to a single tool run (they arrive unsolicited via
-- the event hub), so run_id is nullable and detaches rather than cascades.
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    outbound    INTEGER NOT NULL DEFAULT 0,
    is_channel  INTEGER NOT NULL DEFAULT 0,
    channel_id  TEXT,             -- a channel's slot-independent identity (its history key)
    channel_idx INTEGER,          -- the slot it went out on / arrived on (reference only)
    peer        TEXT,
    peer_name   TEXT,
    text        TEXT    NOT NULL,
    snr         REAL,
    acked       INTEGER,
    created_at  TEXT    NOT NULL,
    scope       TEXT,             -- outbound floods: the region it was sent under, '*' for
                                  --   unscoped, NULL where it was never known
    author      TEXT              -- a room post: its author's key prefix (peer is the room)
);

CREATE INDEX IF NOT EXISTS idx_traces_run ON traces(run_id);
CREATE INDEX IF NOT EXISTS idx_trace_hops_trace ON trace_hops(trace_id);
CREATE INDEX IF NOT EXISTS idx_tx_samples_run ON tx_samples(run_id);
CREATE INDEX IF NOT EXISTS idx_neighbour_reports_pair ON neighbour_reports(repeater, neighbour);
CREATE INDEX IF NOT EXISTS idx_discovered_paths_cat ON discovered_paths(category, width_bytes);
CREATE INDEX IF NOT EXISTS idx_observations_run ON observations(run_id);
CREATE INDEX IF NOT EXISTS idx_observations_node ON observations(node);
-- The reception history is queried by time window from every direction — the monitor's
-- session seed, the dashboard's recent slice, the Time Machine's hour range, every
-- ``since=`` filter — and observed_at stores UTC ISO-8601, which orders lexically, so a
-- plain index turns each of those from a full-table scan into a range walk.
CREATE INDEX IF NOT EXISTS idx_observations_observed_at ON observations(observed_at);
CREATE INDEX IF NOT EXISTS idx_messages_peer ON messages(peer);
"""


def _tune(conn: sqlite3.Connection) -> None:
    """Apply the pragmas for durability and throughput, with the safe choice in each trade-off.

    MeshTerm writes all the time, in very small parts: each packet that the monitor hears
    becomes a row in ``observations``. The host with the most limits (the PicoCalc) keeps
    its database on an SD card. There, the SQLite default (a full ``fsync`` for each
    transaction) costs milliseconds for each transaction. Three pragmas make that cost
    smaller, and they do not make the risk larger:

    * ``journal_mode=WAL``: a writer appends to a log, instead of a new write of the
      rollback journal. Thus each insert does one append, instead of several synchronous
      seeks. Also, a reader (a screen that paints) can run at the same time as a writer
      (the monitor, which stores what it hears), and the reader does not wait for the
      writer. This pragma is persistent: the database file keeps it. Thus, after it is set
      one time, it applies to each later connection.
    * ``synchronous=NORMAL``: this pragma is the one that decreases the latency. But
      MeshTerm **asks for it only after it confirms that WAL is on**. With WAL, the risk of
      NORMAL is the loss of the last few transactions at a power failure, and nothing
      worse. With the rollback journal, NORMAL can make the file corrupt. A PicoCalc on a
      battery pack does lose power in real use. Thus, on a filesystem that refused WAL,
      this pragma stays at the default ``FULL``. A slow database that is intact is better
      than a fast database that MeshTerm cannot read.
    * ``temp_store=MEMORY``: sorts and temporary b-trees stay in RAM, instead of on the
      card. The temporary data of this workload is small (query results of the size of a
      screen). Thus the memory has a limit, and the card always gets less traffic, never
      more.

    WAL can be unavailable for a correct reason: a read-only mount, or a filesystem that
    does not have the shared-memory primitive that WAL must have. SQLite does not raise an
    error for this. It returns the mode that the database is in after the request. Thus
    this function checks the result, and does not assume that the change occurred. In all
    cases, this tuning is best effort. A database that does not accept the pragmas still
    works correctly. Thus nothing here can cause the open to fail.

    Args:
        conn: The connection, immediately after it opens and before any work on the schema.
    """
    # The pragma returns the result mode as a row. It is "wal" only if the change occurred.
    try:
        mode = conn.execute("PRAGMA journal_mode = WAL;").fetchone()
    except sqlite3.Error:  # pragma: no cover - filesystem-dependent
        mode = None
    if mode is not None and str(mode[0]).lower() == "wal":
        conn.execute("PRAGMA synchronous = NORMAL;")
    try:
        conn.execute("PRAGMA temp_store = MEMORY;")
    except sqlite3.Error:  # pragma: no cover - defensive. No failure mode is known.
        pass


def connect(db_path: Path) -> sqlite3.Connection:
    """Open or create the SQLite database, and make sure that the schema exists.

    Args:
        db_path: The location of the database in the filesystem. The function creates the
            parent directories.

    Returns:
        An open connection with the ``Row`` factory, with foreign keys turned on, and with
        the pragmas for durability and throughput applied (refer to :func:`_tune`).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _tune(conn)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Change the tables of an existing database to the current schema.

    ``executescript(_SCHEMA)`` only creates the tables that are missing
    (``IF NOT EXISTS``). It cannot add a column to a table that an older version created.
    Thus this function adds the new columns. A check guards each addition, so the
    migration is idempotent and safe to run at each open. This function also creates the
    indexes on the added columns (not ``_SCHEMA``). Thus ``executescript`` never refers to
    a column that an older database does not have yet.
    """
    message_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "channel_id" not in message_cols:
        # v3 -> v4: the key of the channel history changed from the slot index to a channel
        # identity that does not depend on the slot. MeshTerm fills in the existing rows
        # later, at runtime (refer to Repository.backfill_channel_ids), when it can read
        # the channels of the device.
        conn.execute("ALTER TABLE messages ADD COLUMN channel_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages(channel_id)")

    observation_cols = {row["name"] for row in conn.execute("PRAGMA table_info(observations)")}
    if "node_type" not in observation_cols:
        # v4 -> v5: observations got the advert type of the node that transmits. Thus the
        # map can tell repeaters from leaf nodes. Older rows have NULL (the type is
        # unknown).
        conn.execute("ALTER TABLE observations ADD COLUMN node_type INTEGER")
    if "path" not in observation_cols:
        # v6 -> v7: observations got the relay path of the packets in the RX log. This path
        # is the passive evidence from which MeshTerm builds the graph of the mesh
        # topology. Older rows have NULL (no path).
        conn.execute("ALTER TABLE observations ADD COLUMN path TEXT")
    if "chan_hash" not in observation_cols:
        # v8 -> v9: overheard channel-text (GRP_TXT) packets keep the fields that the packet
        # viewer must have to decrypt them: the channel-hash fingerprint, the 2-byte MAC,
        # and the ciphertext. Thus a channel for which we have the key stays readable, also
        # when the feed gets its first rows from stored history. (A live packet has these
        # fields in its raw payload. A replayed packet had no place to keep them.) Older
        # rows have NULL (MeshTerm can never decrypt them).
        conn.execute("ALTER TABLE observations ADD COLUMN chan_hash TEXT")
        conn.execute("ALTER TABLE observations ADD COLUMN cipher_mac TEXT")
        conn.execute("ALTER TABLE observations ADD COLUMN crypted TEXT")
    if "payload_typename" not in observation_cols:
        # v9 -> v10: 'packet' rows from the RX log keep their payload class (GRP_TXT, TRACE,
        # PATH, ACK, …). When a relayed flood does not name its origin node, the class is
        # the only thing that identifies the flood. Thus the dashboard feed can show
        # "channel text" or "trace" instead of only "?", also when it gets its first rows
        # from history. Older rows have NULL (the class is unknown).
        conn.execute("ALTER TABLE observations ADD COLUMN payload_typename TEXT")
    if "dest" not in observation_cols:
        # v12 -> v13: 'packet' rows keep the addresses of the packet: the hash of the
        # recipient's key, the sender's hash (or the full key that an anonymous request
        # carries), and the token of a class that has one (the checksum of an ack, the tag
        # of a trace). MeshTerm decodes these values from the packet body (refer to
        # meshterm.core.frames), and only a live event has the body. Thus, the same as
        # the three channel-text crypto fields above, a replayed row has no other place to
        # keep these values. Without them, the subject lane of the feed becomes blank when
        # the feed gets its first rows from history. Older rows have NULL until the packet
        # is heard again.
        conn.execute("ALTER TABLE observations ADD COLUMN dest TEXT")
        conn.execute("ALTER TABLE observations ADD COLUMN src TEXT")
        conn.execute("ALTER TABLE observations ADD COLUMN tag TEXT")
    if "public_key" not in observation_cols:
        # v11 -> v12: an advert has the full public key of the node that transmits it, but
        # MeshTerm stored only its key prefix (12 hex digits) as the node id. Keep that
        # prefix as the canonical id (traces, contacts, and the topology graph all match on
        # it). Store the full key next to it, so that a key lane can show more than the
        # twelve stored digits. Older rows have NULL until the node is heard again.
        # MeshTerm did not store the raw payloads that brought the past keys, so the
        # history cannot complete itself.
        conn.execute("ALTER TABLE observations ADD COLUMN public_key TEXT")
    if "trace_snrs" not in observation_cols:
        # v13 -> v14: the path field in the header of a trace does not have relay hashes.
        # It has one signed SNR byte for each hop that the trace went through (refer to
        # meshterm.core.frames.trace_link_snrs). Thus it gets its own column, instead of a
        # place in `path`, where it looks like adjacency data but is not. Live packets had
        # the readings in their raw payload. Without a place to keep them, a replayed row
        # shows the links of a trace as blank. This is the same gap that the crypto fields
        # and the address columns above close.
        conn.execute("ALTER TABLE observations ADD COLUMN trace_snrs TEXT")
    if "route" not in observation_cols:
        # v14 -> v15: the route type of a packet, which tells what its `path` field means.
        # Without the route type, MeshTerm read the field in one way, but packets use it in
        # two ways.
        #
        # A FLOOD packet collects hops: each relay appends its hash, so the path is the
        # route that the packet went through to get to us. A DIRECT packet is the opposite.
        # Its path is a routing instruction that the sender wrote and that the relays
        # consume. Thus it tells where the packet goes, not where it was. An empty path
        # means "route used up", not "arrived in zero hops".
        #
        # MeshTerm read each path as a route that the packet went through. Thus it drew a
        # direct-routed message as if it came to us from nowhere, and it drew our own
        # outgoing route as if it were an incoming route (JP, 2026-09-02). Older rows have
        # NULL. MeshTerm reads them as unknown, and does not assume that they are floods.
        # MeshTerm did not store the raw headers that they came with, so the history
        # cannot supply this value.
        conn.execute("ALTER TABLE observations ADD COLUMN route TEXT")
    if "transport_code" not in observation_cols:
        # v15 -> v16: the region of a scoped flood. A TC_FLOOD packet has a transport code:
        # an HMAC of its own payload, keyed on the region that it was sent into. Nothing
        # else names the region. Thus the only way to find the region is to try each known
        # region name (refer to meshterm.core.regions). The names come late: a repeater
        # tells us which regions it carries a long time after we overheard traffic scoped
        # to them. Thus MeshTerm keeps the code with the exact bytes from which it was
        # computed, and a row heard today can resolve against a name learned next week.
        # Only TC_FLOOD rows keep the body: a plain flood has no code, and a direct packet
        # is never filtered by region. Thus only the traffic that the body explains pays
        # its cost. Older rows have NULL: MeshTerm did not store the raw packets that they
        # came in, so their scope is lost.
        conn.execute("ALTER TABLE observations ADD COLUMN transport_code TEXT")
        conn.execute("ALTER TABLE observations ADD COLUMN scope_body TEXT")
    if "scope" not in message_cols:
        # v16 -> v17: the scope of a message that we sent. A channel can have a region into
        # which its messages are flooded. (A scope is a setting on the companion, made
        # immediately before the send: refer to Device.send_channel_in_scope.) The scope
        # can change from one message to the next, or it can be removed for one resend.
        # Thus, later, the transcript cannot find the scope of a message from the scope
        # that the channel has now. MeshTerm keeps the scope with the message, at the time
        # that it sends the message. The first code that reads it is the message paths
        # dialog. When no repeater relayed a scoped message, the usual cause is that no
        # repeater in radio range carries its region. To tell the user this, the dialog
        # must have the region. The stored value is the bare name, '*' for a flood sent
        # unscoped, and NULL for all other rows: incoming messages, direct messages, and
        # each row sent before this column existed. The scope of these rows was never
        # known, and MeshTerm cannot recover it.
        conn.execute("ALTER TABLE messages ADD COLUMN scope TEXT")
    if "author" not in message_cols:
        # v17 -> v18: the author of a room post. A room server relays the posts of its
        # members as direct messages from itself. Thus the peer of the row is the room, and
        # the member who wrote the post is a second fact that the packet carries next to
        # it: the first four bytes of the member's key, with which the room signs the post.
        # MeshTerm stores that key prefix as lowercase hex. The value is NULL for all other
        # messages, which includes the command-line replies of a room. This NULL is how
        # MeshTerm tells a post from a reply in the one conversation that the two share.
        # Rows from before this column are NULL too: when MeshTerm stored such a post, the
        # author was lost on the way in, and nothing on disk can bring it back.
        conn.execute("ALTER TABLE messages ADD COLUMN author TEXT")
