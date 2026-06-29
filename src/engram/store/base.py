"""Abstract storage interfaces (Protocols).

Why three Protocols:
- EventStore owns raw capture (projects, sessions, events) — the write path.
- MemoryStore owns processed/derived data (memories, task contexts, etc.) — the read path.
- VectorStore owns ANN similarity search — interface-only at P0, implemented at P1.

These are the one upfront abstraction CLAUDE.md permits, because a Postgres
swap at P2 requires them (ADR 0001).  Everything else in the codebase writes
concrete implementations directly; no further protocol indirection is added.

Naming convention for methods: verb_noun (e.g. create_event, list_memories).
All methods are synchronous; the async MCP layer calls them from a thread-pool
executor if needed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

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


@runtime_checkable
class EventStore(Protocol):
    """Append-only capture store: projects, sessions, and raw events.

    Implementors: SQLiteEventStore (P0), PostgresEventStore (P2 swap).
    """

    # --- projects ---

    def create_project(self, project: Project) -> Project:
        """Insert a new project row and return it."""
        ...

    def get_project(self, project_id: str) -> Project | None:
        """Fetch a project by its UUID, or None if not found."""
        ...

    def get_project_by_path(self, root_path: str) -> Project | None:
        """Fetch a project by its root_path (unique), or None if not found."""
        ...

    # --- sessions ---

    def create_session(self, session: AgentSession) -> AgentSession:
        """Insert a new agent session and return it."""
        ...

    def get_session(self, session_id: str) -> AgentSession | None:
        """Fetch a session by UUID, or None."""
        ...

    def update_session_status(
        self, session_id: str, status: str, ended_at: datetime | None = None
    ) -> None:
        """Update session.status (and optionally ended_at) in place."""
        ...

    # --- events ---

    def create_event(self, event: Event) -> Event:
        """Append a new event and return it (no mutation, no dedup check here)."""
        ...

    def get_event(self, event_id: str) -> Event | None:
        """Fetch a single event by UUID, or None."""
        ...

    def list_session_events(self, session_id: str) -> list[Event]:
        """Return all events for a session ordered by seq ascending."""
        ...

    def max_seq_for_session(self, session_id: str) -> int:
        """Return the highest seq seen for this session (0 if no events yet).

        Used at session_end to detect gaps in the sequence (ADR 0004).
        """
        ...


@runtime_checkable
class MemoryStore(Protocol):
    """Processed-data store: memories, task contexts, summaries, traces, evals.

    Implementors: SQLiteMemoryStore (P0), PostgresMemoryStore (P2 swap).
    """

    # --- memories ---

    def create_memory(self, memory: Memory) -> Memory:
        """Insert a new memory.  Caller must ensure content_hash uniqueness."""
        ...

    def get_memory(self, memory_id: str) -> Memory | None:
        """Fetch a memory by UUID, or None."""
        ...

    def get_memory_by_hash(self, content_hash: str) -> Memory | None:
        """Fetch a memory by content_hash for exact-match dedup, or None."""
        ...

    def list_memories(
        self,
        project_id: str | None = None,
        type: str | None = None,
        status: str | None = None,
    ) -> list[Memory]:
        """List memories with optional filters.  Returns active memories by default."""
        ...

    def update_memory(self, memory_id: str, updates: dict[str, Any]) -> None:
        """Apply a partial update dict to a memory row (status, confidence, etc.)."""
        ...

    # --- task_contexts ---

    def create_task_context(self, ctx: TaskContext) -> TaskContext:
        """Insert a new short-term task context."""
        ...

    def get_task_context(self, task_id: str) -> TaskContext | None:
        """Fetch a task context by UUID, or None."""
        ...

    def list_active_task_contexts(self, project_id: str) -> list[TaskContext]:
        """Return all active (non-expired, non-completed) task contexts for a project."""
        ...

    # --- session_summaries ---

    def create_session_summary(self, summary: SessionSummary) -> SessionSummary:
        """Insert a session summary."""
        ...

    def get_session_summary(self, summary_id: str) -> SessionSummary | None:
        """Fetch a session summary by UUID, or None."""
        ...

    # --- memory_sources ---

    def create_memory_source(self, source: MemorySource) -> MemorySource:
        """Insert a provenance link from a memory to its source."""
        ...

    # --- retrieval_traces ---

    def create_retrieval_trace(self, trace: RetrievalTrace) -> RetrievalTrace:
        """Insert a retrieval trace for observability."""
        ...

    # --- eval_cases ---

    def create_eval_case(self, case: EvalCase) -> EvalCase:
        """Insert an eval case."""
        ...

    def list_eval_cases(self, project_id: str | None = None) -> list[EvalCase]:
        """Return all eval cases, optionally filtered by project."""
        ...

    # --- eval_runs ---

    def create_eval_run(self, run: EvalRun) -> EvalRun:
        """Insert an eval run result."""
        ...

    def list_eval_runs(self) -> list[EvalRun]:
        """Return all eval runs ordered by created_at descending."""
        ...


@runtime_checkable
class VectorStore(Protocol):
    """Interface for an ANN (approximate nearest-neighbour) embedding index.

    No P0 implementation — WS-D provides this at P1 using sqlite-vec or Chroma.
    WS-C (retrieval) programs against this protocol so the backing store is
    swappable without changing retrieval logic (ADR 0001 §P1).

    Embeddings are float32 lists; callers manage embedding generation.
    """

    def upsert(self, memory_id: str, embedding: list[float]) -> None:
        """Store or replace the embedding for *memory_id*."""
        ...

    def search(self, embedding: list[float], limit: int = 10) -> list[str]:
        """Return up to *limit* memory_ids ordered by cosine similarity (desc)."""
        ...

    def delete(self, memory_id: str) -> None:
        """Remove the embedding for *memory_id* from the index."""
        ...
