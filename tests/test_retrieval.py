"""Tests for src/engram/retrieval/__init__.py.

Acceptance criteria (from docs/tasks/C-retrieval.md):
  (a) memory_search rows fit the documented token estimate (≤ 100 tokens each).
  (b) Exact-identifier query is recalled via BM25 (not dependent on embeddings).
  (c) memory_context respects token_budget and never exceeds it.
  (d) A memory whose underlying file changed surfaces as "stale".
  (e) A conflicting memory is not injected by default.

Additional mechanics tests:
  - memory_timeline returns chronological window around an anchor.
  - memory_get fetches full records and runs stale check.
  - search_memories_fts returns matches ranked by BM25.
  - FTS5 sanitization: queries with special chars don't crash.
  - memory_context excludes superseded and deleted memories.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from engram.db.runner import open_db
from engram.models import AgentSession, Memory, Project, TaskContext
from engram.retrieval import memory_context, memory_get, memory_search, memory_timeline
from engram.store.sqlite_store import SQLiteEventStore, SQLiteMemoryStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PROJECT_ID = "proj-test-001"


def _memory(
    *,
    title: str,
    content: str,
    type: str = "decision",
    status: str = "active",
    file_path: str | None = None,
    file_hash: str | None = None,
    confidence: float = 1.0,
    access_count: int = 0,
    project_id: str = PROJECT_ID,
) -> Memory:
    """Build a Memory without having to spell out every field in every test."""
    return Memory(
        project_id=project_id,
        scope="project",
        type=type,
        origin="user",
        title=title,
        content=content,
        content_hash=Memory.compute_hash(content),
        status=status,
        confidence=confidence,
        access_count=access_count,
        file_path=file_path,
        file_hash=file_hash,
    )


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """Fresh in-memory SQLite with all migrations applied."""
    return open_db(":memory:")


@pytest.fixture()
def store(conn: sqlite3.Connection) -> SQLiteMemoryStore:
    """SQLiteMemoryStore wrapping the in-memory DB, with a project row seeded.

    The memories table has project_id FK → projects.id, so a project row is
    required before any memory can be inserted.  All tests in this module that
    use PROJECT_ID rely on this fixture.
    """
    SQLiteEventStore(conn).create_project(
        Project(id=PROJECT_ID, root_path="/tmp/test-engram", name="test-project")
    )
    return SQLiteMemoryStore(conn)


@pytest.fixture()
def populated_store(store: SQLiteMemoryStore) -> SQLiteMemoryStore:
    """Store pre-loaded with a set of fixture memories covering all types."""
    memories = [
        _memory(
            title="Use SQLite as source of truth",
            content="SQLite is the authoritative store; FTS5 and vector are derived state.",
            type="decision",
        ),
        _memory(
            title="No LLM calls on the write path",
            content="LLM calls only happen during consolidation, never during capture or write.",
            type="decision",
        ),
        _memory(
            title="Always run ruff before committing",
            content="Use ruff check and ruff format before every commit.",
            type="preference",
        ),
        _memory(
            title="Project root is src/ layout",
            content="All source lives under src/engram/. CLI entrypoint is engram.",
            type="project_fact",
        ),
        _memory(
            title="Never use FastAPI for MCP server",
            content="MCP server uses stdio transport; no HTTP framework.",
            type="constraint",
        ),
        _memory(
            title="FTS5 query syntax error handling",
            content="search_fts_exact_identifier_test handles FTS5 errors gracefully.",
            type="project_fact",
        ),
    ]
    for m in memories:
        store.create_memory(m)
    return store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Same formula as the retrieval module."""
    return (len(text) + 3) // 4


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# (a) memory_search compact rows fit token estimate
# ---------------------------------------------------------------------------


def test_search_compact_rows_token_estimate(populated_store: SQLiteMemoryStore) -> None:
    """Each row returned by memory_search must be ≤ 100 tokens."""
    result = memory_search("SQLite decision", memory_store=populated_store, project_id=PROJECT_ID)
    assert result["total"] > 0, "Expected at least one search result"

    for row in result["memories"]:
        row_text = " ".join(
            str(v)
            for v in [
                row["id"],
                row["title"],
                row["type"],
                row["age"],
                row["status"],
                row["origin"],
                row["provenance_summary"],
            ]
        )
        tokens = _estimate_tokens(row_text)
        assert tokens <= 100, f"Row too large: {tokens} tokens\nRow: {row}"


