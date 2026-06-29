# ADR 0003 — No LLM calls on the write/capture path

**Status:** Accepted

## Context

Some memory systems (`nram`) run an LLM "ingestion judge" (ADD/UPDATE/DELETE/NONE) on every write.
Coding agents are chatty and write a lot, so per-write LLM calls are slow and expensive and put a
network dependency on the hot path. Capture must be fast and reliable.

## Decision

The write/capture path runs no LLM. Event ingestion and write-time dedup are deterministic:
exact `content_hash` match first, then a vector cosine-distance band (`<0.15` duplicate,
`0.15–0.35` conflict-flag, `>0.35` independent). LLMs are used **only** in consolidation
(session summary, candidate extraction, conflict explanation), which runs lazily at session end /
idle / on demand — never blocking a write.

## Consequences

- Capture stays fast, deterministic, and offline-capable; no token cost per event.
- Consolidation is the only LLM cost center and can be batched, rate-limited, and made swappable
  behind a small interface for testing.
- Write-time dedup quality is bounded by embedding similarity, not LLM judgment — acceptable, since
  the consolidation pass refines later.
