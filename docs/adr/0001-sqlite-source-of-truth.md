# ADR 0001 — SQLite as source of truth; Postgres deferred to P2

**Status:** Accepted

## Context

Engram is a local-first, per-developer MCP memory server. Many agent-memory projects use
Postgres + pgvector, which raised the question of whether to follow suit. Evidence from
competitors: `stash` is Postgres-only *because* it inlines pgvector's `<=>` operator into SQL
WHERE/JOIN during consolidation and needs multi-table transactional consolidation — neither
applies to Engram. `nram` runs the full pipeline (FTS5 + in-process pure-Go HNSW) on SQLite by
default while offering Postgres behind an interface, proving SQLite scales to this workload. The
Postgres pattern comes from hosted multi-tenant SaaS (concurrency, RBAC, horizontal scale).

## Decision

SQLite is the single source of truth. FTS5 and any vector index are rebuildable derived state.
Storage sits behind an `EventStore` / `MemoryStore` / `VectorStore` interface so Postgres is a P2
swap, not a rewrite. Vectors use an in-process ANN index (sqlite-vec / HNSW), not pgvector.

## Consequences

- Zero-config, embedded, no Docker — correct for a per-agent local tool, and a defensible
  engineering judgment on a resume (vs cargo-culting Postgres).
- Vector ops are not SQL-integrated: dedup/recall is `embed → ANN.search() → SQL WHERE id IN (...)`,
  one extra in-process round-trip. Negligible at 10k–100k memories.
- The one real risk: keeping the ANN index consistent with SQLite under concurrent writes. Mitigate
  with WAL mode + serialized index updates.
- Derived indexes can be dropped and rebuilt; partial sync failure never corrupts durable memory.
