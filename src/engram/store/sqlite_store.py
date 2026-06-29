"""SQLite implementations of EventStore and MemoryStore.

Both classes take a ``sqlite3.Connection`` produced by ``engram.db.runner.open_db``
(WAL mode, foreign keys ON, row_factory=sqlite3.Row).  Pass ``":memory:"`` for
tests.

Design notes:
- JSON columns (payload_json, changed_files_json, …) are serialised/deserialised
  here so the rest of the codebase always sees typed Python objects (dict, list).
- datetime values are stored as ISO 8601 UTC strings and parsed on read.
  Python 3.11+ fromisoformat handles the 'Z' suffix produced by SQLite's strftime.
- Neither class performs content-hash dedup; callers use get_memory_by_hash to
  check before calling create_memory.  This keeps each method single-purpose.
- update_memory takes a free-form dict because callers know which fields they
  changed (status, confidence, supersedes_memory_id, …); building typed overloads
  for every combination is shallow classitis.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from engram.models import (
    AgentSession,
    EvalCase,
    EvalRun,
    Event,
    Memory,
    MemorySource,
    Project,
    RetrievalTrace,
    SessionSummary,
    TaskContext,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt(value: str | None) -> datetime | None:
    """Parse an ISO 8601 UTC string from SQLite, or return None."""
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _dt_req(value: str) -> datetime:
    """Parse a required ISO 8601 UTC string (raises ValueError on bad input)."""
    return datetime.fromisoformat(value)


def _iso(value: datetime | None) -> str | None:
    """Serialise a datetime to ISO 8601 UTC string for storage."""
    if value is None:
        return None
    # Normalise to UTC if tz-aware; store in the format SQLite uses.
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _iso_req(value: datetime) -> str:
    return _iso(value)  # type: ignore[return-value]


def _jdump(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def _jload(value: str) -> Any:
    return json.loads(value)


def _sanitize_fts_query(query: str) -> str:
    """Convert a raw user query into a safe FTS5 MATCH expression.

    FTS5 special characters and common path separators (/ .) are stripped so
    that identifiers like "src/engram/__init__.py" or "E-0203" are split into
    individual word tokens before matching.  This mirrors how the FTS5
    unicode61 tokenizer splits content, ensuring queries on file paths and
    error codes find their corresponding indexed tokens.

    Tokens are joined with OR so BM25 naturally scores documents that contain
    more matching terms higher.

    Returns an empty string if no tokens survive sanitization.
    """
    # Strip FTS5 operators and common path/identifier separators
    clean = re.sub(r'[-"*^~(){}[\]:@/.]', " ", query)
    tokens = [t.strip() for t in clean.split() if t.strip()]
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    return " OR ".join(tokens)


# ---------------------------------------------------------------------------
# SQLiteEventStore
# ---------------------------------------------------------------------------


class SQLiteEventStore:
    """SQLite-backed implementation of EventStore.

    Owns the projects, agent_sessions, and events tables.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # --- projects ---

    def create_project(self, project: Project) -> Project:
        """Insert a project row.  Raises IntegrityError if root_path already exists."""
        self._conn.execute(
            """
            INSERT INTO projects (id, root_path, name, repo_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                project.id,
                project.root_path,
                project.name,
                project.repo_url,
                _iso_req(project.created_at),
                _iso_req(project.updated_at),
            ),
        )
        self._conn.commit()
        return project

    def get_project(self, project_id: str) -> Project | None:
        row = self._conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        return _row_to_project(row) if row else None

    def get_project_by_path(self, root_path: str) -> Project | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE root_path = ?", (root_path,)
        ).fetchone()
        return _row_to_project(row) if row else None

    # --- sessions ---

    def create_session(self, session: AgentSession) -> AgentSession:
        self._conn.execute(
            """
            INSERT INTO agent_sessions
                (id, project_id, external_session_id, memory_thread_id, agent,
                 branch, git_sha, status, started_at, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.project_id,
                session.external_session_id,
                session.memory_thread_id,
                session.agent,
                session.branch,
                session.git_sha,
                session.status,
                _iso_req(session.started_at),
                _iso(session.ended_at),
            ),
        )
        self._conn.commit()
        return session

    def get_session(self, session_id: str) -> AgentSession | None:
        row = self._conn.execute(
            "SELECT * FROM agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return _row_to_session(row) if row else None

    def update_session_status(
        self, session_id: str, status: str, ended_at: datetime | None = None
    ) -> None:
        self._conn.execute(
            "UPDATE agent_sessions SET status = ?, ended_at = ? WHERE id = ?",
            (status, _iso(ended_at), session_id),
        )
        self._conn.commit()

    # --- events ---

    def create_event(self, event: Event) -> Event:
        """Append an event.  Raises IntegrityError on duplicate (session_id, seq)."""
        self._conn.execute(
            """
            INSERT INTO events
                (id, project_id, session_id, seq, source_type, source_seq,
                 raw_ref_file, raw_ref_offset, capture_confidence, event_type,
                 payload_json, content_hash, occurred_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.project_id,
                event.session_id,
                event.seq,
                event.source_type,
                event.source_seq,
                event.raw_ref_file,
                event.raw_ref_offset,
                event.capture_confidence,
                event.event_type,
                _jdump(event.payload),
                event.content_hash,
                _iso_req(event.occurred_at),
                _iso_req(event.created_at),
            ),
        )
        self._conn.commit()
        return event

    def get_event(self, event_id: str) -> Event | None:
        row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return _row_to_event(row) if row else None

    def list_session_events(self, session_id: str) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY seq ASC", (session_id,)
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def max_seq_for_session(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(seq) FROM events WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row[0] if row and row[0] is not None else 0


# ---------------------------------------------------------------------------
# SQLiteMemoryStore
# ---------------------------------------------------------------------------


class SQLiteMemoryStore:
    """SQLite-backed implementation of MemoryStore.

    Owns memories, task_contexts, session_summaries, memory_sources,
    retrieval_traces, eval_cases, and eval_runs.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # --- memories ---

    def create_memory(self, memory: Memory) -> Memory:
        """Insert a memory.  Raises IntegrityError on duplicate content_hash."""
        self._conn.execute(
            """
            INSERT INTO memories
                (id, project_id, scope, type, origin, title, content, content_hash,
                 status, confidence, valid_from, valid_until, last_seen_at,
                 access_count, file_path, file_hash, supersedes_memory_id,
                 source_event_ids_json, metadata_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory.id,
                memory.project_id,
                memory.scope,
                memory.type,
                memory.origin,
                memory.title,
                memory.content,
                memory.content_hash,
                memory.status,
                memory.confidence,
                _iso(memory.valid_from),
                _iso(memory.valid_until),
                _iso(memory.last_seen_at),
                memory.access_count,
                memory.file_path,
                memory.file_hash,
                memory.supersedes_memory_id,
                _jdump(memory.source_event_ids),
                _jdump(memory.metadata),
                _iso_req(memory.created_at),
                _iso_req(memory.updated_at),
            ),
        )
        self._conn.commit()
        return memory

    def get_memory(self, memory_id: str) -> Memory | None:
        row = self._conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return _row_to_memory(row) if row else None

    def get_memory_by_hash(self, content_hash: str) -> Memory | None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        return _row_to_memory(row) if row else None

    def list_memories(
        self,
        project_id: str | None = None,
        type: str | None = None,
        status: str | None = None,
    ) -> list[Memory]:
        """Return memories matching the given filters (all optional).

        Defaults to *all* statuses — callers should pass ``status="active"``
        for retrieval and ``status=None`` for admin/debug use.
        """
        clauses = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM memories {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [_row_to_memory(r) for r in rows]

    def update_memory(self, memory_id: str, updates: dict[str, Any]) -> None:
        """Apply *updates* (column_name -> value) to the given memory.

        JSON-typed columns (source_event_ids, metadata) must be passed as
        Python lists/dicts; this method serialises them.  datetime fields must
        be passed as datetime objects.
        """
        if not updates:
            return

        json_cols = {"source_event_ids", "metadata"}
        dt_cols = {"valid_from", "valid_until", "last_seen_at", "updated_at"}

        set_clauses = []
        params = []
        for col, val in updates.items():
            db_col = f"{col}_json" if col in json_cols else col
            if col in json_cols:
                val = _jdump(val)
            elif col in dt_cols:
                val = _iso(val)
            set_clauses.append(f"{db_col} = ?")
            params.append(val)

        # Always bump updated_at unless caller explicitly set it
        if "updated_at" not in updates:
            set_clauses.append("updated_at = ?")
            params.append(_iso(datetime.now(UTC)))

        params.append(memory_id)
        self._conn.execute(
            f"UPDATE memories SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        self._conn.commit()

    def search_memories_fts(
        self,
        query: str,
        *,
        project_id: str | None = None,
        type: str | None = None,
        status: str | None = None,
        file_path: str | None = None,
        limit: int = 20,
    ) -> list[Memory]:
        """BM25 full-text search over memories title + content via the FTS5 index.

        FTS5 bm25() returns negative floats; ORDER BY ASC puts best matches first.
        The memories_fts virtual table is a content table backed by the memories
        table, so we JOIN on rowid to get full memory rows.

        Returns an empty list (rather than raising) if the sanitized query is
        empty or produces no FTS matches.
        """
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            return []

        clauses: list[str] = []
        params: list[Any] = [fts_query]

        if project_id is not None:
            clauses.append("m.project_id = ?")
            params.append(project_id)
        if type is not None:
            clauses.append("m.type = ?")
            params.append(type)
        if status is not None:
            clauses.append("m.status = ?")
            params.append(status)
        if file_path is not None:
            clauses.append("m.file_path = ?")
            params.append(file_path)

        extra = (" AND " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        try:
            rows = self._conn.execute(
                f"""
                SELECT m.*
                FROM memories_fts
                JOIN memories m ON m.rowid = memories_fts.rowid
                WHERE memories_fts MATCH ?{extra}
                ORDER BY bm25(memories_fts) ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        except sqlite3.OperationalError:
            # Malformed FTS5 MATCH expression from user input (despite
            # sanitization) — degrade to no results rather than crash. Other
            # error classes (programming/integrity/DB) propagate.
            return []

        return [_row_to_memory(r) for r in rows]

    # --- task_contexts ---

    def create_task_context(self, ctx: TaskContext) -> TaskContext:
        self._conn.execute(
            """
            INSERT INTO task_contexts
                (id, project_id, session_id, task_key, title, content,
                 changed_files_json, next_steps_json, status, ttl_until,
                 source_event_ids_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ctx.id,
                ctx.project_id,
                ctx.session_id,
                ctx.task_key,
                ctx.title,
                ctx.content,
                _jdump(ctx.changed_files),
                _jdump(ctx.next_steps),
                ctx.status,
                _iso(ctx.ttl_until),
                _jdump(ctx.source_event_ids),
                _iso_req(ctx.created_at),
                _iso_req(ctx.updated_at),
            ),
        )
        self._conn.commit()
        return ctx

    def get_task_context(self, task_id: str) -> TaskContext | None:
        row = self._conn.execute("SELECT * FROM task_contexts WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task_context(row) if row else None

    def list_active_task_contexts(self, project_id: str) -> list[TaskContext]:
        rows = self._conn.execute(
            """
            SELECT * FROM task_contexts
            WHERE project_id = ?
              AND status = 'active'
              AND (ttl_until IS NULL OR ttl_until > strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            ORDER BY created_at DESC
            """,
            (project_id,),
        ).fetchall()
        return [_row_to_task_context(r) for r in rows]

    def list_task_contexts(self, project_id: str, status: str | None = None) -> list[TaskContext]:
        if status is not None:
            rows = self._conn.execute(
                "SELECT * FROM task_contexts WHERE project_id = ? AND status = ? "
                "ORDER BY created_at DESC",
                (project_id, status),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM task_contexts WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [_row_to_task_context(r) for r in rows]

    def update_task_context(self, task_id: str, updates: dict[str, Any]) -> None:
        if not updates:
            return

        dt_cols = {"ttl_until", "updated_at"}
        json_cols = {"changed_files", "next_steps", "source_event_ids"}

        set_clauses = []
        params: list[Any] = []
        for col, val in updates.items():
            db_col = f"{col}_json" if col in json_cols else col
            if col in json_cols:
                val = _jdump(val)
            elif col in dt_cols:
                val = _iso(val)
            set_clauses.append(f"{db_col} = ?")
            params.append(val)

        if "updated_at" not in updates:
            set_clauses.append("updated_at = ?")
            params.append(_iso(datetime.now(UTC)))

        params.append(task_id)
        self._conn.execute(
            f"UPDATE task_contexts SET {', '.join(set_clauses)} WHERE id = ?",
            params,
        )
        self._conn.commit()

    # --- session_summaries ---

    def create_session_summary(self, summary: SessionSummary) -> SessionSummary:
        self._conn.execute(
            """
            INSERT INTO session_summaries
                (id, project_id, session_id, request, completed, learned,
                 next_steps, files_read_json, files_modified_json,
                 source_event_ids_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                summary.id,
                summary.project_id,
                summary.session_id,
                summary.request,
                summary.completed,
                summary.learned,
                summary.next_steps,
                _jdump(summary.files_read),
                _jdump(summary.files_modified),
                _jdump(summary.source_event_ids),
                _iso_req(summary.created_at),
            ),
        )
        self._conn.commit()
        return summary

    def get_session_summary(self, summary_id: str) -> SessionSummary | None:
        row = self._conn.execute(
            "SELECT * FROM session_summaries WHERE id = ?", (summary_id,)
        ).fetchone()
        return _row_to_summary(row) if row else None

    # --- memory_sources ---

    def create_memory_source(self, source: MemorySource) -> MemorySource:
        self._conn.execute(
            """
            INSERT INTO memory_sources
                (id, memory_id, source_type, source_id, quote_or_summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                source.id,
                source.memory_id,
                source.source_type,
                source.source_id,
                source.quote_or_summary,
                _iso_req(source.created_at),
            ),
        )
        self._conn.commit()
        return source

    # --- retrieval_traces ---

    def create_retrieval_trace(self, trace: RetrievalTrace) -> RetrievalTrace:
        self._conn.execute(
            """
            INSERT INTO retrieval_traces
                (id, query, project_id, selected_memory_ids_json,
                 candidate_memory_ids_json, ranking_features_json,
                 token_budget, injected_tokens, outcome_label, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trace.id,
                trace.query,
                trace.project_id,
                _jdump(trace.selected_memory_ids),
                _jdump(trace.candidate_memory_ids),
                _jdump(trace.ranking_features),
                trace.token_budget,
                trace.injected_tokens,
                trace.outcome_label,
                _iso_req(trace.created_at),
            ),
        )
        self._conn.commit()
        return trace

    # --- eval_cases ---

    def create_eval_case(self, case: EvalCase) -> EvalCase:
        self._conn.execute(
            """
            INSERT INTO eval_cases
                (id, query, project_id, expected_memory_ids_json,
                 expected_behavior, tags_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case.id,
                case.query,
                case.project_id,
                _jdump(case.expected_memory_ids),
                case.expected_behavior,
                _jdump(case.tags),
                _iso_req(case.created_at),
            ),
        )
        self._conn.commit()
        return case

    def list_eval_cases(self, project_id: str | None = None) -> list[EvalCase]:
        if project_id:
            rows = self._conn.execute(
                "SELECT * FROM eval_cases WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM eval_cases ORDER BY created_at DESC"
            ).fetchall()
        return [_row_to_eval_case(r) for r in rows]

    # --- eval_runs ---

    def create_eval_run(self, run: EvalRun) -> EvalRun:
        self._conn.execute(
            """
            INSERT INTO eval_runs
                (id, run_name, recall_at_5, mrr, stale_injection_rate,
                 avg_injected_tokens, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.run_name,
                run.recall_at_5,
                run.mrr,
                run.stale_injection_rate,
                run.avg_injected_tokens,
                _iso_req(run.created_at),
            ),
        )
        self._conn.commit()
        return run

    def list_eval_runs(self) -> list[EvalRun]:
        rows = self._conn.execute("SELECT * FROM eval_runs ORDER BY created_at DESC").fetchall()
        return [_row_to_eval_run(r) for r in rows]


# ---------------------------------------------------------------------------
# Row → model converters  (private; one per table)
# ---------------------------------------------------------------------------


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        root_path=row["root_path"],
        name=row["name"],
        repo_url=row["repo_url"],
        created_at=_dt_req(row["created_at"]),
        updated_at=_dt_req(row["updated_at"]),
    )


def _row_to_session(row: sqlite3.Row) -> AgentSession:
    return AgentSession(
        id=row["id"],
        project_id=row["project_id"],
        external_session_id=row["external_session_id"],
        memory_thread_id=row["memory_thread_id"],
        agent=row["agent"],
        branch=row["branch"],
        git_sha=row["git_sha"],
        status=row["status"],
        started_at=_dt_req(row["started_at"]),
        ended_at=_dt(row["ended_at"]),
    )


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        project_id=row["project_id"],
        session_id=row["session_id"],
        seq=row["seq"],
        source_type=row["source_type"],
        source_seq=row["source_seq"],
        raw_ref_file=row["raw_ref_file"],
        raw_ref_offset=row["raw_ref_offset"],
        capture_confidence=row["capture_confidence"],
        event_type=row["event_type"],
        payload=_jload(row["payload_json"]),
        content_hash=row["content_hash"],
        occurred_at=_dt_req(row["occurred_at"]),
        created_at=_dt_req(row["created_at"]),
    )


def _row_to_task_context(row: sqlite3.Row) -> TaskContext:
    return TaskContext(
        id=row["id"],
        project_id=row["project_id"],
        session_id=row["session_id"],
        task_key=row["task_key"],
        title=row["title"],
        content=row["content"],
        changed_files=_jload(row["changed_files_json"]),
        next_steps=_jload(row["next_steps_json"]),
        status=row["status"],
        ttl_until=_dt(row["ttl_until"]),
        source_event_ids=_jload(row["source_event_ids_json"]),
        created_at=_dt_req(row["created_at"]),
        updated_at=_dt_req(row["updated_at"]),
    )


def _row_to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=row["id"],
        project_id=row["project_id"],
        scope=row["scope"],
        type=row["type"],
        origin=row["origin"],
        title=row["title"],
        content=row["content"],
        content_hash=row["content_hash"],
        status=row["status"],
        confidence=row["confidence"],
        valid_from=_dt(row["valid_from"]),
        valid_until=_dt(row["valid_until"]),
        last_seen_at=_dt(row["last_seen_at"]),
        access_count=row["access_count"],
        file_path=row["file_path"],
        file_hash=row["file_hash"],
        supersedes_memory_id=row["supersedes_memory_id"],
        source_event_ids=_jload(row["source_event_ids_json"]),
        metadata=_jload(row["metadata_json"]),
        created_at=_dt_req(row["created_at"]),
        updated_at=_dt_req(row["updated_at"]),
    )


def _row_to_summary(row: sqlite3.Row) -> SessionSummary:
    return SessionSummary(
        id=row["id"],
        project_id=row["project_id"],
        session_id=row["session_id"],
        request=row["request"],
        completed=row["completed"],
        learned=row["learned"],
        next_steps=row["next_steps"],
        files_read=_jload(row["files_read_json"]),
        files_modified=_jload(row["files_modified_json"]),
        source_event_ids=_jload(row["source_event_ids_json"]),
        created_at=_dt_req(row["created_at"]),
    )


def _row_to_eval_case(row: sqlite3.Row) -> EvalCase:
    return EvalCase(
        id=row["id"],
        query=row["query"],
        project_id=row["project_id"],
        expected_memory_ids=_jload(row["expected_memory_ids_json"]),
        expected_behavior=row["expected_behavior"],
        tags=_jload(row["tags_json"]),
        created_at=_dt_req(row["created_at"]),
    )


def _row_to_eval_run(row: sqlite3.Row) -> EvalRun:
    return EvalRun(
        id=row["id"],
        run_name=row["run_name"],
        recall_at_5=row["recall_at_5"],
        mrr=row["mrr"],
        stale_injection_rate=row["stale_injection_rate"],
        avg_injected_tokens=row["avg_injected_tokens"],
        created_at=_dt_req(row["created_at"]),
    )
