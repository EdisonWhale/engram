"""Engram MCP server — tools wired to the capture / consolidation / retrieval modules.

Transport: stdio (ADR 0006).  No HTTP / no FastAPI / no Flask.

Each tool validates its input via a pydantic ``*Params`` model, then delegates to
the workstream implementation through a lazily-built, process-wide ``_ServerState``
(one SQLite connection + the three stores + the consolidation worker).

Workstream ownership of the bodies:
- WS-A (capture):       session_start, record_event, session_end
- WS-B (consolidation): memory_consolidate, session_end (flush side)
- WS-C (retrieval):     memory_search, memory_timeline, memory_get, memory_context, memory_list
- memory_add / memory_update: thin manual-admin store operations

The database path comes from ``$ENGRAM_DB`` (set by ``engram --db PATH mcp``),
defaulting to ``~/.engram/engram.db``.  State is built on first tool call, not at
import or MCP ``initialize``, so the handshake never touches the filesystem.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from engram.capture import record_event as capture_record_event
from engram.capture import session_end as capture_session_end
from engram.capture import session_start as capture_session_start
from engram.consolidation import AnthropicLLMClient, ConsolidationWorker, MockLLMClient
from engram.db.runner import open_db
from engram.models import (
    MemoryScope,
    MemoryStatus,
    MemoryType,
    UpdateOperation,
)
from engram.retrieval import memory_context as retrieve_context
from engram.retrieval import memory_get as retrieve_get
from engram.retrieval import memory_search as retrieve_search
from engram.retrieval import memory_timeline as retrieve_timeline
from engram.store.sqlite_store import SQLiteEventStore, SQLiteMemoryStore

logger = logging.getLogger(__name__)

mcp = FastMCP("engram")


# ---------------------------------------------------------------------------
# Process-wide state (lazy): one connection, the stores, the worker.
# ---------------------------------------------------------------------------


@dataclass
class _ServerState:
    event_store: SQLiteEventStore
    memory_store: SQLiteMemoryStore
    worker: ConsolidationWorker


_state: _ServerState | None = None


def _resolve_db_path() -> str:
    return os.environ.get("ENGRAM_DB") or str(Path.home() / ".engram" / "engram.db")


def _build_state() -> _ServerState:
    conn = open_db(_resolve_db_path())
    event_store = SQLiteEventStore(conn)
    memory_store = SQLiteMemoryStore(conn)
    # Real LLM only when a key is configured; otherwise the server stays usable
    # (capture + retrieval) and consolidation produces no summaries.
    if os.environ.get("ANTHROPIC_API_KEY"):
        llm = AnthropicLLMClient()
    else:
        logger.warning(
            "ANTHROPIC_API_KEY not set; using MockLLMClient. Consolidation will "
            "create NO session summaries (capture + retrieval still work)."
        )
        llm = MockLLMClient()
    worker = ConsolidationWorker(event_store=event_store, memory_store=memory_store, llm=llm)
    return _ServerState(event_store=event_store, memory_store=memory_store, worker=worker)


def _get_state() -> _ServerState:
    global _state
    if _state is None:
        _state = _build_state()
    return _state


# ---------------------------------------------------------------------------
# Pydantic input models — explicit validation, directly unit-testable.
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
    project: str | None = None
    token_budget: int = Field(default=1200, ge=100, le=8000)


class MemoryAddParams(BaseModel):
    content: str
    type: MemoryType
    scope: MemoryScope
    project: str | None = None
    title: str | None = None
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
# Session tools  (WS-A)
# ---------------------------------------------------------------------------


@mcp.tool()
def session_start(
    project_path: str,
    agent: str,
    prompt: str,
    git_sha: str,
    branch: str,
) -> dict[str, Any]:
    """Create or resume an agent session.

    Resolves or mints a memory_thread_id from the active task_context rules
    (spec §9.2) and returns the session id and thread.
    """
    params = SessionStartParams(
        project_path=project_path, agent=agent, prompt=prompt, git_sha=git_sha, branch=branch
    )
    st = _get_state()
    return capture_session_start(
        st.event_store,
        st.memory_store,
        params.project_path,
        params.agent,
        params.prompt,
        params.git_sha,
        params.branch,
    )


@mcp.tool()
def record_event(
    session_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a normalised raw event to the session's event log (append-only, ADR 0004).

    The event is also enqueued for lazy consolidation (flushed at session_end or
    via memory_consolidate).
    """
    params = RecordEventParams(session_id=session_id, event_type=event_type, payload=payload or {})
    st = _get_state()
    event = capture_record_event(
        st.event_store, params.session_id, params.event_type, params.payload, source_type="mcp"
    )
    if event is None:
        return {"recorded": False, "reason": "session_not_found", "session_id": params.session_id}
    st.worker.enqueue_event(event.session_id, event.project_id, event.id)
    return {
        "recorded": True,
        "event_id": event.id,
        "seq": event.seq,
        "content_hash": event.content_hash,
    }


