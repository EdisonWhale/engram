# WS-A — Capture (parallel after WS-0, worktree-isolated)

**Goal:** Ingest agent sessions into the append-only event store, with provable capture completeness. Spec §5.2, §14.1, §26.

**Depends on:** WS-0 (`EventStore`, models). **Codes against:** frozen interfaces only.

## Pin before coding (both resolved — verify against a real fixture)

1. **Transcript JSONL record schema** — pinned in [`../capture-schema.md`](../capture-schema.md). Field names are observed, not from a published spec; confirm against a real transcript fixture before relying on them.
2. **Dual session id reconciliation rule** — pinned in spec §9.2 (resolve/mint `memory_thread_id` from active `task_contexts` at `session_start`).

## Deliverables

- **Transcript tailer** — tail `~/.claude/projects/<encodeClaudeProjectPath(cwd)>/*.jsonl` (`/`→`-`, `.`→`-`). Watch the **directory**, not one file. Persist byte offset; truncate reads at the last `\n` (avoid half-written lines). Store `raw_ref_file`/`raw_ref_offset` on every event. Treat the JSONL as authoritative; **do not** build a hook-only path.
- **Ingest API** — `session_start`, `record_event`, `session_end` (spec §8.1). Dedup by `content_hash` (`ON CONFLICT DO NOTHING`). Assign monotonic per-session `seq`.
- **Completeness verification (the differentiator, §26)** — at `session_end`, reconcile the `seq` range; any gap → mark session `capture_incomplete` and exclude from consolidation. A `pending` map flags `tool_use` with no matching `tool_result` (interrupted span).
- **Boundaries** — Claude Code is the only platform guaranteed complete; Codex/Cursor adapters are best-effort, labeled `capture_confidence != exact`.

## Acceptance criteria (test-first on the parser/state-machine — CLAUDE.md)

- Regression test: a recorded transcript fixture in → every tool call AND subagent invocation appears as an event; `seq` has no gaps.
- A simulated dropped line is **detected** (gap reported), not silently passed.
- A `Task` (subagent) call yields a captured event with agentType + token metadata.

## Review

`silent-failure-hunter` (the tailer has many I/O/parse error paths), then `/code-review`.
