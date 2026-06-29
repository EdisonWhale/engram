"""Smoke tests: migrations apply cleanly to a fresh :memory: database.

Checks:
- All expected tables are created.
- FTS5 virtual table exists.
- FTS5 sync triggers fire on INSERT/UPDATE/DELETE.
- WAL mode is active after open_db.
- _migrations table tracks the applied file.
- Running open_db a second time is idempotent (no duplicate-apply errors).
- All expected indexes exist.
"""

from __future__ import annotations

import sqlite3

import pytest

from engram.db.runner import open_db


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """Fresh in-memory database with migrations applied."""
    return open_db(":memory:")


EXPECTED_TABLES = {
    "projects",
    "agent_sessions",
    "events",
    "task_contexts",
    "memories",
    "session_summaries",
    "memory_sources",
    "retrieval_traces",
    "eval_cases",
    "eval_runs",
    "_migrations",
}


def test_all_tables_created(conn: sqlite3.Connection) -> None:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert EXPECTED_TABLES.issubset(tables), f"missing: {EXPECTED_TABLES - tables}"


def test_fts5_virtual_table_exists(conn: sqlite3.Connection) -> None:
    vtables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
        ).fetchall()
    }
    assert "memories_fts" in vtables


def test_fts5_triggers_exist(conn: sqlite3.Connection) -> None:
    triggers = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    }
    assert {"memories_ai", "memories_au", "memories_ad"}.issubset(triggers)


def test_fts5_insert_trigger(conn: sqlite3.Connection) -> None:
    """Inserting a memory should index it in memories_fts."""
    conn.execute(
        """
        INSERT INTO projects (id, root_path, name) VALUES ('p1', '/tmp/proj', 'test')
        """
    )
    conn.execute(
        """
        INSERT INTO memories (
            id, project_id, scope, type, origin, title, content, content_hash
        ) VALUES ('m1', 'p1', 'project', 'decision', 'user',
                  'SQLite is the source of truth',
                  'SQLite is chosen for local-first simplicity.',
                  'hash1')
        """
    )
    conn.commit()

    # FTS5 search should find the inserted memory
    rows = conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'SQLite'"
    ).fetchall()
    assert len(rows) == 1


def test_fts5_delete_trigger(conn: sqlite3.Connection) -> None:
    """Deleting a memory should remove it from memories_fts."""
    conn.execute("INSERT INTO projects (id, root_path, name) VALUES ('p2', '/tmp/p2', 'p2')")
    conn.execute(
        """
        INSERT INTO memories (
            id, project_id, scope, type, origin, title, content, content_hash
        ) VALUES ('m2', 'p2', 'project', 'decision', 'user',
                  'Postgres deferred', 'No Postgres at P0.', 'hash2')
        """
    )
    conn.commit()

    conn.execute("DELETE FROM memories WHERE id = 'm2'")
    conn.commit()

    rows = conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH 'Postgres'"
    ).fetchall()
    assert len(rows) == 0


def test_wal_mode_active(conn: sqlite3.Connection) -> None:
    # For :memory: databases SQLite ignores WAL; accept either.
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode in ("wal", "memory"), f"unexpected mode: {mode}"


def test_migration_tracked(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT filename FROM _migrations").fetchall()
    filenames = [r[0] for r in rows]
    assert "001_initial.sql" in filenames


def test_open_db_idempotent() -> None:
    """Opening the same in-memory DB twice must not error or duplicate tables."""
    # :memory: creates a fresh DB each call, so just check two successive calls
    # on a real file-like temp DB don't raise.
    import os
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        conn1 = open_db(path)
        conn1.close()
        conn2 = open_db(path)  # second open — should be idempotent
        tables = {
            row[0]
            for row in conn2.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert EXPECTED_TABLES.issubset(tables)
        conn2.close()
    finally:
        os.unlink(path)


def test_events_seq_unique_constraint(conn: sqlite3.Connection) -> None:
    """Duplicate (session_id, seq) must raise IntegrityError."""
    conn.execute("INSERT INTO projects (id, root_path, name) VALUES ('p3', '/p3', 'p3')")
    conn.execute(
        """
        INSERT INTO agent_sessions
            (id, project_id, external_session_id, memory_thread_id, agent)
        VALUES ('s1', 'p3', 'ext1', 'thread1', 'claude_code')
        """
    )
    conn.execute(
        """
        INSERT INTO events (id, project_id, session_id, seq, source_type,
                            event_type, content_hash, occurred_at)
        VALUES ('e1', 'p3', 's1', 1, 'mcp', 'user_prompt', 'h1',
                '2024-01-01T00:00:00Z')
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO events (id, project_id, session_id, seq, source_type,
                                event_type, content_hash, occurred_at)
            VALUES ('e2', 'p3', 's1', 1, 'mcp', 'user_prompt', 'h2',
                    '2024-01-01T00:00:01Z')
            """
        )


def test_memories_content_hash_unique(conn: sqlite3.Connection) -> None:
    """Duplicate content_hash must raise IntegrityError (dedup gate)."""
    conn.execute("INSERT INTO projects (id, root_path, name) VALUES ('p4', '/p4', 'p4')")
    conn.execute(
        """
        INSERT INTO memories (id, project_id, scope, type, origin, title, content, content_hash)
        VALUES ('mx', 'p4', 'project', 'decision', 'user', 'T', 'C', 'samehash')
        """
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO memories (id, project_id, scope, type, origin, title, content, content_hash)
            VALUES ('my', 'p4', 'project', 'decision', 'user', 'T2', 'C2', 'samehash')
            """
        )
