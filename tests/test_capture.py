"""Tests for the WS-A capture module.

Test-first coverage required by CLAUDE.md for the transcript parser/state-machine
(high-risk: it is the only path that captures subagents, git calls, and edits).

Acceptance criteria (from A-capture.md):
  AC-1  Fixture in → every tool call AND subagent invocation appears as an event;
        seq has no gaps.
  AC-2  A simulated dropped line is DETECTED (gap reported), not silently passed.
  AC-3  A Task (subagent) call yields an event with agentType + token metadata.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from engram.capture.adapters.claude_code import ClaudeCodeAdapter
from engram.capture.ingest import record_event, session_end, session_start
from engram.capture.tailer import encode_project_path, parse_transcript_lines
from engram.db.runner import open_db
from engram.store.sqlite_store import SQLiteEventStore, SQLiteMemoryStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "transcripts" / "claude_code_session.jsonl"


def _load_fixture() -> list[dict]:
    """Load the JSONL fixture as a list of dicts."""
    records = []
    with FIXTURE_PATH.open() as fh:
        offset = 0
        for line in fh:
            line = line.rstrip("\n")
            if line:
                records.append({"raw": json.loads(line), "offset": offset})
            offset += len(line.encode()) + 1  # +1 for the \n
    return records


def _make_stores():
    conn = open_db(":memory:")
    event_store = SQLiteEventStore(conn)
    memory_store = SQLiteMemoryStore(conn)
    return event_store, memory_store


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter unit tests
# ---------------------------------------------------------------------------


class TestClaudeCodeAdapter:
    """Unit tests for the JSONL parser state-machine."""

    def test_user_record_yields_user_prompt(self):
        adapter = ClaudeCodeAdapter()
        record = {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2024-01-15T10:00:00.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Hello, implement something"}],
            },
        }
        events = adapter.process_record(record, byte_offset=0, source_seq=1)
        assert len(events) == 1
        assert events[0].event_type == "user_prompt"
        assert "implement something" in events[0].payload["text"]

    def test_bash_git_command_yields_git_event(self):
        adapter = ClaudeCodeAdapter()
        record = {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2024-01-15T10:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking git."},
                    {
                        "type": "tool_use",
                        "id": "toolu_001",
                        "name": "Bash",
                        "input": {"command": "git status --short", "description": "Check status"},
                    },
                ],
            },
        }
        events = adapter.process_record(record, byte_offset=100, source_seq=2)
        types = [e.event_type for e in events]
        assert "git" in types
        assert "assistant_summary" in types

    def test_non_git_bash_yields_tool_call(self):
        adapter = ClaudeCodeAdapter()
        record = {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2024-01-15T10:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_002",
                        "name": "Bash",
                        "input": {"command": "uv run pytest", "description": "Run tests"},
                    }
                ],
            },
        }
        events = adapter.process_record(record, byte_offset=200, source_seq=3)
        types = [e.event_type for e in events]
        assert "tool_call" in types
        assert "git" not in types

    def test_read_tool_yields_file_read(self):
        adapter = ClaudeCodeAdapter()
        record = {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2024-01-15T10:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_003",
                        "name": "Read",
                        "input": {"file_path": "/project/src/models.py"},
                    }
                ],
            },
        }
        events = adapter.process_record(record, byte_offset=300, source_seq=4)
        assert any(e.event_type == "file_read" for e in events)

    def test_edit_tool_yields_file_edit(self):
        adapter = ClaudeCodeAdapter()
        record = {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2024-01-15T10:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_004",
                        "name": "Edit",
                        "input": {
                            "file_path": "/project/src/models.py",
                            "old_string": "x",
                            "new_string": "y",
                        },
                    }
                ],
            },
        }
        events = adapter.process_record(record, byte_offset=400, source_seq=5)
        assert any(e.event_type == "file_edit" for e in events)

    def test_task_call_produces_no_immediate_event(self):
        """A Task call must be buffered until the result arrives."""
        adapter = ClaudeCodeAdapter()
        record = {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2024-01-15T10:00:01.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_task_01",
                        "name": "Task",
                        "input": {
                            "description": "Run tests",
                            "isolation": "worktree",
                            "model": "claude-sonnet-4-5",
                            "prompt": "Run pytest",
                            "subagentType": "claude",
                        },
                    }
                ],
            },
        }
        events = adapter.process_record(record, byte_offset=500, source_seq=6)
        # No subagent event yet — only possible assistant_summary for text
        assert not any(e.event_type == "subagent" for e in events)
        assert not any(e.event_type == "tool_call" for e in events)

    # AC-3 ---------------------------------------------------------------
    def test_task_result_yields_subagent_event_with_metadata(self):
        """AC-3: Task call + result → subagent event with agentType + token metadata."""
        adapter = ClaudeCodeAdapter()
        # First the call
        call_record = {
            "type": "assistant",
            "uuid": "a2",
            "timestamp": "2024-01-15T10:00:05.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_task_02",
                        "name": "Task",
                        "input": {
                            "description": "Run tests",
                            "isolation": "worktree",
                            "model": "claude-sonnet-4-5",
                            "prompt": "Run pytest",
                            "subagentType": "claude",
                        },
                    }
                ],
            },
        }
        adapter.process_record(call_record, byte_offset=600, source_seq=7)

        # Then the result
        result_record = {
            "type": "tool_result",
            "uuid": "tr2",
            "timestamp": "2024-01-15T10:00:10.000Z",
            "toolUseResult": {
                "tool_use_id": "toolu_task_02",
                "status": "completed",
                "agentId": "agent-xyz789",
                "agentType": "claude",
                "totalDurationMs": 4823,
                "totalTokens": 2147,
                "totalToolUseCount": 5,
                "toolStats": {"Bash": 3, "Read": 2},
                "content": "All tests passed.",
            },
        }
        events = adapter.process_record(result_record, byte_offset=700, source_seq=8)

        assert len(events) == 1
        subagent_evt = events[0]
        assert subagent_evt.event_type == "subagent"
        # Must carry agentType and token metadata
        assert subagent_evt.payload["agent_type"] == "claude"
        assert subagent_evt.payload["total_tokens"] == 2147
        assert subagent_evt.payload["total_tool_use_count"] == 5
        assert subagent_evt.payload["agent_id"] == "agent-xyz789"

    def test_tool_result_for_non_task_yields_tool_result_event(self):
        adapter = ClaudeCodeAdapter()
        record = {
            "type": "tool_result",
            "uuid": "tr3",
            "timestamp": "2024-01-15T10:00:02.000Z",
            "toolUseResult": {
                "tool_use_id": "toolu_bash_01",
                "content": "M src/file.py",
            },
        }
        events = adapter.process_record(record, byte_offset=800, source_seq=9)
        assert len(events) == 1
        assert events[0].event_type == "tool_result"
        assert events[0].payload["tool_use_id"] == "toolu_bash_01"

    def test_skill_record_yields_skill_event(self):
        adapter = ClaudeCodeAdapter()
        record = {
            "type": "skill",
            "uuid": "sk1",
            "timestamp": "2024-01-15T10:00:01.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "karpathy-guidelines"}],
            },
        }
        events = adapter.process_record(record, byte_offset=900, source_seq=10)
        assert len(events) == 1
        assert events[0].event_type == "skill"

    def test_permission_record_yields_permission_event(self):
        adapter = ClaudeCodeAdapter()
        record = {
            "type": "permission",
            "uuid": "pm1",
            "timestamp": "2024-01-15T10:00:01.000Z",
            "message": {"role": "user", "content": []},
        }
        events = adapter.process_record(record, byte_offset=950, source_seq=11)
        assert len(events) == 1
        assert events[0].event_type == "permission"

    def test_unknown_record_type_yields_no_events(self):
        adapter = ClaudeCodeAdapter()
        record = {
            "type": "unknown_future_type",
            "uuid": "x1",
            "timestamp": "2024-01-15T10:00:00.000Z",
        }
        events = adapter.process_record(record, byte_offset=1000, source_seq=12)
        assert events == []

    def test_raw_ref_offset_set_on_every_event(self):
        adapter = ClaudeCodeAdapter()
        record = {
            "type": "user",
            "uuid": "u5",
            "timestamp": "2024-01-15T10:00:00.000Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        }
        events = adapter.process_record(record, byte_offset=12345, source_seq=13)
        for e in events:
            assert e.raw_ref_offset == 12345


# ---------------------------------------------------------------------------
# AC-1: fixture in → all expected events present, seq contiguous
# ---------------------------------------------------------------------------


class TestFixtureIngestion:
    """Integration test: parse the JSONL fixture end-to-end through the ingest API."""

    @pytest.fixture()
    def stores(self):
        return _make_stores()

    @pytest.fixture()
    def session_id(self, stores):
        event_store, memory_store = stores
        result = session_start(
            event_store=event_store,
            memory_store=memory_store,
            project_path="/tmp/test_project",
            agent="claude_code",
            prompt="Implement Engram capture module",
            git_sha="abc123",
            branch="ws-a-capture",
        )
        return result["session_id"]

    def test_all_expected_event_types_present(self, stores, session_id):
        """AC-1: Every tool call AND subagent invocation appears as an event."""
        event_store, _ = stores
        adapter = ClaudeCodeAdapter()
        raw_file = str(FIXTURE_PATH)

        with FIXTURE_PATH.open("rb") as fh:
            source_seq = 0
            for raw_line in fh:
                byte_offset = fh.tell() - len(raw_line)
                line = raw_line.decode().rstrip("\n")
                if not line:
                    continue
                source_seq += 1
                rec = json.loads(line)
                parsed_events = adapter.process_record(
                    rec, byte_offset=byte_offset, source_seq=source_seq
                )
                for pe in parsed_events:
                    record_event(
                        event_store=event_store,
                        session_id=session_id,
                        event_type=pe.event_type,
                        payload=pe.payload,
                        source_type="transcript",
                        raw_ref_file=raw_file,
                        raw_ref_offset=pe.raw_ref_offset,
                        source_seq=pe.source_seq,
                        capture_confidence=pe.capture_confidence,
                        occurred_at=pe.occurred_at,
                    )

        events = event_store.list_session_events(session_id)
        event_types = {e.event_type for e in events}

        # Every required type must appear
        assert "user_prompt" in event_types, f"missing user_prompt; got {event_types}"
        assert "assistant_summary" in event_types, f"missing assistant_summary; got {event_types}"
        assert "git" in event_types, f"missing git; got {event_types}"
        assert "file_read" in event_types, f"missing file_read; got {event_types}"
        assert "file_edit" in event_types, f"missing file_edit; got {event_types}"
        assert "tool_result" in event_types, f"missing tool_result; got {event_types}"
        assert "subagent" in event_types, f"missing subagent; got {event_types}"

    def test_seq_has_no_gaps(self, stores, session_id):
        """AC-1: seq must be contiguous (no internal gaps)."""
        event_store, _ = stores
        adapter = ClaudeCodeAdapter()

        with FIXTURE_PATH.open("rb") as fh:
            source_seq = 0
            for raw_line in fh:
                byte_offset = fh.tell() - len(raw_line)
                line = raw_line.decode().rstrip("\n")
                if not line:
                    continue
                source_seq += 1
                rec = json.loads(line)
                for pe in adapter.process_record(
                    rec, byte_offset=byte_offset, source_seq=source_seq
                ):
                    record_event(
                        event_store=event_store,
                        session_id=session_id,
                        event_type=pe.event_type,
                        payload=pe.payload,
                        source_type="transcript",
                        raw_ref_file=str(FIXTURE_PATH),
                        raw_ref_offset=pe.raw_ref_offset,
                        source_seq=pe.source_seq,
                        capture_confidence=pe.capture_confidence,
                        occurred_at=pe.occurred_at,
                    )

        events = event_store.list_session_events(session_id)
        seqs = [e.seq for e in events]
        assert seqs == list(range(1, len(seqs) + 1)), f"seq gaps: {seqs}"

    # AC-3 -------------------------------------------------------------------
    def test_task_event_has_agent_type_and_tokens(self, stores, session_id):
        """AC-3: The subagent event from the fixture has agentType + totalTokens."""
        event_store, _ = stores
        adapter = ClaudeCodeAdapter()

        with FIXTURE_PATH.open("rb") as fh:
            source_seq = 0
            for raw_line in fh:
                byte_offset = fh.tell() - len(raw_line)
                line = raw_line.decode().rstrip("\n")
                if not line:
                    continue
                source_seq += 1
                rec = json.loads(line)
                for pe in adapter.process_record(
                    rec, byte_offset=byte_offset, source_seq=source_seq
                ):
                    record_event(
                        event_store=event_store,
                        session_id=session_id,
                        event_type=pe.event_type,
                        payload=pe.payload,
                        source_type="transcript",
                        raw_ref_file=str(FIXTURE_PATH),
                        raw_ref_offset=pe.raw_ref_offset,
                        source_seq=pe.source_seq,
                        capture_confidence=pe.capture_confidence,
                        occurred_at=pe.occurred_at,
                    )

        events = event_store.list_session_events(session_id)
        subagent_events = [e for e in events if e.event_type == "subagent"]
        assert subagent_events, "no subagent event found"
        sa = subagent_events[0]
        assert sa.payload.get("agent_type") == "claude", f"agent_type missing/wrong: {sa.payload}"
        assert sa.payload.get("total_tokens") == 2147, f"total_tokens missing/wrong: {sa.payload}"
        assert sa.payload.get("total_tool_use_count") == 5, f"tool_use_count: {sa.payload}"


# ---------------------------------------------------------------------------
# AC-2: simulated dropped line → gap detected
# ---------------------------------------------------------------------------


class TestGapDetection:
    """AC-2: A dropped source_seq is detected at session_end."""

    @pytest.fixture()
    def stores(self):
        return _make_stores()

    @pytest.fixture()
    def session_id(self, stores):
        event_store, memory_store = stores
        result = session_start(
            event_store=event_store,
            memory_store=memory_store,
            project_path="/tmp/gap_test",
            agent="claude_code",
            prompt="gap test",
            git_sha="def456",
            branch="main",
        )
        return result["session_id"]

    def test_contiguous_source_seq_is_complete(self, stores, session_id):
        event_store, _ = stores
        ts = datetime.now(UTC)
        # Record source_seq 1, 2, 3 — no gap
        for i in range(1, 4):
            record_event(
                event_store,
                session_id,
                "user_prompt",
                {"text": f"msg {i}"},
                source_seq=i,
                occurred_at=ts,
                source_type="transcript",
            )
        result = session_end(event_store, session_id)
        assert result["capture_complete"] is True
        assert result["gaps"] == []

    def test_missing_source_seq_detected_as_gap(self, stores, session_id):
        """AC-2: Gap in source_seq → capture_complete=False, gaps list non-empty."""
        event_store, _ = stores
        ts = datetime.now(UTC)
        # Record source_seq 1, 2, 4, 5 — source_seq 3 is "dropped"
        for i in (1, 2, 4, 5):
            record_event(
                event_store,
                session_id,
                "user_prompt",
                {"text": f"msg {i}"},
                source_seq=i,
                occurred_at=ts,
                source_type="transcript",
            )
        result = session_end(event_store, session_id)
        assert result["capture_complete"] is False, "gap not detected"
        assert 3 in result["gaps"], f"missing seq 3 not in gaps: {result['gaps']}"

    def test_parse_transcript_lines_malformed_surfaces_as_gap(self, stores, session_id):
        """AC-2: a malformed line consumes its source_seq but emits nothing, so the
        missing number is a provable gap — never a silent drop."""
        event_store, _ = stores
        ts = datetime.now(UTC)
        lines = [
            '{"type":"user","message":{"role":"user","content":"one"}}',
            "{ this is not valid json",  # line 2 — must NOT vanish silently
            '{"type":"user","message":{"role":"user","content":"three"}}',
        ]
        emitted = list(parse_transcript_lines(lines, raw_ref_file="x.jsonl"))
        # Only the two valid records are yielded, with source_seq 1 and 3 (2 skipped).
        assert [seq for _, _, _, seq in emitted] == [1, 3]
        for record, _, _, seq in emitted:
            record_event(
                event_store,
                session_id,
                "user_prompt",
                record,
                source_seq=seq,
                occurred_at=ts,
                source_type="transcript",
            )
        result = session_end(event_store, session_id)
        assert result["capture_complete"] is False
        assert 2 in result["gaps"], f"malformed line not surfaced as gap: {result['gaps']}"

    def test_pending_span_makes_session_incomplete(self, stores, session_id):
        """A tool_call with no matching tool_result is a pending span → incomplete."""
        event_store, _ = stores
        ts = datetime.now(UTC)
        # tool_call with tool_use_id but no matching tool_result
        record_event(
            event_store,
            session_id,
            "tool_call",
            {"tool_use_id": "dangling_tu_001", "name": "Bash", "input": {"command": "npm install"}},
            source_seq=1,
            occurred_at=ts,
            source_type="transcript",
        )
        result = session_end(event_store, session_id)
        assert result["capture_complete"] is False
        assert "dangling_tu_001" in result["pending_spans"]

    def test_matched_tool_use_result_pair_is_complete(self, stores, session_id):
        event_store, _ = stores
        ts = datetime.now(UTC)
        record_event(
            event_store,
            session_id,
            "tool_call",
            {"tool_use_id": "matched_tu_001", "name": "Bash", "input": {"command": "ls"}},
            source_seq=1,
            occurred_at=ts,
            source_type="transcript",
        )
        record_event(
            event_store,
            session_id,
            "tool_result",
            {"tool_use_id": "matched_tu_001", "content": "file.py"},
            source_seq=2,
            occurred_at=ts,
            source_type="transcript",
        )
        result = session_end(event_store, session_id)
        assert result["capture_complete"] is True
        assert result["pending_spans"] == []


# ---------------------------------------------------------------------------
# Ingest API tests
# ---------------------------------------------------------------------------


class TestIngestAPI:
    """Tests for session_start / record_event / session_end."""

    @pytest.fixture()
    def stores(self):
        return _make_stores()

    def test_session_start_creates_session_and_project(self, stores):
        event_store, memory_store = stores
        result = session_start(
            event_store=event_store,
            memory_store=memory_store,
            project_path="/tmp/my_project",
            agent="claude_code",
            prompt="Build a new feature",
            git_sha="abc000",
            branch="feature/x",
        )
        assert "session_id" in result
        assert "memory_thread_id" in result
        assert "project_id" in result
        assert result["thread_ambiguous"] is False

        session = event_store.get_session(result["session_id"])
        assert session is not None
        assert session.agent == "claude_code"
        assert session.branch == "feature/x"
        assert session.git_sha == "abc000"
        assert session.memory_thread_id == result["memory_thread_id"]

    def test_session_start_idempotent_project(self, stores):
        """Two session_starts for the same path reuse the same project."""
        event_store, memory_store = stores
        r1 = session_start(
            event_store, memory_store, "/tmp/proj_a", "claude_code", "p1", "sha1", "main"
        )
        r2 = session_start(
            event_store, memory_store, "/tmp/proj_a", "claude_code", "p2", "sha2", "main"
        )
        assert r1["project_id"] == r2["project_id"]

    def test_record_event_assigns_monotonic_seq(self, stores):
        event_store, memory_store = stores
        r = session_start(
            event_store, memory_store, "/tmp/seq_test", "claude_code", "p", "sha", "main"
        )
        sid = r["session_id"]
        ts = datetime.now(UTC)
        evts = [
            record_event(event_store, sid, "user_prompt", {"text": f"msg {i}"}, occurred_at=ts)
            for i in range(3)
        ]
        seqs = [e.seq for e in evts if e is not None]
        assert seqs == [1, 2, 3]

    def test_record_event_stores_raw_ref_fields(self, stores):
        event_store, memory_store = stores
        r = session_start(
            event_store, memory_store, "/tmp/ref_test", "claude_code", "p", "sha", "main"
        )
        sid = r["session_id"]
        evt = record_event(
            event_store,
            sid,
            "git",
            {"tool_use_id": "x", "name": "Bash", "input": {"command": "git log"}},
            source_type="transcript",
            raw_ref_file="/home/user/.claude/projects/-tmp-proj/abc.jsonl",
            raw_ref_offset=256,
            source_seq=3,
            capture_confidence="exact",
            occurred_at=datetime.now(UTC),
        )
        assert evt is not None
        assert evt.raw_ref_file == "/home/user/.claude/projects/-tmp-proj/abc.jsonl"
        assert evt.raw_ref_offset == 256
        assert evt.source_seq == 3
        assert evt.capture_confidence == "exact"

    def test_session_end_marks_session_completed(self, stores):
        event_store, memory_store = stores
        r = session_start(
            event_store, memory_store, "/tmp/end_test", "claude_code", "p", "sha", "main"
        )
        sid = r["session_id"]
        result = session_end(event_store, sid)
        assert result["session_id"] == sid
        assert result["capture_complete"] is True

        session = event_store.get_session(sid)
        assert session is not None
        assert session.status == "completed"
        assert session.ended_at is not None

    def test_session_end_returns_events_captured(self, stores):
        event_store, memory_store = stores
        r = session_start(
            event_store, memory_store, "/tmp/count_test", "claude_code", "p", "sha", "main"
        )
        sid = r["session_id"]
        ts = datetime.now(UTC)
        for i in range(5):
            record_event(event_store, sid, "user_prompt", {"text": f"m{i}"}, occurred_at=ts)
        result = session_end(event_store, sid)
        assert result["events_captured"] == 5

    def test_record_event_content_hash_is_deterministic(self, stores):
        event_store, memory_store = stores
        r = session_start(
            event_store, memory_store, "/tmp/hash_test", "claude_code", "p", "sha", "main"
        )
        sid = r["session_id"]
        payload = {"text": "hello world", "role": "user"}
        ts = datetime.now(UTC)
        e1 = record_event(event_store, sid, "user_prompt", payload, occurred_at=ts)
        e2 = record_event(event_store, sid, "user_prompt", payload, occurred_at=ts)
        assert e1 is not None
        assert e2 is not None
        assert e1.content_hash == e2.content_hash


# ---------------------------------------------------------------------------
# TranscriptTailer helper tests
# ---------------------------------------------------------------------------


class TestEncodeProjectPath:
    """Tests for the path encoding function."""

    def test_slashes_become_dashes(self):
        assert encode_project_path("/Users/user/project") == "-Users-user-project"

    def test_dots_become_dashes(self):
        assert encode_project_path("/home/user/my.project") == "-home-user-my-project"

    def test_mixed_slashes_and_dots(self):
        result = encode_project_path("/Users/user/.claude/project.git")
        assert "/" not in result
        assert "." not in result

    def test_empty_string(self):
        assert encode_project_path("") == ""
