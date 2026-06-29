# Engram Development Spec

## 1. Project Summary

**Name:** Engram: Cross-Session Memory MCP for AI Coding Agents

**Primary language:** Python

**Target users:** Developers using AI coding agents such as Claude Code, Codex, Cursor, and local Ollama-based coding tools.

**One-line description:**

Engram is a Python MCP memory server that gives AI coding agents persistent cross-session memory by capturing raw session events, consolidating them into short-term task handoff and long-term project knowledge, and retrieving only relevant memories through token-aware search, timeline, and fetch workflows with evals and provenance.

## 2. Problem Statement

Modern AI coding agents are strong at local reasoning inside a context window, but weak at durable continuity.

The main pain points are:

1. **Session amnesia**
   Each new session starts nearly from zero. The agent repeatedly re-reads the repo, asks for the same background, and rediscovers previous decisions.

2. **Poor cross-agent handoff**
   A user may start work in Claude Code, continue in Codex, then ask Cursor to finish. Today, task state, failed commands, changed files, and decisions do not transfer reliably.

3. **Lost implementation intent**
   Git diffs show what changed, but not why. Coding agents lose the reasoning behind a partial change, a rejected approach, or a chosen tradeoff.

4. **Prompt bloat**
   Users and agents paste repeated context into every new session. This burns tokens and still misses the most relevant prior facts.

5. **Unsafe memory injection**
   Naively injecting top-k memories can introduce stale, conflicting, or irrelevant context. Bad memory can make the agent worse than no memory.

6. **Lack of evals**
   Many memory tools provide `remember` and `recall`, but do not prove whether retrieval is accurate. AI engineering roles increasingly expect evaluation, observability, and reliability rather than only feature demos.

## 3. Goal

Engram should be a **memory reliability layer for coding agents**, not a generic chatbot memory database.

The core goal is:

```text
capture events -> consolidate useful memory -> retrieve with evals -> inject safely
```

Engram should answer:

- What was this task about?
- What did the previous agent already try?
- Which files were changed and why?
- What decisions were made?
- What preferences or standing instructions should the agent follow?
- Which memories are fresh, proven, relevant, and safe to inject?
- How do we know retrieval quality is improving or regressing?

## 4. Non-Goals

First version should deliberately avoid:

- A full SaaS product.
- Complex multi-tenant auth.
- Slack, Telegram, Discord, or notification integrations.
- A large dashboard.
- Full knowledge graph UI.
- Supporting every agent platform at once.
- Treating memory as a generic document RAG system.
- Storing every message forever as long-term memory.

These are product expansion paths, but they dilute the initial resume and engineering value.

## 5. Target Workflow

### 5.1 Session Start

When an agent starts in a project, Engram should:

1. Identify project root, branch, git SHA, and agent platform.
2. Create or resume an agent session.
3. Retrieve short-term task handoff if active work exists.
4. Retrieve long-term project/user memories relevant to the current prompt.
5. Return token-budgeted context to the agent.

Example injected context:

```text
Engram context:
- Active task: implement replay evals for memory retrieval.
- Last session changed src/evals/runner.py and docs/evals.md.
- Decision: SQLite is source of truth; vector index is derived state.
- User preference: for debugging tasks, analyze root cause before editing.
- Next step: finish MRR calculation and run tests/evals/test_runner.py.
```

### 5.2 During Work

Engram records raw events:

- User prompts.
- Tool calls.
- File reads/writes.
- Git diff summaries.
- Test commands and results.
- Failed attempts.
- Agent decisions.
- User corrections.
- Explicit preferences.

The raw events are append-only. They are not all promoted to memory.

### 5.3 Session End

At the end of a session, Engram should:

1. Write a compact session summary.
2. Create or update short-term handoff for unfinished work.
3. Queue consolidation for long-term candidates.
4. Mark completed task contexts as closed.
5. Record provenance for memories generated from this session.

### 5.4 Future Session

On the next session, from the same or another agent, Engram should:

1. Retrieve active short-term handoff.
2. Retrieve relevant long-term decisions, project facts, and preferences.
3. Avoid stale or conflicting memories.
4. Provide citations/provenance so the agent can inspect the source if needed.

## 6. Short-Term vs Long-Term Memory

### 6.1 Short-Term Memory

Short-term memory answers:

> What does the next agent need to continue the current task?

It is active-task state. It should be temporary, scoped, and replaceable.

Examples:

- Current task goal.
- Changed files.
- Current git diff intent.
- Failed command and error.
- Next step.
- Branch and issue context.
- Current unresolved blocker.

Example:

```text
Task context:
The retrieval eval runner is half implemented. Schema exists in src/evals/schema.py.
Next step is to compute Recall@5 and MRR from expected_memory_ids.
Last failure: tests/evals/test_runner.py failed because the gold set fixture had no expected ids.
```

Properties:

- Has TTL.
- Tied to one project and one active task.
- Can be cleared when task completes.
- Can be compacted into a session summary.
- Should not pollute long-term retrieval.

