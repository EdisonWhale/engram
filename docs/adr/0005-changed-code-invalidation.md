# ADR 0005 — Changed-code invalidation via file content hash

**Status:** Accepted

## Context

Stale project facts mislead agents. No competitor combines note-style agent memory + code
provenance + automatic staleness on file change: `knowing` does code-file staleness (via
content-addressed Merkle trees + cryptographic proofs) but has no conversational memory; graymatter
and remindb have memory but no code-change invalidation. This combination is Engram's differentiator.

## Decision

A memory that references a file stores `file_path` + `file_hash = sha256(file_bytes)` at write time.
At recall time, recompute the current hash; if it differs, surface the memory as `stale` (warn, do
not hard-block). No background watcher is needed for the core case — it is write-time-store /
read-time-compare. An optional `--watch` mode uses OS file-events for real-time invalidation.
`engram verify` exits non-zero if any high-confidence memory is stale vs git HEAD (CI gate).

We deliberately use file-hash compare, not git diff or AST comparison: O(1) per memory, no parser,
language-agnostic. AST-granularity invalidation is reserved for later if function-level proves needed.

## Consequences

- Cheap, simple, always-available staleness without the Merkle/proof machinery of `knowing`.
- Granularity is whole-file: any change to a referenced file marks the memory stale, even if the
  relevant lines were untouched. Acceptable for v1; revisit with AST if false-stale rate is high.
- Requires `file_path`/`file_hash` columns on `memories` (already in spec §9.1) and a recall-time
  hash check (spec §10.6, §11 stage).
