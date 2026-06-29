"""Two-phase consolidation pipeline (spec §27 / nram pattern).

Phase 1 — write-time (fast path, no LLM):
  enqueue_event() registers an event for deferred consolidation.
  run_write_time_dedup() immediately checks content-hash + vector band and
  either bumps access_count (duplicate) or queues for idle consolidation.

Phase 2 — idle-time batch (slow path, LLM allowed):
  Triggered by:
    (a) idle timeout: no new enqueue calls for IDLE_SECONDS seconds.
    (b) write-count threshold: WRITE_THRESHOLD events queued since last run —
        ensures consolidation fires even for an always-busy agent (§27.1 note).
  Runs LLM-based session summary + long-term promotion + task context expiry.

Public class: ConsolidationWorker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from engram.consolidation.dedup import (
    DedupResult,
    NullVectorStore,
    ScoredVectorStore,
    run_write_time_dedup,
)
from engram.consolidation.llm import LLMClient, MockLLMClient
from engram.consolidation.promote import (
    build_memory_from_event,
    classify_event_for_promotion,
)
from engram.consolidation.summarize import build_session_summary
from engram.consolidation.task_context import expire_task_contexts
from engram.models import Memory
from engram.store.base import EventStore, MemoryStore

logger = logging.getLogger(__name__)

# P0 defaults
_IDLE_SECONDS: float = 30.0
_WRITE_THRESHOLD: int = 20


@dataclass
class _QueueItem:
    """One session's pending consolidation work."""

    session_id: str
    project_id: str
    event_ids: list[str] = field(default_factory=list)
    queued_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ConsolidationWorker:
    """Two-phase consolidation pipeline.

    Usage::

        worker = ConsolidationWorker(event_store, memory_store)

        # Write path (fast; call from record_event)
        worker.enqueue_event(session_id, project_id, event_id)

        # Manual trigger (tests, or memory_consolidate MCP tool)
        result = await worker.run_once(project_id=pid, session_id=sid)

        # Background loop (started once from MCP server startup)
        await worker.start()
        ...
        await worker.stop()
    """

    def __init__(
        self,
        event_store: EventStore,
        memory_store: MemoryStore,
        vector_store: ScoredVectorStore | None = None,
        llm: LLMClient | None = None,
        idle_seconds: float = _IDLE_SECONDS,
        write_threshold: int = _WRITE_THRESHOLD,
    ) -> None:
        self._event_store = event_store
        self._memory_store = memory_store
        self._vector_store: ScoredVectorStore = vector_store or NullVectorStore()
        self._llm: LLMClient = llm or MockLLMClient()
        self._idle_seconds = idle_seconds
        self._write_threshold = write_threshold

        self._queue: list[_QueueItem] = []
        self._last_write_at: datetime = datetime.now(UTC)
        self._pending_write_count: int = 0
        self._running = False
        self._bg_task: asyncio.Task | None = None  # type: ignore[type-arg]

    # ------------------------------------------------------------------ #
    # Write-path API                                                       #
    # ------------------------------------------------------------------ #

    def enqueue_event(self, session_id: str, project_id: str, event_id: str) -> None:
        """Fast write-path: register *event_id* for deferred consolidation.

        No I/O, no LLM — just appends to an in-memory queue.
        """
        for item in self._queue:
            if item.session_id == session_id:
                item.event_ids.append(event_id)
                break
        else:
            self._queue.append(
                _QueueItem(
                    session_id=session_id,
                    project_id=project_id,
                    event_ids=[event_id],
                )
            )

        self._last_write_at = datetime.now(UTC)
        self._pending_write_count += 1

    # ------------------------------------------------------------------ #
    # Trigger helpers                                                      #
    # ------------------------------------------------------------------ #

    def should_run_now(self) -> bool:
        """True if write-count threshold is met (busy-agent gate)."""
        return bool(self._queue) and self._pending_write_count >= self._write_threshold

    # ------------------------------------------------------------------ #
    # Consolidation API                                                    #
    # ------------------------------------------------------------------ #

    async def run_once(
        self,
        project_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Process all queued sessions, optionally filtered by project/session.

        This is the function the MCP layer calls for the ``memory_consolidate``
        tool and the ``session_end`` summary side.

        Returns a summary dict::

            {
                "sessions_processed": int,
                "memories_created": int,
                "summaries_created": int,
            }
        """
        items = [
            item
            for item in self._queue
            if (project_id is None or item.project_id == project_id)
            and (session_id is None or item.session_id == session_id)
        ]

        if not items:
            return {"sessions_processed": 0, "memories_created": 0, "summaries_created": 0}

        memories_created = 0
        summaries_created = 0
        processed_items: list[_QueueItem] = []

        for item in items:
            try:
                result = await self._consolidate_session(item)
                memories_created += result["memories_created"]
                summaries_created += result["summaries_created"]
                processed_items.append(item)
            except Exception:
                logger.exception("Consolidation failed for session %s", item.session_id)

        # Remove successfully processed items
        for item in processed_items:
            with contextlib.suppress(ValueError):
                self._queue.remove(item)

        # Expire stale task contexts for processed projects
        processed_project_ids = {item.project_id for item in processed_items}
        for pid in processed_project_ids:
            expire_task_contexts(self._memory_store, pid)

        self._pending_write_count = 0

        return {
            "sessions_processed": len(processed_items),
            "memories_created": memories_created,
            "summaries_created": summaries_created,
        }

    # ------------------------------------------------------------------ #
    # Background loop                                                      #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Start the background idle-trigger loop."""
        self._running = True
        self._bg_task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Graceful shutdown of the background loop."""
        self._running = False
        if self._bg_task is not None:
            self._bg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bg_task

    async def _loop(self) -> None:
        """Idle-trigger loop: fire on timeout OR write-count threshold."""
        while self._running:
            await asyncio.sleep(1)
            if not self._queue:
                continue
            idle = (datetime.now(UTC) - self._last_write_at).total_seconds()
            if idle >= self._idle_seconds or self._pending_write_count >= self._write_threshold:
                await self.run_once()

    # ------------------------------------------------------------------ #
    # Internal consolidation                                               #
    # ------------------------------------------------------------------ #

    async def _consolidate_session(self, item: _QueueItem) -> dict[str, int]:
        """Run full consolidation for one session: summary + promotion."""
        session = self._event_store.get_session(item.session_id)

        events = [
            ev for eid in item.event_ids if (ev := self._event_store.get_event(eid)) is not None
        ]

        memories_created = 0
        summaries_created = 0

        # --- Session summary (LLM — idle-time only) ---
        if session is not None and events:
            summary = build_session_summary(session, events, self._llm)
            self._memory_store.create_session_summary(summary)
            summaries_created += 1

        # --- Promote promotable events to long-term memories ---
        for event in events:
            reason = classify_event_for_promotion(event)
            if reason is None:
                continue

            content: str = event.payload.get("content") or event.payload.get("summary") or ""
            title: str = str(event.payload.get("title") or reason.replace("_", " ").title())

            if not content:
                continue

            content_hash = Memory.compute_hash(content)
            embedding: list[float] | None = event.payload.get("embedding")  # None at P0

            dedup: DedupResult = run_write_time_dedup(
                content=content,
                content_hash=content_hash,
                embedding=embedding,
                memory_store=self._memory_store,
                vector_store=self._vector_store,
            )

            if dedup.action == "duplicate":
                self._handle_duplicate(dedup)
                continue

            # origin: "extracted" — content came directly from the event payload,
            # no LLM synthesis.  When an LLM summarises across events we'd use
            # "synthesized" — not yet implemented at P0.
            memory, sources = build_memory_from_event(
                event, reason, title, content, origin="extracted"
            )

            if dedup.action == "conflict":
                memory = memory.model_copy(update={"status": "conflict"})
                self._memory_store.create_memory(memory)
                for conflict_id in dedup.conflict_memory_ids:
                    self._memory_store.update_memory(conflict_id, {"status": "conflict"})
            else:
                self._memory_store.create_memory(memory)

            for source in sources:
                self._memory_store.create_memory_source(source)

            memories_created += 1

        return {"memories_created": memories_created, "summaries_created": summaries_created}

    def _handle_duplicate(self, dedup: DedupResult) -> None:
        """Bump access_count on the existing memory (duplicate reinforcement)."""
        if dedup.existing_memory_id is None:
            return
        existing = self._memory_store.get_memory(dedup.existing_memory_id)
        if existing is None:
            return
        self._memory_store.update_memory(
            dedup.existing_memory_id,
            {
                "access_count": existing.access_count + 1,
                "last_seen_at": datetime.now(UTC),
            },
        )
