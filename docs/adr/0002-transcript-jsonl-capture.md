# ADR 0002 — Official transcript JSONL is the authoritative capture source, not hooks

**Status:** Accepted

## Context

"Nothing is lost when handing off between agents" depends entirely on what the host platform
exposes. Two capture strategies exist in the wild:

- **Hook-driven** (`claude-mem`): registers PostToolUse etc. Evidence shows it misses subagent
  (`Task`) calls entirely — it does not register `SubagentStop` — and silently drops events when
  the worker times out. No reconciliation.
- **Transcript-driven** (`cctrace`, `TMA1`, `claudewatch`): tail the official transcript JSONL that
  Claude Code already writes append-only to `~/.claude/projects/<encoded-cwd>/*.jsonl`. It contains
  every user/assistant message, `tool_use`/`tool_result`, **subagent launch + result with
  AgentID/AgentType/token counts**, skill, permission, and hook record. These tools capture
  subagents correctly.

## Decision

The official transcript JSONL is the authoritative capture source. Engram tails it (watching the
*directory*, persisting byte offsets, storing `raw_ref_file`/`raw_ref_offset` per event). Hooks are
a **secondary** signal used only for real-time triggering and session boundaries
(SessionStart / Stop / PreCompact), never as the primary record. git operations are a subset of
`Bash` tool records and are covered for free.

## Consequences

- Subagent and git activity are captured without bespoke hook plumbing.
- Capture completeness is guaranteed only on platforms with a complete transcript (Claude Code).
  Codex/Cursor are explicitly best-effort (`capture_confidence != exact`). See ADR 0004.
- An earlier spec draft had hooks as primary; this reverses that. See spec §14.1, §26.
- Defensive details required: truncate tail reads at the last newline; watch the directory so a
  mid-run new session file is not missed; persist offset to resume after restart.
