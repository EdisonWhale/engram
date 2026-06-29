# Engram

Cross-session memory MCP server for AI coding agents (Claude Code / Codex / Cursor).

Engram captures raw session events, consolidates them into short-term task handoff and
long-term project knowledge, and retrieves only relevant memories through token-aware
`search → timeline → get` workflows — with replay evals, provenance, and traces.

- **Design spec:** [`docs/engram-development-spec.md`](docs/engram-development-spec.md)
- **Project rules for agents:** [`CLAUDE.md`](CLAUDE.md)
- **Build plan / task breakdown:** [`docs/tasks/`](docs/tasks/)

Status: early development (P0 scaffold not yet started).
