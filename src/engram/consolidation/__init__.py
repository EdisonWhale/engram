"""Consolidation package: raw events → session summaries, task contexts, long-term memories.

Spec §6 (short vs long-term), §10 (lifecycle), §27 (two-phase pipeline).

Public surface consumed by mcp/server.py
-----------------------------------------
For the ``memory_consolidate`` MCP tool and the ``session_end`` summary side::

    from engram.consolidation import (
        ConsolidationWorker,
        build_session_summary,
        complete_task_context,
        create_task_context,
        expire_task_contexts,
    )

Exact MCP wiring signatures
----------------------------
``memory_consolidate`` tool::

    result = await worker.run_once(
        project_id=params.project,   # optional filter
        session_id=params.session_id, # optional filter
    )
    # result: {"sessions_processed": int, "memories_created": int, "summaries_created": int}

``session_end`` summary side (called after WS-A closes the session)::

    worker.enqueue_event(session_id, project_id, event_id)  # per-event on write path
    result = await worker.run_once(session_id=session_id)   # flush on session_end
"""

from engram.consolidation.llm import AnthropicLLMClient, LLMClient, MockLLMClient
from engram.consolidation.pipeline import ConsolidationWorker
from engram.consolidation.summarize import build_session_summary
from engram.consolidation.task_context import (
    complete_task_context,
    create_task_context,
    expire_task_contexts,
)

__all__ = [
    # Pipeline
    "ConsolidationWorker",
    # Session summary
    "build_session_summary",
    # Task context helpers
    "complete_task_context",
    "create_task_context",
    "expire_task_contexts",
    # LLM clients (for wiring / dependency injection)
    "LLMClient",
    "MockLLMClient",
    "AnthropicLLMClient",
]
