"""Retrieval module: progressive disclosure + BM25 + stale check + context assembly.

Progressive-disclosure workflow (spec §8.2, §11.2):

    memory_search(query)        → compact rows — IDs, titles, type, age, status
    memory_timeline(anchor_id)  → chronological window around an anchor
    memory_get(ids)             → full Memory records for chosen IDs
    memory_context(query)       → prompt-ready context under token_budget

Candidate generation (§11.1):
    P0: FTS5/BM25 via MemoryStore.search_memories_fts
    P1: hybrid vector+RRF — inject VectorStore; defaults to None/disabled

Stale check (§10.6):
    Memories with a file_path have their file_hash recomputed at recall time.
    Hash mismatch → mark "stale" via MemoryStore.update_memory, demote in results.

Context assembly priority (§11.3):
    1. Active short-term task contexts
    2. Relevant decisions
    3. Relevant project facts / constraints
    4. Relevant preferences
    5. Relevant commands
    6. Relevant failure patterns

Never injects memories with status "conflict", "superseded", or "deleted".
Never exceeds token_budget.

Trace shape for WS-D (RetrievalTraceData):
    query              — original query string
    project_id         — project filter, or None
    candidate_ids      — all IDs returned by FTS5 before filtering
    selected_ids       — IDs that made it into the final context
    scores             — {memory_id: float} inverse-rank score (1/(rank+1))
    filters_applied    — dict of filter params used
    ranking_features   — metadata about the ranking pass
    stale_ids          — IDs found stale at recall time
    conflict_ids_excluded — IDs excluded because status="conflict"
    token_budget       — requested budget
    injected_tokens    — actual tokens injected
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from engram.models import Memory
from engram.store.base import MemoryStore, VectorStore

__all__ = [
    "memory_search",
    "memory_timeline",
    "memory_get",
    "memory_context",
    "RetrievalTraceData",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Context assembly priority (§11.3) — lower number = higher priority.
# Types not in this dict are treated as lowest priority (99).
_TYPE_PRIORITY: dict[str, int] = {
    "decision": 0,
    "project_fact": 1,
    "constraint": 2,
    "preference": 3,
    "command": 4,
    "failure_pattern": 5,
}

# Max chars for the provenance_summary field in compact search rows.
# A 100-char preview + punctuation ~ 25-30 tokens, keeping each row ≤ 100 tok.
_PREVIEW_CHARS = 100

# How many FTS5 candidates to pull into memory_context before priority-ranking.
_CONTEXT_CANDIDATE_LIMIT = 50


# ---------------------------------------------------------------------------
# Trace shape
# ---------------------------------------------------------------------------


@dataclass
class RetrievalTraceData:
    """Structured trace for one retrieval call; WS-D persists this to retrieval_traces.

    Field layout mirrors the RetrievalTrace pydantic model so WS-D can build
    the DB row without any transformation::

        trace = memory_context(...)["trace"]
        rt = RetrievalTrace(
            query=trace["query"],
            project_id=trace["project_id"] or "",
            selected_memory_ids=trace["selected_ids"],
            candidate_memory_ids=trace["candidate_ids"],
            ranking_features=trace["ranking_features"],
            token_budget=trace["token_budget"],
            injected_tokens=trace["injected_tokens"],
        )
        memory_store.create_retrieval_trace(rt)
    """

    query: str
    project_id: str | None
    candidate_ids: list[str] = field(default_factory=list)
    selected_ids: list[str] = field(default_factory=list)
    # memory_id -> inverse-rank score 1/(rank+1), starting from 1
    scores: dict[str, float] = field(default_factory=dict)
    filters_applied: dict[str, Any] = field(default_factory=dict)
    ranking_features: dict[str, Any] = field(default_factory=dict)
    stale_ids: list[str] = field(default_factory=list)
    conflict_ids_excluded: list[str] = field(default_factory=list)
    token_budget: int = 0
    injected_tokens: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Conservative token estimate: (len + 3) // 4.

    Consistent with the ~4 chars/token heuristic used across Anthropic/OpenAI
    tooling for English prose + code.  Over-estimates slightly so we stay
    safely under token_budget.
    """
    return (len(text) + 3) // 4