### 6.2 Long-Term Memory

Long-term memory answers:

> What should future agents know across tasks and sessions?

It is durable project/user knowledge.

Examples:

- User preferences.
- Project facts.
- Architecture decisions.
- Stable commands.
- Repeated failure patterns.
- Confirmed constraints.
- Long-lived implementation decisions.

Example:

```text
Decision:
Engram uses SQLite as source of truth and treats FTS/vector indexes as rebuildable derived state.
Reason: event replay, backfill, and provenance are simpler when durable memory is not owned by the vector index.
```

Properties:

- Durable across sessions.
- Has provenance.
- Has confidence.
- May be superseded.
- May be marked stale or conflicting.
- Should be selected by durability, reuse value, stability, and confidence.

### 6.3 Promotion Rules

Do not promote memory only because it appears in multiple sessions. Repetition is only one signal.

Promote to long-term when:

- User gives a standing instruction.
- User corrects agent behavior.
- A technical decision is confirmed.
- A project fact is verified from code/config/tests.
- A command or workflow is stable.
- A failure pattern repeats and has a known fix.
- A session summary contains a durable project insight.

Keep short-term when:

- It is needed only to resume the current task.
- It depends on the current branch or partial diff.
- It is a one-off failure.
- It is a temporary TODO.
- It is likely to expire soon.

Do not store when:

- It is routine narration.
- It is low-value chat.
- It is public knowledge.
- It is unverified speculation.
- It contains private/secret content.
- It is redundant with an existing active memory.

## 7. Architecture

```text
Claude Code / Codex / Cursor / Ollama agent
        |
        | hooks, MCP tools, or CLI adapters
        v
Engram MCP Server
        |
        +-- Agent Adapter Layer
        |     normalize Claude Code, Codex, Cursor, and local-agent events
        |
        +-- Event Ingest API
        |     session_start, record_event, session_end
        |
        +-- Source-of-Truth Store
        |     SQLite for local-first v1; optional Postgres later
        |
        +-- Consolidation Worker
        |     raw events -> task contexts, facts, decisions, preferences
        |
        +-- Retrieval Indexes
        |     SQLite FTS/BM25 + vector index + metadata filters
        |
        +-- Context Assembler
        |     search -> timeline -> get -> token-budgeted context
        |
        +-- Eval and Observability
              replay evals, Recall@5, MRR, traces, stale injection rate
```

### 7.1 Core Components

**MCP Server**

- Exposes memory tools to agents.
- Uses stdio first because it is broadly supported by coding agents.
- Optional HTTP/SSE transport later.

**Agent Adapter Layer**

- Normalizes platform-specific events into Engram's canonical event schema.
- Initial adapters:
  - Claude Code.
  - Codex.
  - Cursor.
  - Generic MCP/manual client.

**Event Store**

- Append-only source of truth.
- Stores raw events before any memory extraction.
- Enables replay, debugging, and eval dataset creation.

**Consolidation Worker**

- Converts raw events into structured memories.
- Runs at session end, on schedule, or manually.
- Handles deduplication, stale detection, conflict detection, and promotion.

**Retrieval Engine**

- Combines lexical search, vector search, metadata filters, and reranking.
- Supports project, file, agent, branch, git SHA, task, time, memory type, and status filters.

**Context Assembler**

- Builds prompt-ready memory context under a token budget.
- Uses progressive disclosure rather than full-memory dump.

**Eval Runner**

- Replays tasks against gold memory labels.
- Measures Recall@5, MRR, stale injection rate, and token budget.

**Trace Logger**

- Records why a memory was retrieved or injected.
- Supports debugging rank regressions and stale/conflicting context.

## 8. MCP Tool Surface

First version should keep the tool surface small and explicit.

### 8.1 Session Tools

```text
session_start(project_path, agent, prompt, git_sha, branch)
```

Creates or resumes a session and returns initial memory context.

```text
record_event(session_id, event_type, payload)
```

Records normalized raw events such as tool calls, file edits, test results, decisions, and user corrections.

```text
session_end(session_id, summary_hint=None)
```

Closes the session, writes a summary, and queues consolidation.

### 8.2 Memory Retrieval Tools

Engram should use a 3-layer retrieval workflow inspired by claude-mem's `search -> timeline -> get_observations` pattern.

```text
memory_search(query, project=None, file=None, type=None, limit=10)
```

Returns a compact index of candidate memories with IDs, titles, type, age, status, and provenance summary.

```text
memory_timeline(anchor_id=None, query=None, before=3, after=3)
```

Returns chronological context around a memory or query match.

```text
memory_get(ids)
```

Fetches full memory records by ID. Agents should only call this after filtering candidates.

```text
memory_context(query, token_budget=1200)
```

Returns final prompt-ready context assembled from short-term and long-term memories.

### 8.3 Memory Management Tools

```text
memory_add(content, type, scope, project=None, metadata=None)
```

Manual memory insert for confirmed facts/preferences/decisions.

```text
memory_update(memory_id, operation, content=None, reason=None)
```