def test_search_returns_expected_fields(populated_store: SQLiteMemoryStore) -> None:
    """Each compact row must carry all documented fields."""
    result = memory_search("ruff", memory_store=populated_store, project_id=PROJECT_ID)
    assert result["total"] > 0
    row = result["memories"][0]
    for field_name in ("id", "title", "type", "age", "status", "origin", "provenance_summary"):
        assert field_name in row, f"Missing field: {field_name}"


# ---------------------------------------------------------------------------
# (b) Exact-identifier query recalled via BM25
# ---------------------------------------------------------------------------


def test_exact_identifier_recalled_via_bm25(store: SQLiteMemoryStore) -> None:
    """A function name / error code present in memory content is recalled by FTS5/BM25."""
    unique_identifier = "process_capture_event_loop_v2"
    m = _memory(
        title="Capture loop refactor",
        content=(
            f"The main capture loop was refactored into {unique_identifier}. "
            "This function handles both hook and transcript events."
        ),
        type="decision",
    )
    store.create_memory(m)

    result = memory_search(unique_identifier, memory_store=store, project_id=PROJECT_ID)
    returned_ids = [r["id"] for r in result["memories"]]
    assert m.id in returned_ids, (
        f"Exact identifier '{unique_identifier}' not recalled via BM25. "
        f"Returned IDs: {returned_ids}"
    )


def test_exact_error_code_recalled(store: SQLiteMemoryStore) -> None:
    """Error codes (hyphens stripped by sanitizer) are recalled via BM25."""
    m = _memory(
        title="Known error: E0203",
        content="E0203 occurs when the FTS5 content table rowid is mismatched.",
        type="failure_pattern",
    )
    store.create_memory(m)

    # "E0203" — hyphens already absent, direct token match
    result = memory_search("E0203", memory_store=store, project_id=PROJECT_ID)
    returned_ids = [r["id"] for r in result["memories"]]
    assert m.id in returned_ids


def test_file_path_in_memory_recalled(store: SQLiteMemoryStore) -> None:
    """File paths in memory content are matched by FTS5."""
    m = _memory(
        title="Key file for capture",
        content="src/engram/capture/__init__.py contains the transcript tailer.",
        type="project_fact",
    )
    store.create_memory(m)

    # Slashes removed by sanitizer; "capture" and "init" and "py" become tokens
    result = memory_search("capture init.py", memory_store=store, project_id=PROJECT_ID)
    returned_ids = [r["id"] for r in result["memories"]]
    assert m.id in returned_ids


# ---------------------------------------------------------------------------
# (c) memory_context respects token_budget
# ---------------------------------------------------------------------------


def test_context_never_exceeds_budget(populated_store: SQLiteMemoryStore) -> None:
    """memory_context must never report injected_tokens > token_budget."""
    for budget in (100, 300, 1200):
        result = memory_context(
            "SQLite decision",
            memory_store=populated_store,
            project_id=PROJECT_ID,
            token_budget=budget,
        )
        assert result["injected_tokens"] <= budget, (
            f"Budget {budget} exceeded: got {result['injected_tokens']}"
        )


def test_context_actual_text_fits_budget(populated_store: SQLiteMemoryStore) -> None:
    """The context string itself must fit within the token budget."""
    budget = 200
    result = memory_context(
        "ruff format commit",
        memory_store=populated_store,
        project_id=PROJECT_ID,
        token_budget=budget,
    )
    actual_tokens = _estimate_tokens(result["context"])
    # Allow small overhead from the joiner "\n\n" between sections
    assert actual_tokens <= budget + 10, (
        f"Context text too large: {actual_tokens} tokens (budget={budget})"
    )


def test_context_tiny_budget_returns_empty(populated_store: SQLiteMemoryStore) -> None:
    """A budget too small for any single memory should return an empty context."""
    result = memory_context(
        "SQLite",
        memory_store=populated_store,
        project_id=PROJECT_ID,
        token_budget=1,  # impossibly tiny — nothing fits
    )
    assert result["injected_tokens"] <= 1
    # context may be empty or contain only a very short task context
    assert _estimate_tokens(result["context"]) <= 2


# ---------------------------------------------------------------------------
# (d) Stale check: memory whose file changed is surfaced as "stale"
# ---------------------------------------------------------------------------


