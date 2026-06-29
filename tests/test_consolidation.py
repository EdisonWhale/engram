"""Tests for the consolidation package (spec §6, §10, §27).

CLAUDE.md forces test-first on the supersede/conflict state machine.

All stores are in-memory fakes; no SQLite, no LLM keys required.
"""

from __future__ import annotations

import asyncio
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
# Fake stores (in-memory; used by all tests)
# ---------------------------------------------------------------------------


class FakeEventStore:
    """In-memory EventStore for tests."""

    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}
        self._sessions: dict[str, AgentSession] = {}
        self._events: dict[str, Event] = {}
        self._session_event_ids: dict[str, list[str]] = {}

    def create_project(self, project: Project) -> Project:
        self._projects[project.id] = project
        return project

    def get_project(self, project_id: str) -> Project | None:
        return self._projects.get(project_id)

    def get_project_by_path(self, root_path: str) -> Project | None:
        return next((p for p in self._projects.values() if p.root_path == root_path), None)

    def create_session(self, session: AgentSession) -> AgentSession:
        self._sessions[session.id] = session
        self._session_event_ids[session.id] = []
        return session

    def get_session(self, session_id: str) -> AgentSession | None:
        return self._sessions.get(session_id)

    def update_session_status(
        self, session_id: str, status: str, ended_at: datetime | None = None
    ) -> None:
        session = self._sessions.get(session_id)
        if session:
            self._sessions[session_id] = session.model_copy(
                update={"status": status, "ended_at": ended_at}
            )

    def create_event(self, event: Event) -> Event:
        self._events[event.id] = event
        self._session_event_ids.setdefault(event.session_id, []).append(event.id)
        return event

    def get_event(self, event_id: str) -> Event | None:
        return self._events.get(event_id)

    def list_session_events(self, session_id: str) -> list[Event]:
        ids = self._session_event_ids.get(session_id, [])
        return [self._events[eid] for eid in ids if eid in self._events]

    def max_seq_for_session(self, session_id: str) -> int:
        events = self.list_session_events(session_id)
        return max((e.seq for e in events), default=0)


