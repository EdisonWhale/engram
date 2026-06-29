"""Database connection management and migration runner.

The public surface here is intentionally small: `open_db` is the single
entry point.  Everything else is implementation detail.

Design decisions:
- `executescript` is used for migration SQL because SQLite's Python driver
  does not support multi-statement `execute`.  It commits any open transaction
  first, which is correct for DDL-only migrations.
- Migrations are tracked in `_migrations` (filename as key) so re-running
  `open_db` on an existing database is idempotent.
- `check_same_thread=False` is intentional: asyncio code (the MCP server)
  runs on one event-loop thread and accesses the connection from there.
  Serialisation is the caller's responsibility; SQLite WAL mode handles
  concurrent readers from separate connections.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def open_db(path: str) -> sqlite3.Connection:
    """Open (or create) the Engram SQLite database at *path*, apply any pending
    migrations, and return a ready connection.

    Pass ``":memory:"`` in tests.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL mode and foreign-key enforcement are connection-level settings;
    # set them on every open even though the migration SQL also sets WAL.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _apply_migrations(conn)
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply all *.sql files in the migrations/ directory that have not yet been run.

    Migration files are applied in lexicographic (filename) order, which is
    consistent with the ``NNN_name.sql`` naming convention.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            filename   TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
        """
    )
    conn.commit()

    for migration_file in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        filename = migration_file.name
        already_applied = conn.execute(
            "SELECT 1 FROM _migrations WHERE filename = ?", (filename,)
        ).fetchone()
        if already_applied:
            continue

        # executescript issues an implicit COMMIT first, then runs the SQL,
        # then auto-commits each statement.  Correct for DDL-only files.
        conn.executescript(migration_file.read_text())

        conn.execute("INSERT INTO _migrations (filename) VALUES (?)", (filename,))
        conn.commit()
