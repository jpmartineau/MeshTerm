# SPDX-License-Identifier: Apache-2.0
"""Repository: the single gateway between the app and the database.

Tools and services never issue SQL directly; they call typed methods here. This keeps
persistence concerns in one place and makes the storage backend swappable.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from collections.abc import Sequence
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


#: The trailing window the dashboard's live observation stats prune to — "what's
#: happening right now" territory, labelled "reception over 2 h" on the RF health card.
#: Distinct from the deeper channel-activity window below: this bounds a live view, that
#: one only feeds a scaling peak. The Live feed screen once shared it and no longer does:
#: its depth is a packet count, not a duration (see
#: :data:`~meshterm.ui.livefeed_screen._FEED_SEED_FLOOR`), so this number means the
#: dashboard's two hours and nothing else.
OBSERVATION_WINDOW = timedelta(hours=2)

#: The trailing window a channel's activity histogram is computed over. Six hours is
#: deeper than the sparkline draws (see :data:`ACTIVITY_DRAWN_BUCKETS`) on purpose: the
#: extra history deepens the pool the shared scaling peak takes its percentile over, so
#: the ceiling glides rather than snaps as a busy stretch ages out of the drawn window
#: (see :func:`~meshterm.ui.braillechart.activity_peak`).
#: All-time volume still lives in the MSGS lane; this stays a live-pulse window.
ACTIVITY_WINDOW = timedelta(hours=6)

#: How many equal time buckets the window is split into — at six hours, five minutes each
#: (matching the drawn cadence). The histogram carries all of them for the scaling peak;
#: only the newest :data:`ACTIVITY_DRAWN_BUCKETS` are actually charted.
ACTIVITY_BUCKETS = 72

#: How many of the histogram's newest buckets the channel sparkline draws — two hours at
#: five minutes each, two columns per braille cell, so 24 buckets fill its twelve
#: characters. The rest of :data:`ACTIVITY_BUCKETS` feeds the scaling peak but isn't shown.
ACTIVITY_DRAWN_BUCKETS = 24

#: How many recent packet frames the contact-scoring hop median is drawn from (see
#: :meth:`Repository.contact_signals`). The same order of magnitude as
#: :meth:`Repository.packet_paths`' own cap, and for the same reasons: it is the one scan
#: there that materialises a row per packet, and old hops describe a topology that has since
#: moved. Deep enough that every contact heard at all in the recent past gets several
#: readings to take a median over.
HOP_EVIDENCE_LIMIT = 20000


def _packet_raw(row: sqlite3.Row) -> dict | None:
    """Rebuild the minimal raw payload a stored ``packet`` observation is read back with.

    What a live RX-log event carried in its raw payload is kept per row, under the very
    keys the event used, so a replayed frame and a fresh one read identically all the way
    up: the frame's payload class (``payload_typename``), so a list can name what the
    packet is; an overheard channel frame's three crypto fields (fingerprint, MAC,
    ciphertext), so a channel we hold the key for stays readable straight from history;
    and what the frame addressed (:mod:`~meshterm.core.frames`) — the recipient, the
    sender, the token — restored to the key each was decoded under. The stored sender is
    a hash or a whole public key, and the stored token an ack's checksum or a trace's tag;
    the value's own width and the frame's class say which, so neither needed a second
    column to be told apart. A trace's per-hop link readings are the one thing that did
    need a column of its own — they arrive where every other class keeps its relay hashes,
    so ``path`` was exactly the wrong place for them. A row that stored none of it carries
    no raw (``None``).
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
        # An addressed frame keeps only its MAC (no channel envelope), so it is restored
        # here rather than in the chan_hash branch above.
        raw["cipher_mac"] = cipher_mac
    if dest:
        raw["dest_hash"] = dest
    if src:
        # A hash is one byte; anything longer is the whole key an anonymous request carries.
        raw["src_key" if len(src) > 2 * ENDPOINT_HASH_BYTES else "src_hash"] = src
    if tag:
        raw["trace_tag" if typename == "TRACE" else "ack_crc"] = tag
    if trace_snrs:
        raw["trace_snrs"] = [float(v) for v in str(trace_snrs).split(",") if v]
    if route:
        raw["route_typename"] = route
    if transport_code:
        # A scoped frame's code and the bytes it was computed over, so its region resolves
        # against names learned after it was heard (see meshterm.core.regions).
        raw["transport_code"] = transport_code
    if scope_body:
        raw["scope_body"] = scope_body
    return raw


def _as_when(iso: Any) -> datetime | None:
    """Parse a stored ISO timestamp, or ``None`` when absent/unparseable."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None


def _row_value(row: sqlite3.Row, key: str) -> Any:
    """Read ``key`` from a row, tolerating a query that didn't select it (returns ``None``)."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _hops_hash_bytes(hops: list[Hop]) -> int | None:
    """Recover a stored trace's per-hop path-hash width from its hop hashes.

    The ``traces`` table predates :attr:`~meshterm.core.models.TraceResult.
    path_hash_bytes`, so the width isn't a column — but it doesn't need to be: a trace
    reply names every hop at exactly the command's width, so the (uniform) length of
    the stored hashes *is* the width. Without it, rehydrated traces would render node
    hashes — and our own device's full public key — at absurd lengths.

    Args:
        hops: The rehydrated hops (the final hash-less hop, our own device, is skipped).

    Returns:
        The width in bytes, or ``None`` when there are no hashes or they disagree
        (which stored trace replies never do; ``None`` falls back to full-width display).
    """
    widths = {len(h.node) for h in hops if h.node}
    if len(widths) != 1:
        return None
    width = widths.pop()
    return width // 2 if width and width % 2 == 0 else None


@dataclass(slots=True)
class ChannelStats:
    """Aggregated message history for one channel conversation.

    Attributes:
        total: Messages ever stored for the channel, sent and received alike.
        recent: Messages within the trailing :data:`ACTIVITY_WINDOW` window.
        last_at: When the channel's most recent message was stored, or ``None`` if the
            stored timestamp can't be parsed.
        histogram: The window's messages split into :data:`ACTIVITY_BUCKETS` equal time
            buckets, *newest first* — bucket 0 is the current five minutes, the order
            :func:`~meshterm.ui.braillechart.activity_sparkline` expects (it flips the
            window so "now" draws at the right edge). ``recent`` is always its sum. The
            sparkline charts only the newest :data:`ACTIVITY_DRAWN_BUCKETS`; the deeper
            tail feeds the shared scaling peak (see
            :func:`~meshterm.ui.braillechart.activity_peak`).
    """

    total: int
    recent: int
    last_at: datetime | None
    histogram: tuple[int, ...]


