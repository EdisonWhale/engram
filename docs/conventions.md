# Engram — Naming & Layout Conventions

The single source of truth for *where code goes* and *what to call things*. Read this before
adding a file or package. It codifies conventions already in the tree (it does **not** invent new
abstractions — see CLAUDE.md "Code style": deep modules, not more components).

Mechanically enforced where possible: `ruff` lint rule `N` (pep8-naming) checks identifier names;
the rest is reviewed against this doc.

---

## 1. Repository layout

```
engram/
├── src/engram/
│   ├── __init__.py
│   ├── cli.py                  # `engram` entrypoint: mcp | eval | doctor (thin; delegates)
│   ├── models.py               # ALL pydantic table models (spec §9.1) — one file, the contract
│   ├── db/                     # connection + schema (no business logic)
│   │   ├── runner.py           # open_db(), migration runner
│   │   └── migrations/         # NNN_name.sql, applied in lexicographic order
│   ├── store/                  # the ONE upfront abstraction (ADR 0001, P2 Postgres swap)
│   │   ├── base.py             # EventStore / MemoryStore / VectorStore Protocols
│   │   └── sqlite_store.py     # SQLite{Event,Memory}Store impls
│   ├── mcp/
│   │   └── server.py           # FastMCP stdio server; tool layer (ADR 0006)
│   ├── capture/                # WS-A — transcript → events (spec §5.2, §14.1, §26)
│   ├── consolidation/          # WS-B — events → memories (spec §10)
│   ├── retrieval/              # WS-C — query → ranked memories + context (spec §11)
│   └── eval/                   # WS-D — replay evals + trace reporting (spec §12)
├── tests/
│   ├── test_<module>.py        # mirrors the module under test
│   └── fixtures/               # recorded transcripts & gold data (see §4)
└── docs/
    ├── engram-development-spec.md   # source of truth for WHAT to build
    ├── conventions.md               # this file — source of truth for WHERE/NAMING
    ├── capture-schema.md            # transcript JSONL → events contract (WS-A)
    ├── adr/NNNN-title.md            # architecture decision records
    └── tasks/<WS>-<name>.md         # per-workstream task specs
```

### Package home per spec §7.1 component

A new file's home is decided by which **seam** (CLAUDE.md) it belongs to — not by what feels tidy.
Do not add a layer *inside* a seam; deepen the existing module instead.

| Spec §7.1 component        | Package                | Owner |
|----------------------------|------------------------|-------|
| MCP Server / tool layer    | `engram/mcp/`          | all (stubs in WS-0) |
| Event/Memory/Vector Store  | `engram/store/`        | WS-0 (frozen) |
| Connection + migrations    | `engram/db/`           | WS-0 (frozen) |
| Agent Adapter Layer        | `engram/capture/adapters/` | WS-A |
| Tailer + ingest + reconcile| `engram/capture/`      | WS-A |
| Consolidation Worker       | `engram/consolidation/`| WS-B |
| Retrieval Engine + Context Assembler | `engram/retrieval/` | WS-C |
| Eval Runner + Trace Logger | `engram/eval/`         | WS-D |

Create a workstream's package only when its workstream starts (no empty `__init__.py` placeholders).
Each package is a deep module with a small public surface; export it from the package `__init__.py`
with an explicit `__all__`, like `engram/store/__init__.py`.

---

## 2. Python naming

| Kind | Rule | Example |
|------|------|---------|
| Module / file | `lower_snake_case.py`, singular noun for a concept | `sqlite_store.py`, `models.py` |
| Package | `lower_snake_case`, singular | `capture/`, `retrieval/` |
| Class (model / dataclass) | `PascalCase`, singular | `Event`, `AgentSession`, `TaskContext` |
| Storage Protocol | `<Role>Store` | `EventStore`, `MemoryStore` |
| Storage impl | `<Backend><Role>Store` | `SQLiteEventStore`, `PostgresMemoryStore` (P2) |
| Function / method | `verb_noun`, `lower_snake_case` | `create_event`, `max_seq_for_session` |
| Store method verbs | `create_` / `get_` / `list_` / `update_`; no `save`/`fetch`/`find` | `list_session_events` |
| Boolean | `is_` / `has_` / `<state>` predicate | `is_stale`, `capture_incomplete` |
| Constant | `UPPER_SNAKE_CASE` at module scope (never function-local — N806) | `_DEFAULT_DB` |
| Private helper | leading underscore | `_apply_migrations`, `_iso`, `_jdump` |
| MCP tool | `lower_snake_case`, `noun_verb` grouped by noun | `memory_search`, `session_start` |
| MCP params model | `<ToolName>Params` (PascalCase of the tool) | `SessionStartParams` |
| Pydantic enum | `PascalCase` type, value strings `lower_snake_case` | `MemoryStatus.superseded` |

Module-private names start with `_`; the public API of a package is exactly its `__init__.__all__`.

---

## 3. Database & schema naming

| Kind | Rule | Example |
|------|------|---------|
| Table | `lower_snake_case`, **plural** | `events`, `memories`, `task_contexts` |
| Column | `lower_snake_case`, singular | `project_id`, `content_hash`, `valid_from` |
| Primary key | `id` (UUID text) | |
| Foreign key | `<referent_singular>_id` | `session_id`, `memory_id` |
| JSON-encoded column | suffix `_json` (deserialized name drops it in the model) | `metadata_json` → `metadata` |
| Timestamp | `<verb>_at` (RFC3339 UTC `Z`) or `valid_from`/`valid_until` for ranges | `created_at`, `ended_at` |
| Index | `idx_<table>_<cols>` | `idx_events_session_seq` |
| FTS5 virtual table | `<table>_fts`; sync triggers `<table>_fts_<ai\|ad\|au>` | `memories_fts`, `memories_fts_ai` |
| Migration file | `NNN_description.sql`, zero-padded, append-only (never edit an applied one) | `001_initial.sql` |

`events` is append-only (CLAUDE.md). Memory edits go through `superseded_by` / status, never hard
delete. Controlled vocabularies (`event_type`, `source_type`, `memory.status`, `capture_confidence`)
are defined once in `models.py` enums and referenced everywhere — do not inline string literals.

---

## 4. Tests & fixtures

- One test module per source module: `tests/test_<module>.py` (`test_sqlite_store.py`).
- Test functions: `test_<unit>_<behavior>` (`test_create_event_assigns_monotonic_seq`).
- Recorded fixtures live in `tests/fixtures/`: transcript JSONL under
  `tests/fixtures/transcripts/`, gold retrieval/eval data under `tests/fixtures/gold/`.
- Per CLAUDE.md, force test-first on: the transcript parser/state-machine, the supersede/conflict
  state machine, and eval metric math. Retrieval quality is judged by replay **evals**, not unit
  tests (spec §12).

---

## 5. Docs & git

| Kind | Rule | Example |
|------|------|---------|
| ADR | `docs/adr/NNNN-kebab-title.md`, 4-digit, immutable once accepted | `0001-sqlite-source-of-truth.md` |
| Task spec | `docs/tasks/<WS>-<name>.md` (`00-` for scaffold) | `A-capture.md` |
| Feature branch | `ws-<id>-<name>` per workstream (CLAUDE.md), `fix/<slug>` otherwise | `ws-a-capture` |
| Commit subject | `WS-<id>: <imperative summary>` while building workstreams | `WS-0: scaffold and contract` |

Never commit to `main`; never auto-push to `main` (CLAUDE.md). Subagents return diffs; the main
agent integrates.
