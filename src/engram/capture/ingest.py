"""Capture ingest API — session_start, record_event, session_end.

This module is the single integration point between the transcript tailer /
MCP tool layer and the EventStore.  It handles:

  - Project creation / resolution by path.
  - memory_thread_id resolution per spec §9.2 (dual session ID pattern).
  - Monotonic per-session seq assignment.
  - Content-hash computation (SHA-256 of canonical payload JSON).
  - Sequence-gap detection at session_end (source_seq continuity).
  - Pending-span detection: tool_use with no matching tool_result.

Architectural invariants (CLAUDE.md):
  - No LLM calls anywhere on this path.
  - Events are append-only (never UPDATE an event row).
  - SQLite is source of truth; write only through EventStore.

CONTRACT NOTE: record_event does NOT deduplicate by content_hash at the
database level — the events table has no unique(content_hash) constraint.
Dedup is enforced at the tailer level (byte-offset tracking prevents
reprocessing).  For the MCP manual path, callers are responsible for not
sending exact duplicates.  A get_event_by_hash() method on EventStore would
enable O(1) DB-level dedup; flagged as an open contract gap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from engram.models import AgentSession, Event, Project
from engram.store.base import EventStore, MemoryStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# event_types that represent "tool use starts" (need a matching result)
_CALL_TYPES: frozenset[str] = frozenset({"tool_call", "git", "file_read", "file_edit"})

# event_types that represent "tool use completed" (provide the matching result)
_RESULT_TYPES: frozenset[str] = frozenset({"tool_result", "subagent"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def session_start(
    event_store: EventStore,
    memory_store: MemoryStore,
    project_path: str,
    agent: str,
    prompt: str,
    git_sha: str,
    branch: str,
) -> dict[str, Any]:
    """Create or resume an agent session.

    Returns a dict with:
      session_id         – internal UUID for subsequent record_event calls
      memory_thread_id   – durable cross-session continuity thread
      thread_ambiguous   – True if multiple active task_contexts exist and no
                           clear branch/sha match was found (spec §9.2 case c)
      project_id         – internal project UUID
    """
    # 1. Resolve or create project
    project = event_store.get_project_by_path(project_path)
    if project is None:
        project = event_store.create_project(
            Project(
                root_path=project_path,
                name=Path(project_path).name or project_path,
            )
        )

    # 2. Resolve memory_thread_id per spec §9.2
    memory_thread_id, thread_ambiguous = _resolve_memory_thread(
        event_store=event_store,
        memory_store=memory_store,
        project_id=project.id,
        branch=branch,
        git_sha=git_sha,
    )

    # 3. Create the session row
    session = event_store.create_session(
        AgentSession(
            project_id=project.id,
            external_session_id=_new_uuid(),
            memory_thread_id=memory_thread_id,
            agent=agent,
            branch=branch or None,
            git_sha=git_sha or None,
            status="active",
        )
    )

    return {
        "session_id": session.id,
        "memory_thread_id": memory_thread_id,
        "thread_ambiguous": thread_ambiguous,
        "project_id": project.id,
    }


def record_event(
    event_store: EventStore,
    session_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    source_type: str = "mcp",
    raw_ref_file: str | None = None,
    raw_ref_offset: int | None = None,
    source_seq: int | None = None,
    capture_confidence: str = "exact",
    occurred_at: datetime | None = None,
) -> Event | None:
    """Append one event to the session's event log.

    Returns the stored Event, or None if the session does not exist.

    Callers on the transcript-tailer path should pass source_type="transcript",
    raw_ref_file, raw_ref_offset, source_seq, and capture_confidence="exact".
    Callers on the MCP manual path can use the defaults.

    INVARIANT: Events are append-only.  Never call update/delete on an event.
    """
    session = event_store.get_session(session_id)
    if session is None:
        return None

    seq = event_store.max_seq_for_session(session_id) + 1
    content_hash = Event.compute_hash(payload)
    ts = occurred_at if occurred_at is not None else datetime.now(UTC)

    event = Event(
        project_id=session.project_id,
        session_id=session_id,
        seq=seq,
        source_type=source_type,  # type: ignore[arg-type]
        source_seq=source_seq,
        raw_ref_file=raw_ref_file,
        raw_ref_offset=raw_ref_offset,
        capture_confidence=capture_confidence,  # type: ignore[arg-type]
        event_type=event_type,
        payload=payload,
        content_hash=content_hash,
        occurred_at=ts,
    )
    return event_store.create_event(event)


def session_end(
    event_store: EventStore,
    session_id: str,
    summary_hint: str | None = None,  # noqa: ARG001 — reserved for WS-B consolidation
) -> dict[str, Any]:
    """Close a session and run completeness reconciliation (spec §26).

    Completeness checks:
      1. source_seq gap detection — any missing line numbers in the
         [min, max] source_seq range indicate a dropped/unparseable record.
      2. Pending-span detection — tool_call/git/file_read/file_edit events
         with no matching tool_result/subagent event (interrupted spans).

    Returns a dict with:
      session_id       – echoed back
      status           – "completed" | "failed"
      events_captured  – total number of events stored for this session
      capture_complete – True only if no gaps AND no pending spans
      gaps             – list of missing source_seq values (int)
      pending_spans    – list of tool_use_ids with no matching result
    """
    events = event_store.list_session_events(session_id)

    # --- Gap detection on source_seq ---
    source_seqs = sorted({e.source_seq for e in events if e.source_seq is not None})
    gaps: list[int] = []
    if source_seqs:
        for i in range(len(source_seqs) - 1):
            expected_next = source_seqs[i] + 1
            actual_next = source_seqs[i + 1]
            gaps.extend(range(expected_next, actual_next))

    # --- Pending-span detection ---
    call_ids: set[str] = set()
    result_ids: set[str] = set()
    for evt in events:
        uid = evt.payload.get("tool_use_id")
        if not uid:
            continue
        if evt.event_type in _CALL_TYPES:
            call_ids.add(uid)
        elif evt.event_type in _RESULT_TYPES:
            result_ids.add(uid)
    pending_spans = sorted(call_ids - result_ids)

    capture_complete = len(gaps) == 0 and len(pending_spans) == 0

    # --- Update session status ---
    event_store.update_session_status(
        session_id,
        status="completed" if capture_complete else "failed",
        ended_at=datetime.now(UTC),
    )

    return {
        "session_id": session_id,
        "status": "completed" if capture_complete else "failed",
        "events_captured": len(events),
        "capture_complete": capture_complete,
        "gaps": gaps,
        "pending_spans": pending_spans,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _resolve_memory_thread(
    event_store: EventStore,
    memory_store: MemoryStore,
    project_id: str,
    branch: str | None,
    git_sha: str | None,
) -> tuple[str, bool]:
    """Resolve or mint a memory_thread_id per spec §9.2.

    Returns (memory_thread_id, thread_ambiguous).
    thread_ambiguous=True signals that multiple candidate task_contexts existed
    and no definitive match was found; the caller can surface this for review.
    """
    active_contexts = memory_store.list_active_task_contexts(project_id)

    if not active_contexts:
        # No active contexts → mint a fresh thread.
        return _new_uuid(), False

    if len(active_contexts) == 1:
        # Exactly one active context → adopt its session's thread.
        session = event_store.get_session(active_contexts[0].session_id)
        if session is not None:
            return session.memory_thread_id, False
        # Session row missing (should not happen) → mint new.
        return _new_uuid(), False

    # Multiple active contexts → disambiguate.
    # (a) Prefer context whose session shares branch or git_sha.
    if branch or git_sha:
        for ctx in active_contexts:
            session = event_store.get_session(ctx.session_id)
            if session is None:
                continue
            if (branch and session.branch == branch) or (git_sha and session.git_sha == git_sha):
                return session.memory_thread_id, False

    # (b) No clear match.  Semantic similarity is not available on the capture
    # path (no LLM — ADR 0003), so we conservatively mint a new thread and
    # flag ambiguity for human review (spec §9.2 case c).
    return _new_uuid(), True


def _new_uuid() -> str:
    return str(uuid4())