@dataclass(slots=True)
class TracedPath:
    """One successful trace's walked path, as evidence for the topology graph.

    Attributes:
        when: When the trace completed.
        hops: The per-hop readings in path order, each ``(node, snr)`` — ``node`` is the
            hop's raw hex hash (``None`` for the final hash-less hop, our own device) and
            ``snr`` the reception measured *arriving at* that hop. Because trace replies
            retrace the path, the sequence covers the outbound and return legs alike.
    """

    when: datetime
    hops: list[tuple[str | None, float]]


@dataclass(slots=True)
class PacketPath:
    """One RX-logged packet's relay path, as evidence for the topology graph.

    Attributes:
        when: When the packet was overheard.
        origin: The originating node's hex hash, when the packet class reveals it
            (adverts do); ``None`` otherwise.
        snr: Our reception SNR (dB) — a reading on the link from the *last relay*
            (or, with no relays, the origin) to us.
        hops: The relay hashes in propagation order, nearest the origin first and the
            repeater we actually heard last; empty for a direct (zero-hop) packet.
    """

    when: datetime
    origin: str | None
    snr: float | None
    hops: list[str]


@dataclass(slots=True)
class NeighbourLink:
    """One repeater-reported direct link, as evidence for the topology graph.

    The fetched counterpart of :class:`TracedPath`/:class:`PacketPath`: instead of being
    inferred from what *we* received, this link was asserted by a remote repeater about
    its own reception (see :meth:`Repository.neighbour_links`).

    Attributes:
        when: The link's recency — when the repeater last heard the neighbour, falling
            back to when we fetched the table.
        repeater: Canonical id of the repeater that reported the link.
        neighbour: The neighbour's hex hash as the repeater replied it (any width; the
            topology layer canonicalizes).
        snr: SNR (dB) measured at the repeater, if reported.
    """

    when: datetime
    repeater: str
    neighbour: str
    snr: float | None


@dataclass(slots=True)
class DiscoveredPath:
    """One record-holding walk in the trophy case (a discipline's leaderboard).

    Records are kept per ``(category, width_bytes)`` — the per-hop hash width bounds
    both a walk's maximum length (the transmitted path field is 64 bytes) and its
    collision odds, so boards at different widths measure different games.

    Attributes:
        id: Primary key (the handle deletion takes).
        category: The category id the record was set in (``grand_tour``, …).
        width_bytes: The per-hop path-hash width the walk was transmitted at.
        spec: The transmitted spec — comma-separated hex hashes, in walk order.
        route: Canonical node ids aligned with the spec's hops, for stable display
            resolution (a 1-byte spec hop is too ambiguous to re-resolve later).
        score: The category score (its unit is the category's: km, nodes, dB, km²).
        stats: The walk's measured statistics (hop count, distinct nodes, km, …).
        app_version: The MeshTerm version that discovered it, for future migrations.
        discovered_at: When the record-setting walk came home (UTC).
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
        id: Primary key of the run.
        tool: Name of the tool that executed.
        profile: Device profile used, if any.
        status: ``running``, ``ok``, or ``error``.
        started_at: ISO-8601 start timestamp.
        finished_at: ISO-8601 finish timestamp, or ``None`` if still running.
        summary: Decoded JSON summary, or ``None``.
    """

    id: int
    tool: str
    profile: str | None
    status: str
    started_at: str
    finished_at: str | None
    summary: dict | None