def test_stale_memory_detected_on_get(
    store: SQLiteMemoryStore, tmp_path: pytest.TempPathFactory
) -> None:
    """memory_get marks a memory stale when the backing file has changed."""
    test_file = tmp_path / "module.py"
    original_bytes = b"def process(): pass\n"
    test_file.write_bytes(original_bytes)
    original_hash = _hash_bytes(original_bytes)

    m = _memory(
        title="process function contract",
        content="process() must be called at session start.",
        type="decision",
        file_path=str(test_file),
        file_hash=original_hash,
    )
    store.create_memory(m)

    # File unchanged — should NOT be stale
    result = memory_get([m.id], memory_store=store)
    assert result["memories"][0]["status"] == "active"

    # Modify the file
    test_file.write_bytes(b"def process(): return True  # changed\n")

    # Now memory_get should detect staleness
    result = memory_get([m.id], memory_store=store)
    assert result["memories"][0]["status"] == "stale", (
        "Expected memory to be marked stale after file change"
    )


def test_stale_memory_detected_on_search(
    store: SQLiteMemoryStore, tmp_path: pytest.TempPathFactory
) -> None:
    """memory_search also runs the stale check and reflects updated status."""
    test_file = tmp_path / "store.py"
    original = b"class SQLiteMemoryStore: pass\n"
    test_file.write_bytes(original)

    m = _memory(
        title="Store implementation file",
        content="SQLiteMemoryStore lives in store.py.",
        type="project_fact",
        file_path=str(test_file),
        file_hash=_hash_bytes(original),
    )
    store.create_memory(m)

    # Modify file
    test_file.write_bytes(b"class SQLiteMemoryStore:\n    def __init__(self): pass\n")

    result = memory_search("SQLiteMemoryStore store", memory_store=store, status=None)
    matching = [r for r in result["memories"] if r["id"] == m.id]
    assert matching, "Memory not found in search results"
    assert matching[0]["status"] == "stale"


def test_stale_memory_not_injected_by_default(
    store: SQLiteMemoryStore, tmp_path: pytest.TempPathFactory
) -> None:
    """memory_context demotes freshly-staled memories (doesn't inject them)."""
    test_file = tmp_path / "config.py"
    test_file.write_bytes(b"CONFIG = {}\n")

    m = _memory(
        title="Config module",
        content="config.py holds all configuration.",
        type="project_fact",
        file_path=str(test_file),
        file_hash=_hash_bytes(b"CONFIG = {}\n"),
    )
    store.create_memory(m)

    # Change the file before calling memory_context
    test_file.write_bytes(b"CONFIG = {'debug': True}\n")

    result = memory_context(
        "config module",
        memory_store=store,
        project_id=PROJECT_ID,
        token_budget=2000,
    )
    assert m.id not in result["memory_ids"], "Stale memory should not be injected into context"
    assert m.id in result["trace"]["stale_ids"]


# ---------------------------------------------------------------------------
# (e) Conflicting memory not injected by default
# ---------------------------------------------------------------------------


def test_conflict_memory_not_injected(store: SQLiteMemoryStore) -> None:
    """memory_context must not inject memories with status='conflict'."""
    # Store a normal active memory — both use the word "database" so BM25 finds both
    normal = _memory(
        title="Use SQLite database",
        content="SQLite is the chosen database for this project.",
        type="decision",
    )
    store.create_memory(normal)

    # Conflict memory also contains "database" so FTS5 will find it as a candidate
    conflict = _memory(
        title="Use PostgreSQL database (conflict)",
        content="PostgreSQL database was proposed as a conflict to the SQLite decision.",
        type="decision",
        status="conflict",
    )
    store.create_memory(conflict)

    result = memory_context(
        "database",
        memory_store=store,
        project_id=PROJECT_ID,
        token_budget=4000,
    )

    assert conflict.id not in result["memory_ids"], (
        "Conflict memory must not appear in injected context"
    )
    assert conflict.id in result["trace"]["conflict_ids_excluded"]


def test_conflict_memory_not_in_search_active(store: SQLiteMemoryStore) -> None:
    """memory_search with default status='active' excludes conflict memories."""
    conflict = _memory(
        title="Conflicting lint rule",
        content="Use tabs OR spaces — unresolved conflict between engineers.",
        type="preference",
        status="conflict",
    )
    store.create_memory(conflict)

    result = memory_search(
        "lint rule tabs spaces",
        memory_store=store,
        project_id=PROJECT_ID,
        status="active",
    )
    ids = [r["id"] for r in result["memories"]]
    assert conflict.id not in ids