Supports supersede, mark_stale, resolve_conflict, delete, and reinforce.

```text
memory_list(project=None, type=None, status=None)
```

Lists memories for inspection.

```text
memory_consolidate(project=None, session_id=None)
```

Runs consolidation manually.

## 9. Data Model

Engram should use SQLite for v1 because it is local-first, easy to inspect, and enough for a resume-grade project. Postgres with pgvector can be a later deployment mode.

### 9.1 Tables

```text
projects
  id
  root_path
  name
  repo_url
  created_at
  updated_at

agent_sessions
  id
  project_id
  external_session_id
  memory_thread_id
  agent
  branch
  git_sha
  status: active | completed | failed
  started_at
  ended_at

events
  id
  project_id
  session_id
  seq                      # monotonic per-session sequence number; gaps = provable capture loss (see §26)
  source_type: hook | mcp | cli | api | transcript
  source_seq               # original line/record index in the source JSONL transcript
  raw_ref_file             # absolute path of the source transcript file
  raw_ref_offset           # byte offset of this record in raw_ref_file (deterministic replay; from cctrace)
  capture_confidence: exact | likely | unknown   # exact = read from official transcript; likely = heuristically correlated
  event_type
  payload_json
  content_hash
  occurred_at
  created_at

task_contexts
  id
  project_id
  session_id
  task_key
  title
  content
  changed_files_json
  next_steps_json
  status: active | completed | expired
  ttl_until
  source_event_ids_json
  created_at
  updated_at

memories
  id
  project_id
  scope: user | project | session
  type: preference | decision | project_fact | failure_pattern | command | constraint
  origin: user | extracted | synthesized   # synthesized = produced by consolidation; powers stale_injection_rate (from nram `origin`)
  title
  content
  content_hash             # exact-match write-time dedup before any embedding (from nram migration 000018)
  status: active | stale | superseded | conflict | deleted
  confidence
  valid_from
  valid_until
  last_seen_at
  access_count             # recall-time reinforcement signal for decay (from graymatter/nram)
  file_path                # file this memory is about, if any
  file_hash                # SHA-256 of file_path bytes at write time; mismatch at recall => stale (from knowing, see §10.6)
  supersedes_memory_id
  source_event_ids_json
  metadata_json
  created_at
  updated_at

session_summaries
  id
  project_id
  session_id
  request
  completed
  learned
  next_steps
  files_read_json
  files_modified_json
  source_event_ids_json
  created_at

memory_sources
  id
  memory_id
  source_type: event | session_summary | manual | import
  source_id
  quote_or_summary
  created_at

retrieval_traces
  id
  query
  project_id
  selected_memory_ids_json
  candidate_memory_ids_json
  ranking_features_json
  token_budget
  injected_tokens
  outcome_label
  created_at

eval_cases
  id
  query
  project_id
  expected_memory_ids_json
  expected_behavior
  tags_json
  created_at

eval_runs
  id
  run_name
  recall_at_5
  mrr
  stale_injection_rate
  avg_injected_tokens
  created_at
```

### 9.2 Dual Session ID Pattern

Use two session identifiers:

- `external_session_id`: ID from Claude Code, Codex, Cursor, or another agent.
- `memory_thread_id`: stable cross-session memory thread for the project/task.

This separates the platform session from the durable memory timeline.

### 9.3 Source of Truth Rule

SQLite is the source of truth. FTS and vector indexes are derived state.

Implications:

- Indexes can be deleted and rebuilt.
- Backfill is possible.
- Partial sync failures should not corrupt durable memory.
- Memory provenance remains inspectable even if vector retrieval changes.

## 10. Memory Lifecycle

### 10.1 Capture

Every useful signal starts as an event:

- User prompt.
- Agent response summary.
- Tool call.
- File edit.
- Git diff.
- Test result.
- Error output.
- User correction.
- Explicit decision.

Events are append-only and content-hashed for deduplication.

### 10.2 Classify

Consolidation classifies event groups into:

- Ignore: low-value or private.
- Short-term task context.
- Long-term memory candidate.
- Eval candidate.
- Conflict candidate.

### 10.3 Promote

Promote to long-term only if:

- It is durable.
- It is likely reusable.
- It has enough evidence.
- It is not private.
- It does not conflict with active memory unless handled.

### 10.4 Update

Never silently overwrite memory. Use event-sourced updates.

Update operations:

- `duplicate`: attach new source, raise confidence.
- `reinforce`: update last_seen_at and confidence.
- `supersede`: mark old memory as superseded and link the new memory.
- `conflict`: keep both, mark conflict, block default injection.
- `stale`: reduce confidence or require verification.
- `delete`: tombstone, do not hard delete by default.

### 10.5 Cleanup

Cleanup should run periodically:

- Expire old short-term task contexts.
- Decay low-confidence memories.
- Mark memories stale when related files change (see §10.6 for the concrete mechanism).
- Surface unresolved conflicts.
- Prune low-value memories with no retrieval usage.

### 10.6 Changed-Code Invalidation (mechanism)

