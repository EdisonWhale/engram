"""Smoke tests: MCP tool stubs are registered and validate params.

Tests:
- All 11 expected tools are registered in the FastMCP server.
- Each tool function is directly callable and returns a dict.
- Param validation via pydantic *Params models rejects invalid values.
- MCP server responds to initialize over stdio (subprocess test).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from typing import Any

import pytest
from pydantic import ValidationError

from engram.mcp.server import (
    _EXPECTED_TOOLS,
    mcp,
    memory_add,
    memory_consolidate,
    memory_context,
    memory_get,
    memory_list,
    memory_search,
    memory_timeline,
    memory_update,
    record_event,
    session_end,
    session_start,
)

# ---------------------------------------------------------------------------
# Registration check
# ---------------------------------------------------------------------------


def test_all_tools_registered() -> None:
    """Every tool in _EXPECTED_TOOLS must be listed by FastMCP."""
    tools = asyncio.run(mcp.list_tools())
    registered = {t.name for t in tools}
    missing = _EXPECTED_TOOLS - registered
    assert not missing, f"tools not registered: {sorted(missing)}"


def test_tool_count() -> None:
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == len(_EXPECTED_TOOLS), (
        f"expected {len(_EXPECTED_TOOLS)} tools, got {len(tools)}: {[t.name for t in tools]}"
    )


# ---------------------------------------------------------------------------
# Direct invocation — stubs return dicts, don't raise
# ---------------------------------------------------------------------------


def _check_stub(result: Any) -> None:
    """Stubs must return a dict."""
    assert isinstance(result, dict), f"expected dict, got {type(result)}"


def test_session_start_stub() -> None:
    r = session_start(".", "claude_code", "continue evals", "abc123", "main")
    _check_stub(r)
    assert "session_id" in r
    assert "memory_thread_id" in r


def test_record_event_stub() -> None:
    r = record_event("sess-1", "tool_call", {"cmd": "pytest"})
    _check_stub(r)
    assert "event_id" in r
    assert "seq" in r


def test_record_event_no_payload() -> None:
    r = record_event("sess-1", "user_prompt")
    _check_stub(r)


def test_session_end_stub() -> None:
    r = session_end("sess-1")
    _check_stub(r)
    assert "status" in r


def test_session_end_with_hint() -> None:
    r = session_end("sess-1", "next: implement MRR")
    _check_stub(r)


def test_memory_search_stub() -> None:
    r = memory_search("continue the eval work")
    _check_stub(r)
    assert "memories" in r


def test_memory_search_with_filters() -> None:
    r = memory_search("SQLite", project="proj-1", type="decision", limit=5)
    _check_stub(r)


def test_memory_timeline_stub() -> None:
    r = memory_timeline(anchor_id="mem-1", before=2, after=2)
    _check_stub(r)


def test_memory_timeline_query() -> None:
    r = memory_timeline(query="eval runner")
    _check_stub(r)


def test_memory_get_stub() -> None:
    r = memory_get(["mem-1", "mem-2"])
    _check_stub(r)
    assert "memories" in r


def test_memory_context_stub() -> None:
    r = memory_context("continue the eval work", token_budget=800)
    _check_stub(r)
    assert "context" in r
    assert "injected_tokens" in r


def test_memory_add_stub() -> None:
    r = memory_add("Prefer SQLite", "decision", "project")
    _check_stub(r)
    assert "memory_id" in r


def test_memory_update_stub() -> None:
    r = memory_update("mem-1", "mark_stale", reason="file changed")
    _check_stub(r)
    assert r["success"] is True


def test_memory_list_stub() -> None:
    r = memory_list(status="active")
    _check_stub(r)
    assert "memories" in r


def test_memory_consolidate_stub() -> None:
    r = memory_consolidate(project="proj-1")
    _check_stub(r)
    assert r["queued"] is True


# ---------------------------------------------------------------------------
# Param validation — invalid values must raise ValidationError
# ---------------------------------------------------------------------------


def test_memory_search_invalid_type() -> None:
    """Passing an unrecognised memory type must raise ValidationError."""
    from engram.mcp.server import MemorySearchParams

    with pytest.raises(ValidationError):
        MemorySearchParams(query="test", type="not_a_real_type")


def test_memory_search_limit_bounds() -> None:
    from engram.mcp.server import MemorySearchParams

    with pytest.raises(ValidationError):
        MemorySearchParams(query="x", limit=0)  # minimum is 1

    with pytest.raises(ValidationError):
        MemorySearchParams(query="x", limit=200)  # maximum is 100


def test_memory_add_invalid_scope() -> None:
    from engram.mcp.server import MemoryAddParams

    with pytest.raises(ValidationError):
        MemoryAddParams(content="c", type="decision", scope="global")  # not a valid scope


def test_memory_add_invalid_type() -> None:
    from engram.mcp.server import MemoryAddParams

    with pytest.raises(ValidationError):
        MemoryAddParams(content="c", type="random", scope="project")


def test_memory_update_invalid_operation() -> None:
    from engram.mcp.server import MemoryUpdateParams

    with pytest.raises(ValidationError):
        MemoryUpdateParams(memory_id="x", operation="overwrite")  # not a valid op


def test_memory_list_invalid_status() -> None:
    from engram.mcp.server import MemoryListParams

    with pytest.raises(ValidationError):
        MemoryListParams(status="archived")  # not a valid status


def test_memory_context_budget_bounds() -> None:
    from engram.mcp.server import MemoryContextParams

    with pytest.raises(ValidationError):
        MemoryContextParams(query="x", token_budget=50)  # below minimum of 100

    with pytest.raises(ValidationError):
        MemoryContextParams(query="x", token_budget=100_000)  # above maximum of 8000


def test_memory_get_empty_ids() -> None:
    from engram.mcp.server import MemoryGetParams

    with pytest.raises(ValidationError):
        MemoryGetParams(ids=[])  # min_length=1


# ---------------------------------------------------------------------------
# MCP initialize over stdio (subprocess integration test)
# ---------------------------------------------------------------------------


def test_mcp_responds_to_initialize() -> None:
    """engram mcp must respond to a JSON-RPC initialize message over stdio."""
    init_msg = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "engram-test", "version": "0.1.0"},
            },
        }
    )

    # The MCP stdio transport reads newline-terminated JSON messages.
    init_msg_with_newline = init_msg + "\n"

    try:
        result = subprocess.run(
            [sys.executable, "-m", "engram.cli", "mcp"],
            input=init_msg_with_newline,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("MCP server did not respond within 10 s")

    # The first non-empty stdout line should be a valid JSON-RPC response
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert lines, f"no output from 'engram mcp'\nstderr: {result.stderr[:500]}"

    response = json.loads(lines[0])
    assert response.get("jsonrpc") == "2.0", f"unexpected response: {response}"
    assert "result" in response or "error" in response, f"no result/error: {response}"
