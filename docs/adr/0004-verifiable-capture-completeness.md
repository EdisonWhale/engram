# ADR 0004 — Verifiable capture completeness via per-session sequence + reconciliation

**Status:** Accepted

## Context

No surveyed competitor (claude-mem, cctrace, TMA1, agenttrace, claudewatch) *verifies* that capture
is complete — they all trust the JSONL is whole. `claudewatch` flags a `tool_use` with no matching
`tool_result` (an interrupted span) but cannot detect a dropped record. "Not lost" must be provable,
not assumed, and the guarantee differs per platform.

## Decision

Two distinct numbers, never conflated:

- **Capture completeness** — was every event recorded? Each event gets a monotonic per-session
  `seq`. At `session_end`, reconcile the range; a gap is provable evidence of a drop → mark the
  session `capture_incomplete` and exclude it from consolidation. A `pending` map flags
  `tool_use` without a matching `tool_result`.
- **Retrieval accuracy** — was the *right* memory injected? This is the eval-measured Recall@5 / MRR
  of spec §12, not a capture property. Never reported as a capture guarantee.

Completeness is guaranteed only for Claude Code (full transcript); Codex/Cursor are best-effort.

## Consequences

- "Not lost" has a concrete, defensible definition: authoritative full ingest + sequence
  reconciliation that *detects* gaps + replayable raw events — not a promise of zero loss.
- The sequence-reconciliation check is novel in this space and is a differentiator worth surfacing.
- Consolidation is lossy by design, so raw events stay append-only and replayable (ADR-adjacent:
  see CLAUDE.md invariants).
