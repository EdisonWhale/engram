# WS-D — Evals & Traces (after WS-C)

**Goal:** Make retrieval quality measurable and regressions diagnosable. This is Engram's differentiator vs claude-mem/stash. Spec §12, §27.

**Depends on:** WS-0, WS-C (retrieval must exist to replay against). Can start against a fake retriever.

## Deliverables

- **Eval case format (§12.1)** — query, project_id, expected_memory_ids, expected_memory_types, must_not_include_ids, tags. Loader for a gold set (JSON).
- **Replay runner (§12.2)** — run each case through retrieval, compute:
  - `Recall@5` (expected memories in top 5)
  - `MRR` (rank of first relevant)
  - `stale_injection_rate` (uses `origin`/`status` — stale or synthesized injected / total injected)
  - `conflict_injection_rate`
  - `avg_injected_tokens`
  - `abstain_rate` (correctly returns nothing)
  - Persist to `eval_runs`.
- **Retrieval traces (§12.3)** — per retrieval: query, candidate ids, selected ids, scores, filters, reranker features, stale/conflict decisions, token budget, final context. Persist to `retrieval_traces`. JSON output for inspection.
- **CLI** — `engram eval --gold <path>` prints the metric table and diffs against the previous run (regression detection).

## Acceptance criteria (test-first on the metric math — CLAUDE.md)

- Recall@5 / MRR computed correctly against hand-verified fixtures (known-answer tests).
- A trace explains, for one query, exactly why each memory was or wasn't injected.
- Injecting a known-stale memory raises `stale_injection_rate` as expected.
- `engram eval` exits non-zero on a metric regression beyond a threshold (CI gate).

## Review

`/code-review`. The metric math is high-risk — verify against worked examples, not just "it runs".
