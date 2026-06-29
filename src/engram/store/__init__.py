"""Storage interfaces and SQLite implementations.

Public API:
    EventStore   — Protocol for raw capture: projects, sessions, events.
    MemoryStore  — Protocol for processed data: memories, task contexts, etc.
    VectorStore  — Protocol for vector similarity search (interface only; no P0 impl).
    SQLiteEventStore
    SQLiteMemoryStore

Usage::

    from engram.db.runner import open_db
    from engram.store import SQLiteEventStore, SQLiteMemoryStore

    conn = open_db("~/.engram/engram.db")
    events = SQLiteEventStore(conn)
    memories = SQLiteMemoryStore(conn)
"""

from engram.store.base import EventStore, MemoryStore, VectorStore
from engram.store.sqlite_store import SQLiteEventStore, SQLiteMemoryStore

__all__ = [
    "EventStore",
    "MemoryStore",
    "VectorStore",
    "SQLiteEventStore",
    "SQLiteMemoryStore",
]
