# WS-0 — Scaffold & Contract (BLOCKING, do first, 1 agent)

**Goal:** Freeze the contract every other workstream depends on. No business logic — just structure, schemas, interfaces, and runnable skeletons.

**Depends on:** nothing. **Blocks:** A, B, C, D.

## Deliverables

1. **Project setup**
   - `pyproject.toml`: Python 3.11+, deps `mcp`, `pydantic`; dev deps `ruff`, `pytest`. Configure ruff (lint+format) and pytest.
   - `src/` layout, package `engram`. CLI entrypoint `engram` with subcommands stubbed: `engram mcp`, `engram eval`, `engram doctor`.
   - `git init`, `.gitignore` (`.venv`, `__pycache__`, `*.db`, `.DS_Store`).

2. **Data model (the contract)** — `src/engram/models.py`
   - pydantic models for every table in spec §9.1 (projects, agent_sessions, events, task_contexts, memories, session_summaries, memory_sources, retrieval_traces, eval_cases, eval_runs). Include the fields added in this revision: events `seq`/`raw_ref_file`/`raw_ref_offset`/`source_seq`/`capture_confidence`; memories `origin`/`content_hash`/`access_count`/`file_path`/`file_hash`.

3. **SQLite DDL + migrations** — `src/engram/db/migrations/`
   - DDL for all tables. WAL mode. FTS5 virtual table + sync triggers for memory content (BM25). Indexes on `(project_id, session_id)`, `events.seq`, `memories.status`.

4. **Storage interfaces** — `src/engram/store/`
   - Abstract `EventStore`, `MemoryStore`, `VectorStore` (Protocol or ABC). SQLite implementations of `EventStore`/`MemoryStore`; `VectorStore` interface only (no impl yet — P1). This is the *one* abstraction allowed upfront (CLAUDE.md).

5. **MCP server skeleton** — `src/engram/mcp/server.py`
   - `FastMCP("engram")` over **stdio** (no HTTP, no FastAPI — CLAUDE.md). Register every tool from spec §8 as a **stub** that validates params and returns a typed placeholder. Tools: session_start, record_event, session_end, memory_search, memory_timeline, memory_get, memory_context, memory_add, memory_update, memory_list, memory_consolidate.

## Acceptance criteria

- `engram --help`, `engram mcp` (starts and responds to MCP `initialize` over stdio), `engram doctor` all run.
- `pytest` green (smoke tests: migrations apply, every model round-trips, every MCP tool stub is registered and validates input).
- `ruff check` clean.
- A second implementer can build A/B/C against these interfaces without reading each other's code.

## After merge

Run `codegraph init` at repo root. Then unblock A/B/C.