class FakeMemoryStore:
    """In-memory MemoryStore + update_task_context extension (gap noted in report)."""

    def __init__(self) -> None:
        self._memories: dict[str, Memory] = {}
        self._by_hash: dict[str, str] = {}
        self._task_contexts: dict[str, TaskContext] = {}
        self._summaries: dict[str, SessionSummary] = {}
        self.sources: list[MemorySource] = []
        self._traces: list[RetrievalTrace] = []
        self._eval_cases: list[EvalCase] = []
        self._eval_runs: list[EvalRun] = []

    # --- memories ---

    def create_memory(self, memory: Memory) -> Memory:
        self._memories[memory.id] = memory
        self._by_hash[memory.content_hash] = memory.id
        return memory

    def get_memory(self, memory_id: str) -> Memory | None:
        return self._memories.get(memory_id)

    def get_memory_by_hash(self, content_hash: str) -> Memory | None:
        mid = self._by_hash.get(content_hash)
        return self._memories.get(mid) if mid else None

    def list_memories(
        self,
        project_id: str | None = None,
        type: str | None = None,
        status: str | None = None,
    ) -> list[Memory]:
        mems = list(self._memories.values())
        if project_id:
            mems = [m for m in mems if m.project_id == project_id]
        if type:
            mems = [m for m in mems if m.type == type]
        if status:
            mems = [m for m in mems if m.status == status]
        return mems

    def update_memory(self, memory_id: str, updates: dict[str, Any]) -> None:
        mem = self._memories.get(memory_id)
        if mem:
            updated = mem.model_copy(update=updates)
            self._memories[memory_id] = updated
            # Re-index hash if content changed
            if "content_hash" in updates:
                self._by_hash[updates["content_hash"]] = memory_id

    # --- task_contexts ---

    def create_task_context(self, ctx: TaskContext) -> TaskContext:
        self._task_contexts[ctx.id] = ctx
        return ctx

    def get_task_context(self, task_id: str) -> TaskContext | None:
        return self._task_contexts.get(task_id)

    def list_active_task_contexts(self, project_id: str) -> list[TaskContext]:
        now = datetime.now(UTC)
        return [
            ctx
            for ctx in self._task_contexts.values()
            if ctx.project_id == project_id
            and ctx.status == "active"
            and (ctx.ttl_until is None or ctx.ttl_until > now)
        ]

    def list_task_contexts(self, project_id: str, status: str | None = None) -> list[TaskContext]:
        return [
            ctx
            for ctx in self._task_contexts.values()
            if ctx.project_id == project_id and (status is None or ctx.status == status)
        ]

    def update_task_context(self, task_id: str, updates: dict[str, Any]) -> None:
        ctx = self._task_contexts.get(task_id)
        if ctx:
            self._task_contexts[task_id] = ctx.model_copy(update=updates)

    # --- session_summaries ---

    def create_session_summary(self, summary: SessionSummary) -> SessionSummary:
        self._summaries[summary.id] = summary
        return summary

    def get_session_summary(self, summary_id: str) -> SessionSummary | None:
        return self._summaries.get(summary_id)

    # --- memory_sources ---

    def create_memory_source(self, source: MemorySource) -> MemorySource:
        self.sources.append(source)
        return source

    # --- retrieval_traces ---

    def create_retrieval_trace(self, trace: RetrievalTrace) -> RetrievalTrace:
        self._traces.append(trace)
        return trace

    # --- eval_cases ---

    def create_eval_case(self, case: EvalCase) -> EvalCase:
        self._eval_cases.append(case)
        return case

    def list_eval_cases(self, project_id: str | None = None) -> list[EvalCase]:
        if project_id:
            return [c for c in self._eval_cases if c.project_id == project_id]
        return list(self._eval_cases)

    # --- eval_runs ---

    def create_eval_run(self, run: EvalRun) -> EvalRun:
        self._eval_runs.append(run)
        return run

    def list_eval_runs(self) -> list[EvalRun]:
        return list(self._eval_runs)


class FakeVectorStore:
    """Fake ScoredVectorStore with pre-configured cosine distances for testing."""

    def __init__(self, distances: dict[str, float] | None = None) -> None:
        """distances: {memory_id → cosine_distance (lower = more similar)}"""
        self._distances: dict[str, float] = distances or {}
        self._embeddings: dict[str, list[float]] = {}

    # VectorStore Protocol
    def upsert(self, memory_id: str, embedding: list[float]) -> None:
        self._embeddings[memory_id] = embedding

    def search(self, embedding: list[float], limit: int = 10) -> list[str]:
        ranked = sorted(self._distances.items(), key=lambda x: x[1])
        return [mid for mid, _ in ranked[:limit]]

    def delete(self, memory_id: str) -> None:
        self._distances.pop(memory_id, None)
        self._embeddings.pop(memory_id, None)

    # ScoredVectorStore extension
    def search_with_scores(
        self, embedding: list[float], limit: int = 10
    ) -> list[tuple[str, float]]:
        """Return (memory_id, cosine_distance) sorted by distance ascending."""
        ranked = sorted(self._distances.items(), key=lambda x: x[1])
        return ranked[:limit]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_decision_event(project_id: str, session_id: str, content: str) -> Event:
    payload = {
        "title": "Architecture decision",
        "content": content,
    }
    return Event(
        project_id=project_id,
        session_id=session_id,
        seq=1,
        source_type="transcript",
        event_type="decision",
        payload=payload,
        content_hash=Event.compute_hash(payload),
        occurred_at=datetime.now(UTC),
    )


