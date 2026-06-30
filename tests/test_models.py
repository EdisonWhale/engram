"""Round-trip tests for every pydantic model.

Each test:
1. Creates a model instance with all fields populated (including optional ones).
2. Serialises to dict.
3. Reconstructs via model_validate.
4. Asserts fields are equal.

Also tests the hash helpers on Event and Memory.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

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

_NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _roundtrip(model_instance):
    """Serialise → reconstruct → assert equal."""
    data = model_instance.model_dump()
    restored = type(model_instance).model_validate(data)
    assert restored == model_instance
    return restored


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


def test_project_roundtrip() -> None:
    p = Project(
        id="proj-1",
        root_path="/home/user/my_project",
        name="my_project",
        repo_url="https://github.com/user/my_project",
        created_at=_NOW,
        updated_at=_NOW,
    )
    _roundtrip(p)


def test_project_optional_repo_url() -> None:
    p = Project(id="p2", root_path="/tmp/x", name="x")
    assert p.repo_url is None
    _roundtrip(p)


def test_project_default_id_generated() -> None:
    p1 = Project(root_path="/a", name="a")
    p2 = Project(root_path="/b", name="b")
    assert p1.id != p2.id  # UUIDs are unique


# ---------------------------------------------------------------------------
# AgentSession
# ---------------------------------------------------------------------------


def test_agent_session_roundtrip() -> None:
    s = AgentSession(
        id="sess-1",
        project_id="proj-1",
        external_session_id="claude-ext-abc",
        memory_thread_id="thread-xyz",
        agent="claude_code",
        branch="main",
        git_sha="deadbeef",
        status="active",
        started_at=_NOW,
        ended_at=None,
    )
    _roundtrip(s)


def test_agent_session_ended() -> None:
    s = AgentSession(
        id="s2",
        project_id="p",
        external_session_id="e",
        memory_thread_id="t",
        agent="codex",
        status="completed",
        started_at=_NOW,
        ended_at=_NOW,
    )
    _roundtrip(s)


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------


def test_event_roundtrip() -> None:
    payload = {"tool": "Bash", "command": "pytest"}
    e = Event(
        id="ev-1",
        project_id="proj-1",
        session_id="sess-1",
        seq=42,
        source_type="transcript",
        source_seq=100,
        raw_ref_file="/home/.claude/projects/-tmp-x/abc.jsonl",
        raw_ref_offset=1024,
        capture_confidence="exact",
        event_type="tool_call",
        payload=payload,
        content_hash=Event.compute_hash(payload),
        occurred_at=_NOW,
        created_at=_NOW,
    )
    _roundtrip(e)


def test_event_compute_hash_deterministic() -> None:
    payload = {"b": 2, "a": 1}
    h1 = Event.compute_hash(payload)
    h2 = Event.compute_hash({"a": 1, "b": 2})  # same keys, different insertion order
    assert h1 == h2  # keys are sorted before hashing


def test_event_compute_hash_different_payloads() -> None:
    assert Event.compute_hash({"a": 1}) != Event.compute_hash({"a": 2})


def test_event_optional_fields_none() -> None:
    e = Event(
        id="ev-2",
        project_id="p",
        session_id="s",
        seq=1,
        source_type="mcp",
        event_type="user_prompt",
        payload={},
        content_hash=Event.compute_hash({}),
        occurred_at=_NOW,
    )
    assert e.source_seq is None
    assert e.raw_ref_file is None
    assert e.raw_ref_offset is None
    _roundtrip(e)


# ---------------------------------------------------------------------------
# TaskContext
# ---------------------------------------------------------------------------


def test_task_context_roundtrip() -> None:
    ctx = TaskContext(
        id="tc-1",
        project_id="proj-1",
        session_id="sess-1",
        task_key="eval-runner",
        title="Implement eval runner",
        content="Recall@5 and MRR are not yet implemented.",
        changed_files=["src/evals/runner.py"],
        next_steps=["compute MRR", "run tests"],
        status="active",
        ttl_until=_NOW,
        source_event_ids=["ev-1", "ev-2"],
        created_at=_NOW,
        updated_at=_NOW,
    )
    _roundtrip(ctx)


def test_task_context_empty_lists() -> None:
    ctx = TaskContext(
        id="tc-2",
        project_id="p",
        session_id="s",
        task_key="k",
        title="t",
        content="c",
    )
    assert ctx.changed_files == []
    assert ctx.next_steps == []
    _roundtrip(ctx)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def test_memory_roundtrip() -> None:
    content = "SQLite is the source of truth; vector index is derived."
    m = Memory(
        id="mem-1",
        project_id="proj-1",
        scope="project",
        type="decision",
        origin="user",
        title="SQLite source of truth",
        content=content,
        content_hash=Memory.compute_hash(content),
        status="active",
        confidence=0.95,
        valid_from=_NOW,
        valid_until=None,
        last_seen_at=_NOW,
        access_count=3,
        file_path="src/engram/db/runner.py",
        file_hash="abc123",
        supersedes_memory_id=None,
        source_event_ids=["ev-1"],
        metadata={"source": "adr-0001"},
        created_at=_NOW,
        updated_at=_NOW,
    )
    _roundtrip(m)


def test_memory_compute_hash() -> None:
    h = Memory.compute_hash("Hello world")
    assert isinstance(h, str)
    assert len(h) == 64  # SHA-256 hex is 64 chars
    assert Memory.compute_hash("Hello world") == h  # deterministic
    assert Memory.compute_hash("Different") != h


def test_memory_all_types() -> None:
    """Verify all allowed type/scope/origin/status combos pass validation."""
    from engram.models import MemoryOrigin, MemoryScope, MemoryType

    for mtype in MemoryType.__args__:
        for scope in MemoryScope.__args__:
            for origin in MemoryOrigin.__args__:
                m = Memory(
                    project_id="p",
                    scope=scope,
                    type=mtype,
                    origin=origin,
                    title="t",
                    content="c",
                    content_hash=f"h-{mtype}-{scope}-{origin}",
                )
                assert m.type == mtype


# ---------------------------------------------------------------------------
# SessionSummary
# ---------------------------------------------------------------------------


def test_session_summary_roundtrip() -> None:
    ss = SessionSummary(
        id="sum-1",
        project_id="proj-1",
        session_id="sess-1",
        request="Implement retrieval eval runner",
        completed="Created schema and runner stub",
        learned="Recall@5 needs gold-set fixture",
        next_steps="Finish MRR calculation",
        files_read=["src/evals/schema.py"],
        files_modified=["src/evals/runner.py"],
        source_event_ids=["ev-1", "ev-2"],
        created_at=_NOW,
    )
    _roundtrip(ss)


# ---------------------------------------------------------------------------
# MemorySource
# ---------------------------------------------------------------------------


def test_memory_source_roundtrip() -> None:
    ms = MemorySource(
        id="src-1",
        memory_id="mem-1",
        source_type="event",
        source_id="ev-1",
        quote_or_summary="Agent decided SQLite for local-first.",
        created_at=_NOW,
    )
    _roundtrip(ms)


def test_memory_source_no_quote() -> None:
    ms = MemorySource(
        id="src-2",
        memory_id="mem-1",
        source_type="manual",
        source_id="cli-import-001",
    )
    assert ms.quote_or_summary is None
    _roundtrip(ms)


# ---------------------------------------------------------------------------
# RetrievalTrace
# ---------------------------------------------------------------------------


def test_retrieval_trace_roundtrip() -> None:
    rt = RetrievalTrace(
        id="trace-1",
        query="continue the eval work",
        project_id="proj-1",
        selected_memory_ids=["mem-1", "mem-2"],
        candidate_memory_ids=["mem-1", "mem-2", "mem-3"],
        ranking_features={"bm25_score": 0.8, "recency": 0.9},
        token_budget=1200,
        injected_tokens=350,
        outcome_label="good",
        created_at=_NOW,
    )
    _roundtrip(rt)


# ---------------------------------------------------------------------------
# EvalCase
# ---------------------------------------------------------------------------


def test_eval_case_roundtrip() -> None:
    ec = EvalCase(
        id="ec-1",
        query="continue the retrieval eval implementation",
        project_id="proj-1",
        expected_memory_ids=["mem-decision-sqlite", "task-eval-next-step"],
        expected_memory_types=["decision", "project_fact"],
        must_not_include_ids=["old-go-implementation-plan"],
        expected_behavior="inject task context + decision",
        tags=["handoff", "evals", "stale"],
        created_at=_NOW,
    )
    _roundtrip(ec)


# ---------------------------------------------------------------------------
# EvalRun
# ---------------------------------------------------------------------------


def test_eval_run_roundtrip() -> None:
    er = EvalRun(
        id="run-1",
        run_name="baseline-2024-06-15",
        recall_at_5=0.80,
        mrr=0.72,
        stale_injection_rate=0.05,
        conflict_injection_rate=0.02,
        avg_injected_tokens=420.0,
        abstain_rate=0.90,
        created_at=_NOW,
    )
    _roundtrip(er)


# ---------------------------------------------------------------------------
# SQLite store round-trips (integration: model → DB → model)
# ---------------------------------------------------------------------------


@pytest.fixture()
def stores():
    """In-memory DB with both stores ready."""
    from engram.db.runner import open_db
    from engram.store import SQLiteEventStore, SQLiteMemoryStore

    conn = open_db(":memory:")
    return SQLiteEventStore(conn), SQLiteMemoryStore(conn)


def test_project_store_roundtrip(stores) -> None:
    event_store, _ = stores
    p = Project(root_path="/tmp/myproj", name="myproj")
    event_store.create_project(p)
    fetched = event_store.get_project(p.id)
    assert fetched is not None
    assert fetched.id == p.id
    assert fetched.root_path == p.root_path


def test_session_store_roundtrip(stores) -> None:
    event_store, _ = stores
    p = Project(root_path="/tmp/proj2", name="proj2")
    event_store.create_project(p)

    s = AgentSession(
        project_id=p.id,
        external_session_id="ext-123",
        memory_thread_id="thread-abc",
        agent="claude_code",
        branch="feature/evals",
        git_sha="cafebabe",
    )
    event_store.create_session(s)
    fetched = event_store.get_session(s.id)
    assert fetched is not None
    assert fetched.memory_thread_id == "thread-abc"
    assert fetched.status == "active"


def test_event_store_roundtrip(stores) -> None:
    event_store, _ = stores
    p = Project(root_path="/tmp/proj3", name="proj3")
    event_store.create_project(p)
    s = AgentSession(
        project_id=p.id,
        external_session_id="e",
        memory_thread_id="t",
        agent="claude_code",
    )
    event_store.create_session(s)

    payload = {"command": "pytest -x"}
    ev = Event(
        project_id=p.id,
        session_id=s.id,
        seq=1,
        source_type="transcript",
        event_type="tool_call",
        payload=payload,
        content_hash=Event.compute_hash(payload),
        occurred_at=_NOW,
    )
    event_store.create_event(ev)

    fetched = event_store.get_event(ev.id)
    assert fetched is not None
    assert fetched.seq == 1
    assert fetched.payload == payload
    assert fetched.content_hash == ev.content_hash


def test_event_max_seq(stores) -> None:
    event_store, _ = stores
    p = Project(root_path="/tmp/proj4", name="proj4")
    event_store.create_project(p)
    s = AgentSession(project_id=p.id, external_session_id="e", memory_thread_id="t", agent="a")
    event_store.create_session(s)

    assert event_store.max_seq_for_session(s.id) == 0

    for seq in [1, 2, 3]:
        payload = {"seq": seq}
        event_store.create_event(
            Event(
                project_id=p.id,
                session_id=s.id,
                seq=seq,
                source_type="mcp",
                event_type="ping",
                payload=payload,
                content_hash=Event.compute_hash(payload),
                occurred_at=_NOW,
            )
        )
    assert event_store.max_seq_for_session(s.id) == 3


def test_memory_store_roundtrip(stores) -> None:
    event_store, memory_store = stores
    p = Project(root_path="/tmp/proj5", name="proj5")
    event_store.create_project(p)

    content = "Prefer SQLite over Postgres for local-first tools."
    m = Memory(
        project_id=p.id,
        scope="project",
        type="decision",
        origin="user",
        title="SQLite preference",
        content=content,
        content_hash=Memory.compute_hash(content),
    )
    memory_store.create_memory(m)

    fetched = memory_store.get_memory(m.id)
    assert fetched is not None
    assert fetched.content == content
    assert fetched.confidence == 1.0
    assert fetched.access_count == 0


def test_memory_dedup_by_hash(stores) -> None:
    event_store, memory_store = stores
    p = Project(root_path="/tmp/proj6", name="proj6")
    event_store.create_project(p)

    content = "Unique content"
    h = Memory.compute_hash(content)
    m = Memory(
        project_id=p.id,
        scope="project",
        type="decision",
        origin="user",
        title="t",
        content=content,
        content_hash=h,
    )
    memory_store.create_memory(m)

    existing = memory_store.get_memory_by_hash(h)
    assert existing is not None
    assert existing.id == m.id


def test_task_context_store_roundtrip(stores) -> None:
    event_store, memory_store = stores
    p = Project(root_path="/tmp/proj7", name="proj7")
    event_store.create_project(p)
    s = AgentSession(project_id=p.id, external_session_id="e", memory_thread_id="t", agent="a")
    event_store.create_session(s)

    ctx = TaskContext(
        project_id=p.id,
        session_id=s.id,
        task_key="eval-work",
        title="Finish eval runner",
        content="MRR not yet computed.",
        next_steps=["implement MRR"],
    )
    memory_store.create_task_context(ctx)

    active = memory_store.list_active_task_contexts(p.id)
    assert len(active) == 1
    assert active[0].task_key == "eval-work"


def test_session_summary_store_roundtrip(stores) -> None:
    event_store, memory_store = stores
    p = Project(root_path="/tmp/proj8", name="proj8")
    event_store.create_project(p)
    s = AgentSession(project_id=p.id, external_session_id="e", memory_thread_id="t", agent="a")
    event_store.create_session(s)

    summary = SessionSummary(
        project_id=p.id,
        session_id=s.id,
        request="implement evals",
        completed="schema done",
        learned="need gold set",
        next_steps="compute MRR",
    )
    memory_store.create_session_summary(summary)

    fetched = memory_store.get_session_summary(summary.id)
    assert fetched is not None
    assert fetched.request == "implement evals"


def test_eval_run_store_roundtrip(stores) -> None:
    _, memory_store = stores
    run = EvalRun(
        run_name="baseline",
        recall_at_5=0.8,
        mrr=0.7,
        conflict_injection_rate=0.1,
        abstain_rate=0.9,
    )
    memory_store.create_eval_run(run)

    runs = memory_store.list_eval_runs()
    assert len(runs) == 1
    assert runs[0].run_name == "baseline"
    assert runs[0].conflict_injection_rate == 0.1
    assert runs[0].abstain_rate == 0.9


def test_eval_case_store_roundtrip(stores) -> None:
    event_store, memory_store = stores
    p = Project(root_path="/tmp/proj-eval-case", name="proj-eval-case")
    event_store.create_project(p)

    case = EvalCase(
        query="continue the retrieval eval implementation",
        project_id=p.id,
        expected_memory_ids=["mem-1"],
        expected_memory_types=["decision"],
        must_not_include_ids=["mem-old"],
        tags=["handoff"],
    )
    memory_store.create_eval_case(case)

    cases = memory_store.list_eval_cases(project_id=p.id)
    assert len(cases) == 1
    assert cases[0].expected_memory_types == ["decision"]
    assert cases[0].must_not_include_ids == ["mem-old"]
