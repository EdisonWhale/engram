"""Engram capture package — WS-A public API.

This is the integration surface the MCP server.py tools should call.
All three session tools (session_start / record_event / session_end) are
exposed here with their exact signatures.

Wire-up example for server.py (to be done by the main agent):

    from engram.capture import session_start, record_event, session_end
    from engram.db.runner import open_db
    from engram.store.sqlite_store import SQLiteEventStore, SQLiteMemoryStore

    _conn = open_db(db_path)
    _event_store = SQLiteEventStore(_conn)
    _memory_store = SQLiteMemoryStore(_conn)

    @mcp.tool()
    def session_start_tool(project_path, agent, prompt, git_sha, branch):
        return session_start(_event_store, _memory_store,
                             project_path, agent, prompt, git_sha, branch)

    @mcp.tool()
    def record_event_tool(session_id, event_type, payload=None):
        evt = record_event(_event_store, session_id, event_type, payload or {})
        return {"event_id": evt.id if evt else None, "seq": evt.seq if evt else None}

    @mcp.tool()
    def session_end_tool(session_id, summary_hint=None):
        return session_end(_event_store, session_id, summary_hint)

See also:
  - src/engram/capture/ingest.py   — session lifecycle implementation
  - src/engram/capture/tailer.py   — transcript directory tailer
  - src/engram/capture/adapters/   — per-platform JSONL parsers
"""

from __future__ import annotations

from engram.capture.adapters.claude_code import ClaudeCodeAdapter, ParsedEvent
from engram.capture.ingest import record_event, session_end, session_start
from engram.capture.tailer import TranscriptTailer, encode_project_path

__all__ = [
    # Ingest API — wired into MCP session tools
    "session_start",
    "record_event",
    "session_end",
    # Tailer — used by the background transcript-watching task
    "TranscriptTailer",
    "encode_project_path",
    # Adapter — used by tests and the tailer integration
    "ClaudeCodeAdapter",
    "ParsedEvent",
]
