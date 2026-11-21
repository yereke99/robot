"""SQLite history of every bid the bot ran.

Synchronous sqlite3 calls run in the default executor — sqlite reads are
microseconds for our row count, not worth aiosqlite. WAL mode so concurrent
read while writing doesn't lock.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS bids (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  username TEXT,
  announce_id TEXT NOT NULL,
  tender_name TEXT,
  tender_type TEXT,
  lot_ids TEXT NOT NULL,
  price INTEGER NOT NULL,
  app_id TEXT,
  status TEXT NOT NULL,
  parent_bid_id INTEGER REFERENCES bids(id),
  qualification_source TEXT,
  error TEXT,
  started_at REAL NOT NULL,
  finished_at REAL,
  elapsed_s REAL,
  schema_version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_bids_user ON bids(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_bids_announce ON bids(announce_id, status);
"""


class HistoryDB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            path,
            isolation_level=None,         # autocommit
            check_same_thread=False,      # we serialize via the GIL + short calls
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)

    def insert_pending(
        self,
        *,
        user_id: int,
        chat_id: int,
        username: Optional[str],
        announce_id: str,
        tender_name: Optional[str],
        tender_type: Optional[str],
        lot_ids: list[int],
        price: int,
        parent_bid_id: Optional[int] = None,
        qualification_source: Optional[str] = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO bids (
                user_id, chat_id, username, announce_id, tender_name, tender_type,
                lot_ids, price, status, parent_bid_id, qualification_source, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (
                user_id, chat_id, username, announce_id, tender_name, tender_type,
                ",".join(str(x) for x in lot_ids), price,
                parent_bid_id, qualification_source, time.time(),
            ),
        )
        return int(cur.lastrowid)

    def finish(
        self,
        bid_id: int,
        *,
        status: str,
        app_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        now = time.time()
        self._conn.execute(
            """UPDATE bids
               SET status = ?, app_id = COALESCE(?, app_id),
                   error = ?, finished_at = ?,
                   elapsed_s = ? - started_at
               WHERE id = ?""",
            (status, app_id, error, now, now, bid_id),
        )

    def last_bids(self, user_id: Optional[int] = None, limit: int = 20) -> list[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        if user_id is None:
            cur = self._conn.execute(
                "SELECT * FROM bids ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM bids WHERE user_id = ? ORDER BY started_at DESC LIMIT ?",
                (user_id, limit),
            )
        return list(cur.fetchall())

    def last_successful(
        self, user_id: int, tender_type: Optional[str] = None
    ) -> Optional[sqlite3.Row]:
        """For auto qualification-source: most recent submitted/dry-run-ok bid
        with the same tender_type, if known.
        """
        self._conn.row_factory = sqlite3.Row
        if tender_type:
            cur = self._conn.execute(
                """SELECT * FROM bids WHERE user_id = ? AND tender_type = ?
                   AND status IN ('submitted', 'dry_run_ok')
                   ORDER BY started_at DESC LIMIT 1""",
                (user_id, tender_type),
            )
        else:
            cur = self._conn.execute(
                """SELECT * FROM bids WHERE user_id = ?
                   AND status IN ('submitted', 'dry_run_ok')
                   ORDER BY started_at DESC LIMIT 1""",
                (user_id,),
            )
        row = cur.fetchone()
        return row

    def mark_interrupted_pending(self, older_than_s: float = 600.0) -> int:
        """Sweep pending rows older than `older_than_s` and mark as interrupted.

        Called once at startup so /history doesn't show forever-pending rows
        after a bot restart. Returns rowcount.
        """
        cutoff = time.time() - older_than_s
        cur = self._conn.execute(
            "UPDATE bids SET status='interrupted', finished_at=? WHERE status='pending' AND started_at < ?",
            (time.time(), cutoff),
        )
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()
