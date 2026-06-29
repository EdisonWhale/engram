"""Claude Code transcript JSONL → Engram ParsedEvent adapter.

capture_confidence = "exact": Claude Code is the authoritative source.

Record format (from capture-schema.md, fields are observed not published):

  Top-level envelope:
    {"type": "user"|"assistant"|"tool_result"|"skill"|"permission"|"hook", ...}

  user/assistant records carry:
    "message": {"role": "...", "content": [<content blocks>]}

  Content block types inside message.content:
    {"type": "text",       "text": "..."}
    {"type": "tool_use",   "id": "...", "name": "...", "input": {...}}
    {"type": "tool_result","tool_use_id": "...", "content": "..."}

  tool_result top-level records carry:
    "toolUseResult": {"tool_use_id": "...", ...}

  For a Task (subagent) tool_result, toolUseResult also contains:
    agentId, agentType, totalDurationMs, totalTokens, totalToolUseCount, toolStats

Event-type mapping (capture-schema.md §Mapping):
  user + text blocks        → user_prompt
  assistant + text parts    → assistant_summary
  tool_use  name=Bash, git  → git
  tool_use  name=Read       → file_read
  tool_use  name=Edit/Write → file_edit
  tool_use  name=Task       → buffered; emitted as subagent when result arrives
  tool_use  other           → tool_call
  tool_result (non-Task)    → tool_result
  tool_use + tool_result
    where name=Task         → subagent  (single combined event)
  skill                     → skill
  permission                → permission

Design note: this class is stateful because Task calls must be held until their
result arrives.  One adapter instance per session; create a new one for each
session to avoid cross-session bleed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class ParsedEvent:
    """Normalised event ready for storage; produced by ClaudeCodeAdapter."""

    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime
    source_seq: int  # line number in source JSONL (for gap detection)
    raw_ref_offset: int  # byte offset of this record in the JSONL file
    capture_confidence: str = "exact"


class ClaudeCodeAdapter:
    """Stateful parser for Claude Code transcript JSONL records.

    Call process_record() once per JSONL line, in order.  Call pending_task_ids()
    at session_end to find Task calls that never received a result (interrupted spans).
    """

    def __init__(self) -> None:
        # Maps tool_use_id → tool_use content block for pending Task calls.
        # Non-Task tool_use blocks are NOT buffered — they emit immediately.
        self._pending_tasks: dict[str, dict[str, Any]] = {}

    def process_record(
        self,
        record: dict[str, Any],
        byte_offset: int,
        source_seq: int,
    ) -> list[ParsedEvent]:
        """Parse one JSONL record. Returns zero or more ParsedEvents."""
        record_type = record.get("type", "")
        ts = _parse_ts(record.get("timestamp", ""))

        match record_type:
            case "user":
                return self._handle_user(record, byte_offset, source_seq, ts)
            case "assistant":
                return self._handle_assistant(record, byte_offset, source_seq, ts)
            case "tool_result":
                return self._handle_top_level_tool_result(record, byte_offset, source_seq, ts)
            case "skill" | "permission" | "hook":
                return [
                    ParsedEvent(
                        event_type=record_type,
                        payload={"record": record},
                        occurred_at=ts,
                        source_seq=source_seq,
                        raw_ref_offset=byte_offset,
                    )
                ]
            case _:
                # Unknown record type — build defensively; skip silently
                return []

    def pending_task_ids(self) -> list[str]:
        """Return tool_use_ids for Task calls that have no matching result yet."""
        return list(self._pending_tasks)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _handle_user(
        self,
        record: dict[str, Any],
        byte_offset: int,
        source_seq: int,
        ts: datetime,
    ) -> list[ParsedEvent]:
        message = record.get("message") or {}
        content = message.get("content") or []
        events: list[ParsedEvent] = []

        text_parts = [b["text"] for b in content if b.get("type") == "text" and b.get("text")]
        if text_parts:
            events.append(
                ParsedEvent(
                    event_type="user_prompt",
                    payload={"text": " ".join(text_parts), "role": "user"},
                    occurred_at=ts,
                    source_seq=source_seq,
                    raw_ref_offset=byte_offset,
                )
            )
        # user messages can also carry tool_result content blocks (API format);
        # handle them the same as top-level tool_result records.
        for block in content:
            if block.get("type") == "tool_result":
                events.extend(self._emit_tool_result_block(block, byte_offset, source_seq, ts))
        return events

    def _handle_assistant(
        self,
        record: dict[str, Any],
        byte_offset: int,
        source_seq: int,
        ts: datetime,
    ) -> list[ParsedEvent]:
        message = record.get("message") or {}
        content = message.get("content") or []
        events: list[ParsedEvent] = []

        text_parts: list[str] = []
        for block in content:
            block_type = block.get("type", "")
            if block_type == "text":
                text = block.get("text", "")
                if text:
                    text_parts.append(text)
            elif block_type == "tool_use":
                events.extend(self._handle_tool_use_block(block, byte_offset, source_seq, ts))

        if text_parts:
            # Emit assistant_summary first (before tool_use events from same record)
            events.insert(
                0,
                ParsedEvent(
                    event_type="assistant_summary",
                    payload={"text": "\n".join(text_parts)[:2000]},  # cap at 2 KB
                    occurred_at=ts,
                    source_seq=source_seq,
                    raw_ref_offset=byte_offset,
                ),
            )
        return events

    def _handle_tool_use_block(
        self,
        block: dict[str, Any],
        byte_offset: int,
        source_seq: int,
        ts: datetime,
    ) -> list[ParsedEvent]:
        tool_id = block.get("id", "")
        name = block.get("name", "")
        inp = block.get("input") or {}

        if name == "Task":
            # Buffer; emit when the matching result arrives.
            self._pending_tasks[tool_id] = block
            return []

        event_type = _tool_use_event_type(name, inp)
        return [
            ParsedEvent(
                event_type=event_type,
                payload={"tool_use_id": tool_id, "name": name, "input": inp},
                occurred_at=ts,
                source_seq=source_seq,
                raw_ref_offset=byte_offset,
            )
        ]

    def _handle_top_level_tool_result(
        self,
        record: dict[str, Any],
        byte_offset: int,
        source_seq: int,
        ts: datetime,
    ) -> list[ParsedEvent]:
        result = record.get("toolUseResult") or {}
        return self._emit_tool_result_block(result, byte_offset, source_seq, ts)

    def _emit_tool_result_block(
        self,
        result: dict[str, Any],
        byte_offset: int,
        source_seq: int,
        ts: datetime,
    ) -> list[ParsedEvent]:
        tool_use_id = result.get("tool_use_id", "")

        if tool_use_id in self._pending_tasks:
            task_block = self._pending_tasks.pop(tool_use_id)
            return [
                ParsedEvent(
                    event_type="subagent",
                    payload={
                        "tool_use_id": tool_use_id,
                        "input": task_block.get("input") or {},
                        "agent_type": result.get("agentType", ""),
                        "agent_id": result.get("agentId", ""),
                        "status": result.get("status", ""),
                        "total_duration_ms": result.get("totalDurationMs", 0),
                        "total_tokens": result.get("totalTokens", 0),
                        "total_tool_use_count": result.get("totalToolUseCount", 0),
                        "tool_stats": result.get("toolStats") or {},
                        "content": result.get("content", ""),
                    },
                    occurred_at=ts,
                    source_seq=source_seq,
                    raw_ref_offset=byte_offset,
                )
            ]

        return [
            ParsedEvent(
                event_type="tool_result",
                payload={
                    "tool_use_id": tool_use_id,
                    "content": result.get("content", ""),
                },
                occurred_at=ts,
                source_seq=source_seq,
                raw_ref_offset=byte_offset,
            )
        ]


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


_GIT_TOOL_NAMES: frozenset[str] = frozenset({"Bash"})
_FILE_READ_TOOLS: frozenset[str] = frozenset({"Read"})
_FILE_EDIT_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit"})


def _tool_use_event_type(name: str, inp: dict[str, Any]) -> str:
    """Map tool name + input to the canonical event_type string."""
    if name in _GIT_TOOL_NAMES:
        cmd = (inp.get("command") or "").strip()
        if _is_git_command(cmd):
            return "git"
        return "tool_call"
    if name in _FILE_READ_TOOLS:
        return "file_read"
    if name in _FILE_EDIT_TOOLS:
        return "file_edit"
    return "tool_call"


def _is_git_command(cmd: str) -> bool:
    """Return True if *cmd* is a git invocation."""
    return cmd == "git" or cmd.startswith("git ") or cmd.startswith("git\t")


def _parse_ts(ts_str: str) -> datetime:
    """Parse an RFC3339 timestamp string, falling back to now(UTC) on failure."""
    if not ts_str:
        return datetime.now(UTC)
    try:
        # Python 3.11+ handles 'Z' suffix via fromisoformat
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
