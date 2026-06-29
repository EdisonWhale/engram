# ADR 0006 — stdio MCP transport; no web framework in P0/P1

**Status:** Accepted

## Context

MCP over stdio speaks JSON-RPC over stdin/stdout — it is not HTTP. A common mistake is to reach for
FastAPI/Flask on seeing the word "server." The other moving parts (consolidation worker, JSONL
tailer, eval runner) are async tasks / CLI commands, not web servers.

## Decision

P0/P1 is a single stdio MCP process + in-process asyncio tasks + SQLite, built on the official MCP
Python SDK (`mcp`, `FastMCP` — note: not FastAPI). No FastAPI/Flask dependency. An HTTP layer is
considered only at P2, and even then the MCP SDK's own Starlette-based HTTP/SSE transport likely
suffices; FastAPI enters only if a separate REST API or dashboard (unrelated to the MCP protocol) is
built.

## Consequences

- Zero web dependencies in P0; smaller surface, simpler deploy (`pip install` / `uv`).
- Broad agent compatibility (stdio is the most widely supported MCP transport).
- Remote access, if ever needed, rides the SDK's transport rather than a hand-rolled HTTP server.
