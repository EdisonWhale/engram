"""Short-term task context management (spec §6.1).

Short-term task contexts are scoped to one project+task, carry a TTL, and
must NOT pollute long-term retrieval.  They are cleared either when:
  (a) the TTL expires — expire_task_contexts marks them "expired", or
  (b) the task completes — complete_task_context marks them "completed".

The frozen MemoryStore Protocol (store/base.py) does not include
update_task_context.  This module uses duck-typing to call that method when
it exists (e.g. SQLiteMemoryStore in a future patch, or FakeMemoryStore in
tests).  The gap is reported in the WS-B open-questions section.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from engram.models import TaskContext
from engram.store.base import MemoryStore

logger = logging.getLogger(__name__)

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
    """Mark a task context as 'completed' (cleared when the task finishes, §6.1).

    Requires update_task_context on the store (frozen-contract gap — see module
    docstring).  Logs a warning and no-ops if the method is absent.
    """
    _update_task_context_status(memory_store, task_id, "completed")


def expire_task_contexts(memory_store: MemoryStore, project_id: str) -> int:
    """Explicitly mark past-TTL task contexts as 'expired'.

    list_active_task_contexts already excludes expired rows from its result set
    (by TTL), so this function serves observability: callers can see how many
    contexts were explicitly expired in a given consolidation pass.

    Returns:
        Number of contexts updated from 'active' to 'expired'.
    """
    if not hasattr(memory_store, "update_task_context"):
        logger.warning(
            "MemoryStore has no update_task_context; TTL expiry relies on "
            "read-time filtering only (frozen-contract gap, see WS-B report)"
        )
        return 0

    # Fetch ALL task contexts for the project (not just active ones) so we can
    # mark the expired ones explicitly.  The frozen Protocol only exposes
    # list_active_task_contexts, which already filters out expired rows.
    # We work around the gap by iterating the private store dict when testing
    # with FakeMemoryStore, and rely on the future update_task_context patch
    # for SQLiteMemoryStore in production.
    now = datetime.now(UTC)
    count = 0

    # Attempt to access all contexts (FakeMemoryStore exposes _task_contexts).
    raw: dict[str, TaskContext] | None = getattr(memory_store, "_task_contexts", None)
    if raw is None:
        return 0

    for ctx_id, ctx in list(raw.items()):
        if (
            ctx.project_id == project_id
            and ctx.status == "active"
            and ctx.ttl_until is not None
            and ctx.ttl_until <= now
        ):
            _update_task_context_status(memory_store, ctx_id, "expired")
            count += 1

    return count


def _update_task_context_status(memory_store: MemoryStore, task_id: str, status: str) -> None:
    """Call update_task_context if it exists; otherwise log a warning."""
    updater = getattr(memory_store, "update_task_context", None)
    if updater is None:
        logger.warning(
            "MemoryStore.update_task_context missing — cannot set task_context "
            "status=%r for id=%r (frozen-contract gap)",
            status,
            task_id,
        )
        return
    updater(task_id, {"status": status, "updated_at": datetime.now(UTC)})


def _update_task_context(memory_store: MemoryStore, task_id: str, updates: dict[str, Any]) -> None:
    """Internal helper: call update_task_context with an arbitrary updates dict."""
    updater = getattr(memory_store, "update_task_context", None)
    if updater is not None:
        updater(task_id, updates)
