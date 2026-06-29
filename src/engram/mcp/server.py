"""Engram MCP server — all tools registered as validated stubs.

Transport: stdio (ADR 0006).  No HTTP / no FastAPI / no Flask.

Each tool:
1. Defines pydantic input validation via a *Params model.
2. Returns a typed placeholder dict (TypedDict) so callers know the schema.
3. Contains a docstring explaining purpose and which workstream implements it.

Workstreams that fill in these stubs:
- WS-A (capture):       session_start, record_event, session_end
- WS-B (consolidation): memory_consolidate, session_end (summary side)
- WS-C (retrieval):     memory_search, memory_timeline, memory_get,
                        memory_context, memory_list
- WS-D (eval/mgmt):     memory_add, memory_update (plus eval runner)
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from engram.models import (
    MemoryScope,
    MemoryStatus,
    MemoryType,
    UpdateOperation,
)

mcp = FastMCP("engram")

# ---------------------------------------------------------------------------
# Pydantic input models — used for explicit validation inside each stub.
# FastMCP also validates via type annotations; these models provide a clear
# contract for callers and enable direct unit-testing of validation logic.
# ---------------------------------------------------------------------------


class SessionStartParams(BaseModel):
    project_path: str
    agent: str
    prompt: str
    git_sha: str
    branch: str


class RecordEventParams(BaseModel):
    session_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SessionEndParams(BaseModel):
    session_id: str
    summary_hint: str | None = None


class MemorySearchParams(BaseModel):
    query: str
    project: str | None = None
    file: str | None = None
    type: MemoryType | None = None
    limit: int = Field(default=10, ge=1, le=100)


class MemoryTimelineParams(BaseModel):
    anchor_id: str | None = None
    query: str | None = None
    before: int = Field(default=3, ge=0, le=20)
    after: int = Field(default=3, ge=0, le=20)


class MemoryGetParams(BaseModel):
    ids: list[str] = Field(min_length=1)


class MemoryContextParams(BaseModel):
    query: str
    token_budget: int = Field(default=1200, ge=100, le=8000)


class MemoryAddParams(BaseModel):
    content: str
    type: MemoryType
    scope: MemoryScope
    project: str | None = None
    metadata: dict[str, Any] | None = None


class MemoryUpdateParams(BaseModel):
    memory_id: str
    operation: UpdateOperation
    content: str | None = None
    reason: str | None = None


class MemoryListParams(BaseModel):
    project: str | None = None
    type: MemoryType | None = None
    status: MemoryStatus | None = None


class MemoryConsolidateParams(BaseModel):
    project: str | None = None
    session_id: str | None = None


# ---------------------------------------------------------------------------
# Session tools  (WS-A implements the bodies)
# ---------------------------------------------------------------------------


@mcp.tool()
def session_start(
    project_path: str,
    agent: str,
    prompt: str,
    git_sha: str,
    branch: str,
) -> dict[str, Any]:
    """Create or resume an agent session and return initial memory context.

    Resolves or mints a memory_thread_id using the active task_context rules
    (spec §9.2).  Returns the session id and any relevant prior memories for
    the agent to inject at the start of a new session.

    Implemented by WS-A.
    """
    params = SessionStartParams(
        project_path=project_path,
        agent=agent,
        prompt=prompt,
        git_sha=git_sha,
        branch=branch,
    )
    return {
        "session_id": "stub",
        "memory_thread_id": "stub",
        "thread_ambiguous": False,
        "context": "",
        "project_id": "stub",
        # Echo validated params so callers can see what was accepted
        "_params": params.model_dump(),
    }


@mcp.tool()
def record_event(
    session_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a normalised raw event to the session's event log.

    Events are append-only (ADR 0004).  The capture workstream computes
    seq, content_hash, and raw_ref fields before delegating to the EventStore.

    Implemented by WS-A.
    """
    params = RecordEventParams(
        session_id=session_id,
        event_type=event_type,
        payload=payload or {},
    )
    return {
        "event_id": "stub",
        "seq": 0,
        "content_hash": "stub",
        "_params": params.model_dump(),
    }


@mcp.tool()
def session_end(
    session_id: str,
    summary_hint: str | None = None,
) -> dict[str, Any]:
    """Close a session, write a summary, and queue consolidation.

    Updates session status to 'completed', persists a SessionSummary, and
    queues long-term memory candidates for the consolidation worker (WS-B).
    Runs sequence-gap reconciliation (ADR 0004).

    Implemented by WS-A (close/summary) and WS-B (consolidation queue).
    """
    params = SessionEndParams(session_id=session_id, summary_hint=summary_hint)
    return {
        "session_id": params.session_id,
        "status": "completed",
        "summary_id": "stub",
        "events_captured": 0,
        "capture_complete": True,
        "_params": params.model_dump(),
    }


# ---------------------------------------------------------------------------
# Retrieval tools  (WS-C implements the bodies)
# ---------------------------------------------------------------------------


