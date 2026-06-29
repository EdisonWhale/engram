# Capture Schema — Claude Code transcript JSONL → Engram events

Contract for WS-A. Pins the record shapes Engram reads from the official transcript and how each maps
to an `events` row (spec §9.1). Authoritative source per ADR 0002. Shapes below were observed from
the transcript-reading tools (cctrace, claudewatch) — **verify against a real fixture before relying
on field names**, since Claude Code's format is not a published spec and can change between versions.

## Location & discovery

- Directory: `~/.claude/projects/<encodeClaudeProjectPath(cwd)>/`
- `encodeClaudeProjectPath(cwd)`: replace `/` → `-` and `.` → `-`.
- A session is one `*.jsonl` file. **Watch the directory**, not a single file — a new session may
  create a new file mid-run. Persist a byte offset per file; truncate reads at the last `\n`.

## Record envelope (one JSON object per line)

```jsonc
{
  "type": "user" | "assistant" | "tool_use" | "tool_result"
        | "subagent" | "skill" | "permission" | "hook",
  "uuid": "string",            // record id when present
  "timestamp": "RFC3339",
  "message": { "role": "...", "content": [ <content blocks> ] },  // for user/assistant turns
  "toolUseResult": { ... }     // present on tool/subagent result records
}
```

Content blocks inside `message.content` carry `tool_use` (with `id`, `name`, `input`) and
`tool_result` (with `tool_use_id`, `content`) — correlate a call to its result by `tool_use_id`.

## Subagent (Task tool) shapes — the records hooks miss

Input (the launch):

```jsonc
{ "description": "string", "isolation": "string", "model": "string",
  "prompt": "string", "subagentType": "string" }
```

Result:

```jsonc
{ "status": "string", "agentId": "string", "agentType": "string",
  "totalDurationMs": 0, "totalTokens": 0.0,
  "totalToolUseCount": 0.0, "toolStats": { } , "content": <raw> }
```

## Mapping to `events`

| Transcript record | `events.event_type` | `source_type` | Notes |
|---|---|---|---|
| `user` message | `user_prompt` | `transcript` | |
| `assistant` turn | `assistant_summary` | `transcript` | store a summary, not full text, if large |
| `tool_use` (name=`Bash`, git cmd) | `git` | `transcript` | git is a subset of Bash calls |
| `tool_use` (name=`Read`/`Edit`/`Write`) | `file_read` / `file_edit` | `transcript` | |
| `tool_use` (other) | `tool_call` | `transcript` | |
| `tool_result` | `tool_result` | `transcript` | correlate via `tool_use_id` |
| `tool_use` (name=`Task`) + result | `subagent` | `transcript` | capture agentType + token/tool stats |
| `skill` | `skill` | `transcript` | |
| `permission` | `permission` | `transcript` | |

Every row also gets: `seq` (monotonic per session), `raw_ref_file` + `raw_ref_offset` (byte offset),
`content_hash` (dedup), `capture_confidence = exact` (read straight from transcript).

## Non-Claude-Code platforms

Codex transcripts have a different/weaker format; Cursor exposes little. Adapters normalize what they
can into the same `events` shape but set `capture_confidence = likely | unknown` and are **not**
guaranteed complete (ADR 0004). Do not block P0 on them.
