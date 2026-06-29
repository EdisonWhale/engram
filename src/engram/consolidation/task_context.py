"""Short-term task context management (spec §6.1).

Short-term task contexts are scoped to one project+task, carry a TTL, and
must NOT pollute long-term retrieval.  They are cleared either when:
  (a) the TTL expires — expire_task_contexts marks them "expired", or
  (b) the task completes — complete_task_context marks them "completed".

Clearing relies on MemoryStore.update_task_context / list_task_contexts
(added to the storage contract during integration).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from engram.models import TaskContext
from engram.store.base import MemoryStore

DEFAULT_TTL_HOURS: float = 24.0


def create_task_context(
    project_id: str,
    session_id: str,
    task_key: str,
    title: str,
    content: str,
    memory_store: MemoryStore,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    changed_files: list[str] | None = None,
    next_steps: list[str] | None = None,
    source_event_ids: list[str] | None = None,
) -> TaskContext:
    """Create and persist a short-term task context with a TTL.

    Args:
        ttl_hours: Time-to-live in hours from now.  Pass a negative value to
                   create an already-expired context (useful in tests).
    """
    ctx = TaskContext(
        project_id=project_id,
        session_id=session_id,
        task_key=task_key,
        title=title,
        content=content,
        changed_files=changed_files or [],
        next_steps=next_steps or [],
        ttl_until=datetime.now(UTC) + timedelta(hours=ttl_hours),
        source_event_ids=source_event_ids or [],
    )
    return memory_store.create_task_context(ctx)


def complete_task_context(memory_store: MemoryStore, task_id: str) -> None:
    """Mark a task context as 'completed' (cleared when the task finishes, §6.1)."""
    memory_store.update_task_context(
        task_id, {"status": "completed", "updated_at": datetime.now(UTC)}
    )


def expire_task_contexts(memory_store: MemoryStore, project_id: str) -> int:
    """Mark past-TTL active task contexts as 'expired'.

    list_active_task_contexts already excludes expired rows by TTL at read
    time, so injection correctness does not depend on this sweep; it keeps the
    persisted status column accurate (observability + cleanup).

    Returns:
        Number of contexts updated from 'active' to 'expired'.
    """
    now = datetime.now(UTC)
    count = 0
    for ctx in memory_store.list_task_contexts(project_id, status="active"):
        if ctx.ttl_until is not None and ctx.ttl_until <= now:
            memory_store.update_task_context(ctx.id, {"status": "expired", "updated_at": now})
            count += 1
    return count