This is a primary differentiator (no competitor combines note-style memory + code provenance + auto-staleness). Borrowed and simplified from `knowing`'s content-addressed staleness, dropping its Merkle/proof machinery:

- At write time, a memory that references a file stores `file_path` + `file_hash = sha256(file_bytes)`.
- At recall time, recompute the current hash of `file_path`. If it differs from the stored `file_hash`, surface the memory as `stale` (warn, do not hard-block — a memory assistant should flag, not refuse).
- No background watcher needed for the core case: it is write-time-store / read-time-compare. The optional `--watch` enhancement uses OS file-events (watchdog/inotify/kqueue) to push invalidation in real time.
- Expose an `engram verify` command that exits non-zero if any high-confidence memories are stale relative to current git HEAD (knowing's `knowing stale` CI-gate pattern).

Why not git diff / AST: hash-compare is O(1) per memory, needs no parser, and works across any language. Reserve AST-level invalidation for later if function-granularity proves necessary.

## 11. Retrieval Design

### 11.1 Retrieval Stages

1. **Candidate generation**
   Use FTS/BM25, vector search, and metadata filters.

2. **Filtering**
   Filter by project, file, memory type, status, agent, git SHA, branch, time range, and privacy visibility.

3. **Reranking**
   Combine lexical score, vector score, recency, confidence, source quality, file overlap, task overlap, and stale/conflict penalties.

4. **Context assembly**
   Build prompt-ready context under token budget.

5. **Trace**
   Log candidates, selected memories, ranking features, and token usage.

### 11.2 Progressive Disclosure

Agents should not fetch full memory first.

Workflow:

```text
memory_search(query)
  -> compact rows with IDs and titles

memory_timeline(anchor_id)
  -> surrounding events/summaries

memory_get(ids)
  -> full details only for chosen IDs

memory_context(query, token_budget)
  -> final injected memory context
```

Benefits:

- Lower token use.
- Better agent control.
- Easier evals.
- Easier debugging.
- Less stale context injection.

### 11.3 Context Assembly Priority

When building `memory_context`, priority should be:

1. Active short-term task context.
2. Directly relevant decisions.
3. Directly relevant project facts.
4. Relevant user preferences.
5. Relevant failure patterns.
6. Older session summaries.

Do not inject unresolved conflicts by default.

## 12. Evals and Observability

Evals are a core feature, not an afterthought.

### 12.1 Replay Eval Dataset

Each eval case should include:

```text
query
project_id
expected_memory_ids
expected_memory_types
must_not_include_ids
tags
```

Example:

```json
{
  "query": "continue the retrieval eval implementation",
  "expected_memory_ids": ["mem_decision_sqlite_source", "task_eval_next_step"],
  "must_not_include_ids": ["old_go_implementation_plan"],
  "tags": ["handoff", "evals", "stale"]
}
```

### 12.2 Metrics

Required:

- `Recall@5`: whether expected memories appear in top 5.
- `MRR`: rank quality for first relevant memory.
- `stale_injection_rate`: stale memories injected / total injected memories.
- `conflict_injection_rate`: conflicting memories injected / total injected memories.
- `avg_injected_tokens`: context cost.
- `abstain_rate`: cases where Engram correctly returns no memory.

Optional:

- task success delta.
- repeated context token reduction.
- latency p50/p95.

### 12.3 Tracing

Each retrieval should produce a trace:

```text
query
candidate ids
selected ids
scores
filters
reranker features
stale/conflict decisions
token budget
final context
```

This supports the resume bullet about evals and tracing.

## 13. Privacy and Safety

### 13.1 Private Tags

Support tags such as:

```text
<private>...</private>
<do-not-store>...</do-not-store>
```

Content inside these tags should not enter events, summaries, memories, or indexes.

### 13.2 Secret Detection

Detect obvious secrets:

- API keys.
- OAuth tokens.
- Private keys.
- `.env` values.
- Password-like values.

Default behavior:

- Store redacted event metadata if needed.
- Do not store raw secret content.
- Add trace note that content was redacted.

### 13.3 Conflict Safety

If two active memories conflict:

- Do not inject either by default unless one is clearly superseded.
- Return a conflict warning in traces.
- Ask user or run verification if needed.

## 14. Integration Plan

### 14.1 Claude Code

Primary integration path:

- MCP stdio server.
- **Transcript JSONL is the source of truth for capture, not hooks.** Claude Code already writes a complete append-only transcript to `~/.claude/projects/<encoded-cwd>/*.jsonl` containing every user/assistant message, `tool_use`, `tool_result`, **subagent (Task) launch + result with AgentID/AgentType/token counts**, skill, permission, and hook record. Engram tails this file (byte-offset bookkeeping, watch the directory not a single file) and treats it as authoritative.
- **Hooks are a secondary signal**, used only for real-time triggering and session boundaries (SessionStart / Stop / PreCompact), not as the primary record. Evidence: a hook-only design (claude-mem) misses subagent calls entirely (no `SubagentStop`) and silently drops events on worker timeout; JSONL-tailing tools (cctrace, TMA1, claudewatch) capture subagents correctly. The earlier draft had this priority reversed.
- `encodeClaudeProjectPath(cwd)`: replace `/` -> `-` and `.` -> `-`, then pick transcript files in that dir.
- See §26 for the full capture-completeness + reconciliation design.

### 14.2 Codex

Integration path:

- MCP server config.
- Hook or transcript watcher if available.
- Normalize Codex events into Engram event schema.

### 14.3 Cursor

Integration path:

- MCP config.
- Optional adapter for session logs or exported context.
- Cursor support is useful for ATS, but should not dominate implementation.

### 14.4 Ollama/Local Agents

Integration path:

- Generic MCP client.
- Manual `session_start`, `record_event`, and `memory_context` calls.

## 15. Feature Roadmap

### P0: Resume-Grade MVP

Goal: prove the core memory infrastructure works end to end.

Features:

- Python MCP server.
- SQLite source-of-truth database.
- Session start/end tools.
- Event ingestion.
- Short-term task contexts.
- Session summaries.
- Long-term memory table.
- Manual and automatic consolidation.
- FTS/BM25 retrieval.
- `memory_search`, `memory_timeline`, `memory_get`, `memory_context`.
- Basic replay eval runner with Recall@5 and MRR.
- Retrieval traces.
- README with demo workflow.

### P1: AI Engineering Depth

Goal: make it clearly stronger than a vector DB demo.

Features:

- Vector search.
- Hybrid retrieval with score fusion.
- Metadata filters.
- Provenance and citations.
- Stale detection.
- Conflict detection.
- Privacy tags.
- Memory update/supersede workflow.
- Claude Code, Codex, and Cursor adapters.
- Token-budgeted context assembly.
- Evals for stale and conflict injection.

### P2: Product Polish

Goal: improve usability without scope explosion.

Features:

- Simple local viewer.
- Import/export.
- Corpus generation for evals.
- Memory decay.
- Admin cleanup commands.
- HTTP API.
- Docker compose.
- Optional Postgres + pgvector backend.

## 16. Implementation Milestones

### Milestone 1: Scaffold

- Create Python package.
- Add MCP server entrypoint.
- Add SQLite migrations.
- Add config loading.
- Add basic test setup.

Deliverable:

```text
engram --help
engram mcp
```

### Milestone 2: Event Store

- Implement `session_start`.
- Implement `record_event`.
- Implement `session_end`.
- Persist sessions and events.
- Add dedup by content hash.

Deliverable:

```text
Can record a coding-agent session and inspect events in SQLite.
```

### Milestone 3: Memory Consolidation

- Generate session summaries.
- Create short-term task contexts.
- Extract long-term candidates.
- Add manual approval or confidence policy.

Deliverable:

```text
Raw session events become task handoff + durable memory candidates.
```

### Milestone 4: Retrieval

- Add FTS/BM25 search.
- Add `memory_search`.
- Add `memory_timeline`.
- Add `memory_get`.
- Add `memory_context`.

Deliverable:

```text
Agent can retrieve compact index, inspect timeline, and fetch full memories.
```

### Milestone 5: Evals

- Add eval case format.
- Add replay runner.
- Compute Recall@5 and MRR.
- Persist eval runs.
- Add trace output.

Deliverable:

```text
Retrieval quality can be measured and regressions can be diagnosed.
```

### Milestone 6: Reliability

- Add provenance.
- Add stale/conflict handling.
- Add privacy tags.
- Add token-budgeted context assembly.

Deliverable:

```text
Engram avoids injecting stale/private/conflicting memory by default.
```

### Milestone 7: Agent Integrations

- Claude Code setup docs.
- Codex setup docs.
- Cursor setup docs.
- Demo videos or terminal transcripts.

Deliverable:

```text
Same project memory can be used across Claude Code, Codex, and Cursor.
```

## 17. Technical Choices

### Python

Use Python because:

- Strong AI engineering ecosystem.
- Easy eval runner implementation.
- Good SQLite, FTS, and vector library support.
- Better fit for LLM tooling and benchmarking.
- User already has Go projects; Python adds profile diversity.

### SQLite First

Use SQLite in v1 because:

- Local-first.
- Easy install.
- Easy to inspect.
- Strong FTS5 support.
- Good enough for side project scale.
- Can be migrated to Postgres later.

**SQLite vs Postgres — evidence from competitors.** `nram` runs the full memory pipeline (FTS5 + in-process pure-Go HNSW for vectors, 59 migrations, multi-phase consolidation) on **SQLite by default** while also offering Postgres/Qdrant behind a `VectorStore` interface — proof that SQLite scales to this workload. `stash` is Postgres-only, but only because it inlines `pgvector`'s `<=>` cosine operator directly into SQL WHERE/JOIN during consolidation and needs multi-table transactional consolidation — **neither requirement applies to Engram**. The "many agent-memory projects use Postgres" pattern comes from hosted multi-tenant SaaS (concurrency, RBAC, horizontal scale), not local-first per-developer tools. Decision: **stay SQLite**; keep storage behind an interface so Postgres is a P2 swap, not a rewrite.

**Known cost / risk of the SQLite route:** vector ops are not SQL-integrated — a dedup/recall is embed -> `HNSW.Search()` -> `SQL WHERE id IN (...)`, one extra in-process round-trip vs stash's single pgvector query. Negligible at 10k–100k memories. The one real engineering risk is keeping the HNSW index consistent with SQLite under concurrent writes: use WAL mode + serialized HNSW updates.

### Optional Vector Store

Start with SQLite FTS/BM25. Add vector as P1.

Options:

- sqlite-vec.
- Chroma.
- Qdrant.
- Postgres + pgvector.

Preferred progression:

```text
SQLite FTS -> sqlite-vec or Chroma -> optional Postgres/pgvector
```

### LLM Use

Use LLMs for consolidation, not for every retrieval.

LLM tasks:

- Session summary.
- Candidate extraction.
- Preference extraction.
- Conflict explanation.

Non-LLM tasks:

- Dedup.
- FTS.
- Metadata filtering.
- Basic scoring.
- Eval metrics.
- Confidence decay.

## 18. Reference Projects and Lessons

### claude-mem

Useful lessons:

- Persistent memory across sessions.
- Lifecycle hooks.
- SQLite source of truth.
- Chroma/vector sync.
- Progressive disclosure: search -> timeline -> get.
- Web viewer and citations.
- Privacy tags.

What not to copy in v1:

- Large installer surface.
- Broad platform integrations.
- OpenClaw/Gemini/OpenRouter distribution scope.
- Large UI.

Engram differentiation:

- Stronger retrieval evals.
- Stale/conflict injection metrics.
- Memory promotion policy.
- Explicit short-term vs long-term memory model.

### Stash

Useful lessons:

- Separate raw episodes, consolidated facts, and short-lived context.
- Consolidate periodically rather than constantly.
- Store preferences, constraints, decisions, corrections, project facts, goals, failures, and useful summaries.
- Treat active context as short-lived.
- Include contradiction detection and confidence decay.

Engram adaptation:

- `events` correspond to episodes.
- `memories` correspond to facts/preferences/decisions.
- `task_contexts` correspond to short-lived active context.
- Consolidation worker should extract durable memory from raw event history.

### nram, distill, knowing, tma1

General lessons from prior research:

- Hybrid lexical/vector retrieval matters.
- Provenance and changed-code invalidation are strong differentiators.
- Memory quality and evals are more valuable than another generic RAG interface.
- Observability and traces make the project more SDE2/AI-engineer aligned.

## 19. Demo Scenario

### Scenario

1. User starts in Claude Code:

```text
Implement memory replay evals for Engram.
```

2. Claude Code records events:

- Files read.
- Schema decision.
- Test failure.
- Partial implementation.

3. Session ends:

Engram writes:

- Short-term handoff: next step is MRR implementation.
- Long-term decision: SQLite is source of truth; vector index is derived.

4. User opens Codex next day:

```text
Continue the Engram eval work.
```

5. Engram injects:

- Active task context.
- Relevant decisions.
- Failed command.
- User preference.

6. Codex continues without re-reading all context.

7. Eval runner measures:

- Whether expected memory IDs appeared in top 5.
- How many stale memories were injected.
- How many tokens were used.

## 20. Resume Mapping

Current target bullets:

```text
Built an MCP memory harness that hands off session state between AI agents, cutting repeated context tokens ~40%.

Engineered Claude Code/Codex/Cursor memory consolidation from session events into short-term task context and long-term decisions, project facts, and preferences.

Built token-aware memory retrieval with search/timeline/get workflows, replay evals, and traces to track Recall@5/MRR and stale context injection.
```

Important constraint:

The `~40%` token reduction must be benchmarked before being claimed as a real metric. Until measured, use:

```text
reducing repeated context across sessions
```

**Benchmark methodology (reproducible, no LLM calls — adapted from graymatter's `benchmarks/token_count`):**

1. Baseline ("no-memory agent"): re-inject full CLAUDE.md + last N conversation turns every session. Count every injected token.
2. Engram path: a single `memory_context(task, max_tokens=800)` call. Count returned tokens.
3. Token counting: `tokens ≈ word_count × 1.6` (graymatter uses 1.33 for prose; bumped up because code token density is higher) — or use tiktoken directly.
4. Corpus: **real Claude Code sessions (50–200 exchanges)**, not synthetic data. Shuffle insertion order to remove recency bias.
5. Report the reduction curve at session 1 / 10 / 30 / 100 — savings grow with session count. A single headline number is less defensible than the curve.

Honesty notes on the number: graymatter measured 90% but on a toy 100-record non-coding scenario; remindb's 82–99.8% is not reproducible from its own test suite and its 99.8% only counts context-gathering overhead. A conservative, defensible claim for a coding agent is **30–50% on repeated cross-session context** — and only after running the benchmark above.

## 21. Interview Talking Points

### Why not just vector search?

Because vector search alone cannot decide whether memory is stale, conflicting, private, or safe to inject. Engram uses a source-of-truth event store, metadata filters, provenance, and evals around retrieval.

### Why separate short-term and long-term memory?

Short-term memory keeps the active task resumable. Long-term memory keeps durable project and user knowledge reusable. Mixing them pollutes retrieval with temporary branch state.

### Why evals?

Memory injection can hurt agents if it retrieves the wrong context. Replay evals prove whether the memory system retrieves the expected memories and avoids stale/conflicting memories.

### Why progressive disclosure?

Fetching full memories first wastes tokens. A search -> timeline -> get workflow lets the agent inspect low-cost candidates before pulling full details.

### Why source-of-truth SQLite?

It makes memory auditable and replayable. Vector indexes are useful for retrieval but should be rebuildable derived state, not the only durable memory store.

## 22. Success Criteria

The project is successful when:

- A session can be recorded from at least one coding agent.
- Another session can resume from Engram context.
- Short-term and long-term memories are visibly separated.
- Retrieval uses search -> timeline -> get.
- Evals compute Recall@5 and MRR.
- Traces explain why memory was injected.
- Stale/conflicting/private memories are not injected by default.
- Documentation shows Claude Code, Codex, and Cursor integration plan.

## 23. Risks

### Scope creep

Risk: building a giant platform instead of a strong memory core.

Mitigation: keep P0 focused on MCP, event store, consolidation, retrieval, evals.

### No reliable metrics

Risk: resume claims are not defensible.

Mitigation: implement replay evals before claiming token reduction or task success improvements.

### Memory pollution

Risk: storing every event as long-term memory makes retrieval worse.

Mitigation: use promotion policy, confidence, TTL, and cleanup.

### Agent integration complexity

Risk: each agent has different hooks and session formats.

Mitigation: normalize through adapter layer and start with one primary platform.

### Stale memory

Risk: old project facts mislead agents.

Mitigation: valid_from/valid_until, supersede links, file-change invalidation, and stale injection evals.

## 24. Open Questions

1. Should v1 use SQLite FTS only, or include vector search from the start?
   → Leaning FTS5/BM25 only in P0; hybrid FTS5+vector+RRF in P1. Coding agents need BM25 for exact identifiers (function names, error codes, file paths) — pure-vector recall (stash's weakness) misses these. (See §27.)
2. Which platform should be the first full integration: Claude Code or Codex?
   → **Claude Code.** It is the only platform whose official transcript JSONL gives full, verifiable capture (incl. subagents). Codex/Cursor are "best-effort" (§26). Committing to cross-platform parity in P0 is poor ROI.
3. Should consolidation require user approval for long-term memory candidates?
   → Default no-LLM-on-write; consolidate lazily (§27 two-phase). Surface candidates, auto-promote high-confidence, queue ambiguous ones — avoid blocking the write path.
4. Should preferences be global user scope or project scope by default?
   → Open. nram's `about_me` (global persona) + per-project split is a reasonable model to copy.
5. What benchmark will justify the `~40%` repeated-context token reduction claim?
   → **Resolved** — see §20 benchmark methodology (graymatter-derived, reproducible). Report the per-session-count curve, claim 30–50% conservatively until measured.
6. Should the simple viewer be included in P1 or delayed to P2?
   → P2. An `engram doctor` health-check CLI (graymatter pattern) is higher-value than a viewer for P1.

## 25. Recommended First Build

Build this first:

```text
Python MCP server
SQLite event store
session_start / record_event / session_end
short-term task_contexts
long-term memories
memory_search / memory_timeline / memory_get
replay eval runner with Recall@5 and MRR
trace JSON output
```

Do not build UI first. Do not build all integrations first. Do not start with a memory graph.

The strongest first demo is:

```text
Claude Code records a half-finished task.
Codex resumes it.
Engram retrieves the right handoff and decisions.
Replay eval proves the expected memories are in top 5.
Trace explains why they were injected.
```

## 26. Capture Completeness and Verification

"Nothing is lost when handing off between agents" is a per-adapter guarantee, not a universal one. It must be **provable**, not assumed. No surveyed competitor (claude-mem, cctrace, TMA1, agenttrace, claudewatch) verifies capture completeness — they all trust the JSONL transcript is complete. Engram's verification layer is therefore a genuine differentiator.

### 26.1 Two distinct accuracy numbers (do not conflate)

- **Capture completeness** — was every event (tool/git/subagent) recorded? On Claude Code with the JSONL-as-source-of-truth design: **~99–100%, and provable** via sequence reconciliation.
- **Retrieval accuracy** — was the *right* memory injected? This is the eval-measured `Recall@5`/`MRR` of §12. Not a capture property; never report it until measured.

### 26.2 The four mechanisms (combine all; no competitor does all four)

1. **Official JSONL as source of truth** (cctrace/TMA1) — tail `~/.claude/projects/<encoded-cwd>/*.jsonl`. git is a subset of `Bash` tool records; subagents appear as `Task` tool records with full metadata — both covered for free.
2. **`raw_ref{file, byte_offset}` on every event** (cctrace) — deterministic replay and audit.
3. **Monotonic `seq` per session + reconcile the range at `session_end`** (*novel — nobody does this*) — a gap in the sequence is provable evidence of a drop. Mark such a session `capture_incomplete` and exclude it from consolidation.
4. **`pending` map** (claudewatch) — a `tool_use` with no matching `tool_result` after a full scan = an interrupted/incomplete span; flag it at handoff.

Defensive details to copy: truncate a tail read at the last `\n` to avoid parsing a half-written line (claudewatch `readLiveJSONL`); watch the **directory**, not a single file, so a mid-run new session file is not missed (cctrace's bug); persist the byte offset so a restart resumes without reprocessing.

### 26.3 Honest boundaries

- **Cross-platform is not equal.** Codex transcripts are weaker; Cursor exposes little. Capture completeness is guaranteed only for Claude Code (P0); Codex/Cursor are explicitly "best-effort."
- **Consolidation is lossy by design.** events -> summary discards detail. Compensation: raw events are append-only and replayable, so detail is recoverable even though the summary is compressed.
- **Working definition of "not lost":** authoritative full ingest from the official JSONL + sequence reconciliation that can *detect* gaps + replayable raw events — **not** a promise of zero loss.

## 27. Reference Implementations (borrowed patterns)

Concrete, source-verified patterns to copy, with origin repo. Prioritized; "do not copy" list at the end.

### 27.1 High value (P0/P1)

- **search -> timeline -> get progressive disclosure** (claude-mem) — already in §8; keep. Search returns IDs/titles only (~50–100 tok), get fetches full detail only for chosen IDs.
- **Write-time dedup, two layers, no LLM on the write path:** (1) exact `content_hash` match first — zero cost, catches 100% of exact dups before embedding (nram migration 000018); (2) vector cosine band — `< 0.15` = duplicate (bump `access_count`, skip insert), `0.15–0.35` = conflict (insert + flag), `> 0.35` = independent (distill `findSimilar`). Do **not** run an LLM ADD/UPDATE/DELETE judge on every write (nram does — too expensive for chatty coding agents).
- **`superseded_by` soft-delete + forward pointer** (distill/nram) — one column handles update/delete/supersede chains; empty pointer = expire-without-replacement.
- **Hybrid FTS5 + vector + RRF fusion** (graymatter `recall.go`, nram) — RRF with k=60; recency gets half weight. Coding agents *need* BM25 for exact identifiers; pure-vector recall (stash) misses function names/error codes/paths.
- **`origin` field** (user/extracted/synthesized) (nram) — required to compute `stale_injection_rate`: you can measure what fraction of recalled memories are machine-synthesized.
- **file-hash staleness** (knowing) — see §10.6.
- **Two-phase consolidation** (nram): fast queue-based write-time enrichment + lazy idle-time batch ("dreaming"). Add a **write-count trigger** in addition to the idle gate, or consolidation never fires for an always-busy agent (nram's gap).

### 27.2 Medium value (P1/P2)

- **`mie_analyze`-style pre-write tool** (mie) — before storing, surface what's already known so the agent decides add/update/skip; cuts duplicate writes at the source.
- **Decay with access reinforcement** (graymatter/nram) — `confidence *= (1 - rate)` past an age window, but recall resets/raises it. distill's tiered decay (Full -> Summary -> Keywords -> Evict) is elegant but its keyword stage destroys meaning for code ("ban var" -> loses "var"); adapt before using.
- **`fact` triple (entity, property, value)** (stash) — structured recall beyond fuzzy match; maps to decisions/preferences/facts.
- **Delta sync via cursor + snapshot id** (remindb `MemoryDelta`) — agent reports last position, gets only what changed across sessions.
- **`engram doctor` health-check CLI** (graymatter) — validates SQLite, MCP wiring, file hashes in one command. Higher P1 value than a viewer.
- **`node_type` column** (mie taxonomy: fact/decision/preference/constraint) — typed recall and per-type decay, *without* adopting a graph DB.

### 27.3 Do not copy

- **mie's CozoDB/Datalog graph** — Rust+CGo deploy cost is high; its conflict detection is a separate query, not write-time, which is dangerous for a coding agent (two contradictory lint rules cause harm between writes). Take only the typed-node *concept*.
- **claude-mem's mandatory Chroma + always-on HTTP worker** — heavy, network failure mode, silent drop on timeout. Use SQLite FTS5 + optional in-process embeddings instead.
- **distill's O(N²) full-table-scan dedup** — replace with an ANN (sqlite-vec/HNSW) index from day one.
- **nram's multi-tenant OAuth/WebAuthn/RBAC** — irrelevant to a local single-agent tool.
- **stash's pure-vector recall (no BM25)** and **Postgres-only** storage.
- **remindb's custom TOON compression** — modest gains on irregular code text; not worth the parser.
