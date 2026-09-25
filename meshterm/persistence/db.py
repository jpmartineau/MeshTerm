# SPDX-License-Identifier: Apache-2.0
"""SQLite database bootstrap and schema.

The schema is intentionally generic: a central ``runs`` table records every tool
execution (with arguments and a JSON summary), and measurement tables reference it so
new tools can persist data without schema churn.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 17

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
    scope       TEXT              -- outbound floods: the region it was sent under, '*' for
                                  --   unscoped, NULL where it was never known
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
    """Apply the durability/throughput pragmas, on the safe side of every trade-off.

    MeshTerm writes constantly and in tiny pieces — every packet the monitor hears becomes
    an ``observations`` row — and its most constrained host (the PicoCalc) keeps its
    database on an SD card, where SQLite's default of a full ``fsync`` per transaction
    costs milliseconds *each*. Three pragmas move that cost without moving the risk:

    * ``journal_mode=WAL`` — writers append to a log instead of rewriting the rollback
      journal, which turns each insert's several synchronous seeks into one append, and
      lets a reader (a screen redrawing) run against a writer (the monitor recording)
      instead of blocking on it. This one is *persistent*: it lives in the database file,
      so it survives into every later connection once set.
    * ``synchronous=NORMAL`` — the actual latency win, but **only requested once WAL is
      confirmed engaged**. Under WAL, NORMAL risks losing the last few transactions to a
      power cut and nothing worse; under the rollback journal it can leave the file
      *corrupt*. A PicoCalc running on a battery pack loses power for real, so on any
      filesystem that refused WAL this stays at the default ``FULL`` — slow and intact
      beats fast and unreadable.
    * ``temp_store=MEMORY`` — sorts and temporary b-trees stay in RAM rather than landing
      on the card. This workload's temporaries are small (screen-sized query results), so
      the memory is bounded and the card sees strictly less traffic.

    WAL can legitimately be unavailable — a read-only mount, or a filesystem without the
    shared-memory primitive it needs — and SQLite reports that by *returning* the mode it
    actually left the database in rather than raising, so the result is inspected instead
    of trusted. Either way this is best-effort tuning: a database that will not take the
    pragmas is still a perfectly working database, so nothing here is allowed to fail the
    open.

    Args:
        conn: The freshly opened connection, before any schema work.
    """
    # Returns the resulting mode as a row — "wal" only if the switch actually took.
    try:
        mode = conn.execute("PRAGMA journal_mode = WAL;").fetchone()
    except sqlite3.Error:  # pragma: no cover - filesystem-dependent
        mode = None
    if mode is not None and str(mode[0]).lower() == "wal":
        conn.execute("PRAGMA synchronous = NORMAL;")
    try:
        conn.execute("PRAGMA temp_store = MEMORY;")
    except sqlite3.Error:  # pragma: no cover - defensive; no known failure mode
        pass


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite database and ensure the schema exists.

    Args:
        db_path: Filesystem location of the database. Parent directories are created.

    Returns:
        An open connection with ``Row`` factory, foreign keys enabled, and the
        durability/throughput pragmas applied (see :func:`_tune`).
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
    """Bring an existing database's tables up to the current schema.

    ``executescript(_SCHEMA)`` only *creates* missing tables (``IF NOT EXISTS``); it cannot
    add a column to a table an older version already created. Column additions are applied
    here, guarded so the migration is idempotent and safe to run on every open. Indexes on
    added columns are created here too (not in ``_SCHEMA``), so ``executescript`` never
    references a column an older database hasn't grown yet.
    """
    message_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "channel_id" not in message_cols:
        # v3 -> v4: channel history moved from being keyed by slot index to a
        # slot-independent channel identity. Existing rows are backfilled lazily at runtime
        # (see Repository.backfill_channel_ids) once the device's channels can be read.
        conn.execute("ALTER TABLE messages ADD COLUMN channel_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages(channel_id)")

    observation_cols = {row["name"] for row in conn.execute("PRAGMA table_info(observations)")}
    if "node_type" not in observation_cols:
        # v4 -> v5: observations gained the transmitting node's advert type, so the map can
        # tell repeaters from leaf nodes. Older rows simply carry NULL (type unknown).
        conn.execute("ALTER TABLE observations ADD COLUMN node_type INTEGER")
    if "path" not in observation_cols:
        # v6 -> v7: observations gained the relay path of RX-logged packets, the passive
        # evidence the mesh topology graph is built from. Older rows carry NULL (no path).
        conn.execute("ALTER TABLE observations ADD COLUMN path TEXT")
    if "chan_hash" not in observation_cols:
        # v8 -> v9: overheard channel-text (GRP_TXT) frames keep the fields the packet
        # viewer needs to decrypt them — the channel-hash fingerprint, the 2-byte MAC, and
        # the ciphertext — so a channel we hold the key for stays readable even when the feed
        # is seeded from stored history (a live frame carries them in its raw payload; a
        # replayed one had nowhere to keep them). Older rows carry NULL (never decryptable).
        conn.execute("ALTER TABLE observations ADD COLUMN chan_hash TEXT")
        conn.execute("ALTER TABLE observations ADD COLUMN cipher_mac TEXT")
        conn.execute("ALTER TABLE observations ADD COLUMN crypted TEXT")
    if "payload_typename" not in observation_cols:
        # v9 -> v10: 'packet' RX-log rows keep their payload class (GRP_TXT, TRACE, PATH,
        # ACK, …), the one identifying thing a relayed flood carries when it names no
        # origin node — so the dashboard feed can read "channel text" / "trace" instead of
        # a bare "?" even when seeded from history. Older rows carry NULL (class unknown).
        conn.execute("ALTER TABLE observations ADD COLUMN payload_typename TEXT")
    if "dest" not in observation_cols:
        # v12 -> v13: 'packet' rows keep what the frame addressed — the recipient's key
        # hash, the sender's (a hash, or the whole key an anonymous request carries), and
        # the token a tokened class stands on: an ack's checksum, a trace's tag. Decoded
        # from the frame body (see meshterm.core.frames), which only a live event carries,
        # so — exactly like the channel-text crypto trio above — a replayed row would
        # otherwise have nowhere to keep it and the feed's subject lane would go blank the
        # moment it was seeded from history. Older rows carry NULL until re-heard.
        conn.execute("ALTER TABLE observations ADD COLUMN dest TEXT")
        conn.execute("ALTER TABLE observations ADD COLUMN src TEXT")
        conn.execute("ALTER TABLE observations ADD COLUMN tag TEXT")
    if "public_key" not in observation_cols:
        # v11 -> v12: adverts carry the transmitting node's full public key, but only its
        # 12-hex prefix was ever stored as the node id. Keep that prefix as the canonical id
        # (traces, contacts, and the topology graph all match on it) and record the whole key
        # alongside it, so a hash lane can show more than the twelve stored digits. Older rows
        # carry NULL until re-heard — the raw payloads the past keys arrived in were never
        # persisted, so history can't be completed from itself.
        conn.execute("ALTER TABLE observations ADD COLUMN public_key TEXT")
    if "trace_snrs" not in observation_cols:
        # v13 -> v14: a trace's header path field is not relay hashes but one signed SNR
        # byte per hop it traversed (see meshterm.core.frames.trace_link_snrs), so it gets
        # a column of its own rather than sitting in `path` pretending to be adjacency.
        # Live frames carried the readings in their raw payload; without somewhere to keep
        # them a replayed row would show a trace's links blank, the same gap the crypto
        # trio and the addressing columns above were added to close.
        conn.execute("ALTER TABLE observations ADD COLUMN trace_snrs TEXT")
    if "route" not in observation_cols:
        # v14 -> v15: a frame's route type, which says what its `path` field *means* — and
        # without it the field was being read one way for packets that meant it two.
        #
        # A FLOOD packet accumulates: every relay appends its hash, so the path is the route
        # the packet actually travelled to reach us. A DIRECT packet is the opposite — the
        # path is a routing *instruction* the sender wrote and the relays consume, so it
        # describes where the packet is going, not where it has been, and an empty one means
        # "route used up", not "arrived in zero hops".
        #
        # Reading every path as a travelled route therefore drew a direct-routed message as
        # having reached us out of nowhere, and drew our own outgoing route as if it were an
        # inbound one (JP, 2026-09-02). Older rows carry NULL and are read as unknown rather
        # than assumed flooded; the raw headers they arrived in were never persisted, so this
        # cannot be filled in from history.
        conn.execute("ALTER TABLE observations ADD COLUMN route TEXT")
    if "transport_code" not in observation_cols:
        # v15 -> v16: a scoped flood's region. A TC_FLOOD frame carries a transport code —
        # an HMAC of its own payload keyed on the region it was sent into — and nothing
        # else names the region, so which one it was can only be answered by trying the
        # region names known (see meshterm.core.regions). Names arrive late: a repeater
        # tells us what it carries long after we overheard traffic scoped to it. So the
        # code is kept with the exact bytes it was computed over, and a row heard today
        # resolves against a name learned next week. The body is kept for TC_FLOOD rows
        # alone — a plain flood has no code, a direct frame is never region-filtered — so
        # the price is paid only by the traffic it explains. Older rows carry NULL: the
        # raw frames they arrived in were never persisted, so their scope is lost.
        conn.execute("ALTER TABLE observations ADD COLUMN transport_code TEXT")
        conn.execute("ALTER TABLE observations ADD COLUMN scope_body TEXT")
    if "scope" not in message_cols:
        # v16 -> v17: the scope a message we sent went out under. A channel can be given a
        # region its messages are flooded into (a scope is a *setting on the companion*,
        # made just before the send — see Device.send_channel_in_scope), and the scope can
        # change between one message and the next, or be dropped for one resend. So the
        # transcript cannot work out afterwards what a message went out under from what the
        # channel's scope is *now*; it is kept with the message, the moment it is sent.
        # Its first reader is the message paths dialog: a scoped message nothing relayed is
        # most often one no repeater in earshot carries the region of, and saying so needs
        # the region. Stored as the bare name, '*' for a flood sent unscoped, and NULL for
        # anything else — inbound messages, direct messages, and every row sent before this
        # column existed, whose scope was never known and cannot be recovered.
        conn.execute("ALTER TABLE messages ADD COLUMN scope TEXT")
