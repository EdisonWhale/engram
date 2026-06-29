# WS-B — Consolidation (parallel after WS-0, worktree-isolated)

**Goal:** Turn raw events into session summaries, short-term task contexts, and long-term memory candidates. Spec §6, §10, §27.

**Depends on:** WS-0 (`EventStore`, `MemoryStore`, models). Use a fake EventStore seeded with fixtures until WS-A lands.

## Deliverables

- **Session summary** (spec §10.1, §8.1 `session_end`) — request / completed / learned / next_steps / files_read / files_modified.
- **Short-term task_contexts** (§6.1) — active-task state, TTL, tied to one project+task. Clearable on completion.
- **Long-term promotion** (§6.3, §10.3) — promotion policy (standing instruction / correction / confirmed decision / verified fact / stable command / repeated failure). Set `origin` (`extracted` vs `synthesized`).
- **Two-phase pipeline (§27)** — fast write-time enrichment queue + lazy idle-time batch consolidation. Trigger on **idle OR write-count threshold** (don't let an always-busy agent starve consolidation — nram's gap).
- **Write-time dedup, NO LLM on write path (CLAUDE.md)** — (1) exact `content_hash`; (2) vector cosine band: `<0.15` duplicate (bump `access_count`, skip), `0.15–0.35` conflict (insert + flag), `>0.35` independent.
- **Update semantics (§10.4)** — duplicate / reinforce / supersede (`superseded_by` + forward pointer) / conflict (keep both, block default injection) / stale / delete (tombstone). Never hard-delete by default.
- **Provenance** — write `memory_sources` rows (episode→memory) for every promotion.
- **LLM use** — only here (summary, extraction, conflict explanation). Keep the LLM call site behind a small interface so it's swappable and mockable in tests.

## Acceptance criteria (test-first on the supersede/conflict state machine — CLAUDE.md)

- A confirmed decision from events becomes a long-term memory with provenance rows.
- A contradicting fact creates a conflict (both retained, injection-blocked), not a silent overwrite.
- Duplicate content does not create a second memory; `access_count` increments.
- Short-term context expires on TTL and on task completion.

## Review

`/code-review` + `silent-failure-hunter` (LLM/JSON-parse fallback paths).