@mcp.tool()
async def session_end(
    session_id: str,
    summary_hint: str | None = None,
) -> dict[str, Any]:
    """Close a session, reconcile capture completeness (ADR 0004), then flush consolidation.

    The capture close (WS-A) always runs.  Consolidation (WS-B) only runs for a
    completely-captured session; its outcome is reported under ``consolidation``
    and a consolidation failure does not fail the session close.
    """
    params = SessionEndParams(session_id=session_id, summary_hint=summary_hint)
    st = _get_state()
    result = capture_session_end(st.event_store, params.session_id, params.summary_hint)

    if result.get("capture_complete"):
        try:
            result["consolidation"] = await st.worker.run_once(session_id=params.session_id)
        except Exception as exc:  # noqa: BLE001 — surfaced to caller, not swallowed
            result["consolidation_error"] = f"{type(exc).__name__}: {exc}"
    else:
        result["consolidation"] = {"skipped": "capture_incomplete"}
    return result


# ---------------------------------------------------------------------------
# Retrieval tools  (WS-C)
# ---------------------------------------------------------------------------


@mcp.tool()
def memory_search(
    query: str,
    project: str | None = None,
    file: str | None = None,
    type: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Stage 1 progressive disclosure (spec §11.2): compact candidate rows (id/title/type/age)."""
    params = MemorySearchParams(query=query, project=project, file=file, type=type, limit=limit)
    st = _get_state()
    return retrieve_search(
        params.query,
        memory_store=st.memory_store,
        project_id=params.project,
        file_path=params.file,
        type=params.type,
        limit=params.limit,
    )


@mcp.tool()
def memory_timeline(
    anchor_id: str | None = None,
    query: str | None = None,
    before: int = 3,
    after: int = 3,
) -> dict[str, Any]:
    """Stage 2 progressive disclosure: chronological window around an anchor/query match."""
    params = MemoryTimelineParams(anchor_id=anchor_id, query=query, before=before, after=after)
    st = _get_state()
    return retrieve_timeline(
        memory_store=st.memory_store,
        anchor_id=params.anchor_id,
        query=params.query,
        before=params.before,
        after=params.after,
    )


@mcp.tool()
def memory_get(ids: list[str]) -> dict[str, Any]:
    """Stage 3 progressive disclosure: full memory records for specific IDs."""
    params = MemoryGetParams(ids=ids)
    st = _get_state()
    return retrieve_get(params.ids, memory_store=st.memory_store)


@mcp.tool()
def memory_context(
    query: str,
    project: str | None = None,
    token_budget: int = 1200,
) -> dict[str, Any]:
    """Final prompt-ready context under a token budget (§11.3).

    Never injects stale, conflicting, or deleted memories.
    """
    params = MemoryContextParams(query=query, project=project, token_budget=token_budget)
    st = _get_state()
    return retrieve_context(
        params.query,
        memory_store=st.memory_store,
        project_id=params.project,
        token_budget=params.token_budget,
    )


# ---------------------------------------------------------------------------
# Memory management tools
# ---------------------------------------------------------------------------


@mcp.tool()
def memory_add(
    content: str,
    type: str,
    scope: str,
    project: str | None = None,
    title: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Manually insert a confirmed fact/preference/decision.

    Input is validated, but the body is intentionally not implemented: no
    workstream specs manual memory creation, and the data model requires a
    resolved project_id plus a defined user-scope handling that needs a spec
    decision before encoding (see docs/tasks). Validation still runs so callers
    get correct type/scope errors today.
    """
    params = MemoryAddParams(
        content=content, type=type, scope=scope, project=project, title=title, metadata=metadata
    )
    return {
        "implemented": False,
        "reason": "manual memory_add not yet specced (project_id resolution + user-scope)",
        "validated": params.model_dump(mode="json"),
    }


@mcp.tool()
def memory_update(
    memory_id: str,
    operation: str,
    content: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Update a memory's lifecycle status (supersede/stale/tombstone, never hard-delete).

    Input is validated, but the body is intentionally not implemented: the manual
    update semantics (esp. supersede/resolve_conflict, which mutate two rows and
    set forward pointers) overlap the WS-B consolidation state machine and need a
    spec decision on the manual-vs-automatic boundary before encoding.
    """
    params = MemoryUpdateParams(
        memory_id=memory_id, operation=operation, content=content, reason=reason
    )
    return {
        "implemented": False,
        "reason": "manual memory_update not yet specced (overlaps WS-B supersede/conflict path)",
        "validated": params.model_dump(mode="json"),
    }


@mcp.tool()
def memory_list(
    project: str | None = None,
    type: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """List memories for inspection/admin. status: active|stale|superseded|conflict|deleted."""
    params = MemoryListParams(project=project, type=type, status=status)
    st = _get_state()
    memories = st.memory_store.list_memories(
        project_id=params.project, type=params.type, status=params.status
    )
    return {"memories": [m.model_dump(mode="json") for m in memories], "total": len(memories)}


@mcp.tool()
async def memory_consolidate(
    project: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Manually run the consolidation worker for a project or session (no LLM on write path)."""
    params = MemoryConsolidateParams(project=project, session_id=session_id)
    st = _get_state()
    result = await st.worker.run_once(project_id=params.project, session_id=params.session_id)
    return {"scope": {"project": params.project, "session_id": params.session_id}, **result}


# ---------------------------------------------------------------------------
# Entry point — called by the CLI's `engram mcp` subcommand
# ---------------------------------------------------------------------------

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