@mcp.tool()
def memory_search(
    query: str,
    project: str | None = None,
    file: str | None = None,
    type: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Return a compact candidate index: IDs, titles, type, age, status, provenance.

    Stage 1 of the progressive-disclosure workflow (spec §11.2).
    Agents should inspect these lightweight rows before calling memory_get.

    Implemented by WS-C.
    """
    params = MemorySearchParams(query=query, project=project, file=file, type=type, limit=limit)
    return {
        "memories": [],
        "total": 0,
        "query": params.query,
        "_params": params.model_dump(),
    }


@mcp.tool()
def memory_timeline(
    anchor_id: str | None = None,
    query: str | None = None,
    before: int = 3,
    after: int = 3,
) -> dict[str, Any]:
    """Return chronological context surrounding a memory or query match.

    Stage 2 of the progressive-disclosure workflow.  Useful for understanding
    what happened before and after a specific decision or event.

    Implemented by WS-C.
    """
    params = MemoryTimelineParams(anchor_id=anchor_id, query=query, before=before, after=after)
    return {
        "anchor": None,
        "before": [],
        "after": [],
        "_params": params.model_dump(),
    }


@mcp.tool()
def memory_get(ids: list[str]) -> dict[str, Any]:
    """Fetch full memory records for a specific set of IDs.

    Stage 3 of the progressive-disclosure workflow — only call after
    filtering candidates with memory_search / memory_timeline.

    Implemented by WS-C.
    """
    params = MemoryGetParams(ids=ids)
    return {
        "memories": [],
        "not_found": [],
        "_params": params.model_dump(),
    }


@mcp.tool()
def memory_context(
    query: str,
    token_budget: int = 1200,
) -> dict[str, Any]:
    """Return final prompt-ready context assembled under a token budget.

    Combines short-term task context + ranked long-term memories into an
    injectable string.  Does not inject stale, conflicting, or deleted memories.

    Implemented by WS-C.
    """
    params = MemoryContextParams(query=query, token_budget=token_budget)
    return {
        "context": "",
        "injected_tokens": 0,
        "memory_ids": [],
        "trace_id": "stub",
        "_params": params.model_dump(),
    }


# ---------------------------------------------------------------------------
# Memory management tools  (WS-D / manual use)
# ---------------------------------------------------------------------------


@mcp.tool()
def memory_add(
    content: str,
    type: str,
    scope: str,
    project: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Manually insert a confirmed fact, preference, or decision as a memory.

    Performs exact-match dedup via content_hash before insert.
    Valid type: preference | decision | project_fact | failure_pattern | command | constraint.
    Valid scope: user | project | session.

    Implemented by WS-D.
    """
    params = MemoryAddParams(
        content=content, type=type, scope=scope, project=project, metadata=metadata
    )
    return {
        "memory_id": "stub",
        "deduplicated": False,
        "_params": params.model_dump(),
    }


@mcp.tool()
def memory_update(
    memory_id: str,
    operation: str,
    content: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Update a memory's status via a named operation.

    Valid operations:
    - supersede: mark old memory superseded and link a replacement.
    - mark_stale: reduce confidence; surface as stale.
    - resolve_conflict: pick one of two conflicting memories as canonical.
    - delete: soft-delete (tombstone); does not hard-delete by default.
    - reinforce: bump access_count and last_seen_at.

    Implemented by WS-D.
    """
    params = MemoryUpdateParams(
        memory_id=memory_id, operation=operation, content=content, reason=reason
    )
    return {
        "memory_id": params.memory_id,
        "operation": params.operation,
        "success": True,
        "_params": params.model_dump(),
    }


@mcp.tool()
def memory_list(
    project: str | None = None,
    type: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """List memories for inspection and admin use.

    Valid status values: active | stale | superseded | conflict | deleted.

    Implemented by WS-C.
    """
    params = MemoryListParams(project=project, type=type, status=status)
    return {
        "memories": [],
        "total": 0,
        "_params": params.model_dump(),
    }


@mcp.tool()
def memory_consolidate(
    project: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Manually trigger the consolidation worker for a project or session.

    Consolidation is normally queued at session_end; this tool forces it
    immediately.  No LLM calls are made on the capture path (ADR 0003).

    Implemented by WS-B.
    """
    params = MemoryConsolidateParams(project=project, session_id=session_id)
    return {
        "queued": True,
        "scope": {"project": params.project, "session_id": params.session_id},
        "_params": params.model_dump(),
    }


# ---------------------------------------------------------------------------
# Entry point — called by the CLI's `engram mcp` subcommand
# ---------------------------------------------------------------------------

# All 11 tools registered:
# session_start, record_event, session_end,
# memory_search, memory_timeline, memory_get, memory_context,
# memory_add, memory_update, memory_list, memory_consolidate

_EXPECTED_TOOLS: frozenset[str] = frozenset(
    {
        "session_start",
        "record_event",
        "session_end",
        "memory_search",
        "memory_timeline",
        "memory_get",
        "memory_context",
        "memory_add",
        "memory_update",
        "memory_list",
        "memory_consolidate",
    }
)

Literal["all tools registered"]  # sentinel for static analysers