def _make_fact_event(project_id: str, session_id: str, content: str, seq: int = 1) -> Event:
    payload = {"title": "Project fact", "content": content}
    return Event(
        project_id=project_id,
        session_id=session_id,
        seq=seq,
        source_type="transcript",
        event_type="fact",
        payload=payload,
        content_hash=Event.compute_hash(payload),
        occurred_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# 1. classify_distance — pure function
# ---------------------------------------------------------------------------


def test_classify_distance_duplicate_band():
    from engram.consolidation.dedup import classify_distance

    assert classify_distance(0.0) == "duplicate"
    assert classify_distance(0.10) == "duplicate"
    assert classify_distance(0.14) == "duplicate"


def test_classify_distance_conflict_band():
    from engram.consolidation.dedup import classify_distance

    assert classify_distance(0.15) == "conflict"
    assert classify_distance(0.25) == "conflict"
    assert classify_distance(0.34) == "conflict"


def test_classify_distance_independent_band():
    from engram.consolidation.dedup import classify_distance

    assert classify_distance(0.35) == "independent"
    assert classify_distance(0.50) == "independent"
    assert classify_distance(1.0) == "independent"


# ---------------------------------------------------------------------------
# 2. run_write_time_dedup — exact hash match
# ---------------------------------------------------------------------------


def test_dedup_exact_hash_match_returns_duplicate():
    from engram.consolidation.dedup import run_write_time_dedup

    memory_store = FakeMemoryStore()
    content = "Use SQLite as the source of truth."
    content_hash = Memory.compute_hash(content)

    existing = Memory(
        project_id="proj-1",
        scope="project",
        type="decision",
        origin="extracted",
        title="SQLite decision",
        content=content,
        content_hash=content_hash,
    )
    memory_store.create_memory(existing)

    result = run_write_time_dedup(
        content=content,
        content_hash=content_hash,
        embedding=None,
        memory_store=memory_store,
        vector_store=None,
    )

    assert result.action == "duplicate"
    assert result.existing_memory_id == existing.id


def test_dedup_no_match_returns_insert():
    from engram.consolidation.dedup import run_write_time_dedup

    memory_store = FakeMemoryStore()
    content = "Brand new fact."
    content_hash = Memory.compute_hash(content)

    result = run_write_time_dedup(
        content=content,
        content_hash=content_hash,
        embedding=None,
        memory_store=memory_store,
        vector_store=None,
    )

    assert result.action == "insert"


# ---------------------------------------------------------------------------
# 3. run_write_time_dedup — vector cosine band
# ---------------------------------------------------------------------------


def test_dedup_vector_duplicate_band():
    """Vector distance < 0.15 → duplicate (access_count bump, no insert)."""
    from engram.consolidation.dedup import run_write_time_dedup

    memory_store = FakeMemoryStore()
    existing_content = "Use spaces, not tabs."
    existing = Memory(
        project_id="proj-1",
        scope="project",
        type="preference",
        origin="extracted",
        title="Indentation",
        content=existing_content,
        content_hash=Memory.compute_hash(existing_content),
    )
    memory_store.create_memory(existing)

    new_content = "Prefer spaces over tabs."
    new_hash = Memory.compute_hash(new_content)
    assert new_hash != existing.content_hash  # not exact match

    vector_store = FakeVectorStore(distances={existing.id: 0.10})  # duplicate band

    result = run_write_time_dedup(
        content=new_content,
        content_hash=new_hash,
        embedding=[0.1, 0.2],
        memory_store=memory_store,
        vector_store=vector_store,
    )

    assert result.action == "duplicate"
    assert result.existing_memory_id == existing.id


def test_dedup_vector_conflict_band_keeps_both():
    """Vector distance 0.15–0.35 → conflict (both retained, injection-blocked)."""
    from engram.consolidation.dedup import run_write_time_dedup

    memory_store = FakeMemoryStore()
    content_a = "Python version requirement is 3.10"
    existing = Memory(
        project_id="proj-1",
        scope="project",
        type="project_fact",
        origin="extracted",
        title="Python version",
        content=content_a,
        content_hash=Memory.compute_hash(content_a),
    )
    memory_store.create_memory(existing)

    content_b = "Python version requirement is 3.12"
    hash_b = Memory.compute_hash(content_b)
    assert hash_b != existing.content_hash

    vector_store = FakeVectorStore(distances={existing.id: 0.20})  # conflict band

    result = run_write_time_dedup(
        content=content_b,
        content_hash=hash_b,
        embedding=[0.1, 0.2],
        memory_store=memory_store,
        vector_store=vector_store,
    )

    assert result.action == "conflict"
    assert existing.id in result.conflict_memory_ids

    # Apply the conflict update (what the pipeline does)
    new_memory = Memory(
        project_id="proj-1",
        scope="project",
        type="project_fact",
        origin="extracted",
        title="Python version",
        content=content_b,
        content_hash=hash_b,
        status="conflict",
    )
    memory_store.create_memory(new_memory)
    memory_store.update_memory(existing.id, {"status": "conflict"})

    all_mems = memory_store.list_memories()
    assert len(all_mems) == 2

    # Both are injection-blocked (not "active")
    active = memory_store.list_memories(status="active")
    assert len(active) == 0

    conflict = memory_store.list_memories(status="conflict")
    assert len(conflict) == 2


def test_dedup_vector_independent_returns_insert():
    """Vector distance > 0.35 → independent (new insert)."""
    from engram.consolidation.dedup import run_write_time_dedup

    memory_store = FakeMemoryStore()
    existing_content = "Use SQLite."
    existing = Memory(
        project_id="proj-1",
        scope="project",
        type="decision",
        origin="extracted",
        title="DB choice",
        content=existing_content,
        content_hash=Memory.compute_hash(existing_content),
    )
    memory_store.create_memory(existing)

    new_content = "Run tests with pytest."
    new_hash = Memory.compute_hash(new_content)
    vector_store = FakeVectorStore(distances={existing.id: 0.80})  # independent

    result = run_write_time_dedup(
        content=new_content,
        content_hash=new_hash,
        embedding=[0.5, 0.6],
        memory_store=memory_store,
        vector_store=vector_store,
    )

    assert result.action == "insert"


# ---------------------------------------------------------------------------
# 4. Acceptance criteria (a): confirmed decision → long-term memory with
#    provenance rows.
# ---------------------------------------------------------------------------


def test_confirmed_decision_creates_long_term_memory_with_provenance():
    """A promotable 'decision' event → Memory(type=decision) + MemorySource row."""
    from engram.consolidation.llm import MockLLMClient
    from engram.consolidation.pipeline import ConsolidationWorker

    event_store = FakeEventStore()
    memory_store = FakeMemoryStore()

    project = Project(root_path="/myproject", name="myproject")
    event_store.create_project(project)

    session = AgentSession(
        project_id=project.id,
        external_session_id="ext-1",
        memory_thread_id="thread-1",
        agent="claude_code",
    )
    event_store.create_session(session)

    content = "We decided to use SQLite as the source of truth for Engram."
    event = _make_decision_event(project.id, session.id, content)
    event_store.create_event(event)

    llm = MockLLMClient(
        canned='{"request":"build","completed":"scaffold","learned":"SQLite","next_steps":"tests","files_read":[],"files_modified":[]}'
    )
    worker = ConsolidationWorker(event_store=event_store, memory_store=memory_store, llm=llm)
    worker.enqueue_event(session.id, project.id, event.id)

    asyncio.run(worker.run_once())

    memories = memory_store.list_memories()
    assert len(memories) == 1, f"Expected 1 memory, got {len(memories)}"

    mem = memories[0]
    assert mem.type == "decision"
    assert mem.origin == "extracted"
    assert mem.status == "active"
    assert mem.project_id == project.id

    # Provenance row exists
    assert len(memory_store.sources) == 1
    src = memory_store.sources[0]
    assert src.memory_id == mem.id
    assert src.source_type == "event"
    assert src.source_id == event.id


# ---------------------------------------------------------------------------
# 5. Acceptance criteria (b): contradicting fact → conflict (both retained,
#    injection-blocked), NOT a silent overwrite.
# ---------------------------------------------------------------------------


def test_contradicting_fact_creates_conflict_both_retained_injection_blocked():
    """A conflict-band vector hit → both memories status='conflict', neither active."""
    from engram.consolidation.llm import MockLLMClient
    from engram.consolidation.pipeline import ConsolidationWorker

    event_store = FakeEventStore()
    memory_store = FakeMemoryStore()

    project = Project(root_path="/proj", name="proj")
    event_store.create_project(project)

    session = AgentSession(
        project_id=project.id,
        external_session_id="ext-2",
        memory_thread_id="thread-2",
        agent="claude_code",
    )
    event_store.create_session(session)

    # First fact
    content_a = "Python version requirement is 3.10"
    event_a = _make_fact_event(project.id, session.id, content_a, seq=1)
    event_store.create_event(event_a)

    llm = MockLLMClient(
        canned='{"request":"r","completed":"c","learned":"l","next_steps":"n","files_read":[],"files_modified":[]}'
    )
    worker = ConsolidationWorker(event_store=event_store, memory_store=memory_store, llm=llm)
    worker.enqueue_event(session.id, project.id, event_a.id)
    asyncio.run(worker.run_once())

    assert len(memory_store.list_memories()) == 1
    memory_a = memory_store.list_memories()[0]

    # Second (contradicting) fact — same session, new worker run
    content_b = "Python version requirement is 3.12"
    event_b = _make_fact_event(project.id, session.id, content_b, seq=2)
    event_store.create_event(event_b)

    # Wire vector store so content_b gets distance 0.20 (conflict band) from content_a
    vector_store = FakeVectorStore(distances={memory_a.id: 0.20})
    worker2 = ConsolidationWorker(
        event_store=event_store,
        memory_store=memory_store,
        vector_store=vector_store,
        llm=llm,
    )
    worker2.enqueue_event(session.id, project.id, event_b.id)
    asyncio.run(worker2.run_once())

    all_mems = memory_store.list_memories()
    assert len(all_mems) == 2, f"Expected 2 memories (both retained), got {len(all_mems)}"

    statuses = {m.status for m in all_mems}
    assert statuses == {"conflict"}, f"Both should be 'conflict', got: {statuses}"

    # Injection-blocked: no active memories
    active = memory_store.list_memories(status="active")
    assert len(active) == 0


# ---------------------------------------------------------------------------
# 6. Acceptance criteria (c): duplicate content → access_count increments,
#    no second memory created.
# ---------------------------------------------------------------------------


def test_duplicate_content_increments_access_count_no_new_memory():
    """Exact content_hash match → access_count bumped, no second memory row."""
    from engram.consolidation.llm import MockLLMClient
    from engram.consolidation.pipeline import ConsolidationWorker

    event_store = FakeEventStore()
    memory_store = FakeMemoryStore()

    project = Project(root_path="/proj-dup", name="proj-dup")
    event_store.create_project(project)

    session = AgentSession(
        project_id=project.id,
        external_session_id="ext-3",
        memory_thread_id="thread-3",
        agent="claude_code",
    )
    event_store.create_session(session)

    content = "Use tabs for indentation."
    event1 = _make_decision_event(project.id, session.id, content)
    event_store.create_event(event1)

    llm = MockLLMClient(
        canned='{"request":"r","completed":"c","learned":"l","next_steps":"n","files_read":[],"files_modified":[]}'
    )
    worker = ConsolidationWorker(event_store=event_store, memory_store=memory_store, llm=llm)
    worker.enqueue_event(session.id, project.id, event1.id)
    asyncio.run(worker.run_once())

    assert len(memory_store.list_memories()) == 1
    mem_v1 = memory_store.list_memories()[0]
    assert mem_v1.access_count == 0  # not yet reinforced

    # Same content, second event
    session2 = AgentSession(
        project_id=project.id,
        external_session_id="ext-3b",
        memory_thread_id="thread-3",
        agent="claude_code",
    )
    event_store.create_session(session2)
    event2 = _make_decision_event(project.id, session2.id, content)
    event_store.create_event(event2)

    worker2 = ConsolidationWorker(event_store=event_store, memory_store=memory_store, llm=llm)
    worker2.enqueue_event(session2.id, project.id, event2.id)
    asyncio.run(worker2.run_once())

    # Still exactly one memory
    all_mems = memory_store.list_memories()
    assert len(all_mems) == 1, f"Expected 1 memory (dup suppressed), got {len(all_mems)}"

    # access_count was bumped
    updated = memory_store.get_memory(mem_v1.id)
    assert updated is not None
    assert updated.access_count == 1


# ---------------------------------------------------------------------------
# 7. Acceptance criteria (d): short-term context expires on TTL AND on
#    task completion.
# ---------------------------------------------------------------------------


def test_task_context_expires_on_ttl():
    """Context with ttl_until in the past is NOT returned by list_active."""
    from engram.consolidation.task_context import create_task_context, expire_task_contexts

    memory_store = FakeMemoryStore()

    ctx = create_task_context(
        project_id="proj-ttl",
        session_id="sess-ttl",
        task_key="my-task",
        title="Do something",
        content="In progress",
        memory_store=memory_store,
        ttl_hours=-1,  # Already expired (negative TTL)
    )

    # Not returned as active (TTL expired)
    active = memory_store.list_active_task_contexts("proj-ttl")
    assert len(active) == 0

    # expire_task_contexts returns a count >= 0 and does not raise
    count = expire_task_contexts(memory_store, "proj-ttl")
    assert isinstance(count, int)

    # Verify the context was explicitly marked expired
    updated = memory_store.get_task_context(ctx.id)
    assert updated is not None
    assert updated.status == "expired"


def test_task_context_clears_on_completion():
    """complete_task_context marks status='completed'; context no longer active."""
    from engram.consolidation.task_context import complete_task_context, create_task_context

    memory_store = FakeMemoryStore()

    ctx = create_task_context(
        project_id="proj-complete",
        session_id="sess-complete",
        task_key="build-feature",
        title="Build the feature",
        content="Half done",
        memory_store=memory_store,
    )

    assert len(memory_store.list_active_task_contexts("proj-complete")) == 1

    complete_task_context(memory_store, ctx.id)

    active = memory_store.list_active_task_contexts("proj-complete")
    assert len(active) == 0

    updated = memory_store.get_task_context(ctx.id)
    assert updated is not None
    assert updated.status == "completed"


# ---------------------------------------------------------------------------
# 8. Session summary written during consolidation
# ---------------------------------------------------------------------------


def test_session_summary_created_during_consolidation():
    """run_once writes a SessionSummary for the processed session."""
    from engram.consolidation.llm import MockLLMClient
    from engram.consolidation.pipeline import ConsolidationWorker

    event_store = FakeEventStore()
    memory_store = FakeMemoryStore()

    project = Project(root_path="/proj-summary", name="proj-summary")
    event_store.create_project(project)

    session = AgentSession(
        project_id=project.id,
        external_session_id="ext-sum",
        memory_thread_id="thread-sum",
        agent="claude_code",
    )
    event_store.create_session(session)

    content = "Refactored the DB layer."
    event = _make_fact_event(project.id, session.id, content)
    event_store.create_event(event)

    canned_summary = (
        '{"request":"refactor","completed":"db layer done",'
        '"learned":"sqlite is fast","next_steps":"add indexes",'
        '"files_read":["src/db.py"],"files_modified":["src/db.py"]}'
    )
    llm = MockLLMClient(canned=canned_summary)
    worker = ConsolidationWorker(event_store=event_store, memory_store=memory_store, llm=llm)
    worker.enqueue_event(session.id, project.id, event.id)

    result = asyncio.run(worker.run_once())

    assert result["summaries_created"] == 1
    assert len(memory_store._summaries) == 1

    summary = list(memory_store._summaries.values())[0]
    assert summary.session_id == session.id
    assert summary.project_id == project.id
    assert summary.request == "refactor"
    assert any("db.py" in f for f in summary.files_read)


# ---------------------------------------------------------------------------
# 9. build_session_summary — LLM output parsing
# ---------------------------------------------------------------------------


def test_build_session_summary_parses_llm_output():
    from engram.consolidation.llm import MockLLMClient
    from engram.consolidation.summarize import build_session_summary

    project = Project(root_path="/p", name="p")
    session = AgentSession(
        project_id=project.id,
        external_session_id="e",
        memory_thread_id="t",
        agent="claude_code",
    )
    payload = {"content": "did stuff"}
    event = Event(
        project_id=project.id,
        session_id=session.id,
        seq=1,
        source_type="transcript",
        event_type="fact",
        payload=payload,
        content_hash=Event.compute_hash(payload),
        occurred_at=datetime.now(UTC),
    )

    llm = MockLLMClient(
        canned='{"request":"req","completed":"done","learned":"learned","next_steps":"next","files_read":["a.py"],"files_modified":["b.py"]}'
    )
    summary = build_session_summary(session=session, events=[event], llm=llm)

    assert summary.request == "req"
    assert summary.completed == "done"
    assert "a.py" in summary.files_read
    assert "b.py" in summary.files_modified
    assert event.id in summary.source_event_ids


def test_build_session_summary_handles_bad_json():
    """Malformed LLM output → None (no crash, no fabricated empty summary)."""
    from engram.consolidation.llm import MockLLMClient
    from engram.consolidation.summarize import build_session_summary

    project = Project(root_path="/p2", name="p2")
    session = AgentSession(
        project_id=project.id,
        external_session_id="e2",
        memory_thread_id="t2",
        agent="claude_code",
    )

    llm = MockLLMClient(canned="this is not json {{{ broken")
    summary = build_session_summary(session=session, events=[], llm=llm, hint="test hint")

    # Unparseable output must not persist a fake summary — distinguishes
    # "LLM failed" from "genuinely empty session".
    assert summary is None


def test_build_session_summary_empty_output_returns_none():
    """Empty LLM output (e.g. no-key MockLLMClient) → None, not an empty summary."""
    from engram.consolidation.llm import MockLLMClient
    from engram.consolidation.summarize import build_session_summary

    project = Project(root_path="/p3", name="p3")
    session = AgentSession(
        project_id=project.id,
        external_session_id="e3",
        memory_thread_id="t3",
        agent="claude_code",
    )

    summary = build_session_summary(session=session, events=[], llm=MockLLMClient())
    assert summary is None


# ---------------------------------------------------------------------------
# 10. Promote helpers
# ---------------------------------------------------------------------------


def test_classify_event_for_promotion_decision():
    from engram.consolidation.promote import classify_event_for_promotion

    payload = {"content": "x"}
    event = Event(
        project_id="p",
        session_id="s",
        seq=1,
        source_type="transcript",
        event_type="decision",
        payload=payload,
        content_hash=Event.compute_hash(payload),
        occurred_at=datetime.now(UTC),
    )
    assert classify_event_for_promotion(event) == "confirmed_decision"


def test_classify_event_for_promotion_unknown():
    from engram.consolidation.promote import classify_event_for_promotion

    payload = {"content": "x"}
    event = Event(
        project_id="p",
        session_id="s",
        seq=1,
        source_type="transcript",
        event_type="tool_call",
        payload=payload,
        content_hash=Event.compute_hash(payload),
        occurred_at=datetime.now(UTC),
    )
    assert classify_event_for_promotion(event) is None


# ---------------------------------------------------------------------------
# 11. MockLLMClient
# ---------------------------------------------------------------------------


def test_mock_llm_client_call_count():
    from engram.consolidation.llm import MockLLMClient

    llm = MockLLMClient(canned="response")
    llm.complete("hello")
    llm.complete("world")
    assert llm.call_count == 2


def test_mock_llm_client_returns_canned():
    from engram.consolidation.llm import MockLLMClient

    llm = MockLLMClient(canned="my response")
    assert llm.complete("anything") == "my response"


# ---------------------------------------------------------------------------
# 12. AnthropicLLMClient — import-only test (no live key required)
# ---------------------------------------------------------------------------


def test_anthropic_llm_client_importable():
    """AnthropicLLMClient must be importable without a live API key."""
    from engram.consolidation.llm import AnthropicLLMClient  # noqa: F401

    client = AnthropicLLMClient()
    assert client._model == "claude-sonnet-4-6"
