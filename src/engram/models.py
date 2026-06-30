"""Pydantic models for every Engram table (spec §9.1).

These are the contract: every workstream (capture, consolidation, retrieval)
programs to these models.  Storage is the only layer that touches SQLite row
dicts directly; everything above sees typed Python objects.

JSON columns (payload, changed_files, …) are surfaced as real Python types
(dict / list) here.  The SQLite store serialises them with json.dumps/loads.

All datetime fields carry UTC timezone.  content_hash fields use the helpers
on their respective classes so callers don't have to know the algorithm.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Enum-like literals — shared across models and MCP tool signatures
# ---------------------------------------------------------------------------

AgentSessionStatus = Literal["active", "completed", "failed"]
SourceType = Literal["hook", "mcp", "cli", "api", "transcript"]
CaptureConfidence = Literal["exact", "likely", "unknown"]
TaskContextStatus = Literal["active", "completed", "expired"]
MemoryScope = Literal["user", "project", "session"]
MemoryType = Literal[
    "preference", "decision", "project_fact", "failure_pattern", "command", "constraint"
]
MemoryOrigin = Literal["user", "extracted", "synthesized"]
MemoryStatus = Literal["active", "stale", "superseded", "conflict", "deleted"]
MemorySourceType = Literal["event", "session_summary", "manual", "import"]
UpdateOperation = Literal["supersede", "mark_stale", "resolve_conflict", "delete", "reinforce"]


# ---------------------------------------------------------------------------
# projects
# ---------------------------------------------------------------------------


class Project(BaseModel):
    """A repository or working-tree root that Engram tracks memories for."""

    id: str = Field(default_factory=_uuid)
    root_path: str
    name: str
    repo_url: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# agent_sessions
# ---------------------------------------------------------------------------


class AgentSession(BaseModel):
    """One platform-level coding-agent session (e.g. one Claude Code invocation).

    Many sessions can belong to a single memory_thread_id — that thread is
    the durable cross-session continuity unit (spec §9.2).
    """

    id: str = Field(default_factory=_uuid)
    project_id: str
    external_session_id: str
    memory_thread_id: str
    agent: str  # e.g. "claude_code" | "codex" | "cursor" | "generic"
    branch: str | None = None
    git_sha: str | None = None
    status: AgentSessionStatus = "active"
    started_at: datetime = Field(default_factory=_now)
    ended_at: datetime | None = None


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------


class Event(BaseModel):
    """Append-only raw event record — the source of truth for capture.

    seq is monotonic per session; a gap proves capture loss (ADR 0004).
    raw_ref_file + raw_ref_offset enable deterministic replay from the
    official transcript JSONL (ADR 0002).
    """

    id: str = Field(default_factory=_uuid)
    project_id: str
    session_id: str
    seq: int  # monotonic per-session; UNIQUE(session_id, seq) in DB
    source_type: SourceType
    source_seq: int | None = None  # line/record index in source JSONL
    raw_ref_file: str | None = None  # absolute path of source transcript
    raw_ref_offset: int | None = None  # byte offset of this record in raw_ref_file
    capture_confidence: CaptureConfidence = "unknown"
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    content_hash: str  # sha256 of canonical payload JSON
    occurred_at: datetime
    created_at: datetime = Field(default_factory=_now)

    @staticmethod
    def compute_hash(payload: dict[str, Any]) -> str:
        """SHA-256 of the canonical JSON payload; use for write-time dedup."""
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# task_contexts  (short-term memory)
# ---------------------------------------------------------------------------


class TaskContext(BaseModel):
    """Active-task handoff state: what the next agent needs to continue work.

    Scoped to one project + task; expires via TTL; not promoted to long-term
    retrieval (spec §6.1).
    """

    id: str = Field(default_factory=_uuid)
    project_id: str
    session_id: str
    task_key: str
    title: str
    content: str
    changed_files: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    status: TaskContextStatus = "active"
    ttl_until: datetime | None = None
    source_event_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# memories  (long-term memory)
# ---------------------------------------------------------------------------


class Memory(BaseModel):
    """A durable long-term memory record: decision, preference, project fact, etc.

    origin distinguishes human-provided memories from machine-extracted or
    machine-synthesised ones — required for stale_injection_rate (spec §27.1).

    content_hash enables exact-match dedup before any embedding lookup (spec §27.1).

    file_path + file_hash enable changed-code invalidation: at recall time, if
    sha256(file_path) != file_hash, surface this memory as stale (ADR 0005).

    access_count is a recall-time reinforcement signal that counteracts confidence
    decay (spec §27.2).
    """

    id: str = Field(default_factory=_uuid)
    project_id: str
    scope: MemoryScope
    type: MemoryType
    origin: MemoryOrigin
    title: str
    content: str
    content_hash: str  # sha256(content); exact-match dedup at write time
    status: MemoryStatus = "active"
    confidence: float = 1.0  # [0.0, 1.0]
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    last_seen_at: datetime | None = None
    access_count: int = 0
    file_path: str | None = None
    file_hash: str | None = None  # sha256(file_path bytes) at write time
    supersedes_memory_id: str | None = None
    source_event_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @staticmethod
    def compute_hash(content: str) -> str:
        """SHA-256 of the memory content; use for exact-match dedup."""
        return hashlib.sha256(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# session_summaries
# ---------------------------------------------------------------------------


class SessionSummary(BaseModel):
    """Compact end-of-session summary written during session_end consolidation.

    Structured to answer the most common 'what happened last session?' questions
    without requiring full event replay.
    """

    id: str = Field(default_factory=_uuid)
    project_id: str
    session_id: str
    request: str  # the task / goal the agent was working on
    completed: str  # what was actually finished
    learned: str  # insights / decisions surfaced
    next_steps: str  # what the next session should do first
    files_read: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    source_event_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# memory_sources  (provenance)
# ---------------------------------------------------------------------------


class MemorySource(BaseModel):
    """Provenance link from a memory back to the event or summary that produced it.

    Multiple sources can back a single memory (reinforcement case).
    """

    id: str = Field(default_factory=_uuid)
    memory_id: str
    source_type: MemorySourceType
    source_id: str  # event.id, session_summary.id, or import name
    quote_or_summary: str | None = None
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# retrieval_traces
# ---------------------------------------------------------------------------


class RetrievalTrace(BaseModel):
    """Full audit trail for one retrieval call: candidates, selected, scores, budget.

    Used to debug rank regressions and stale/conflict injection (spec §12.3).
    """

    id: str = Field(default_factory=_uuid)
    query: str
    project_id: str
    selected_memory_ids: list[str] = Field(default_factory=list)
    candidate_memory_ids: list[str] = Field(default_factory=list)
    ranking_features: dict[str, Any] = Field(default_factory=dict)
    token_budget: int
    injected_tokens: int = 0
    outcome_label: str | None = None  # e.g. "good", "stale_injected", "miss"
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# eval_cases
# ---------------------------------------------------------------------------


class EvalCase(BaseModel):
    """One replay-eval query with a gold set of expected memory IDs (spec §12.1)."""

    id: str = Field(default_factory=_uuid)
    query: str
    project_id: str
    expected_memory_ids: list[str] = Field(default_factory=list)
    expected_memory_types: list[str] = Field(default_factory=list)
    must_not_include_ids: list[str] = Field(default_factory=list)
    expected_behavior: str | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# eval_runs
# ---------------------------------------------------------------------------


class EvalRun(BaseModel):
    """Aggregate metrics for one pass of the eval suite (spec §12.2)."""

    id: str = Field(default_factory=_uuid)
    run_name: str
    recall_at_5: float = 0.0  # fraction of cases where expected memory is in top 5
    mrr: float = 0.0  # mean reciprocal rank
    stale_injection_rate: float = 0.0  # stale memories injected / total injected
    conflict_injection_rate: float = 0.0  # conflicting memories injected / total injected
    avg_injected_tokens: float = 0.0
    abstain_rate: float = 0.0  # correctly-abstained cases / cases expecting abstain
    created_at: datetime = Field(default_factory=_now)
