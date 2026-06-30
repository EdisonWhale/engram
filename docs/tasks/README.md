# Engram — Task Breakdown & Parallel Build Flow

Source of truth for *what* to build: `docs/engram-development-spec.md`. Project rules: `CLAUDE.md`.
Where code goes and what to call it: [`docs/conventions.md`](../conventions.md) (package home per workstream).
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
4. Review every diff before merge — **match review intensity to risk, default to the cheapest tier** (see "Review strategy" below). Then `ruff` + `pytest` must pass.
5. WS-D after WS-C merges.

## Review strategy (cost vs accuracy)

The cost driver of a review is *how many independent agent contexts re-read the
same code*, not how thorough it is. A single agent reading a large diff
incrementally (cache-warm) is far cheaper than a fan-out where every agent
cold-reads the whole diff. So **start at the cheapest tier and escalate only on
risk**:

| Tier | How | When |
|---|---|---|
| **0 (default)** | Main agent reads the diff hunks + enclosing functions, reasons once. Zero subagents. Scope to risk-bearing `src/` files; skip `uv.lock`/generated/tests. | ~90% of changes, incl. routine integration reviews |
| **1** | Main agent + **one** focused subagent for a single risk axis (`silent-failure-hunter` for swallowed errors; `security-auditor` for privacy/secret). | A specific risk dimension worth an independent pass |
| **2** | 2-4 subagents partitioned **by module/file** (disjoint surface → linear cost), not by "angle". | Large diff spanning independent subsystems |
| **3** | The adversarial `/code-review high/xhigh/max` workflow panel (8 finders + a verifier per finding). | Pre-release audit, money/auth/migration code only |

Rules: **never trigger the Tier-3 panel for a routine review** — it fans out to
~40+ agents. When you do fan out, cap at 1-3 subagents and **run review/finder
subagents on Sonnet, not Opus** (review is read+match+cite; Sonnet's accuracy
holds at ~1/10 the cost, and subagents inherit the session model).

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
| 0 | Scaffold & Contract | — | done (merged) |
| A | Capture | WS-0 | done — integrated + server-wired |
| B | Consolidation | WS-0 | done — integrated + server-wired |
| C | Retrieval | WS-0 | done — integrated + server-wired |
| D | Evals & Traces | WS-0, WS-C | done — PR open against `p0-integration` |

**Integration notes (P0):** A/B/C built in parallel worktrees (Sonnet 4.6), merged onto `p0-integration`,
MCP `server.py` wired to all three. Contract extensions added during integration:
`MemoryStore.search_memories_fts` (WS-C, BM25), `MemoryStore.list_task_contexts` + `update_task_context`
(closes WS-B's task-context gap). Deferred / needs a spec decision before building:
`memory_add` / `memory_update` tool bodies (no workstream specs manual create/update; validation-only stubs),
the consolidation idle background loop (needs FastMCP lifespan wiring — manual + session_end flush works now),
and `ScoredVectorStore.search_with_scores` staying consolidation-owned until P1 embeddings land.

**WS-D notes:** closed a gap between the frozen WS-0 models and spec §12 — `EvalCase` was missing
`expected_memory_types`/`must_not_include_ids` and `EvalRun` was missing `conflict_injection_rate`/
`abstain_rate`; both extended via migration `002_eval_metrics.sql`. `engram eval --gold <path>` replays
a gold set, persists one `RetrievalTrace` + one `EvalRun` per run, and exits non-zero on a >0.05
recall_at_5/mrr regression vs. the previous run.