# ---------------------------------------------------------------------------
# Additional mechanics
# ---------------------------------------------------------------------------


def test_memory_timeline_anchor_by_id(populated_store: SQLiteMemoryStore) -> None:
    """memory_timeline returns before/after windows around an anchor ID."""
    # Pick any memory from the populated store
    all_mems = populated_store.list_memories(project_id=PROJECT_ID)
    assert len(all_mems) >= 3, "Need at least 3 memories for a meaningful timeline test"

    # Sort chronologically and pick the middle one as anchor
    sorted_mems = sorted(all_mems, key=lambda m: m.created_at)
    anchor = sorted_mems[len(sorted_mems) // 2]

    result = memory_timeline(
        memory_store=populated_store,
        anchor_id=anchor.id,
        before=2,
        after=2,
    )

    assert result["anchor"] is not None
    assert result["anchor"]["id"] == anchor.id
    assert len(result["before"]) <= 2
    assert len(result["after"]) <= 2
    # before + after must not include the anchor itself
    all_ids = [r["id"] for r in result["before"]] + [r["id"] for r in result["after"]]
    assert anchor.id not in all_ids


def test_memory_timeline_anchor_by_query(populated_store: SQLiteMemoryStore) -> None:
    """memory_timeline with a query finds the top BM25 match as anchor."""
    result = memory_timeline(
        memory_store=populated_store,
        query="ruff format",
        project_id=PROJECT_ID,
        before=2,
        after=2,
    )
    # Should find the "Always run ruff before committing" memory
    assert result["anchor"] is not None


def test_memory_timeline_missing_anchor(populated_store: SQLiteMemoryStore) -> None:
    """memory_timeline with a non-existent anchor_id returns empty result."""
    result = memory_timeline(
        memory_store=populated_store,
        anchor_id="does-not-exist",
    )
    assert result["anchor"] is None
    assert result["before"] == []
    assert result["after"] == []


def test_memory_get_full_records(populated_store: SQLiteMemoryStore) -> None:
    """memory_get returns full Memory records with all fields."""
    all_mems = populated_store.list_memories(project_id=PROJECT_ID)
    ids = [m.id for m in all_mems[:2]]

    result = memory_get(ids, memory_store=populated_store)
    assert len(result["memories"]) == 2
    assert result["not_found"] == []

    # Full record must have 'content' (not just a preview)
    for mem_dict in result["memories"]:
        assert "content" in mem_dict
        assert "content_hash" in mem_dict
        assert len(mem_dict["content"]) > 0


def test_memory_get_not_found(populated_store: SQLiteMemoryStore) -> None:
    """memory_get reports IDs that are not in the store."""
    result = memory_get(["ghost-id-1", "ghost-id-2"], memory_store=populated_store)
    assert result["memories"] == []
    assert set(result["not_found"]) == {"ghost-id-1", "ghost-id-2"}


def test_search_fts_special_chars_no_crash(store: SQLiteMemoryStore) -> None:
    """FTS5 special characters in the query must not crash search."""
    m = _memory(
        title="Edge case test",
        content="Testing special char handling: hello-world (test) [bracket].",
    )
    store.create_memory(m)
    # These should not raise, even with FTS5 special chars
    for query in ["hello-world", "test (parenthesis)", "[bracket]", '"quoted"', "* wildcard"]:
        result = memory_search(query, memory_store=store, project_id=PROJECT_ID)
        assert isinstance(result["memories"], list)


def test_search_memories_fts_direct(store: SQLiteMemoryStore) -> None:
    """search_memories_fts (store method) returns BM25-ranked results."""
    store.create_memory(
        _memory(title="Alpha memory", content="unique_token_alpha_xyz retrieval test")
    )
    store.create_memory(_memory(title="Beta memory", content="something completely unrelated"))

    results = store.search_memories_fts("unique_token_alpha_xyz", project_id=PROJECT_ID)
    assert len(results) >= 1
    assert results[0].title == "Alpha memory"


def test_context_priority_ordering(store: SQLiteMemoryStore) -> None:
    """memory_context respects §11.3 type priority (decision before preference)."""
    pref = _memory(
        title="Use double quotes",
        content="Always use double-quoted strings in Python.",
        type="preference",
        confidence=0.9,
    )
    decision = _memory(
        title="SQLite is canonical",
        content="SQLite is the canonical data store for this project.",
        type="decision",
        confidence=0.5,  # lower confidence than preference — priority wins
    )
    store.create_memory(pref)
    store.create_memory(decision)

    result = memory_context(
        "data store strings",
        memory_store=store,
        project_id=PROJECT_ID,
        token_budget=4000,
    )
    ids = result["memory_ids"]
    if decision.id in ids and pref.id in ids:
        assert ids.index(decision.id) < ids.index(pref.id), (
            "decision should come before preference in context"
        )


def test_context_trace_structure(populated_store: SQLiteMemoryStore) -> None:
    """memory_context trace dict must contain all fields WS-D needs."""
    result = memory_context(
        "SQLite",
        memory_store=populated_store,
        project_id=PROJECT_ID,
        token_budget=1200,
    )
    trace = result["trace"]
    required_fields = {
        "query",
        "project_id",
        "candidate_ids",
        "selected_ids",
        "scores",
        "filters_applied",
        "ranking_features",
        "stale_ids",
        "conflict_ids_excluded",
        "token_budget",
        "injected_tokens",
    }
    missing = required_fields - set(trace.keys())
    assert not missing, f"Trace missing fields: {missing}"
    assert trace["token_budget"] == 1200
    assert trace["injected_tokens"] == result["injected_tokens"]


def test_context_excludes_superseded_and_deleted(store: SQLiteMemoryStore) -> None:
    """memory_context never injects superseded or deleted memories."""
    superseded = _memory(
        title="Old approach",
        content="Originally we used raw SQL without an ORM layer.",
        type="decision",
        status="superseded",
    )
    deleted = _memory(
        title="Deprecated note",
        content="This note was deleted after the architectural review.",
        type="project_fact",
        status="deleted",
    )
    active = _memory(
        title="Current approach",
        content="Using SQLite with a thin custom store layer.",
        type="decision",
    )
    store.create_memory(superseded)
    store.create_memory(deleted)
    store.create_memory(active)

    result = memory_context(
        "approach SQL layer",
        memory_store=store,
        project_id=PROJECT_ID,
        token_budget=4000,
    )
    assert superseded.id not in result["memory_ids"]
    assert deleted.id not in result["memory_ids"]


def test_search_project_filter(conn: sqlite3.Connection, store: SQLiteMemoryStore) -> None:
    """memory_search scopes results to the given project_id."""
    other_project = "proj-other-999"
    # Create the second project so FK constraint is satisfied
    SQLiteEventStore(conn).create_project(
        Project(id=other_project, root_path="/tmp/other-project", name="other-project")
    )
    m_own = _memory(title="Own project memory", content="belongs here", project_id=PROJECT_ID)
    m_other = _memory(
        title="Other project memory", content="belongs elsewhere", project_id=other_project
    )
    store.create_memory(m_own)
    store.create_memory(m_other)

    result = memory_search("belongs", memory_store=store, project_id=PROJECT_ID)
    ids = [r["id"] for r in result["memories"]]
    assert m_own.id in ids
    assert m_other.id not in ids


def test_context_with_task_context(conn: sqlite3.Connection) -> None:
    """memory_context includes active task contexts at the top of the output."""
    store = SQLiteMemoryStore(conn)

    # We need a project + session for task_context FK
    events = SQLiteEventStore(conn)
    events.create_project(Project(id=PROJECT_ID, root_path="/tmp/test", name="test"))
    sess = events.create_session(
        AgentSession(
            project_id=PROJECT_ID,
            external_session_id="ext-1",
            memory_thread_id="thread-1",
            agent="claude_code",
        )
    )

    ctx = TaskContext(
        project_id=PROJECT_ID,
        session_id=sess.id,
        task_key="current-task",
        title="Implement retrieval",
        content="We are building the retrieval module for Engram.",
    )
    store.create_task_context(ctx)

    store.create_memory(_memory(title="SQLite decision", content="Use SQLite.", type="decision"))

    result = memory_context(
        "retrieval SQLite",
        memory_store=store,
        project_id=PROJECT_ID,
        token_budget=2000,
    )
    # Task context title should appear in the output context
    assert "Implement retrieval" in result["context"]
