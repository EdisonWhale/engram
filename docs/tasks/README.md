# Engram — Task Breakdown & Parallel Build Flow

Source of truth for *what* to build: `docs/engram-development-spec.md`. Project rules: `CLAUDE.md`.
This directory breaks P0 into workstreams sized for subagent execution.

## Flow: scaffold-first, then fan out

Do **not** parallelize from an empty repo. The modules share one data model (spec §9); the contract must be frozen first or parallel agents collide on schemas and layout.

```
WS-0  Scaffold & Contract   (1 agent, sequential, BLOCKING)   -> 00-scaffold.md
        │  freezes: pydantic models, SQLite DDL, storage interfaces, MCP skeleton
        ▼  after merge + `codegraph init`
   ┌──────────┬──────────┬──────────┐
  WS-A Capture  WS-B Consol  WS-C Retrieval   (3 agents, PARALLEL, worktree-isolated)
   └──────────┴────┬─────┴──────────┘
                   ▼
              WS-D Evals & Traces   (depends on WS-C)
                   ▼
        Integration + Review
```

## How to spawn the parallel batch (after WS-0 lands)

1. Merge WS-0, then run `codegraph init` at repo root so agents can navigate the frozen contract via `codegraph_explore`.
2. Spawn WS-A / WS-B / WS-C in one message, each with `isolation: "worktree"`, each pointed at its task file. They code only against the frozen interfaces (use fakes/stubs for the others).
3. Each agent's final message returns: files changed, acceptance-criteria checklist, open questions. Do **not** let them merge — they return diffs to the main agent.
4. Review every diff before merge: `/code-review` (bugs) + `silent-failure-hunter` subagent (swallowed errors — this project has many catch/fallback paths) + `/security-review` (privacy/secret handling). Then `ruff` + `pytest` must pass.
5. WS-D after WS-C merges.

## Review tooling reference

| Need | Tool |
|---|---|
| Bug review on a diff | `/code-review` |
| Swallowed errors / bad fallbacks | `silent-failure-hunter` subagent |
| Privacy / secret leakage | `/security-review` |
| Run the MCP server to verify | `/verify`, `/run` |
| Locate code in the frozen contract | `codegraph_explore` (after `codegraph init`) |

## Status

| WS | Title | Depends on | Status |
|----|-------|-----------|--------|
| 0 | Scaffold & Contract | — | not started |
| A | Capture | WS-0 | blocked |
| B | Consolidation | WS-0 | blocked |
| C | Retrieval | WS-0 | blocked |
| D | Evals & Traces | WS-0, WS-C | blocked |
