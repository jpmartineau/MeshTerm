# SPDX-License-Identifier: Apache-2.0
"""Repository: the only gateway between the app and the database.

Tools and services never run SQL directly. They call the typed methods here. Thus all the
persistence code is in one place, and a different storage backend can replace this one.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.frames import ADDRESSED_CLASSES, CHANNEL_CLASSES, ENDPOINT_HASH_BYTES
from ..core.models import (
    PATH_TRACE_TARGET,
    ChatMessage,
    HeardNode,
    Hop,
    NeighbourInfo,
    Observation,
    SelfActivity,
    TraceResult,
    TxLevelResult,
    utcnow,
)
from ..core.regions import SCOPED_ROUTES, raw_scope_body
from . import db

if TYPE_CHECKING:
    from ..core.contact_score import ContactSignals


#: The trailing time window to which MeshTerm prunes the live observation statistics of
#: the dashboard. It is for what happens now, and the RF health card labels it
#: "reception over 2 h". It is different from the longer channel-activity window below:
#: this window limits what a live screen shows, and that window only supplies a scaling
#: peak. The Live feed screen used this window before, but it does not now. Its depth is a
#: number of packets, not a duration (refer to
#: :data:`~meshterm.ui.livefeed_screen._FEED_SEED_FLOOR`). Thus this number means the two
#: hours of the dashboard and nothing else.
OBSERVATION_WINDOW = timedelta(hours=2)

#: The trailing time window over which MeshTerm computes the activity histogram of a
#: channel. Six hours is longer than the sparkline draws (refer to
#: :data:`ACTIVITY_DRAWN_BUCKETS`), on purpose. The extra history makes the pool larger
#: from which the shared scaling peak takes its percentile. Thus, when a busy period moves
#: out of the drawn window, the ceiling changes gradually, not suddenly (refer to
#: :func:`~meshterm.ui.braillechart.activity_peak`).
#: The MSGS lane still shows the volume of all time. This window stays a window of recent
#: activity.
ACTIVITY_WINDOW = timedelta(hours=6)

#: The number of equal time buckets in the window. At six hours, each bucket is five
#: minutes (the same as the drawn cadence). The histogram has all of them for the scaling
#: peak. The chart draws only the newest :data:`ACTIVITY_DRAWN_BUCKETS`.
ACTIVITY_BUCKETS = 72

#: The number of the newest buckets of the histogram that the channel sparkline draws: two
#: hours at five minutes each. A braille cell has two dot columns, so 24 buckets fill its
#: twelve characters. The rest of :data:`ACTIVITY_BUCKETS` supplies the scaling peak, but
#: the sparkline does not show them.
ACTIVITY_DRAWN_BUCKETS = 24

#: The number of recent packets from which the contact score takes its hop median (refer
#: to :meth:`Repository.contact_signals`). It is of the same order of magnitude as the
#: limit of :meth:`Repository.packet_paths`, for the same reasons. It is the only scan
#: there that builds one row in memory for each packet, and old hops describe a topology
#: that changed since then. The limit is large enough that each contact heard in the
#: recent past gets several readings for a median.
HOP_EVIDENCE_LIMIT = 20000


def _packet_raw(row: sqlite3.Row) -> dict | None:
    """Rebuild the minimum raw payload with which MeshTerm reads a stored ``packet`` row.

    Each row keeps what a live RX-log event had in its raw payload, under the same keys
    that the event used. Thus a replayed packet and a new packet read the same in all the
    layers above. The row keeps:

    * the payload class of the packet (``payload_typename``), so that a list can name
      what the packet is.
    * the three crypto fields of an overheard channel packet (fingerprint, MAC,
      ciphertext), so that a channel for which we have the key stays readable directly
      from history.
    * the addresses of the packet (:mod:`~meshterm.core.frames`): the recipient, the
      sender, and the token. Each value goes back under the key with which it was decoded.

    The stored sender is a hash or a full public key. The stored token is the checksum of
    an ack or the tag of a trace. The width of the value and the class of the packet tell
    which, so a second column was not necessary to tell them apart. The link readings of a
    trace (one for each hop) are the only values for which a separate column was
    necessary. They arrive in the place where each other class keeps its relay hashes, so
    ``path`` was the wrong place for them. A row that stored none of these values has no raw payload
    (``None``).
    """
    typename = _row_value(row, "payload_typename")
    chan_hash = row["chan_hash"]
    cipher_mac = _row_value(row, "cipher_mac")
    dest = _row_value(row, "dest")
    src = _row_value(row, "src")
    tag = _row_value(row, "tag")
    trace_snrs = _row_value(row, "trace_snrs")
    route = _row_value(row, "route")
    transport_code = _row_value(row, "transport_code")
    scope_body = _row_value(row, "scope_body")
    if not any((typename, chan_hash, cipher_mac, dest, src, tag, trace_snrs, route)):
        return None
    raw: dict = {}
    if typename:
        raw["payload_typename"] = typename
    if chan_hash:
        raw.setdefault("payload_typename", "GRP_TXT")  # pre-v10 rows: only channel text kept one
        raw["chan_hash"] = chan_hash
        raw["cipher_mac"] = row["cipher_mac"]
        raw["crypted"] = row["crypted"]
    if cipher_mac and not chan_hash:
        # An addressed packet keeps only its MAC (no channel envelope). Thus this branch
        # restores the MAC, instead of the chan_hash branch above.
        raw["cipher_mac"] = cipher_mac
    if dest:
        raw["dest_hash"] = dest
    if src:
        # A hash is one byte. A longer value is the full key that an anonymous request has.
        raw["src_key" if len(src) > 2 * ENDPOINT_HASH_BYTES else "src_hash"] = src
    if tag:
        raw["trace_tag" if typename == "TRACE" else "ack_crc"] = tag
    if trace_snrs:
        raw["trace_snrs"] = [float(v) for v in str(trace_snrs).split(",") if v]
    if route:
        raw["route_typename"] = route
    if transport_code:
        # The code of a scoped packet and the bytes from which it was computed. Thus its
        # region can resolve against names that MeshTerm learns after it heard the packet
        # (refer to meshterm.core.regions).
        raw["transport_code"] = transport_code
    if scope_body:
        raw["scope_body"] = scope_body
    return raw


def _as_when(iso: Any) -> datetime | None:
    """Parse a stored ISO timestamp, or return ``None`` when it is absent or not valid."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None