class Repository:
    """Typed data-access layer over the SQLite database."""

    def __init__(self, db_path: Path) -> None:
        """Open the repository against a database file.

        Args:
            db_path: Location of the SQLite database (created if absent).
        """
        self._conn = db.connect(db_path)

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    # -- runs -------------------------------------------------------------------

    def start_run(self, tool: str, args: dict[str, Any], profile: str | None = None) -> int:
        """Record the start of a tool execution.

        Args:
            tool: Tool name.
            args: Arguments the tool was invoked with (must be JSON-serializable).
            profile: Active device profile name, if any.

        Returns:
            The new run's primary key.
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
            status: Terminal status (``ok`` or ``error``).
            summary: Optional JSON-serializable result summary.
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
            limit: Maximum number of rows to return.

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
        """Persist a single trace and its per-hop SNR readings.

        Args:
            run_id: The owning run.
            trace: The trace result to store.

        Returns:
            The new trace's primary key.
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
        """Return the most recently recorded trace to ``target``, rehydrated with hops.

        Used to show the previous run's result before a new trace starts.

        Args:
            target: The trace destination to look up.
            exclude_run_id: A run to skip (typically the in-progress one) so we surface
                a genuinely prior result.
            success_only: When ``True``, ignore traces that timed out.

        Returns:
            The latest matching :class:`TraceResult`, or ``None`` if none is stored.
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
        """Return recent traces to ``target``, newest first, rehydrated with hops.

        This is the history the link-quality baseline is computed over, so both timed-out
        and successful traces are included (a rise in failures is itself a regression).

        Args:
            target: The trace destination to load.
            limit: Maximum number of traces to return.

        Returns:
            The matching :class:`TraceResult` objects, newest first.
        """
        rows = self._conn.execute(
            "SELECT * FROM traces WHERE target = ? ORDER BY id DESC LIMIT ?",
            (target, limit),
        ).fetchall()
        return [self._hydrate_trace(row) for row in rows]

    def _hydrate_trace(self, row: sqlite3.Row) -> TraceResult:
        """Rebuild a :class:`TraceResult` (with hops) from a ``traces`` row.

        Args:
            row: A ``traces`` table row.

        Returns:
            The rehydrated trace.
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

        This feeds the target picker's *Recently traced* section, so the list is
        deliberately short and recency-ordered: an all-time tally only ever grows,
        burying current work under stale names (``--mock`` targets included).
        Target-less walks — the hand-composed ones recorded under
        :data:`~meshterm.core.models.PATH_TRACE_TARGET` — are excluded: they aren't
        destinations one can pick.

        Args:
            limit: Maximum number of distinct targets to return.

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
        """Per trace target, when it was most recently traced (any outcome).

        Feeds the Trace-target picker's ``TRACED`` column and its default sort — how long
        ago each node was last aimed at, so the picker opens with the most-recently-traced
        node on top. Keyed by the raw target string the trace was filed under (a contact
        name or a hex hash), exactly like :meth:`target_trace_counts`; the caller matches
        its node against those keys. Timed-out attempts count — you *traced* the node
        whether or not it answered — and the hand-composed path walks filed under
        :data:`~meshterm.core.models.PATH_TRACE_TARGET` are excluded (they name no target).

        Returns:
            ``target → last-traced timestamp`` (aware UTC) for every distinct non-path-walk
            target.
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
        """Per trace target, its ``(successes, total)`` across every stored trace.

        The substrate for a route's observed reliability: unlike a walk's route, a target
        is on every trace row — timed-out attempts included — so a target's success rate is
        the one delivery figure the history can honestly answer (a failed trace records no
        path, only the target it was aimed at). Keyed by the raw target string the trace
        was filed under (a contact name or a hex hash); the caller matches its node against
        those keys. The hand-composed path walks filed under
        :data:`~meshterm.core.models.PATH_TRACE_TARGET` are excluded — they name no target.

        Returns:
            ``target → (successes, total)`` for every distinct non-path-walk target.
        """
        rows = self._conn.execute(
            "SELECT target, SUM(success) AS ok, COUNT(*) AS n FROM traces "
            "WHERE target != ? GROUP BY target",
            (PATH_TRACE_TARGET,),
        ).fetchall()
        return {row["target"]: (int(row["ok"] or 0), int(row["n"])) for row in rows}

    def trace_paths(self, *, limit: int = 2000) -> list[TracedPath]:
        """Return the walked paths of recent successful traces, newest first.

        This is the trace side of the topology evidence: every successful trace is a
        packet that demonstrably crossed each link in its path (out and back), with an
        SNR reading at every hop. Hops are returned raw — hex hashes at whatever width
        the original command addressed them — for the topology layer to canonicalize.

        Args:
            limit: Maximum number of traces to load.

        Returns:
            One :class:`TracedPath` per successful trace that recorded hops.
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
                continue  # a malformed stray contributes no evidence
            paths.append(TracedPath(when=when, hops=[(h["node"], h["snr"]) for h in hop_rows]))
        return paths

    def packet_paths(self, *, limit: int = 5000) -> list[PacketPath]:
        """Return the relay paths of recent RX-logged packets, newest first.

        The passive side of the topology evidence: each row is a packet the companion
        overheard whose header carried the repeater path it had traversed so far. Only
        ``packet``-kind observations carry one (see :meth:`record_observation`); rows
        whose path is NULL are skipped, and an empty path (a direct packet) is returned
        with no hops so a known origin still yields a direct-link reading.

        Args:
            limit: Maximum number of packet observations to load.

        Returns:
            One :class:`PacketPath` per stored packet observation.
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
                continue  # a malformed stray contributes no evidence
            hops = [h for h in (row["path"] or "").split(",") if h]
            paths.append(PacketPath(when=when, origin=row["node"], snr=row["snr"], hops=hops))
        return paths

    def record_neighbours(
        self, run_id: int, repeater: str, neighbours: list[NeighbourInfo]
    ) -> None:
        """Persist one fetched neighbour-table snapshot from a remote repeater.

        Every entry becomes a row; refetching the same repeater later appends a fresh
        snapshot rather than overwriting, and :meth:`neighbour_links` reads back only
        the latest row per ``(repeater, neighbour)`` pair — a neighbour table is the
        repeater's *current* state, so a new snapshot supersedes the old one instead
        of stacking as extra evidence.

        Args:
            run_id: The owning run.
            repeater: Canonical id of the repeater the table came from.
            neighbours: The fetched entries (may be empty — recorded as no rows).
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
        """Return the current repeater-reported links, newest report first.

        The fetched side of the topology evidence. Only the most recent row per
        ``(repeater, neighbour)`` pair is returned (see :meth:`record_neighbours`), so
        repeatedly refreshing a table never inflates a link's sample count.

        Args:
            limit: Maximum number of links to load.

        Returns:
            One :class:`NeighbourLink` per currently-reported link.
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
                continue  # a malformed stray contributes no evidence
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
        """Persist one robust TX-power level from an optimization sweep.

        The ``median_min_snr`` column stores this optimizer's headline metric — the
        median SNR measured *at the target* — rather than a path bottleneck.

        Args:
            run_id: The owning run.
            level: Aggregated result for a single TX power level.
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
        """Persist one measured candidate path from a path-probe sweep.

        Args:
            run_id: The owning probe run.
            target: The node the candidate paths lead to.
            path: The forced outbound path measured (comma-separated hex hashes).
            bottleneck_snr: Median of the candidate's per-trace bottleneck SNRs (dB),
                or ``None`` when no trace over it succeeded.
            success_rate: Fraction of traces over this path that replied, ``[0, 1]``.
            median_rtt_ms: Median round-trip time over this path, if measured.
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
        """Offer one walk to a category leaderboard; store it only if it places.

        The leaderboard invariant lives here so every caller shares it: a walk enters
        the ``(category, width_bytes)`` board when it beats the standing entries (or
        the board isn't full), an identical spec only ever keeps its *best* score
        (re-walking a known route never duplicates a row), and the board is pruned
        back to ``keep`` rows on the way out.

        Args:
            category: The category id the walk is offered to.
            width_bytes: The per-hop hash width the walk was transmitted at.
            spec: The transmitted spec (comma-separated hex hashes).
            route: Canonical node ids aligned with the spec's hops.
            score: The category score of this walk.
            stats: JSON-serializable walk statistics.
            app_version: The running MeshTerm version, stamped on the row.
            keep: Board size (rows kept per category and width).
            ascending: ``True`` for categories where *lower* scores win
                (Thin thread hunts the weakest surviving link).

        Returns:
            The stored row's id when the walk placed (a fresh row or an improved
            re-walk), or ``None`` when it didn't make the board.
        """

        def beats(challenger: float, standing: float) -> bool:
            return challenger < standing if ascending else challenger > standing

        existing = self._conn.execute(
            "SELECT id, score FROM discovered_paths "
            "WHERE category = ? AND width_bytes = ? AND spec = ?",
            (category, width_bytes, spec),
        ).fetchone()
        if existing is not None:
            # A known route: keep the row (and its discovery date) unless this walk
            # genuinely bettered its own record.
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
        # Prune the board back to ``keep``: worst scores go, oldest first among ties,
        # so the walk that set a mark holds it against an equal latecomer.
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
        """Return stored trophy-case records, optionally narrowed.

        Rows come back unranked (grouped by category, newest first within one) — the
        service layer owns each category's score direction and sorts for display.

        Args:
            category: Only this category's records, or ``None`` for all.
            width_bytes: Only records at this hash width, or ``None`` for all.

        Returns:
            The matching :class:`DiscoveredPath` rows.
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
        """Delete one trophy-case record by id; ``True`` when a row actually went."""
        cursor = self._conn.execute("DELETE FROM discovered_paths WHERE id = ?", (discovery_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def delete_discoveries(
        self, category: str | None = None, *, width_bytes: int | None = None
    ) -> int:
        """Delete trophy-case records wholesale, optionally narrowed; returns the count.

        Args:
            category: Only this category's records, or ``None`` for every category.
            width_bytes: Only records at this hash width, or ``None`` for all widths.

        Returns:
            How many records were deleted.
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
        """Persist one overheard packet from a monitoring run.

        ``packet``-kind observations (the companion's RX packet log) also carry the relay
        path the packet traversed — the raw material of the topology graph (see
        :meth:`packet_paths`).

        An overheard channel frame also keeps the three fields the packet viewer decrypts
        from — the channel-hash fingerprint, the 2-byte MAC, and the ciphertext — lifted
        out of the raw payload so a channel we hold the key for is still readable when the
        feed is later seeded from stored history, and so a channel *datagram*, which the
        library never breaks out at all, can still be named by the key its MAC confirms.

        What the frame addressed is kept the same way, and for the same reason: the
        recipient's key hash, the sender's (a hash, or the whole key an anonymous request
        carries), and a tokened class's own token — an ack's checksum, a trace's tag. All
        three are decoded from the frame body (:mod:`~meshterm.core.frames`) that only a
        live event carries, so a replayed row would otherwise lose everything it had to
        say about itself.

        Args:
            run_id: The owning run.
            obs: The observation to store.
        """
        chan_hash = cipher_mac = crypted = None
        raw = obs.raw if isinstance(obs.raw, dict) else {}
        # The payload class identifies a relayed 'packet' that names no origin node — kept
        # for every packet, not only the classed ones the fields below apply to.
        typename = raw.get("payload_typename") if obs.kind == "packet" else None
        if typename in CHANNEL_CLASSES:
            chan_hash = raw.get("chan_hash")
            cipher_mac = raw.get("cipher_mac")
            crypted = raw.get("crypted")
        elif typename in ADDRESSED_CLASSES:
            # An addressed frame's MAC, in the same column its channel sibling uses: a tag
            # over the encrypted body, so copies of one message share it and the next
            # message's do not. The ciphertext itself is *not* kept — we could never read
            # it, and only the fingerprint is needed to tell one message's frames apart
            # from another's (see :mod:`~meshterm.services.message_paths`).
            cipher_mac = raw.get("cipher_mac")
        # One column each for the three shapes of addressing: the sender is a hash or a
        # whole key (its length says which), the token an ack's checksum or a trace's tag
        # (the payload class says which) — so neither needs a column of its own.
        dest = raw.get("dest_hash") if typename else None
        src = (raw.get("src_key") or raw.get("src_hash")) if typename else None
        tag = (raw.get("ack_crc") or raw.get("trace_tag")) if typename else None
        # A trace's per-hop readings, which live in its header path field where every other
        # class keeps relay hashes — so they get their own column rather than `path`.
        readings = raw.get("trace_snrs") if typename == "TRACE" else None
        trace_snrs = ",".join(f"{v:g}" for v in readings) if readings else None
        # What the frame's `path` means — see the v15 migration. Only a packet row has one.
        route = raw.get("route_typename") if obs.kind == "packet" else None
        # A scoped frame's transport code, and — for a scoped flood — the bytes it was
        # computed over, so its region can be resolved against a name learned later (see
        # the v16 migration). Nothing is kept for a frame that carries no code.
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
        """Return the total number of overheard packets stored across every run.

        Backs the monitor's "total" counter, so it counts all observations ever logged,
        not just the current session's.

        Returns:
            The row count of the ``observations`` table.
        """
        row = self._conn.execute("SELECT COUNT(*) AS n FROM observations").fetchone()
        return int(row["n"]) if row else 0

    def table_counts(self) -> dict[str, int]:
        """Return a row count for every table in the database, by name.

        Read off ``sqlite_master`` rather than a list written out here, which is the whole
        point: a table added to the schema starts being reported the day it lands, and a
        table removed stops, with nothing to keep in step. The one thing this must never
        become is a curated set of the counts somebody thought were interesting — the
        shape of a database is the aggregate, and the interesting one is always the table
        nobody expected to be full.

        Every value is a *count*. Nothing here reads a row, so the result says how much
        history a report's author has without saying a word about who they talk to.

        Returns:
            Table name to row count, alphabetically, SQLite's own internal tables
            excluded (they describe the file format, not this app's data).
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
            # The names come from sqlite_master, so they are this database's own tables
            # and not caller input; quoted anyway, because an identifier cannot be bound.
            row = self._conn.execute(f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()
            counts[name] = int(row["n"]) if row else 0
        return counts

    def failed_run_count(self) -> int:
        """Return how many recorded tool runs ended in an error.

        The single most useful number a reporter can hand over: it says whether this
        install has been failing quietly for weeks or broke for the first time today,
        and it costs one query rather than an interview.

        Returns:
            The number of ``runs`` rows whose status is ``error``.
        """
        row = self._conn.execute("SELECT COUNT(*) AS n FROM runs WHERE status = 'error'").fetchone()
        return int(row["n"]) if row else 0

    def observation_span(self) -> tuple[datetime | None, datetime | None]:
        """Return the first and last times anything was overheard.

        How much mesh this database has actually seen. A report whose span is an hour and
        a report whose span is a year describe different installs, and the difference
        explains a class of "it works here" before anyone else has to ask for it.

        Returns:
            ``(first, last)`` as aware UTC times, or ``(None, None)`` when nothing has
            ever been heard.
        """
        row = self._conn.execute(
            "SELECT MIN(observed_at) AS first, MAX(observed_at) AS last FROM observations"
        ).fetchone()
        if row is None or row["first"] is None:
            return None, None
        return _as_when(row["first"]), _as_when(row["last"])

    def recent_observations(self, *, since: datetime, limit: int = 4000) -> list[Observation]:
        """Return raw stored observations in a recent window, oldest first.

        The dashboard's seed: everything overheard in the window — adverts, telemetry,
        and ``packet`` RX-log rows alike — hydrated back into
        :class:`~meshterm.core.models.Observation` values so the live screen can treat
        stored history and fresh hub events identically. Timestamps compare as strings
        (every ``observed_at`` is written by ``datetime.isoformat`` in UTC), the same
        trick :meth:`channel_stats` relies on.

        Args:
            since: Only observations at or after this time.
            limit: Hard cap on rows (newest kept), so a very busy window stays cheap.

        Returns:
            The window's observations, oldest first (ready to append live events to).
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
        """RX-logged ``packet`` frames inside a closed time window, oldest first.

        The message-paths view's feed: every relayed frame the radio reported in the
        window around a chat message, raw crypto fields included, so a channel frame
        can be decrypted and matched to the message and a direct frame correlated by
        time. Timestamps compare as ISO strings, like every observation query.

        Args:
            start: The window's inclusive start.
            end: The window's inclusive end.
            limit: Hard cap on rows (oldest kept — the window centres on the message).

        Returns:
            The window's ``packet`` observations, oldest first.
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
                continue  # a malformed stray simply doesn't make the window
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
        """Every stored reception of one node, oldest first — its longitudinal record.

        The Time Machine's per-node feed. ``packet``-kind rows are excluded for the
        same reason :meth:`heard_nodes` drops them: their SNR describes the last relay,
        not the node itself, so they would poison a reception timeline.

        Args:
            node: The node's stored id (the 12-hex key prefix observations carry).
            since: Only observations at or after this time, if given.
            limit: Hard cap on rows (newest kept) so an ancient, chatty node stays cheap.

        Returns:
            The node's observations, oldest first.
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
                continue  # a malformed stray simply doesn't make the record
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
        """Per-day activity totals across the whole stored history, oldest first.

        Days are **local** calendar days: ``observed_at`` is stored as UTC ISO-8601,
        but SQLite's ``datetime(…, 'localtime')`` rotates each timestamp into the
        machine's zone (per-instant, so DST-correct) before the day prefix is sliced,
        so a bar breaks at local midnight — not at the UTC-offset hour. Read the same
        database in another zone and the days re-bucket to wherever you are, which is
        the point: history is shown in the viewer's local time.

        Returns:
            ``(day, packets, nodes)`` per day with any activity: the day as a local
            ``YYYY-MM-DD``, every stored observation counted, and the distinct
            identified nodes heard (``packet`` rows excluded — no reliable identity).
        """
        rows = self._conn.execute(
            "SELECT substr(datetime(observed_at, 'localtime'), 1, 10) AS day, "
            "COUNT(*) AS pkts, "
            "COUNT(DISTINCT CASE WHEN kind != 'packet' THEN node END) AS nodes "
            "FROM observations GROUP BY day ORDER BY day"
        ).fetchall()
        return [(row["day"], int(row["pkts"]), int(row["nodes"])) for row in rows]

    def hourly_series(self, since: datetime) -> list[tuple[str, int, int]]:
        """Per-clock-hour activity totals since a time, oldest first (the 24 h window feed).

        The hour-resolution sibling of :meth:`daily_activity`: the same packet and
        distinct-node counts, grouped down to the hour. ``observed_at`` is stored as
        UTC, so SQLite's ``datetime(…, 'localtime')`` rotates each timestamp into the
        machine's zone before the ``YYYY-MM-DDTHH`` key is sliced — the buckets break
        on local hour boundaries in every zone, half-hour offsets included, not on UTC
        edges. A day of history folds into ~24 rows however dense it is. Only hours
        with traffic come back; the caller fills the quiet ones (see
        :func:`~meshterm.ui.timemachine_screen._fill_hours`) so the axis is real
        clock time. The ``since`` filter stays on the raw UTC column — the window is
        an absolute span; only the labelling is local.

        Args:
            since: Only observations at or after this time.

        Returns:
            ``(hour, packets, nodes)`` per active hour: ``hour`` as a local
            ``YYYY-MM-DDTHH``, every observation counted, and the distinct identified
            nodes heard (``packet`` rows excluded from the node count — no reliable
            identity), oldest first.
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
        """Observation counts by local hour of day (0–23) across the stored history.

        The whole-mesh Rhythm chart's feed: every stored observation counted into
        the hour-of-day it arrived. ``observed_at`` is stored as UTC, so SQLite's
        ``datetime(…, 'localtime')`` rotates each timestamp into the machine's zone
        (per-instant, so DST-correct) before the ``HH`` slice, giving a histogram
        already in local hours — no caller rotation needed. The cost stays 24 rows
        however deep the history grows.

        Args:
            since: Only observations at or after this time, if given.

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
                continue  # a malformed stray timestamp simply isn't counted
        return counts

    def rhythm_activity(self, *, since: datetime | None = None) -> list[int]:
        """Observation counts by local minute of day (0–1439) across the history.

        The base grid behind the whole-mesh Rhythm chart: a minute divides every slice
        width the chart may settle on (1/5/10/15/20/30/60 minutes — see the Time
        Machine's slice ladder), so one scan here folds client-side into any of them
        without re-querying. The slot index is ``HH * 60 + MM``, computed in SQL off the
        ``HH``/``MM`` substrings of ``datetime(…, 'localtime')`` (``observed_at`` is
        stored as UTC, rotated per-instant into the machine's zone before slicing, so
        DST-correct), so the histogram lands in local time with no caller rotation. The
        cost stays at most 1440 rows however deep the history grows.

        Args:
            since: Only observations at or after this time, if given.

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
                continue  # a malformed stray timestamp simply isn't counted
        return counts

    def self_transmissions(self, *, since: datetime | None = None) -> list[datetime]:
        """Timestamps of everything this station put on the air, oldest first.

        The own-node counterpart of :meth:`node_observations`: our node is never in the
        reception history — we don't overhear ourselves — so its Time Machine volume and
        rhythm are drawn from what we *sent* instead. Every trace we launched and every
        message we sent unions into one transmission timeline. Both tables stamp their
        rows in UTC ISO-8601 (``created_at``), the same form observations carry, so the
        timeline drops straight into the bucketing the node page's charts already use.

        Args:
            since: Only transmissions at or after this time, if given.

        Returns:
            Transmission timestamps (trace launches + sent messages), oldest first.
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
                continue  # a malformed stray timestamp simply isn't charted
        stamps.sort()
        return stamps

    def self_trace_reach(
        self, *, since: datetime | None = None
    ) -> list[tuple[datetime, bool, float | None, int | None]]:
        """Per-trace reach outcomes, oldest first: ``(when, came_home, min_snr, hop_count)``.

        The measurement behind the own-node page's Reach section. Every trace row is one
        probe of how far we get out: whether it came home, the bottleneck SNR of the path
        it walked (``min_snr`` — the reach's weakest link), and how many hops it crossed.
        Timed-out attempts are kept — a run of failures is itself a reach story — but carry
        no SNR (nothing came back to measure), so the SNR band draws only the ones that did.

        Args:
            since: Only traces at or after this time, if given.

        Returns:
            ``(created_at, success, min_snr, hop_count)`` per trace, oldest first.
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
                continue  # a malformed stray timestamp simply isn't charted
            out.append((when, bool(row["success"]), row["min_snr"], row["hop_count"]))
        return out

    def self_activity_ledger(self, *, since: datetime | None = None) -> SelfActivity:
        """Roll-up tallies of this station's outbound life (see :class:`SelfActivity`).

        One aggregate pass each over the traces, messages, and tx-sample tables — every
        count the own-node page's Ledger line prints, filtered to the window. Hand-composed
        path walks (filed under :data:`PATH_TRACE_TARGET`) still count among the traces we
        launched, but are excluded from the distinct-target tally: they aim at no target.

        Args:
            since: Only activity at or after this time, if given.

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
        """When each node first ever appeared in the history, newest arrivals first.

        The Time Machine's "new arrivals" feed: one grouped scan yields each node's
        earliest observation, labelled by its most recent advertised name (via
        :meth:`node_names`; ``packet`` rows excluded — no reliable identity).

        Args:
            since: Only nodes whose *first* appearance is at or after this time.

        Returns:
            ``(node, latest_name, first_heard)`` triples, most recent arrival first.
        """
        # One streaming pass, unsorted: ``observed_at`` is UTC ISO-8601 and compares
        # correctly as a string, so each node's earliest stamp and latest name are
        # tracked by string comparison, and only the ~one winning stamp per node is
        # parsed — instead of ordering the whole history and walking it row-object by
        # row-object as this used to.
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
        """Stored observation tallies by packet class, optionally windowed.

        The persistent seed of the dashboard's traffic panel: what the recorder has
        heard across sessions, by kind, so the panel opens populated instead of
        counting from zero every launch. A raw ``packet`` frame carrying a parsed
        payload class buckets as ``packet:<TYPENAME>`` (``packet:GRP_TXT``,
        ``packet:TRACE``, …) so the panel can name what the frames were; only a
        class-less frame stays a bare ``packet``. Messages and acks are separate
        event families (not observations) and are counted live on top of this.

        Args:
            since: Only observations at or after this time, if given.

        Returns:
            ``bucket → count`` for every packet class ever stored (in the window).
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
        """Delete observations that aged past the retention window (housekeeping).

        The once-per-session sweep that keeps a permanently-recording database
        bounded: everything the dashboard and Time Machine read lives inside the
        retention window, so rows beyond it are pure weight. Timestamps compare as
        strings, like every other ``observed_at`` filter here.

        Args:
            older_than: Observations strictly before this time are deleted.

        Returns:
            How many rows were removed (0 when the window has no stale rows).
        """
        cur = self._conn.execute(
            "DELETE FROM observations WHERE observed_at < ?", (older_than.isoformat(),)
        )
        self._conn.commit()
        return cur.rowcount if cur.rowcount is not None and cur.rowcount > 0 else 0

    def node_names(self) -> dict[str, str]:
        """The most recent advertised name per node, across everything ever recorded.

        The fill-in-the-blanks source for screens that meet a node id without a name
        (a telemetry-only node in the Time Machine, a relay hash in the dashboard
        feed): whatever name that node *ever* put on the air. One indexed scan;
        ``packet`` rows are excluded because they carry no reliable identity.

        Returns:
            Latest non-empty name keyed by stored node id (the 12-hex key prefix).
        """
        # SQLite's bare-column-with-MAX guarantee: grouped with ``MAX(observed_at)``, the
        # ungrouped ``name`` is taken from the row that supplied the maximum — the latest
        # name per node in one aggregate scan, no Python walk over the whole history.
        # ``NOT INDEXED``, because the planner otherwise walks ``idx_observations_node``
        # row by row (a random-access fetch per entry — measurably slower than the scan).
        rows = self._conn.execute(
            "SELECT node, name, MAX(observed_at) FROM observations NOT INDEXED "
            "WHERE node IS NOT NULL AND name IS NOT NULL AND name != '' "
            "AND kind != 'packet' GROUP BY node"
        ).fetchall()
        return {row["node"]: row["name"] for row in rows}

    def last_heard_by_node(self) -> dict[str, datetime]:
        """The most recent reception per node, stamped by *our* clock.

        The evidence half of a contact's heard time. The device's contact table reports
        ``last_advert``, which the advertising node stamped with its own clock — hearsay a
        wrong RTC can hold days in the past forever — whereas every row here was written
        when *we* received something (see
        :meth:`~meshterm.services.device_state.DeviceState.contacts`, which takes the later
        of the two). ``packet`` rows are excluded for the same reason
        :meth:`heard_nodes` excludes them: a relayed frame tells us we heard its last
        *relay*, not its originator.

        This is :meth:`heard_nodes` reduced to the one column that answers "when last?" —
        the aggregate happens in SQLite and yields one row per node, rather than streaming
        the whole history into Python to build per-node stat objects. That matters because
        every contacts fetch calls this, and the row-building is the expensive half.

        Returns:
            Latest reception time keyed by stored node id (the 12-hex key prefix), aware UTC.
        """
        # ``NOT INDEXED`` for the same reason as :meth:`node_names`: the planner otherwise
        # walks ``idx_observations_node`` with a random-access fetch per row, slower than
        # the plain scan this aggregate wants.
        rows = self._conn.execute(
            "SELECT node, MAX(observed_at) AS last FROM observations NOT INDEXED "
            "WHERE node IS NOT NULL AND kind != 'packet' GROUP BY node"
        ).fetchall()
        return {row["node"]: datetime.fromisoformat(row["last"]) for row in rows}

    def heard_nodes(self, *, since: datetime | None = None) -> list[HeardNode]:
        """Aggregate stored observations into per-node reception statistics.

        Spans every monitoring run (optionally limited to recent history), so the result
        is a longitudinal view of which nodes have been heard, how strongly, and where —
        the substrate for the monitor summary and the coverage map. ``packet``-kind rows
        are excluded: their SNR describes our link to the packet's *last relay*, not to
        the originating node, so folding them in would misattribute reception quality
        (they feed the topology graph instead — see :meth:`packet_paths`).

        Args:
            since: Only include observations at or after this time, if given.

        Returns:
            One :class:`HeardNode` per distinct node, ordered by most-recently heard.
        """
        sql = (
            "SELECT node, public_key, name, node_type, snr, rssi, lat, lon, observed_at "
            "FROM observations WHERE kind != 'packet'"
        )
        params: list[Any] = []
        if since is not None:
            sql += " AND observed_at >= ?"
            params.append(since.isoformat())

        # One streaming pass, aggregating in place. This is a whole-history scan on the
        # open path of half the screens (Contacts, the map, a trace's target list), so it
        # never materializes per-row Observation objects or parses per-row timestamps —
        # ``observed_at`` is UTC ISO-8601, which compares correctly as a *string*, so each
        # "most recent X" is tracked by string comparison and only the one winning stamp
        # per node is parsed at the end. ``>=`` on every comparison keeps the old
        # sort-then-walk-backwards tie behaviour: among equal stamps, the later row wins.
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
        """Gather every scoring signal for a set of nodes, in a fixed number of scans.

        The evidence behind the Contacts sweep (see
        :mod:`~meshterm.core.contact_score`). Deliberately *set-based*: a table of several
        hundred contacts is answered by five grouped passes over the history rather than by
        five queries per contact, so the sweep's cost tracks the size of the database and
        not the size of the table being swept.

        Each pass fills one part of :class:`~meshterm.core.contact_score.ContactSignals`:

        * **Reception** — first heard, last heard, and the transmission tally, from
          ``observations``. ``packet`` rows are excluded exactly as :meth:`heard_nodes`
          excludes them, so the tally means "heard *from* this node" and stays the same
          number the contact list's ``PKTS`` lane shows.
        * **Hops** — the median relay count of packets seen originating from the node, from
          the ``packet`` rows this time, since those are the only ones carrying a path (see
          :meth:`packet_paths`). Median rather than minimum: one lucky direct reception
          shouldn't make a four-hop node read as a neighbour.
        * **Direct messages** — totals, our own outbound share, and the age of the latest,
          matched by *prefix*: ``messages.peer`` holds whatever width the wire addressed,
          which is not always the 12 hex an observation keys on (see
          :meth:`last_message_by_peer`).
        * **Channel posts** — attributed by the ``Name: `` prefix a channel message
          carries, because the wire gives a channel frame no sender key at all. A name held
          by two contacts attributes to *neither*: the caller marks those unattributed so
          the score reads them as unknown rather than as silence.

        Args:
            nodes: The 12-hex canonical node ids to gather for. Anything absent from the
                history simply comes back with an empty record, which the score protects
                rather than punishes.

        Returns:
            One :class:`~meshterm.core.contact_score.ContactSignals` per requested node,
            keyed by that id. Name-keyed channel attribution is *not* filled in here (the
            repository knows nothing about which name belongs to which contact) — see
            :meth:`channel_post_counts`.
        """
        from ..core.contact_score import ContactSignals

        wanted = {n for n in nodes if n}
        if not wanted:
            return {}
        now = utcnow()

        def age_days(iso: str | None) -> float | None:
            """Days from a stored ISO stamp to now, or ``None`` for an unparseable one."""
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

        # -- hops: the median relay count of packets originating from each node -------
        # Bounded to the most recent frames, like :meth:`packet_paths`, and for the same
        # two reasons: this is the one pass that builds a row object per packet rather than
        # aggregating in SQL, and a year-old hop count is evidence about a topology that no
        # longer exists. Newest first, so the cap keeps the readings worth having.
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
                # Either side may be the shorter: the wire addresses at whatever width it
                # likes, so a stored 6-hex peer and a 12-hex node id are the same node when
                # one is a prefix of the other.
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
        """How many channel messages each *name* has posted, lowercased.

        A channel frame carries no sender key — senders identify themselves by prefixing
        the text with ``Name: `` (see
        :func:`~meshterm.core.channels.split_channel_sender`), so this is the only
        attribution available and it is by display name alone. The caller is responsible
        for refusing to trust a name two contacts share; this method only counts.

        Our own outbound posts are excluded: they say nothing about anyone else.

        Returns:
            Post counts keyed by lowercased sender name. Messages whose text carries no
            usable name prefix contribute nothing.
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
        """Persist one chat message (sent or received).

        Args:
            msg: The message to store. Its ``peer`` is normalized to lowercase so a
                direct conversation queries back consistently.
            run_id: The owning background ``chat`` run, if any (inbound messages log to
                one; outbound sends may not).

        Returns:
            The new message's primary key.
        """
        peer = msg.peer.lower() if msg.peer else None
        cur = self._conn.execute(
            "INSERT INTO messages "
            "(run_id, outbound, is_channel, channel_id, channel_idx, peer, peer_name, text, "
            "snr, acked, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def update_chat_ack(self, message_id: int, acked: bool) -> None:
        """Update one outbound message's delivery acknowledgement (used on retry).

        Args:
            message_id: The ``messages`` row to update.
            acked: The new delivery state — ``True`` acknowledged, ``False`` not.
        """
        self._conn.execute("UPDATE messages SET acked = ? WHERE id = ?", (int(acked), message_id))
        self._conn.commit()

    def delete_chat_history(self, peer: str | None) -> int:
        """Delete every stored message of one direct conversation.

        The chat picker's per-contact history delete: removes only that peer's direct
        messages — channel history and every other conversation stay untouched. The
        peer matches how :meth:`record_chat_message` stores it (lowercased key prefix).

        Args:
            peer: The contact's key prefix (the direct conversation's identity).

        Returns:
            How many messages were deleted.
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
    ) -> list[ChatMessage]:
        """Return a conversation's most recent messages, oldest-first.

        Args:
            is_channel: Whether to load a channel conversation.
            channel_id: The channel's slot-independent identity (channel conversations).
            peer: The contact key prefix (direct conversations).
            limit: Maximum number of messages to return.

        Returns:
            The messages in chronological order (ready to render as a transcript).
        """
        if is_channel:
            where, params = "is_channel = 1 AND channel_id = ?", [channel_id]
        else:
            where, params = "is_channel = 0 AND peer = ?", [(peer or "").lower()]
        rows = self._conn.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        return [self._row_to_chat(row) for row in reversed(rows)]

    def last_chat_messages(self) -> dict[str, ChatMessage]:
        """Return the latest message per conversation, keyed by conversation key.

        Backs the conversation picker's preview snippets. One row per distinct
        conversation, taken as the highest-id (most recent) message in each.

        Returns:
            A mapping of :func:`~meshterm.core.models.conversation_key` to its latest
            :class:`ChatMessage`.
        """
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE id IN ("
            "  SELECT MAX(id) FROM messages GROUP BY "
            "  CASE WHEN is_channel = 1 THEN 'chan:' || channel_id "
            "       ELSE 'dm:' || peer END)"
        ).fetchall()
        return {msg.key: msg for msg in (self._row_to_chat(r) for r in rows)}

    def last_message_by_peer(self) -> dict[str, datetime]:
        """The most recent *inbound* direct message per peer, stamped by our clock.

        The other half of the heard-time evidence (see :meth:`last_heard_by_node`). A direct
        message received from a node is, in the app's own lexicon, hearing that node — but
        it arrives as a ``CONTACT_MSG_RECV`` event and is stored here, never as an
        observation, so nothing in the reception history knows about it. It is deliberately
        *not* recorded as an observation instead: a routed message's SNR describes the link
        to its last relay, which is exactly why :meth:`heard_nodes` excludes ``packet``
        rows, and counting it would inflate a lane that means overheard traffic.

        Outbound messages are excluded — sending to a node is not hearing from it.

        Returns:
            Latest inbound-message time keyed by stored peer prefix (lowercased, as
            :meth:`record_chat_message` writes it), aware UTC. The prefix width is whatever
            the wire addressed, so callers match it as a prefix, not by equality.
        """
        rows = self._conn.execute(
            "SELECT peer, MAX(created_at) AS last FROM messages "
            "WHERE is_channel = 0 AND outbound = 0 AND peer IS NOT NULL GROUP BY peer"
        ).fetchall()
        return {row["peer"]: datetime.fromisoformat(row["last"]) for row in rows}

    def direct_message_bounds(
        self, peer: str | None, when: datetime, *, outbound: bool
    ) -> tuple[datetime | None, datetime | None]:
        """The times of the direct messages either side of ``when`` in one conversation.

        What bounds the message-paths search for a direct message (see
        :mod:`~meshterm.services.message_paths`). A direct frame is encrypted, so the log
        cannot say which message it carried; a fixed window around the message is therefore
        the only bound available — and at conversational pace it is far too wide. Nine
        messages of one real exchange fell inside a single ±90 s window, so every one of
        them listed all nine messages' frames as its own.

        The messages around it are the natural edges: a frame logged after the *next*
        message was composed belongs to that one, not this one. The caller clamps its window
        to the midpoint of each gap, so the bound tightens exactly as the conversation
        speeds up.

        Only messages travelling the **same way** count as neighbours, because the two
        directions are stamped by two different clocks: our own sends carry ours, taken as
        they leave, while a received message carries the *sender's*, which may drift from
        ours by minutes. Ordering an inbound message against an outbound one therefore
        compares two clocks and can bound a message by an edge that, in its own clock,
        hasn't happened yet — which is exactly how the first cut of this clamp gave a
        received message an empty window. Within one direction the stamps are consistent,
        so the neighbours mean what they say.

        Args:
            peer: The contact's key prefix, as :meth:`record_chat_message` stores it.
            when: The message's own time.
            outbound: The direction to look along — the message's own.

        Returns:
            ``(previous, next)`` same-direction message times, either of which is ``None``
            when this message is the first or last of its side of the conversation.
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
        """Aggregate stored channel messages into per-channel statistics.

        Backs the channel manager's list lanes: each configured channel's row shows its
        total message count, the age of its last message, and an activity sparkline over
        the trailing :data:`ACTIVITY_WINDOW` — whose :data:`ACTIVITY_BUCKETS`-column
        histogram is built here (the sparkline draws its newest
        :data:`ACTIVITY_DRAWN_BUCKETS`; the rest feeds the shared scaling peak). Two
        passes, each grouped/filtered in SQL so the cost tracks message volume, not
        channel count: an aggregate for the all-time totals, then the window's individual
        timestamps, bucketed in Python (a few hours of chatter, so the row set stays small).

        Timestamps are compared as strings: every ``created_at`` is written by
        ``utcnow().isoformat()`` (a fixed-width UTC ISO-8601 form), so lexicographic order
        *is* chronological order and the window cutoff needs no per-row parsing.
        Messages predating identity-keyed history (a ``NULL`` ``channel_id``; see
        :meth:`backfill_channel_ids`) have no channel to be counted under and are skipped.

        Returns:
            A mapping of channel identity to its :class:`ChannelStats`. Channels with no
            stored messages simply have no entry.
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
                # Bucket by *age* so the histogram comes out newest-first (bucket 0 holds
                # the current five minutes) — the order the sparkline widget expects.
                idx = min(ACTIVITY_BUCKETS - 1, int((now - created_at) / bucket_span))
            except (TypeError, ValueError):
                continue  # a malformed/naive stray simply doesn't land in a bucket
            histogram = histograms.setdefault(row["channel_id"], [0] * ACTIVITY_BUCKETS)
            histogram[max(0, idx)] += 1

        stats: dict[str, ChannelStats] = {}
        for row in rows:
            try:
                last_at = datetime.fromisoformat(row["last_at"])
            except (TypeError, ValueError):
                last_at = None  # a malformed stray must not hide the channel's counts
            histogram = tuple(histograms.get(row["channel_id"], [0] * ACTIVITY_BUCKETS))
            stats[row["channel_id"]] = ChannelStats(
                total=int(row["total"]),
                recent=sum(histogram),
                last_at=last_at,
                histogram=histogram,
            )
        return stats

    def backfill_channel_ids(self, mapping: dict[int, str]) -> int:
        """Give legacy channel messages an identity, keyed by the slot they were stored on.

        Messages written before channel history was keyed by identity have a ``NULL``
        ``channel_id``. There is no record of which channel occupied each slot back then, so
        the best available guess is the channel *currently* at that slot. ``mapping`` maps a
        slot index to the identity of the channel now there; only rows still missing an
        identity are touched, so this is safe to run on every startup.

        Args:
            mapping: Slot index to the current channel identity at that slot.

        Returns:
            The number of legacy rows given an identity.
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

    # -- persisted UI state -----------------------------------------------------

    def get_map_view(self) -> tuple[float, float, int] | None:
        """Return the last saved map viewport as ``(center_lat, center_lon, zoom)``.

        Lets the interactive map reopen exactly where the user left it. Returns ``None``
        when no view has been saved yet, or a stored value can't be parsed (treated as
        absent rather than an error, so a corrupt row just refits to the nodes).
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
        """Persist the map viewport so the next session reopens on the same spot.

        Args:
            lat: Latitude at the centre of the view.
            lon: Longitude at the centre of the view.
            zoom: Display zoom level.
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
        )
