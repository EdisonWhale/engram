# Architecture Decision Records

Each ADR captures one significant, hard-to-reverse decision: its context, the choice, and the
consequences. Format is lightweight (Nygard / MADR). An ADR is immutable once `Accepted` — to
change a decision, add a new ADR that supersedes it rather than editing history.

These records exist so a future developer or AI agent understands *why* the code is the way it is,
without re-deriving it. They mirror the invariants in `../../CLAUDE.md` and the design in
`../engram-development-spec.md`.

| ADR | Decision | Status |
|-----|----------|--------|
| [0001](0001-sqlite-source-of-truth.md) | SQLite is the source of truth; Postgres deferred to P2 | Accepted |
| [0002](0002-transcript-jsonl-capture.md) | Official transcript JSONL is the authoritative capture source, not hooks | Accepted |
| [0003](0003-no-llm-on-write-path.md) | No LLM calls on the write/capture path | Accepted |
| [0004](0004-verifiable-capture-completeness.md) | Verifiable capture completeness via per-session sequence + reconciliation | Accepted |
| [0005](0005-changed-code-invalidation.md) | Changed-code invalidation via file content hash | Accepted |
| [0006](0006-stdio-mcp-no-web-framework.md) | stdio MCP transport; no web framework in P0/P1 | Accepted |
