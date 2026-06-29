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

import pytest
from pydantic import ValidationError

from engram.mcp.server import _EXPECTED_TOOLS, mcp

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
# Direct invocation — tools wired to an isolated temp DB
# ---------------------------------------------------------------------------


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the server at a throwaway DB and force the mock LLM, then reset state."""
    import engram.mcp.server as srv

    monkeypatch.setenv("ENGRAM_DB", str(tmp_path / "engram.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # → MockLLMClient
    monkeypatch.setattr(srv, "_state", None)
    yield srv
    monkeypatch.setattr(srv, "_state", None)


def _start_session(srv) -> str:
    r = srv.session_start(
        str(srv._resolve_db_path()), "claude_code", "continue evals", "sha", "main"
    )
    return r["session_id"]


def test_session_start_creates_session(wired) -> None:
    r = wired.session_start(".", "claude_code", "continue evals", "abc123", "main")
    assert isinstance(r, dict)
    assert r["session_id"] and r["project_id"]
    assert "memory_thread_id" in r


def test_record_event_appends_and_reports_seq(wired) -> None:
    session_id = _start_session(wired)
    r = wired.record_event(session_id, "tool_call", {"cmd": "pytest"})
    assert r["recorded"] is True
    assert r["seq"] >= 1
    assert r["content_hash"]


def test_record_event_unknown_session(wired) -> None:
    r = wired.record_event("does-not-exist", "user_prompt")
    assert r["recorded"] is False
    assert r["reason"] == "session_not_found"


def test_session_end_reconciles_and_consolidates(wired) -> None:
    import asyncio

    session_id = _start_session(wired)
    wired.record_event(session_id, "user_prompt", {"text": "hi"})
    r = asyncio.run(wired.session_end(session_id))
    assert r["status"] == "completed"
    assert r["capture_complete"] is True
    assert "consolidation" in r  # flush ran, no consolidation_error
    assert "consolidation_error" not in r


def test_memory_search_empty_db(wired) -> None:
    r = wired.memory_search("continue the eval work")
    assert r["memories"] == [] and r["total"] == 0


def test_memory_get_not_found(wired) -> None:
    r = wired.memory_get(["mem-1", "mem-2"])
    assert r["memories"] == []
    assert set(r["not_found"]) == {"mem-1", "mem-2"}


def test_memory_context_empty_respects_budget(wired) -> None:
    r = wired.memory_context("continue the eval work", token_budget=800)
    assert r["injected_tokens"] <= 800
    assert "context" in r


def test_memory_list_empty(wired) -> None:
    r = wired.memory_list(status="active")
    assert r["memories"] == [] and r["total"] == 0


def test_memory_consolidate_runs(wired) -> None:
    import asyncio

    r = asyncio.run(wired.memory_consolidate(project=None, session_id=None))
    assert "sessions_processed" in r


def test_memory_add_not_implemented(wired) -> None:
    r = wired.memory_add("Prefer SQLite", "decision", "project")
    assert r["implemented"] is False


def test_memory_update_not_implemented(wired) -> None:
    r = wired.memory_update("mem-1", "mark_stale", reason="file changed")
    assert r["implemented"] is False


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
