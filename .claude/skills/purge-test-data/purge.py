"""Purge simulator/test pollution from the default MeshTerm database.

``meshterm --mock`` (and headless test drives against the simulator) write to the
same ``~/.meshterm/meshterm.db`` as real radio sessions, so mock nodes leak into
the history that powers the mesh walk, the dashboard, and the trace suggestions. This
script removes exactly the rows carrying the simulator's fingerprints — the
:class:`MockDevice` identities in ``meshterm/core/connection.py`` — and nothing
else. Real rows never match: the fingerprints are fixed synthetic keys, the
``hopN`` placeholder hashes only the simulator emits, and the mock contact names.

A timestamped ``.bak`` copy of the database is written first; ``--dry-run`` only
reports what would go.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

DB = Path.home() / ".meshterm" / "meshterm.db"

#: The simulator's key prefixes: its four contacts and its all-zero self key.
#: Kept in sync with MockDevice in meshterm/core/connection.py. Eight hex digits
#: each — long enough that no real key plausibly collides.
MOCK_PREFIXES = ("a1b2c3d4", "b2c3d4e5", "c3d4e5f6", "d4e5f6a7", "00000000")

#: The simulator's display names (contacts, its own node, and demo trace targets).
MOCK_NAMES = (
    "Yagi-Repeater", "Local-Repeater", "Observer-Bot", "Alice", "MockCompanion",
    "hub-1", "relay-1", "relay-2",
)


def _mock_hash(value: str | None) -> bool:
    """Whether a stored hash/prefix carries a simulator fingerprint.

    Matches only the unmistakable forms — the ``hopN`` placeholders and hashes
    carrying a full 8-hex mock prefix. A short real hop (``a1`` traced at 1-byte
    width) must never match, so no reverse-prefix matching: a stray narrow mock
    hop left behind is better than a purged real one.
    """
    if not value:
        return False
    value = value.lower().removeprefix("0x")
    return value.startswith("hop") or any(value.startswith(p) for p in MOCK_PREFIXES)


def purge(db: Path, *, dry_run: bool) -> dict[str, int]:
    """Delete every mock-fingerprinted row; returns per-table deletion counts."""
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys = ON")
    cur = con.cursor()
    counts: dict[str, int] = {}

    def delete(table: str, where: str, params: tuple = ()) -> None:
        n = cur.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", params
        ).fetchone()[0]
        counts[table] = counts.get(table, 0) + n
        if not dry_run and n:
            cur.execute(f"DELETE FROM {table} WHERE {where}", params)

    names = ",".join("?" for _ in MOCK_NAMES)

    # Traces: any trace that walked a mock hop, or targeted a mock name.
    hop_rows = cur.execute("SELECT DISTINCT trace_id, node FROM trace_hops").fetchall()
    mock_traces = {tid for tid, node in hop_rows if _mock_hash(node)}
    target_rows = cur.execute(f"SELECT id FROM traces WHERE target IN ({names})",
                              MOCK_NAMES).fetchall()
    mock_traces |= {tid for (tid,) in target_rows}
    if mock_traces:
        ids = ",".join(str(t) for t in mock_traces)
        delete("trace_hops", f"trace_id IN ({ids})")
        delete("traces", f"id IN ({ids})")
    else:
        counts.setdefault("trace_hops", 0)
        counts.setdefault("traces", 0)

    # Observations and messages carrying a mock node or name.
    obs = cur.execute("SELECT id, node, name FROM observations").fetchall()
    mock_obs = [oid for oid, node, name in obs
                if _mock_hash(node) or (name in MOCK_NAMES)]
    delete("observations", f"id IN ({','.join(map(str, mock_obs)) or '0'})")
    msgs = cur.execute("SELECT id, peer, peer_name FROM messages").fetchall()
    mock_msgs = [mid for mid, peer, peer_name in msgs
                 if _mock_hash(peer) or (peer_name in MOCK_NAMES)]
    delete("messages", f"id IN ({','.join(map(str, mock_msgs)) or '0'})")

    # Sweep tooling under mock targets/identities.
    delete("tx_samples", f"target IN ({names})", MOCK_NAMES)
    delete("path_candidates", f"target IN ({names})", MOCK_NAMES)
    nbr = cur.execute("SELECT id, repeater, neighbour FROM neighbour_reports").fetchall()
    mock_nbr = [nid for nid, rep, ngh in nbr if _mock_hash(rep) or _mock_hash(ngh)]
    delete("neighbour_reports", f"id IN ({','.join(map(str, mock_nbr)) or '0'})")

    # The places that the place finder stored for mock nodes (schema 23 and later).
    has_places = cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'node_places'"
    ).fetchone()
    if has_places:
        place_rows = cur.execute("SELECT node FROM node_places").fetchall()
        mock_places = [node for (node,) in place_rows if _mock_hash(node)]
        marks = ",".join("?" for _ in mock_places) or "''"
        delete("node_places", f"node IN ({marks})", tuple(mock_places))

    # Runs that now hold nothing and were mock-shaped to begin with: trace runs on
    # mock targets/specs. Background monitor/chat run rows are left alone — they
    # are session bookkeeping shared with real use, and empty ones are harmless.
    delete(
        "runs",
        "tool = 'trace'"
        f" AND json_extract(args_json, '$.target') IN ({names})"
        " AND id NOT IN (SELECT run_id FROM traces)"
        " AND id NOT IN (SELECT run_id FROM path_candidates)",
        MOCK_NAMES,
    )

    if not dry_run:
        con.commit()
        cur.execute("VACUUM")
    con.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be deleted, touch nothing")
    parser.add_argument("--db", type=Path, default=DB, help="database path")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"no database at {args.db} — nothing to do")
        return
    if not args.dry_run:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = args.db.with_name(f"{args.db.name}.bak-{stamp}")
        shutil.copy2(args.db, backup)
        print(f"backup: {backup}")

    counts = purge(args.db, dry_run=args.dry_run)
    verb = "would delete" if args.dry_run else "deleted"
    for table in sorted(counts):
        print(f"{verb} {counts[table]:>5}  {table}")


if __name__ == "__main__":
    main()
