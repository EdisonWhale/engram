"""Session summary generation (spec §10.1, §8.1 session_end).

build_session_summary is called ONLY from idle-time consolidation, never from
the write/capture path (ADR 0003).
"""

from __future__ import annotations

import json
import logging

from engram.consolidation.llm import LLMClient
from engram.models import AgentSession, Event, SessionSummary

logger = logging.getLogger(__name__)

_SUMMARY_PROMPT = """\
You are summarizing a completed agent coding session for a cross-session memory system.
Given the session events below, produce a JSON object with exactly these fields:
  "request"       : the main task or goal the agent was working on (string)
  "completed"     : what was actually finished during this session (string)
  "learned"       : key insights, decisions, or facts discovered (string)
  "next_steps"    : what the next session should do first (string)
  "files_read"    : list of file paths read (from file_read events) (array of strings)
  "files_modified": list of file paths modified (from file_write/edit events) (array of strings)

Session events (JSON):
{events_json}

Summary hint from caller (may be empty): {hint}

Return ONLY valid JSON with those six fields. No markdown fences, no extra keys.
"""


def build_session_summary(
    session: AgentSession,
    events: list[Event],
    llm: LLMClient,
    hint: str | None = None,
) -> SessionSummary | None:
    """Synthesize a SessionSummary from a completed session's events via LLM.

    Returns ``None`` when the LLM produced no usable output (empty string or
    unparseable JSON — e.g. the no-API-key MockLLMClient, or a real API
    failure).  The caller must NOT persist a summary in that case: a fabricated
    "empty summary" is indistinguishable from a genuinely empty session and
    silently pollutes storage.  A sparse-but-valid JSON object IS a real summary
    and is returned as-is.

    Args:
        session: The AgentSession being summarised.
        events:  Raw events from the session (up to 50 used; extras trimmed).
        llm:     LLM client — called exactly once per invocation.
        hint:    Optional free-text hint from the session_end caller.
    """
    event_summaries = [
        {
            "event_type": e.event_type,
            "payload_keys": list(e.payload.keys()),
        }
        for e in events[:50]
    ]
    prompt = _SUMMARY_PROMPT.format(
        events_json=json.dumps(event_summaries, indent=2),
        hint=hint or "(none)",
    )

    raw = llm.complete(prompt)

    if not raw or not raw.strip():
        logger.error(
            "LLM returned empty output for session %s summary; skipping summary "
            "(no API key / mock client, or upstream failure)",
            session.id,
        )
        return None

    try:
        data: dict = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.error(
            "LLM returned unparseable JSON for session %s summary; skipping summary. "
            "Payload prefix: %r",
            session.id,
            raw[:200],
        )
        return None

    return SessionSummary(
        project_id=session.project_id,
        session_id=session.id,
        request=str(data.get("request", hint or "unknown")),
        completed=str(data.get("completed", "")),
        learned=str(data.get("learned", "")),
        next_steps=str(data.get("next_steps", "")),
        files_read=list(data.get("files_read", [])),
        files_modified=list(data.get("files_modified", [])),
        source_event_ids=[e.id for e in events],
    )