def _row_value(row: sqlite3.Row, key: str) -> Any:
    """Read ``key`` from a row. If the query did not select ``key``, return ``None``."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _hops_hash_bytes(hops: list[Hop]) -> int | None:
    """Recover the path-hash width (for each hop) of a stored trace from its hop hashes.

    The ``traces`` table is older than
    :attr:`~meshterm.core.models.TraceResult.path_hash_bytes`, so the width is not a
    column. But it does not have to be a column. A trace reply names each hop at exactly
    the width of the command. Thus the length of the stored hashes (the same for all) is
    the width. Without this function, traces read back from the database render node
    hashes (and the full public key of our node) at lengths that make no sense.

    Args:
        hops: The hops read back from the database. The function skips the final hop,
            which has no hash (our node).

    Returns:
        The width in bytes, or ``None`` when there are no hashes or their widths are not
        the same. (Stored trace replies always have the same widths. With ``None``, the
        hashes show at full width.)
    """
    widths = {len(h.node) for h in hops if h.node}
    if len(widths) != 1:
        return None
    width = widths.pop()
    return width // 2 if width and width % 2 == 0 else None


@dataclass(slots=True)
class ChannelStats:
    """The aggregated message history of one channel conversation.

    Attributes:
        total: All the messages stored for the channel, sent and received.
        recent: The messages in the trailing :data:`ACTIVITY_WINDOW` window.
        last_at: The time at which MeshTerm stored the most recent message of the channel,
            or ``None`` if MeshTerm cannot parse the stored timestamp.
        histogram: The messages of the window in :data:`ACTIVITY_BUCKETS` equal time
            buckets, newest first. Bucket 0 is the current five minutes. This is the order
            that :func:`~meshterm.ui.braillechart.activity_sparkline` expects (it reverses
            the window, so that "now" is drawn at the right edge). ``recent`` is always the
            sum of the histogram. The sparkline draws only the newest
            :data:`ACTIVITY_DRAWN_BUCKETS`. The older buckets supply the shared scaling
            peak (refer to :func:`~meshterm.ui.braillechart.activity_peak`).
    """

    total: int
    recent: int
    last_at: datetime | None
    histogram: tuple[int, ...]


@dataclass(slots=True)
class TracedPath:
    """The walked path of one successful trace, as evidence for the topology graph.

    Attributes:
        when: The time at which the trace completed.
        hops: The readings for each hop in path order, each ``(node, snr)``. ``node`` is
            the raw hex hash of the hop (``None`` for the final hop, which has no hash:
            our node). ``snr`` is the reception measured at that hop, when the packet
            arrived at it. Because a trace reply goes back along the path, the sequence
            has both the outbound leg and the return leg.
    """

    when: datetime
    hops: list[tuple[str | None, float]]


@dataclass(slots=True)
class PacketPath:
    """The relay path of one packet from the RX log, as evidence for the topology graph.

    Attributes:
        when: The time at which MeshTerm overheard the packet.
        origin: The hex hash of the origin node, when the packet class shows it (adverts
            do). Otherwise ``None``.
        snr: The SNR (dB) of our reception: a reading on the link from the last relay to
            us (or from the origin, when there are no relays).
        hops: The relay hashes in propagation order: the hop nearest the origin first,
            and the repeater that we heard last. Empty for a direct (zero-hop) packet.
    """

    when: datetime
    origin: str | None
    snr: float | None
    hops: list[str]


@dataclass(slots=True)
class NeighbourLink:
    """One direct link that a repeater reported, as evidence for the topology graph.

    This is the counterpart of :class:`TracedPath` and :class:`PacketPath` that MeshTerm
    gets from a repeater. MeshTerm does not infer this link from what we received. A
    remote repeater stated it about its own reception (refer to
    :meth:`Repository.neighbour_links`).

    Attributes:
        when: The recency of the link: the time at which the repeater last heard the
            neighbour. If that time is not known, the time at which we got the table.
        repeater: The canonical id of the repeater that reported the link.
        neighbour: The hex hash of the neighbour, as the repeater sent it in its reply
            (any width: the topology layer makes it canonical).
        snr: The SNR (dB) measured at the repeater, if the repeater reported it.
    """

    when: datetime
    repeater: str
    neighbour: str
    snr: float | None


@dataclass(slots=True)
class DiscoveredPath:
    """One walk that holds a record in the trophy case (the leaderboard of a discipline).

    MeshTerm keeps records for each ``(category, width_bytes)``. The hash width of each hop
    limits the maximum length of a walk (the transmitted path field is 64 bytes) and its
    chance of a collision. Thus boards at different widths measure different contests.

    Attributes:
        id: The primary key (the handle that a delete takes).
        category: The id of the category in which the walk set the record
            (``grand_tour``, …).
        width_bytes: The path-hash width for each hop, at which the walk was transmitted.
        spec: The transmitted spec: comma-separated hex hashes, in walk order.
        route: The canonical node ids, aligned with the hops of the spec, so that the
            names that MeshTerm shows stay stable (a 1-byte hop in the spec is too
            ambiguous to resolve again later).
        score: The score in the category (the category sets its unit: km, nodes, dB,
            km²).
        stats: The measured statistics of the walk (hop count, distinct nodes, km, …).
        app_version: The MeshTerm version that discovered the walk, for future
            migrations.
        discovered_at: The time at which the walk that set the record came home (UTC).
    """

    id: int
    category: str
    width_bytes: int
    spec: str
    route: tuple[str, ...]
    score: float
    stats: dict
    app_version: str
    discovered_at: datetime


@dataclass(slots=True)
class RunRecord:
    """A summary row from the ``runs`` table.

    Attributes:
        id: The primary key of the run.
        tool: The name of the tool that ran.
        profile: The device profile that the run used, if any.
        status: ``running``, ``ok``, or ``error``.
        started_at: The ISO-8601 timestamp of the start.
        finished_at: The ISO-8601 timestamp of the end, or ``None`` if the run has not
            finished.
        summary: The decoded JSON summary, or ``None``.
    """

    id: int
    tool: str
    profile: str | None
    status: str
    started_at: str
    finished_at: str | None
    summary: dict | None


class Repository:
    """The typed data-access layer on the SQLite database."""

    def __init__(self, db_path: Path) -> None:
        """Open the repository on a database file.

        Args:
            db_path: The location of the SQLite database (created if it does not exist).
        """
        self._conn = db.connect(db_path)

    def close(self) -> None:
        """Close the database connection that the repository uses."""
        self._conn.close()

    # -- runs -------------------------------------------------------------------

    def start_run(self, tool: str, args: dict[str, Any], profile: str | None = None) -> int:
        """Store the start of a tool run.

        Args:
            tool: The name of the tool.
            args: The arguments with which the tool was called (they must be
                JSON-serializable).
            profile: The name of the active device profile, if any.

        Returns:
            The primary key of the new run.
        """
        cur = self._conn.execute(
            "INSERT INTO runs (tool, profile, args_json, status, started_at) "
            "VALUES (?, ?, ?, 'running', ?)",
            (tool, profile, json.dumps(args, default=str), utcnow().isoformat()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, summary: dict | None = None) -> None:
        """Mark a run as finished.

        Args:
            run_id: The run to update.
            status: The final status (``ok`` or ``error``).
            summary: An optional JSON-serializable summary of the result.
        """
        self._conn.execute(
            "UPDATE runs SET status = ?, summary_json = ?, finished_at = ? WHERE id = ?",
            (
                status,
                json.dumps(summary, default=str) if summary is not None else None,
                utcnow().isoformat(),
                run_id,
            ),
        )
        self._conn.commit()

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        """Return the most recent runs, newest first.

        Args:
            limit: The maximum number of rows to return.

        Returns:
            A list of :class:`RunRecord`.
        """
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        """Map a ``runs`` row to a :class:`RunRecord`."""
        return RunRecord(
            id=row["id"],
            tool=row["tool"],
            profile=row["profile"],
            status=row["status"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            summary=json.loads(row["summary_json"]) if row["summary_json"] else None,
        )

    # -- traces -----------------------------------------------------------------

    def record_trace(self, run_id: int, trace: TraceResult) -> int:
        """Store one trace and its SNR readings for each hop.

        Args:
            run_id: The run that owns the trace.
            trace: The trace result to store.

        Returns:
            The primary key of the new trace.
        """
        cur = self._conn.execute(
            "INSERT INTO traces "
            "(run_id, target, success, hop_count, min_snr, round_trip_ms, tx_power, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                trace.target,
                int(trace.success),
                trace.hop_count,
                trace.min_snr,
                trace.round_trip_ms,
                trace.tx_power,
                trace.timestamp.isoformat(),
            ),
        )
        trace_id = int(cur.lastrowid)
        self._conn.executemany(
            "INSERT INTO trace_hops (trace_id, hop_index, node, snr) VALUES (?, ?, ?, ?)",
            [(trace_id, h.index, h.node, h.snr) for h in trace.hops],
        )
        self._conn.commit()
        return trace_id

    def latest_trace(
        self,
        target: str,
        *,
        exclude_run_id: int | None = None,
        success_only: bool = True,
    ) -> TraceResult | None:
        """Return the most recently stored trace to ``target``, rebuilt with its hops.

        MeshTerm uses it to show the result of the previous run before a new trace starts.

        Args:
            target: The trace destination to find.
            exclude_run_id: A run to skip (usually the run in progress), so that the
                result comes from an earlier run.
            success_only: When ``True``, ignore traces that timed out.

        Returns:
            The latest :class:`TraceResult` that matches, or ``None`` if no trace is
            stored.
        """
        sql = "SELECT * FROM traces WHERE target = ?"
        params: list[Any] = [target]
        if success_only:
            sql += " AND success = 1"
        if exclude_run_id is not None:
            sql += " AND run_id != ?"
            params.append(exclude_run_id)
        sql += " ORDER BY id DESC LIMIT 1"
        row = self._conn.execute(sql, params).fetchone()
        if row is None:
            return None

        hop_rows = self._conn.execute(
            "SELECT hop_index, node, snr FROM trace_hops WHERE trace_id = ? ORDER BY hop_index",
            (row["id"],),
        ).fetchall()
        hops = [Hop(index=h["hop_index"], node=h["node"], snr=h["snr"]) for h in hop_rows]
        return TraceResult(
            target=row["target"],
            success=bool(row["success"]),
            hops=hops,
            round_trip_ms=row["round_trip_ms"],
            tx_power=row["tx_power"],
            path_hash_bytes=_hops_hash_bytes(hops),
            timestamp=datetime.fromisoformat(row["created_at"]),
        )

    def recent_traces(self, target: str, *, limit: int = 200) -> list[TraceResult]:
        """Return recent traces to ``target``, newest first, rebuilt with their hops.

        MeshTerm computes the link-quality baseline over this history. Thus the result
        includes the traces that timed out and the traces that succeeded (an increase in
        failures is also a regression).

        Args:
            target: The trace destination to read.
            limit: The maximum number of traces to return.

        Returns:
            The :class:`TraceResult` objects that match, newest first.
        """
        rows = self._conn.execute(
            "SELECT * FROM traces WHERE target = ? ORDER BY id DESC LIMIT ?",
            (target, limit),
        ).fetchall()
        return [self._hydrate_trace(row) for row in rows]

    def _hydrate_trace(self, row: sqlite3.Row) -> TraceResult:
        """Rebuild a :class:`TraceResult` (with hops) from a ``traces`` row.

        Args:
            row: A row of the ``traces`` table.

        Returns:
            The rebuilt trace.
        """
        hop_rows = self._conn.execute(
            "SELECT hop_index, node, snr FROM trace_hops WHERE trace_id = ? ORDER BY hop_index",
            (row["id"],),
        ).fetchall()
        hops = [Hop(index=h["hop_index"], node=h["node"], snr=h["snr"]) for h in hop_rows]
        return TraceResult(
            target=row["target"],
            success=bool(row["success"]),
            hops=hops,
            round_trip_ms=row["round_trip_ms"],
            tx_power=row["tx_power"],
            path_hash_bytes=_hops_hash_bytes(hops),
            timestamp=datetime.fromisoformat(row["created_at"]),
        )

    def traced_targets(self, *, limit: int = 5) -> list[str]:
        """Return recent trace destinations, most recently traced first.

        This list supplies the "Recently traced" section of the target picker. Thus the
        list is short on purpose, and in order of recency. A count of all time only grows,
        and it puts the current work under old names (also the ``--mock`` targets). Walks
        without a target are not included (the walks composed by hand and stored under
        :data:`~meshterm.core.models.PATH_TRACE_TARGET`), because they are not
        destinations that the user can select.

        Args:
            limit: The maximum number of distinct targets to return.

        Returns:
            Distinct target names, most recently traced first.
        """
        rows = self._conn.execute(
            "SELECT target, MAX(id) AS latest FROM traces WHERE target != ? "
            "GROUP BY target ORDER BY latest DESC LIMIT ?",
            (PATH_TRACE_TARGET, limit),
        ).fetchall()
        return [row["target"] for row in rows]

    def target_last_traced(self) -> dict[str, datetime]:
        """For each trace target, the time of its most recent trace (with any result).

        This value supplies the ``TRACED`` column of the Trace-target picker and its
        default sort: how long ago each node was last the target of a trace. Thus the
        picker opens with the most recently traced node at the top. The keys are the raw
        target strings under which the traces were stored (a contact name or a hex hash),
        the same as in :meth:`target_trace_counts`. The caller matches its node against
        these keys. Attempts that timed out count, because you traced the node, also if
        it did not answer. The path walks composed by hand and stored under
        :data:`~meshterm.core.models.PATH_TRACE_TARGET` are not included (they name no
        target).

        Returns:
            ``target → last-traced timestamp`` (aware UTC) for each distinct target that
            is not a path walk.
        """
        rows = self._conn.execute(
            "SELECT target, MAX(created_at) AS latest FROM traces "
            "WHERE target != ? GROUP BY target",
            (PATH_TRACE_TARGET,),
        ).fetchall()
        out: dict[str, datetime] = {}
        for row in rows:
            if row["latest"]:
                out[row["target"]] = datetime.fromisoformat(row["latest"])
        return out

    def target_trace_counts(self) -> dict[str, tuple[int, int]]:
        """For each trace target, its ``(successes, total)`` over all the stored traces.

        This is the base for the observed reliability of a route. The route of a walk is
        not on each trace row, but a target is (also on the attempts that timed out). Thus
        the success rate of a target is the only delivery value that the history can give
        correctly. (A failed trace stores no path, only the target of the trace.) The keys
        are the raw target strings under which the traces were stored (a contact name or a
        hex hash). The caller matches its node against these keys. The path walks
        composed by hand and stored under :data:`~meshterm.core.models.PATH_TRACE_TARGET`
        are not included, because they name no target.

        Returns:
            ``target → (successes, total)`` for each distinct target that is not a path
            walk.
        """
        rows = self._conn.execute(
            "SELECT target, SUM(success) AS ok, COUNT(*) AS n FROM traces "
            "WHERE target != ? GROUP BY target",
            (PATH_TRACE_TARGET,),
        ).fetchall()
        return {row["target"]: (int(row["ok"] or 0), int(row["n"])) for row in rows}

    def trace_paths(self, *, limit: int = 2000) -> list[TracedPath]:
        """Return the walked paths of recent successful traces, newest first.

        This is the trace side of the topology evidence. Each successful trace is a packet
        that is known to have crossed each link in its path (out and back), with an SNR
        reading at each hop. The function returns the hops raw: hex hashes at the width
        that the original command used for them. The topology layer makes them canonical.

        Args:
            limit: The maximum number of traces to read.

        Returns:
            One :class:`TracedPath` for each successful trace that stored hops.
        """
        rows = self._conn.execute(
            "SELECT t.id, t.created_at FROM traces t "
            "WHERE t.success = 1 ORDER BY t.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        paths: list[TracedPath] = []
        for row in rows:
            hop_rows = self._conn.execute(
                "SELECT node, snr FROM trace_hops WHERE trace_id = ? ORDER BY hop_index",
                (row["id"],),
            ).fetchall()
            if not hop_rows:
                continue
            try:
                when = datetime.fromisoformat(row["created_at"])
            except (TypeError, ValueError):
                continue  # a malformed row gives no evidence
            paths.append(TracedPath(when=when, hops=[(h["node"], h["snr"]) for h in hop_rows]))
        return paths

    def packet_paths(self, *, limit: int = 5000) -> list[PacketPath]:
        """Return the relay paths of recent packets from the RX log, newest first.

        This is the passive side of the topology evidence. Each row is a packet that the
        companion overheard, with a header that had the repeater path that the packet
        went through until then. Only observations of the ``packet`` kind have a path
        (refer to :meth:`record_observation`). The function skips rows with a NULL path.
        It returns an empty path (a direct packet) with no hops, so that a known origin
        still gives a direct-link reading.

        Args:
            limit: The maximum number of packet observations to read.

        Returns:
            One :class:`PacketPath` for each stored packet observation.
        """
        rows = self._conn.execute(
            "SELECT node, snr, path, observed_at FROM observations "
            "WHERE kind = 'packet' AND path IS NOT NULL ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        paths: list[PacketPath] = []
        for row in rows:
            try:
                when = datetime.fromisoformat(row["observed_at"])
            except (TypeError, ValueError):
                continue  # a malformed row gives no evidence
            hops = [h for h in (row["path"] or "").split(",") if h]
            paths.append(PacketPath(when=when, origin=row["node"], snr=row["snr"], hops=hops))
        return paths

    def record_neighbours(
        self, run_id: int, repeater: str, neighbours: list[NeighbourInfo]
    ) -> None:
        """Store one snapshot of the neighbour table that MeshTerm got from a remote repeater.

        Each entry becomes a row. When MeshTerm gets the table of the same repeater again
        later, it appends a new snapshot and does not overwrite the old one.
        :meth:`neighbour_links` reads back only the latest row for each
        ``(repeater, neighbour)`` pair. A neighbour table is the current state of the
        repeater, so a new snapshot replaces the old one, and the two do not add up as
        more evidence.

        Args:
            run_id: The run that owns the snapshot.
            repeater: The canonical id of the repeater that sent the table.
            neighbours: The entries that MeshTerm got (this list can be empty: then no
                rows are stored).
        """
        fetched_at = utcnow().isoformat()
        self._conn.executemany(
            "INSERT INTO neighbour_reports "
            "(run_id, repeater, neighbour, snr, heard_at, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    repeater,
                    n.node,
                    n.snr,
                    n.heard_at.isoformat() if n.heard_at is not None else None,
                    fetched_at,
                )
                for n in neighbours
            ],
        )
        self._conn.commit()

    def neighbour_links(self, *, limit: int = 2000) -> list[NeighbourLink]:
        """Return the current links that repeaters reported, newest report first.

        This is the side of the topology evidence that MeshTerm gets from repeaters. The
        function returns only the most recent row for each ``(repeater, neighbour)`` pair
        (refer to :meth:`record_neighbours`). Thus, when MeshTerm gets a table again and
        again, the sample count of a link does not increase.

        Args:
            limit: The maximum number of links to read.

        Returns:
            One :class:`NeighbourLink` for each link that is reported now.
        """
        rows = self._conn.execute(
            "SELECT r.repeater, r.neighbour, r.snr, r.heard_at, r.fetched_at "
            "FROM neighbour_reports r "
            "JOIN (SELECT MAX(id) AS id FROM neighbour_reports "
            "      GROUP BY repeater, neighbour) latest ON latest.id = r.id "
            "ORDER BY r.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        links: list[NeighbourLink] = []
        for row in rows:
            when: datetime | None = None
            for stamp in (row["heard_at"], row["fetched_at"]):
                try:
                    when = datetime.fromisoformat(stamp)
                    break
                except (TypeError, ValueError):
                    continue
            if when is None:
                continue  # a malformed row gives no evidence
            links.append(
                NeighbourLink(
                    when=when,
                    repeater=row["repeater"],
                    neighbour=row["neighbour"],
                    snr=row["snr"],
                )
            )
        return links

    def record_tx_sample(self, run_id: int, level: TxLevelResult) -> None:
        """Store one robust TX-power level from an optimization sweep.

        The ``median_min_snr`` column stores the main metric of this optimizer: the
        median SNR measured at the target, instead of a path bottleneck.

        Args:
            run_id: The run that owns the sample.
            level: The aggregated result for one TX power level.
        """
        self._conn.execute(
            "INSERT INTO tx_samples "
            "(run_id, target, tx_power, median_min_snr, success_rate, samples, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                level.stats.target,
                level.tx_power,
                level.target_snr,
                level.success_rate,
                level.samples,
                utcnow().isoformat(),
            ),
        )
        self._conn.commit()

    def record_path_candidate(
        self,
        run_id: int,
        target: str,
        path: str,
        *,
        bottleneck_snr: float | None,
        success_rate: float,
        median_rtt_ms: float | None,
    ) -> None:
        """Store one measured candidate path from a path-probe sweep.

        Args:
            run_id: The probe run that owns the candidate.
            target: The node to which the candidate paths go.
            path: The forced outbound path that was measured (comma-separated hex
                hashes).
            bottleneck_snr: The median of the bottleneck SNRs (dB) of the traces on the
                candidate, or ``None`` when no trace on it succeeded.
            success_rate: The fraction of traces on this path that replied, ``[0, 1]``.
            median_rtt_ms: The median round-trip time on this path, if it was measured.
        """
        self._conn.execute(
            "INSERT INTO path_candidates "
            "(run_id, target, path_json, bottleneck_snr, success_rate, median_rtt_ms, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                target,
                json.dumps(path.split(",")),
                bottleneck_snr,
                success_rate,
                median_rtt_ms,
                utcnow().isoformat(),
            ),
        )
        self._conn.commit()

    # -- discovered paths (trophy-case records) ----------------------------------

    def record_discovery(
        self,
        category: str,
        width_bytes: int,
        spec: str,
        route: tuple[str, ...] | list[str],
        *,
        score: float,
        stats: dict,
        app_version: str,
        keep: int = 5,
        ascending: bool = False,
    ) -> int | None:
        """Offer one walk to a category leaderboard. Store it only if it gets a place.

        The leaderboard invariant is here, so that all the callers share it:

        * A walk goes on the ``(category, width_bytes)`` board when it beats the current
          entries (or when the board is not full).
        * An identical spec keeps only its best score (a new walk on a known route never
          makes a second row).
        * Before the function returns, it prunes the board to ``keep`` rows.

        Args:
            category: The id of the category to which the walk is offered.
            width_bytes: The hash width for each hop, at which the walk was transmitted.
            spec: The transmitted spec (comma-separated hex hashes).
            route: The canonical node ids, aligned with the hops of the spec.
            score: The score of this walk in the category.
            stats: The JSON-serializable statistics of the walk.
            app_version: The MeshTerm version that runs now, stamped on the row.
            keep: The board size (the rows kept for each category and width).
            ascending: ``True`` for categories in which lower scores win
                (Thin thread looks for the weakest link that survives).

        Returns:
            The id of the stored row when the walk got a place (a new row, or a better
            score on a known route), or ``None`` when the walk did not get on the board.
        """

        def beats(challenger: float, standing: float) -> bool:
            return challenger < standing if ascending else challenger > standing

        existing = self._conn.execute(
            "SELECT id, score FROM discovered_paths "
            "WHERE category = ? AND width_bytes = ? AND spec = ?",
            (category, width_bytes, spec),
        ).fetchone()
        if existing is not None:
            # A known route: keep the row (and its discovery date), unless this walk beat
            # the record of the route.
            if not beats(score, float(existing["score"])):
                return None
            self._conn.execute(
                "UPDATE discovered_paths SET score = ?, stats_json = ?, "
                "app_version = ?, discovered_at = ? WHERE id = ?",
                (
                    score,
                    json.dumps(stats),
                    app_version,
                    utcnow().isoformat(),
                    existing["id"],
                ),
            )
            self._conn.commit()
            return int(existing["id"])
        cursor = self._conn.execute(
            "INSERT INTO discovered_paths "
            "(category, width_bytes, spec, route_json, score, stats_json, "
            "app_version, discovered_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                category,
                width_bytes,
                spec,
                json.dumps(list(route)),
                score,
                json.dumps(stats),
                app_version,
                utcnow().isoformat(),
            ),
        )
        new_id = int(cursor.lastrowid)
        # Prune the board to ``keep`` rows. The worst scores go. Among equal scores, the
        # oldest row stays, so the walk that set a mark keeps it against a later walk with
        # the same score.
        order = "ASC" if not ascending else "DESC"  # worst-first for deletion
        overflow = self._conn.execute(
            "SELECT id FROM discovered_paths WHERE category = ? AND width_bytes = ? "
            f"ORDER BY score {order}, id DESC",
            (category, width_bytes),
        ).fetchall()
        doomed = [row["id"] for row in overflow[: max(0, len(overflow) - keep)]]
        if doomed:
            self._conn.executemany(
                "DELETE FROM discovered_paths WHERE id = ?", [(i,) for i in doomed]
            )
        self._conn.commit()
        return None if new_id in doomed else new_id

    def discoveries(
        self, category: str | None = None, *, width_bytes: int | None = None
    ) -> list[DiscoveredPath]:
        """Return the stored records of the trophy case, with optional filters.

        The rows are not ranked (they are grouped by category, newest first in each
        category). The service layer owns the score direction of each category, and it
        sorts the rows before they are shown.

        Args:
            category: Only the records of this category, or ``None`` for all.
            width_bytes: Only the records at this hash width, or ``None`` for all.

        Returns:
            The :class:`DiscoveredPath` rows that match.
        """
        clauses, params = ["1=1"], []
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if width_bytes is not None:
            clauses.append("width_bytes = ?")
            params.append(width_bytes)
        rows = self._conn.execute(
            "SELECT * FROM discovered_paths WHERE "
            + " AND ".join(clauses)
            + " ORDER BY category, id DESC",
            params,
        ).fetchall()
        out: list[DiscoveredPath] = []
        for row in rows:
            try:
                when = datetime.fromisoformat(row["discovered_at"])
            except (TypeError, ValueError):
                when = utcnow()
            try:
                route = tuple(json.loads(row["route_json"]))
            except (TypeError, ValueError):
                route = ()
            try:
                stats = json.loads(row["stats_json"]) or {}
            except (TypeError, ValueError):
                stats = {}
            out.append(
                DiscoveredPath(
                    id=int(row["id"]),
                    category=row["category"],
                    width_bytes=int(row["width_bytes"]),
                    spec=row["spec"],
                    route=route,
                    score=float(row["score"]),
                    stats=stats,
                    app_version=row["app_version"],
                    discovered_at=when,
                )
            )
        return out

    def delete_discovery(self, discovery_id: int) -> bool:
        """Delete one record of the trophy case by id. Return ``True`` when a row was deleted."""
        cursor = self._conn.execute("DELETE FROM discovered_paths WHERE id = ?", (discovery_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_discoveries(
        self, category: str | None = None, *, width_bytes: int | None = None
    ) -> int:
        """Delete records of the trophy case in bulk, with optional filters. Return the count.

        Args:
            category: Only the records of this category, or ``None`` for all categories.
            width_bytes: Only the records at this hash width, or ``None`` for all widths.

        Returns:
            The number of deleted records.
        """
        clauses, params = ["1=1"], []
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if width_bytes is not None:
            clauses.append("width_bytes = ?")
            params.append(width_bytes)
        cursor = self._conn.execute(
            "DELETE FROM discovered_paths WHERE " + " AND ".join(clauses), params
        )
        self._conn.commit()
        return cursor.rowcount

    # -- observations (passive monitoring) --------------------------------------

    def record_observation(self, run_id: int, obs: Observation) -> None:
        """Store one overheard packet from a monitor run.

        Observations of the ``packet`` kind (the RX packet log of the companion) also have
        the relay path that the packet went through. These paths are the raw material of
        the topology graph (refer to :meth:`packet_paths`).

        An overheard channel packet also keeps the three fields from which the packet
        viewer decrypts: the channel-hash fingerprint, the 2-byte MAC, and the ciphertext.
        The function takes them out of the raw payload for two reasons. A channel for
        which we have the key stays readable when the feed later gets its first rows from
        stored history. Also, MeshTerm can still name a channel datagram (which the library
        never parses) by the key that its MAC confirms.

        The addresses of the packet are kept in the same way, for the same reason: the
        hash of the recipient's key, the sender's hash (or the full key that an anonymous
        request carries), and the token of a class that has one (the checksum of an ack,
        the tag of a trace). MeshTerm decodes all three from the packet body
        (:mod:`~meshterm.core.frames`), and only a live event has the body. Without these
        columns, a replayed row loses all that it can tell about itself.

        Args:
            run_id: The run that owns the observation.
            obs: The observation to store.
        """
        chan_hash = cipher_mac = crypted = None
        raw = obs.raw if isinstance(obs.raw, dict) else {}
        # The payload class identifies a relayed 'packet' that names no origin node. Keep
        # it for each packet, not only for the classes to which the fields below apply.
        typename = raw.get("payload_typename") if obs.kind == "packet" else None
        if typename in CHANNEL_CLASSES:
            chan_hash = raw.get("chan_hash")
            cipher_mac = raw.get("cipher_mac")
            crypted = raw.get("crypted")
        elif typename in ADDRESSED_CLASSES:
            # The MAC of an addressed packet, in the same column that a channel packet
            # uses. It is a tag on the encrypted body, so copies of one message have the
            # same MAC, and the next message has a different MAC. The ciphertext is not
            # kept, because we can never read it. Only the fingerprint is necessary to tell
            # the packets of one message from the packets of another message (refer to
            # :mod:`~meshterm.services.message_paths`).
            cipher_mac = raw.get("cipher_mac")
        # One column for each of the three forms of address. The sender is a hash or a
        # full key (its length tells which). The token is the checksum of an ack or the
        # tag of a trace (the payload class tells which). Thus no second column is
        # necessary for either value.
        dest = raw.get("dest_hash") if typename else None
        src = (raw.get("src_key") or raw.get("src_hash")) if typename else None
        tag = (raw.get("ack_crc") or raw.get("trace_tag")) if typename else None
        # The readings of a trace for each hop. They are in the path field of its header,
        # where each other class keeps relay hashes. Thus they get their own column,
        # instead of `path`.
        readings = raw.get("trace_snrs") if typename == "TRACE" else None
        trace_snrs = ",".join(f"{v:g}" for v in readings) if readings else None
        # What the `path` of the packet means (refer to the v15 migration). Only a packet
        # row has one.
        route = raw.get("route_typename") if obs.kind == "packet" else None
        # The transport code of a scoped packet and, for a scoped flood, the bytes from
        # which it was computed. Thus its region can resolve against a name that MeshTerm
        # learns later (refer to the v16 migration). Nothing is kept for a packet that has
        # no code.
        transport_code = raw.get("transport_code") if route in SCOPED_ROUTES else None
        body = raw_scope_body(raw) if route == "TC_FLOOD" and transport_code else None
        scope_body = body.hex() if body is not None else None
        self._conn.execute(
            "INSERT INTO observations "
            "(run_id, node, public_key, name, kind, node_type, snr, rssi, lat, lon, path, "
            "observed_at, chan_hash, cipher_mac, crypted, payload_typename, dest, src, tag, "
            "trace_snrs, route, transport_code, scope_body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                obs.node,
                obs.public_key,
                obs.name,
                obs.kind,
                obs.node_type,
                obs.snr,
                obs.rssi,
                obs.lat,
                obs.lon,
                obs.path,
                obs.observed_at.isoformat(),
                chan_hash,
                cipher_mac,
                crypted,
                typename,
                dest,
                src,
                tag,
                trace_snrs,
                route,
                transport_code,
                scope_body,
            ),
        )
        self._conn.commit()

    def observation_count(self) -> int:
        """Return the total number of overheard packets stored in all the runs.

        The "total" counter of the monitor uses this value. Thus the counter counts all
        the observations ever stored, not only those of the current session.

        Returns:
            The row count of the ``observations`` table.
        """
        row = self._conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()
        return int(row["n"]) if row else 0

    def table_counts(self) -> dict[str, int]:
        """Return a row count for each table in the database, by name.

        The table names come from ``sqlite_master``, not from a list written here. This is
        the purpose of the function: a table added to the schema is in the report from the
        day that it is added, and a removed table goes out of the report. Nothing must be
        kept in step by hand. This function must never become a selected set of the counts
        that somebody thought interesting. The shape of a database is the aggregate, and
        the interesting table is always the table that nobody expected to be full.

        Each value is a count. Nothing here reads a row. Thus the result tells how much
        history the author of a report has, but it tells nothing about the persons with
        whom the author communicates.

        Returns:
            Table name to row count, in alphabetical order. The internal tables of SQLite
            are not included (they describe the file format, not the data of this app).
        """
        names = [
            str(row["name"])
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        counts: dict[str, int] = {}
        for name in names:
            # The names come from sqlite_master, so they are the tables of this database
            # and not input from the caller. The query quotes them all the same, because
            # an identifier cannot be bound.
            row = self._conn.execute(f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()
            counts[name] = int(row["n"]) if row else 0
        return counts

    def failed_run_count(self) -> int:
        """Return the number of stored tool runs that ended with an error.

        This is the most useful number that a person who reports a problem can give. It
        tells if this installation has failed silently for weeks, or failed for the first
        time today. It costs one query, instead of many questions to the person.

        Returns:
            The number of ``runs`` rows with the status ``error``.
        """
        row = self._conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'error'").fetchone()
        return int(row["n"]) if row else 0

    def observation_span(self) -> tuple[datetime | None, datetime | None]:
        """Return the first and the last time that something was overheard.

        This span tells how much of the mesh this database heard. A report with a span of
        one hour and a report with a span of one year describe different installations.
        The difference explains a class of "it works here" problems before somebody must
        ask for it.

        Returns:
            ``(first, last)`` as aware UTC times, or ``(None, None)`` when nothing was
            ever heard.
        """
        row = self._conn.execute(
            "SELECT MIN(observed_at) AS first, MAX(observed_at) AS last FROM observations"
        ).fetchone()
        if row is None or row["first"] is None:
            return None, None
        return _as_when(row["first"]), _as_when(row["last"])

    def recent_observations(self, *, since: datetime, limit: int = 4000) -> list[Observation]:
        """Return the raw stored observations in a recent time window, oldest first.

        This is the initial data of the dashboard: all that MeshTerm overheard in the
        window (adverts, telemetry, and ``packet`` rows from the RX log). The function
        rebuilds them as :class:`~meshterm.core.models.Observation` values. Thus the live
        screen can use stored history and new hub events in the same way. The timestamps
        compare as strings, because ``datetime.isoformat`` writes each ``observed_at`` in
        UTC. :meth:`channel_stats` uses the same method.

        Args:
            since: Only the observations at or after this time.
            limit: A fixed maximum of rows (the newest stay), so that a very busy window
                stays fast.

        Returns:
            The observations of the window, oldest first (ready for the caller to append
            live events).
        """
        rows = self._conn.execute(
            "SELECT node, name, kind, node_type, snr, rssi, lat, lon, path, observed_at, "
            "chan_hash, cipher_mac, crypted, payload_typename, dest, src, tag, trace_snrs, route, "
            "transport_code, scope_body "
            "FROM observations WHERE observed_at >= ? ORDER BY observed_at DESC LIMIT ?",
            (since.isoformat(), limit),
        ).fetchall()
        return self._hydrate_observations(reversed(rows))

    def packet_frames_between(
        self, start: datetime, end: datetime, *, limit: int = 2000
    ) -> list[Observation]:
        """The ``packet`` rows from the RX log in a closed time window, oldest first.

        This is the data for the message paths dialog: each relayed packet that the radio
        reported in the window around a chat message, with the raw crypto fields. Thus
        MeshTerm can decrypt a channel packet and match it to the message, and it can
        correlate a direct packet by time. The timestamps compare as ISO strings, as in
        each observation query.

        Args:
            start: The inclusive start of the window.
            end: The inclusive end of the window.
            limit: A fixed maximum of rows (the oldest stay, because the window is
                centred on the message).

        Returns:
            The ``packet`` observations of the window, oldest first.
        """
        rows = self._conn.execute(
            "SELECT node, name, kind, node_type, snr, rssi, lat, lon, path, observed_at, "
            "chan_hash, cipher_mac, crypted, payload_typename, dest, src, tag, trace_snrs, route, "
            "transport_code, scope_body "
            "FROM observations WHERE kind = 'packet' AND observed_at >= ? "
            "AND observed_at <= ? ORDER BY observed_at ASC LIMIT ?",
            (start.isoformat(), end.isoformat(), limit),
        ).fetchall()
        return self._hydrate_observations(rows)

    def _hydrate_observations(self, rows) -> list[Observation]:  # noqa: ANN001 - sqlite rows
        """Rebuild :class:`Observation` values from full observation rows, in order."""
        observations: list[Observation] = []
        for row in rows:
            try:
                observed_at = datetime.fromisoformat(row["observed_at"])
            except (TypeError, ValueError):
                continue  # a malformed row does not go into the window
            observations.append(
                Observation(
                    node=row["node"],
                    name=row["name"],
                    kind=row["kind"] or "advert",
                    node_type=row["node_type"],
                    snr=row["snr"],
                    rssi=row["rssi"],
                    lat=row["lat"],
                    lon=row["lon"],
                    path=row["path"],
                    observed_at=observed_at,
                    raw=_packet_raw(row),
                )
            )
        return observations

    def node_observations(
        self, node: str, *, since: datetime | None = None, limit: int = 50000
    ) -> list[Observation]:
        """All the stored receptions of one node, oldest first: its record over time.

        This is the data of the Time Machine for one node. Rows of the ``packet`` kind
        are not included, for the same reason that :meth:`heard_nodes` removes them:
        their SNR describes the last relay, not the node. Thus they make a reception
        timeline wrong.

        Args:
            node: The stored id of the node (the key prefix of 12 hex digits that
                observations have).
            since: Only the observations at or after this time, if given.
            limit: A fixed maximum of rows (the newest stay), so that a very old node
                with much traffic stays fast.

        Returns:
            The observations of the node, oldest first.
        """
        sql = (
            "SELECT node, name, kind, node_type, snr, rssi, lat, lon, path, observed_at "
            "FROM observations WHERE node = ? AND kind != 'packet'"
        )
        params: list[Any] = [node]
        if since is not None:
            sql += " AND observed_at >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        observations: list[Observation] = []
        for row in reversed(rows):
            try:
                observed_at = datetime.fromisoformat(row["observed_at"])
            except (TypeError, ValueError):
                continue  # a malformed row does not go into the record
            observations.append(
                Observation(
                    node=row["node"],
                    name=row["name"],
                    kind=row["kind"] or "advert",
                    node_type=row["node_type"],
                    snr=row["snr"],
                    rssi=row["rssi"],
                    lat=row["lat"],
                    lon=row["lon"],
                    path=row["path"],
                    observed_at=observed_at,
                )
            )
        return observations

    def daily_activity(self) -> list[tuple[str, int, int]]:
        """The activity totals for each day in all the stored history, oldest first.

        The days are **local** calendar days. ``observed_at`` is stored as UTC ISO-8601,
        but the SQLite ``datetime(…, 'localtime')`` converts each timestamp into the time
        zone of the machine before the day prefix is sliced. (It converts each instant
        separately, so DST is correct.) Thus a bar breaks at local midnight, not at the
        hour of the UTC offset. If you read the same database in a different time zone,
        the days go into new buckets for your zone. This is the purpose: MeshTerm shows
        history in the local time of the user.

        Returns:
            ``(day, packets, nodes)`` for each day that has activity: the day as a local
            ``YYYY-MM-DD``, the count of all the stored observations, and the count of
            the distinct identified nodes heard. (The ``packet`` rows are not included,
            because they have no reliable identity.)
        """
        rows = self._conn.execute(
            "SELECT substr(datetime(observed_at, 'localtime'), 1, 10) AS day, "
            "COUNT(*) AS pkts, "
            "COUNT(DISTINCT CASE WHEN kind != 'packet' THEN node END) AS nodes "
            "FROM observations GROUP BY day ORDER BY day"
        ).fetchall()
        return [(row["day"], int(row["pkts"]), int(row["nodes"])) for row in rows]

    def hourly_series(self, since: datetime) -> list[tuple[str, int, int]]:
        """Activity totals for each clock hour since a time, oldest first (for the 24 h window).

        This is the version of :meth:`daily_activity` with a resolution of one hour: the
        same counts of packets and distinct nodes, grouped by hour. ``observed_at`` is
        stored as UTC. Thus the SQLite ``datetime(…, 'localtime')`` converts each
        timestamp into the time zone of the machine before the ``YYYY-MM-DDTHH`` key is
        sliced. The buckets break at local hour boundaries in each time zone (also in
        zones with a half-hour offset), not at UTC boundaries. One day of history becomes
        approximately 24 rows, however much traffic it has. Only the hours with traffic
        are returned. The caller fills in the quiet hours (refer to
        :func:`~meshterm.ui.timemachine_screen._fill_hours`), so that the axis is real
        clock time. The ``since`` filter stays on the raw UTC column, because the window
        is an absolute span. Only the labels are local.

        Args:
            since: Only the observations at or after this time.

        Returns:
            ``(hour, packets, nodes)`` for each active hour, oldest first: ``hour`` as a
            local ``YYYY-MM-DDTHH``, the count of all the observations, and the count of
            the distinct identified nodes heard. (The node count does not include the
            ``packet`` rows, because they have no reliable identity.)
        """
        rows = self._conn.execute(
            "SELECT replace(substr(datetime(observed_at, 'localtime'), 1, 13), ' ', 'T') "
            "AS hour, COUNT(*) AS pkts, "
            "COUNT(DISTINCT CASE WHEN kind != 'packet' THEN node END) AS nodes "
            "FROM observations WHERE observed_at >= ? GROUP BY hour ORDER BY hour",
            (since.isoformat(),),
        ).fetchall()
        return [(row["hour"], int(row["pkts"]), int(row["nodes"])) for row in rows]

    def hourly_activity(self, *, since: datetime | None = None) -> list[int]:
        """The observation counts by local hour of the day (0 to 23) in the stored history.

        This is the data of the Rhythm chart for the whole mesh: each stored observation,
        counted in the hour of the day in which it arrived. ``observed_at`` is stored as
        UTC. Thus the SQLite ``datetime(…, 'localtime')`` converts each timestamp into the
        time zone of the machine before the ``HH`` slice. (It converts each instant
        separately, so DST is correct.) The result is a histogram that is already in local
        hours, and the caller does not have to convert it. The cost stays at 24 rows,
        however long the history becomes.

        Args:
            since: Only the observations at or after this time, if given.

        Returns:
            24 counts, index = local hour.
        """
        sql = (
            "SELECT substr(datetime(observed_at, 'localtime'), 12, 2) AS hh, "
            "COUNT(*) AS n FROM observations"
        )
        params: list[Any] = []
        if since is not None:
            sql += " WHERE observed_at >= ?"
            params.append(since.isoformat())
        sql += " GROUP BY hh"
        counts = [0] * 24
        for row in self._conn.execute(sql, params).fetchall():
            try:
                counts[int(row["hh"])] += int(row["n"])
            except (TypeError, ValueError, IndexError):
                continue  # a malformed timestamp is not counted
        return counts

    def rhythm_activity(self, *, since: datetime | None = None) -> list[int]:
        """The observation counts by local minute of the day (0 to 1439) in the history.

        This is the base grid of the Rhythm chart for the whole mesh. A minute divides each
        slice width that the chart can use (1, 5, 10, 15, 20, 30, or 60 minutes: refer to
        the slice ladder of the Time Machine). Thus the client can fold one scan here into
        any of these widths, and it does not have to query again. The slot index is
        ``HH * 60 + MM``. SQL computes it from the ``HH`` and ``MM`` substrings of
        ``datetime(…, 'localtime')``. (``observed_at`` is stored as UTC, and each instant is
        converted into the time zone of the machine before the slice, so DST is correct.)
        Thus the histogram is in local time, and the caller does not have to convert it.
        The cost stays at a maximum of 1440 rows, however long the history becomes.

        Args:
            since: Only the observations at or after this time, if given.

        Returns:
            1440 counts, index = local minute of the day.
        """
        sql = (
            "SELECT CAST(substr(datetime(observed_at, 'localtime'), 12, 2) AS INTEGER) "
            "* 60 + CAST(substr(datetime(observed_at, 'localtime'), 15, 2) AS INTEGER) "
            "AS slot, COUNT(*) AS n FROM observations"
        )
        params: list[Any] = []
        if since is not None:
            sql += " WHERE observed_at >= ?"
            params.append(since.isoformat())
        sql += " GROUP BY slot"
        counts = [0] * 1440
        for row in self._conn.execute(sql, params).fetchall():
            try:
                counts[int(row["slot"])] += int(row["n"])
            except (TypeError, ValueError, IndexError):
                continue  # a malformed timestamp is not counted
        return counts

    def flood_frames(self, *, since: datetime | None = None) -> list[tuple[datetime, dict]]:
        """All the stored flood packets, oldest first, with the data that gives their scope.

        This is the data for the scope filters of the Time Machine. To name the region of a
        scoped packet, MeshTerm computes its transport code again with the key of each
        known region. SQL cannot do this. Thus the function returns the packets as the raw
        mapping that :meth:`~meshterm.core.region_store.RegionStore.scope_of` reads (the
        route, the transport code, and the bytes from which the code was computed), and
        the caller resolves them. Only floods are returned: a direct packet has no scope,
        and a packet that was stored before MeshTerm kept its route cannot tell which type
        it was.

        Args:
            since: Only the packets at or after this time, if given.

        Returns:
            ``(observed_at, raw)`` for each flood packet, oldest first.
        """
        sql = (
            "SELECT observed_at, route, transport_code, scope_body FROM observations "
            "WHERE kind = 'packet' AND upper(route) IN ('FLOOD', 'TC_FLOOD')"
        )
        params: list[Any] = []
        if since is not None:
            sql += " AND observed_at >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY observed_at"
        frames: list[tuple[datetime, dict]] = []
        for row in self._conn.execute(sql, params):
            when = _as_when(row["observed_at"])
            if when is None:
                continue  # a malformed row is not counted
            raw = {"route_typename": row["route"]}
            if row["transport_code"]:
                raw["transport_code"] = row["transport_code"]
            if row["scope_body"]:
                raw["scope_body"] = row["scope_body"]
            frames.append((when, raw))
        return frames

    def self_transmissions(self, *, since: datetime | None = None) -> list[datetime]:
        """The timestamps of all that our node transmitted, oldest first.

        This is the counterpart of :meth:`node_observations` for our node. Our node is
        never in the reception history, because we do not overhear ourselves. Thus its
        volume and rhythm in the Time Machine come from what we sent. Each trace that we
        started and each message that we sent go into one transmission timeline (a SQL
        union). Both tables stamp their rows in UTC ISO-8601 (``created_at``), the same
        form as the observations. Thus the timeline goes directly into the bucketing that
        the charts of the node page use.

        Args:
            since: Only the transmissions at or after this time, if given.

        Returns:
            The transmission timestamps (trace starts and sent messages), oldest first.
        """
        sql = "SELECT created_at FROM traces"
        params: list[Any] = []
        if since is not None:
            sql += " WHERE created_at >= ?"
            params.append(since.isoformat())
        sql += " UNION ALL SELECT created_at FROM messages WHERE outbound = 1"
        if since is not None:
            sql += " AND created_at >= ?"
            params.append(since.isoformat())
        stamps: list[datetime] = []
        for row in self._conn.execute(sql, params).fetchall():
            try:
                stamps.append(datetime.fromisoformat(row["created_at"]))
            except (TypeError, ValueError):
                continue  # a malformed timestamp is not charted
        stamps.sort()
        return stamps

    def self_trace_reach(
        self, *, since: datetime | None = None
    ) -> list[tuple[datetime, bool, float | None, int | None]]:
        """The reach of each trace, oldest first: ``(when, came_home, min_snr, hop_count)``.

        This is the measurement for the Reach section of the page of our node. Each trace
        row is one probe of how far we get out: if the trace came home, the bottleneck SNR
        of the path that it walked (``min_snr``: the weakest link of the reach), and how
        many hops it crossed. The attempts that timed out are kept, because a series of
        failures also tells about the reach. But they have no SNR (nothing came back to
        measure). Thus the SNR band draws only the attempts that came home.

        Args:
            since: Only the traces at or after this time, if given.

        Returns:
            ``(created_at, success, min_snr, hop_count)`` for each trace, oldest first.
        """
        sql = "SELECT created_at, success, min_snr, hop_count FROM traces"
        params: list[Any] = []
        if since is not None:
            sql += " WHERE created_at >= ?"
            params.append(since.isoformat())
        sql += " ORDER BY created_at"
        out: list[tuple[datetime, bool, float | None, int | None]] = []
        for row in self._conn.execute(sql, params).fetchall():
            try:
                when = datetime.fromisoformat(row["created_at"])
            except (TypeError, ValueError):
                continue  # a malformed timestamp is not charted
            out.append((when, bool(row["success"]), row["min_snr"], row["hop_count"]))
        return out

    def self_activity_ledger(self, *, since: datetime | None = None) -> SelfActivity:
        """The totals of the outbound activity of our node (refer to :class:`SelfActivity`).

        The function does one aggregate pass on each of the traces, messages, and
        tx-sample tables. The result is each count that the Ledger line of the page of our
        node shows, filtered to the window. The path walks composed by hand (stored under
        :data:`PATH_TRACE_TARGET`) still count in the traces that we started. But they are
        not in the count of distinct targets, because they have no target.

        Args:
            since: Only the activity at or after this time, if given.

        Returns:
            The populated :class:`SelfActivity`.
        """
        window = ""
        param: list[Any] = []
        if since is not None:
            window = " AND created_at >= ?"
            param = [since.isoformat()]

        traces = self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(success), 0) AS ok, "
            "COUNT(DISTINCT CASE WHEN target != ? THEN target END) AS targets "
            "FROM traces WHERE 1 = 1" + window,
            [PATH_TRACE_TARGET, *param],
        ).fetchone()

        msgs = self._conn.execute(
            "SELECT "
            "COALESCE(SUM(is_channel), 0) AS chan, "
            "COALESCE(SUM(1 - is_channel), 0) AS dm, "
            "COALESCE(SUM(CASE WHEN is_channel = 0 AND acked = 1 THEN 1 END), 0) AS acked, "
            "COALESCE(SUM(CASE WHEN is_channel = 0 AND acked IS NOT NULL THEN 1 END), 0) "
            "  AS ackable, "
            "COUNT(DISTINCT CASE WHEN is_channel = 0 THEN peer END) AS peers "
            "FROM messages WHERE outbound = 1" + window,
            list(param),
        ).fetchone()

        tx = self._conn.execute(
            "SELECT COUNT(*) AS n FROM tx_samples WHERE 1 = 1" + window, list(param)
        ).fetchone()

        return SelfActivity(
            trace_total=int(traces["n"]),
            trace_ok=int(traces["ok"]),
            trace_targets=int(traces["targets"]),
            msg_channel=int(msgs["chan"]),
            msg_dm=int(msgs["dm"]),
            dm_acked=int(msgs["acked"]),
            dm_ackable=int(msgs["ackable"]),
            dm_peers=int(msgs["peers"]),
            tx_samples=int(tx["n"]),
        )

    def first_seen(
        self, *, since: datetime | None = None
    ) -> list[tuple[str, str | None, datetime]]:
        """The time at which each node first appeared in the history, newest arrivals first.

        This is the data for the "new arrivals" list of the Time Machine. One grouped scan
        gives the earliest observation of each node, with the most recent name that the
        node advertised as its label (through :meth:`node_names`). The ``packet`` rows are
        not included, because they have no reliable identity.

        Args:
            since: Only the nodes whose first appearance is at or after this time.

        Returns:
            ``(node, latest_name, first_heard)`` triples, most recent arrival first.
        """
        # One streaming pass, not sorted. ``observed_at`` is UTC ISO-8601 and compares
        # correctly as a string. Thus a string comparison finds the earliest stamp and the
        # latest name of each node, and the function parses only approximately one stamp
        # (the stamp that wins) for each node. Before, this function sorted all the
        # history and walked it one row object at a time.
        firsts: dict[str, str] = {}
        names: dict[str, tuple[str, str]] = {}
        for row in self._conn.execute(
            "SELECT node, name, observed_at FROM observations "
            "WHERE node IS NOT NULL AND kind != 'packet'"
        ):
            node, iso = row["node"], row["observed_at"]
            earliest = firsts.get(node)
            if earliest is None or iso < earliest:
                firsts[node] = iso
            if row["name"]:
                named = names.get(node)
                if named is None or iso >= named[0]:
                    names[node] = (iso, row["name"])
        arrivals: list[tuple[str, str | None, datetime]] = []
        for node, iso in firsts.items():
            try:
                first = datetime.fromisoformat(iso)
            except (TypeError, ValueError):
                continue
            if since is None or first >= since:
                named = names.get(node)
                arrivals.append((node, named[1] if named else None, first))
        arrivals.sort(key=lambda t: t[2], reverse=True)
        return arrivals

    def kind_counts(self, *, since: datetime | None = None) -> dict[str, int]:
        """The counts of the stored observations by packet class, with an optional window.

        This is the stored initial data of the traffic panel of the dashboard: what the
        recorder heard in all sessions, by kind. Thus the panel opens with data, and does
        not count from zero at each start. A raw ``packet`` row that has a parsed payload
        class goes into the bucket ``packet:<TYPENAME>`` (``packet:GRP_TXT``,
        ``packet:TRACE``, …), so that the panel can name what the packets were. Only a
        packet without a class stays a bare ``packet``. Messages and acks are different
        event families (not observations), and the panel counts them live in addition to
        these counts.

        Args:
            since: Only the observations at or after this time, if given.

        Returns:
            ``bucket → count`` for each packet class ever stored (in the window).
        """
        sql = (
            "SELECT CASE WHEN kind = 'packet' AND payload_typename IS NOT NULL "
            "THEN 'packet:' || payload_typename ELSE kind END AS bucket, "
            "COUNT(*) AS n FROM observations"
        )
        params: list[Any] = []
        if since is not None:
            sql += " WHERE observed_at >= ?"
            params.append(since.isoformat())
        sql += " GROUP BY bucket"
        rows = self._conn.execute(sql, params).fetchall()
        return {row["bucket"]: row["n"] for row in rows}

    def prune_observations(self, older_than: datetime) -> int:
        """Delete the observations that are older than the retention window (housekeeping).

        This sweep runs one time in each session. It keeps a limit on the size of a
        database that stores data all the time. All the data that the dashboard and the
        Time Machine read is in the retention window, so the rows outside it have no use.
        The timestamps compare as strings, as in each other ``observed_at`` filter here.

        Args:
            older_than: The observations before this time (not at this time) are
                deleted.

        Returns:
            The number of deleted rows (0 when no rows are older than the window).
        """
        cur = self._conn.execute(
            "DELETE FROM observations WHERE observed_at < ?", (older_than.isoformat(),)
        )
        self._conn.commit()
        return cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0

    def node_names(self) -> dict[str, str]:
        """The most recent advertised name of each node, in all that was ever stored.

        Screens that find a node id without a name use this source to fill in the name
        (a node with only telemetry in the Time Machine, a relay hash in the dashboard
        feed). It gives any name that the node ever transmitted. It is one aggregate scan.
        The ``packet`` rows are not included, because they have no reliable identity.

        Returns:
            The latest name that is not empty, keyed by the stored node id (the key
            prefix of 12 hex digits).
        """
        # The SQLite bare-column-with-MAX guarantee: in a group with ``MAX(observed_at)``,
        # the ungrouped ``name`` comes from the row that has the maximum. Thus one
        # aggregate scan gives the latest name of each node, and Python does not walk all
        # the history. ``NOT INDEXED``, because without it the planner walks
        # ``idx_observations_node`` row by row (one random-access read for each entry,
        # which is measurably slower than the scan).
        rows = self._conn.execute(
            "SELECT node, name, MAX(observed_at) FROM observations NOT INDEXED "
            "WHERE node IS NOT NULL AND name IS NOT NULL AND name != '' "
            "AND kind != 'packet' GROUP BY node"
        ).fetchall()
        return {row["node"]: row["name"] for row in rows}

    def last_heard_by_node(self) -> dict[str, datetime]:
        """The most recent reception of each node, with the time from our clock.

        This is the evidence part of the heard time of a contact. The contact table of the
        device reports ``last_advert``, which the node that sent the advert stamped with
        its own clock. That value is only a claim: a wrong RTC can keep it days in the past, for
        all time. But MeshTerm wrote each row here when we received something (refer to
        :meth:`~meshterm.services.device_state.DeviceState.contacts`, which takes the later
        of the two). The ``packet`` rows are not included, for the same reason that
        :meth:`heard_nodes` does not include them: a relayed packet tells us that we heard
        its last relay, not its origin node.

        This method is :meth:`heard_nodes` with only the one column that answers "when
        last?". SQLite does the aggregate and gives one row for each node. The method does
        not stream all the history into Python to build statistics objects for each node.
        This is important, because each read of the contacts calls this method, and the
        row building is the expensive part.

        Returns:
            The latest reception time, keyed by the stored node id (the key prefix of 12
            hex digits), in aware UTC.
        """
        # ``NOT INDEXED`` for the same reason as in :meth:`node_names`: without it, the
        # planner walks ``idx_observations_node`` with one random-access read for each
        # row. That is slower than the plain scan that this aggregate must have.
        rows = self._conn.execute(
            "SELECT node, MAX(observed_at) AS last FROM observations NOT INDEXED "
            "WHERE node IS NOT NULL AND kind != 'packet' GROUP BY node"
        ).fetchall()
        return {row["node"]: datetime.fromisoformat(row["last"]) for row in rows}

    def heard_nodes(self, *, since: datetime | None = None) -> list[HeardNode]:
        """Aggregate the stored observations into reception statistics for each node.

        The result includes all the monitor runs (with an optional limit to recent
        history). Thus it is a record over time of which nodes were heard, how strongly,
        and where. It is the base for the monitor summary and the coverage map. The rows
        of the ``packet`` kind are not included. Their SNR describes our link to the last
        relay of the packet, not to the origin node. Thus they put the reception quality on
        the wrong node. (They supply the topology graph instead: refer to
        :meth:`packet_paths`.)

        Args:
            since: Include only the observations at or after this time, if given.

        Returns:
            One :class:`HeardNode` for each distinct node, most recently heard first.
        """
        sql = (
            "SELECT node, public_key, name, node_type, snr, rssi, lat, lon, observed_at "
            "FROM observations WHERE kind != 'packet'"
        )
        params: list[Any] = []
        if since is not None:
            sql += " AND observed_at >= ?"
            params.append(since.isoformat())

        # One streaming pass, which aggregates in place. This scan of all the history is on
        # the open path of half the screens (Contacts, the map, the target list of a
        # trace). Thus it never builds an Observation object for each row, and it never
        # parses the timestamp of each row. ``observed_at`` is UTC ISO-8601, which compares
        # correctly as a string. Thus a string comparison finds each "most recent X", and
        # the function parses only the one stamp that wins for each node, at the end. ``>=``
        # in each comparison keeps the tie behaviour of the old method (sort, then walk
        # backwards): among equal stamps, the later row wins.
        stats: dict[str | None, list] = {}
        for row in self._conn.execute(sql, params):
            iso = row["observed_at"]
            snr = row["snr"]
            s = stats.get(row["node"])
            if s is None:
                # [count, snrs, last_iso, last_rssi, name, name_iso, type, type_iso,
                #  key, key_iso, lat, lon, loc_iso]
                stats[row["node"]] = s = [
                    0,
                    [],
                    "",
                    None,
                    None,
                    "",
                    None,
                    "",
                    None,
                    "",
                    None,
                    None,
                    "",
                ]
            s[0] += 1
            if snr is not None:
                s[1].append(snr)
            if iso >= s[2]:
                s[2], s[3] = iso, row["rssi"]
            if row["name"] and iso >= s[5]:
                s[4], s[5] = row["name"], iso
            if row["node_type"] is not None and iso >= s[7]:
                s[6], s[7] = row["node_type"], iso
            if row["public_key"] and iso >= s[9]:
                s[8], s[9] = row["public_key"], iso
            if row["lat"] is not None and row["lon"] is not None and iso >= s[12]:
                s[10], s[11], s[12] = row["lat"], row["lon"], iso

        nodes = [
            HeardNode(
                node=node,
                name=s[4],
                count=s[0],
                median_snr=statistics.median(s[1]) if s[1] else None,
                best_snr=max(s[1]) if s[1] else None,
                last_rssi=s[3],
                last_seen=datetime.fromisoformat(s[2]),
                lat=s[10],
                lon=s[11],
                node_type=s[6],
                public_key=s[8],
            )
            for node, s in stats.items()
        ]
        return sorted(nodes, key=lambda n: n.last_seen, reverse=True)

    def contact_signals(self, nodes: Sequence[str]) -> dict[str, ContactSignals]:
        """Get all the scoring signals for a set of nodes, in a fixed number of scans.

        This is the evidence for the Contacts sweep (refer to
        :mod:`~meshterm.core.contact_score`). The method is set-based on purpose. For a
        table of several hundred contacts, it does five grouped passes over the history,
        instead of five queries for each contact. Thus the cost of the sweep follows the
        size of the database, not the size of the table that the sweep examines.

        Each pass fills one part of :class:`~meshterm.core.contact_score.ContactSignals`:

        * **Reception**: first heard, last heard, and the transmission count, from
          ``observations``. The ``packet`` rows are not included, the same as in
          :meth:`heard_nodes`. Thus the count means "heard from this node", and it stays
          the same number that the ``PKTS`` lane of the contact list shows.
        * **Hops**: the median relay count of the packets that came from the node as their
          origin. This time the data comes from the ``packet`` rows, because only these
          rows have a path (refer to :meth:`packet_paths`). The median, not the minimum:
          one direct reception by chance must not make a node at four hops look like a
          neighbour.
        * **Direct messages**: the totals, our own outbound share, and the age of the
          latest message, matched by prefix. ``messages.peer`` has the width that the
          packet used for the address. This is not always the 12 hex digits that are the
          key of an observation (refer to :meth:`last_message_by_peer`).
        * **Channel messages**: attributed by the ``Name: `` prefix of a channel message,
          because a channel packet has no sender key. A name that two contacts have is
          attributed to neither contact. The caller marks these messages as not
          attributed, so that the score reads them as unknown, not as silence.

        Args:
            nodes: The canonical node ids (12 hex digits) to get signals for. A node that
                is not in the history comes back with an empty record. The score protects
                such a record, and does not punish it.

        Returns:
            One :class:`~meshterm.core.contact_score.ContactSignals` for each requested
            node, keyed by its id. This method does not fill in the channel attribution by
            name, because the repository does not know which name belongs to which contact
            (refer to :meth:`channel_post_counts`).
        """
        from ..core.contact_score import ContactSignals

        wanted = {n for n in nodes if n}
        if not wanted:
            return {}
        now = utcnow()

        def age_days(iso: str | None) -> float | None:
            """The days from a stored ISO stamp to now, or ``None`` for a stamp not valid."""
            if not iso:
                return None
            try:
                when = datetime.fromisoformat(iso)
            except (TypeError, ValueError):
                return None
            return max(0.0, (now - when).total_seconds() / 86400.0)

        # -- reception: one grouped pass over the non-packet history ------------------
        heard: dict[str, tuple[str, str, int]] = {}
        for row in self._conn.execute(
            "SELECT node, MIN(observed_at) AS first, MAX(observed_at) AS last, "
            "COUNT(*) AS n FROM observations "
            "WHERE node IS NOT NULL AND kind != 'packet' GROUP BY node"
        ):
            if row["node"] in wanted:
                heard[row["node"]] = (row["first"], row["last"], int(row["n"] or 0))

        # -- hops: the median relay count of the packets from each origin node --------
        # Limited to the most recent packets, as in :meth:`packet_paths`, for the same two
        # reasons. This is the only pass that builds a row object for each packet, instead
        # of an aggregate in SQL. Also, a hop count from one year ago is evidence about a
        # topology that does not exist now. Newest first, so that the limit keeps the
        # useful readings.
        hop_counts: dict[str, list[int]] = {}
        for row in self._conn.execute(
            "SELECT node, path FROM observations "
            "WHERE kind = 'packet' AND node IS NOT NULL AND path IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (HOP_EVIDENCE_LIMIT,),
        ):
            if row["node"] not in wanted:
                continue
            hops = [h for h in (row["path"] or "").split(",") if h]
            hop_counts.setdefault(row["node"], []).append(len(hops))

        # -- direct messages: totals, our share, and the latest, matched by prefix ----
        dm_rows = self._conn.execute(
            "SELECT peer, COUNT(*) AS total, SUM(outbound) AS sent, "
            "MAX(created_at) AS last FROM messages "
            "WHERE is_channel = 0 AND peer IS NOT NULL GROUP BY peer"
        ).fetchall()

        signals: dict[str, ContactSignals] = {}
        for node in sorted(wanted):
            first, last, packets = heard.get(node, (None, None, 0))
            hops = hop_counts.get(node)
            total = sent = 0
            latest: str | None = None
            for row in dm_rows:
                peer = (row["peer"] or "").lower()
                # Either side can be the shorter one. A packet can use any width for an
                # address. Thus a stored peer of 6 hex digits and a node id of 12 hex digits
                # are the same node when one is a prefix of the other.
                if not peer or not (peer.startswith(node) or node.startswith(peer)):
                    continue
                total += int(row["total"] or 0)
                sent += int(row["sent"] or 0)
                if latest is None or (row["last"] or "") > latest:
                    latest = row["last"]
            signals[node] = ContactSignals(
                node=node,
                heard_age_days=age_days(last),
                packets=packets,
                dm_total=total,
                dm_outbound=sent,
                dm_age_days=age_days(latest),
                hops=statistics.median(hops) if hops else None,
                known_days=age_days(first),
            )
        return signals

    def channel_post_counts(self) -> dict[str, int]:
        """The number of channel messages that each name sent, by lowercase name.

        A channel packet has no sender key. A sender identifies itself with the prefix
        ``Name: `` before the text (refer to
        :func:`~meshterm.core.channels.split_channel_sender`). Thus this is the only
        attribution that is available, and it uses only the display name. The caller must
        not trust a name that two contacts share. This method only counts.

        Our own outbound messages are not included, because they tell nothing about other
        nodes.

        Returns:
            Message counts keyed by the lowercase sender name. A message without a usable
            name prefix in its text is not counted.
        """
        from ..core.channels import split_channel_sender

        counts: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT text FROM messages WHERE is_channel = 1 AND outbound = 0"
        ):
            name, _ = split_channel_sender(row["text"] or "")
            if name:
                key = name.strip().casefold()
                counts[key] = counts.get(key, 0) + 1
        return counts

    # -- chat messages ----------------------------------------------------------

    def record_chat_message(self, msg: ChatMessage, *, run_id: int | None = None) -> int:
        """Store one chat message (sent or received).

        Args:
            msg: The message to store. Its ``peer`` is normalized to lowercase, so that
                the queries for a direct conversation get consistent results.
            run_id: The background ``chat`` run that owns the message, if any. (Inbound
                messages are logged to one. Outbound sends can have no run.)

        Returns:
            The primary key of the new message.
        """
        peer = msg.peer.lower() if msg.peer else None
        cur = self._conn.execute(
            "INSERT INTO messages "
            "(run_id, outbound, is_channel, channel_id, channel_idx, peer, peer_name, text, "
            "snr, acked, created_at, scope, author) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                int(msg.outbound),
                int(msg.is_channel),
                msg.channel_id,
                msg.channel_idx,
                peer,
                msg.peer_name,
                msg.text,
                msg.snr,
                None if msg.acked is None else int(msg.acked),
                msg.created_at.isoformat(),
                msg.scope,
                msg.author.lower() if msg.author else None,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def has_room_post(self, msg: ChatMessage) -> bool:
        """Whether this room post is already in the history of the room.

        A room sends a post again when it did not hear our acknowledgement. For the room,
        an acknowledgement lost on the way back looks the same as a lost post. Also, a
        login asks for each post newer than the last post that the companion stored, and
        that post can be older than the last post that we stored. In both cases, the same
        post arrives two times, and the history must keep it one time. A room stamps each
        post with a unique time from its own clock. Thus the room, the author, the time,
        and the text together identify the post.

        Args:
            msg: An inbound room post (:attr:`~ChatMessage.is_post`).

        Returns:
            ``True`` when a row with the same room, author, time, and text exists.
        """
        row = self._conn.execute(
            "SELECT 1 FROM messages WHERE is_channel = 0 AND peer = ? AND author = ? "
            "AND created_at = ? AND text = ? LIMIT 1",
            (
                (msg.peer or "").lower(),
                (msg.author or "").lower(),
                msg.created_at.isoformat(),
                msg.text,
            ),
        ).fetchone()
        return row is not None

    def update_chat_ack(self, message_id: int, acked: bool) -> None:
        """Change the delivery acknowledgement of one outbound message (used at a retry).

        Args:
            message_id: The ``messages`` row to update.
            acked: The new delivery state: ``True`` for acknowledged, ``False`` for not
                acknowledged.
        """
        self._conn.execute("UPDATE messages SET acked = ? WHERE id = ?", (int(acked), message_id))
        self._conn.commit()

    def delete_chat_history(self, peer: str | None) -> int:
        """Delete all the stored messages of one direct conversation.

        This is the history delete for one contact in the chat picker. It deletes only the
        direct messages of that peer. The channel history and all the other conversations
        do not change. The peer matches the form in which :meth:`record_chat_message`
        stores it (the key prefix in lowercase).

        Args:
            peer: The key prefix of the contact (the identity of the direct conversation).

        Returns:
            The number of deleted messages.
        """
        cur = self._conn.execute(
            "DELETE FROM messages WHERE is_channel = 0 AND peer = ?",
            ((peer or "").lower(),),
        )
        self._conn.commit()
        return int(cur.rowcount or 0)

    def recent_chat_messages(
        self,
        *,
        is_channel: bool,
        channel_id: str | None = None,
        peer: str | None = None,
        limit: int = 200,
        posts_only: bool = False,
    ) -> list[ChatMessage]:
        """Return the most recent messages of a conversation, oldest first.

        Args:
            is_channel: Whether to read a channel conversation.
            channel_id: The identity of the channel, which does not depend on the slot
                (for channel conversations).
            peer: The key prefix of the contact (for direct conversations), or of a room.
            limit: The maximum number of messages to return.
            posts_only: Read the board of a room: the posts that it relayed to us and the
                posts that we posted to it. Leave out the other messages that arrive from a
                room under the same key: its replies to the commands of an admin, and each
                post stored before posts had their author (refer to
                :attr:`ChatMessage.is_post`).

        Returns:
            The messages in chronological order (ready to render as a transcript).
        """
        if is_channel:
            where, params = "is_channel = 1 AND channel_id = ?", [channel_id]
        else:
            where, params = "is_channel = 0 AND peer = ?", [(peer or "").lower()]
            if posts_only:
                where += " AND (outbound = 1 OR author IS NOT NULL)"
        rows = self._conn.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [self._row_to_chat(row) for row in reversed(rows)]

    def last_chat_messages(self, rooms: Iterable[str] = ()) -> dict[str, ChatMessage]:
        """Return the latest message of each conversation, keyed by conversation key.

        The preview snippets of the conversation picker use this result. There is one row
        for each distinct conversation: the message with the highest id (the most recent)
        in that conversation.

        Args:
            rooms: The key prefixes of the room servers in the conversations, in lowercase
                as stored. The latest message of a room is its latest post (what
                :meth:`recent_chat_messages` reads with ``posts_only``). Thus the reply to
                a command of an admin never shows as the last message on the board.

        Returns:
            A mapping of :func:`~meshterm.core.models.conversation_key` to its latest
            :class:`ChatMessage`.
        """
        peers = sorted({room.lower() for room in rooms if room})
        replies = ""
        if peers:
            # Leave out the inbound rows of a room that are not posts. A peer must match
            # exactly, because the rows of a room are stored under the prefix that its own
            # contact entry has.
            marks = ", ".join("?" * len(peers))
            replies = (
                f" WHERE NOT (is_channel = 0 AND outbound = 0 AND author IS NULL "
                f"AND peer IN ({marks}))"
            )
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE id IN ("
            f"  SELECT MAX(id) FROM messages{replies} GROUP BY "
            "  CASE WHEN is_channel = 1 THEN 'chan:' || channel_id "
            "       ELSE 'dm:' || peer END)",
            peers,
        ).fetchall()
        return {msg.key: msg for msg in (self._row_to_chat(r) for r in rows)}

    def last_message_by_peer(self) -> dict[str, datetime]:
        """The most recent inbound direct message of each peer, with the time from our clock.

        This is the other part of the heard-time evidence (refer to
        :meth:`last_heard_by_node`). In the lexicon of the app, a direct message received
        from a node means that we heard that node. But the message arrives as a
        ``CONTACT_MSG_RECV`` event and is stored here, never as an observation. Thus
        nothing in the reception history knows about it. We do not store it as an
        observation, on purpose. The SNR of a routed message describes the link to its last
        relay, which is the reason that :meth:`heard_nodes` does not include ``packet``
        rows. Also, a count of it makes a lane larger whose meaning is overheard traffic.

        Outbound messages are not included: to send to a node is not to hear from it.

        Returns:
            The latest time of an inbound message, keyed by the stored peer prefix (in
            lowercase, as :meth:`record_chat_message` writes it), in aware UTC. The prefix
            width is the width that the packet used for the address. Thus the callers
            match it as a prefix, not by equality.
        """
        rows = self._conn.execute(
            "SELECT peer, MAX(created_at) AS last FROM messages "
            "WHERE is_channel = 0 AND outbound = 0 AND peer IS NOT NULL GROUP BY peer"
        ).fetchall()
        return {row["peer"]: datetime.fromisoformat(row["last"]) for row in rows}

    def direct_message_bounds(
        self, peer: str | None, when: datetime, *, outbound: bool
    ) -> tuple[datetime | None, datetime | None]:
        """The times of the direct messages on each side of ``when`` in one conversation.

        These times limit the message-paths search for a direct message (refer to
        :mod:`~meshterm.services.message_paths`). A direct packet is encrypted, so the log
        cannot tell which message it carried. Thus a fixed window around the message is
        the only limit that is available, and at the speed of a conversation it is much
        too wide. Nine messages of one real exchange were in a single ±90 s window. Thus
        each of them showed the packets of all nine messages as its own.

        The messages around it are the natural edges: a packet logged after the user
        composed the next message belongs to that next message, not to this one. The
        caller clamps its window to the midpoint of each gap. Thus the limit becomes
        narrower exactly as the conversation becomes faster.

        Only the messages that go in the **same direction** count as neighbours, because
        two different clocks stamp the two directions. Our own sends have our clock, read
        when they leave. A received message has the clock of the sender, which can be
        minutes away from ours. Thus, if MeshTerm puts an inbound message in order against
        an outbound message, it compares two clocks. It can then limit a message with an
        edge that, in the clock of that message, did not occur yet. That is how the first
        version of this clamp gave a received message an empty window. In one direction,
        the stamps are consistent, so the neighbours mean what they say.

        Args:
            peer: The key prefix of the contact, as :meth:`record_chat_message` stores it.
            when: The time of the message.
            outbound: The direction in which to look: the direction of the message.

        Returns:
            ``(previous, next)``: the times of the messages in the same direction. Each of
            them is ``None`` when this message is the first or the last on its side of the
            conversation.
        """
        key = (peer or "").lower()
        args = (key, int(outbound), when.isoformat())
        row = self._conn.execute(
            "SELECT MAX(created_at) AS edge FROM messages "
            "WHERE is_channel = 0 AND peer = ? AND outbound = ? AND created_at < ?",
            args,
        ).fetchone()
        prev = _as_when(row["edge"] if row else None)
        row = self._conn.execute(
            "SELECT MIN(created_at) AS edge FROM messages "
            "WHERE is_channel = 0 AND peer = ? AND outbound = ? AND created_at > ?",
            args,
        ).fetchone()
        return prev, _as_when(row["edge"] if row else None)

    def channel_stats(self) -> dict[str, ChannelStats]:
        """Aggregate the stored channel messages into statistics for each channel.

        The list lanes of the channel manager use this result. The row of each configured
        channel shows its total message count, the age of its last message, and an
        activity sparkline over the trailing :data:`ACTIVITY_WINDOW`. This method builds
        the histogram of the sparkline, with :data:`ACTIVITY_BUCKETS` columns. (The
        sparkline draws its newest :data:`ACTIVITY_DRAWN_BUCKETS`. The rest supplies the
        shared scaling peak.) There are two passes, each grouped and filtered in SQL, so
        that the cost follows the message volume, not the channel count. The first pass is
        an aggregate for the totals of all time. The second pass gets the individual
        timestamps of the window, and Python puts them in buckets (a few hours of
        messages, so the row set stays small).

        The timestamps are compared as strings. ``utcnow().isoformat()`` writes each
        ``created_at`` (a fixed-width UTC ISO-8601 form). Thus the lexicographic order is
        the chronological order, and the window cutoff does not have to parse each row.
        Messages from before the history had identity keys (a ``NULL`` ``channel_id``:
        refer to :meth:`backfill_channel_ids`) have no channel to count under, and the
        method skips them.

        Returns:
            A mapping of channel identity to its :class:`ChannelStats`. Channels with no
            stored messages have no entry.
        """
        now = utcnow()
        cutoff = now - ACTIVITY_WINDOW
        bucket_span = ACTIVITY_WINDOW / ACTIVITY_BUCKETS
        rows = self._conn.execute(
            "SELECT channel_id, COUNT(*) AS total, MAX(created_at) AS last_at "
            "FROM messages WHERE is_channel = 1 AND channel_id IS NOT NULL "
            "GROUP BY channel_id"
        ).fetchall()

        histograms: dict[str, list[int]] = {}
        recent_rows = self._conn.execute(
            "SELECT channel_id, created_at FROM messages "
            "WHERE is_channel = 1 AND channel_id IS NOT NULL AND created_at >= ?",
            (cutoff.isoformat(),),
        ).fetchall()
        for row in recent_rows:
            try:
                created_at = datetime.fromisoformat(row["created_at"])
                # Bucket by age, so that the histogram comes out newest first (bucket 0 has
                # the current five minutes). This is the order that the sparkline widget
                # expects.
                idx = min(ACTIVITY_BUCKETS - 1, int((now - created_at) / bucket_span))
            except (TypeError, ValueError):
                continue  # a malformed or naive timestamp does not go in a bucket
            histogram = histograms.setdefault(row["channel_id"], [0] * ACTIVITY_BUCKETS)
            histogram[max(0, idx)] += 1

        stats: dict[str, ChannelStats] = {}
        for row in rows:
            try:
                last_at = datetime.fromisoformat(row["last_at"])
            except (TypeError, ValueError):
                last_at = None  # a malformed value must not hide the counts of the channel
            histogram = tuple(histograms.get(row["channel_id"], [0] * ACTIVITY_BUCKETS))
            stats[row["channel_id"]] = ChannelStats(
                total=int(row["total"]),
                recent=sum(histogram),
                last_at=last_at,
                histogram=histogram,
            )
        return stats

    def channel_packets(self) -> list[tuple[str, str, str, str | None, str | None]]:
        """Each different channel packet that MeshTerm stored, for the channel guide.

        A repeater relays a message, so MeshTerm often hears one message several times. The
        copies have the same hash, MAC, and ciphertext, and the query groups them into one
        row. Thus each row is one message, with the time when MeshTerm first and last heard
        it. A packet with no MAC or no ciphertext cannot be checked against a key, and the
        query does not return it.

        Returns:
            ``(chan_hash, cipher_mac, crypted, first_heard, last_heard)`` for each message.
            The times are the stored ISO 8601 text.
        """
        rows = self._conn.execute(
            "SELECT chan_hash, cipher_mac, crypted, "
            "MIN(observed_at) AS first_at, MAX(observed_at) AS last_at FROM observations "
            "WHERE chan_hash IS NOT NULL AND cipher_mac IS NOT NULL AND crypted IS NOT NULL "
            "GROUP BY chan_hash, cipher_mac, crypted"
        ).fetchall()
        return [
            (r["chan_hash"], r["cipher_mac"], r["crypted"], r["first_at"], r["last_at"])
            for r in rows
        ]

    def message_texts(self) -> list[str]:
        """The text of each stored chat message, for the names that the channel guide tests.

        Returns:
            The texts, in no specific order. An empty text is not returned.
        """
        rows = self._conn.execute("SELECT text FROM messages WHERE text IS NOT NULL AND text != ''")
        return [row["text"] for row in rows]

    def backfill_channel_ids(self, mapping: dict[int, str]) -> int:
        """Give old channel messages an identity, keyed by the slot on which they were stored.

        Messages written before the channel history had identity keys have a ``NULL``
        ``channel_id``. No record tells which channel was in each slot at that time. Thus
        the best available guess is the channel that is in that slot now. ``mapping`` maps
        a slot index to the identity of the channel that is there now. The method changes
        only the rows that do not have an identity yet, so it is safe to run at each
        start.

        Args:
            mapping: The slot index to the identity of the current channel at that slot.

        Returns:
            The number of old rows that got an identity.
        """
        changed = 0
        for idx, channel_id in mapping.items():
            cur = self._conn.execute(
                "UPDATE messages SET channel_id = ? "
                "WHERE is_channel = 1 AND channel_id IS NULL AND channel_idx = ?",
                (channel_id, idx),
            )
            changed += cur.rowcount
        if changed:
            self._conn.commit()
        return changed

    # -- stored UI state --------------------------------------------------------

    def get_map_view(self) -> tuple[float, float, int] | None:
        """Return the last stored map viewport as ``(center_lat, center_lon, zoom)``.

        With it, the interactive map opens again exactly where the user left it. The
        function returns ``None`` when no viewport is stored yet, or when it cannot parse
        a stored value. (It treats such a value as absent, not as an error. Thus a corrupt
        row only fits the map to the nodes again.)
        """
        row = self._conn.execute("SELECT value FROM app_state WHERE key = 'map_view'").fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["value"])
            return float(data["lat"]), float(data["lon"]), int(data["zoom"])
        except (ValueError, KeyError, TypeError):
            return None

    def set_map_view(self, lat: float, lon: float, zoom: int) -> None:
        """Store the map viewport, so that the next session opens the map at the same place.

        Args:
            lat: The latitude at the centre of the viewport.
            lon: The longitude at the centre of the viewport.
            zoom: The zoom level of the map.
        """
        self._conn.execute(
            "INSERT OR REPLACE INTO app_state(key, value) VALUES ('map_view', ?)",
            (json.dumps({"lat": lat, "lon": lon, "zoom": zoom}),),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_chat(row: sqlite3.Row) -> ChatMessage:
        """Rebuild a :class:`ChatMessage` from a ``messages`` row."""
        return ChatMessage(
            text=row["text"],
            outbound=bool(row["outbound"]),
            is_channel=bool(row["is_channel"]),
            channel_id=row["channel_id"],
            channel_idx=row["channel_idx"],
            peer=row["peer"],
            peer_name=row["peer_name"],
            snr=row["snr"],
            acked=None if row["acked"] is None else bool(row["acked"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            row_id=row["id"],
            scope=row["scope"] if "scope" in row.keys() else None,
            author=row["author"] if "author" in row.keys() else None,
        )