def _age_str(dt: datetime) -> str:
    """Human-readable age: '5s ago', '3m ago', '2h ago', '7d ago'."""
    now = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _hash_file(path: str) -> str:
    """SHA-256 hash of the file at *path*; raises OSError if unreadable."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_and_mark_stale(memory: Memory, store: MemoryStore) -> Memory:
    """Stale check at recall time (§10.6 / ADR 0005).

    If the memory has a file_path + file_hash and the file has changed since
    the memory was written, mark it stale in the store and return a copy with
    updated status.

    Silently skips the check when:
    - file_path or file_hash is None
    - the memory is already degraded (stale/superseded/deleted)
    - the file cannot be read (file moved/deleted — don't mark stale on I/O error)
    """
    if memory.file_path is None or memory.file_hash is None:
        return memory
    if memory.status in ("stale", "superseded", "deleted"):
        return memory

    try:
        current_hash = _hash_file(memory.file_path)
    except OSError:
        return memory

    if current_hash != memory.file_hash:
        store.update_memory(memory.id, {"status": "stale"})
        return memory.model_copy(update={"status": "stale"})
    return memory


def _compact_row(memory: Memory) -> dict[str, Any]:
    """Produce a compact row suitable for memory_search (~50–100 tokens).

    Fields: id, title, type, age, status, origin, provenance_summary.
    provenance_summary is the first _PREVIEW_CHARS characters of content.
    """
    preview = memory.content[:_PREVIEW_CHARS]
    if len(memory.content) > _PREVIEW_CHARS:
        preview += "..."
    return {
        "id": memory.id,
        "title": memory.title,
        "type": memory.type,
        "age": _age_str(memory.created_at),
        "status": memory.status,
        "origin": memory.origin,
        "provenance_summary": preview,
    }


def _rank_key(memory: Memory) -> tuple[int, float, int]:
    """Sort key for context assembly priority (lower = higher priority).

    Tie-breaks: higher confidence → lower key; higher access_count → lower key.
    """
    priority = _TYPE_PRIORITY.get(memory.type, 99)
    return (priority, -memory.confidence, -memory.access_count)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def memory_search(
    query: str,
    *,
    memory_store: MemoryStore,
    project_id: str | None = None,
    file_path: str | None = None,
    type: str | None = None,
    status: str | None = "active",
    limit: int = 10,
    vector_store: VectorStore | None = None,  # P1 seam — unused at P0
) -> dict[str, Any]:
    """Return compact candidate rows ranked by BM25 (stage 1 of progressive disclosure).

    Each row is ~50–100 tokens (id + title + type + age + status + 100-char preview).
    Runs the stale check (§10.6) and demotes any memory whose backing file changed.
    Memories with status "deleted" are filtered from the output.

    Args:
        query:        Free-text query; FTS5 special chars are sanitized automatically.
        memory_store: Store to search.
        project_id:   Scope results to one project.
        file_path:    Filter to memories associated with a specific file.
        type:         Filter to one memory type (decision, preference, …).
        status:       Filter by status; defaults to "active" (pass None for all).
        limit:        Max results to return.
        vector_store: P1 seam; pass a VectorStore to enable hybrid BM25+vector+RRF.

    Returns::

        {
            "memories": [
                {
                    "id": str,
                    "title": str,
                    "type": str,
                    "age": str,          # e.g. "2d ago"
                    "status": str,
                    "origin": str,
                    "provenance_summary": str,  # first 100 chars of content
                }
            ],
            "total": int,
            "query": str,
            "filters": {"project_id": ..., "file_path": ..., "type": ..., "status": ...},
        }
    """
    candidates = memory_store.search_memories_fts(
        query,
        project_id=project_id,
        type=type,
        status=status,
        file_path=file_path,
        limit=limit,
    )

    # Stale check — may update store and flip status
    checked = [_check_and_mark_stale(m, memory_store) for m in candidates]

    # Suppress deleted memories from output (stale ones are shown, marked)
    visible = [m for m in checked if m.status != "deleted"]

    return {
        "memories": [_compact_row(m) for m in visible],
        "total": len(visible),
        "query": query,
        "filters": {
            "project_id": project_id,
            "file_path": file_path,
            "type": type,
            "status": status,
        },
    }


def memory_timeline(
    *,
    memory_store: MemoryStore,
    anchor_id: str | None = None,
    query: str | None = None,
    project_id: str | None = None,
    before: int = 3,
    after: int = 3,
) -> dict[str, Any]:
    """Return chronological context around a memory anchor (stage 2).

    Supply either *anchor_id* (a specific memory UUID) or *query* (finds the
    top BM25 match as the anchor).  Retrieves *before* memories created before
    the anchor and *after* memories created after it, in the same project.

    Returns::

        {
            "anchor": compact row or None,
            "before": [compact row, ...],   # oldest first
            "after":  [compact row, ...],   # newest first relative to anchor
        }
    """
    anchor: Memory | None = None

    if anchor_id:
        anchor = memory_store.get_memory(anchor_id)
    elif query:
        hits = memory_store.search_memories_fts(
            query,
            project_id=project_id,
            status=None,  # don't restrict status for timeline anchoring
            limit=1,
        )
        anchor = hits[0] if hits else None

    if anchor is None:
        return {"anchor": None, "before": [], "after": []}

    anchor = _check_and_mark_stale(anchor, memory_store)

    # Fetch all memories for the project (no status filter — full timeline view)
    all_memories = memory_store.list_memories(
        project_id=anchor.project_id,
        status=None,
    )
    # Chronological order
    all_memories.sort(key=lambda m: m.created_at)

    anchor_idx = next((i for i, m in enumerate(all_memories) if m.id == anchor.id), None)
    if anchor_idx is None:
        # Anchor might be filtered (different status) — return it alone
        return {"anchor": _compact_row(anchor), "before": [], "after": []}

    before_slice = all_memories[max(0, anchor_idx - before) : anchor_idx]
    after_slice = all_memories[anchor_idx + 1 : anchor_idx + 1 + after]

    return {
        "anchor": _compact_row(anchor),
        "before": [_compact_row(m) for m in before_slice],
        "after": [_compact_row(m) for m in after_slice],
    }


def memory_get(
    ids: list[str],
    *,
    memory_store: MemoryStore,
) -> dict[str, Any]:
    """Fetch full Memory records for specific IDs (stage 3).

    Only call after filtering candidates with memory_search or memory_timeline.
    Runs the stale check on each returned memory.

    Returns::

        {
            "memories": [Memory.model_dump(), ...],
            "not_found": [id, ...],
        }
    """
    memories: list[dict[str, Any]] = []
    not_found: list[str] = []

    for mid in ids:
        m = memory_store.get_memory(mid)
        if m is None:
            not_found.append(mid)
        else:
            m = _check_and_mark_stale(m, memory_store)
            memories.append(m.model_dump(mode="json"))

    return {"memories": memories, "not_found": not_found}


def memory_context(
    query: str,
    *,
    memory_store: MemoryStore,
    project_id: str | None = None,
    token_budget: int = 1200,
    vector_store: VectorStore | None = None,  # P1 seam — unused at P0
) -> dict[str, Any]:
    """Return final prompt-ready context assembled under *token_budget* (stage 4).

    Assembly priority (§11.3):
      1. Active short-term task contexts (TaskContext)
      2. Decisions → project facts → constraints → preferences → commands → failure patterns

    Safety invariants:
      - Never injects "conflict", "superseded", or "deleted" memories.
      - Never exceeds token_budget (uses conservative 4-chars/token estimate).

    Args:
        query:        Query used to find relevant memories via BM25.
        memory_store: Store to query.
        project_id:   Scope to one project.
        token_budget: Hard upper bound on injected tokens.
        vector_store: P1 seam for hybrid retrieval; currently unused.

    Returns::

        {
            "context":         str,          # ready to inject into a prompt
            "injected_tokens": int,
            "memory_ids":      [str, ...],   # IDs of memories included
            "trace": {
                "query":                str,
                "project_id":           str | None,
                "candidate_ids":        [str, ...],
                "selected_ids":         [str, ...],
                "scores":               {id: float, ...},
                "filters_applied":      {...},
                "ranking_features":     {...},
                "stale_ids":            [str, ...],
                "conflict_ids_excluded":[str, ...],
                "token_budget":         int,
                "injected_tokens":      int,
            },
        }
    """
    trace = RetrievalTraceData(
        query=query,
        project_id=project_id,
        token_budget=token_budget,
        filters_applied={"project_id": project_id, "status": "python_filtered"},
    )

    # ------------------------------------------------------------------ #
    # Phase 1: Short-term task contexts (highest priority, §11.3 step 1)  #
    # ------------------------------------------------------------------ #
    task_ctx_parts: list[str] = []
    if project_id:
        for ctx in memory_store.list_active_task_contexts(project_id):
            task_ctx_parts.append(f"[Task: {ctx.title}]\n{ctx.content}")

    # ------------------------------------------------------------------ #
    # Phase 2: FTS5 candidate generation                                  #
    # Search without a status filter so conflict/stale/superseded         #
    # memories are visible in the trace (tracked in conflict_ids_excluded  #
    # and stale_ids).  Status-based exclusion happens in Phase 4 below.   #
    # ------------------------------------------------------------------ #
    raw_candidates = memory_store.search_memories_fts(
        query,
        project_id=project_id,
        status=None,  # all statuses — filtered in Python for full trace coverage
        limit=_CONTEXT_CANDIDATE_LIMIT,
    )
    trace.candidate_ids = [m.id for m in raw_candidates]

    # ------------------------------------------------------------------ #
    # Phase 3: Stale check — may flip status to "stale"                   #
    # ------------------------------------------------------------------ #
    stale_ids: list[str] = []
    checked: list[Memory] = []
    for m in raw_candidates:
        updated = _check_and_mark_stale(m, memory_store)
        if updated.status == "stale" and m.status != "stale":
            stale_ids.append(m.id)
        checked.append(updated)
    trace.stale_ids = stale_ids

    # ------------------------------------------------------------------ #
    # Phase 4: Safety filter                                              #
    # Exclude: conflict (never inject unresolved), superseded, deleted,   #
    #          freshly-staled memories (demoted, not injected by default). #
    # ------------------------------------------------------------------ #
    injectable_statuses = {"active"}
    conflict_excluded: list[str] = []
    safe: list[Memory] = []
    for m in checked:
        if m.status == "conflict":
            conflict_excluded.append(m.id)
        elif m.status in injectable_statuses:
            safe.append(m)
    trace.conflict_ids_excluded = conflict_excluded

    # ------------------------------------------------------------------ #
    # Phase 5: Priority ranking (§11.3)                                   #
    # ------------------------------------------------------------------ #
    safe.sort(key=_rank_key)

    trace.ranking_features = {
        "method": "bm25+type_priority",
        "type_priority": _TYPE_PRIORITY,
        "candidates_total": len(raw_candidates),
        "after_safety_filter": len(safe),
        "stale_demoted": len(stale_ids),
        "conflict_excluded": len(conflict_excluded),
    }

    # ------------------------------------------------------------------ #
    # Phase 6: Greedy token packing                                       #
    # ------------------------------------------------------------------ #
    parts: list[str] = []
    used_tokens: int = 0

    # Task contexts first
    for ctx_text in task_ctx_parts:
        t = _estimate_tokens(ctx_text)
        if used_tokens + t <= token_budget:
            parts.append(ctx_text)
            used_tokens += t

    # Ranked memories
    selected_ids: list[str] = []
    scores: dict[str, float] = {}
    for rank, m in enumerate(safe):
        mem_text = f"[{m.type}] {m.title}\n{m.content}"
        t = _estimate_tokens(mem_text)
        if used_tokens + t <= token_budget:
            parts.append(mem_text)
            selected_ids.append(m.id)
            used_tokens += t
            scores[m.id] = 1.0 / (rank + 1)

    trace.selected_ids = selected_ids
    trace.scores = scores
    trace.injected_tokens = used_tokens

    context_str = "\n\n".join(parts)

    return {
        "context": context_str,
        "injected_tokens": used_tokens,
        "memory_ids": selected_ids,
        "trace": {
            "query": trace.query,
            "project_id": trace.project_id,
            "candidate_ids": trace.candidate_ids,
            "selected_ids": trace.selected_ids,
            "scores": trace.scores,
            "filters_applied": trace.filters_applied,
            "ranking_features": trace.ranking_features,
            "stale_ids": trace.stale_ids,
            "conflict_ids_excluded": trace.conflict_ids_excluded,
            "token_budget": trace.token_budget,
            "injected_tokens": trace.injected_tokens,
        },
    }
