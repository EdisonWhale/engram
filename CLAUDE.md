# Engram — Project Instructions

Cross-session memory MCP server for AI coding agents. Full design in `docs/engram-development-spec.md` — read it before non-trivial work. This file holds only project-specific invariants and stance; global workflow/testing/tooling rules still apply and are not repeated here.

## Architectural invariants (do not "optimize" these away)

These look changeable but are load-bearing. Changing one is an architecture decision, not a refactor — flag it, don't just do it.

- **SQLite is the source of truth.** FTS5 and any vector index are rebuildable derived state. Never make durable memory owned by the vector index.
- **Capture reads the official transcript JSONL, not hooks.** Claude Code writes a complete append-only transcript to `~/.claude/projects/<encoded-cwd>/*.jsonl` (incl. tool calls, git, subagent/Task launch+result). Tail that as authoritative. Hooks are only a secondary signal for real-time triggering + session boundaries. Do not "simplify" capture back to a hook-only design — it silently drops subagents. (Spec §14.1, §26.)
- **No LLM calls on the write/capture path.** LLMs run only in consolidation (summary, extraction, conflict explanation). Write-time dedup is content-hash + vector distance, never an LLM judge.
- **Events are append-only.** Never update/overwrite an event row. Memory edits go through supersede/stale/tombstone (`superseded_by`), never hard delete by default. (Spec §10.4.)
- **Consolidation is lossy by design**, so raw events must stay replayable. Don't drop raw events after consolidating.
- **Capture completeness is verifiable, per-platform.** Per-session monotonic `seq` + reconcile at session_end; a gap is a provable drop. Completeness is guaranteed only for Claude Code; Codex/Cursor are explicitly best-effort. (Spec §26.)
- **Storage is behind an interface.** SQLite is the only P0 implementation; Postgres is a P2 swap. This interface is the one abstraction worth having upfront — see style below.

## No web framework

P0/P1 is a single **stdio** MCP process + in-process asyncio tasks + SQLite. MCP over stdio speaks JSON-RPC over stdin/stdout — it is **not** HTTP. Do not add FastAPI/Flask. Use the official MCP Python SDK (`mcp`, `FastMCP` — note this is NOT FastAPI). The consolidation worker, JSONL tailer, and eval runner are async tasks / CLI commands, not servers. An HTTP layer is only considered at P2, and even then the MCP SDK's own Starlette HTTP/SSE transport likely suffices.

## Code style

Follow the `karpathy-guidelines` skill (surgical changes, surface assumptions, verifiable success criteria) and deep-module design (`codebase-design` skill).

- **Deep modules, not more components.** Resist classitis and premature abstraction — the most common failure mode in Python AI projects. A module whose interface is as large as its implementation (pass-through, getter/setter shells) is shallow; merge it.
- **The natural seams are already in the spec**: `EventStore`, `MemoryStore`, `VectorStore`, `Retrieval`, `Consolidation`, and the MCP tool layer. Module boundaries go there; do not add layers inside them.
- **Only the storage layer gets an interface upfront** (for the P2 Postgres swap). Everything else: write the concrete implementation first; extract an interface when a second implementation actually appears (YAGNI). E.g. no abstract base class for the reranker while there is one BM25 impl.
- Prefer plain functions and `@dataclass`/pydantic models over class hierarchies. Reach for a class only when there is real state to encapsulate.

## Toolchain

- **Python 3.11+**, full type hints.
- **pydantic** for schemas (MCP tool params/returns need validation).
- **ruff** for lint + format. **pytest** for tests.
- `src/` layout. CLI entrypoint: `engram` (e.g. `engram mcp`, `engram eval`, `engram doctor`).

## Parallel build: commit & permission policy

- **Subagents return diffs; they do not commit or merge.** The main agent is the integration point.
- **Auto commit + push is the main agent's job, gated on verification.** After a subagent's work passes review (`/code-review` + `silent-failure-hunter`, `/security-review` where privacy/secret code changed) AND `ruff check` + `pytest` are green, the main agent may commit and push **without asking** — but to a **per-workstream feature branch** (e.g. `ws-a-capture`), then open a PR. **Never auto-commit or push to `main`.** Verification is the gate; if review or tests fail, do not commit.
- **Spawn parallel coding subagents in `acceptEdits` mode** so in-project file edits don't prompt, but genuinely dangerous ops (rm, network, non-allowlisted commands) still stop. Do **not** use `bypassPermissions` — it auto-approves destructive commands.
- The allowlist in `.claude/settings.json` (ruff/pytest/git/gh/codegraph) is what lets commit+push proceed without prompting. Adjust it there.

## Testing (project-specific additions to the global policy)

- **Retrieval is evaluated with replay evals, not unit tests** — gold memory IDs per query; track Recall@5 / MRR / stale_injection_rate. (Spec §12.)
- **Capture completeness needs a regression test**: a recorded transcript fixture in -> every tool/subagent event reconciled out, `seq` with no gaps.
- Force test-first on: the parser/state-machine that reads transcript JSONL, the supersede/conflict state machine, and the eval metric math.
