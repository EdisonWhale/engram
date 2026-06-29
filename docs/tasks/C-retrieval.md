# WS-C — Retrieval (parallel after WS-0, worktree-isolated)

**Goal:** Token-aware retrieval with the search → timeline → get → context workflow. Spec §8.2, §11, §27.

**Depends on:** WS-0 (`MemoryStore`, models). Seed a fake MemoryStore with fixture memories until WS-B lands.

## Deliverables

- **Progressive disclosure tools (§8.2, §11.2)**:
  - `memory_search` — compact rows (id, title, type, age, status, provenance summary). IDs/titles only, ~50–100 tokens each.
  - `memory_timeline` — chronological window around an anchor/query.
  - `memory_get` — full records by id.
  - `memory_context` — final prompt-ready context under `token_budget`, assembled by §11.3 priority (short-term task > decisions > project facts > preferences > failure patterns > old summaries). **Never inject unresolved conflicts by default.**
- **Candidate generation** — P0: FTS5/BM25 + metadata filters. Hybrid vector+RRF is **P1** (do not block P0 on embeddings). Leave a seam for the `VectorStore` interface.
- **Filtering (§11.1)** — project, file, type, status, agent, git_sha, branch, time range, privacy visibility.
- **Stale check at recall (§10.6)** — recompute `file_hash` for memories with a `file_path`; mark mismatches `stale`, demote/flag them.
- **Token budget packing** — fill greedily by rank up to `token_budget`; return what fits (knowing's pattern).

## Acceptance criteria

- `memory_search` returns compact rows that fit the documented token estimate.
- Exact-identifier query (function name / error code / file path) is recalled via BM25 — pure-vector would miss it (this is why FTS5 is P0).
- `memory_context` respects `token_budget` and never exceeds it.
- A memory whose underlying file changed is surfaced as `stale`.
- A conflicting memory is not injected by default.

## Review

`/code-review`. Hand WS-D the trace fields it needs (candidates, selected, ranking features, token usage).
